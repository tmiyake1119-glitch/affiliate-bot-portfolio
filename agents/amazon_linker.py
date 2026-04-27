import os
import re
import sys
from urllib.parse import quote

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

AMAZON_ASSOCIATE_ID = os.getenv("AMAZON_ASSOCIATE_ID", "")


def generate_amazon_link(product_name: str) -> str:
    """Return an Amazon.co.jp search affiliate URL for the given product name."""
    encoded = quote(product_name, safe='')
    url = f"https://www.amazon.co.jp/s?k={encoded}"
    if AMAZON_ASSOCIATE_ID:
        url += f"&tag={AMAZON_ASSOCIATE_ID}"
    return url


def _clean_title(title: str) -> str:
    """Strip Rakuten noise (specs, punctuation, repeated words) to get a clean search query."""
    # Remove common Rakuten padding patterns
    title = re.sub(r'【[^】]*】', '', title)      # 【...】blocks
    title = re.sub(r'［[^］]*］', '', title)      # ［...］blocks
    title = re.sub(r'\([^)]*\)', '', title)       # (...)
    title = re.sub(r'[/｜\|★☆◆◇●○▲△■□]', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip()
    # Keep only the first 30 chars to avoid over-specified queries
    return title[:30].strip()


def find_matching_amazon_product(product: dict) -> str:
    """Take a Rakuten product dict and return an Amazon affiliate search URL."""
    title = product.get("name", product.get("title", ""))
    query = _clean_title(title) if title else ""
    if not query:
        return ""
    return generate_amazon_link(query)


if __name__ == "__main__":
    samples = [
        {"name": "【楽天1位】コードレス掃除機 軽量 強力吸引 充電式 スティック型"},
        {"name": "【ふるさと納税】エアウィーヴ スマートZ1 寝具 マットレストッパー"},
        {"name": "山崎蒸溜所 シングルモルトウイスキー 700ml"},
    ]
    for p in samples:
        link = find_matching_amazon_product(p)
        print(f"Title : {p['name'][:50]}")
        print(f"Amazon: {link}")
        print()
