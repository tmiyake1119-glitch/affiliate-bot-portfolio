import os
import sys
from urllib.parse import quote

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(__file__))

from amazon_linker import find_matching_amazon_product
from dotenv import load_dotenv
from url_shortener import shorten_url
from product_selector import select_top_products
from sale_detector import is_time_limited
from trend_agent import get_trending_products

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID")


_TASTING_KEYWORDS = ["香り", "味わい", "テイスティング", "フレーバー", "飲み口"]


def _extract_product_description(caption: str, genre: str) -> str:
    if not caption:
        return ""
    if genre in ("alcohol", "wine"):
        for sentence in caption.split("。"):
            sentence = sentence.strip()
            if sentence and any(kw in sentence for kw in _TASTING_KEYWORDS):
                return sentence[:100]
        return caption[:80].strip()
    return caption[:80].strip()



def format_product(product: dict) -> dict:
    item_url = product.get("url", "")
    affiliate_url = (
        f"https://hb.afl.rakuten.co.jp/ichiba/{RAKUTEN_AFFILIATE_ID}/?pc={quote(item_url, safe='')}"
        if RAKUTEN_AFFILIATE_ID and item_url
        else item_url
    )
    review_summary = ""
    old_price      = product.get("price_old")
    amazon_url     = find_matching_amazon_product(product)

    # Star rating — only show if >= 4.0
    rating      = product.get("review_average", 0.0) or 0.0
    review_count = product.get("review_count", 0) or 0
    rating_str  = f"⭐{rating}点（{review_count}件）" if rating >= 4.0 and review_count > 0 else ""

    # Free shipping badge
    free_shipping = bool(product.get("postageFlag") == 1)

    review_quote = ""
    product_description = _extract_product_description(
        product.get("item_caption", ""), product.get("genre", "")
    )

    return {
        "title":          product.get("name", ""),
        "price":          product.get("price", 0),
        "price_str":      f"¥{product['price']:,}" if product.get("price") else "",
        "url":            item_url,
        "genre":          product.get("genre", ""),
        "affiliate_url":  shorten_url(affiliate_url),
        "image_url":      product.get("image_url", ""),
        "review_summary": review_summary,
        "point_rate":     product.get("point_rate"),
        "price_dropped":  product.get("price_dropped", False),
        "price_drop_pct": product.get("price_drop_pct"),
        "price_old_str":  f"¥{old_price:,}" if old_price else None,
        "amazon_url":     shorten_url(amazon_url) if amazon_url else "",
        "rating_str":     rating_str,
        "free_shipping":  free_shipping,
        "review_quote":        review_quote,
        "product_description": product_description,
        "rank":           product.get("rank", 0),
        "review_average": rating,
        "review_count":   review_count,
        "postageFlag":    product.get("postageFlag", 0),
        "time_limited":   is_time_limited(product),
        "discount_rate":  product.get("discount_rate", 0.0),
        "price_avg_30d":  product.get("price_avg_30d"),
    }


def format_products(products: list[dict]) -> list[dict]:
    return [format_product(p) for p in products]


if __name__ == "__main__":
    print("Fetching and selecting top products...")
    top_products = select_top_products(get_trending_products())
    formatted = format_products(top_products)

    print(f"\n=== Formatted Products ({len(formatted)} total) ===\n")
    for i, p in enumerate(formatted, start=1):
        print(f"{i:>2}. [{p['genre']:12}] {p['price_str']}")
        print(f"    Title: {p['title'][:60]}")
        print(f"    URL:   {p['url'][:80]}")
        print(f"    Aff:   {p['affiliate_url'][:80]}")
        print()
