"""
sale_detector.py

Detects whether the current date falls within known Rakuten sale periods.

Rakuten Super Sale: runs ~1 week in March, June, September, December
Rakuten Marathon:   runs ~1 week at the start of most months (roughly days 1-5)
"""

from datetime import date


# Super Sale months (day ranges are approximate; actual dates vary year to year)
_SUPER_SALE_MONTHS = {3, 6, 9, 12}
_SUPER_SALE_DAYS   = range(1, 12)   # typically first ~10 days of the month

# Marathon typically runs for ~4 days, roughly twice a month
_MARATHON_DAYS = set(range(1, 6)) | set(range(15, 20))

# 0と5のつく日: Rakuten Card bonus point days
_ZERO_FIVE_DAYS = {5, 10, 15, 20, 25, 30}


def get_sale_info(today: date | None = None) -> dict:
    """
    Return sale info for the given date (defaults to today).

    Returns:
        {
            "is_sale":      bool,
            "sale_type":    str | None,   e.g. "スーパーSALE" or "マラソン"
            "banner":       str | None,   ready-to-use emoji banner
            "discount_info": str | None,
        }
    """
    if today is None:
        today = date.today()

    month = today.month
    day   = today.day

    if month in _SUPER_SALE_MONTHS and day in _SUPER_SALE_DAYS:
        return {
            "is_sale":       True,
            "sale_type":     "スーパーSALE",
            "banner":        "🎉 楽天スーパーSALE開催中！",
            "discount_info": "期間限定で最大50%OFF＋ポイント最大43倍！",
        }

    if day in _MARATHON_DAYS:
        return {
            "is_sale":       True,
            "sale_type":     "マラソン",
            "banner":        "🛒 お買い物マラソン開催中！",
            "discount_info": "お買い回りでポイント最大10倍！",
        }

    if day in _ZERO_FIVE_DAYS:
        return {
            "is_sale":       True,
            "sale_type":     "0と5のつく日",
            "banner":        "🗓️ 0と5のつく日！",
            "discount_info": "楽天カード利用でポイント5倍！",
        }

    return {
        "is_sale":       False,
        "sale_type":     None,
        "banner":        None,
        "discount_info": None,
    }


def is_sale_day(today: date | None = None) -> tuple[bool, str]:
    """Return (True, sale_name) if today is a Rakuten sale day, else (False, '')."""
    info = get_sale_info(today)
    if info["is_sale"]:
        return True, info["sale_type"]
    return False, ""


def get_sale_banner(today: date | None = None) -> str:
    """Return a ready-to-prepend banner string, or '' if no sale is active."""
    info = get_sale_info(today)
    if not info["is_sale"]:
        return ""
    return f"{info['banner']}\n{info['discount_info']}\n\n"


def is_time_limited(product: dict) -> bool:
    """Return True if the product has Rakuten limited-stock or flash-sale flags.

    Rakuten API fields checked:
      - limitedFlag (1 = limited availability)
      - pointRate > 5 combined with asurakuFlag (same-day shipping + high points
        is a strong signal of a flash promotion)
    """
    if product.get("limitedFlag") == 1:
        return True
    if (product.get("pointRate", 1) or 1) > 5 and product.get("asurakuFlag") == 1:
        return True
    return False


if __name__ == "__main__":
    import datetime
    today = datetime.date.today()
    info  = get_sale_info(today)
    print(f"Date : {today}")
    print(f"Sale : {info}")
