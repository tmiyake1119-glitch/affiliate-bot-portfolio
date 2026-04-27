import os
import sys
from datetime import datetime, timezone

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from discord_notifier import send_discord, send_discord_error_embed
from threads_poster import _create_container, _publish_container
from database import get_all_queue_entries, get_all_post_logs

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID      = os.getenv("THREADS_USER_ID", "me")

PRICE_LIMIT = 1000


def _parse_price(price_val) -> int:
    """Convert ¥1,234 string or int to int."""
    if isinstance(price_val, int):
        return price_val
    if isinstance(price_val, str):
        try:
            return int(price_val.replace("¥", "").replace(",", "").strip())
        except ValueError:
            return 0
    return 0


def _get_budget_products(max_price: int = PRICE_LIMIT, n: int = 3) -> list[dict]:
    """Find up to n products under max_price from queue + post_log."""
    queue = get_all_queue_entries()
    log   = get_all_post_logs()

    # Build affiliate_url map from log
    url_map = {p.get("title", ""): p.get("affiliate_url", "") for p in log}

    candidates = []
    seen_titles = set()

    for entry in queue:
        price = _parse_price(entry.get("price", 0))
        title = entry.get("title", "")
        if price <= 0 or price > max_price:
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        candidates.append({
            "title":         title,
            "price":         price,
            "price_str":     f"¥{price:,}",
            "genre":         entry.get("genre", ""),
            "affiliate_url": entry.get("affiliate_url", "") or url_map.get(title, ""),
            "image_url":     entry.get("image_url", ""),
        })

    # Sort by price descending (best value near the limit)
    candidates.sort(key=lambda p: p["price"], reverse=True)
    return candidates[:n]


def _build_budget_post(products: list[dict]) -> str:
    lines = ["💰 今週のお得品特集（〜1000円）💰\n"]

    for i, p in enumerate(products, start=1):
        title = p["title"][:35]
        lines.append(f"{i}️⃣ {title}")
        lines.append(f"   💴 {p['price_str']}")
        if p.get("affiliate_url"):
            lines.append(f"   🔗 {p['affiliate_url']}")
        lines.append("")

    lines.append("#楽天市場 #お得 #プチプラ #節約")
    return "\n".join(lines)


def post_price_range() -> dict:
    """Post the Thursday budget special. Returns result dict."""
    products = _get_budget_products()

    if not products:
        print("[PriceRange] No products under ¥1,000 found — skipping.")
        return {"status": "skipped", "reason": "no budget products"}

    text = _build_budget_post(products)
    print("\n--- Budget Special Post ---")
    print(text)

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
        send_discord(f"💰 お得品特集 投稿完了 | {post_id}")
        print(f"[OK] Budget special posted | post_id={post_id}")
        return {"status": "success", "post_id": post_id}
    except Exception as e:
        err = str(e)
        print(f"[ERROR] Budget special: {err}")
        send_discord_error_embed("Threads", err, "お得品特集")
        return {"status": "error", "error": err}


if __name__ == "__main__":
    result = post_price_range()
    print(f"\nResult: {result}")
