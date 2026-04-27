"""
ban_risk_monitor.py — BANリスク自動評価・緊急停止エージェント

リスクレベル:
  OK       : 正常
  WARNING  : 警告（投稿続行、Discord通知のみ）
  CRITICAL : 即時停止（emergency_stop() を実行）

停止フラグ: data/ban_risk_status.json
  {"stopped": true, "reason": "...", "stopped_at": "ISO"}
  → stopped_at から6時間経過で自動解除
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

from database import get_all_post_logs, get_all_follower_history

_STATUS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'ban_risk_status.json')

_AUTO_RESUME_HOURS = 6     # 停止から何時間後に自動解除するか


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _load_json(path: str):
    """follower_history / ban_risk_status など JSON のまま残すファイル用。"""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _load_status() -> dict:
    if os.path.exists(_STATUS_PATH):
        try:
            with open(_STATUS_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"stopped": False, "reason": "", "stopped_at": None}


def _save_status(status: dict) -> None:
    with open(_STATUS_PATH, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 自動停止 / 解除
# ---------------------------------------------------------------------------

def emergency_stop(reason: str) -> None:
    """CRITICAL 判定時に停止フラグを書き込み Discord 緊急通知を送る。"""
    from discord_notifier import send_discord_error
    now_iso = datetime.now(timezone.utc).isoformat()
    _save_status({"stopped": True, "reason": reason, "stopped_at": now_iso})
    msg = (
        f"🚨 BANリスク検知！投稿を自動停止しました\n"
        f"**停止理由:** {reason}\n"
        f"**停止時刻:** {now_iso[:19]} UTC\n"
        f"**自動再開:** {_AUTO_RESUME_HOURS}時間後\n"
        f"**手動再開:** `data/ban_risk_status.json` の `stopped` を `false` に変更"
    )
    send_discord_error(msg)
    print(f"[BanRisk] 🚨 CRITICAL: emergency_stop 実行 — {reason}")


def check_resume() -> bool:
    """
    停止フラグを確認する。
    - stopped=True かつ 6時間未経過 → True（投稿をスキップすべき）
    - stopped=True かつ 6時間経過   → 自動解除して False を返す
    - stopped=False                 → False
    """
    status = _load_status()
    if not status.get("stopped"):
        return False

    stopped_at_str = status.get("stopped_at")
    if stopped_at_str:
        try:
            stopped_at = datetime.fromisoformat(stopped_at_str)
            elapsed_h  = (datetime.now(timezone.utc) - stopped_at).total_seconds() / 3600
            if elapsed_h >= _AUTO_RESUME_HOURS:
                _save_status({"stopped": False, "reason": "", "stopped_at": None})
                print(f"[BanRisk] 停止から{elapsed_h:.1f}時間経過 → 自動解除")
                return False
        except (ValueError, TypeError):
            pass

    print(f"[BanRisk] 停止中 — reason: {status.get('reason', '')}")
    return True


# ---------------------------------------------------------------------------
# リスク評価
# ---------------------------------------------------------------------------

def check_ban_risk() -> str:
    """
    BANリスクを評価して 'OK' / 'WARNING' / 'CRITICAL' を返す。
    CRITICAL の場合は emergency_stop() も呼び出す。
    """
    from discord_notifier import send_discord
    now   = datetime.now(timezone.utc)
    logs  = get_all_post_logs()
    reasons_critical: list[str] = []
    reasons_warning:  list[str] = []

    if not logs:
        return "OK"

    # ── 直近100件の成功率チェック ──────────────────────────────────────────
    recent100 = [p for p in logs if p.get("status") in ("success", "error")][-100:]
    if len(recent100) >= 10:
        ok100   = sum(1 for p in recent100 if p.get("status") == "success")
        rate100 = ok100 / len(recent100)
        if rate100 <= 0.20:
            reasons_warning.append(
                f"直近{len(recent100)}件の成功率が低い ({ok100}/{len(recent100)} = {rate100*100:.0f}%)"
            )

    # ── 直近1時間のログを抽出 ──────────────────────────────────────────────
    cutoff_1h  = now - timedelta(hours=1)
    cutoff_30m = now - timedelta(minutes=30)
    cutoff_24h = now - timedelta(hours=24)

    def _parse_dt(entry: dict) -> datetime | None:
        s = entry.get("posted_at") or ""
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    logs_1h  = [p for p in logs if (_parse_dt(p) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff_1h]
    logs_30m = [p for p in logs if (_parse_dt(p) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff_30m]
    logs_24h = [p for p in logs if (_parse_dt(p) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff_24h]

    # ── CRITICAL: 直近30分に10件以上 ──────────────────────────────────────
    if len(logs_30m) >= 10:
        reasons_critical.append(f"直近30分の投稿数が多すぎる ({len(logs_30m)}件 ≥ 10件)")

    # ── CRITICAL: 直近1時間のエラー率50%以上 ─────────────────────────────
    if logs_1h:
        err_1h  = sum(1 for p in logs_1h if p.get("status") == "error")
        rate_1h = err_1h / len(logs_1h)
        if rate_1h >= 0.5:
            reasons_critical.append(
                f"直近1時間のエラー率 {rate_1h*100:.0f}% ({err_1h}/{len(logs_1h)}件)"
            )

    # ── CRITICAL: error_subcode=2207051 が直近3件以上連続 ────────────────
    import re
    consecutive = 0
    for p in reversed(logs):
        err = str(p.get("error") or "")
        if "2207051" in err:
            consecutive += 1
        else:
            break
    if consecutive >= 3:
        reasons_critical.append(f"error_subcode 2207051 が {consecutive}件連続（Threads日次上限超過）")

    # ── WARNING: 直近24時間のエラー率30%以上 ──────────────────────────────
    if logs_24h:
        err_24h  = sum(1 for p in logs_24h if p.get("status") == "error")
        rate_24h = err_24h / len(logs_24h)
        if rate_24h >= 0.3:
            reasons_warning.append(
                f"直近24時間のエラー率 {rate_24h*100:.0f}% ({err_24h}/{len(logs_24h)}件)"
            )

    # ── WARNING: 直近1時間に5件以上 ───────────────────────────────────────
    if len(logs_1h) >= 5:
        reasons_warning.append(f"直近1時間の投稿数 {len(logs_1h)}件 ≥ 5件")

    # ── WARNING: フォロワーが前回比10%以上減少 ────────────────────────────
    follower_hist = get_all_follower_history()
    if len(follower_hist) >= 2:
        try:
            prev_count = follower_hist[-2].get("threads") or 0
            curr_count = follower_hist[-1].get("threads") or 0
            if prev_count > 0 and curr_count < prev_count * 0.9:
                drop_pct = (prev_count - curr_count) / prev_count * 100
                reasons_warning.append(
                    f"フォロワーが前回比 {drop_pct:.1f}% 減少 ({prev_count} → {curr_count})"
                )
        except (TypeError, KeyError):
            pass

    # ── 判定 ──────────────────────────────────────────────────────────────
    if reasons_critical:
        reason_str = " / ".join(reasons_critical)
        emergency_stop(reason_str)
        return "CRITICAL"

    if reasons_warning:
        reason_str = " / ".join(reasons_warning)
        send_discord(f"⚠️ [BanRisk] WARNING: {reason_str}")
        print(f"[BanRisk] WARNING: {reason_str}")
        return "WARNING"

    print("[BanRisk] OK — リスクなし")
    return "OK"
