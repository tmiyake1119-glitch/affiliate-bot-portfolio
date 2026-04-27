"""
daily_summary_notifier.py — 22時JST 日次まとめ Discord通知

処理フロー:
  1. post_log.json から今日の成功投稿を取得
  2. learning_data.json からエンゲージメント上位3件を選出
  3. GPT-4o-mini で楽天ROOM用紹介文を生成
  4. DISCORD_WEBHOOK_REPORT に Embed + 動画添付で送信
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from llm_core_bridge import ask as _ask_llm_core

from dotenv import load_dotenv
from database import get_all_post_logs, get_all_learning_data, get_all_queue_entries

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_REPORT") or os.getenv("DISCORD_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------

def _today_posts() -> list[dict]:
    """今日UTC日付の成功投稿を返す（engagement genre を除外）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log   = get_all_post_logs()
    return [
        p for p in log
        if p.get("status") == "success"
        and (p.get("posted_at") or "").startswith(today)
        and p.get("genre") != "engagement"
    ]


def _top_engagement(n: int = 3) -> list[dict]:
    """learning_data からエンゲージメントスコア上位 n 件を返す。"""
    data = get_all_learning_data()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # 今日の投稿のみ対象
    today_data = [e for e in data if (e.get("posted_at") or "").startswith(today)]
    if not today_data:
        # データが少ない場合は全期間から
        today_data = data
    return sorted(today_data, key=lambda e: e.get("engagement", 0), reverse=True)[:n]


# ---------------------------------------------------------------------------
# GPT-4o-mini で楽天ROOM紹介文を生成
# ---------------------------------------------------------------------------

def _generate_room_text(product: dict) -> str:
    """楽天ROOM用の短い紹介文を生成（最大80文字）。失敗時はフォールバック。"""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key in ("", "xxxx"):
        return f"図鑑に載せたくなる一品だよ！{product.get('title', '')[:30]}"

    title  = product.get("title", "")[:60]
    price  = product.get("price", "")
    genre  = product.get("genre", "")

    # ─── llm_core path ────────────────────────────────────────────────────────
    _core_prompt_r = (
        "あなたはずかぽんというキャラクターです。"
        "楽天ROOMに商品を登録する際の短い紹介文を生成してください。"
        "語尾は「だよ！」「だぽん！」を使い、ゆるくポップな口調で。80文字以内で。ハッシュタグ不要。"
        f"\n\n商品名: {title}\n価格: {price}\nジャンル: {genre}"
    )
    _core_result_r = _ask_llm_core(_core_prompt_r, "room_text")
    if _core_result_r is not None:
        return _core_result_r
    # ─── end llm_core path ────────────────────────────────────────────────────

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=80,
            temperature=0.5,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたはずかぽんというキャラクターです。"
                        "楽天ROOMに商品を登録する際の短い紹介文を生成してください。"
                        "語尾は「だよ！」「だぽん！」を使い、ゆるくポップな口調で。"
                        "80文字以内で。ハッシュタグ不要。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"商品名: {title}\n価格: {price}\nジャンル: {genre}",
                },
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[DailySummary] GPT-4o-mini エラー: {e}")
        return f"ずかぽんのおすすめだよ！{title[:30]}"


# ---------------------------------------------------------------------------
# 動画生成
# ---------------------------------------------------------------------------

def _supplement_image_url(product: dict) -> dict:
    """image_url が空の場合、posts_queue DB からタイトル一致で補完する。"""
    if product.get("image_url"):
        return product
    queue = get_all_queue_entries()
    title = (product.get("title") or "").strip()
    for item in reversed(queue):
        if item.get("image_url") and (item.get("title") or "").strip().startswith(title[:15]):
            product = dict(product)
            product["image_url"] = item["image_url"]
            print(f"[DailySummary] image_url を posts_queue から補完: {item['image_url'][:60]}")
            return product
    return product


def _make_video_for_product(product: dict) -> str | None:
    """product dict から動画を生成。失敗時は None。"""
    try:
        from video_generator import generate_video
        product = _supplement_image_url(product)
        return generate_video(product)
    except Exception as e:
        print(f"[DailySummary] 動画生成スキップ: {e}")
        return None


# ---------------------------------------------------------------------------
# Discord 送信
# ---------------------------------------------------------------------------

def _build_embed(today_posts: list[dict], top3: list[dict], room_texts: list[str]) -> dict:
    """Discord Embed payload を構築する。"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 今日の投稿リスト（最大10件）
    post_lines = []
    for p in today_posts[:10]:
        genre = p.get("genre", "-")
        title = (p.get("title") or "")[:35]
        price = p.get("price") or ""
        post_lines.append(f"• [{genre}] {title} {price}")
    if len(today_posts) > 10:
        post_lines.append(f"… 他 {len(today_posts) - 10} 件")
    posts_text = "\n".join(post_lines) if post_lines else "投稿なし"

    # 楽天ROOM候補（上位3件）
    room_fields = []
    for i, (item, room_text) in enumerate(zip(top3, room_texts), 1):
        title    = (item.get("title") or "")[:40]
        price    = item.get("price") or ""
        score    = item.get("engagement", 0)
        url      = item.get("url") or ""
        # 検索キーワード: タイトルの最初の20文字
        search_kw = title[:20]

        value = (
            f"**{title}**\n"
            f"価格: {price}  スコア: {score}\n"
            f"紹介文: {room_text}\n"
            f"検索KW: `{search_kw}`\n"
            f"URL: {url[:80]}"
        )
        room_fields.append({
            "name":   f"🏆 ROOM候補 #{i}",
            "value":  value,
            "inline": False,
        })

    embed = {
        "title":       "📖 ずかぽんの日次まとめ",
        "description": f"**今日の投稿: {len(today_posts)} 件**\n```\n{posts_text}\n```",
        "color":       0x4A90D9,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": f"Generated at {now_str}"},
        "fields":      room_fields,
    }
    return embed


def _send_to_discord(embed: dict, video_path: str | None) -> bool:
    """Embed と動画ファイルを Discord Webhook に送信。"""
    if not _WEBHOOK_URL:
        print("[DailySummary] DISCORD_WEBHOOK_REPORT 未設定 → スキップ")
        return False

    payload = {"embeds": [embed]}

    try:
        if video_path and os.path.exists(video_path):
            with open(video_path, "rb") as vf:
                resp = requests.post(
                    _WEBHOOK_URL,
                    data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                    files={"file": (os.path.basename(video_path), vf, "video/mp4")},
                    timeout=60,
                )
        else:
            resp = requests.post(_WEBHOOK_URL, json=payload, timeout=30)

        resp.raise_for_status()
        print(f"[DailySummary] Discord 送信完了 (status={resp.status_code})")
        return True

    except requests.HTTPError as e:
        print(f"[DailySummary] Discord HTTP エラー: {e.response.status_code} {e.response.text[:200]}")
        return False
    except Exception as e:
        print(f"[DailySummary] Discord 送信エラー: {e}")
        return False


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def post_daily_summary() -> None:
    """22時JST に main.py から呼ばれる日次まとめ通知。"""
    print("\n--- Daily Summary: 日次まとめ通知 ---")

    today_posts = _today_posts()
    print(f"[DailySummary] 今日の投稿数: {len(today_posts)}")

    top3 = _top_engagement(n=3)
    print(f"[DailySummary] エンゲージメント上位: {len(top3)} 件")

    # 楽天ROOM紹介文を生成（上位3件分）
    room_texts = [_generate_room_text(p) for p in top3]

    # 上位1件の動画を生成
    video_path: str | None = None
    if top3:
        print("[DailySummary] 動画生成中（上位1件）...")
        video_path = _make_video_for_product(top3[0])

    embed = _build_embed(today_posts, top3, room_texts)
    _send_to_discord(embed, video_path)


if __name__ == "__main__":
    post_daily_summary()
