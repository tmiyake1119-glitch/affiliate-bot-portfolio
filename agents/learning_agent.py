# shared_knowledge: /home/shared_knowledge/ も参照すること
"""
learning_agent.py — 投稿データからの自動学習・knowledge ファイル更新

週次（月曜 3:00 JST）で cron から実行。
knowledge/ ディレクトリの以下ファイルを上書き更新する:
  - genre_trends.md  : ジャンル別成功傾向
  - hooks.md         : 勝ちフック・CTA パターン
  - anti_patterns.md : 避けるべき負けパターン

cron 設定例 (VPS):
  0 3 * * 1 cd /home/affiliate_bot && python3 agents/learning_agent.py >> logs/learning_agent.log 2>&1
"""

import logging
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from database import get_all_learning_data, get_db

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger("affiliate_bot.learning_agent")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
KNOWLEDGE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'knowledge')
MAX_FILE_CHARS    = 800

# データチェック閾値
MIN_AB_RESULTS    = 50
MIN_VIEWS_RECORDS = 10


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_ab_result_count() -> int:
    conn = get_db()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM ab_test_results").fetchone()
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error("[LearningAgent] ab_test_results カウントエラー: %s", e)
        return 0
    finally:
        conn.close()


def _get_views_positive_count() -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM learning_data WHERE views > 0"
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error("[LearningAgent] learning_data views カウントエラー: %s", e)
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Knowledge file helpers
# ---------------------------------------------------------------------------

def _write_knowledge(filename: str, content: str) -> None:
    """knowledge/ に書き出す。800文字を超える場合は末尾を切る。"""
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    if len(content) > MAX_FILE_CHARS:
        content = content[:MAX_FILE_CHARS]
    path = os.path.join(KNOWLEDGE_DIR, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.info("[LearningAgent] %s 更新完了 (%d文字)", filename, len(content))
    print(f"[LearningAgent] {filename} 更新完了 ({len(content)}文字)")


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def _analyze_with_claude(posts: list[dict], instruction: str) -> str:
    """Claude Haiku に投稿データを渡して分析させる。エラー時は空文字を返す。"""
    if not ANTHROPIC_API_KEY:
        logger.warning("[LearningAgent] ANTHROPIC_API_KEY 未設定 → Claude 分析スキップ")
        return ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # 最大 20 件のサマリーを生成（post_text なければ title で代替）
        post_lines = []
        for p in posts[:20]:
            title     = (p.get("title") or "")[:50]
            genre     = p.get("genre", "")
            score     = p.get("engagement", 0)
            views     = p.get("views", 0)
            # post_text カラムは learning_data に存在しないため title を使用
            post_lines.append(
                f"- [{genre}] views={views} score={score}: {title}"
            )

        posts_text = "\n".join(post_lines)
        prompt     = f"{instruction}\n\n【投稿データ】\n{posts_text}"

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip()
        logger.info("[LearningAgent] Claude 分析完了 (%d文字)", len(result))
        return result

    except Exception as e:
        logger.error("[LearningAgent] Claude API エラー: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_learning_agent() -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("[LearningAgent] 開始: %s UTC", now_str)
    print(f"[LearningAgent] 開始 {now_str} UTC")

    # ── データチェック① : ab_test_results 件数 ──────────────────────
    ab_count = _get_ab_result_count()
    if ab_count < MIN_AB_RESULTS:
        msg = (
            f"[SKIP] データ不足のためlearning_agentをスキップ"
            f"（現在{ab_count}件 / 必要{MIN_AB_RESULTS}件）"
        )
        logger.info(msg)
        print(msg)
        return

    # ── データチェック② : learning_data views>0 件数 ─────────────────
    views_count = _get_views_positive_count()
    if views_count < MIN_VIEWS_RECORDS:
        msg = (
            f"[SKIP] データ不足のためlearning_agentをスキップ"
            f"（views>0が現在{views_count}件 / 必要{MIN_VIEWS_RECORDS}件）"
        )
        logger.info(msg)
        print(msg)
        return

    logger.info(
        "[LearningAgent] データチェック通過: ab_results=%d views_positive=%d",
        ab_count, views_count,
    )

    # ── 全 learning_data 取得・views>0 に絞る ─────────────────────────
    all_data   = get_all_learning_data()
    with_views = [d for d in all_data if d.get("views", 0) > 0]

    if not with_views:
        logger.warning("[LearningAgent] views>0 のレコードが取得できなかった → スキップ")
        return

    sorted_data = sorted(with_views, key=lambda x: x.get("views", 0), reverse=True)
    total       = len(sorted_data)
    top_n       = max(1, total // 5)   # 上位 20%
    bot_n       = max(1, total // 5)   # 下位 20%

    winners = sorted_data[:top_n]
    losers  = sorted_data[-bot_n:]

    logger.info(
        "[LearningAgent] 勝ち=%d件 負け=%d件 全体=%d件",
        len(winners), len(losers), total,
    )
    print(f"[LearningAgent] 勝ち={len(winners)}件 負け={len(losers)}件 全体={total}件")

    # ── 勝ち投稿分析 → genre_trends.md / hooks.md ─────────────────────
    win_instruction = (
        "以下の投稿データから成功パターンを箇条書きで分析してください。"
        "フック・CTA・内容の特徴を中心に。800文字以内で。"
    )
    win_analysis = _analyze_with_claude(winners, win_instruction)

    if win_analysis:
        today = datetime.now().strftime("%Y-%m-%d")

        # genre_trends.md: ジャンル別トップ商品 + AI 分析
        genre_map: dict[str, list[dict]] = {}
        for p in winners:
            genre_map.setdefault(p.get("genre", "unknown"), []).append(p)

        genre_lines = [f"# ジャンル別トレンド (更新: {today})\n"]
        for genre, posts in sorted(genre_map.items()):
            top3 = sorted(posts, key=lambda x: x.get("views", 0), reverse=True)[:3]
            genre_lines.append(f"\n## {genre} ({len(posts)}件)\n")
            for p in top3:
                genre_lines.append(
                    f"- {(p.get('title') or '')[:45]} (views={p.get('views', 0)})\n"
                )
        genre_lines.append(f"\n## 分析\n{win_analysis}")
        _write_knowledge("genre_trends.md", "".join(genre_lines))

        # hooks.md: フック・CTA に特化した分析
        hooks_content = (
            f"# 成功フック・CTA分析 (更新: {today})\n\n"
            f"{win_analysis}"
        )
        _write_knowledge("hooks.md", hooks_content)
    else:
        logger.warning("[LearningAgent] 勝ち投稿分析: Claude から結果なし")

    # ── 負け投稿分析 → anti_patterns.md ──────────────────────────────
    lose_instruction = (
        "以下は低パフォーマンス（views 数が少ない）投稿のデータです。"
        "避けるべきパターン・失敗の共通点を箇条書きで分析してください。800文字以内で。"
    )
    lose_analysis = _analyze_with_claude(losers, lose_instruction)

    if lose_analysis:
        today = datetime.now().strftime("%Y-%m-%d")
        anti_content = (
            f"# アンチパターン分析 (更新: {today})\n\n"
            f"{lose_analysis}"
        )
        _write_knowledge("anti_patterns.md", anti_content)
    else:
        logger.warning("[LearningAgent] 負け投稿分析: Claude から結果なし")

    logger.info("[LearningAgent] 完了 %s UTC", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    print("[LearningAgent] 完了")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    run_learning_agent()
