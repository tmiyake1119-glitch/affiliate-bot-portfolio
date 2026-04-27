import os
import random
import sys
from datetime import datetime, timezone

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from discord_notifier import send_discord
from database import get_all_queue_entries, insert_post_log

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID      = os.getenv("THREADS_USER_ID", "me")

GENRE_LABELS = {
    "food":        "グルメ",
    "electronics": "家電",
    "health":      "健康グッズ",
    "daily_goods": "日用品",
    "alcohol":     "お酒",
    "wine":        "ワイン",
}


def _pick_pair() -> tuple[dict, dict] | None:
    """Pick 2 products from the same genre in the queue. Returns None if not possible."""
    queue = get_all_queue_entries()
    by_genre: dict[str, list[dict]] = {}
    for entry in queue:
        genre = entry.get("genre", "")
        if genre:
            by_genre.setdefault(genre, []).append(entry)

    eligible = {g: entries for g, entries in by_genre.items() if len(entries) >= 2}
    if not eligible:
        return None

    genre = random.choice(list(eligible.keys()))
    a, b = random.sample(eligible[genre], 2)
    return a, b


def _price_int(entry: dict) -> int:
    raw = entry.get("price", "0").replace("¥", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return 0


def _build_comparison_post(a: dict, b: dict) -> str:
    genre      = a.get("genre", "")
    label      = GENRE_LABELS.get(genre, genre)
    title_a    = a.get("title", "")[:35]
    title_b    = b.get("title", "")[:35]
    price_a    = a.get("price", "—")
    price_b    = b.get("price", "—")
    url_a      = a.get("affiliate_url") or a.get("url", "")
    url_b      = b.get("affiliate_url") or b.get("url", "")

    price_int_a = _price_int(a)
    price_int_b = _price_int(b)
    cheaper = "A" if price_int_a <= price_int_b else "B"

    lines = [
        f"🆚 {label} どっちがいい？",
        "",
        f"【A】{title_a}",
        f"💴 {price_a}",
        f"🔗 {url_a}",
        "",
        f"【B】{title_b}",
        f"💴 {price_b}",
        f"🔗 {url_b}",
        "",
        f"💰 価格が安いのは → {cheaper}",
        "",
        "コメントで教えてください👇",
        "どっちを買いたい？理由も聞かせてね！",
        "",
        f"#楽天市場 #{label}比較 #買って良かった",
    ]
    return "\n".join(lines)


def post_comparison() -> dict:
    """Pick a product pair, build a comparison post, and publish to Threads."""
    pair = _pick_pair()
    if not pair:
        print("[Comparison] Not enough products in queue for a comparison post.")
        return {"status": "skip", "error": "not enough products"}

    a, b = pair
    post_text = _build_comparison_post(a, b)
    print(f"[Comparison] {a.get('title', '')[:30]} vs {b.get('title', '')[:30]}")
    print(post_text[:120] + "...")

    log_entry = {
        "title":     f"{a.get('title','')[:30]} vs {b.get('title','')[:30]}",
        "genre":     "comparison",
        "price":     "",
        "url":       "",
        "account":   "mono_zukan_jp",
        "ab_group":  None,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "status":    None,
        "post_id":   None,
        "error":     None,
    }

    url         = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    last_error  = None

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, params={
                "media_type":   "TEXT",
                "text":         post_text,
                "access_token": THREADS_ACCESS_TOKEN,
            }, timeout=15)
            resp.raise_for_status()
            creation_id = resp.json()["id"]

            resp2 = requests.post(publish_url, params={
                "creation_id":  creation_id,
                "access_token": THREADS_ACCESS_TOKEN,
            }, timeout=15)
            resp2.raise_for_status()
            post_id = resp2.json()["id"]

            log_entry["status"]  = "success"
            log_entry["post_id"] = post_id
            print(f"[Comparison] Posted | post_id={post_id} (attempt {attempt}/3)")
            send_discord(
                f"🆚 比較投稿完了 post_id={post_id}\n"
                f"「{a.get('title','')[:30]}」vs「{b.get('title','')[:30]}」"
            )
            break
        except requests.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"[Comparison RETRY {attempt}/3] {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[Comparison RETRY {attempt}/3] {last_error}")
        if attempt < 3:
            import time
            time.sleep(5)
    else:
        log_entry["status"] = "error"
        log_entry["error"]  = last_error
        print(f"[Comparison FAIL] {last_error}")

    insert_post_log(log_entry)
    return log_entry


if __name__ == "__main__":
    result = post_comparison()
    print(f"\nStatus: {result['status']}")
    if result.get("error"):
        print(f"Error : {result['error']}")
