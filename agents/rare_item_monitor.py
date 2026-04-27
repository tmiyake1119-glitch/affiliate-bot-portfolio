"""
rare_item_monitor.py  —  full lifecycle management

State file: data/rare_items_seen.json
Per-item structure:
{
  "<itemCode>": {
    "name": str, "price": int, "keyword": str, "category": str,
    "first_seen": ISO, "last_seen": ISO, "last_checked": ISO,
    "posted": bool, "sold_out": bool, "sold_out_posted": bool,
    "restock_count": int, "price_history": [{"price": int, "timestamp": ISO}],
    "daily_post_count": {"date": str, "count": int}
  }
}

Lifecycle events:
  NEW      → 🔥 入荷速報！ / 📦 予約受付中！
  RESTOCK  → 🔄 再入荷！
  SOLDOUT  → ⚠️ 売り切れ・予約終了
  (price drop / low stock are added as extras to the above)
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from discord_notifier import send_discord
from database import (
    get_all_rare_items_with_history, upsert_rare_item,
    save_rare_item_price_history_bulk,
)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JST = timezone(timedelta(hours=9))

RAKUTEN_APP_ID       = os.getenv("RAKUTEN_APP_ID", "")
RAKUTEN_ACCESS_KEY   = os.getenv("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID", "")
SEARCH_URL    = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
LAST_RUN_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'rare_monitor_last_run.json')

_PREORDER_WORDS              = ("予約", "受注")
_MAX_DELIVERY_DAYS           = 60
_MAX_DAILY_POSTS_PER_KEYWORD = 2          # max posts per keyword per day
_MAX_POSTS_PER_RUN           = 2          # 1実行あたりの投稿上限
_POST_INTERVAL_SEC           = 300        # 投稿間のウェイト（秒）
_MIN_RUN_INTERVAL_HOURS      = 2          # skip run if last run < 2 hours ago
_MIN_SAME_ITEM_INTERVAL_H    = 24         # ③ 同一商品の最低再投稿間隔（時間）
_MIN_GENRE_INTERVAL_MIN      = 30         # ③ 同一カテゴリの最低投稿間隔（分、実行内）

# JST hours when main bot posts — rare monitor skips posting to avoid overlap
MAIN_POST_HOURS: set[int] = {9, 21}

RARE_KEYWORDS: list[dict] = [
    {"keyword": "サントリー山崎",            "typical_price":  7000},
    {"keyword": "白州",                      "typical_price":  6000},
    {"keyword": "響",                        "typical_price":  6500},
    {"keyword": "フロムザバレル",            "typical_price":  3500},
    {"keyword": "イチローズモルト",          "typical_price":  6000},
    {"keyword": "ウイスキー 新発売",          "typical_price": 15000},
    {"keyword": "ウイスキー 予約",            "typical_price": 15000},
    {"keyword": "ジャパニーズウイスキー 新発売", "typical_price": 20000},
    {"keyword": "バーボン 新発売",            "typical_price":  8000},
    {"keyword": "スコッチ 新発売",            "typical_price": 10000},
    {"keyword": "限定ウイスキー",             "typical_price": 20000},
    # ポケモンカード系: 売り切れ率0%のため除外
    # {"keyword": "ポケモンカード 強化拡張パック",  "typical_price":  3000, "min_price":  800},
    # {"keyword": "ポケモンカード BOX 予約",        "typical_price": 15000, "min_price":  800},
    # {"keyword": "ポケモンカード スターターセット", "typical_price":  2000, "min_price":  800},
    {"keyword": "遊戯王 パック",                      "typical_price":  3000, "min_price":  800},
    {"keyword": "遊戯王 ボックス",                    "typical_price":  8000, "min_price":  800},
    {"keyword": "遊戯王 レアリティコレクション",      "typical_price":  8000, "min_price": 3000},
    {"keyword": "遊戯王 プレミアムパック",            "typical_price":  5000, "min_price": 2000},
    {"keyword": "遊戯王 デュエリストパック",          "typical_price":  3000, "min_price":  800},
    {"keyword": "ワンピースカード 新弾",              "typical_price":  5000, "min_price":  800},
    {"keyword": "ワンピースカード BOX 予約",          "typical_price": 15000, "min_price": 3000},
    {"keyword": "ワンピースカード スターターデッキ",  "typical_price":  2000, "min_price":  800},
    {"keyword": "ソニー ヘッドフォン 限定",       "typical_price": 50000},
    {"keyword": "ゼンハイザー 限定",              "typical_price": 40000},
    {"keyword": "ナイキ 限定 スニーカー",         "typical_price": 20000},
    {"keyword": "アディダス コラボ 限定",         "typical_price": 15000},
    {"keyword": "ワンピース フィギュア 限定",     "typical_price": 15000, "min_price": 3000},
    {"keyword": "鬼滅の刃 フィギュア 限定",      "typical_price": 10000, "min_price": 3000},
    {"keyword": "ドラゴンボール フィギュア 限定", "typical_price": 10000, "min_price": 3000},
    {"keyword": "Nintendo Switch2 予約",         "typical_price": 50000, "min_price": 2000},
    {"keyword": "ポケモン 新作 予約",             "typical_price":  6000, "min_price": 2000},
    {"keyword": "ゼルダ 新作 予約",               "typical_price":  8000, "min_price": 2000},
    {"keyword": "マリオ 新作 予約",               "typical_price":  6000, "min_price": 2000},
    {"keyword": "ゲーム 初回特典 予約",           "typical_price":  8000, "min_price": 2000},
    {"keyword": "PS5 ソフト 新作 予約",           "typical_price":  8000, "min_price": 2000},
    {"keyword": "スプラトゥーン 新作",            "typical_price":  6000, "min_price": 2000},
]

WHISKY_KEYWORDS: set[str] = {
    "サントリー山崎", "白州", "響", "フロムザバレル", "イチローズモルト",
    "ウイスキー 新発売", "ウイスキー 予約", "ジャパニーズウイスキー 新発売",
    "バーボン 新発売", "スコッチ 新発売", "限定ウイスキー",
}

GAME_KEYWORDS: set[str] = {
    "Nintendo Switch2 予約", "ポケモン 新作 予約", "ゼルダ 新作 予約",
    "マリオ 新作 予約", "ゲーム 初回特典 予約", "PS5 ソフト 新作 予約",
    "スプラトゥーン 新作",
}

HOBBY_KEYWORDS: set[str] = GAME_KEYWORDS | {
    "遊戯王 パック", "遊戯王 ボックス",
    "遊戯王 レアリティコレクション", "遊戯王 プレミアムパック", "遊戯王 デュエリストパック",
    "ワンピースカード 新弾", "ワンピースカード BOX 予約", "ワンピースカード スターターデッキ",
    "ワンピース フィギュア 限定", "鬼滅の刃 フィギュア 限定", "ドラゴンボール フィギュア 限定",
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_jst() -> datetime:
    return datetime.now(JST)


def _today_str() -> str:
    return _now_jst().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_quiet_hours() -> bool:
    """0:00–7:00 JST → no posting, continue checking."""
    return 0 <= _now_jst().hour < 7


def _is_weekly_summary_time() -> bool:
    """Sunday 21:00 JST."""
    now = _now_jst()
    return now.weekday() == 6 and now.hour == 21


def _is_main_post_hour() -> bool:
    """Return True if current JST hour overlaps with main bot posting schedule.
    Posting is skipped during these hours; checking continues normally."""
    return _now_jst().hour in MAIN_POST_HOURS


def _load_last_run() -> datetime | None:
    """Return the UTC datetime of the last completed run, or None."""
    if not os.path.exists(LAST_RUN_PATH):
        return None
    try:
        with open(LAST_RUN_PATH, 'r', encoding='utf-8') as f:
            ts = json.load(f).get("last_run")
        return datetime.fromisoformat(ts) if ts else None
    except Exception:
        return None


def _save_last_run() -> None:
    os.makedirs(os.path.dirname(LAST_RUN_PATH), exist_ok=True)
    with open(LAST_RUN_PATH, 'w', encoding='utf-8') as f:
        json.dump({"last_run": _now_iso()}, f)


def _too_soon_since_last_run() -> bool:
    """Return True if the last run was less than _MIN_RUN_INTERVAL_HOURS ago."""
    last = _load_last_run()
    if last is None:
        return False
    elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return elapsed_hours < _MIN_RUN_INTERVAL_HOURS


# ---------------------------------------------------------------------------
# Item name analysis helpers
# ---------------------------------------------------------------------------

USED_ITEM_KEYWORDS = ["中古", "used", "USED", "Used", "訳あり", "ジャンク", "返品", "難あり"]
SINGLE_CARD_KEYWORDS = ["シングル", "1枚", "一枚", "単品カード", "バラ", "ノーマル", "コモン", "アンコモン"]


def _is_used_item(name: str) -> bool:
    return any(kw in name for kw in USED_ITEM_KEYWORDS)


def _is_single_card(name: str) -> bool:
    return any(kw in name for kw in SINGLE_CARD_KEYWORDS)


def _is_preorder(name: str) -> bool:
    return any(w in name for w in _PREORDER_WORDS)


def _extract_delivery_date(name: str) -> date | None:
    today = date.today()

    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = re.search(r'(\d{4})年(\d{1,2})月', name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            pass

    m = re.search(r'(\d{1,2})月', name)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            year = today.year if month >= today.month else today.year + 1
            try:
                return date(year, month, 1)
            except ValueError:
                pass

    return None


def _delivery_too_far(name: str) -> bool:
    d = _extract_delivery_date(name)
    if d is None:
        return False
    return (d - date.today()).days > _MAX_DELIVERY_DAYS


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _load_seen() -> dict:
    """DB から全レアアイテムを {item_code: record} 形式で返す。"""
    items = get_all_rare_items_with_history()
    return {item["item_code"]: item for item in items}


def _save_seen(seen: dict) -> None:
    """DB に全レアアイテムを upsert する。"""
    for item_code, record in seen.items():
        record["item_code"] = item_code
        price_history = record.get("price_history", [])
        upsert_rare_item(record)
        save_rare_item_price_history_bulk(item_code, price_history)


def _new_record(item: dict, keyword: str, category: str) -> dict:
    now   = _now_iso()
    price = item.get("itemPrice") or 0
    return {
        "name":             item.get("itemName", "")[:100],
        "price":            price,
        "keyword":          keyword,
        "category":         category,
        "first_seen":       now,
        "last_seen":        now,
        "last_checked":     now,
        "posted":           False,
        "sold_out":         False,
        "sold_out_posted":  False,
        "restock_count":    0,
        "price_history":    [{"price": price, "timestamp": now}],
        "daily_post_count": {"date": _today_str(), "count": 0},
    }


def _update_price_history(record: dict, current_price: int) -> None:
    history = record.setdefault("price_history", [])
    if not history or history[-1]["price"] != current_price:
        history.append({"price": current_price, "timestamp": _now_iso()})
    if len(history) > 30:
        record["price_history"] = history[-30:]


def _check_price_drop(record: dict, current_price: int) -> tuple[bool, float]:
    """Return (dropped, drop_pct). Dropped when >5% cheaper than last recorded price."""
    history = record.get("price_history", [])
    if not history:
        return False, 0.0
    last_price = history[-1]["price"]
    if last_price <= 0 or current_price >= last_price:
        return False, 0.0
    drop_pct = (last_price - current_price) / last_price * 100
    return drop_pct > 5.0, round(drop_pct, 1)


def _keyword_daily_count(seen: dict, keyword: str) -> int:
    """Sum of today's post counts across all items for this keyword."""
    today = _today_str()
    total = 0
    for rec in seen.values():
        if rec.get("keyword") != keyword:
            continue
        dpc = rec.get("daily_post_count", {})
        if dpc.get("date") == today:
            total += dpc.get("count", 0)
    return total


def _increment_daily_count(record: dict) -> None:
    today = _today_str()
    dpc   = record.setdefault("daily_post_count", {})
    if dpc.get("date") != today:
        record["daily_post_count"] = {"date": today, "count": 1}
    else:
        dpc["count"] = dpc.get("count", 0) + 1


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """
    Strip shop-specific decorations to get a comparable product name.
    Removes:
      - 【…】 and ［…］ bracket blocks (shop prefixes / badges)
      - Leading/trailing whitespace and runs of spaces
      - Standalone numbers and single punctuation tokens
    """
    # Remove bracket blocks: 【...】 ［...］ 《...》 「...」 (non-greedy)
    name = re.sub(r'[【【】】\[［\]］《》「」『』].*?[【【】】\[［\]］《》「」『』]', '', name)
    name = re.sub(r'[【\[［《「『][^】\]］》」』]*[】\]］》」』]', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    # Lower-case for comparison only (original name kept on the item dict)
    return name.lower()


def _deduplicate_by_name(items: list[dict]) -> list[dict]:
    """
    Group items by normalized product name, keep only the cheapest per group.
    Returns the deduplicated list; each surviving item gets a 'shop_count' field
    indicating how many listings were merged.
    """
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = _normalize_name(item.get("itemName", ""))
        groups.setdefault(key, []).append(item)

    result: list[dict] = []
    for key, group in groups.items():
        cheapest = min(group, key=lambda i: i.get("itemPrice") or 0)
        cheapest = dict(cheapest)  # shallow copy so we don't mutate the original
        cheapest["shop_count"] = len(group)
        result.append(cheapest)

    removed = len(items) - len(result)
    if removed:
        print(f"[RareMonitor] Deduplication: {len(items)} → {len(result)} items "
              f"({removed} duplicate shop listing(s) removed)")
    return result


# ---------------------------------------------------------------------------
# Rakuten / URL helpers
# ---------------------------------------------------------------------------

def _search_rakuten(keyword: str, hits: int = 20) -> list[dict]:
    try:
        resp = requests.get(SEARCH_URL, params={
            "applicationId": RAKUTEN_APP_ID,
            "accessKey":     RAKUTEN_ACCESS_KEY,
            "keyword":       keyword,
            "hits":          hits,
            "sort":          "-updateTimestamp",
            "format":        "json",
            "formatVersion": 2,
            "availability":  1,   # 在庫あり only
        }, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        return [
            item for item in items
            if not _is_used_item(item.get("itemName", ""))
            and not _is_single_card(item.get("itemName", ""))
        ]
    except Exception as e:
        print(f"[RareMonitor] Search error for '{keyword}': {e}")
        return []


def _build_affiliate_url(item_url: str) -> str:
    if RAKUTEN_AFFILIATE_ID and item_url:
        return (
            f"https://hb.afl.rakuten.co.jp/ichiba/{RAKUTEN_AFFILIATE_ID}/"
            f"?pc={quote(item_url, safe='')}"
        )
    return item_url


def _get_named_account(name: str) -> dict | None:
    """Load a named Threads account from accounts.json, filling tokens from env."""
    accounts_path = os.path.join(os.path.dirname(__file__), '..', 'accounts.json')
    if not os.path.exists(accounts_path):
        return None
    with open(accounts_path, 'r', encoding='utf-8') as f:
        accounts = json.load(f)
    for acc in accounts:
        if acc.get("name") == name:
            if not acc.get("threads_token"):
                acc["threads_token"] = os.getenv("THREADS_ACCESS_TOKEN", "")
            if not acc.get("threads_user_id"):
                acc["threads_user_id"] = os.getenv("THREADS_USER_ID", "me")
            return acc
    return None


def _get_whisky_account() -> dict | None:
    return _get_named_account("osake")


# ---------------------------------------------------------------------------
# Post dict builders
# ---------------------------------------------------------------------------

def _build_post_dict(item: dict, keyword: str, event: str,
                     price_drop: bool = False, drop_pct: float = 0.0,
                     low_stock: bool = False) -> dict:
    name      = item.get("itemName", "")
    price     = item.get("itemPrice") or 0
    item_url  = item.get("itemUrl", "")
    item_code = item.get("itemCode", "")
    images    = item.get("mediumImageUrls", [])
    image_url = images[0].replace("_ex=128x128", "_ex=600x600") if images else ""
    aff_url   = _build_affiliate_url(item_url)
    price_str = f"¥{price:,}"

    # ⑥ シンプルフォーマット（煽り禁止）
    if event == "restock":
        header  = "【再入荷】"
        comment = "在庫復活してる"
        hashtag = f"#楽天市場 #再入荷 #限定品 #プレミアム #{keyword.replace(' ', '')}"
    elif _is_preorder(name):
        header  = "【予約受付中】"
        comment = "予約できる"
        hashtag = f"#楽天市場 #予約 #限定品 #入手困難 #{keyword.replace(' ', '')}"
    else:
        header  = "【入荷】"
        comment = "今なら買える"
        hashtag = f"#楽天市場 #入荷速報 #限定品 #レア #入手困難 #{keyword.replace(' ', '')}"

    extras: list[str] = []
    if price_drop:
        extras.append(f"📉 {drop_pct:.1f}%OFF")
    if low_stock:
        extras.append("残りわずか")
    extras_line = "  ".join(extras)

    post_text = (
        f"{header}\n\n"
        f"{name[:60]}\n"
        f"今 {price_str}"
        + (f"  {extras_line}" if extras_line else "")
        + f"\n\n{comment}\n\n"
        f"👇 詳細・購入はこちら\n"
        f"{aff_url}\n\n"
        f"{hashtag}"
    )

    return {
        "title":         name,
        "price":         price_str,
        "price_str":     price_str,
        "genre":         "rare",
        "url":           item_url,
        "affiliate_url": aff_url,
        "image_url":     image_url,
        "post_text":     post_text,
        "ab_group":      "A",
        "item_code":     item_code,
        "is_preorder":   _is_preorder(name),
    }


def _build_soldout_post_dict(record: dict, keyword: str) -> dict:
    name  = record.get("name", "")
    price = record.get("price", 0)
    post_text = (
        f"⚠️ 売り切れ・予約終了\n\n"
        f"「{name[:60]}」\n\n"
        f"現在在庫がありません。再入荷をお待ちください。\n\n"
        f"#楽天市場 #売り切れ #限定品 #{keyword.replace(' ', '')}"
    )
    return {
        "title":         name,
        "price":         f"¥{price:,}",
        "price_str":     f"¥{price:,}",
        "genre":         "rare",
        "url":           "",
        "affiliate_url": "",
        "image_url":     "",
        "post_text":     post_text,
        "ab_group":      "A",
        "is_preorder":   False,
    }


# ---------------------------------------------------------------------------
# ② 入荷速報品質フィルタ
# ---------------------------------------------------------------------------

def _keyword_has_soldout_history(seen: dict, keyword: str) -> bool:
    """過去にこのキーワードで売り切れ実績があるか確認する。"""
    return any(
        rec.get("keyword") == keyword and rec.get("sold_out_posted")
        for rec in seen.values()
    )


def _is_quality_alert(item: dict, record: dict | None, keyword: str, typical_price: int,
                      seen: dict | None = None) -> bool:
    """NEW/RESTOCK 投稿の品質チェック。

    条件A（価格異常でない）:
      - 履歴がある場合: current_price <= avg_history × 1.2

    条件B（すべて満たす）:
      ① レビュー件数 >= 50（実績ある商品のみ）
      ② 在庫数が取得できる場合は残り10件以下
      ③ 以下のどれか1つ:
         - 割引率 >= 10%（typical_price 比）
         - 人気銘柄（WHISKY_KEYWORDS）
         - 価格 >= 10,000 円（高額帯）
         - 過去に同キーワードで売り切れ実績あり
    """
    price        = item.get("itemPrice") or 0
    review_count = item.get("reviewCount") or 0

    # 条件B-①: レビュー件数チェック（少なすぎる商品は対象外）
    if review_count < 50:
        return False

    # 条件A: 価格異常チェック（履歴が3件以上ある場合のみ）
    if record and record.get("price_history"):
        history = record["price_history"]
        if len(history) >= 3:
            recent = history[-10:]
            avg_price = sum(p["price"] for p in recent) / len(recent)
            if avg_price > 0 and price > avg_price * 1.2:
                return False  # 転売価格

    # 条件B-②: 在庫数チェック（inventoryStatus が取得できる場合）
    inventory = item.get("inventoryStatus")
    if inventory is not None and isinstance(inventory, int) and inventory > 10:
        return False

    # 条件B-③: 価値判断（どれか1つ）
    # 割引率 >= 10%
    if typical_price > 0 and price <= typical_price * 0.9:
        return True
    # 人気銘柄
    if keyword in WHISKY_KEYWORDS:
        return True
    # 高額帯
    if price >= 10000:
        return True
    # 過去に売り切れ実績あり
    if seen and _keyword_has_soldout_history(seen, keyword):
        return True

    return False


def _item_posted_within_24h(record: dict | None) -> bool:
    """③ 同一商品が24時間以内に投稿済みか判定。"""
    if record is None:
        return False
    last = record.get("last_posted_at")
    if not last:
        return False
    try:
        elapsed_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
        return elapsed_h < _MIN_SAME_ITEM_INTERVAL_H
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

_EVENT_BANNERS = {
    "new":     "🔥 入荷速報！",
    "restock": "🔄 再入荷！",
    "soldout": "⚠️ 売り切れ",
}


def _do_post(post: dict, keyword: str, event: str, shop_count: int = 1) -> None:
    from threads_poster   import post_to_threads
    from instagram_poster import post_to_instagram
    from bluesky_poster   import post_to_bluesky

    # 価格フィルター（product_selector.py の PRICE_FILTER_MAX と統一）
    _price_filter_max = int(os.getenv("PRICE_FILTER_MAX", "5000"))
    raw_price = post.get("price", "")
    if isinstance(raw_price, str):
        _price_num = int(raw_price.replace("¥", "").replace(",", "").strip() or "0")
    else:
        _price_num = int(raw_price or 0)
    if _price_num > _price_filter_max:
        print(f"[RareMonitor] 価格フィルターでスキップ: ¥{_price_num:,} > ¥{_price_filter_max:,}")
        return

    banner = _EVENT_BANNERS.get(event, "🔥")
    if event == "new" and post.get("is_preorder"):
        banner = "📦 予約受付中！"

    title = post["title"][:60]
    print(f"[RareMonitor] {banner} [{keyword}] {title}"
          + (f" ({shop_count}ショップ中最安値)" if shop_count > 1 else ""))

    # Discord notification
    discord_msg = (
        f"{banner} [{keyword}]\n"
        f"**{title}**\n"
        f"価格: {post['price_str']}"
    )
    if shop_count > 1:
        discord_msg += f"\n📊 {shop_count}ショップ中最安値を選択"
    if post.get("affiliate_url"):
        discord_msg += f"\n{post['affiliate_url']}"
    send_discord(discord_msg)

    # ウイスキー系はアルコールポリシー違反リスクのため Threads 投稿をスキップ
    if keyword in WHISKY_KEYWORDS:
        send_discord(f"⚠️ ウイスキー系商品はポリシー上投稿スキップ: {title}")
        print(f"[RareMonitor] WHISKY_KEYWORDS → Threads 投稿スキップ: {title}")
        return

    # Threads routing — 全キーワードを mono_zukan_jp に統一
    # (osake/hobbyアカウントはBAN済みのため廃止)
    r = post_to_threads(post)
    if r["status"] == "success":
        print(f"[RareMonitor] Threads(mono_zukan_jp) OK: {r['post_id']}")
    else:
        print(f"[RareMonitor] Threads(mono_zukan_jp) FAIL: {r.get('error')}")

    # Instagram / Bluesky — HOBBYはThreadsのみ、それ以外は全プラットフォーム
    if keyword in HOBBY_KEYWORDS:
        print(f"[RareMonitor] Instagram/Bluesky スキップ（hobbyジャンル）")
    else:
        if event != "soldout" and post.get("image_url"):
            ig = post_to_instagram(post)
            if ig["status"] == "success":
                print(f"[RareMonitor] Instagram OK: {ig['post_id']}")
            elif ig["status"] == "error":
                print(f"[RareMonitor] Instagram FAIL: {ig.get('error')}")

        bsky = post_to_bluesky(post)
        if bsky["status"] == "success":
            print(f"[RareMonitor] Bluesky OK: {bsky['post_id']}")
        else:
            print(f"[RareMonitor] Bluesky FAIL: {bsky.get('error')}")


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def _post_weekly_summary(seen: dict) -> None:
    from threads_poster import post_to_threads

    week_ago_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    items_this_week = [
        rec for rec in seen.values()
        if rec.get("first_seen", "") >= week_ago_iso and rec.get("posted")
    ]
    if not items_this_week:
        print("[RareMonitor] Weekly summary: no new items this week.")
        return

    lines = [f"📦 今週の入荷速報まとめ（{len(items_this_week)}件）\n"]
    for rec in items_this_week[:10]:
        lines.append(f"・{rec['name'][:40]}（¥{rec['price']:,}）")
    if len(items_this_week) > 10:
        lines.append(f"… 他{len(items_this_week) - 10}件")
    summary_text = "\n".join(lines)

    post = {
        "title":         "今週の入荷速報まとめ",
        "price_str":     "",
        "genre":         "rare",
        "url":           "",
        "affiliate_url": "",
        "image_url":     "",
        "post_text":     summary_text,
        "ab_group":      "A",
    }
    result = post_to_threads(post)
    if result["status"] == "success":
        print(f"[RareMonitor] Weekly summary posted: {result['post_id']}")
    send_discord(f"📊 週次入荷速報サマリーを投稿しました（{len(items_this_week)}件）")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_rare_item_monitor() -> int:
    """
    Run full lifecycle check for all rare keywords.
    Scheduled every 2 hours via Windows Task Scheduler (RareItemMonitor).
    Returns the total number of events posted.
    """
    if not RAKUTEN_APP_ID:
        print("[RareMonitor] RAKUTEN_APP_ID not set — skipping.")
        return 0

    # Skip if called too soon after the last run (guards against accidental double-runs)
    if _too_soon_since_last_run():
        last = _load_last_run()
        elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
        print(f"[RareMonitor] Too soon since last run ({elapsed:.0f} min ago, "
              f"minimum {_MIN_RUN_INTERVAL_HOURS * 60:.0f} min). Skipping.")
        return 0

    now_jst = _now_jst()
    quiet   = _is_quiet_hours()
    overlap = _is_main_post_hour()

    if quiet:
        print(f"[RareMonitor] Quiet hours (JST {now_jst.hour:02d}:xx) — checking only, no posts.")
    elif overlap:
        print(f"[RareMonitor] Main bot post hour (JST {now_jst.hour:02d}:xx) — checking only, no posts.")

    # Suppress posting during quiet hours OR main-bot overlap hours
    no_post = quiet or overlap

    seen = _load_seen()

    # Weekly summary (Sunday 21:00 JST, outside suppression window)
    if _is_weekly_summary_time() and not no_post:
        _post_weekly_summary(seen)

    total_events   = 0
    run_post_count = 0      # 1実行内の投稿件数カウンター
    now_iso        = _now_iso()
    # ③ 実行内ジャンル別最終投稿時刻（30min間隔制御）
    _last_category_post: dict[str, datetime] = {}

    for entry in RARE_KEYWORDS:
        keyword       = entry["keyword"]
        typical_price = entry["typical_price"]
        ceiling       = typical_price * 1.5
        min_price     = entry.get("min_price", 1000)
        category      = "whisky" if keyword in WHISKY_KEYWORDS else (
                         "hobby" if keyword in HOBBY_KEYWORDS else "other")

        # Daily post limit check
        daily_count = _keyword_daily_count(seen, keyword)
        if daily_count >= _MAX_DAILY_POSTS_PER_KEYWORD:
            print(f"[RareMonitor] Daily limit reached for '{keyword}' ({daily_count}/{_MAX_DAILY_POSTS_PER_KEYWORD}) — skipping.")
            continue

        print(f"[RareMonitor] Checking: {keyword} (ceiling ¥{ceiling:,.0f})")
        raw_items = _search_rakuten(keyword)

        # Filter before deduplication so we only compare valid candidates
        filtered: list[dict] = []
        for item in raw_items:
            if not item.get("itemCode"):
                continue
            if (item.get("reviewCount") or 0) >= 50:
                continue
            price = item.get("itemPrice") or 0
            if price < min_price or price > ceiling:
                continue
            if _delivery_too_far(item.get("itemName", "")):
                print(f"[RareMonitor] Skip (delivery >60d): {item.get('itemName', '')[:50]}")
                continue
            filtered.append(item)

        items     = _deduplicate_by_name(filtered)
        found_now: set[str] = set()

        for item in items:
            item_code  = item.get("itemCode", "")
            price      = item.get("itemPrice") or 0
            name       = item.get("itemName", "")
            low_stock  = item.get("limitedFlag") == 1
            shop_count = item.get("shop_count", 1)

            found_now.add(item_code)
            record = seen.get(item_code)

            def _can_post_now(rec: dict | None) -> tuple[bool, str]:
                """③ 24h同一商品・30minジャンル間隔の複合チェック。"""
                if _item_posted_within_24h(rec):
                    return False, "24h以内に投稿済み"
                last_cat = _last_category_post.get(category)
                if last_cat:
                    elapsed_min = (datetime.now(timezone.utc) - last_cat).total_seconds() / 60
                    if elapsed_min < _MIN_GENRE_INTERVAL_MIN:
                        return False, f"ジャンル間隔 {elapsed_min:.0f}min < {_MIN_GENRE_INTERVAL_MIN}min"
                return True, ""

            def _mark_posted(rec: dict) -> None:
                rec["posted"]         = True
                rec["last_posted_at"] = now_iso
                _increment_daily_count(rec)
                _last_category_post[category] = datetime.now(timezone.utc)

            # ---- NEW item ----
            if record is None:
                record = _new_record(item, keyword, category)
                seen[item_code] = record
                if not no_post:
                    # ② 品質フィルタ
                    if not _is_quality_alert(item, record, keyword, typical_price, seen=seen):
                        pass  # 基準未達: 記録のみ
                    else:
                        ok, reason = _can_post_now(record)
                        if not ok:
                            print(f"[RareMonitor] Skip({reason}): {name[:40]}")
                        else:
                            if run_post_count >= _MAX_POSTS_PER_RUN:
                                print(f"[RareMonitor] 実行上限({_MAX_POSTS_PER_RUN}件)到達 — 以降スキップ")
                                break
                            post = _build_post_dict(item, keyword, "new", low_stock=low_stock)
                            _do_post(post, keyword, "new", shop_count=shop_count)
                            _mark_posted(record)
                            total_events   += 1
                            run_post_count += 1
                            if run_post_count < _MAX_POSTS_PER_RUN:
                                time.sleep(_POST_INTERVAL_SEC)

            # ---- RESTOCK (was sold out, now back) ----
            elif record.get("sold_out"):
                record["sold_out"]        = False
                record["sold_out_posted"] = False
                record["restock_count"]   = record.get("restock_count", 0) + 1
                record["last_seen"]       = now_iso
                record["last_checked"]    = now_iso
                _update_price_history(record, price)
                record["price"] = price
                if not no_post:
                    # ② 品質フィルタ
                    if not _is_quality_alert(item, record, keyword, typical_price, seen=seen):
                        pass
                    else:
                        ok, reason = _can_post_now(record)
                        if not ok:
                            print(f"[RareMonitor] Skip({reason}): {name[:40]}")
                        else:
                            if run_post_count >= _MAX_POSTS_PER_RUN:
                                print(f"[RareMonitor] 実行上限({_MAX_POSTS_PER_RUN}件)到達 — 以降スキップ")
                                break
                            post = _build_post_dict(item, keyword, "restock", low_stock=low_stock)
                            _do_post(post, keyword, "restock", shop_count=shop_count)
                            _mark_posted(record)
                            total_events   += 1
                            run_post_count += 1
                            if run_post_count < _MAX_POSTS_PER_RUN:
                                time.sleep(_POST_INTERVAL_SEC)

            # ---- KNOWN item — check price drop ----
            else:
                price_dropped, drop_pct = _check_price_drop(record, price)
                record["last_seen"]    = now_iso
                record["last_checked"] = now_iso
                _update_price_history(record, price)
                record["price"] = price
                if price_dropped and not quiet:
                    daily_count = _keyword_daily_count(seen, keyword)
                    if daily_count < _MAX_DAILY_POSTS_PER_KEYWORD:
                        ok, reason = _can_post_now(record)
                        if ok:
                            if run_post_count >= _MAX_POSTS_PER_RUN:
                                print(f"[RareMonitor] 実行上限({_MAX_POSTS_PER_RUN}件)到達 — 以降スキップ")
                                break
                            post = _build_post_dict(item, keyword, "new",
                                                    price_drop=True, drop_pct=drop_pct,
                                                    low_stock=low_stock)
                            _do_post(post, keyword, "new", shop_count=shop_count)
                            _mark_posted(record)
                            total_events   += 1
                            run_post_count += 1
                            if run_post_count < _MAX_POSTS_PER_RUN:
                                time.sleep(_POST_INTERVAL_SEC)

        # ---- SOLD OUT detection ----
        # Items tracked under this keyword that didn't appear in results this run
        for item_code, record in seen.items():
            if record.get("keyword") != keyword:
                continue
            if item_code in found_now:
                continue
            if not record.get("posted"):
                continue
            if record.get("sold_out") or record.get("sold_out_posted"):
                continue
            # Mark and post sold-out once
            record["sold_out"]        = True
            record["sold_out_posted"] = True
            record["last_checked"]    = now_iso
            if not no_post:
                if run_post_count >= _MAX_POSTS_PER_RUN:
                    print(f"[RareMonitor] 実行上限({_MAX_POSTS_PER_RUN}件)到達 — soldout スキップ")
                    continue
                so_post = _build_soldout_post_dict(record, keyword)
                _do_post(so_post, keyword, "soldout")
                total_events   += 1
                run_post_count += 1
                if run_post_count < _MAX_POSTS_PER_RUN:
                    time.sleep(_POST_INTERVAL_SEC)

        time.sleep(1)  # rate limit between keyword searches

    _save_seen(seen)
    _save_last_run()
    print(f"[RareMonitor] Done. {total_events} event(s) posted.")
    return total_events


if __name__ == "__main__":
    run_rare_item_monitor()
