"""フォロー・いいね リマインダー

毎朝 9:00 JST に目標を通知し、毎晩 21:00 JST に実績をまとめて通知する。
実績は update_follow_stats() で手動入力する設計。
"""

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from database import (
    get_all_follower_history,
    get_today_follow_log,
    upsert_follow_log,
    get_follow_log_recent,
)
from discord_notifier import send_discord_report

# JST
_JST = timezone(timedelta(hours=9))

# フォロワー数別 1日上限テーブル
_TIERS: list[tuple[int, int, int]] = [
    # (min_followers, target_follows, target_likes)
    (500, 10, 30),
    (200, 15, 40),
    (  0, 20, 50),
]

# 1時間あたり上限
HOURLY_MAX_FOLLOWS = 5
HOURLY_MAX_LIKES   = 15


def _today_jst() -> str:
    return datetime.now(_JST).strftime("%Y-%m-%d")


def _latest_follower_counts() -> tuple[int | None, int | None]:
    """最新の (instagram, threads) フォロワー数を返す。"""
    history = get_all_follower_history()
    if not history:
        return None, None
    latest = history[-1]
    return latest.get("instagram"), latest.get("threads")


def _get_targets(instagram: int | None) -> tuple[int, int]:
    """Instagramフォロワー数からその日の (target_follows, target_likes) を決定する。"""
    count = instagram or 0
    for min_f, tf, tl in _TIERS:
        if count >= min_f:
            return tf, tl
    return 20, 50  # フォールバック


def _ts_to_jst_date(ts_str: str) -> str:
    """ISO timestamp 文字列を JST 日付文字列 (YYYY-MM-DD) に変換する。"""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_JST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _follower_gain_yesterday() -> dict[str, int | None]:
    """昨日（JST）のフォロワー増加数を返す。

    昨日の最後のレコードと昨日より前の最後のレコードを比較して
    {instagram, threads, bluesky} の増加数（None = データ不足）を返す。
    """
    history = get_all_follower_history()  # timestamp 昇順
    if len(history) < 2:
        return {"instagram": None, "threads": None, "bluesky": None}

    yesterday = (datetime.now(_JST) - timedelta(days=1)).strftime("%Y-%m-%d")

    # 昨日のレコードと昨日より前のレコードに分類
    yesterday_records  = [r for r in history if _ts_to_jst_date(r.get("timestamp", "")) == yesterday]
    before_yesterday   = [r for r in history if _ts_to_jst_date(r.get("timestamp", "")) < yesterday]

    # 昨日の最後のレコード
    last_yesterday = yesterday_records[-1] if yesterday_records else None
    # 昨日より前の最後のレコード（比較基準）
    last_before    = before_yesterday[-1]  if before_yesterday  else None

    # 昨日データがない場合は直近2レコードで近似
    if last_yesterday is None:
        last_yesterday = history[-1]
        last_before    = history[-2] if len(history) >= 2 else None

    if last_before is None:
        return {"instagram": None, "threads": None, "bluesky": None}

    def _gain(key: str) -> int | None:
        a = last_yesterday.get(key)
        b = last_before.get(key)
        if a is not None and b is not None:
            return a - b
        return None

    return {
        "instagram": _gain("instagram"),
        "threads":   _gain("threads"),
        "bluesky":   _gain("bluesky"),
    }


def _fmt_gain(val: int | None) -> str:
    """増加数を表示用文字列に変換する。"""
    if val is None:
        return "不明"
    return f"+{val}" if val >= 0 else str(val)


def send_morning_reminder() -> None:
    """朝のフォロー目標通知を Discord に送る。昨日のフォロワー増加も表示。"""
    instagram, threads = _latest_follower_counts()
    target_follows, target_likes = _get_targets(instagram)

    today = _today_jst()

    # 昨日のフォロワー増加を計算
    gain = _follower_gain_yesterday()
    ig_gain  = gain["instagram"]
    th_gain  = gain["threads"]
    bk_gain  = gain["bluesky"]

    # 今日のレコードを目標値・増加数で初期化・保存
    upsert_follow_log(
        today, follows=0, likes=0,
        target_follows=target_follows, target_likes=target_likes,
        follower_gain_ig=ig_gain      if ig_gain is not None else 0,
        follower_gain_threads=th_gain if th_gain is not None else 0,
        follower_gain_bluesky=bk_gain if bk_gain is not None else 0,
    )

    ig_str = f"{instagram}人" if instagram is not None else "未取得"
    th_str = f"{threads}人"   if threads   is not None else "未取得"

    msg = (
        f"🌅 おはようございます！ずかぽんだぽん🔍\n\n"
        f"📈 昨日のフォロワー増加\n"
        f"📱 Instagram: {_fmt_gain(ig_gain)}人\n"
        f"🧵 Threads: {_fmt_gain(th_gain)}人\n"
        f"🦋 Bluesky: {_fmt_gain(bk_gain)}人\n\n"
        f"📋 今日のフォロー目標\n"
        f"👥 フォロー: {target_follows}件（1時間に{HOURLY_MAX_FOLLOWS}件以内）\n"
        f"❤️ いいね: {target_likes}件（1時間に{HOURLY_MAX_LIKES}件以内）\n\n"
        f"現在のフォロワー数:\n"
        f"📱 Instagram: {ig_str}\n"
        f"🧵 Threads: {th_str}\n\n"
        f"今日も頑張ってだぽん！"
    )
    send_discord_report(msg)
    print(
        f"[FollowReminder] 朝の通知を送信しました "
        f"(target: {target_follows}フォロー / {target_likes}いいね, "
        f"gain: ig={ig_gain} th={th_gain} bk={bk_gain})"
    )


def send_evening_reminder() -> None:
    """夜のフォロー実績通知を Discord に送る。"""
    today = _today_jst()
    log   = get_today_follow_log(today)

    follows        = log.get("follows", 0)         or 0
    likes          = log.get("likes",   0)         or 0
    target_follows = log.get("target_follows", 20) or 20
    target_likes   = log.get("target_likes",   50) or 50

    f_ok    = follows >= target_follows
    l_ok    = likes   >= target_likes
    f_diff  = target_follows - follows
    l_diff  = target_likes   - likes

    f_status = "達成✅" if f_ok else f"不足⚠️ {f_diff}件"
    l_status = "達成✅" if l_ok else f"不足⚠️ {l_diff}件"
    closing  = "今日も頑張ったぽん！明日もよろしく😊" if (f_ok and l_ok) \
               else "明日は少し多めにいいねしてみてだぽん！"

    update_cmd = (
        "python -c \"from agents.follow_reminder import update_follow_stats; "
        f"update_follow_stats(follows={follows}, likes={likes})\""
    )

    msg = (
        f"🌙 お疲れ様だぽん！ずかぽんだよ🔍\n\n"
        f"📊 今日のフォロー実績\n"
        f"👥 フォロー: {follows}/{target_follows}件 {f_status}\n"
        f"❤️ いいね: {likes}/{target_likes}件 {l_status}\n\n"
        f"{closing}\n\n"
        f"今日の実績を更新するには：\n"
        f"```\n{update_cmd}\n```"
    )
    send_discord_report(msg)
    print(f"[FollowReminder] 夜の通知を送信しました ({follows}フォロー / {likes}いいね)")


def update_follow_stats(follows: int, likes: int) -> None:
    """今日のフォロー・いいね実績を手動で記録する。

    使い方:
        python -c "from agents.follow_reminder import update_follow_stats; update_follow_stats(follows=20, likes=50)"
    """
    instagram, _ = _latest_follower_counts()
    target_follows, target_likes = _get_targets(instagram)
    today = _today_jst()
    upsert_follow_log(today, follows=follows, likes=likes,
                      target_follows=target_follows, target_likes=target_likes)
    print(f"[FollowReminder] 実績を更新しました: {today} — フォロー {follows}件 / いいね {likes}件")


if __name__ == "__main__":
    import sys as _sys
    cmd = _sys.argv[1] if len(_sys.argv) > 1 else "morning"
    if cmd == "morning":
        send_morning_reminder()
    elif cmd == "evening":
        send_evening_reminder()
    else:
        print("Usage: python follow_reminder.py [morning|evening]")
