import os
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SEASONAL_KEYWORDS: dict[int, list[str]] = {
    1:  ["正月", "新年", "初売り", "冬"],
    2:  ["バレンタイン", "冬", "節分"],
    3:  ["新生活", "春", "卒業", "引っ越し"],
    4:  ["新生活", "入学", "春", "花見"],
    5:  ["ゴールデンウィーク", "母の日", "春"],
    6:  ["梅雨", "父の日", "夏準備"],
    7:  ["夏", "熱中症対策", "キャンプ", "海"],
    8:  ["夏", "お盆", "キャンプ", "アウトドア"],
    9:  ["秋", "運動会", "敬老の日"],
    10: ["ハロウィン", "秋", "紅葉"],
    11: ["冬準備", "鍋", "防寒"],
    12: ["クリスマス", "年末", "ギフト", "大掃除"],
}

# Upcoming events with (month, day_start, day_end, keyword, score_boost)
# score_boost is added to product sort score when product name/genre matches keyword
UPCOMING_EVENTS: list[tuple[int, int, int, str, float]] = [
    (1,  1,  7,   "正月",           0.3),
    (2,  7,  14,  "バレンタイン",   0.3),
    (3,  1,  31,  "新生活",         0.2),
    (4,  1,  15,  "花見",           0.2),
    (5,  1,  7,   "ゴールデンウィーク", 0.2),
    (5,  1,  14,  "母の日",         0.3),
    (6,  1,  20,  "父の日",         0.3),
    (6,  1,  30,  "梅雨",           0.2),
    (7,  1,  31,  "夏",             0.2),
    (8,  1,  31,  "夏休み",         0.2),
    (9,  15, 25,  "敬老の日",       0.3),
    (10, 20, 31,  "ハロウィン",     0.3),
    (11, 1,  25,  "冬準備",         0.2),
    (12, 1,  25,  "クリスマス",     0.4),
    (12, 20, 31,  "大掃除",         0.3),
    # Furusato nozei peak season (Oct–Dec year-end giving)
    (10, 1,  31,  "ふるさと納税",   2.0),
    (11, 1,  30,  "ふるさと納税",   2.0),
    (12, 1,  31,  "ふるさと納税",   2.0),
]


# Rakuten Super Sale: 4th–11th of each month
# Rakuten 買い回り (round & round): 1st–7th of each month
def get_sale_info(day: int | None = None) -> tuple[str | None, int]:
    """Return (sale_keyword, extra_hits). extra_hits is added to base search hits."""
    if day is None:
        day = datetime.now().day
    if 4 <= day <= 11:
        return "スーパーセール", 20
    if 1 <= day <= 7:
        return "買い回り", 20
    return None, 0


def get_seasonal_keywords(month: int | None = None, day: int | None = None) -> list[str]:
    if month is None:
        month = datetime.now().month
    keywords = list(SEASONAL_KEYWORDS.get(month, []))
    # Also include keywords from the next month if within 10 days of month-end
    if day is None:
        day = datetime.now().day
    try:
        import calendar
        last_day = calendar.monthrange(datetime.now().year, month)[1]
        if last_day - day <= 10:
            next_month = (month % 12) + 1
            for kw in SEASONAL_KEYWORDS.get(next_month, [])[:2]:
                if kw not in keywords:
                    keywords.append(kw)
    except Exception:
        pass
    sale_kw, _ = get_sale_info(day)
    if sale_kw:
        keywords.insert(0, sale_kw)
    return keywords


def get_seasonal_boost(product_name: str, month: int | None = None, day: int | None = None) -> float:
    """Return a score boost multiplier (0.0–0.4) if the product matches active/upcoming seasonal events."""
    now = datetime.now()
    if month is None:
        month = now.month
    if day is None:
        day = now.day

    total_boost = 0.0
    name_lower = product_name.lower() if product_name else ""
    for ev_month, ev_start, ev_end, ev_keyword, ev_boost in UPCOMING_EVENTS:
        if ev_month != month:
            continue
        if ev_start <= day <= ev_end:
            if ev_keyword in product_name:
                total_boost += ev_boost
    return min(total_boost, 0.5)  # cap at 0.5


if __name__ == "__main__":
    now = datetime.now()
    keywords = get_seasonal_keywords(now.month, now.day)
    sale_kw, extra_hits = get_sale_info(now.day)
    print(f"Seasonal keywords for {now.strftime('%Y-%m-%d')} (month={now.month}, day={now.day}):")
    for kw in keywords:
        print(f"  - {kw}")
    if sale_kw:
        print(f"\nSale active: {sale_kw}  (search hits +{extra_hits})")
    else:
        print("\nNo sale period active today.")
