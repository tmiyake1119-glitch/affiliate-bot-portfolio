"""
post_generator の llm_core 統合 dry-run テスト。
本番投稿は一切行わない。_openai_body（Pattern-A）を直接2回呼んでキャッシュを検証する。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'agents'))

# .env 読み込み（affiliate_bot ルート）
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'), override=True)

print(f"[config] USE_LLM_CORE={os.getenv('USE_LLM_CORE')}")
print(f"[config] OPENAI_API_KEY={'set' if os.getenv('OPENAI_API_KEY') else 'not set'}\n")

# post_generator の内部関数を直接呼ぶ（dry-run）
from post_generator import _openai_body

SAMPLE_PRODUCT = {
    "title":               "山崎 12年 シングルモルトウイスキー 700ml",
    "price_str":           "¥12,800",
    "price_raw":           12800,
    "genre":               "お酒",
    "discount_rate":       0.15,
    "price_avg_30d":       15000,
    "review_average":      4.6,
    "review_count":        320,
    "product_description": "サントリーが誇る国産シングルモルト。華やかな香りとまろやかな口当たり。",
}

def run(label: str) -> None:
    print(f"\n{'='*50}")
    print(f"[RUN] {label}")
    print('='*50)
    result = _openai_body(SAMPLE_PRODUCT)
    print("\n[RESULT]")
    print(result)
    print('='*50)

if __name__ == "__main__":
    run("1回目（初回 / キャッシュなし想定）")
    run("2回目（同一プロンプト / キャッシュあり想定）")
