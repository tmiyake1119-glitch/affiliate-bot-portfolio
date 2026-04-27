import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

# ──────────────────────────────────────────────────────────────────────
# 価格フィルター設定
#   .env で上書き可能（VPS の .env に追記するだけで変更できる）
#   PRICE_FILTER_MIN: 下限（円）デフォルト 500
#   PRICE_FILTER_MAX: 上限（円）デフォルト 5000
# ──────────────────────────────────────────────────────────────────────
_PRICE_FILTER_MIN: int = int(os.getenv("PRICE_FILTER_MIN", "500"))
_PRICE_FILTER_MAX: int = int(os.getenv("PRICE_FILTER_MAX", "5000"))

from trend_agent import get_trending_products
from keyword_agent import get_seasonal_boost
from seasonal_content import get_seasonal_boost as get_genre_seasonal_boost
from database import (
    get_all_learning_data, get_all_price_history_as_dict,
    get_recent_posted_urls, get_recent_posted_titles,
    get_all_follower_history, get_recent_post_genres,
)

logger = logging.getLogger("affiliate_bot.product_selector")

COMMISSION_MULTIPLIERS: dict[str, float] = {
    "food":        1.0,
    "daily_goods": 1.0,
    "health":      1.2,
    "electronics": 0.9,
    "alcohol":     1.3,
    "wine":        1.2,
    "pet":         1.2,
    "outdoor":     1.2,
    "cosmetics":   1.1,
    "kitchen":     1.0,
    "furusato":    1.3,
    "contact":     1.1,
}

# CTR優先度ウェイト（デフォルト / main アカウント）
_CTR_WEIGHT: dict[str, float] = {
    "alcohol":     1.3,
    "electronics": 1.2,
    "daily_goods": 1.1,
    "kitchen":     1.1,
    "wine":        1.1,
    "food":        0.7,   # 食品（レトルト等）: 説明が必要で低CTR
    "furusato":    0.3,   # ふるさと納税: 年間1回限りでリピートなし・低CTR
}

# ④ osake アカウント: ウイスキー・高額帯優先、食品フィルタ無効
_CTR_WEIGHT_OSAKE: dict[str, float] = {
    "alcohol":     1.5,
    "wine":        1.4,
    "food":        1.0,   # 食品フィルタ無効
    "furusato":    1.0,
    "electronics": 0.8,
    "daily_goods": 0.8,
}

# ④ hobby アカウント: 家電・ゲーム周辺優先、転売品は price_selector で除外済み
_CTR_WEIGHT_HOBBY: dict[str, float] = {
    "electronics": 1.4,
    "outdoor":     1.2,
    "daily_goods": 1.0,
    "food":        0.6,
    "furusato":    0.6,
    "alcohol":     0.7,
}

_ACCOUNT_WEIGHTS: dict[str, dict[str, float]] = {
    "mono_zukan_jp":  _CTR_WEIGHT,
    "osakezukan_jp":  _CTR_WEIGHT_OSAKE,
    "hobbyzukan_jp":  _CTR_WEIGHT_HOBBY,
}

# Anomaly class constants
_FORCED   = 3   # 強制採用
_PRIORITY = 2   # 優先
_NORMAL   = 1   # 通常
_EXCLUDE  = 0   # 転売除外


def load_genre_scores() -> dict[str, float]:
    """Return avg engagement score per genre from learning_data DB."""
    data = get_all_learning_data()
    totals: dict[str, list[int]] = {}
    for entry in data:
        genre = entry.get("genre")
        score = entry.get("engagement", 0)
        if genre:
            totals.setdefault(genre, []).append(score)
    return {genre: sum(scores) / len(scores) for genre, scores in totals.items()}


def _get_price_stats(url: str, history: dict) -> dict:
    """Return min_30d and avg_30d from time-series price history.

    Handles both legacy single-point format {price, timestamp}
    and new array format [{price, timestamp}, ...].
    Returns None values when insufficient data.
    """
    entry = history.get(url)
    if not entry:
        return {"min_30d": None, "avg_30d": None}

    # Legacy single-point format
    if isinstance(entry, dict):
        p = entry.get("price")
        return {"min_30d": p, "avg_30d": p}

    # Array format — filter to last 30 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    recent: list[int] = []
    for point in entry:
        try:
            ts = datetime.fromisoformat(point["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff and point.get("price"):
                recent.append(point["price"])
        except (KeyError, ValueError):
            continue

    if not recent:
        return {"min_30d": None, "avg_30d": None}

    return {
        "min_30d": min(recent),
        "avg_30d": sum(recent) / len(recent),
    }


def _classify_price(current: int, stats: dict) -> tuple[int, float]:
    """Return (anomaly_class, discount_rate) based on price anomaly rules.

    ① discount_rate >= 0.15        → FORCED
    ② current < min_30d            → FORCED
    ③ current < avg_30d * 0.8      → PRIORITY
    ④ price >= 10000 & rate >= 0.1 → PRIORITY
    ⑤ current > avg_30d * 1.3      → EXCLUDE (転売)
    銘柄は補助（スコア加点のみ）
    """
    avg_30d = stats.get("avg_30d")
    min_30d = stats.get("min_30d")

    if avg_30d is None or avg_30d == 0:
        return _NORMAL, 0.0

    discount_rate = (avg_30d - current) / avg_30d

    # ⑤ Reseller exclusion — check first
    if current > avg_30d * 1.3:
        return _EXCLUDE, discount_rate

    # ① 15%+ discount
    if discount_rate >= 0.15:
        return _FORCED, discount_rate

    # ② New 30-day minimum
    if min_30d is not None and current < min_30d:
        return _FORCED, discount_rate

    # ③ 20%+ below average
    if current < avg_30d * 0.8:
        return _PRIORITY, discount_rate

    # ④ High-price tier with 10%+ discount
    if current >= 10000 and discount_rate >= 0.1:
        return _PRIORITY, discount_rate

    return _NORMAL, discount_rate


def _normalize_title(name: str) -> str:
    """商品名を正規化して返す（post_logs.title と同じ形式）。

    post_logs.title は threads_poster が post["title"][:60] で保存するため、
    先頭60文字に切り出したあとスペース除去・小文字化する。
    """
    import re as _re
    truncated = name[:60]
    return _re.sub(r"[\s\u3000]+", "", truncated).lower()


def _load_strategy_hints() -> tuple[str | None, str | None]:
    """
    daily_strategy.json から (top_genre, avoid_genre) を返す。
    ファイルなし・今日の日付でない場合は (None, None)。
    """
    try:
        from strategy_agent import load_today_strategy
        strategy = load_today_strategy()
        if strategy:
            return strategy.get("top_genre"), strategy.get("avoid_genre")
    except Exception:
        pass
    return None, None


def _load_selection_strategy() -> dict | None:
    """data/strategy_for_selection.json を読み込む（存在しない場合は None）。"""
    path = os.path.join(os.path.dirname(__file__), '..', 'data', 'strategy_for_selection.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _get_current_follower_count() -> int:
    """follower_history から最新の Threads フォロワー数を返す（DB 参照のみ・API 呼び出しなし）。"""
    try:
        history = get_all_follower_history()
        if history:
            val = history[-1].get("threads")
            return int(val) if val else 1
    except Exception:
        pass
    return 1


def select_top_products(products: list[dict],
                        min_price: int | None = None,
                        max_price: int | None = None,
                        top_n: int = 10,
                        account_name: str | None = None) -> list[dict]:
    """トレンド商品リストからスコアリングで上位 top_n 件を返す。

    min_price / max_price を省略した場合は .env の PRICE_FILTER_MIN / PRICE_FILTER_MAX
    (デフォルト 500円 / 5000円) が適用される。
    """
    # .env の値をデフォルトとして使用（引数で明示的に渡された場合はそちらを優先）
    effective_min = min_price if min_price is not None else _PRICE_FILTER_MIN
    effective_max = max_price if max_price is not None else _PRICE_FILTER_MAX

    # ── 価格フィルター ────────────────────────────────────────────────
    # 下限・上限の両方を適用し、除外件数をログに出力する
    before_price = len(products)
    filtered = [
        p for p in products
        if p.get("price") and effective_min <= p["price"] <= effective_max
    ]
    excluded_price = before_price - len(filtered)
    print(
        f"[ProductSelector] 価格フィルター ({effective_min}円〜{effective_max}円): "
        f"{excluded_price} 件除外 / 残り {len(filtered)} 件"
    )
    if excluded_price > 0:
        # 除外内訳（下限未満 / 上限超過）も出力
        below = sum(1 for p in products if p.get("price") and p["price"] < effective_min)
        above = sum(1 for p in products if p.get("price") and p["price"] > effective_max)
        print(f"[ProductSelector]   └ 下限未満(<{effective_min}円): {below}件 / 上限超過(>{effective_max}円): {above}件")

    price_history = get_all_price_history_as_dict()
    genre_scores  = load_genre_scores()
    max_genre_score = max(genre_scores.values(), default=1) or 1

    # 直近7日に投稿済みの URL を除外（重複投稿防止）
    recent_posted = get_recent_posted_urls(days=7)
    before_dedup = len(filtered)
    filtered = [p for p in filtered if p.get("url", "") not in recent_posted]
    if len(filtered) < before_dedup:
        print(f"[ProductSelector] 重複除外(URL): {before_dedup - len(filtered)} 件 (直近7日投稿済み)")

    # 商品名ベースの重複除外（別ショップ同一商品の重複投稿防止）
    recent_titles = get_recent_posted_titles(days=7)
    before_title_dedup = len(filtered)
    filtered = [p for p in filtered if _normalize_title(p.get("name", "")) not in recent_titles]
    if len(filtered) < before_title_dedup:
        print(f"[ProductSelector] 重複除外(商品名): {before_title_dedup - len(filtered)} 件 (別ショップ同一商品)")

    # 同一ラン内での商品名重複除外（別ショップ同名商品が複数選出されるのを防ぐ）
    seen_this_run: set[str] = set()
    before_run_dedup = len(filtered)
    unique_filtered: list[dict] = []
    for p in filtered:
        nt = _normalize_title(p.get("name", ""))
        if nt not in seen_this_run:
            seen_this_run.add(nt)
            unique_filtered.append(p)
    filtered = unique_filtered
    if len(filtered) < before_run_dedup:
        print(f"[ProductSelector] 重複除外(同一ラン): {before_run_dedup - len(filtered)} 件 (同名別ショップ)")

    # ジャンル偏り制御
    recent_genres = get_recent_post_genres(n=5)
    consecutive_exclude_genre: str | None = None
    freq_penalize_genres: set[str] = set()

    if len(recent_genres) >= 2 and recent_genres[0] == recent_genres[1]:
        consecutive_exclude_genre = recent_genres[0]
        logger.info("[ProductSelector] 連続ジャンル除外: %s (直近2件が同一)", consecutive_exclude_genre)
        print(f"[ProductSelector] 連続ジャンル除外: {consecutive_exclude_genre}")

    genre_counter = Counter(recent_genres)
    for genre, count in genre_counter.items():
        if count >= 3:
            freq_penalize_genres.add(genre)
    if freq_penalize_genres:
        logger.info("[ProductSelector] 頻度ペナルティ対象: %s (直近5件中3件以上)", freq_penalize_genres)
        print(f"[ProductSelector] 頻度ペナルティ(×0.5): {freq_penalize_genres}")

    # ジャンル均等化ボーナス（直近20件の出現回数が少ないジャンルを優先・最大+50%）
    recent_genres_20 = get_recent_post_genres(n=20)
    genre_count_20   = Counter(recent_genres_20)
    max_genre_count  = max(genre_count_20.values(), default=1)
    if genre_count_20:
        zero_genres = [g for g in ["alcohol", "pet", "outdoor", "cosmetics", "contact",
                                   "health", "kitchen", "electronics", "daily_goods",
                                   "food", "wine", "furusato"]
                       if genre_count_20.get(g, 0) == 0]
        if zero_genres:
            logger.info("[ProductSelector] 直近20件で出現0のジャンル: %s", zero_genres)
            print(f"[ProductSelector] 均等化ボーナス対象(0件): {zero_genres}")

    # 低パフォーマンス商品の除外（AB テスト評価結果を利用）
    try:
        from database import get_low_performance_products
        follower_count = _get_current_follower_count()
        low_perf_urls  = get_low_performance_products(follower_count)
        if low_perf_urls:
            before_lp = len(filtered)
            filtered = [p for p in filtered if p.get("url", "") not in low_perf_urls]
            if len(filtered) < before_lp:
                print(f"[ProductSelector] 低パフォーマンス除外: {before_lp - len(filtered)} 件")
    except Exception:
        low_perf_urls = set()

    # 選定戦略の読み込み（strategy_for_selection.json）
    sel = _load_selection_strategy()
    sel_priority_genres = set(sel.get("priority_genres", [])) if sel else set()
    sel_avoid_genres    = set(sel.get("avoid_genres", []))    if sel else set()
    sel_price_range     = sel.get("price_range", {})           if sel else {}
    sel_price_min       = sel_price_range.get("min", 0)
    sel_price_max       = sel_price_range.get("max", float("inf"))
    if sel:
        print(f"[ProductSelector] 選定戦略: 注力={list(sel_priority_genres)} / "
              f"除外={list(sel_avoid_genres)} / 価格{sel_price_min}-{sel_price_max}")

    # 今日の戦略ヒント
    strategy_top, strategy_avoid = _load_strategy_hints()
    if strategy_top:
        print(f"[ProductSelector] 戦略ヒント: 注力={strategy_top} / 回避={strategy_avoid}")

    buckets: dict[int, list[tuple[float, dict]]] = {
        _FORCED: [], _PRIORITY: [], _NORMAL: [],
    }

    for p in filtered:
        url     = p.get("url", "")
        p_genre = p.get("genre", "")

        # 連続ジャンル除外
        if consecutive_exclude_genre and p_genre == consecutive_exclude_genre:
            continue

        # 選定戦略: avoid_genres は完全除外
        if p_genre in sel_avoid_genres:
            continue

        stats = _get_price_stats(url, price_history)
        anomaly_class, discount_rate = _classify_price(p["price"], stats)

        if anomaly_class == _EXCLUDE:
            continue

        # Attach anomaly data for downstream use
        p["discount_rate"] = round(discount_rate, 4)
        p["price_avg_30d"] = stats.get("avg_30d")
        p["price_min_30d"] = stats.get("min_30d")

        # Genre score bonus (supplementary / 補助)
        genre_score  = genre_scores.get(p_genre, 0)
        genre_bonus  = 1 + (genre_score / max_genre_score) * (genre_score / 10)
        commission   = COMMISSION_MULTIPLIERS.get(p_genre, 1.0)
        review_bonus = 1.1 if p.get("review_average", 0.0) >= 4.0 else 1.0
        seasonal_b   = (1.0 + get_seasonal_boost(p.get("name", ""))) * get_genre_seasonal_boost(p_genre)
        weight_table = _ACCOUNT_WEIGHTS.get(account_name or "mono_zukan_jp", _CTR_WEIGHT)
        ctr_weight   = weight_table.get(p_genre, 1.0)

        # 戦略ヒント反映: top_genre +30% / avoid_genre -50%
        if strategy_top   and p_genre == strategy_top:   ctr_weight *= 1.3
        if strategy_avoid and p_genre == strategy_avoid: ctr_weight *= 0.5

        base_score = p["price"] * genre_bonus * commission * review_bonus * seasonal_b * ctr_weight

        # ジャンル頻度ペナルティ ×0.5
        if p_genre in freq_penalize_genres:
            base_score *= 0.5

        # ジャンル均等化ボーナス: 直近20件で出現が少ないほど最大+50%
        genre_appear_count = genre_count_20.get(p_genre, 0)
        diversity_bonus = 1.0 + (1.0 - genre_appear_count / max(max_genre_count, 1)) * 0.5
        base_score *= diversity_bonus

        # 選定戦略: priority_genres +10%
        if p_genre in sel_priority_genres:
            base_score *= 1.1

        # 選定戦略: price_range 外 -20%
        if sel_price_range:
            price = p["price"]
            if price < sel_price_min or price > sel_price_max:
                base_score *= 0.8

        buckets[anomaly_class].append((base_score, p))

    for bucket in buckets.values():
        bucket.sort(key=lambda x: -x[0])

    result = (
        [p for _, p in buckets[_FORCED]]
        + [p for _, p in buckets[_PRIORITY]]
        + [p for _, p in buckets[_NORMAL]]
    )
    return result[:top_n]


if __name__ == "__main__":
    print("Fetching trending products...")
    all_products = get_trending_products()
    print(f"Total fetched: {len(all_products)}\n")

    top_products = select_top_products(all_products)
    print(f"=== Top {len(top_products)} Products (price anomaly detection) ===")
    for i, p in enumerate(top_products, start=1):
        rate_str = f"{p.get('discount_rate', 0)*100:.1f}%OFF" if p.get('discount_rate') else "-"
        print(
            f"{i:>2}. [{p['genre']:12}] JPY{p['price']:>8,}  "
            f"{rate_str:>8}  {p['name'][:45]}"
        )
