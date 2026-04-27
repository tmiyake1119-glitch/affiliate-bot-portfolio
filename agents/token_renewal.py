"""
token_renewal.py

Checks if the Instagram / Threads access tokens expire soon.
- Instagram: exchanges for a new long-lived token and updates .env in place.
- Threads  : notifies via Discord (WARNING at 30 days, CRITICAL at 7 days).
             Threads tokens cannot be auto-renewed by the bot; manual action required.
"""

import logging
import os
import re
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

logger = logging.getLogger("affiliate_bot.token_renewal")

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=ENV_PATH)

RENEW_THRESHOLD_DAYS = 7


def _read_env() -> str:
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def _write_env(content: str) -> None:
    with open(ENV_PATH, 'w', encoding='utf-8') as f:
        f.write(content)


def _update_env_key(key: str, value: str) -> None:
    content = _read_env()
    content = re.sub(
        rf'^{re.escape(key)}=.*$',
        f'{key}={value}',
        content,
        flags=re.MULTILINE,
    )
    _write_env(content)


def check_token_expiry() -> dict:
    """
    Debug the current INSTAGRAM_ACCESS_TOKEN via graph.facebook.com/debug_token.
    Returns dict with keys: expires_at (datetime|None), days_remaining (int|None), valid (bool).
    """
    token      = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    app_id     = os.getenv("INSTAGRAM_APP_ID", "")
    app_secret = os.getenv("INSTAGRAM_APP_SECRET", "")

    if not token:
        return {"valid": False, "expires_at": None, "days_remaining": None, "error": "no token"}

    try:
        resp = requests.get(
            "https://graph.facebook.com/debug_token",
            params={
                "input_token":  token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        if not data.get("is_valid"):
            return {"valid": False, "expires_at": None, "days_remaining": None,
                    "error": data.get("error", {}).get("message", "invalid token")}

        expires_ts = data.get("expires_at", 0)
        if expires_ts == 0:
            # Never-expiring token (e.g. page tokens)
            return {"valid": True, "expires_at": None, "days_remaining": 9999}

        expires_at     = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
        days_remaining = (expires_at - datetime.now(timezone.utc)).days
        return {"valid": True, "expires_at": expires_at, "days_remaining": days_remaining}
    except Exception as e:
        return {"valid": False, "expires_at": None, "days_remaining": None, "error": str(e)}


def renew_token() -> dict:
    """
    Exchange the current token for a new long-lived token and write it to .env.
    Returns dict with keys: success (bool), new_token (str), expires_in (int), error (str).
    """
    token      = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    app_id     = os.getenv("INSTAGRAM_APP_ID", "")
    app_secret = os.getenv("INSTAGRAM_APP_SECRET", "")

    try:
        resp = requests.get(
            "https://graph.facebook.com/v22.0/oauth/access_token",
            params={
                "grant_type":        "fb_exchange_token",
                "client_id":         app_id,
                "client_secret":     app_secret,
                "fb_exchange_token": token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data        = resp.json()
        new_token   = data["access_token"]
        expires_in  = data.get("expires_in", 0)

        _update_env_key("INSTAGRAM_ACCESS_TOKEN", new_token)
        # Reload env so subsequent code in this process uses the new token
        load_dotenv(dotenv_path=ENV_PATH, override=True)

        print(f"[TokenRenewal] Token renewed. Expires in {expires_in // 86400} days.")
        return {"success": True, "new_token": new_token, "expires_in": expires_in, "error": None}
    except Exception as e:
        return {"success": False, "new_token": None, "expires_in": 0, "error": str(e)}


_THREADS_WARN_DAYS     = 30   # WARNING threshold (days)
_THREADS_CRITICAL_DAYS = 7    # CRITICAL threshold (days)


def check_threads_token_expiry() -> str | None:
    """
    Check the Threads access token expiry via graph.facebook.com/debug_token.

    Uses .env keys:
      THREADS_ACCESS_TOKEN  — the token to inspect
      THREADS_APP_ID        — app ID (part of the app access token)
      THREADS_APP_SECRET    — app secret (part of the app access token)

    Returns a Discord notification string when the token is expiring soon
    or is already invalid, otherwise None.

    Note: Threads tokens cannot be auto-renewed by the bot itself.
    Manual re-generation in Meta Business Suite is required.
    """
    token      = os.getenv("THREADS_ACCESS_TOKEN", "")
    app_id     = os.getenv("THREADS_APP_ID", "")
    app_secret = os.getenv("THREADS_APP_SECRET", "")

    if not token or not app_id or not app_secret:
        logger.warning("[ThreadsToken] 必要な環境変数が未設定: THREADS_ACCESS_TOKEN / THREADS_APP_ID / THREADS_APP_SECRET")
        return "⚠️ [Threads] トークン確認に必要な環境変数が未設定です"

    try:
        resp = requests.get(
            "https://graph.facebook.com/debug_token",
            params={
                "input_token":  token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        if not data.get("is_valid"):
            err_msg = data.get("error", {}).get("message", "invalid token")
            logger.error("[ThreadsToken] トークンが無効: %s", err_msg)
            return f"🚨 [Threads] アクセストークンが無効です: {err_msg}\n手動でトークンを再発行してください。"

        expires_ts = data.get("expires_at", 0)
        if expires_ts == 0:
            # Never-expiring token
            logger.info("[ThreadsToken] Threads token does not expire.")
            return None

        expires_at     = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
        days_remaining = (expires_at - datetime.now(timezone.utc)).days

        logger.info("[ThreadsToken] Threads token expires in %d day(s).", days_remaining)

        if days_remaining <= _THREADS_CRITICAL_DAYS:
            return (
                f"🚨 [Threads] アクセストークンの有効期限まで残り **{days_remaining}日** です！\n"
                f"期限: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                "⚡ 今すぐ Meta Business Suite でトークンを再発行してください。"
            )
        if days_remaining <= _THREADS_WARN_DAYS:
            return (
                f"⚠️ [Threads] アクセストークンの有効期限まで残り {days_remaining}日 です。\n"
                f"期限: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                "Meta Business Suite でトークンの更新をご検討ください。"
            )

        return None

    except Exception:
        logger.exception("[ThreadsToken] トークン確認中にエラーが発生しました")
        return "⚠️ [Threads] トークン有効期限の確認中にエラーが発生しました"


def check_and_renew() -> str | None:
    """
    Main entry point called from main.py.
    Returns a Discord notification string if action was taken, else None.
    """
    info = check_token_expiry()

    if not info["valid"]:
        err = info.get("error", "unknown")
        print(f"[TokenRenewal] Token invalid: {err}")
        return f"⚠️ [Instagram] アクセストークンが無効です: {err}"

    days = info["days_remaining"]
    print(f"[TokenRenewal] Instagram token expires in {days} day(s).")

    if days <= RENEW_THRESHOLD_DAYS:
        print(f"[TokenRenewal] Renewing token (expires in {days} days)...")
        result = renew_token()
        if result["success"]:
            new_expiry_days = result["expires_in"] // 86400
            return (
                f"🔄 [Instagram] アクセストークンを自動更新しました。\n"
                f"新しい有効期限: 約{new_expiry_days}日後"
            )
        else:
            return f"❌ [Instagram] トークン更新失敗: {result['error']}"
    return None


if __name__ == "__main__":
    msg = check_and_renew()
    if msg:
        print(msg)
