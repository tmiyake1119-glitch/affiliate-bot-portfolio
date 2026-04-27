import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from database import get_all_learning_data

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_AB_COUNTER_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'ab_counter.txt')
_AB_CONFIG_PATH  = os.path.join(os.path.dirname(__file__), '..', 'data', 'ab_config.json')


def _load_ab_config() -> dict:
    """Load ab_config.json. Default: 50/50 split."""
    if os.path.exists(_AB_CONFIG_PATH):
        with open(_AB_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"ratio_a": 0.5, "ratio_b": 0.5, "winner": None}


def _save_ab_config(config: dict) -> None:
    os.makedirs(os.path.dirname(_AB_CONFIG_PATH), exist_ok=True)
    with open(_AB_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _next_group() -> str:
    """Return 'A' or 'B' based on current ratio in ab_config.json."""
    config = _load_ab_config()
    ratio_a = config.get("ratio_a", 0.5)
    return "A" if random.random() < ratio_a else "B"


def assign_group() -> str:
    """Assign the next A/B group for a new queue entry."""
    return _next_group()


def analyze_ab_results() -> str:
    """Analyze A/B results from the last 7 days of learning_data.

    Returns "A", "B", or "insufficient_data" (requires ≥5 samples per group).
    """
    data = get_all_learning_data()
    if not data:
        return "insufficient_data"

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent = [e for e in data if e.get("posted_at", "") >= cutoff]

    scores: dict[str, list[int]] = {}
    for e in recent:
        group = e.get("ab_group", "A")
        scores.setdefault(group, []).append(e.get("engagement", 0))

    a_scores = scores.get("A", [])
    b_scores = scores.get("B", [])

    avg_a_str = f"{sum(a_scores)/len(a_scores):.2f}" if a_scores else "0.00"
    avg_b_str = f"{sum(b_scores)/len(b_scores):.2f}" if b_scores else "0.00"
    print(f"[AB] 直近7日 — A: {len(a_scores)}件 avg={avg_a_str}"
          f"  B: {len(b_scores)}件 avg={avg_b_str}")

    if len(a_scores) < 5 or len(b_scores) < 5:
        return "insufficient_data"

    avg_a = sum(a_scores) / len(a_scores)
    avg_b = sum(b_scores) / len(b_scores)
    return "B" if avg_b > avg_a else "A"


def auto_adjust_ab_ratio() -> None:
    """Adjust A/B ratio based on analyze_ab_results() and save to ab_config.json.

    When data is insufficient, resets to 50/50 if 7+ days have passed since the
    last reset — breaking the self-reinforcing A-dominance loop.
    """
    from discord_notifier import send_discord

    winner = analyze_ab_results()
    config = _load_ab_config()
    now = datetime.now(timezone.utc)

    if winner == "insufficient_data":
        last_reset_str = config.get("last_reset_at", "")
        if last_reset_str:
            try:
                last_reset = datetime.fromisoformat(last_reset_str)
                days_since = (now - last_reset).days
            except ValueError:
                days_since = 999
        else:
            days_since = 999  # never reset → treat as overdue

        if days_since >= 7:
            config["ratio_a"] = 0.5
            config["ratio_b"] = 0.5
            config["last_reset_at"] = now.isoformat()
            _save_ab_config(config)
            print(f"[AB] データ不足({days_since}日超) → 比率を50/50にリセット")
            send_discord("🔬 A/Bテスト: データ不足のため比率を 50/50 にリセット")
        else:
            print(f"[AB] データ不足のため比率調整スキップ（前回リセットから{days_since}日）")
        return

    prev_winner = config.get("winner")

    if winner == "A":
        config["ratio_a"] = 0.7
        config["ratio_b"] = 0.3
    else:
        config["ratio_a"] = 0.3
        config["ratio_b"] = 0.7
    config["winner"] = winner
    config["last_reset_at"] = ""

    _save_ab_config(config)

    ratio_str = f"A {int(config['ratio_a']*100)}% / B {int(config['ratio_b']*100)}%"
    changed = "（変更なし）" if winner == prev_winner else "（更新）"
    print(f"[AB] 勝者: Group {winner}  →  比率 {ratio_str} {changed}")
    send_discord(f"🔬 A/Bテスト自動調整 {changed}\n勝者: Group {winner}\n新比率: {ratio_str}")


def generate_post_b(product: dict, hashtags: str) -> str:
    """Group B templates: question, story, urgency styles."""
    rs = product.get("review_summary", "")
    review_block = f"📝 レビューまとめ：\n{rs}\n\n" if rs else ""

    templates = [
        # question-based
        lambda: (
            f"これ知ってる？👀\n\n"
            f"「{product['title']}」\n\n"
            f"{review_block}"
            f"価格: {product['price_str']}\n\n"
            f"知らなかったら損してるかも！\n"
            f"詳細はこちら👇\n"
            f"{product['affiliate_url']}\n\n"
            f"{hashtags}"
        ),
        # story-based
        lambda: (
            f"先日買ってみたんだけど、これが大当たりでした😊\n\n"
            f"「{product['title']}」\n\n"
            f"{review_block}"
            f"値段は {product['price_str']} — 使ってみると全然違う！\n"
            f"気になる人はチェックを👇\n"
            f"{product['affiliate_url']}\n\n"
            f"{hashtags}"
        ),
        # urgency-based
        lambda: (
            f"⚠️ 在庫なくなる前に！\n\n"
            f"「{product['title']}」\n\n"
            f"{review_block}"
            f"今なら {product['price_str']} で購入可能🔔\n"
            f"売り切れ注意！今すぐチェック👇\n"
            f"{product['affiliate_url']}\n\n"
            f"{hashtags}"
        ),
    ]
    return random.choice(templates)()
