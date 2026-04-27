"""
auto_recovery_agent.py — 異常検知・自動対応エージェント

検知する異常:
  異常①: 連続エラー    — 直近10件で7件以上エラー → 投稿間隔2倍
  異常②: フォロワー急減 — 前回比5%以上減少       → 24時間アフィリ停止
  異常③: エンゲージ急落 — 直近7日avg < 30日avg×0.5 → 共感系トーンに強制
  異常④: API障害       — 直近1時間で同一エラー3回以上 → 6時間プラットフォーム停止

設定ファイル: data/recovery_config.json
  {
    "post_interval_multiplier": 1.0,
    "affiliate_suspended": false,
    "suspended_until": null,
    "suspended_platforms": [],
    "resume_at": null
  }
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

from database import get_db

_DATA_DIR        = os.path.join(os.path.dirname(__file__), '..', 'data')
_RECOVERY_CONFIG = os.path.join(_DATA_DIR, 'recovery_config.json')
_STRATEGY_PATH   = os.path.join(_DATA_DIR, 'daily_strategy.json')

_DEFAULT_CONFIG: dict = {
    "post_interval_multiplier": 1.0,
    "affiliate_suspended":      False,
    "suspended_until":          None,
    "suspended_platforms":      [],
    "resume_at":                None,
}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _load_json(path: str, default=None):
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def load_recovery_config() -> dict:
    """recovery_config.json を読み込む。存在しない場合はデフォルトを返す。"""
    cfg = _load_json(_RECOVERY_CONFIG, default={})
    result = _DEFAULT_CONFIG.copy()
    result.update(cfg)
    return result


def _save_recovery_config(config: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_RECOVERY_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_plus(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _discord_notify(message: str) -> None:
    try:
        from discord_notifier import send_discord
        send_discord(message)
    except Exception as e:
        print(f"[AutoRecovery] Discord通知エラー: {e}")


# ---------------------------------------------------------------------------
# 自動回復チェック
# ---------------------------------------------------------------------------

def check_recovery() -> dict:
    """
    recovery_config.json を確認して停止期間が終了していれば自動解除する。
    - suspended_until を過ぎていれば affiliate_suspended を解除
    - resume_at を過ぎていれば suspended_platforms を解除
    更新後の config を返す。
    """
    config = load_recovery_config()
    now    = datetime.now(timezone.utc)
    changed = False

    # アフィリ停止の自動解除
    suspended_until = config.get("suspended_until")
    if config.get("affiliate_suspended") and suspended_until:
        try:
            until_dt = datetime.fromisoformat(suspended_until)
            if now >= until_dt:
                config["affiliate_suspended"] = False
                config["suspended_until"]     = None
                changed = True
                print("[AutoRecovery] ✅ アフィリ投稿停止を自動解除しました")
                _discord_notify("✅ [AutoRecovery] アフィリ投稿停止期間が終了 → 自動解除しました")
        except (ValueError, TypeError):
            pass

    # プラットフォーム停止の自動解除
    resume_at = config.get("resume_at")
    if config.get("suspended_platforms") and resume_at:
        try:
            resume_dt = datetime.fromisoformat(resume_at)
            if now >= resume_dt:
                platforms = config.get("suspended_platforms", [])
                config["suspended_platforms"] = []
                config["resume_at"]           = None
                changed = True
                print(f"[AutoRecovery] ✅ プラットフォーム停止を自動解除: {platforms}")
                _discord_notify(
                    f"✅ [AutoRecovery] {', '.join(platforms)} の停止期間が終了 → 自動解除しました"
                )
        except (ValueError, TypeError):
            pass

    if changed:
        _save_recovery_config(config)

    return config


# ---------------------------------------------------------------------------
# 異常検知
# ---------------------------------------------------------------------------

def _check_consecutive_errors() -> bool:
    """異常①: 直近10件で7件以上エラー → True"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status FROM post_logs"
            " WHERE status IN ('success', 'error')"
            " ORDER BY posted_at DESC LIMIT 10"
        ).fetchall()
    if len(rows) < 5:
        return False
    error_count = sum(1 for r in rows if r["status"] == "error")
    return error_count >= 7


def _check_follower_drop() -> tuple[bool, float]:
    """
    異常②: 前回比5%以上フォロワー減少 → (True, drop_pct)
    threads が NULL の場合は instagram、それも NULL なら bluesky にフォールバック。
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT threads, instagram, bluesky FROM follower_history"
            " ORDER BY timestamp DESC LIMIT 2"
        ).fetchall()
    if len(rows) < 2:
        return False, 0.0

    def _pick(row) -> int | None:
        for col in ("threads", "instagram", "bluesky"):
            v = row[col]
            if v is not None:
                return int(v)
        return None

    curr = _pick(rows[0])
    prev = _pick(rows[1])

    if curr is None or prev is None or prev == 0:
        return False, 0.0

    drop_pct = (prev - curr) / prev * 100
    return drop_pct >= 5.0, drop_pct


def _check_engagement_drop() -> tuple[bool, float, float]:
    """
    異常③: 直近7日avg < 過去30日avg × 0.5 → (True, avg_7d, avg_30d)
    """
    with get_db() as conn:
        row7 = conn.execute(
            "SELECT AVG(engagement_score) AS avg, COUNT(*) AS cnt FROM learning_data"
            " WHERE posted_at >= datetime('now', '-7 days')"
        ).fetchone()
        row30 = conn.execute(
            "SELECT AVG(engagement_score) AS avg, COUNT(*) AS cnt FROM learning_data"
            " WHERE posted_at >= datetime('now', '-30 days')"
        ).fetchone()

    cnt_7d  = row7["cnt"]  if row7  else 0
    cnt_30d = row30["cnt"] if row30 else 0

    # 誤検知防止①: 7日以内のレコードが10件未満
    if cnt_7d < 10:
        return False, 0.0, 0.0

    # 誤検知防止②: 30日以内のレコードが5件未満
    if cnt_30d < 5:
        return False, 0.0, 0.0

    avg_7d  = row7["avg"]  or 0.0
    avg_30d = row30["avg"] or 0.0

    # 誤検知防止③: 30日平均が実質ゼロ（データ不足 or スコア未蓄積）
    if avg_30d <= 0.05:
        return False, avg_7d, avg_30d

    # 誤検知防止④: 7日平均と30日平均の差が誤差レベル（0.02以下）
    if abs(avg_30d - avg_7d) < 0.02:
        return False, avg_7d, avg_30d

    # 誤検知防止⑤: 7日平均が実質ゼロ（スコア未蓄積）
    if avg_7d <= 0.05:
        return False, avg_7d, avg_30d

    return avg_7d < avg_30d * 0.5, avg_7d, avg_30d


def _check_api_errors() -> list[str]:
    """
    異常④: 直近1時間で同一プラットフォームのエラーが3回以上 → 対象プラットフォームリスト
    エラーメッセージから platform を推定する。
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT posted_at, error FROM post_logs"
            " WHERE status = 'error'"
            " AND posted_at >= datetime('now', '-1 hour')"
        ).fetchall()

    if not rows:
        return []

    recent_errors: list[str] = [str(r["error"] or "") for r in rows]

    # プラットフォーム推定（エラー文字列のパターンから）
    platform_patterns: list[tuple[str, list[str]]] = [
        ("threads",   ["threads.net", "2207051", "OAuthException"]),
        ("instagram", ["graph.facebook.com", "instagram", "Instagram"]),
        ("bluesky",   ["bsky.app", "bluesky", "Bluesky", "atproto"]),
    ]

    platform_hits: Counter = Counter()
    for err in recent_errors:
        for platform, patterns in platform_patterns:
            if any(pat in err for pat in patterns):
                platform_hits[platform] += 1

    return [p for p, count in platform_hits.items() if count >= 3]


# ---------------------------------------------------------------------------
# 自動対応アクション
# ---------------------------------------------------------------------------

def _handle_consecutive_errors(config: dict) -> dict:
    """異常①: 投稿間隔を2倍に設定"""
    config["post_interval_multiplier"] = 2.0
    msg = "⚠️ [AutoRecovery] 連続エラー検知 → 投稿間隔を2倍に延長"
    print(f"[AutoRecovery] {msg}")
    _discord_notify(msg)
    return config


def _handle_follower_drop(config: dict, drop_pct: float) -> dict:
    """異常②: 24時間アフィリ投稿停止"""
    config["affiliate_suspended"] = True
    config["suspended_until"]     = _iso_plus(24)
    msg = (
        f"🚨 [AutoRecovery] フォロワー急減検知（-{drop_pct:.1f}%）"
        f" → 24時間アフィリ投稿停止\n"
        f"再開予定: {config['suspended_until'][:19]} UTC"
    )
    print(f"[AutoRecovery] フォロワー急減: -{drop_pct:.1f}% → 24h停止")
    _discord_notify(msg)
    return config


def _handle_engagement_drop(avg_7d: float, avg_30d: float) -> None:
    """異常③: daily_strategy.jsonのpost_toneを「共感系」に上書き"""
    strategy_data: dict = {}
    if os.path.exists(_STRATEGY_PATH):
        try:
            with open(_STRATEGY_PATH, 'r', encoding='utf-8') as f:
                strategy_data = json.load(f)
        except Exception:
            pass

    strategy = strategy_data.get("strategy", {})
    strategy["post_tone"] = "共感系"
    strategy_data["strategy"] = strategy

    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_STRATEGY_PATH, 'w', encoding='utf-8') as f:
        json.dump(strategy_data, f, ensure_ascii=False, indent=2)

    msg = (
        f"📉 [AutoRecovery] エンゲージメント急落検知\n"
        f"直近7日avg: {avg_7d:.1f} / 30日avg: {avg_30d:.1f}"
        f"（{avg_7d/avg_30d*100:.0f}%）→ 共感系トーンに切り替え"
    )
    print(f"[AutoRecovery] エンゲージ急落 ({avg_7d:.1f} vs {avg_30d:.1f}) → 共感系")
    _discord_notify(msg)


def _handle_api_errors(config: dict, platforms: list[str]) -> dict:
    """異常④: 対象プラットフォームを6時間停止"""
    existing = config.get("suspended_platforms") or []
    merged   = list(set(existing + platforms))
    config["suspended_platforms"] = merged
    config["resume_at"]           = _iso_plus(6)

    msg = (
        f"🔴 [AutoRecovery] API障害検知: {', '.join(platforms)}"
        f" → 6時間投稿停止\n"
        f"再開予定: {config['resume_at'][:19]} UTC"
    )
    print(f"[AutoRecovery] API障害: {platforms} → 6h停止")
    _discord_notify(msg)
    return config


# ---------------------------------------------------------------------------
# メイン検知ループ
# ---------------------------------------------------------------------------

def run_auto_recovery() -> dict:
    """
    全異常を検知して自動対応を実行する。
    更新後の recovery_config を返す。
    main.py から毎回呼び出す。
    """
    print("\n[AutoRecovery] 異常検知チェック開始")
    config  = load_recovery_config()
    changed = False

    # 異常①: 連続エラー
    if _check_consecutive_errors():
        if config.get("post_interval_multiplier", 1.0) < 2.0:
            config  = _handle_consecutive_errors(config)
            changed = True
        else:
            print("[AutoRecovery] ① 連続エラー: 既に延長済み → スキップ")
    else:
        # 異常が解消されていれば interval を元に戻す
        if config.get("post_interval_multiplier", 1.0) > 1.0:
            config["post_interval_multiplier"] = 1.0
            changed = True
            print("[AutoRecovery] ① 連続エラー解消 → 投稿間隔を標準に戻す")

    # 異常②: フォロワー急減
    if not config.get("affiliate_suspended"):
        drop, drop_pct = _check_follower_drop()
        if drop:
            config  = _handle_follower_drop(config, drop_pct)
            changed = True

    # 異常③: エンゲージメント急落
    eng_drop, avg_7d, avg_30d = _check_engagement_drop()
    if eng_drop:
        _handle_engagement_drop(avg_7d, avg_30d)
        # daily_strategy は別ファイルなので config の changed フラグは不要

    # 異常④: API障害
    bad_platforms = _check_api_errors()
    if bad_platforms:
        existing = config.get("suspended_platforms") or []
        new_ones  = [p for p in bad_platforms if p not in existing]
        if new_ones:
            config  = _handle_api_errors(config, new_ones)
            changed = True
        else:
            print(f"[AutoRecovery] ④ API障害: {bad_platforms} は既に停止済み → スキップ")

    if changed:
        _save_recovery_config(config)
        print(f"[AutoRecovery] recovery_config 更新: {config}")
    else:
        print("[AutoRecovery] 異常なし — 変更なし")

    return config


if __name__ == "__main__":
    cfg = check_recovery()
    print(f"[checkRecovery] {cfg}")
    cfg = run_auto_recovery()
    print(f"[runAutoRecovery] {cfg}")
