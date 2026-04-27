"""
strategy_agent.py — 毎朝の戦略会議エージェント

処理フロー:
  1. 各種データファイルから昨日の実績・現状を収集
  2. GPT-4o-mini に渡して今日の戦略 JSON を生成
  3. Discord WEBHOOK_REPORT に Embed 送信
  4. data/daily_strategy.json に保存

main.py から: 9時JST (start.hour == 0 UTC) の実行時のみ呼び出す
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from llm_core_bridge import ask as _ask_llm_core
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

import requests
from database import get_all_post_logs, get_all_learning_data, get_all_follower_history

_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
_HOURLY_ENG             = os.path.join(_DATA_DIR, 'hourly_engagement.json')
_BAN_RISK_STATUS        = os.path.join(_DATA_DIR, 'ban_risk_status.json')
_AB_CONFIG              = os.path.join(_DATA_DIR, 'ab_config.json')
_STRATEGY_PATH          = os.path.join(_DATA_DIR, 'daily_strategy.json')
_SELECTION_STRATEGY_PATH = os.path.join(_DATA_DIR, 'strategy_for_selection.json')

_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_REPORT") or os.getenv("DISCORD_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# データ収集
# ---------------------------------------------------------------------------

def _load_json(path: str, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _collect_yesterday_results() -> dict:
    """post_log.json から昨日（JST）の投稿結果を集計する。"""
    now_jst  = datetime.now(timezone.utc) + timedelta(hours=9)
    yesterday = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")

    log = get_all_post_logs()
    yesterday_posts = [
        p for p in log
        if (p.get("posted_at") or "").startswith(yesterday)
    ]

    success = [p for p in yesterday_posts if p.get("status") == "success"]
    error   = [p for p in yesterday_posts if p.get("status") == "error"]

    genre_count: dict[str, int] = {}
    for p in success:
        g = p.get("genre", "unknown")
        genre_count[g] = genre_count.get(g, 0) + 1

    return {
        "date":          yesterday,
        "total":         len(yesterday_posts),
        "success":       len(success),
        "error":         len(error),
        "success_rate":  round(len(success) / len(yesterday_posts) * 100, 1) if yesterday_posts else 0,
        "genre_breakdown": genre_count,
    }


def _collect_top_genres() -> list[dict]:
    """learning_data.json から直近7日のジャンル別平均エンゲージメントを返す。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    data   = get_all_learning_data()

    recent = [e for e in data if (e.get("posted_at") or "") >= cutoff]
    if not recent:
        recent = data  # データ不足時は全期間

    genre_scores: dict[str, list[int]] = {}
    for e in recent:
        g = e.get("genre")
        s = e.get("engagement", 0)
        if g:
            genre_scores.setdefault(g, []).append(s)

    ranked = sorted(
        [{"genre": g, "avg_engagement": round(sum(v) / len(v), 1), "count": len(v)}
         for g, v in genre_scores.items()],
        key=lambda x: -x["avg_engagement"]
    )
    return ranked[:5]


def _collect_follower_info() -> dict:
    """follower_history DB から最新フォロワー数と前回比増減を返す。"""
    history = get_all_follower_history()
    if not history:
        return {"threads": None, "instagram": None, "bluesky": None, "diff_threads": None}

    latest = history[-1]
    prev   = history[-2] if len(history) >= 2 else None

    def _get(entry, key):
        v = entry.get(key) or entry.get("count") if key == "threads" else entry.get(key)
        return int(v) if v is not None else None

    curr_t = _get(latest, "threads")
    prev_t = _get(prev, "threads") if prev else None
    diff_t = (curr_t - prev_t) if (curr_t is not None and prev_t is not None) else None

    return {
        "threads":      curr_t,
        "instagram":    _get(latest, "instagram"),
        "bluesky":      _get(latest, "bluesky"),
        "diff_threads": diff_t,
    }


def _collect_best_hours() -> list[dict]:
    """hourly_engagement.json からエンゲージメント上位3時間を返す (JST)。"""
    hourly = _load_json(_HOURLY_ENG, default={})
    if not hourly:
        return []
    ranked = sorted(
        [{"hour_jst": int(h), "avg": v["avg"], "count": v["count"]}
         for h, v in hourly.items() if v.get("count", 0) >= 1],
        key=lambda x: -x["avg"]
    )
    return ranked[:3]


def _collect_ban_status() -> dict:
    """ban_risk_status.json の現在状態を返す。"""
    status = _load_json(_BAN_RISK_STATUS, default={"stopped": False, "reason": ""})
    return {
        "stopped": status.get("stopped", False),
        "reason":  status.get("reason", ""),
    }


def _collect_ab_config() -> dict:
    """ab_config.json の現在の A/B 比率を返す。"""
    cfg = _load_json(_AB_CONFIG, default={"ratio_a": 0.5, "ratio_b": 0.5, "winner": None})
    return {
        "ratio_a": cfg.get("ratio_a", 0.5),
        "ratio_b": cfg.get("ratio_b", 0.5),
        "winner":  cfg.get("winner"),
    }


# ---------------------------------------------------------------------------
# GPT-4o-mini 戦略生成
# ---------------------------------------------------------------------------

_STRATEGY_SCHEMA = {
    "summary":        "昨日の結果の一言まとめ",
    "risk_level":     "low/medium/high",
    "top_genre":      "今日注力すべきジャンル",
    "avoid_genre":    "今日避けるべきジャンル",
    "post_tone":      "今日の投稿トーン（例：お得感強調・共感系・情報系）",
    "advice":         "今日の具体的なアドバイス（100文字以内）",
    "hashtag_focus":  "今日強化すべきハッシュタグ（3個）",
}

_FALLBACK_STRATEGY = {
    "summary":       "データ不足のためデフォルト戦略を適用",
    "risk_level":    "low",
    "top_genre":     "daily_goods",
    "avoid_genre":   "rare",
    "post_tone":     "お得感強調",
    "advice":        "通常どおりアフィリエイト投稿を続けてください。",
    "hashtag_focus": "#楽天市場 #買って良かった #おすすめ",
    "selection": {
        "priority_genres": ["daily_goods"],
        "price_range":     {"min": 1000, "max": 15000},
        "avoid_genres":    ["rare"],
        "tone":            "formal",
    },
}


def _generate_strategy(context: dict) -> dict:
    """GPT-4o-mini で戦略 JSON を生成。失敗時はフォールバックを返す。"""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key in ("", "xxxx"):
        print("[StrategyAgent] OpenAI APIキー未設定 → フォールバック戦略")
        return _FALLBACK_STRATEGY.copy()

    prompt_data = json.dumps(context, ensure_ascii=False, indent=2)

    # ─── llm_core path ────────────────────────────────────────────────────────
    _STRATEGY_SYSTEM = (
        "あなたは楽天アフィリエイトSNSボットの戦略AIです。\n"
        "データを分析して今日の投稿戦略を日本語で提案してください。\n"
        "キャラクター名はずかぽんです。\n"
        "回答は必ず以下のJSON形式で返してください：\n"
        '{"summary":"昨日の結果の一言まとめ","risk_level":"low/medium/high",'
        '"top_genre":"今日注力すべきジャンル（food/electronics/health/daily_goods/kitchen/pet/outdoor/cosmetics/alcohol/wine/furusato/rare のいずれか）",'
        '"avoid_genre":"今日避けるべきジャンル（同上）",'
        '"post_tone":"今日の投稿トーン（お得感強調・共感系・情報系 のいずれか）",'
        '"advice":"今日の具体的なアドバイス（100文字以内）",'
        '"hashtag_focus":"今日強化すべきハッシュタグ3個をスペース区切りで",'
        '"selection":{"priority_genres":["注力ジャンルキーを最大2つ列挙"],'
        '"price_range":{"min":最低価格整数,"max":最高価格整数},'
        '"avoid_genres":["除外するジャンルキーを列挙"],'
        '"tone":"casual（親近感重視）またはformal（信頼感重視）"}}'
    )
    _core_prompt_s = (
        _STRATEGY_SYSTEM
        + f"\n\n以下のデータを分析して今日の戦略を提案してください：\n\n{prompt_data}"
    )
    _core_result_s = _ask_llm_core(_core_prompt_s, "strategy")
    if _core_result_s is not None:
        try:
            strategy = json.loads(_core_result_s)
            for key, default in _FALLBACK_STRATEGY.items():
                if key not in strategy:
                    strategy[key] = default
            print(f"[StrategyAgent] llm_core 戦略生成完了: {strategy.get('post_tone')} / top={strategy.get('top_genre')}")
            return strategy
        except json.JSONDecodeError:
            pass  # パース失敗時はOpenAIへフォールバック
    # ─── end llm_core path ────────────────────────────────────────────────────

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=400,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたは楽天アフィリエイトSNSボットの戦略AIです。\n"
                        "データを分析して今日の投稿戦略を日本語で提案してください。\n"
                        "キャラクター名はずかぽんです。\n"
                        "回答は必ず以下のJSON形式で返してください：\n"
                        "{\n"
                        '  "summary": "昨日の結果の一言まとめ",\n'
                        '  "risk_level": "low/medium/high",\n'
                        '  "top_genre": "今日注力すべきジャンル（英語キー: food/electronics/health/daily_goods/kitchen/pet/outdoor/cosmetics/alcohol/wine/furusato/rare のいずれか）",\n'
                        '  "avoid_genre": "今日避けるべきジャンル（同上）",\n'
                        '  "post_tone": "今日の投稿トーン（お得感強調・共感系・情報系 のいずれか）",\n'
                        '  "advice": "今日の具体的なアドバイス（100文字以内）",\n'
                        '  "hashtag_focus": "今日強化すべきハッシュタグ3個をスペース区切りで",\n'
                        '  "selection": {\n'
                        '    "priority_genres": ["注力ジャンルキーを最大2つ列挙"],\n'
                        '    "price_range": {"min": 最低価格(整数), "max": 最高価格(整数)},\n'
                        '    "avoid_genres": ["除外するジャンルキーを列挙"],\n'
                        '    "tone": "casual（親近感重視）またはformal（信頼感重視）"\n'
                        '  }\n'
                        "}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"以下のデータを分析して今日の戦略を提案してください：\n\n{prompt_data}",
                },
            ],
        )
        raw = resp.choices[0].message.content.strip()
        strategy = json.loads(raw)
        # 必須キーの補完
        for key, default in _FALLBACK_STRATEGY.items():
            if key not in strategy:
                strategy[key] = default
        print(f"[StrategyAgent] GPT-4o-mini 戦略生成完了: {strategy.get('post_tone')} / top={strategy.get('top_genre')}")
        return strategy

    except json.JSONDecodeError as e:
        print(f"[StrategyAgent] JSONパースエラー: {e} → フォールバック")
        return _FALLBACK_STRATEGY.copy()
    except Exception as e:
        print(f"[StrategyAgent] GPT-4o-mini エラー: {e} → フォールバック")
        return _FALLBACK_STRATEGY.copy()


# ---------------------------------------------------------------------------
# Discord 送信
# ---------------------------------------------------------------------------

_RISK_COLOR = {"low": 0x2ECC71, "medium": 0xF39C12, "high": 0xE74C3C}


def _send_strategy_embed(strategy: dict, context: dict) -> bool:
    """戦略結果を Discord WEBHOOK_REPORT に Embed 送信。"""
    if not _WEBHOOK_URL:
        print("[StrategyAgent] DISCORD_WEBHOOK_REPORT 未設定 → スキップ")
        return False

    date_str   = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    risk_level = strategy.get("risk_level", "low")
    color      = _RISK_COLOR.get(risk_level, 0x95A5A6)

    yesterday  = context.get("yesterday", {})
    followers  = context.get("followers", {})
    best_hours = context.get("best_hours", [])

    follower_line = f"Threads: {followers.get('threads', '?')}人"
    if followers.get("diff_threads") is not None:
        diff = followers["diff_threads"]
        follower_line += f"（前回比 {'+' if diff >= 0 else ''}{diff}）"
    if followers.get("instagram"):
        follower_line += f"  Instagram: {followers['instagram']}人"
    if followers.get("bluesky"):
        follower_line += f"  Bluesky: {followers['bluesky']}人"

    hour_str = "データなし"
    if best_hours:
        hour_str = " / ".join([f"{h['hour_jst']}時(avg:{h['avg']})" for h in best_hours])

    embed = {
        "title":  f"🤖 ずかぽん戦略会議 - {date_str}",
        "color":  color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "strategy_agent.py by ずかぽんBot"},
        "fields": [
            {
                "name":   "📋 昨日のまとめ",
                "value":  strategy.get("summary", "—"),
                "inline": False,
            },
            {
                "name":   "⚠️ リスクレベル",
                "value":  risk_level.upper(),
                "inline": True,
            },
            {
                "name":   "🎯 注力ジャンル",
                "value":  strategy.get("top_genre", "—"),
                "inline": True,
            },
            {
                "name":   "🚫 避けるジャンル",
                "value":  strategy.get("avoid_genre", "—"),
                "inline": True,
            },
            {
                "name":   "🎭 今日のトーン",
                "value":  strategy.get("post_tone", "—"),
                "inline": True,
            },
            {
                "name":   "🏷 強化ハッシュタグ",
                "value":  strategy.get("hashtag_focus", "—"),
                "inline": True,
            },
            {
                "name":   "👥 フォロワー",
                "value":  follower_line,
                "inline": False,
            },
            {
                "name":   "⏰ 高エンゲージメント時間帯 (JST)",
                "value":  hour_str,
                "inline": False,
            },
            {
                "name":   "💡 AIアドバイス",
                "value":  strategy.get("advice", "—"),
                "inline": False,
            },
            {
                "name":   "昨日の投稿",
                "value": (
                    f"成功: {yesterday.get('success', 0)}件 / "
                    f"失敗: {yesterday.get('error', 0)}件 / "
                    f"成功率: {yesterday.get('success_rate', 0)}%"
                ),
                "inline": False,
            },
        ],
    }

    try:
        resp = requests.post(_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        resp.raise_for_status()
        print(f"[StrategyAgent] Discord 送信完了 (status={resp.status_code})")
        return True
    except requests.HTTPError as e:
        print(f"[StrategyAgent] Discord HTTP エラー: {e.response.status_code} {e.response.text[:200]}")
        return False
    except Exception as e:
        print(f"[StrategyAgent] Discord 送信エラー: {e}")
        return False


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

def _save_strategy(strategy: dict) -> None:
    """data/daily_strategy.json に戦略を保存する。"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    payload = {
        "date":         datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"),
        "strategy":     strategy,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(_STRATEGY_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[StrategyAgent] 戦略を保存: {_STRATEGY_PATH}")


def _save_selection_strategy(strategy: dict) -> None:
    """data/strategy_for_selection.json に商品選定用の戦略を保存する。

    GPT が strategy 内の "selection" キーとして出力した JSON をそのまま保存する。
    "selection" キーが存在しない場合は top_genre/avoid_genre から導出する。
    """
    selection = strategy.get("selection")

    if not selection or not isinstance(selection, dict):
        # フォールバック: 既存キーから導出
        top_genre   = strategy.get("top_genre", "")
        avoid_genre = strategy.get("avoid_genre", "")
        post_tone   = strategy.get("post_tone", "")
        selection = {
            "priority_genres": [top_genre]   if top_genre   else [],
            "price_range":     {"min": 1000, "max": 15000},
            "avoid_genres":    [avoid_genre] if avoid_genre else [],
            "tone":            "casual" if post_tone == "共感系" else "formal",
        }

    payload = {
        "updated_at":      datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S"),
        "priority_genres": selection.get("priority_genres", []),
        "price_range":     selection.get("price_range", {"min": 1000, "max": 15000}),
        "avoid_genres":    selection.get("avoid_genres", []),
        "tone":            selection.get("tone", "formal"),
    }
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_SELECTION_STRATEGY_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[StrategyAgent] 選定戦略を保存: {_SELECTION_STRATEGY_PATH}")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def run_strategy_meeting() -> dict:
    """
    戦略会議を実行する。
    1. データ収集
    2. GPT-4o-mini で戦略生成
    3. Discord Embed 送信
    4. daily_strategy.json に保存
    戦略 dict を返す。
    """
    print("\n--- StrategyAgent: 戦略会議を開始 ---")

    # 1. データ収集
    yesterday  = _collect_yesterday_results()
    top_genres = _collect_top_genres()
    followers  = _collect_follower_info()
    best_hours = _collect_best_hours()
    ban_status = _collect_ban_status()
    ab_config  = _collect_ab_config()

    context = {
        "yesterday":   yesterday,
        "top_genres":  top_genres,
        "followers":   followers,
        "best_hours":  best_hours,
        "ban_status":  ban_status,
        "ab_config":   ab_config,
    }

    print(f"[StrategyAgent] 昨日: 成功{yesterday['success']}件/失敗{yesterday['error']}件 "
          f"成功率{yesterday['success_rate']}%")
    print(f"[StrategyAgent] フォロワー: {followers}")

    # 2. GPT-4o-mini 戦略生成
    strategy = _generate_strategy(context)

    # 3. Discord 送信
    _send_strategy_embed(strategy, context)

    # 4. 保存（daily_strategy.json + strategy_for_selection.json）
    _save_strategy(strategy)
    _save_selection_strategy(strategy)

    print(f"[StrategyAgent] 完了 — トーン: {strategy.get('post_tone')} / "
          f"注力: {strategy.get('top_genre')} / 回避: {strategy.get('avoid_genre')}")
    return strategy


def load_today_strategy() -> dict | None:
    """
    daily_strategy.json から今日の戦略を読み込む。
    今日の日付と一致しない場合・ファイルなしの場合は None を返す。
    """
    if not os.path.exists(_STRATEGY_PATH):
        return None
    try:
        with open(_STRATEGY_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return None
        return data.get("strategy")
    except Exception:
        return None


if __name__ == "__main__":
    run_strategy_meeting()
