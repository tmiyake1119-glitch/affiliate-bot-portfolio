import os
import sys
from datetime import datetime, timezone, timedelta

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from discord_notifier import send_discord
from threads_poster import _create_container, _publish_container
from database import get_all_queue_entries, get_all_learning_data, insert_post_log

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID      = os.getenv("THREADS_USER_ID", "me")


def _top_from_learning(n: int = 3) -> list[dict]:
    """Return top n products by engagement from the past 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    data = get_all_learning_data()
    recent = [
        e for e in data
        if e.get("fetched_at")
        and datetime.fromisoformat(e["fetched_at"]) >= cutoff
        and e.get("engagement", 0) > 0
    ]
    recent.sort(key=lambda e: e["engagement"], reverse=True)
    return recent[:n]


def _top_from_queue(n: int = 3) -> list[dict]:
    """Return top n products from the queue sorted by price descending."""
    queue = get_all_queue_entries()
    # Normalize price string to int for sorting
    def price_int(entry: dict) -> int:
        raw = entry.get("price", "0").replace("¥", "").replace(",", "")
        try:
            return int(raw)
        except ValueError:
            return 0
    queue.sort(key=price_int, reverse=True)
    return queue[:n]


def _short_description(entry: dict) -> str:
    """Generate a one-line description from engagement or genre."""
    genre_desc = {
        "food":        "食卓を豊かにするグルメ商品",
        "electronics": "生活を便利にする人気家電",
        "health":      "健康をサポートするアイテム",
        "daily_goods": "毎日使える便利な日用品",
        "alcohol":     "こだわりのお酒・ウイスキー",
        "wine":        "厳選されたワイン",
    }
    engagement = entry.get("engagement")
    genre = entry.get("genre", "")
    base = genre_desc.get(genre, "楽天市場の人気商品")
    if engagement:
        return f"{base}（エンゲージメントスコア: {engagement}）"
    return base


def _build_digest_post(products: list[dict]) -> str:
    lines = ["🏆 今週のおすすめ3選 🏆", ""]
    for i, p in enumerate(products, start=1):
        title       = p.get("title") or p.get("name", "")
        price       = p.get("price") or p.get("price_str", "")
        url         = p.get("affiliate_url") or p.get("url", "")
        description = _short_description(p)
        lines += [
            f"【{i}位】{title[:40]}",
            f"💴 {price}",
            f"📌 {description}",
            f"🔗 {url}",
            "",
        ]
    lines.append("#楽天市場 #週間おすすめ #買って良かった")
    return "\n".join(lines)


def post_digest() -> dict:
    """Select top 3 products, build digest post, publish to Threads, notify Discord."""
    print("Building digest from learning data...")
    products = _top_from_learning(n=3)

    if len(products) < 3:
        shortfall = 3 - len(products)
        print(f"Only {len(products)} learning record(s) found — filling {shortfall} from queue.")
        queue_tops = _top_from_queue(n=shortfall + 3)  # fetch extra to avoid overlap
        seen_titles = {p.get("title") or p.get("name", "") for p in products}
        for entry in queue_tops:
            title = entry.get("title") or entry.get("name", "")
            if title not in seen_titles:
                products.append(entry)
                seen_titles.add(title)
            if len(products) == 3:
                break

    if not products:
        print("No products available for digest.")
        return {"status": "skip", "error": "no products"}

    post_text = _build_digest_post(products)
    print("\n--- Digest Post ---")
    print(post_text)
    print()

    log_entry = {
        "title":     "今週のおすすめ3選",
        "genre":     "digest",
        "price":     "",
        "url":       "",
        "account":   "mono_zukan_jp",
        "ab_group":  None,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "status":    None,
        "post_id":   None,
        "error":     None,
    }

    last_error = None
    for attempt in range(1, 4):
        try:
            creation_id = _create_container(post_text)
            post_id = _publish_container(creation_id)
            log_entry["status"]  = "success"
            log_entry["post_id"] = post_id
            print(f"[OK] Digest posted | post_id={post_id} (attempt {attempt}/3)")
            send_discord(f"📋 今週のおすすめ3選を投稿しました！ post_id={post_id}")
            break
        except requests.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"[RETRY {attempt}/3] {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[RETRY {attempt}/3] {last_error}")
        if attempt < 3:
            import time
            time.sleep(5)
    else:
        log_entry["status"] = "error"
        log_entry["error"]  = last_error
        print(f"[FAIL] Digest post failed. Last error: {last_error}")

    insert_post_log(log_entry)
    return log_entry


if __name__ == "__main__":
    result = post_digest()
    print(f"\nStatus: {result['status']}")
    if result.get("error"):
        print(f"Error : {result['error']}")
