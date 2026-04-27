"""
url_shortener.py

Shortens URLs via the TinyURL API (free, no key required).
Results are cached in data/url_cache.json to avoid duplicate API calls.
"""

import json
import os
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CACHE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'url_cache.json')
TINYURL_API = "https://tinyurl.com/api-create.php"


def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def shorten_url(long_url: str) -> str:
    """
    Return a shortened TinyURL for long_url.
    Uses cache to avoid duplicate API calls.
    Falls back to the original URL on any error.
    """
    if not long_url:
        return long_url

    cache = _load_cache()
    if long_url in cache:
        return cache[long_url]

    try:
        resp = requests.get(
            TINYURL_API,
            params={"url": long_url},
            timeout=10,
        )
        resp.raise_for_status()
        short = resp.text.strip()
        if short.startswith("http"):
            cache[long_url] = short
            _save_cache(cache)
            return short
    except Exception as e:
        print(f"[URLShortener] Failed to shorten URL: {e}")

    return long_url


if __name__ == "__main__":
    samples = [
        "https://hb.afl.rakuten.co.jp/ichiba/5249977a.da5b56e0.5249977b.7566a1b1/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Ftest%2F",
        "https://www.amazon.co.jp/s?k=%E3%82%B3%E3%83%BC%E3%83%89%E3%83%AC%E3%82%B9&tag=whiskycardbot-22",
    ]
    for url in samples:
        short = shorten_url(url)
        print(f"Original : {url[:60]}...")
        print(f"Shortened: {short}")
        print()
