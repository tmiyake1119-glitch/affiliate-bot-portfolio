"""
follower_strategy.py — フォロワー数連動の投稿戦略エージェント

フォロワー数に応じた時間帯別投稿アクション:
  < 200  : {"9": "engagement", "13": "skip", "21": "affiliate"}
  200–499: {"9": "affiliate",  "13": "engagement", "21": "affiliate"}
  500+   : {"9": "affiliate",  "13": "engagement", "21": "affiliate", "18": "affiliate"}

アクション:
  "affiliate"  → 通常アフィリエイト投稿
  "engagement" → エンゲージメント投稿 (post_engagement)
  "skip"       → 投稿スキップ

マイルストーン通知:
  200・500フォロワー到達時に Discord 通知
"""

import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from database import get_all_follower_history
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_MILESTONE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'follower_milestones.json')

# フォロワー数ティア別のデフォルト戦略 (JST時間文字列 → アクション)
_STRATEGIES = {
    "low":    {"9": "engagement", "13": "skip",        "21": "affiliate"},
    "mid":    {"9": "affiliate",  "13": "engagement",  "21": "affiliate"},
    "high":   {"9": "affiliate",  "13": "engagement",  "21": "affiliate", "18": "affiliate"},
}

_MILESTONES = [200, 500]


# ---------------------------------------------------------------------------
# フォロワー数取得
# ---------------------------------------------------------------------------

def _get_latest_follower_count() -> int | None:
    """DB から最新の Threads フォロワー数を返す。"""
    try:
        history = get_all_follower_history()
        if not history:
            return None
        count = history[-1].get("threads")
        return int(count) if count is not None else None
    except Exception as e:
        print(f"[FollowerStrategy] follower_history DB 読み込みエラー: {e}")
        return None


def _get_previous_follower_count() -> int | None:
    """DB から1つ前の Threads フォロワー数を返す。"""
    try:
        history = get_all_follower_history()
        if len(history) < 2:
            return None
        count = history[-2].get("threads")
        return int(count) if count is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# マイルストーン通知
# ---------------------------------------------------------------------------

def _load_milestones() -> set:
    """既に通知済みのマイルストーンを返す。"""
    if not os.path.exists(_MILESTONE_PATH):
        return set()
    try:
        with open(_MILESTONE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get("notified", []))
    except Exception:
        return set()


def _save_milestones(notified: set) -> None:
    os.makedirs(os.path.dirname(_MILESTONE_PATH), exist_ok=True)
    with open(_MILESTONE_PATH, 'w', encoding='utf-8') as f:
        json.dump({"notified": sorted(notified)}, f, ensure_ascii=False, indent=2)


def check_follower_milestones() -> None:
    """
    フォロワー数がマイルストーンを超えた場合に Discord 通知を送る。
    一度通知したマイルストーンは再通知しない。
    """
    current = _get_latest_follower_count()
    if current is None:
        return

    notified = _load_milestones()
    newly_notified = set()

    for milestone in _MILESTONES:
        if milestone not in notified and current >= milestone:
            try:
                from discord_notifier import send_discord
                send_discord(
                    f"🎉 フォロワー {milestone} 人達成！\n"
                    f"現在のフォロワー数: **{current}** 人\n"
                    f"投稿戦略を自動更新しました。"
                )
                print(f"[FollowerStrategy] 🎉 マイルストーン達成通知: {milestone}人")
            except Exception as e:
                print(f"[FollowerStrategy] Discord 通知エラー: {e}")
            newly_notified.add(milestone)

    if newly_notified:
        _save_milestones(notified | newly_notified)


# ---------------------------------------------------------------------------
# 戦略取得
# ---------------------------------------------------------------------------

def get_posting_strategy() -> dict:
    """
    現在のフォロワー数に基づく投稿戦略を返す。
    戻り値: {JST時間文字列: アクション文字列} の dict
      例: {"9": "affiliate", "13": "engagement", "21": "affiliate"}
    データなし・取得失敗時は "low" 戦略をフォールバックとして返す。
    """
    count = _get_latest_follower_count()

    if count is None:
        print("[FollowerStrategy] フォロワーデータなし → low 戦略 (フォールバック)")
        return _STRATEGIES["low"].copy()

    if count >= 500:
        tier = "high"
    elif count >= 200:
        tier = "mid"
    else:
        tier = "low"

    print(f"[FollowerStrategy] フォロワー数: {count} → ティア: {tier}")
    return _STRATEGIES[tier].copy()


def get_action_for_hour(jst_hour: int) -> str:
    """
    指定 JST 時間のアクションを返す。
    戦略に該当する時間がない場合は "affiliate" を返す（デフォルト動作）。
    """
    strategy = get_posting_strategy()
    action = strategy.get(str(jst_hour), "affiliate")
    print(f"[FollowerStrategy] JST {jst_hour}時 → アクション: {action}")
    return action


if __name__ == "__main__":
    count = _get_latest_follower_count()
    print(f"最新フォロワー数: {count}")
    strategy = get_posting_strategy()
    print(f"現在の投稿戦略: {strategy}")
    check_follower_milestones()
