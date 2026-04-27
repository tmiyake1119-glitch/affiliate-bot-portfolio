import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from database import (
    get_all_post_logs, get_all_learning_data, save_learning_data_bulk,
    insert_follower_history, get_all_follower_history,
    save_ab_result, get_ab_stats, get_recent_ab_results, get_low_performance_products,
)

logger = logging.getLogger("affiliate_bot.analytics_agent")

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

THREADS_ACCESS_TOKEN  = os.getenv("THREADS_ACCESS_TOKEN")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_USER_ID      = os.getenv("INSTAGRAM_USER_ID")
BLUESKY_HANDLE         = os.getenv("BLUESKY_HANDLE")

HOURLY_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'hourly_engagement.json')


def _save_json(path: str, data) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_insights(post_id: str) -> dict:
    """Fetch likes, replies, reposts for a post from Threads API."""
    url = f"https://graph.threads.net/v1.0/{post_id}/insights"
    resp = requests.get(url, params={
        "metric":       "likes,replies,reposts,views",
        "access_token": THREADS_ACCESS_TOKEN,
    }, timeout=15)
    resp.raise_for_status()
    metrics = {item["name"]: item["values"][0]["value"]
               for item in resp.json().get("data", [])}
    return metrics


def fetch_instagram_insights(post_id: str) -> dict:
    """Fetch like_count, comments_count, reach for an Instagram post via Graph API.

    reach は Business/Creator アカウントのみ取得可能。Personal の場合は 0 にフォールバックする。
    戻り値キーは analytics_agent 内の統一名: likes / comments / reach / views(=reach)
    """
    resp = requests.get(
        f"https://graph.facebook.com/v22.0/{post_id}",
        params={
            "fields":       "like_count,comments_count",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data     = resp.json()
    likes    = int(data.get("like_count",    0) or 0)
    comments = int(data.get("comments_count", 0) or 0)

    reach = 0
    try:
        ins = requests.get(
            f"https://graph.facebook.com/v22.0/{post_id}/insights",
            params={"metric": "reach", "access_token": INSTAGRAM_ACCESS_TOKEN},
            timeout=15,
        )
        ins.raise_for_status()
        for item in ins.json().get("data", []):
            if item.get("name") == "reach":
                reach = int(item.get("values", [{}])[0].get("value", 0) or 0)
    except Exception:
        pass  # Business/Creator アカウント専用: 取得できない場合は 0 のまま

    return {"likes": likes, "comments": comments, "reach": reach, "views": reach}


def compute_engagement(metrics: dict, platform: str = "threads") -> int:
    """Weighted engagement score.

    Threads   : likes×3 + replies×5 + reposts×4
    Instagram : likes×3 + comments×5
    """
    if platform == "instagram":
        return (
            metrics.get("likes",    0) * 3 +
            metrics.get("comments", 0) * 5
        )
    return (
        metrics.get("likes",   0) * 3 +
        metrics.get("replies", 0) * 5 +
        metrics.get("reposts", 0) * 4
    )


def _load_ab_test_accounts() -> dict[str, str]:
    """accounts.json から ab_test=true のアカウントを {name: ab_platform} dict で返す。

    ab_platform が未設定の場合は "threads" をデフォルトとする。
    空の場合は evaluate_ab_test / fetch_all_insights がスキップ判定に使う。
    """
    accounts_path = os.path.join(os.path.dirname(__file__), '..', 'accounts.json')
    if not os.path.exists(accounts_path):
        return {"mono_zukan_jp": "instagram"}  # デフォルト
    try:
        with open(accounts_path, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
        return {
            a["name"]: a.get("ab_platform", "threads")
            for a in accounts if a.get("ab_test", False)
        }
    except Exception:
        return {"mono_zukan_jp": "instagram"}


def fetch_all_insights() -> list[dict]:
    post_log = get_all_post_logs()
    successful = [p for p in post_log if p.get("status") == "success" and p.get("post_id")]

    if not successful:
        print("No successful posts found in post_logs DB.")
        return []

    existing = get_all_learning_data()
    existing_ids = {e["post_id"] for e in existing}

    # フォロワー数取得（API 呼び出しなし・DB から最新値を取得）
    follower_history_cache = get_all_follower_history()
    current_followers = 1
    if follower_history_cache:
        val = follower_history_cache[-1].get("threads")
        current_followers = max(int(val), 1) if val else 1

    # AB_TEST_PATTERN は post_logs.ab_group が NULL の場合のフォールバック
    ab_pattern = os.getenv("AB_TEST_PATTERN", "A").strip().upper()

    # ab_test=true のアカウントセット（save_ab_result フィルタ用）
    ab_test_accounts = _load_ab_test_accounts()

    now = datetime.now(timezone.utc)
    results = []
    for post in successful:
        post_id = post["post_id"]

        # Skip posts less than 2 hours old — no meaningful engagement yet
        posted_at_str = post.get("posted_at", "")
        if posted_at_str:
            try:
                posted_at = datetime.fromisoformat(posted_at_str)
                if (now - posted_at).total_seconds() < 7200:
                    print(f"[analytics] スキップ（投稿から2時間未満）: {post_id}")
                    continue
            except (ValueError, TypeError):
                pass

        # post_logs.platform で API を切り替える（旧レコードは "threads" 扱い）
        post_platform = post.get("platform") or "threads"
        print(f"Fetching insights for post {post_id} [{post['genre']}] ({post_platform})...", end=" ")
        try:
            if post_platform == "instagram":
                metrics = fetch_instagram_insights(post_id)
            else:
                metrics = fetch_insights(post_id)
            score = compute_engagement(metrics, platform=post_platform)
            entry = {
                "post_id":    post_id,
                "title":      post["title"],
                "genre":      post["genre"],
                "price":      post["price"],
                "image_url":  post.get("image_url", ""),
                "url":        post.get("url", ""),
                "ab_group":   post.get("ab_group", "A"),
                "posted_at":  post["posted_at"],
                "likes":      metrics.get("likes",   0),
                "replies":    metrics.get("replies", 0),  # Instagram では常に 0
                "reposts":    metrics.get("reposts", 0),  # Instagram では常に 0
                "views":      metrics.get("views",   0),  # Instagram では reach の値
                "engagement": score,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            if post_id in existing_ids:
                # Update in-place
                for i, e in enumerate(existing):
                    if e["post_id"] == post_id:
                        existing[i] = entry
                        break
            else:
                existing.append(entry)
                existing_ids.add(post_id)

            # A/B テスト結果を保存
            if post_platform == "instagram":
                raw_engagement = metrics.get("likes", 0) + metrics.get("comments", 0)
            else:
                raw_engagement = (
                    metrics.get("likes",   0) +
                    metrics.get("replies", 0) +
                    metrics.get("reposts", 0)
                )
            if current_followers <= 0:
                logger.warning("フォロワー数0のためABカウントをスキップ (post_id=%s)", post_id)
                results.append(entry)
                continue
            eng_rate = raw_engagement / current_followers
            # ab_test=true のアカウントかつ post の platform が accounts.json の ab_platform と一致する場合のみ保存
            # "account" フィールドがない旧投稿は mono_zukan_jp 扱い（後方互換）
            post_account = post.get("account", "mono_zukan_jp")
            if post_account not in ab_test_accounts:
                logger.info(
                    "ABカウントスキップ: ab_test=false アカウント %s (post_id=%s)",
                    post_account, post_id,
                )
            elif post_platform != ab_test_accounts.get(post_account):
                logger.info(
                    "ABカウントスキップ: platform不一致 post=%s account_ab=%s (post_id=%s)",
                    post_platform, ab_test_accounts.get(post_account), post_id,
                )
            elif raw_engagement == 0 and metrics.get("views", 0) == 0:
                logger.info("ABカウントスキップ: エンゲージメント0かつviews0 (post_id=%s)", post_id)
            else:
                post_ab_group = post.get("ab_group")
                if post_ab_group is None:
                    logger.info("ABカウントスキップ: ab_group=None (post_id=%s)", post_id)
                    results.append(entry)
                    continue
                try:
                    save_ab_result(
                        pattern           = post_ab_group,
                        post_id           = post_id,
                        engagement_rate   = eng_rate,
                        followers_at_post = current_followers,
                        genre             = post.get("genre", ""),
                        posted_at         = post.get("posted_at", ""),
                        views             = metrics.get("views", 0),
                    )
                except Exception as ab_err:
                    logger.warning("save_ab_result 失敗 (post_id=%s): %s", post_id, ab_err)

            results.append(entry)
            print(f"likes={entry['likes']} replies={entry['replies']} reposts={entry['reposts']} views={entry['views']} score={score}")
        except requests.HTTPError as e:
            if e.response.status_code == 500:
                continue
            print(f"HTTP {e.response.status_code}: {e.response.text[:120]}")
        except Exception as e:
            print(f"Error: {e}")

    save_learning_data_bulk(existing)
    _update_hourly_engagement(existing)
    return results


def _update_hourly_engagement(learning_data: list[dict]) -> None:
    """Rebuild hourly_engagement.json: avg engagement score per JST hour (0-23)."""
    # Group scores by JST hour of posted_at
    hour_scores: dict[int, list[int]] = {}
    for entry in learning_data:
        posted_at = entry.get("posted_at")
        if not posted_at:
            continue
        try:
            # Convert to JST (UTC+9)
            dt_utc  = datetime.fromisoformat(posted_at)
            jst_hour = (dt_utc.hour + 9) % 24
        except (ValueError, AttributeError):
            continue
        hour_scores.setdefault(jst_hour, []).append(entry.get("engagement", 0))

    hourly = {
        str(hour): {
            "count": len(scores),
            "avg":   round(sum(scores) / len(scores), 2),
        }
        for hour, scores in hour_scores.items()
    }
    _save_json(HOURLY_PATH, hourly)


def _fetch_threads_followers() -> int | None:
    try:
        resp = requests.get(
            "https://graph.threads.net/v1.0/me",
            params={"fields": "followers_count", "access_token": THREADS_ACCESS_TOKEN},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("followers_count")
    except Exception as e:
        print(f"[Followers] Threads error: {e}")
        return None


def _fetch_instagram_followers() -> int | None:
    if not INSTAGRAM_ACCESS_TOKEN or not INSTAGRAM_USER_ID:
        return None
    try:
        resp = requests.get(
            f"https://graph.facebook.com/v22.0/{INSTAGRAM_USER_ID}",
            params={"fields": "followers_count", "access_token": INSTAGRAM_ACCESS_TOKEN},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("followers_count")
    except Exception as e:
        print(f"[Followers] Instagram error: {e}")
        return None


def _fetch_bluesky_followers() -> int | None:
    if not BLUESKY_HANDLE:
        return None
    try:
        resp = requests.get(
            "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile",
            params={"actor": BLUESKY_HANDLE},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("followersCount")
    except Exception as e:
        print(f"[Followers] Bluesky error: {e}")
        return None


def fetch_follower_count() -> dict:
    """Fetch follower counts for all platforms and append to follower_history.json.

    Returns dict with keys: threads, instagram, bluesky (None on failure).
    Format: {"timestamp": "...", "threads": N, "instagram": N, "bluesky": N}
    """
    counts = {
        "threads":   _fetch_threads_followers(),
        "instagram": _fetch_instagram_followers(),
        "bluesky":   _fetch_bluesky_followers(),
    }

    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **counts}
    insert_follower_history(entry)

    print(f"[Followers] threads={counts['threads']}  instagram={counts['instagram']}  bluesky={counts['bluesky']}")
    return counts


def get_follower_diff(platform: str = "threads") -> tuple[int | None, int | None]:
    """Return (current_count, week_diff) for the given platform.

    platform: "threads" | "instagram" | "bluesky"
    """
    from datetime import timedelta

    history = get_all_follower_history()
    if not history:
        return None, None

    def _get(entry: dict) -> int | None:
        v = entry.get(platform)
        return int(v) if v is not None else None

    current = _get(history[-1])
    if current is None:
        return None, None

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    older = [
        h for h in history[:-1]
        if datetime.fromisoformat(h["timestamp"]) <= cutoff and _get(h) is not None
    ]
    if not older:
        return current, None

    prev = _get(older[-1])
    return current, (current - prev) if prev is not None else None


def get_best_posting_hours(min_posts: int = 10) -> list[int] | None:
    """Return top 3 JST hours by avg engagement, or None if insufficient data."""
    if not os.path.exists(HOURLY_PATH):
        return None
    with open(HOURLY_PATH, 'r', encoding='utf-8') as f:
        hourly = json.load(f)
    total = sum(v["count"] for v in hourly.values())
    if total < min_posts:
        return None
    ranked = sorted(hourly.items(), key=lambda x: x[1]["avg"], reverse=True)
    return [int(h) for h, _ in ranked[:3]]


def evaluate_ab_test() -> None:
    """A/Bテストを評価してDiscordに通知する。

    評価条件:
    通常切り替え: A・B それぞれ15投稿以上 かつ 開始から5日経過
    早期打ち切り: 直近10投稿のengagement_rate連続低下 or フォロワー3日連続減少

    判定:
    相対差 >= +5%  → B採用
    相対差 <= -5%  → A継続確定
    それ以外       → 差なし・A継続

    ab_test=true のアカウントが1つも存在しない場合はスキップ。
    """
    from discord_notifier import send_discord, send_discord_report

    # ab_test=true のアカウントがなければ評価スキップ
    ab_test_accounts = _load_ab_test_accounts()
    if not ab_test_accounts:
        logger.info("[ABTest] ab_test=true のアカウントなし → 評価スキップ")
        return

    stats = get_ab_stats()
    logger.info("[ABTest] 現在のA/B統計: %s", stats)

    if not stats:
        logger.info("[ABTest] 統計データなし → 評価スキップ")
        return

    stat_a = stats.get("A", {})
    stat_b = stats.get("B", {})
    count_a = stat_a.get("count", 0)
    count_b = stat_b.get("count", 0)

    # サンプル不足チェック（5件未満は評価対象外）
    if count_a < 5 or count_b < 5:
        logger.info("ABテスト評価スキップ: サンプル不足 (A=%d件, B=%d件)", count_a, count_b)
        return

    # --- フォロワー数取得 ---
    follower_history_cache = get_all_follower_history()
    current_followers = 1
    if follower_history_cache:
        val = follower_history_cache[-1].get("threads")
        current_followers = max(int(val), 1) if val else 1

    # --- 早期打ち切り: engagement_rate 連続低下 ---
    early_term_reason = ""
    recent_results = get_recent_ab_results(limit=10)
    if len(recent_results) >= 10:
        rate_threshold = 0.0056 if current_followers < 100 else 0.008
        if all(r.get("engagement_rate", 0) < rate_threshold for r in recent_results):
            early_term_reason = (
                f"直近10投稿全て engagement_rate < {rate_threshold:.4%} "
                f"(フォロワー {current_followers})"
            )
            logger.info("[ABTest] 早期打ち切り条件: %s", early_term_reason)

    # --- 早期打ち切り: フォロワー3日連続減少 ---
    if not early_term_reason and len(follower_history_cache) >= 3:
        recent_3 = follower_history_cache[-3:]
        vals = [h.get("threads") for h in recent_3 if h.get("threads") is not None]
        if len(vals) >= 3 and vals[0] > vals[1] > vals[2]:
            early_term_reason = (
                f"Threadsフォロワーが3日連続減少: {vals[0]}→{vals[1]}→{vals[2]}"
            )
            logger.info("[ABTest] 早期打ち切り条件: %s", early_term_reason)

    # --- 通常切り替え条件チェック ---
    ready_for_normal_eval = False

    if count_a >= 15 and count_b >= 15:
        started_at_str = stat_a.get("started_at") or stat_b.get("started_at") or ""
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str)
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                days_elapsed = (datetime.now(timezone.utc) - started_at).days
                if days_elapsed >= 5:
                    ready_for_normal_eval = True
            except Exception:
                pass

    should_evaluate = ready_for_normal_eval or bool(early_term_reason)

    if not should_evaluate:
        logger.info(
            "[ABTest] 評価条件未達: A=%d件 B=%d件 (15件以上 & 5日経過が必要)",
            count_a, count_b,
        )
        return

    # --- 判定 ---
    avg_a = stat_a.get("avg_rate", 0.0)
    avg_b = stat_b.get("avg_rate", 0.0)

    if avg_a == 0:
        logger.info("[ABTest] パターンAのデータなし → 評価スキップ")
        return

    relative_diff = (avg_b - avg_a) / avg_a

    logger.info(
        "[ABTest] A平均=%.6f B平均=%.6f 相対差=%.2f%%",
        avg_a, avg_b, relative_diff * 100,
    )

    eval_reason = early_term_reason if early_term_reason else "通常評価（15投稿以上・5日経過）"

    if relative_diff >= 0.05:
        msg = (
            f"✅ **ABテスト結果: パターンB採用**\n"
            f"A平均: {avg_a:.6f} / B平均: {avg_b:.6f} / 相対差: +{relative_diff*100:.1f}%\n"
            f"理由: {eval_reason}\n"
            f"→ .env の `AB_TEST_PATTERN=B` を維持してください。"
        )
        logger.info("[ABTest] B採用: 相対差=+%.1f%%", relative_diff * 100)
        send_discord_report(msg)
    elif relative_diff <= -0.05:
        msg = (
            f"✅ **ABテスト結果: パターンA継続**\n"
            f"A平均: {avg_a:.6f} / B平均: {avg_b:.6f} / 相対差: {relative_diff*100:.1f}%\n"
            f"理由: {eval_reason}\n"
            f"→ .env の `AB_TEST_PATTERN=A` を確認してください。"
        )
        logger.info("[ABTest] A継続確定: 相対差=%.1f%%", relative_diff * 100)
        send_discord_report(msg)
    else:
        msg = (
            f"📊 **ABテスト結果: 差なし・A継続**\n"
            f"A平均: {avg_a:.6f} / B平均: {avg_b:.6f} / 相対差: {relative_diff*100:.1f}%\n"
            f"理由: {eval_reason}"
        )
        logger.info("[ABTest] 差なし・A継続: 相対差=%.1f%%", relative_diff * 100)
        send_discord(msg)

    # --- 低パフォーマンス商品の停止評価 ---
    try:
        low_perf_urls = get_low_performance_products(current_followers)
        if low_perf_urls:
            logger.info(
                "[ABTest] 低パフォーマンス商品 %d 件を検出 (閾値: フォロワー%d人)",
                len(low_perf_urls), current_followers,
            )
            for url in list(low_perf_urls)[:5]:  # ログは最大5件
                logger.info("[ABTest] 停止対象URL: %s", url[:80])
        else:
            logger.info("[ABTest] 低パフォーマンス商品なし")
    except Exception as e:
        logger.warning("[ABTest] get_low_performance_products エラー: %s", e)


def print_summary(results: list[dict]) -> None:
    if not results:
        return

    print(f"\n{'='*50}")
    print(f"  Engagement Summary ({len(results)} post(s))")
    print(f"{'='*50}")

    by_genre: dict[str, list] = {}
    for r in results:
        by_genre.setdefault(r["genre"], []).append(r)

    for genre, posts in sorted(by_genre.items()):
        avg_score = sum(p["engagement"] for p in posts) / len(posts)
        top = max(posts, key=lambda p: p["engagement"])
        print(f"\n  [{genre}]  {len(posts)} post(s)  avg score={avg_score:.1f}")
        print(f"    Top: {top['title'][:50]}")
        print(f"         likes={top['likes']} replies={top['replies']} reposts={top['reposts']} score={top['engagement']}")

    best = max(results, key=lambda p: p["engagement"])
    print(f"\n  Best overall: [{best['genre']}] score={best['engagement']}")
    print(f"    {best['title'][:60]}")
    print(f"{'='*50}")


if __name__ == "__main__":
    print("Running analytics...\n")
    results = fetch_all_insights()
    print_summary(results)
    print("\nLearning data saved to SQLite DB.")
