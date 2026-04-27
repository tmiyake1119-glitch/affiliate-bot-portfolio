import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from ab_tester import assign_group
from formatter import format_products
from post_generator import generate_posts
from product_selector import select_top_products
from trend_agent import get_trending_products
from database import (
    get_successful_post_urls,
    get_all_queue_entries,
    get_pending_queue,
    count_pending_queue,
    get_queue_urls,
    insert_queue_entry,
    update_queue_entry_status,
    is_url_in_queue,
)

logger = logging.getLogger("affiliate_bot.post_manager")


def _recently_posted_urls(days: int = 7) -> set[str]:
    """Return URLs of products successfully posted within the last `days` days."""
    return get_successful_post_urls(days=days)


def save_posts(formatted_products: list[dict], posts: list[str]) -> int:
    existing_urls = get_queue_urls()
    recent_urls = _recently_posted_urls(days=7)

    added = 0
    for product, post_text in zip(formatted_products, posts):
        url = product['url']
        if url in existing_urls:
            continue
        if url in recent_urls:
            print(f"[Skip] Posted within last 7 days: {product['title'][:50]}")
            continue
        # pending 状態での重複確認（キュー競合防止）
        if is_url_in_queue(url):
            logger.warning("URL already pending in queue, skipping: %s", url[:80])
            existing_urls.add(url)
            continue
        insert_queue_entry({
            'url':                product['url'],
            'affiliate_url':      product['affiliate_url'],
            'title':              product['title'],
            'price':              product['price_str'],
            'price_raw':          product.get('price', 0),
            'genre':              product['genre'],
            'image_url':          product.get('image_url', ''),
            'post_text':          post_text,
            'ab_group':           assign_group(),
            'status':             'pending',
            'created_at':         datetime.now(timezone.utc).isoformat(),
            'sent_at':            None,
            'rating_str':         product.get('rating_str', ''),
            'free_shipping':      product.get('free_shipping', False),
            'review_quote':       product.get('review_quote', ''),
            'product_description': product.get('product_description', ''),
            'rank':               product.get('rank', 0),
            'review_average':     product.get('review_average', 0.0),
            'review_count':       product.get('review_count', 0),
            'point_rate':         product.get('point_rate', 1),
            'postage_flag':       product.get('postageFlag', 0),
            'discount_rate':      product.get('discount_rate', 0.0),
            'price_avg_30d':      product.get('price_avg_30d'),
        })
        existing_urls.add(product['url'])
        added += 1

    return added


def get_next_post() -> dict | None:
    pending = get_pending_queue()
    if not pending:
        return None
    entry = pending[0]
    sent_at = datetime.now(timezone.utc).isoformat()
    update_queue_entry_status(entry['id'], 'sent', sent_at)
    entry = dict(entry)
    entry['status'] = 'sent'
    entry['sent_at'] = sent_at
    return entry


def refill_queue(min_pending: int = 3) -> int:
    """If pending posts drop below min_pending, fetch new products and enqueue them.
    Returns the number of posts added."""
    pending = count_pending_queue()
    if pending >= min_pending:
        return 0
    print(f"[Auto-refill] Only {pending} pending post(s) — below threshold of {min_pending}. Refilling...")
    all_products = get_trending_products()
    top_products = select_top_products(all_products)
    formatted    = format_products(top_products)
    posts        = generate_posts(formatted)
    added = save_posts(formatted, posts)
    print(f"[Auto-refill] Added {added} new post(s) to queue.")
    return added


def print_queue_status() -> None:
    queue = get_all_queue_entries()
    pending = sum(1 for e in queue if e['status'] == 'pending')
    sent    = sum(1 for e in queue if e['status'] == 'sent')
    print(f"\n=== Queue Status ===")
    print(f"  Total  : {len(queue)}")
    print(f"  Pending: {pending}")
    print(f"  Sent   : {sent}")


if __name__ == "__main__":
    print("Fetching products and generating posts...")
    all_products = get_trending_products()
    top_products = select_top_products(all_products)
    formatted    = format_products(top_products)
    posts        = generate_posts(formatted)

    added = save_posts(formatted, posts)
    print(f"\nAdded {added} new post(s) to queue (duplicates skipped).")
    print_queue_status()
