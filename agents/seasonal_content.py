"""
seasonal_content.py

Provides genre-based seasonal boost multipliers and hashtags based on the current month.
"""
from datetime import datetime

# Bimonthly seasonal data: months → keywords and boosted genres
_SEASONAL_DATA: list[tuple[tuple[int, ...], list[str], list[str]]] = [
    ((3, 4),   ["新生活", "入学", "花見", "春"],              ["food", "kitchen", "daily_goods"]),
    ((5, 6),   ["母の日", "梅雨", "UV対策"],                  ["cosmetics", "health"]),
    ((7, 8),   ["夏", "BBQ", "熱中症対策", "アウトドア"],      ["outdoor", "pet"]),
    ((9, 10),  ["秋", "運動会", "ハロウィン"],                 ["food", "health"]),
    ((11, 12), ["クリスマス", "お歳暮", "年末", "ふるさと納税"], ["furusato", "food", "alcohol"]),
    ((1, 2),   ["新年", "バレンタイン", "防寒"],               ["food", "alcohol", "health"]),
]

# Exposed dict for introspection / testing: month → keywords
SEASONAL_KEYWORDS: dict[int, list[str]] = {}
for _months, _keywords, _genres in _SEASONAL_DATA:
    for _m in _months:
        SEASONAL_KEYWORDS[_m] = _keywords


def _current_season(month: int | None = None) -> tuple[list[str], list[str]] | None:
    """Return (keywords, genres) for the current month, or None if not found."""
    if month is None:
        month = datetime.now().month
    for months, keywords, genres in _SEASONAL_DATA:
        if month in months:
            return keywords, genres
    return None


def get_seasonal_boost(genre: str, month: int | None = None) -> float:
    """Return a score multiplier (1.0–1.5) for the given genre based on the current season."""
    season = _current_season(month)
    if season and genre in season[1]:
        return 1.5
    return 1.0


def get_seasonal_hashtags(month: int | None = None) -> list[str]:
    """Return a list of seasonal hashtags for the current month."""
    season = _current_season(month)
    if not season:
        return []
    return [f"#{kw}" for kw in season[0]]


if __name__ == "__main__":
    now = datetime.now()
    season = _current_season(now.month)
    print(f"Month: {now.month}")
    if season:
        print(f"Keywords : {season[0]}")
        print(f"Genres   : {season[1]}")
        print(f"Hashtags : {get_seasonal_hashtags(now.month)}")
    else:
        print("No season data for this month.")
    print(f"\nBoost examples:")
    for g in ["food", "outdoor", "electronics", "furusato"]:
        print(f"  {g:15} → {get_seasonal_boost(g, now.month)}")
