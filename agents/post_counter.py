"""post_counter.py — アカウント別日次投稿数を管理する共通モジュール。

data/daily_post_counts.json に以下の形式で保存:
{
  "2026-04-03": {
    "osake": {"normal": 3, "alert": 2},
    "hobby":  {"normal": 1, "alert": 4},
    "main":   {"normal": 2, "alert": 1}
  }
}

data/posting_log.json に構造化ログを追記:
[
  {"timestamp": "...", "account": "osake", "type": "normal",
   "product_id": "...", "posted": true},
  ...
]
"""

import json
import os
from datetime import datetime, timezone

_COUNTS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'daily_post_counts.json')
_LOG_PATH    = os.path.join(os.path.dirname(__file__), '..', 'data', 'posting_log.json')

# ① 1日あたりの投稿上限（通常投稿 + 入荷速報の合算）
MAX_DAILY_POSTS: dict[str, int] = {
    "mono_zukan_jp": 6,
}

# ① 入荷速報の上限（MAX_DAILY_POSTS の内枠）
MAX_ALERT_POSTS: dict[str, int] = {
    "mono_zukan_jp": 4,
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    os.makedirs(os.path.dirname(_COUNTS_PATH), exist_ok=True)
    if os.path.exists(_COUNTS_PATH):
        with open(_COUNTS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save(data: dict) -> None:
    with open(_COUNTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_today_counts(account: str) -> dict[str, int]:
    """今日の {normal, alert} 投稿数を返す。"""
    today = _today()
    data  = _load()
    return data.get(today, {}).get(account, {"normal": 0, "alert": 0})


def can_post(account: str, post_type: str = "normal") -> bool:
    """投稿可能か判定する。

    post_type = "normal" | "alert"
    """
    counts = get_today_counts(account)
    total  = counts.get("normal", 0) + counts.get("alert", 0)
    daily_max = MAX_DAILY_POSTS.get(account, 6)
    alert_max = MAX_ALERT_POSTS.get(account, 4)

    if total >= daily_max:
        return False
    if post_type == "alert" and counts.get("alert", 0) >= alert_max:
        return False
    return True


def record_post(account: str, post_type: str, product_id: str, posted: bool) -> None:
    """投稿後にカウントを+1し、構造化ログに追記する。"""
    today = _today()
    data  = _load()
    day   = data.setdefault(today, {})
    acc   = day.setdefault(account, {"normal": 0, "alert": 0})

    if posted:
        acc[post_type] = acc.get(post_type, 0) + 1
    _save(data)

    # ⑧ 構造化ログ
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    log: list = []
    if os.path.exists(_LOG_PATH):
        with open(_LOG_PATH, 'r', encoding='utf-8') as f:
            log = json.load(f)
    log.append({
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "account":    account,
        "type":       post_type,
        "product_id": product_id,
        "posted":     posted,
    })
    # 直近1000件のみ保持
    if len(log) > 1000:
        log = log[-1000:]
    with open(_LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
