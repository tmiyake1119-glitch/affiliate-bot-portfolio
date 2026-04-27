"""Discord Bot — フォロー・いいね実績管理

スマートフォンから Discord でフォロー実績をリアルタイム更新するための Bot。
comment_reply_bot.py と同様に単独プロセスとして常駐させる。

起動方法:
    python agents/discord_bot.py

Windowsタスクスケジューラ設定:
    トリガー: コンピューターの起動時
    操作: python "C:\\path\\to\\affiliate_bot\\agents\\discord_bot.py"
    開始: C:\\path\\to\\affiliate_bot

コマンド一覧:
    !follow [フォロー数] [いいね数]  — 今日の実績を更新
    !follow_status                   — 今日の実績を表示
    !follow_help                     — このヘルプを表示
"""

import os
import sys
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

from database import (
    get_all_follower_history,
    get_today_follow_log,
    upsert_follow_log,
)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

_JST = timezone(timedelta(hours=9))

# フォロワー数別 1日上限テーブル（follow_reminder.py と同値）
_TIERS: list[tuple[int, int, int]] = [
    (500, 10, 30),
    (200, 15, 40),
    (  0, 20, 50),
]


def _today_jst() -> str:
    return datetime.now(_JST).strftime("%Y-%m-%d")


def _latest_instagram() -> int | None:
    history = get_all_follower_history()
    return history[-1].get("instagram") if history else None


def _get_targets(instagram: int | None) -> tuple[int, int]:
    count = instagram or 0
    for min_f, tf, tl in _TIERS:
        if count >= min_f:
            return tf, tl
    return 20, 50


def _progress_bar(current: int, target: int, width: int = 10) -> str:
    """テキストプログレスバーを生成する。"""
    pct   = min(1.0, current / target) if target > 0 else 0.0
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Bot セットアップ
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready() -> None:
    print(f"[FollowBot] Logged in as {bot.user} (id={bot.user.id})")
    print(f"[FollowBot] Serving {len(bot.guilds)} guild(s)")


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------

@bot.command(name="follow")
async def cmd_follow(ctx: commands.Context, follows: int = None, likes: int = None) -> None:  # type: ignore[assignment]
    """!follow [フォロー数] [いいね数] — 今日の実績を更新する。"""
    if follows is None or likes is None:
        await ctx.send(
            "❌ 使い方: `!follow [フォロー数] [いいね数]`\n"
            "例: `!follow 20 50`"
        )
        return

    instagram = _latest_instagram()
    target_follows, target_likes = _get_targets(instagram)
    today = _today_jst()

    upsert_follow_log(
        today, follows=follows, likes=likes,
        target_follows=target_follows, target_likes=target_likes,
    )

    f_ok     = follows >= target_follows
    l_ok     = likes   >= target_likes
    f_status = "✅ 達成" if f_ok else f"⚠️ 不足 {target_follows - follows}件"
    l_status = "✅ 達成" if l_ok else f"⚠️ 不足 {target_likes - likes}件"
    closing  = "今日もお疲れ様だぽん！🔍" if (f_ok and l_ok) else "もう少し頑張れだぽん！💪"

    f_bar = _progress_bar(follows, target_follows)
    l_bar = _progress_bar(likes,   target_likes)

    await ctx.send(
        f"✅ 実績を更新しました！\n"
        f"👥 フォロー: {follows}/{target_follows}件 {f_status}\n"
        f"　 `{f_bar}` {round(follows/target_follows*100) if target_follows else 0}%\n"
        f"❤️ いいね: {likes}/{target_likes}件 {l_status}\n"
        f"　 `{l_bar}` {round(likes/target_likes*100) if target_likes else 0}%\n"
        f"{closing}"
    )
    print(f"[FollowBot] 実績更新: {today} follows={follows} likes={likes} by {ctx.author}")


@bot.command(name="follow_status")
async def cmd_follow_status(ctx: commands.Context) -> None:
    """!follow_status — 今日の実績を表示する。"""
    instagram = _latest_instagram()
    target_follows, target_likes = _get_targets(instagram)
    today = _today_jst()
    log   = get_today_follow_log(today, target_follows=target_follows, target_likes=target_likes)

    follows = log.get("follows", 0)         or 0
    likes   = log.get("likes",   0)         or 0
    tf      = log.get("target_follows", target_follows) or target_follows
    tl      = log.get("target_likes",   target_likes)   or target_likes

    f_pct    = round(follows / tf * 100) if tf else 0
    l_pct    = round(likes   / tl * 100) if tl else 0
    f_status = "✅" if follows >= tf else "⚠️"
    l_status = "✅" if likes   >= tl else "⚠️"
    f_bar    = _progress_bar(follows, tf)
    l_bar    = _progress_bar(likes,   tl)

    await ctx.send(
        f"📊 **今日 ({today}) の実績**\n"
        f"👥 フォロー: {follows}/{tf}件 {f_status}\n"
        f"　 `{f_bar}` {f_pct}%\n"
        f"❤️ いいね: {likes}/{tl}件 {l_status}\n"
        f"　 `{l_bar}` {l_pct}%"
    )


@bot.command(name="follow_help")
async def cmd_follow_help(ctx: commands.Context) -> None:
    """!follow_help — コマンド一覧を表示する。"""
    await ctx.send(
        "📋 **フォロー Bot コマンド一覧**\n\n"
        "`!follow [フォロー数] [いいね数]`\n"
        "　→ 今日の実績を更新\n"
        "　　例: `!follow 20 50`\n\n"
        "`!follow_status`\n"
        "　→ 今日の進捗を確認\n\n"
        "`!follow_help`\n"
        "　→ このヘルプを表示"
    )


# ---------------------------------------------------------------------------
# エラーハンドリング
# ---------------------------------------------------------------------------

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ 数値を入力してください。例: `!follow 20 50`")
    elif isinstance(error, commands.CommandNotFound):
        pass  # 不明コマンドは無視
    else:
        print(f"[FollowBot] Error: {error}")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("[FollowBot] ERROR: DISCORD_BOT_TOKEN が .env に設定されていません")
        sys.exit(1)
    print("[FollowBot] Starting...")
    bot.run(DISCORD_BOT_TOKEN)
