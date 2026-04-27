import os
import sys
from datetime import date

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

DISCORD_WEBHOOK_URL    = os.getenv("DISCORD_WEBHOOK_URL", "")     # general / 投稿ログ
DISCORD_WEBHOOK_ERROR  = os.getenv("DISCORD_WEBHOOK_ERROR", "")   # エラー通知
DISCORD_WEBHOOK_REPORT = os.getenv("DISCORD_WEBHOOK_REPORT", "")  # 週次レポート


def _post(webhook_url: str, message: str, channel_label: str) -> bool:
    """Send message to a webhook URL. Returns True on success."""
    if not webhook_url:
        print(f"[Discord:{channel_label}] Webhook not set — skipping notification.")
        return False
    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.HTTPError as e:
        print(f"[Discord:{channel_label}] HTTP error: {e.response.status_code} {e.response.text[:120]}")
        return False
    except Exception as e:
        print(f"[Discord:{channel_label}] Error: {e}")
        return False


def send_discord(message: str) -> bool:
    """Post to the general channel (投稿ログ)."""
    return _post(DISCORD_WEBHOOK_URL, message, "general")


def send_discord_error(message: str) -> bool:
    """Post to the error channel, falling back to general if not configured."""
    url = DISCORD_WEBHOOK_ERROR or DISCORD_WEBHOOK_URL
    return _post(url, message, "error")


def send_discord_report(message: str) -> bool:
    """Post to the report channel, falling back to general if not configured."""
    url = DISCORD_WEBHOOK_REPORT or DISCORD_WEBHOOK_URL
    return _post(url, message, "report")


# Suggested fixes for known error patterns
_ERROR_FIXES: list[tuple[str, str]] = [
    ("190",             "アクセストークンが無効または期限切れです。トークンを再発行してください。"),
    ("invalid.*token",  "アクセストークンが無効です。.envのトークンを確認してください。"),
    ("400",             "リクエストパラメータを確認してください。"),
    ("401",             "認証エラーです。認証情報を再確認してください。"),
    ("403",             "権限が不足しています。アプリのパーミッション設定を確認してください。"),
    ("429",             "レート制限に達しました。しばらく待ってから再試行します。"),
    ("500|502|503",     "APIサーバー側のエラーです。次回の定期実行で自動リトライします。"),
    ("payment",         "APIの利用上限に達しました。プランのアップグレードを検討してください。"),
    ("image",           "画像URLが無効またはアクセス不可です。商品画像を確認してください。"),
]


def _suggest_fix(error: str) -> str:
    import re
    for pattern, fix in _ERROR_FIXES:
        if re.search(pattern, error, re.IGNORECASE):
            return fix
    return "次回の定期実行で自動リトライします。"


def send_discord_error_embed(
    platform: str,
    error: str,
    title: str = "",
    retry_minutes: int = 30,
) -> bool:
    """
    Send a rich embed error notification to the error webhook.
    Includes platform, error message, suggested fix, and retry time.
    """
    url = DISCORD_WEBHOOK_ERROR or DISCORD_WEBHOOK_URL
    if not url:
        return False

    fix = _suggest_fix(error)
    embed = {
        "title":       f"❌ {platform} 投稿エラー",
        "color":       0xEF4444,   # red
        "fields": [
            {"name": "商品", "value": title[:100] or "—",   "inline": False},
            {"name": "エラー内容", "value": error[:500],     "inline": False},
            {"name": "対処法",    "value": fix,              "inline": False},
            {"name": "次回リトライ", "value": f"約{retry_minutes}分後（次の定期実行時）", "inline": True},
        ],
        "timestamp": __import__('datetime').datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        resp = requests.post(url, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Discord:error-embed] Error: {e}")
        return False


def check_instagram_rename_reminder(today: date | None = None) -> bool:
    """Send a Discord reminder on April 13, 2026 to rename the Instagram profile.
    Instagram accounts must wait 14 days after creation before renaming.
    Account created: 2026-03-30 → rename available: 2026-04-13.
    Returns True if the reminder was sent."""
    if today is None:
        today = date.today()
    if today == date(2026, 4, 13):
        msg = (
            "📝 Instagramのプロフィール名を「買って良かったモノ図鑑」に変更できます！\n"
            "アカウント作成から14日が経過しました。\n"
            "Instagram → プロフィール編集 → 名前 で変更してください。"
        )
        send_discord(msg)
        print("[Reminder] Instagram rename reminder sent.")
        return True
    return False


if __name__ == "__main__":
    ok = send_discord("🤖 AffiliateBotが起動しました")
    print("general: sent." if ok else "general: failed.")
