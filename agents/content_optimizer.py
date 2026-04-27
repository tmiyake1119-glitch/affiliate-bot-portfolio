"""
content_optimizer.py — 投稿文自動改善ループエージェント

処理フロー:
  1. analyze_post_performance()  — learning_data / post_log から実績分析
  2. generate_improved_templates() — GPT-4o-mini で改善テンプレート生成
  3. apply_improvements()         — post_generator.py の ACCOUNT_OPENERS に追記
  4. run_content_optimizer()      — 上記を順番に実行してDiscord通知

main.py から: 月曜 9時JST (start.weekday()==0 and start.hour==0) のみ呼ぶ
"""

import ast
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from llm_core_bridge import ask as _ask_llm_core
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

import requests
from database import get_all_post_logs, get_all_learning_data

_DATA_DIR          = os.path.join(os.path.dirname(__file__), '..', 'data')
_IMPROVEMENTS_PATH = os.path.join(_DATA_DIR, 'content_improvements.json')
_POST_GEN_PATH     = os.path.join(os.path.dirname(__file__), 'post_generator.py')

_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_REPORT") or os.getenv("DISCORD_WEBHOOK_URL", "")


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


def _avg(values: list) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _parse_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 1. パフォーマンス分析
# ---------------------------------------------------------------------------

def analyze_post_performance() -> dict:
    """
    learning_data.json と post_log.json からパフォーマンスを分析する。

    Returns:
        {
          "ab_groups":   {"A": avg, "B": avg},
          "genres":      {"genre": avg, ...},
          "openers":     {"冒頭20文字": avg, ...},
          "time_bands":  {"hour_jst": avg, ...},
          "top5":        [最高スコア上位5件],
          "bottom5":     [最低スコア下位5件（5件以上ある場合）],
          "total_posts": N,
        }
    """
    learning = get_all_learning_data()
    log      = get_all_post_logs()

    # post_id → post_text マッピング（post_log から）
    text_by_id: dict[str, str] = {
        p["post_id"]: p.get("post_text", "")
        for p in log
        if p.get("post_id") and p.get("post_text")
    }

    # 分析用バケット
    ab_scores:   dict[str, list[int]] = defaultdict(list)
    genre_scores: dict[str, list[int]] = defaultdict(list)
    opener_scores: dict[str, list[int]] = defaultdict(list)
    hour_scores:  dict[int, list[int]] = defaultdict(list)

    scored_entries: list[dict] = []

    for entry in learning:
        score = entry.get("engagement", 0)
        scored_entries.append(entry)

        # A/B グループ
        ab = entry.get("ab_group", "A") or "A"
        ab_scores[ab].append(score)

        # ジャンル
        genre = entry.get("genre", "unknown") or "unknown"
        genre_scores[genre].append(score)

        # 時間帯（JST）
        dt = _parse_dt(entry.get("posted_at", ""))
        if dt:
            jst_hour = (dt.hour + 9) % 24
            hour_scores[jst_hour].append(score)

        # オープナー（post_log の post_text 先頭20文字）
        post_id = entry.get("post_id", "")
        text    = text_by_id.get(post_id, "")
        if text:
            opener = text.strip()[:20]
            opener_scores[opener].append(score)

    # ソート済み上位/下位
    scored_entries.sort(key=lambda e: e.get("engagement", 0), reverse=True)
    top5    = scored_entries[:5]
    bottom5 = scored_entries[-5:] if len(scored_entries) >= 5 else []

    result = {
        "ab_groups":  {k: _avg(v) for k, v in ab_scores.items()},
        "genres":     {k: _avg(v) for k, v in
                       sorted(genre_scores.items(), key=lambda x: -_avg(x[1]))},
        "openers":    {k: _avg(v) for k, v in
                       sorted(opener_scores.items(), key=lambda x: -_avg(x[1]))[:20]},
        "time_bands": {str(k): _avg(v) for k, v in
                       sorted(hour_scores.items(), key=lambda x: -_avg(x[1]))},
        "top5":    [{"title": e.get("title", "")[:40], "genre": e.get("genre"),
                     "score": e.get("engagement", 0), "ab": e.get("ab_group")}
                   for e in top5],
        "bottom5": [{"title": e.get("title", "")[:40], "genre": e.get("genre"),
                     "score": e.get("engagement", 0), "ab": e.get("ab_group")}
                   for e in bottom5],
        "total_posts": len(scored_entries),
    }

    print(f"[ContentOptimizer] 分析完了: {result['total_posts']}件 "
          f"/ ジャンル{len(result['genres'])}種 / オープナー{len(result['openers'])}種")
    return result


# ---------------------------------------------------------------------------
# 2. 改善テンプレート生成
# ---------------------------------------------------------------------------

_FALLBACK_TEMPLATES = {
    "templates": [
        {
            "name":   "コスパ特化型",
            "opener": "コスパ最強を厳選したよ！💎",
            "style":  "お得感強調",
            "reason": "価格訴求は高CTRと相関が強い",
        },
        {
            "name":   "共感誘発型",
            "opener": "これ見て！ずかぽんも驚いたぽん✨",
            "style":  "共感系",
            "reason": "感情的なオープナーはリプライを誘発する",
        },
        {
            "name":   "データ訴求型",
            "opener": "楽天レビュー4.5以上だけ厳選だよ📊",
            "style":  "情報系",
            "reason": "具体的な数字は信頼性を高める",
        },
    ],
    "low_performers":  "データ不足のためフォールバックテンプレートを使用",
    "high_performers": "データ不足のためフォールバックテンプレートを使用",
}


def generate_improved_templates(analysis: dict) -> dict:
    """
    GPT-4o-mini にパフォーマンスデータを渡して改善テンプレートを生成する。
    失敗時はフォールバックを返す。
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key in ("", "xxxx"):
        print("[ContentOptimizer] OpenAI APIキー未設定 → フォールバック")
        return _FALLBACK_TEMPLATES.copy()

    if analysis.get("total_posts", 0) < 5:
        print("[ContentOptimizer] データ不足（5件未満）→ フォールバック")
        return _FALLBACK_TEMPLATES.copy()

    prompt_data = json.dumps(analysis, ensure_ascii=False, indent=2)

    # ─── llm_core path ────────────────────────────────────────────────────────
    _OPT_SYSTEM = (
        "あなたは楽天アフィリエイト投稿の改善AIです。\n"
        "パフォーマンスデータを見て、より良い投稿テンプレートを3種類生成してください。\n"
        "キャラクターはずかぽん（ゆるくてポップな虫眼鏡キャラ）です。\n"
        "以下のJSON形式で返してください：\n"
        '{"templates":[{"name":"テンプレート名","opener":"冒頭文（30文字以内）",'
        '"style":"お得感強調/共感系/情報系","reason":"このテンプレートが効果的な理由"}],'
        '"low_performers":"スコアが低かった投稿の共通点",'
        '"high_performers":"スコアが高かった投稿の共通点"}'
    )
    _core_prompt_o = (
        _OPT_SYSTEM
        + "\n\n以下の投稿パフォーマンスデータを分析して、改善テンプレートを提案してください：\n\n"
        + prompt_data
    )
    _core_result_o = _ask_llm_core(_core_prompt_o, "content_optimize")
    if _core_result_o is not None:
        try:
            result = json.loads(_core_result_o)
            for key in ("templates", "low_performers", "high_performers"):
                if key not in result:
                    result[key] = _FALLBACK_TEMPLATES[key]
            if not isinstance(result.get("templates"), list) or len(result["templates"]) == 0:
                result["templates"] = _FALLBACK_TEMPLATES["templates"]
            print(f"[ContentOptimizer] llm_core テンプレート生成完了: {len(result['templates'])}件")
            return result
        except json.JSONDecodeError:
            pass  # パース失敗時はOpenAIへフォールバック
    # ─── end llm_core path ────────────────────────────────────────────────────

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            temperature=0.5,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたは楽天アフィリエイト投稿の改善AIです。\n"
                        "パフォーマンスデータを見て、より良い投稿テンプレートを\n"
                        "3種類生成してください。\n"
                        "キャラクターはずかぽん（ゆるくてポップな虫眼鏡キャラ）です。\n"
                        "以下のJSON形式で返してください：\n"
                        "{\n"
                        '  "templates": [\n'
                        '    {\n'
                        '      "name": "テンプレート名",\n'
                        '      "opener": "冒頭文（30文字以内）",\n'
                        '      "style": "お得感強調/共感系/情報系",\n'
                        '      "reason": "このテンプレートが効果的な理由"\n'
                        "    }\n"
                        "  ],\n"
                        '  "low_performers": "スコアが低かった投稿の共通点",\n'
                        '  "high_performers": "スコアが高かった投稿の共通点"\n'
                        "}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "以下の投稿パフォーマンスデータを分析して、"
                        "改善テンプレートを提案してください：\n\n"
                        + prompt_data
                    ),
                },
            ],
        )
        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)

        # 必須キー補完
        for key in ("templates", "low_performers", "high_performers"):
            if key not in result:
                result[key] = _FALLBACK_TEMPLATES[key]

        # templatesが空・不正な場合フォールバック
        if not isinstance(result.get("templates"), list) or len(result["templates"]) == 0:
            result["templates"] = _FALLBACK_TEMPLATES["templates"]

        print(f"[ContentOptimizer] テンプレート生成完了: {len(result['templates'])}件")
        return result

    except json.JSONDecodeError as e:
        print(f"[ContentOptimizer] JSONパースエラー: {e} → フォールバック")
        return _FALLBACK_TEMPLATES.copy()
    except Exception as e:
        print(f"[ContentOptimizer] GPT-4o-miniエラー: {e} → フォールバック")
        return _FALLBACK_TEMPLATES.copy()


# ---------------------------------------------------------------------------
# 3. 改善適用 — post_generator.py の ACCOUNT_OPENERS を書き換える
# ---------------------------------------------------------------------------

# post_generator.py 内の mono_zukan_jp オープナーリストにマッチする正規表現
_OPENER_LIST_RE = re.compile(
    r'(ACCOUNT_OPENERS\s*:\s*dict\[.*?\]\s*=\s*\{\s*"mono_zukan_jp"\s*:\s*\[)'
    r'(.*?)'
    r'(\s*\],\s*\})',
    re.DOTALL,
)


def _read_current_openers() -> list[str]:
    """post_generator.py から現在の ACCOUNT_OPENERS["mono_zukan_jp"] を読み取る。"""
    if not os.path.exists(_POST_GEN_PATH):
        return []
    with open(_POST_GEN_PATH, 'r', encoding='utf-8') as f:
        src = f.read()

    m = _OPENER_LIST_RE.search(src)
    if not m:
        return []

    list_body = m.group(2)
    # 文字列リテラルを抽出
    openers = re.findall(r'"([^"]+)"', list_body)
    return openers


def _write_new_openers(new_openers: list[str]) -> bool:
    """
    post_generator.py の ACCOUNT_OPENERS["mono_zukan_jp"] に new_openers を追記する。
    既存リストは保持したまま最大3件追加。重複は追加しない。
    Returns True on success.
    """
    if not os.path.exists(_POST_GEN_PATH):
        print("[ContentOptimizer] post_generator.py が見つからない → スキップ")
        return False

    with open(_POST_GEN_PATH, 'r', encoding='utf-8') as f:
        src = f.read()

    m = _OPENER_LIST_RE.search(src)
    if not m:
        print("[ContentOptimizer] ACCOUNT_OPENERS パターンが見つからない → スキップ")
        return False

    existing = re.findall(r'"([^"]+)"', m.group(2))

    # 重複を排除して追加するオープナーを選択（最大3件）
    to_add: list[str] = []
    for op in new_openers:
        op_clean = op.strip()
        if op_clean and op_clean not in existing and len(to_add) < 3:
            to_add.append(op_clean)

    if not to_add:
        print("[ContentOptimizer] 追加するオープナーなし（全て重複）")
        return True  # 正常終了

    # 既存リストのインデント形式を揃えて再構築
    all_openers = existing + to_add
    indent      = "        "  # 8スペース
    items_str   = ",\n".join(f'{indent}"{op}"' for op in all_openers)
    new_body    = f"\n{items_str},\n    "

    new_src = src[: m.start(2)] + new_body + src[m.end(2):]

    with open(_POST_GEN_PATH, 'w', encoding='utf-8') as f:
        f.write(new_src)

    print(f"[ContentOptimizer] post_generator.py 更新: {len(to_add)}件追加 → "
          f"計{len(all_openers)}件")
    return True


def _save_improvement_history(analysis: dict, result: dict, added: list[str]) -> None:
    """data/content_improvements.json に改善履歴を追記する。"""
    history: list[dict] = _load_json(_IMPROVEMENTS_PATH, default=[])
    entry = {
        "date":         datetime.now(timezone.utc + timedelta(hours=9)).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "added_openers": added,
        "analysis": {
            "total_posts":  analysis.get("total_posts", 0),
            "top_genre":    next(iter(analysis.get("genres", {})), None),
            "ab_groups":    analysis.get("ab_groups", {}),
        },
        "gpt_result": {
            "high_performers": result.get("high_performers", ""),
            "low_performers":  result.get("low_performers", ""),
            "templates":       result.get("templates", []),
        },
    }
    history.append(entry)
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_IMPROVEMENTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[ContentOptimizer] 改善履歴保存: {_IMPROVEMENTS_PATH}")


def apply_improvements(analysis: dict, result: dict) -> list[str]:
    """
    GPT-4o-miniが生成したテンプレートのopenerをpost_generator.pyに追記する。
    実際に追加されたopenerのリストを返す。
    """
    new_openers = [t.get("opener", "") for t in result.get("templates", [])]
    new_openers = [o.strip() for o in new_openers if o.strip()]

    # 書き込み前の状態を記録
    before = _read_current_openers()

    success = _write_new_openers(new_openers)

    if not success:
        return []

    after  = _read_current_openers()
    added  = [o for o in after if o not in before]

    _save_improvement_history(analysis, result, added)
    return added


# ---------------------------------------------------------------------------
# 4. Discord通知
# ---------------------------------------------------------------------------

def _send_discord_report(analysis: dict, result: dict, added_openers: list[str]) -> None:
    """改善完了を WEBHOOK_REPORT に Embed 送信する。"""
    if not _WEBHOOK_URL:
        print("[ContentOptimizer] DISCORD_WEBHOOK_REPORT 未設定 → スキップ")
        return

    date_str = datetime.now(timezone.utc + timedelta(hours=9)).strftime("%Y-%m-%d")

    opener_lines = "\n".join(f"・{o}" for o in added_openers) if added_openers else "追加なし（全て重複）"

    # ジャンル上位3件
    genres = analysis.get("genres", {})
    genre_lines = "\n".join(
        f"・{g}: avg {s}" for g, s in list(genres.items())[:3]
    ) or "データなし"

    # A/B グループ
    ab = analysis.get("ab_groups", {})
    ab_str = "  /  ".join(f"グループ{k}: {v}" for k, v in ab.items()) or "データなし"

    # 時間帯上位2件
    time_bands = analysis.get("time_bands", {})
    time_lines = "\n".join(
        f"・{h}時: avg {s}" for h, s in list(time_bands.items())[:2]
    ) or "データなし"

    embed = {
        "title":     f"🔄 投稿文自動改善完了 - {date_str}",
        "color":     0x9B59B6,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "content_optimizer.py by ずかぽんBot"},
        "fields": [
            {
                "name":   "✅ 追加されたオープナー",
                "value":  opener_lines,
                "inline": False,
            },
            {
                "name":   "📈 高パフォーマンスの共通点",
                "value":  result.get("high_performers", "—")[:300],
                "inline": False,
            },
            {
                "name":   "📉 低パフォーマンスの共通点",
                "value":  result.get("low_performers", "—")[:300],
                "inline": False,
            },
            {
                "name":   "🎯 ジャンル別スコア（上位3）",
                "value":  genre_lines,
                "inline": True,
            },
            {
                "name":   "🔬 A/B グループ比較",
                "value":  ab_str,
                "inline": True,
            },
            {
                "name":   "⏰ 高エンゲージメント時間帯 (JST)",
                "value":  time_lines,
                "inline": False,
            },
            {
                "name":   "📊 分析対象",
                "value":  f"{analysis.get('total_posts', 0)}件",
                "inline": True,
            },
        ],
    }

    try:
        resp = requests.post(_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        resp.raise_for_status()
        print(f"[ContentOptimizer] Discord送信完了 (status={resp.status_code})")
    except requests.HTTPError as e:
        print(f"[ContentOptimizer] Discord HTTPエラー: {e.response.status_code}")
    except Exception as e:
        print(f"[ContentOptimizer] Discord送信エラー: {e}")


# ---------------------------------------------------------------------------
# 5. メイン関数
# ---------------------------------------------------------------------------

def run_content_optimizer() -> None:
    """
    投稿文自動改善ループを実行する。
    main.py から月曜 9時JST のみ呼び出す。
    """
    print("\n--- ContentOptimizer: 投稿文自動改善ループ開始 ---")

    # Step 1: パフォーマンス分析
    print("[ContentOptimizer] Step1: パフォーマンス分析")
    analysis = analyze_post_performance()

    # Step 2: 改善テンプレート生成
    print("[ContentOptimizer] Step2: テンプレート生成")
    result = generate_improved_templates(analysis)

    # Step 3: 改善適用
    print("[ContentOptimizer] Step3: post_generator.py に適用")
    added = apply_improvements(analysis, result)

    # Step 4: Discord通知
    _send_discord_report(analysis, result, added)

    print(f"[ContentOptimizer] 完了 — 追加オープナー: {len(added)}件")


if __name__ == "__main__":
    run_content_optimizer()
