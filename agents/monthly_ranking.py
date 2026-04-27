import os
import sys
from datetime import datetime, timezone, timedelta

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from discord_notifier import send_discord, send_discord_error_embed
from threads_poster import _create_container, _publish_container
from instagram_poster import post_to_instagram
from bluesky_poster import post_to_bluesky
from database import get_all_learning_data, get_all_post_logs

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID      = os.getenv("THREADS_USER_ID", "me")


def _get_top3_this_month() -> list[dict]:
    """Return top 3 products by engagement score posted this calendar month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    learning_data = get_all_learning_data()
    post_log      = get_all_post_logs()

    # Build URL lookup from post_log for affiliate URLs
    url_by_post_id = {
        p["post_id"]: p.get("affiliate_url", "")
        for p in post_log
        if p.get("post_id")
    }

    this_month = [
        e for e in learning_data
        if e.get("posted_at")
        and datetime.fromisoformat(e["posted_at"]) >= month_start
        and e.get("engagement", 0) >= 0  # include all, sort below
        and e.get("title")
    ]

    this_month.sort(key=lambda e: e["engagement"], reverse=True)
    top3 = this_month[:3]

    # Attach affiliate_url from post_log
    for entry in top3:
        entry["affiliate_url"] = url_by_post_id.get(entry.get("post_id", ""), "")

    return top3


def _build_ranking_text(top3: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    month_str = f"{now.year}年{now.month}月"

    lines = [f"🏆 {month_str} 買って良かったTOP3 🏆\n"]
    medals = ["🥇", "🥈", "🥉"]

    for i, entry in enumerate(top3):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        title = entry.get("title", "—")[:35]
        price = entry.get("price", "")
        price_str = f"¥{price:,}" if isinstance(price, int) and price else str(price)
        score = entry.get("engagement", 0)
        url   = entry.get("affiliate_url", "")

        lines.append(f"{medal} {title}")
        if price_str and price_str != "":
            lines.append(f"💴 {price_str}")
        lines.append(f"❤️ エンゲージメント: {score}")
        if url:
            lines.append(f"🔗 {url}")
        lines.append("")

    lines.append("#楽天市場 #月間ランキング #買って良かった")
    return "\n".join(lines)


def post_monthly_ranking() -> dict:
    """Build and post the monthly ranking to Threads (and other platforms)."""
    top3 = _get_top3_this_month()

    if not top3:
        print("[MonthlyRanking] No engagement data this month — skipping.")
        return {"status": "skipped", "reason": "no data"}

    text = _build_ranking_text(top3)
    print("\n--- Monthly Ranking Post ---")
    print(text)

    # Post to Threads
    result = {"status": "error", "post_id": None}
    try:
        creation_id = _create_container(
            text=text,
            token=THREADS_ACCESS_TOKEN,
            user_id=THREADS_USER_ID,
        )
        post_id = _publish_container(
            creation_id=creation_id,
            token=THREADS_ACCESS_TOKEN,
            user_id=THREADS_USER_ID,
        )
        result = {"status": "success", "post_id": post_id}
        print(f"[OK] Monthly ranking posted to Threads | post_id={post_id}")
        send_discord(f"🏆 月間ランキング投稿完了 | {post_id}")
    except Exception as e:
        err = str(e)
        print(f"[ERROR] Threads monthly ranking: {err}")
        send_discord_error_embed("Threads", err, "月間ランキング")

    # Post to Instagram — use top product image if available
    # Skip if TOP1 genre is alcohol / wine / electronics (platform-specific content)
    _IG_SKIP_GENRES = {"alcohol", "wine", "electronics"}
    top_post = top3[0] if top3 else {}
    ig_product = {
        "title":         top_post.get("title", "月間ランキング"),
        "post_text":     text,
        "image_url":     top_post.get("image_url", ""),
        "affiliate_url": top_post.get("affiliate_url", ""),
        "genre":         top_post.get("genre", ""),
        "price":         top_post.get("price", 0),
    }
    top_genre = top_post.get("genre", "")
    if top_genre in _IG_SKIP_GENRES:
        print(f"[MonthlyRanking] Instagram スキップ（TOP1ジャンル: {top_genre}）")
    else:
        ig_result = post_to_instagram(ig_product)
        if ig_result["status"] == "success":
            send_discord(f"✅ [Instagram] 月間ランキング投稿完了")
        elif ig_result["status"] == "error":
            send_discord_error_embed("Instagram", ig_result.get("error", ""), "月間ランキング")

    # Post to Bluesky — same genre filter as Instagram
    if top_genre in _IG_SKIP_GENRES:
        print(f"[MonthlyRanking] Bluesky スキップ（TOP1ジャンル: {top_genre}）")
    else:
        bsky_result = post_to_bluesky(ig_product)
        if bsky_result["status"] == "success":
            send_discord(f"✅ [Bluesky] 月間ランキング投稿完了")
        elif bsky_result["status"] == "error":
            send_discord_error_embed("Bluesky", bsky_result.get("error", ""), "月間ランキング")

    return result


if __name__ == "__main__":
    result = post_monthly_ranking()
    print(f"\nResult: {result}")
