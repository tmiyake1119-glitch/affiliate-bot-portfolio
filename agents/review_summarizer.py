import os
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def summarize_reviews(review_texts: list[str]) -> str:
    """Use Claude to produce a 60-80 char Japanese summary of the given reviews.
    Returns empty string if no API key or no review texts."""
    if not review_texts:
        return ""
    if not (ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-")):
        return ""

    import anthropic

    joined = "\n".join(f"- {t}" for t in review_texts)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                "以下の複数のレビューを読んで、60〜80文字の自然な日本語でまとめてください。"
                "一人称は使わず、商品の特徴と評価をまとめた文にしてください。"
                "まとめの文章のみを出力し、前置きや説明は不要です。\n\n"
                f"{joined}"
            ),
        }],
    )
    return response.content[0].text.strip()
