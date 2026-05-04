import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

logger = logging.getLogger("affiliate_bot.trend_agent")

sys.path.insert(0, os.path.dirname(__file__))

from keyword_agent import get_seasonal_keywords, get_sale_info
from database import get_price_history, add_price_history

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

RAKUTEN_APP_ID     = os.getenv("RAKUTEN_APP_ID")
RAKUTEN_ACCESS_KEY = os.getenv("RAKUTEN_ACCESS_KEY")
SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

_RETRY_WAITS = [5, 10]  # 429 時のリトライ待機秒数（1回目・2回目）


def fetch_rakuten_with_retry(url: str, params: dict, max_retries: int = 3) -> requests.Response | None:
    """Rakuten API を呼び出す。429 が返った場合はバックオフしてリトライする。

    Returns:
        requests.Response  成功時
        None               max_retries 回失敗時
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                wait = _RETRY_WAITS[attempt - 1] if attempt - 1 < len(_RETRY_WAITS) else 30
                logger.warning(
                    "Rakuten API 429 Too Many Requests (attempt %d/%d). "
                    "Waiting %ds before retry.",
                    attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            logger.warning("Rakuten API HTTP error (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(_RETRY_WAITS[min(attempt - 1, len(_RETRY_WAITS) - 1)])
        except Exception as e:
            logger.warning("Rakuten API error (attempt %d/%d): %s", attempt, max_retries, e)
            return None

    logger.warning("Rakuten API: max retries (%d) exceeded. Skipping.", max_retries)
    return None


def _check_price_drop(url: str, current_price: int) -> tuple[bool, float, int]:
    """Return (dropped, drop_pct, old_price). Adds new price point to DB."""
    now    = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=30)).isoformat()

    entry = [p for p in get_price_history(url) if p.get("timestamp", "") >= cutoff]
    prev_price = entry[-1]["price"] if entry else None

    add_price_history(url, current_price, now.isoformat())

    if prev_price and prev_price > current_price:
        drop_pct = round((prev_price - current_price) / prev_price * 100, 1)
        if drop_pct >= 5.0:
            return True, drop_pct, prev_price
    return False, 0.0, 0

USED_ITEM_KEYWORDS = ["中古", "used", "USED", "Used", "訳あり", "ジャンク", "返品", "難あり"]


def _is_used_item(name: str) -> bool:
    return any(kw in name for kw in USED_ITEM_KEYWORDS)


GENRES = {
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
    "contact":     "コンタクトレンズ 人気 ワンデー",
}

WINE_GIFT_KEYWORDS = ["ギフト", "セット", "飲み比べ", "木箱", "化粧箱", "詰め合わせ"]

PET_CONSUMABLE_KEYWORDS = ["フード", "ごはん", "おやつ", "シート", "砂", "消臭", "ペットシーツ"]
PET_CARE_KEYWORDS       = ["シャンプー", "ブラシ", "爪切り", "歯ブラシ", "サプリ", "ケア"]
PET_TOY_KEYWORDS        = ["おもちゃ", "ボール", "ロープ", "ベッド", "マット", "クッション"]


def classify_wine(product: dict) -> str:
    name  = product.get("name", "")
    price = product.get("price", 0)
    if any(kw in name for kw in WINE_GIFT_KEYWORDS):
        return "wine_gift"
    if price >= 3000:
        return "wine_premium"
    return "wine_daily"


def classify_pet(product: dict) -> str:
    name = product.get("name", "")
    if any(kw in name for kw in PET_CONSUMABLE_KEYWORDS):
        return "pet_consumable"
    if any(kw in name for kw in PET_CARE_KEYWORDS):
        return "pet_care"
    if any(kw in name for kw in PET_TOY_KEYWORDS):
        return "pet_toy"
    return "pet"


def fetch_ranking(genre_name: str, keyword: str, hits: int = 30) -> list[dict]:
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey":     RAKUTEN_ACCESS_KEY,
        "keyword":       keyword,
        "hits":          hits,
        "sort":          "-reviewCount",
        "format":        "json",
        "formatVersion": 2,
        "itemTypeId":    1,   # 1=新品 only (excludes 中古/再生品)
        "availability":  1,   # 在庫あり only
    }
    response = fetch_rakuten_with_retry(SEARCH_URL, params=params)
    if response is None:
        logger.warning("fetch_ranking スキップ (genre=%s)", genre_name)
        return []
    data = response.json()

    products = []
    for i, item in enumerate(data.get("Items", []), start=1):
        if _is_used_item(item.get("itemName", "")):
            continue
        medium_images = item.get("mediumImageUrls", [])
        image_url = medium_images[0].replace("_ex=128x128", "_ex=600x600") if medium_images else ""
        item_url      = item.get("itemUrl", "")
        current_price = item.get("itemPrice") or 0

        dropped, drop_pct, old_price = _check_price_drop(item_url, current_price)
        point_rate = item.get("pointRate", 1)

        product = {
            "rank":           i,
            "name":           item.get("itemName"),
            "price":          current_price,
            "genre":          genre_name,
            "url":            item_url,
            "image_url":      image_url,
            "review_count":   item.get("reviewCount", 0),
            "review_average": item.get("reviewAverage", 0.0),
            "item_caption":   item.get("itemCaption", "")[:200] if item.get("itemCaption") else "",
            "point_rate":     int(point_rate) if point_rate else 1,
            "price_dropped":  dropped,
            "price_drop_pct": drop_pct if dropped else None,
            "price_old":      old_price if dropped else None,
            "postageFlag":    item.get("postageFlag", 0),
            "limitedFlag":    item.get("limitedFlag", 0),
            "asurakuFlag":    item.get("asurakuFlag", 0),
        }
        if genre_name == "wine":
            product["genre"] = classify_wine(product)
        elif genre_name == "pet":
            product["genre"] = classify_pet(product)
        products.append(product)

    if genre_name in ("wine", "pet"):
        sub_counts: dict[str, int] = {}
        for p in products:
            sub_counts[p["genre"]] = sub_counts.get(p["genre"], 0) + 1
        logger.info("[TrendAgent] %s subcategory counts: %s", genre_name, sub_counts)
        print(f"[TrendAgent] {genre_name} subcategory counts: {sub_counts}")

    return products


def get_trending_products() -> list[dict]:
    seasonal = get_seasonal_keywords()
    seasonal_suffix = f" {seasonal[0]}" if seasonal else ""
    _, extra_hits = get_sale_info()
    hits = min(30 + extra_hits, 30)  # 楽天 API の hits 上限は 30

    all_products = []
    for genre_name, keyword in GENRES.items():
        full_keyword = keyword + seasonal_suffix
        print(f"Fetching search for: {genre_name} (keyword={full_keyword}, hits={hits})")
        try:
            products = fetch_ranking(genre_name, full_keyword, hits=hits)
            all_products.extend(products)
            print(f"  -> {len(products)} items fetched")
        except requests.RequestException as e:
            print(f"  -> Error fetching {genre_name}: {e}")
        time.sleep(1)
    return all_products


if __name__ == "__main__":
    products = get_trending_products()
    print(f"\n=== Trending Products ({len(products)} total) ===")
    for p in products:
        print(f"[{p['genre']:12}] #{p['rank']:>2}  JPY{p['price']:>7,}  {p['name'][:60]}")
