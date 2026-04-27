"""
comment_reply_bot.py

Every 30 minutes:
  1. Fetches replies on recent Threads posts
  2. Generates a Japanese AI reply via Claude API (≤100 chars)
  3. Posts comment + suggestion to #コメント承認 in Discord
  4. On 👍 reaction → posts the reply to Threads automatically
"""

import asyncio
import json
import os
import sys

import openai
import discord
import requests
from discord.ext import tasks
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

DISCORD_BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN", "")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID      = os.getenv("THREADS_USER_ID", "me")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")

APPROVAL_CHANNEL_NAME = "コメント承認"
THUMBS_UP             = "👍"
MAX_REPLY_CHARS       = 100
RECENT_POSTS_LIMIT    = 10  # how many recent post_log entries to check

DATA_DIR              = os.path.join(os.path.dirname(__file__), "..", "data")
POST_LOG_PATH         = os.path.join(DATA_DIR, "post_log.json")
PROCESSED_PATH        = os.path.join(DATA_DIR, "processed_comments.json")
PENDING_PATH          = os.path.join(DATA_DIR, "pending_approvals.json")


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_processed() -> set[str]:
    if os.path.exists(PROCESSED_PATH):
        with open(PROCESSED_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def _save_processed(ids: set[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROCESSED_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def _load_pending() -> dict[int, dict]:
    """Load pending_approvals from JSON. Keys are stored as str, convert to int."""
    if os.path.exists(PENDING_PATH):
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    return {}


def _save_pending(approvals: dict[int, dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in approvals.items()}, f, ensure_ascii=False, indent=2)


def _load_recent_post_ids() -> list[str]:
    if not os.path.exists(POST_LOG_PATH):
        return []
    with open(POST_LOG_PATH, "r", encoding="utf-8") as f:
        log = json.load(f)
    successful = [e["post_id"] for e in log if e.get("status") == "success" and e.get("post_id")]
    return successful[-RECENT_POSTS_LIMIT:]


# ---------------------------------------------------------------------------
# Threads API helpers
# ---------------------------------------------------------------------------

def _fetch_replies(post_id: str) -> list[dict]:
    """Return list of reply dicts with keys: id, text, username, timestamp.

    Uses /conversation (all nested replies) as primary, falls back to
    /replies (top-level only) if the conversation endpoint errors.
    Note: /me/replies does not exist in the Threads API.
    """
    # Fields: username only works for public users and yourself; safe to request.
    fields = "id,text,username,timestamp"
    base = "https://graph.threads.net/v1.0"

    for endpoint in (f"{base}/{post_id}/conversation", f"{base}/{post_id}/replies"):
        try:
            resp = requests.get(endpoint, params={
                "fields":       fields,
                "access_token": THREADS_ACCESS_TOKEN,
            }, timeout=15)
            if not resp.ok:
                print(
                    f"[Threads] {resp.status_code} from {endpoint} — "
                    f"{resp.text[:300]}"
                )
                continue
            data = resp.json().get("data", [])
            print(f"[Threads] {len(data)} replies from {endpoint}")
            return data
        except Exception as e:
            print(f"[Threads] Error calling {endpoint}: {e}")

    return []


def _post_reply_to_threads(comment_id: str, reply_text: str) -> bool:
    """Create and publish a reply to a Threads comment. Returns True on success."""
    try:
        # Step 1: create container
        create_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
        resp = requests.post(create_url, params={
            "media_type":   "TEXT",
            "text":         reply_text,
            "reply_to_id":  comment_id,
            "access_token": THREADS_ACCESS_TOKEN,
        }, timeout=15)
        resp.raise_for_status()
        creation_id = resp.json()["id"]

        # Step 2: publish
        publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
        resp = requests.post(publish_url, params={
            "creation_id":  creation_id,
            "access_token": THREADS_ACCESS_TOKEN,
        }, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Threads] Failed to post reply to comment {comment_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Claude API helper
# ---------------------------------------------------------------------------

def _generate_reply(comment_text: str) -> str:
    """Generate a natural Japanese reply ≤100 chars using OpenAI."""
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    prompt = (
        f"以下のThreadsコメントに対して、自然で親しみやすい日本語の返信を1文で生成してください。"
        f"絶対に{MAX_REPLY_CHARS}文字以内にしてください。返信文のみ出力し、説明は不要です。\n\n"
        f"コメント: {comment_text}"
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    reply = response.choices[0].message.content.strip()
    return reply[:MAX_REPLY_CHARS]


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = discord.Client(intents=intents)

# Persistent map: discord message_id (int) -> {comment_id, post_id, reply_text}
pending_approvals: dict[int, dict] = _load_pending()


def _get_approval_channel() -> discord.TextChannel | None:
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == APPROVAL_CHANNEL_NAME:
                return channel
    return None


@tasks.loop(minutes=30)
async def poll_comments() -> None:
    print("[Poll] Checking Threads comments...")
    channel = _get_approval_channel()
    if channel is None:
        print(f"[Poll] Channel #{APPROVAL_CHANNEL_NAME} not found — skipping.")
        return

    post_ids      = _load_recent_post_ids()
    processed_ids = _load_processed()
    new_processed = set()

    for post_id in post_ids:
        replies = _fetch_replies(post_id)
        for comment in replies:
            comment_id   = comment.get("id", "")
            comment_text = comment.get("text", "")
            username     = comment.get("username", "unknown")

            if not comment_id or comment_id in processed_ids:
                continue

            print(f"[Poll] New comment {comment_id} from @{username}: {comment_text[:60]}")

            # Generate AI reply
            try:
                reply_text = _generate_reply(comment_text)
            except Exception as e:
                print(f"[Claude] Error generating reply: {e}")
                reply_text = "ありがとうございます！"

            # Post to Discord for approval
            embed = discord.Embed(
                title="💬 新しいコメントが届きました",
                color=discord.Color.blue(),
            )
            embed.add_field(name="投稿ID",      value=post_id,      inline=True)
            embed.add_field(name="コメントID",   value=comment_id,   inline=True)
            embed.add_field(name="ユーザー",      value=f"@{username}", inline=True)
            embed.add_field(name="コメント内容",  value=comment_text, inline=False)
            embed.add_field(name="AI返信案",     value=reply_text,   inline=False)
            embed.set_footer(text=f"👍 を押すと自動返信します（{MAX_REPLY_CHARS}文字以内）")

            try:
                msg = await channel.send(embed=embed)
                await msg.add_reaction(THUMBS_UP)
                pending_approvals[msg.id] = {
                    "comment_id": comment_id,
                    "post_id":    post_id,
                    "reply_text": reply_text,
                }
                new_processed.add(comment_id)
            except discord.DiscordException as e:
                print(f"[Discord] Failed to send approval message: {e}")

    if new_processed:
        processed_ids.update(new_processed)
        _save_processed(processed_ids)
        print(f"[Poll] Processed {len(new_processed)} new comment(s).")
    else:
        print("[Poll] No new comments found.")


@poll_comments.before_loop
async def before_poll() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    print(f"[Bot] Logged in as {bot.user} (id={bot.user.id})")
    print(f"[Bot] Loaded {len(pending_approvals)} pending approval(s) from disk.")
    poll_comments.start()


@bot.event
async def on_disconnect() -> None:
    _save_pending(pending_approvals)
    print(f"[Bot] Saved {len(pending_approvals)} pending approval(s) to disk.")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    if str(payload.emoji) != THUMBS_UP:
        return

    approval = pending_approvals.pop(payload.message_id, None)
    _save_pending(pending_approvals)
    if approval is None:
        return

    comment_id = approval["comment_id"]
    reply_text = approval["reply_text"]

    print(f"[Approve] 👍 received — posting reply to comment {comment_id}")
    success = await asyncio.get_event_loop().run_in_executor(
        None, _post_reply_to_threads, comment_id, reply_text
    )

    # Update the Discord message to reflect the outcome
    channel = bot.get_channel(payload.channel_id)
    if channel:
        try:
            msg = await channel.fetch_message(payload.message_id)
            status_text = "✅ 返信を投稿しました！" if success else "❌ 返信の投稿に失敗しました。"
            await msg.reply(status_text)
        except discord.DiscordException:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("[Error] DISCORD_BOT_TOKEN is not set in .env")
        sys.exit(1)
    if not THREADS_ACCESS_TOKEN:
        print("[Error] THREADS_ACCESS_TOKEN is not set in .env")
        sys.exit(1)
    bot.run(DISCORD_BOT_TOKEN)
