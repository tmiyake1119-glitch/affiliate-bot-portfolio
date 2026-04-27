"""
trend_analyzer.py — 競合分析・トレンド検知エージェント

処理フロー:
  1. analyze_rakuten_trends()   — 12ジャンルの指標集計・急上昇検知
  2. detect_seasonal_trends()   — 季節キーワードの検索件数比較
  3. generate_trend_report()    — GPT-4o-mini でトレンドレポート生成
  4. apply_trend_to_strategy()  — daily_strategy.json に反映
  5. save_trend_history()       — trend_history.json に蓄積
  6. run_trend_analyzer()       — 上記を順番に実行 + Discord通知

main.py から: 日曜 9時JST (start.weekday()==6 and start.hour==0) のみ呼ぶ
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_RAKUTEN_APP_ID     = os.getenv("RAKUTEN_APP_ID", "")
_RAKUTEN_ACCESS_KEY = os.getenv("RAKUTEN_ACCESS_KEY", "")
_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

_DATA_DIR       = os.path.join(os.path.dirname(__file__), '..', 'data')
_TREND_HIST     = os.path.join(_DATA_DIR, 'trend_history.json')
_STRATEGY_PATH  = os.path.join(_DATA_DIR, 'daily_strategy.json')

_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_REPORT") or os.getenv("DISCORD_WEBHOOK_URL", "")

# 12ジャンル定義（trend_agent.py と同じキー）
_GENRES: dict[str, str] = {
    "food":        "食品 人気",
    "electronics": "家電 人気",
    "health":      "健康 人気",
    "daily_goods": "日用品 人気",
    "alcohol":     "ウイスキー 人気",
    "wine":        "ワイン 人気",
    "pet":         "ペット用品 人気",
    "outdoor":     "アウトドア キャンプ 人気",
    "cosmetics":   "コスメ 人気 プチプラ",
    "kitchen":     "キッチン用品 人気",
    "furusato":    "ふるさと納税 人気",
    "contact":     "コンタクトレンズ 人気",
}

# 急上昇検知のしきい値
_REVIEW_COUNT_RISE_THRESH = 0.20   # レビュー件数 +20%以上
_PRICE_DROP_RISE_THRESH   = 0.30   # 価格下落率 +30%以上
_KEYWORD_RISE_THRESH      = 0.30   # 検索件数 +30%以上


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _load_json(path: str, default=None):
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _avg(values: list) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


_RETRY_WAITS = [5, 10]  # 429 時のリトライ待機秒数


def _search_rakuten(keyword: str, hits: int = 10) -> dict:
    """
    楽天 IchibaItem/Search を呼んで raw JSON を返す。
    429 Too Many Requests の場合はバックオフしてリトライする。
    失敗時は空 dict を返す。
    """
    if not _RAKUTEN_APP_ID:
        return {}
    params = {
        "applicationId": _RAKUTEN_APP_ID,
        "accessKey":     _RAKUTEN_ACCESS_KEY,
        "keyword":       keyword,
        "hits":          hits,
        "sort":          "-reviewCount",
        "format":        "json",
        "formatVersion": 2,
        "itemTypeId":    1,   # 新品のみ
        "availability":  1,   # 在庫あり
    }
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(_SEARCH_URL, params=params, timeout=15)
            if resp.status_code == 429:
                wait = _RETRY_WAITS[attempt - 1] if attempt - 1 < len(_RETRY_WAITS) else 30
                print(f"[TrendAnalyzer] 429 Too Many Requests ({keyword}) "
                      f"attempt {attempt}/{max_retries} → wait {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            print(f"[TrendAnalyzer] Rakuten HTTP エラー ({keyword}) attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                time.sleep(_RETRY_WAITS[min(attempt - 1, len(_RETRY_WAITS) - 1)])
        except Exception as e:
            print(f"[TrendAnalyzer] Rakuten APIエラー ({keyword}): {e}")
            return {}
    print(f"[TrendAnalyzer] Rakuten API: リトライ上限超過 ({keyword}) → スキップ")
    return {}


def _extract_items(data: dict) -> list[dict]:
    return data.get("Items", [])


def _total_count(data: dict) -> int:
    """検索結果の総件数を返す。"""
    return data.get("count", 0)


# ---------------------------------------------------------------------------
# 1. ジャンル別トレンド分析
# ---------------------------------------------------------------------------

def _compute_genre_stats(items: list[dict]) -> dict:
    """
    商品リストから以下の統計を計算する：
      avg_price, avg_review_score, avg_review_count,
      free_ship_rate, price_drop_rate
    """
    if not items:
        return {
            "avg_price":       0,
            "avg_review_score": 0.0,
            "avg_review_count": 0,
            "free_ship_rate":  0.0,
            "price_drop_rate": 0.0,
            "item_count":      0,
        }

    prices        = [it.get("itemPrice", 0) or 0        for it in items]
    review_scores = [it.get("reviewAverage", 0.0) or 0.0 for it in items]
    review_counts = [it.get("reviewCount", 0) or 0       for it in items]
    free_ships    = [1 if (it.get("postageFlag", 0) == 1) else 0 for it in items]

    # 価格下落: 楽天検索では price_drop は直接取れないため
    # itemCaption に "OFF" や "値下げ" を含む商品を簡易検出
    def _has_drop(it: dict) -> bool:
        text = (it.get("itemCaption", "") or "") + (it.get("itemName", "") or "")
        return any(kw in text for kw in ["%OFF", "値下げ", "セール", "SALE", "割引"])

    price_drops = [1 if _has_drop(it) else 0 for it in items]

    return {
        "avg_price":        int(_avg(prices)),
        "avg_review_score": _avg(review_scores),
        "avg_review_count": int(_avg(review_counts)),
        "free_ship_rate":   round(sum(free_ships)  / len(items), 3),
        "price_drop_rate":  round(sum(price_drops) / len(items), 3),
        "item_count":       len(items),
    }


def analyze_rakuten_trends() -> dict[str, dict]:
    """
    12ジャンルの上位10商品を楽天APIで取得し、統計を集計する。
    前回データ (trend_history.json) と比較して急上昇ジャンルを検知。

    Returns:
        {
          "genre": {
            stats...,
            "rising": True/False,
            "rising_reason": "...",
          },
          ...
        }
    """
    print("[TrendAnalyzer] ジャンル別トレンド分析開始")

    # 前回データを取得
    history = _load_json(_TREND_HIST, default=[])
    prev_genres: dict[str, dict] = {}
    if history:
        prev_genres = history[-1].get("genres", {})

    genre_stats: dict[str, dict] = {}

    for genre, keyword in _GENRES.items():
        print(f"  [{genre}] {keyword} ...", end=" ", flush=True)
        data  = _search_rakuten(keyword, hits=10)
        items = _extract_items(data)
        stats = _compute_genre_stats(items)

        # 急上昇検知
        rising        = False
        rising_reason = ""
        prev = prev_genres.get(genre, {})

        if prev.get("avg_review_count", 0) > 0:
            review_change = (stats["avg_review_count"] - prev["avg_review_count"]) / prev["avg_review_count"]
            if review_change >= _REVIEW_COUNT_RISE_THRESH:
                rising        = True
                rising_reason += f"レビュー件数 +{review_change*100:.0f}% / "

        if prev.get("price_drop_rate", 0) > 0:
            drop_change = (stats["price_drop_rate"] - prev["price_drop_rate"]) / prev["price_drop_rate"]
            if drop_change >= _PRICE_DROP_RISE_THRESH:
                rising        = True
                rising_reason += f"価格下落率 +{drop_change*100:.0f}% / "

        stats["rising"]        = rising
        stats["rising_reason"] = rising_reason.rstrip(" /")
        genre_stats[genre]     = stats

        status = "📈" if rising else "  "
        print(f"{status} avg_price={stats['avg_price']:,} review={stats['avg_review_count']} "
              f"drop={stats['price_drop_rate']:.0%}")
        time.sleep(1.0)  # レート制限対策

    rising_count = sum(1 for s in genre_stats.values() if s["rising"])
    print(f"[TrendAnalyzer] 急上昇ジャンル: {rising_count}件")
    return genre_stats


# ---------------------------------------------------------------------------
# 2. 季節キーワードトレンド検知
# ---------------------------------------------------------------------------

def detect_seasonal_trends() -> dict[str, dict]:
    """
    seasonal_content.py から今月の季節キーワードを取得し、
    楽天APIで検索件数・平均価格を集計。
    前回比 30%以上増加のキーワードを急上昇として検知。

    Returns:
        {
          "keyword": {
            "count": N,
            "avg_price": N,
            "rising": True/False,
            "change_pct": float,
          },
          ...
        }
    """
    print("[TrendAnalyzer] 季節キーワードトレンド検知開始")

    from seasonal_content import _current_season
    season = _current_season()
    if not season:
        print("  季節データなし → スキップ")
        return {}

    raw_keywords, _ = season  # ["新生活", "入学", ...]

    # 前回データ
    history = _load_json(_TREND_HIST, default=[])
    prev_keywords: dict[str, dict] = {}
    if history:
        prev_keywords = history[-1].get("keywords", {})

    kw_stats: dict[str, dict] = {}

    for kw in raw_keywords:
        print(f"  [keyword] {kw} ...", end=" ", flush=True)
        data  = _search_rakuten(kw, hits=10)
        items = _extract_items(data)
        count = _total_count(data)
        prices = [it.get("itemPrice", 0) or 0 for it in items]
        avg_price = int(_avg(prices)) if prices else 0

        # 前回比較
        rising     = False
        change_pct = 0.0
        prev = prev_keywords.get(kw, {})
        if prev.get("count", 0) > 0:
            change_pct = (count - prev["count"]) / prev["count"] * 100
            if change_pct >= _KEYWORD_RISE_THRESH * 100:
                rising = True

        kw_stats[kw] = {
            "count":      count,
            "avg_price":  avg_price,
            "rising":     rising,
            "change_pct": round(change_pct, 1),
        }

        status = "📈" if rising else "  "
        print(f"{status} count={count:,} avg_price={avg_price:,} "
              f"change={change_pct:+.1f}%")
        time.sleep(0.8)

    rising_kws = [k for k, v in kw_stats.items() if v["rising"]]
    print(f"[TrendAnalyzer] 急上昇キーワード: {rising_kws}")
    return kw_stats


# ---------------------------------------------------------------------------
# 3. GPT-4o-mini トレンドレポート生成
# ---------------------------------------------------------------------------

_FALLBACK_REPORT = {
    "hot_genres":         [],
    "cooling_genres":     [],
    "seasonal_keywords":  [],
    "price_trends":       "データ不足のため分析できませんでした",
    "recommendation":     "通常どおりの投稿戦略を継続してください。",
    "risk_genres":        "データ不足のため特定できませんでした",
}


def generate_trend_report(genre_stats: dict, kw_stats: dict) -> dict:
    """
    GPT-4o-mini にトレンドデータを渡してレポートJSONを生成する。
    失敗時はフォールバックを返す。
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key in ("", "xxxx"):
        print("[TrendAnalyzer] OpenAI APIキー未設定 → フォールバック")
        return _FALLBACK_REPORT.copy()

    # GPTに渡す要約データを整形
    rising_genres    = [g for g, s in genre_stats.items() if s.get("rising")]
    non_rising       = sorted(
        [(g, s) for g, s in genre_stats.items() if not s.get("rising")],
        key=lambda x: x[1].get("avg_review_count", 0),
    )
    cooling_genres   = [g for g, _ in non_rising[:3]]   # レビュー少ない上位3件
    rising_keywords  = [k for k, v in kw_stats.items() if v.get("rising")]

    # GPTへ渡すサマリ（トークン節約のためコンパクトに）
    summary = {
        "rising_genres": [
            {
                "genre":  g,
                "reason": genre_stats[g].get("rising_reason", ""),
                "stats":  {k: v for k, v in genre_stats[g].items()
                           if k in ("avg_price", "avg_review_count", "price_drop_rate")},
            }
            for g in rising_genres
        ],
        "all_genres_stats": {
            g: {k: v for k, v in s.items()
                if k in ("avg_price", "avg_review_score", "avg_review_count",
                         "free_ship_rate", "price_drop_rate")}
            for g, s in genre_stats.items()
        },
        "keyword_trends": {
            k: {"count": v["count"], "change_pct": v["change_pct"], "rising": v["rising"]}
            for k, v in kw_stats.items()
        },
        "rising_keywords": rising_keywords,
    }

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=500,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたは楽天市場のトレンド分析AIです。\n"
                        "データを分析して今週注目すべきトレンドを\n"
                        "日本語で報告してください。\n"
                        "以下のJSON形式で返してください：\n"
                        "{\n"
                        '  "hot_genres": ["急上昇ジャンル1", "急上昇ジャンル2"],\n'
                        '  "cooling_genres": ["下降ジャンル1"],\n'
                        '  "seasonal_keywords": ["今週の季節キーワード"],\n'
                        '  "price_trends": "価格トレンドの一言まとめ（50文字以内）",\n'
                        '  "recommendation": "今週の投稿戦略への提言（150文字以内）",\n'
                        '  "risk_genres": "避けるべきジャンルとその理由（80文字以内）"\n'
                        "}\n"
                        "hot_genres / cooling_genres は英語キー"
                        "（food/electronics/health/daily_goods/kitchen/pet/"
                        "outdoor/cosmetics/alcohol/wine/furusato/contact）で返すこと。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "以下のトレンドデータを分析してレポートを生成してください：\n\n"
                        + json.dumps(summary, ensure_ascii=False, indent=2)
                    ),
                },
            ],
        )
        raw    = resp.choices[0].message.content.strip()
        result = json.loads(raw)

        # 必須キー補完
        for key, default in _FALLBACK_REPORT.items():
            if key not in result:
                result[key] = default

        print(f"[TrendAnalyzer] レポート生成完了: "
              f"hot={result.get('hot_genres')} / cooling={result.get('cooling_genres')}")
        return result

    except json.JSONDecodeError as e:
        print(f"[TrendAnalyzer] JSONパースエラー: {e} → フォールバック")
        return _FALLBACK_REPORT.copy()
    except Exception as e:
        print(f"[TrendAnalyzer] GPT-4o-miniエラー: {e} → フォールバック")
        return _FALLBACK_REPORT.copy()


# ---------------------------------------------------------------------------
# 4. 戦略への反映
# ---------------------------------------------------------------------------

def apply_trend_to_strategy(report: dict) -> None:
    """
    トレンドレポートを daily_strategy.json に反映する。
    - hot_genres[0] → top_genre
    - cooling_genres[0] → avoid_genre
    - recommendation → advice
    daily_strategy.json が存在しない場合は新規作成する。
    """
    strategy_data: dict = {}
    if os.path.exists(_STRATEGY_PATH):
        try:
            with open(_STRATEGY_PATH, 'r', encoding='utf-8') as f:
                strategy_data = json.load(f)
        except Exception:
            pass

    strategy: dict = strategy_data.get("strategy", {})

    hot      = report.get("hot_genres", [])
    cooling  = report.get("cooling_genres", [])
    rec      = report.get("recommendation", "")

    if hot:
        strategy["top_genre"] = hot[0]
        print(f"[TrendAnalyzer] top_genre → {hot[0]}")

    if cooling:
        strategy["avoid_genre"] = cooling[0]
        print(f"[TrendAnalyzer] avoid_genre → {cooling[0]}")

    if rec:
        strategy["advice"] = rec[:150]
        print(f"[TrendAnalyzer] advice 更新")

    strategy_data["strategy"]     = strategy
    strategy_data["trend_updated_at"] = datetime.now(timezone.utc).isoformat()
    if "date" not in strategy_data:
        strategy_data["date"] = (
            datetime.now(timezone.utc) + timedelta(hours=9)
        ).strftime("%Y-%m-%d")

    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_STRATEGY_PATH, 'w', encoding='utf-8') as f:
        json.dump(strategy_data, f, ensure_ascii=False, indent=2)
    print(f"[TrendAnalyzer] daily_strategy.json 更新完了")


# ---------------------------------------------------------------------------
# 5. 履歴保存
# ---------------------------------------------------------------------------

def save_trend_history(genre_stats: dict, kw_stats: dict) -> None:
    """
    今回のトレンドデータを trend_history.json に日付付きで追記する。
    直近90日分を保持し、それより古いエントリは削除する。
    """
    history: list[dict] = _load_json(_TREND_HIST, default=[])

    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")

    # 同日エントリがあれば上書き
    history = [h for h in history if h.get("date") != today]

    # genre_stats から保存用に rising_reason のみ残す（容量節約）
    genres_to_save = {
        g: {
            "avg_price":        s.get("avg_price", 0),
            "avg_review_score": s.get("avg_review_score", 0.0),
            "avg_review_count": s.get("avg_review_count", 0),
            "free_ship_rate":   s.get("free_ship_rate", 0.0),
            "price_drop_rate":  s.get("price_drop_rate", 0.0),
            "rising":           s.get("rising", False),
            "rising_reason":    s.get("rising_reason", ""),
        }
        for g, s in genre_stats.items()
    }

    history.append({
        "date":      today,
        "saved_at":  datetime.now(timezone.utc).isoformat(),
        "genres":    genres_to_save,
        "keywords":  {
            k: {"count": v["count"], "avg_price": v["avg_price"],
                "rising": v["rising"], "change_pct": v["change_pct"]}
            for k, v in kw_stats.items()
        },
    })

    # 90日超の古いエントリを削除
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date", "") >= cutoff]

    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_TREND_HIST, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[TrendAnalyzer] trend_history.json 更新: {len(history)}件保持")


# ---------------------------------------------------------------------------
# 8. Discord通知
# ---------------------------------------------------------------------------

def _send_discord_report(report: dict, genre_stats: dict, kw_stats: dict) -> None:
    """週次トレンドレポートを WEBHOOK_REPORT に Embed 送信する。"""
    if not _WEBHOOK_URL:
        print("[TrendAnalyzer] DISCORD_WEBHOOK_REPORT 未設定 → スキップ")
        return

    date_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")

    hot_str      = "、".join(report.get("hot_genres", [])) or "なし"
    cooling_str  = "、".join(report.get("cooling_genres", [])) or "なし"
    season_str   = "、".join(report.get("seasonal_keywords", [])) or "なし"

    # ジャンル統計サマリ（上位3ジャンルをレビュー件数で）
    top_genres_sorted = sorted(
        genre_stats.items(),
        key=lambda x: x[1].get("avg_review_count", 0),
        reverse=True,
    )[:3]
    genre_lines = "\n".join(
        f"・{g}: avg¥{s['avg_price']:,} / review{s['avg_review_count']} "
        f"/ drop{s['price_drop_rate']:.0%}"
        for g, s in top_genres_sorted
    ) or "データなし"

    # 急上昇キーワード
    rising_kws = [f"{k}(+{v['change_pct']:.0f}%)" for k, v in kw_stats.items() if v.get("rising")]
    kw_str = "、".join(rising_kws) if rising_kws else "なし"

    embed = {
        "title":     f"📊 週次トレンドレポート - {date_str}",
        "color":     0x1ABC9C,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "trend_analyzer.py by ずかぽんBot"},
        "fields": [
            {
                "name":   "📈 急上昇ジャンル",
                "value":  hot_str,
                "inline": True,
            },
            {
                "name":   "📉 下降ジャンル",
                "value":  cooling_str,
                "inline": True,
            },
            {
                "name":   "🌸 急上昇季節キーワード",
                "value":  kw_str,
                "inline": False,
            },
            {
                "name":   "📅 今週の季節キーワード",
                "value":  season_str,
                "inline": False,
            },
            {
                "name":   "💰 価格トレンド",
                "value":  report.get("price_trends", "—")[:200],
                "inline": False,
            },
            {
                "name":   "🎯 今週の戦略提言",
                "value":  report.get("recommendation", "—")[:300],
                "inline": False,
            },
            {
                "name":   "⚠️ リスクジャンル",
                "value":  report.get("risk_genres", "—")[:200],
                "inline": False,
            },
            {
                "name":   "📋 ジャンル別スコア上位3",
                "value":  genre_lines,
                "inline": False,
            },
        ],
    }

    try:
        resp = requests.post(_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        resp.raise_for_status()
        print(f"[TrendAnalyzer] Discord送信完了 (status={resp.status_code})")
    except requests.HTTPError as e:
        print(f"[TrendAnalyzer] Discord HTTPエラー: {e.response.status_code}")
    except Exception as e:
        print(f"[TrendAnalyzer] Discord送信エラー: {e}")


# ---------------------------------------------------------------------------
# 6. メイン関数
# ---------------------------------------------------------------------------

def run_trend_analyzer() -> None:
    """
    週次トレンド分析を実行する。
    main.py から日曜 9時JST のみ呼び出す。
    """
    print("\n--- TrendAnalyzer: 週次トレンド分析開始 ---")

    if not _RAKUTEN_APP_ID:
        print("[TrendAnalyzer] RAKUTEN_APP_ID 未設定 → スキップ")
        return

    # Step 1: ジャンル別トレンド分析
    genre_stats = analyze_rakuten_trends()

    # Step 2: 季節キーワードトレンド検知
    kw_stats = detect_seasonal_trends()

    # Step 3: GPT-4o-mini レポート生成
    report = generate_trend_report(genre_stats, kw_stats)

    # Step 4: daily_strategy.json に反映
    apply_trend_to_strategy(report)

    # Step 5: 履歴保存
    save_trend_history(genre_stats, kw_stats)

    # Step 8: Discord通知
    _send_discord_report(report, genre_stats, kw_stats)

    hot_count = len(report.get("hot_genres", []))
    print(f"\n[TrendAnalyzer] 完了 — 急上昇ジャンル: {hot_count}件")


if __name__ == "__main__":
    run_trend_analyzer()
