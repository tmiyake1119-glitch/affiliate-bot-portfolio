"""
llm_core integration bridge for affiliate_bot.

Toggle USE_LLM_CORE = False to fall back to all legacy OpenAI/Claude calls.
"""

import logging
import os

logger = logging.getLogger(__name__)

# ── 切替フラグ ─────────────────────────────────────────────────────────────────
# 開発時のみ ON にすること。本番では必ず OFF（デフォルト）。
# 有効化: .env または環境変数に USE_LLM_CORE=true を設定する。
USE_LLM_CORE: bool = os.getenv("USE_LLM_CORE", "false") == "true"

# 直前のask()呼び出しがキャッシュヒットだったか（呼び出し元がコスト集計に使用）
last_cached: bool = False


def ask(prompt: str, task: str) -> str | None:
    """Send prompt to llm_core orchestrator.

    Returns the response text on success, or None on failure / flag off.
    Callers must fall back to their legacy LLM path when None is returned.
    """
    global last_cached
    last_cached = False

    if not USE_LLM_CORE:
        return None

    try:
        from llm_core.orchestrator import chat
        result = chat(
            prompt=prompt,
            session_id="affiliate-bot",
            project_id="affiliate_bot",
            metadata={"source": "affiliate", "task": task},
        )
        last_cached = result.get("cached", False)
        _log(result, task)
        return result.get("response", "").strip() or None
    except Exception as exc:
        logger.warning("[llm_core_bridge] task=%s llm_core失敗 → レガシーへ: %s", task, exc)
        return None


def _log(result: dict, task: str) -> None:
    judgment = result.get("judgment") or {}
    print(
        f"[llm_core] task={task}"
        f" intent={result.get('intent', '-')}"
        f" model={result.get('model_used', '-')}"
        f" cached={result.get('cached', False)}"
        f" memory={result.get('memory_injected', 0)}件"
        f" judgment_type={judgment.get('type', '-')}"
        f" importance={judgment.get('importance', 0.0):.2f}"
        f" consistency={result.get('consistency_score', 1.0):.2f}"
    )
