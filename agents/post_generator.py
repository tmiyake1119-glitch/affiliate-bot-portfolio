import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from ab_tester import generate_post_b
from dotenv import load_dotenv
import llm_core_bridge as _llm_core_bridge
from llm_core_bridge import ask as _ask_llm_core
from formatter import format_products
from product_selector import select_top_products
from sale_detector import get_sale_banner
from seasonal_content import get_seasonal_hashtags
from trend_agent import get_trending_products

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
load_dotenv(dotenv_path=_env_path, override=False)
load_dotenv(override=False)  # fallback: カレントディレクトリの .env
OPENAI_MODEL    = "gpt-4o"   # gpt-5 / gpt-4o など環境に合わせて変更
DAILY_API_LIMIT = 50         # 1日あたりの最大OpenAI呼び出し回数

API_USAGE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'api_usage.json')
KNOWLEDGE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'knowledge')


def _load_knowledge_context(genre: str) -> str:
    """knowledge/ から genre_trends.md と hooks.md を読み込み、合計 1500 文字以内で返す。

    ファイルが空または存在しない場合は空文字を返す（プロンプトへの追加をスキップ）。
    """
    def _read(filename: str) -> str:
        path = os.path.join(KNOWLEDGE_DIR, filename)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return ""

    genre_text = _read("genre_trends.md")
    hooks_text = _read("hooks.md")

    # 両方空なら何も追加しない
    if not genre_text and not hooks_text:
        return ""

    parts: list[str] = []
    if genre_text:
        parts.append(f"[ジャンル傾向]\n{genre_text}")
    if hooks_text:
        parts.append(f"[フック・CTA]\n{hooks_text}")

    combined = "\n\n".join(parts)
    # 上限 1500 文字
    if len(combined) > 1500:
        combined = combined[:1500]
    return combined


def _get_today_generation_count() -> int:
    """今日の総生成数（キャッシュヒット含む）を返す。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not os.path.exists(API_USAGE_PATH):
        return 0
    with open(API_USAGE_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    entry = data.get(today, {})
    if isinstance(entry, int):  # 旧フォーマット互換
        return entry
    return entry.get("generation", 0)


def _cleanup_old_usage(data: dict) -> dict:
    """7日より古いエントリを削除して返す。"""
    today = datetime.now(timezone.utc).date()
    new_data = {}
    for k, v in data.items():
        try:
            d = datetime.strptime(k, "%Y-%m-%d").date()
            if (today - d).days <= 7:
                new_data[k] = v
        except Exception:
            continue
    return new_data


def _increment_counts(is_llm_call: bool = True) -> tuple[int, int]:
    """総生成数+1し、is_llm_call=Trueの場合はLLM呼び出し数も+1。
    更新後の (generation_count, llm_call_count) を返す。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(API_USAGE_PATH), exist_ok=True)
    data: dict = {}
    if os.path.exists(API_USAGE_PATH):
        with open(API_USAGE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    entry = data.get(today, {})
    if isinstance(entry, int):  # 旧フォーマット互換
        entry = {"generation": entry, "llm_call": entry}
    entry["generation"] = entry.get("generation", 0) + 1
    if is_llm_call:
        entry["llm_call"] = entry.get("llm_call", 0) + 1
    data[today] = entry
    data = _cleanup_old_usage(data)
    with open(API_USAGE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return entry["generation"], entry["llm_call"]

GENRE_HASHTAGS: dict[str, str] = {
    "food":        "#楽天市場 #買って良かった #おすすめグルメ #食品 #グルメ #お取り寄せ #美味しいもの #食べ物",
    "electronics": "#楽天市場 #買って良かった #おすすめ家電 #家電 #ガジェット #テック #便利家電",
    "health":      "#楽天市場 #買って良かった #健康グッズ #健康 #ヘルスケア #ダイエット #美容健康",
    "daily_goods": "#楽天市場 #買って良かった #おすすめ日用品 #生活用品 #暮らし #便利グッズ #生活雑貨",
    "kitchen":     "#楽天市場 #買って良かった #キッチン用品 #料理 #キッチン #調理器具 #台所",
    "pet":         "#楽天市場 #買って良かった #ペット用品 #犬 #猫 #ペットグッズ #愛犬 #愛猫",
    "outdoor":     "#楽天市場 #買って良かった #アウトドア #キャンプ #アウトドアグッズ #キャンプ用品",
    "cosmetics":   "#楽天市場 #買って良かった #コスメ #美容 #スキンケア #おすすめコスメ #美容グッズ",
    "alcohol":     "#楽天市場 #買って良かった #お酒 #ウイスキー #日本酒 #ワイン #晩酌",
    "wine":        "#楽天市場 #買って良かった #ワイン #赤ワイン #白ワイン #ワイン好き #お酒",
    "furusato":    "#楽天市場 #買って良かった #ふるさと納税 #お取り寄せ #産地直送 #ふるさと",
    "contact":     "#コンタクトレンズ #カラコン #アイケア #目のケア #楽天市場 #買って良かった",
    "rare":        "#楽天市場 #買って良かった #限定品 #レア #再入荷 #入手困難 #プレミアム",
}

CTA = "👇 詳細・購入はこちら"

ACCOUNT_OPENERS: dict[str, list[str]] = {
    "mono_zukan_jp": [
        "ずかぽんが発見したよ！🔍",
        "これ買って良かったぽん！📖",
        "コスパ最強を見つけたよ！✨",
        "図鑑に載せたくなる一品だよ！🔍",
        "ずかぽん的に神アイテムだぽん！⭐",
        "楽天で掘り出したよ！🎉",
    ],
    "osakezukan_jp": [
        "今日の晩酌に合いそうな一本を見つけた。",
        "これはつまみと合わせたいやつだ。",
        "落ち着いた夜に開けたいぽん。",
        "大人の週末にぴったりかも。",
        "晩酌のレベルが上がりそうな一品。",
        "ゆっくり味わいたい系だぽん。",
    ],
    "hobbyzukan_jp": [
        "これ絶対ハマるやつだぽん！",
        "コレクション欲が止まらないぽん！",
        "ずかぽんも欲しくなってきたぽん！🎮",
        "ホビー好きなら見逃せないぽん！",
        "これは推せるやつだぽん！⭐",
        "ガチ勢もにわか勢も注目だぽん！",
    ],
}

_BUDGET_OPENERS  = ["💰 これはお得すぎるぽん！", "コスパ最高を発見だよ！💰", "ボクが選んだコスパ抜群アイテムだよ！🎉"]
_MID_OPENERS     = ["最近ボクが気に入ってるやつだよ！🙌", "これ、ほんとに良かったぽん✨", "ずかぽん的におすすめしたい一品だね！✨"]
_PREMIUM_OPENERS = ["図鑑に載せたくなる上質アイテムだよ！👑", "ボクが選ぶプレミアムはこれだぽん！👑", "特別な一品を発見したよ！👑"]
_TIPS_OPENERS    = ["📖 ずかぽんが調べたよ！", "知ってた？これめちゃくちゃ便利だぽん📚", "データで選ぶならこれだよ！📊"]

# post_tone → オープナーリスト のマッピング（共感系は実行時に解決）
_TONE_OPENER_MAP: dict[str, list[str]] = {
    "お得感強調": _BUDGET_OPENERS,
    "情報系":     _TIPS_OPENERS,
}


def _load_today_tone() -> str | None:
    """daily_strategy.json から今日の post_tone を返す。なければ None。"""
    try:
        from strategy_agent import load_today_strategy
        strategy = load_today_strategy()
        if strategy:
            return strategy.get("post_tone")
    except Exception:
        pass
    return None


_ACCOUNT_FOOTERS: dict[str, str] = {
    "osakezukan_jp": "🍶 お酒図鑑より",
    "hobbyzukan_jp": "🎮 ホビー図鑑より",
}


def _hashtags(genre: str, account_name: str | None = None) -> str:
    base = GENRE_HASHTAGS.get(genre, "#楽天市場 #買って良かった")
    seasonal = " ".join(get_seasonal_hashtags()[:2])
    tags = f"{base} {seasonal}".strip() if seasonal else base
    footer = _ACCOUNT_FOOTERS.get(account_name or "", "📖 ずかぽんの図鑑より")
    return f"{footer}\n\n{tags}"


def _inject_sale_banner(text: str) -> str:
    banner = get_sale_banner()
    return banner + text if banner else text


def _inject_amazon_link(text: str, amazon_url: str) -> str:
    if not amazon_url:
        return text
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("#"):
            lines.insert(i, f"🛒 Amazon: {amazon_url}")
            break
    return "\n".join(lines)


def _rank_badge(rank: int) -> str:
    if rank == 1:  return "🥇 楽天1位！\n\n"
    if rank == 2:  return "🥈 楽天2位！\n\n"
    if rank == 3:  return "🥉 楽天3位！\n\n"
    if 4 <= rank <= 10: return "🏅 楽天トップ10！\n\n"
    return ""


def _stars(rating: float) -> str:
    """float → ★表示（4.5 → ★★★★½、4.0 → ★★★★☆）"""
    full  = int(rating)
    half  = 1 if (rating - full) >= 0.3 else 0
    empty = 5 - full - half
    return "★" * full + ("½" if half else "") + "☆" * empty


def _review_badge(product: dict) -> str:
    """レビュー評価と送料無料を1行にまとめて返す。表示すべき内容がなければ空文字。"""
    parts: list[str] = []
    rating = product.get("review_average", 0.0) or 0.0
    count  = product.get("review_count", 0) or 0
    if rating >= 4.0 and count > 0:
        parts.append(f"{_stars(rating)}（{count}件）")
    if product.get("free_shipping"):
        parts.append("[送料無料]")
    return "  ".join(parts)


def _get_opener(raw_price: int, account_name: str | None) -> str:
    # 今日の post_tone が設定されていればそちらを優先
    tone = _load_today_tone()
    if tone:
        if tone == "共感系":
            pool = ACCOUNT_OPENERS.get(account_name or "mono_zukan_jp", _MID_OPENERS)
        else:
            pool = _TONE_OPENER_MAP.get(tone)
        if pool:
            return random.choice(pool)

    # tone 未設定時は従来ロジック
    if account_name and account_name in ACCOUNT_OPENERS:
        return random.choice(ACCOUNT_OPENERS[account_name])
    if raw_price < 5000:
        return random.choice(_BUDGET_OPENERS)
    if raw_price <= 20000:
        return random.choice(_MID_OPENERS)
    return random.choice(_PREMIUM_OPENERS)


def _description_block(product: dict) -> str:
    desc = product.get("product_description", "")
    if not desc:
        return ""
    emoji = "🥃" if product.get("genre") in ("alcohol", "wine") else "📝"
    return f"{emoji} {desc}\n\n"


def _build_badge_block(product: dict) -> str:
    parts = []
    if product.get("time_limited"):
        parts.append("⚡ 本日限り！")
    if product.get("rating_str"):
        parts.append(product["rating_str"])
    if product.get("free_shipping"):
        parts.append("🚚 送料無料")
    return (" / ".join(parts) + "\n\n") if parts else ""


_PATTERN_B_STYLES = [
    "体験談型",
    "用途提案型",
    "比較型",
    "疑問型",
    "さらっと紹介型",
]

# スタイルごとの書き方ガイド（冒頭フォーマット参考）
_PATTERN_B_STYLE_GUIDE = {
    "体験談型":      "「{商品名}を使ってみたら〜だった」形式の一人称体験談",
    "用途提案型":    "「〜な人に向いてるかも」形式の具体的な用途提案",
    "比較型":        "「前は〜してたけど、これにしてから〜」形式の前後比較",
    "疑問型":        "「〜って知ってた？」で始まる事実紹介",
    "さらっと紹介型": "「最近これ使ってる」などの短く自然な紹介",
}

# 禁止表現（誇張・押し売り系）
_PATTERN_B_FORBIDDEN = (
    "コスパ最強", "コスパ抜群", "コスパ最高", "地味に便利",
    "これ普通にアリ", "神アイテム", "最高", "すごい",
    "ぜひ購入を", "今すぐチェック", "絶品", "贅沢", "新鮮",
)

# 自然なCTA例（最後の1行に使用するフォールバック用）
_PATTERN_B_CTA_EXAMPLES = [
    "こういうの探してた人はありかも",
    "気になる人はチェックしてみて",
    "用途ハマる人にはかなり便利そうだぽん",
    "ちょっと気になってる人は見てみて",
    "使い道ある人には向いてるかも",
]

_PATTERN_B_SYSTEM_PROMPT = """\
あなたは「ずかぽん」というキャラクターのSNS投稿ライターです。

【キャラクター設定: ずかぽん（mono_zukan_jp）】
・ずかぽんは図鑑から飛び出してきた案内キャラ。商品を発見して紹介するのが大好き。
・一人称は「ボク」または「ずかぽん」を使う
・語尾「〜だぽん」「〜ぽん」「〜だよ」「〜だね」をランダムに混ぜる（必ず最低1文にずかぽん語尾を入れること）
・テンションは穏やか・自然体。「！！」の連発や過度な盛り上がりはNG
・キャラ優先で商品説明が弱くなるのはNG。情報>キャラの優先順位を守る

【必須要素 ①: ターゲット明示（冒頭または序盤に1行）】
・投稿の冒頭または序盤に「誰向けか」を具体的なシーンや状況で書くこと
・良い例: 「一人暮らしでキッチン狭い人には」「在宅ワーク多い人に」「収納に困ってる人向け」
・NG例: 「便利な人に」（抽象的すぎる）

【必須要素 ②: 1投稿1メッセージ】
・伝えるポイントは1つだけに絞る
・「便利・おしゃれ・安い・コンパクト」を全部言うのはNG
・最も刺さるポイントを1つ選んで深く伝える

【必須要素 ③: 自然なCTA（最後の1行）】
・最後の1行だけ自然なCTAを入れること
・良い例: 「こういうの探してた人はありかも」「気になる人はチェックしてみて」「用途ハマる人にはかなり便利そうだぽん」
・NG例: 「ぜひ購入を」「今すぐチェック」（直接的すぎる）

【禁止表現】
コスパ最強・コスパ抜群・地味に便利・これ普通にアリ・神アイテム・最高・絶品・ぜひ購入を・今すぐチェック

【必須要素 ④: 価格の自然な挿入】
・価格「¥XX,XXX」を投稿の序盤か中盤に自然に入れること（最後にまとめて書かない）
・割引がある場合は「¥XX,XXX（通常より約XX%OFF）」の形で
・良い例: 「¥25,800でこれ、正直安いと思う」「¥3,980なのにこのクオリティ」
・悪い例: 「価格：¥25,800」（無機質な羅列はNG）

【文体ルール】
・体験・用途ベースで書く
・言い切らない表現を適度に使う（「〜かも」「〜な気がする」「〜してみては」）
・絵文字は2〜3個以内
・URLやハッシュタグは含めない（後で追加する）
・150文字以内
・商品名と価格を必ず含める

【出力例】
在宅ワーク多い人に向いてるかもぽん。

{商品名}、¥{価格}。
こういう使い方ができるよ！

気になる人はチェックしてみてね\
"""

_OSAKE_SYSTEM_PROMPT = """\
あなたは「ずかぽん」というキャラクターのSNS投稿ライターです（osakezukan_jp アカウント）。

【キャラクター設定: ずかぽん（お酒専門）】
・ずかぽんはお酒の図鑑から飛び出してきた案内キャラ。晩酌にぴったりな一本を発見して紹介するのが大好き。
・語尾「〜だぽん」は控えめに（全文の10%程度。ほぼ使わない）
・落ち着いた大人のトーン。静かな充実感を伝える
・晩酌・お酒・おつまみのシーンを想像させる文章
・過度な盛り上がりや感嘆符の連発はNG

【必須要素 ①: シーン明示（冒頭または序盤に1行）】
・どんな場面・気分に合うかを書くこと
・良い例: 「今日の晩酌に合いそう」「週末の夜にゆっくり楽しみたい一本」「おつまみと一緒に」

【必須要素 ②: 1投稿1メッセージ】
・香り・味・ペアリング・価格帯など、伝えるポイントは1つに絞る

【必須要素 ③: 自然なCTA（最後の1行）】
・良い例: 「気になる人は一度試してみては」「こういうの探してた人にはありかも」
・NG例: 「ぜひ購入を」「今すぐチェック」

【禁止表現】
コスパ最強・コスパ抜群・神アイテム・最高・絶品・ぜひ購入を・今すぐチェック

【必須要素 ④: 価格の自然な挿入】
・価格「¥XX,XXX」を投稿の序盤か中盤に自然に入れること（最後にまとめて書かない）
・割引がある場合は「¥XX,XXX（通常より約XX%OFF）」の形で
・良い例: 「¥3,800でこの一本、晩酌のレベルが上がりそう」
・悪い例: 「価格：¥3,800」（無機質な羅列はNG）

【文体ルール】
・落ち着いた語り口で書く
・絵文字は1〜2個以内（🥃🍷程度）
・URLやハッシュタグは含めない（後で追加する）
・150文字以内
・商品名と価格を必ず含める

【出力例】
週末の夜にゆっくり楽しみたい人向けかも。

{商品名}、¥{価格}。
おつまみと合わせると雰囲気出るよ。

気になる人は一度試してみては\
"""

_HOBBY_SYSTEM_PROMPT = """\
あなたは「ずかぽん」というキャラクターのSNS投稿ライターです（hobbyzukan_jp アカウント）。

【キャラクター設定: ずかぽん（ホビー専門）】
・ずかぽんはホビーの図鑑から飛び出してきた案内キャラ。コレクション欲をくすぐるアイテムを発見して紹介するのが大好き。
・語尾「〜だぽん」を積極的に使用する（全文の50%程度）
・テンション高め・ワクワク感を前面に出す
・コレクション欲・所有欲・ファン心理を刺激する表現を使う
・「！」は適度に使用OK（連発しすぎはNG）

【必須要素 ①: ターゲット明示（冒頭または序盤に1行）】
・どんなファン・コレクターに刺さるかを書くこと
・良い例: 「これ絶対ハマるやつだぽん」「コレクション勢は見逃せないぽん」

【必須要素 ②: 1投稿1メッセージ】
・限定感・レア度・再現度・遊び応えなど、1点に絞って熱く語る

【必須要素 ③: 自然なCTA（最後の1行）】
・良い例: 「気になったら早めにチェックだぽん」「コレクション欲止まらないぽん」
・NG例: 「ぜひ購入を」「今すぐチェック」

【禁止表現】
神アイテム・最高・コスパ最強・ぜひ購入を・今すぐチェック

【必須要素 ④: 価格の自然な挿入】
・価格「¥XX,XXX」を投稿の序盤か中盤に自然に入れること（最後にまとめて書かない）
・割引がある場合は「¥XX,XXX（通常より約XX%OFF）」の形で
・良い例: 「¥5,280でこれ、コレクション欲が止まらないぽん！」
・悪い例: 「価格：¥5,280」（無機質な羅列はNG）

【文体ルール】
・ワクワク・興奮・発見のトーンで書く
・絵文字は2〜4個以内（🎮🎲⭐など）
・URLやハッシュタグは含めない（後で追加する）
・150文字以内
・商品名と価格を必ず含める

【出力例】
フィギュア好きには刺さるやつだぽん！

{商品名}、¥{価格}。
コレクション欲が止まらないぽん！

気になったら早めにチェックだぽん\
"""

_ACCOUNT_SYSTEM_PROMPTS: dict[str, str] = {
    "osakezukan_jp": _OSAKE_SYSTEM_PROMPT,
    "hobbyzukan_jp": _HOBBY_SYSTEM_PROMPT,
}


def _openai_body_pattern_b(product: dict, account_name: str | None = None) -> str:
    """パターンB: ずかぽんキャラ・ターゲット明示・1メッセージ・自然CTAの投稿文を生成。

    account_name に応じてシステムプロンプトを切り替える:
      osakezukan_jp → 落ち着き・大人トーン
      hobbyzukan_jp → テンション高め・コレクター向け
      その他        → 通常のずかぽんプロンプト

    スタイルは _PATTERN_B_STYLES からランダムに選択する。
    max_tokens=160, temperature=0.75 で自然な文体を優先する。
    失敗時はテンプレートにフォールバック。
    """
    product_name  = product.get("title", "")
    price_str     = product.get("price_str", "")
    genre         = product.get("genre", "")
    discount_rate = product.get("discount_rate", 0.0)
    discount_pct  = int(round(discount_rate * 100))

    def _template_b() -> str:
        cta = random.choice(_PATTERN_B_CTA_EXAMPLES)
        return random.choice([
            f"{product_name}、最近使ってる。{price_str}。\n{cta}",
            f"{product_name}を使ってみたら思ったよりよかった。{price_str}。\n{cta}",
            f"これ、{product_name}。{price_str}で買えるぽん。\n{cta}",
        ])

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key.strip() in ("", "xxxx"):
        return _template_b()

    today_usage = _get_today_generation_count()
    if today_usage >= DAILY_API_LIMIT:
        return _template_b()

    style         = random.choice(_PATTERN_B_STYLES)
    style_guide   = _PATTERN_B_STYLE_GUIDE[style]
    discount_hint = f"割引情報: {discount_pct}%OFF（参考として伝えてよい）\n" if discount_pct > 0 else ""

    # itemCaption 由来の商品説明
    desc = product.get("product_description", "")
    desc_hint = f"商品特徴（itemCaption抜粋・特徴抽出に使ってよい）: {desc}\n" if desc else ""

    # レビュー情報（★4.0以上・件数あり の場合のみ補足として提示）
    review_avg = product.get("review_average", 0.0) or 0.0
    review_cnt = product.get("review_count",   0)   or 0
    review_hint = (
        f"レビュー評価: ★{review_avg}（{review_cnt:,}件） ※信頼性の補足として言及してよい\n"
        if review_avg >= 4.0 and review_cnt > 0 else ""
    )

    # アカウント別にシステムプロンプトを切り替える
    system_prompt = _ACCOUNT_SYSTEM_PROMPTS.get(account_name or "", _PATTERN_B_SYSTEM_PROMPT)

    # knowledge/ からジャンル傾向・フックパターンを読み込んでプロンプトに追加
    knowledge_ctx = _load_knowledge_context(genre)
    knowledge_block = (
        f"\n\n【過去の投稿分析から得られた成功パターン】\n{knowledge_ctx}\nこれを参考に投稿を作成してください。完全に模倣せず自然な文章で。"
        if knowledge_ctx else ""
    )

    # ─── llm_core path ────────────────────────────────────────────────────────
    _core_prompt_b = (
        system_prompt
        + f"\n\n【スタイル指定】\nスタイル: {style}\nフォーマット参考: {style_guide}"
        + knowledge_block
        + "\n\n---\n"
        + f"商品名: {product_name}\n価格: {price_str}\nジャンル: {genre}\n"
        + discount_hint + desc_hint + review_hint
        + (
            f"価格表記: 「{price_str}（通常より約{discount_pct}%OFF）」を序盤か中盤に自然に入れること\n"
            if discount_pct > 0 else
            f"価格表記: 「{price_str}」を序盤か中盤に自然に入れること\n"
        )
        + f"\nスタイル「{style}」で、ずかぽんらしい投稿本文を作成してください。"
          "\n（ターゲット明示・1メッセージ・自然なCTAを忘れずに）"
    )
    _core_result_b = _ask_llm_core(_core_prompt_b, "post_generate_pattern_b")
    if _core_result_b is not None:
        gen_count, llm_count = _increment_counts(is_llm_call=not _llm_core_bridge.last_cached)
        print(f"[post_generator] llm_core Pattern-B使用 account={account_name} style={style} [生成] 総生成数: {gen_count}/{DAILY_API_LIMIT} | LLM呼び出し: {llm_count}/{DAILY_API_LIMIT}")
        return _core_result_b
    # ─── end llm_core path ────────────────────────────────────────────────────

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=160,
            temperature=0.75,
            messages=[
                {
                    "role": "system",
                    "content": (
                        system_prompt
                        + f"\n\n【スタイル指定】\nスタイル: {style}\nフォーマット参考: {style_guide}"
                        + knowledge_block
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"商品名: {product_name}\n"
                        f"価格: {price_str}\n"
                        f"ジャンル: {genre}\n"
                        + discount_hint
                        + desc_hint
                        + review_hint
                        + (
                            f"価格表記: 「{price_str}（通常より約{discount_pct}%OFF）」を序盤か中盤に自然に入れること\n"
                            if discount_pct > 0 else
                            f"価格表記: 「{price_str}」を序盤か中盤に自然に入れること\n"
                        )
                        + f"\nスタイル「{style}」で、ずかぽんらしい投稿本文を作成してください。"
                        "\n（ターゲット明示・1メッセージ・自然なCTAを忘れずに）"
                    ),
                },
            ],
        )
        gen_count, llm_count = _increment_counts(is_llm_call=True)
        print(f"[post_generator] OpenAI Pattern-B使用 account={account_name} style={style} [生成] 総生成数: {gen_count}/{DAILY_API_LIMIT} | LLM呼び出し: {llm_count}/{DAILY_API_LIMIT}")
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[post_generator] Pattern-B OpenAIエラー → テンプレ使用: {e}")
        return _template_b()


def _openai_body(product: dict) -> str:
    """OpenAIで投稿本文を生成する。安全制御を全て内包。

    ④ APIキー未設定 / "xxxx" → テンプレ
    ① 日次上限超過 → テンプレ
    ② max_tokens=80, temperature=0.3 を強制
    ③ 例外発生 → テンプレ
    ⑤ 使用状況をログ出力
    """
    product_name  = product.get("title", "")
    price_str     = product.get("price_str", "")
    discount_rate = product.get("discount_rate", 0.0)
    discount_pct  = int(round(discount_rate * 100))

    price_avg_30d    = product.get("price_avg_30d")
    price_raw        = product.get("price_raw") or product.get("price", 0)
    discount_amount  = (int(price_avg_30d) - int(price_raw)) if price_avg_30d and price_raw else 0

    def _template() -> str:
        if price_avg_30d and discount_amount > 0 and discount_pct > 0:
            price_line = f"今 {price_str}（-{discount_amount:,}円 / {discount_pct}%OFF）"
        elif discount_pct > 0:
            price_line = f"今 {price_str}（{discount_pct}%OFF）"
        else:
            price_line = f"今 {price_str}"
        return (
            f"これちょっと安い\n\n"
            f"{product_name}\n"
            f"{price_line}\n\n"
            f"普通にあり"
        )

    # ④ APIキー未設定チェック（毎回 os.getenv で読み直す）
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key.strip() in ("", "xxxx"):
        print("[post_generator] OpenAI: スキップ（APIキー未設定） → テンプレ使用")
        return _template()

    # ① 日次上限チェック
    today_usage = _get_today_generation_count()
    if today_usage >= DAILY_API_LIMIT:
        print(f"[post_generator] OpenAI: 上限到達（{today_usage}/{DAILY_API_LIMIT}回） → テンプレ使用")
        return _template()

    # ③ OpenAI呼び出し（try-except）
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        discount_hint = f"割引率: {discount_pct}%OFF\n" if discount_pct > 0 else ""

        # reviewAverage / reviewCount（参考情報）
        review_avg   = product.get("review_average", 0.0) or 0.0
        review_cnt   = product.get("review_count",   0)   or 0
        review_ref   = f"レビュー: ★{review_avg}（{review_cnt:,}件）\n" if review_avg >= 4.0 and review_cnt > 0 else ""

        # itemCaption 由来の商品説明（参考情報）
        desc = product.get("product_description", "")
        desc_ref = f"商品特徴（参考）: {desc}\n" if desc else ""

        # knowledge/ からジャンル傾向・フックパターンを読み込む
        knowledge_ctx_a = _load_knowledge_context(product.get("genre", ""))
        knowledge_ref = (
            f"参考（過去の成功パターン）: {knowledge_ctx_a[:300]}\n"
            if knowledge_ctx_a else ""
        )

        # ─── llm_core path ────────────────────────────────────────────────────
        _PATTERN_A_SYSTEM = (
            "以下のフォーマットに数値を埋めて出力しなさい。\n"
            "フォーマット以外の文字を一切追加してはならない。\n\n"
            "【禁止事項】\n"
            "・感情表現禁止（例: 美味しそう、最高、すごい、ぴったり）\n"
            "・主観禁止（例: おすすめ、良い、気になる）\n"
            "・抽象表現禁止（例: 贅沢、人気、新鮮、絶品）\n"
            "・説明文・キャッチコピー禁止\n"
            "・URLやハッシュタグ禁止\n\n"
            "【出力フォーマット（厳守）】\n"
            "割引額・割引率あり:\nこれちょっと安い\n\n{商品名}\n"
            "今 ¥{price}（-{discount_amount}円 / {discount_rate}%OFF）\n\n普通にあり\n\n"
            "割引なし:\nこれちょっと安い\n\n{商品名}\n今 ¥{price}\n\n普通にあり"
        )
        _core_prompt_a = (
            _PATTERN_A_SYSTEM
            + "\n\n---\n"
            + f"商品名: {product_name}\n現在価格: {price_str}\n"
            + (f"割引額: -{discount_amount:,}円\n" if discount_amount > 0 else "")
            + f"割引率: {discount_pct}%OFF\n"
            + review_ref + desc_ref + knowledge_ref
            + "※ 価格は必ずフォーマット内に含めること\n"
        )
        _core_result_a = _ask_llm_core(_core_prompt_a, "post_generate_pattern_a")
        if _core_result_a is not None:
            gen_count, llm_count = _increment_counts(is_llm_call=not _llm_core_bridge.last_cached)
            print(f"[post_generator] llm_core使用 [生成] 総生成数: {gen_count}/{DAILY_API_LIMIT} | LLM呼び出し: {llm_count}/{DAILY_API_LIMIT}")
            return _core_result_a
        # ─── end llm_core path ────────────────────────────────────────────────

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=80,       # ② トークン制限
            temperature=0.3,     # ② 温度制限
            messages=[
                {
                    "role": "system",
                    "content": (
                        "以下のフォーマットに数値を埋めて出力しなさい。\n"
                        "フォーマット以外の文字を一切追加してはならない。\n\n"
                        "【禁止事項】\n"
                        "・感情表現禁止（例: 美味しそう、最高、すごい、ぴったり）\n"
                        "・主観禁止（例: おすすめ、良い、気になる）\n"
                        "・抽象表現禁止（例: 贅沢、人気、新鮮、絶品）\n"
                        "・説明文・キャッチコピー禁止\n"
                        "・URLやハッシュタグ禁止\n\n"
                        "【出力フォーマット（厳守）】\n"
                        "割引額・割引率あり:\n"
                        "これちょっと安い\n\n"
                        "{商品名}\n"
                        "今 ¥{price}（-{discount_amount}円 / {discount_rate}%OFF）\n\n"
                        "普通にあり\n\n"
                        "割引なし:\n"
                        "これちょっと安い\n\n"
                        "{商品名}\n"
                        "今 ¥{price}\n\n"
                        "普通にあり"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"商品名: {product_name}\n"
                        f"現在価格: {price_str}\n"
                        + (f"割引額: -{discount_amount:,}円\n" if discount_amount > 0 else "")
                        + f"割引率: {discount_pct}%OFF\n"
                        + review_ref
                        + desc_ref
                        + knowledge_ref
                        + "※ 価格は必ずフォーマット内に含めること\n"
                    ),
                },
            ],
        )
        gen_count, llm_count = _increment_counts(is_llm_call=True)
        print(f"[post_generator] OpenAI使用 [生成] 総生成数: {gen_count}/{DAILY_API_LIMIT} | LLM呼び出し: {llm_count}/{DAILY_API_LIMIT}")
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[post_generator] OpenAIエラー → テンプレ使用: {e}")  # ⑤
        return _template()


def generate_post(product: dict, ab_group: str = "A", account_name: str | None = None) -> str:
    hashtags   = _hashtags(product.get("genre", ""), account_name=account_name)
    rank_badge = _rank_badge(product.get("rank", 0))
    point_rate = product.get("point_rate", 1)
    spu_block  = f"🎯 SPU対象！ポイント最大{point_rate}倍！\n\n" if point_rate and point_rate >= 2 else ""

    # ── OpenAI path ──────────────────────────────────────────────────
    if os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_API_KEY") not in ("", "xxxx"):
        # osake / hobby アカウントは常にキャラパス(Pattern-B相当)を使用
        # mono_zukan_jp は AB_TEST_PATTERN env var に従う
        if account_name in _ACCOUNT_SYSTEM_PROMPTS:
            body = _openai_body_pattern_b(product, account_name=account_name)
        else:
            ab_pattern = os.getenv("AB_TEST_PATTERN", "A").strip().upper()
            if ab_pattern == "B":
                body = _openai_body_pattern_b(product, account_name=account_name)
            else:
                body = _openai_body(product)
        review_line = _review_badge(product)
        post = (
            f"{body}\n\n"
            + (f"{review_line}\n\n" if review_line else "")
            + f"{CTA}\n"
            f"{product.get('affiliate_url', '')}\n\n"
            f"{hashtags}"
        )
        return rank_badge + spu_block + _inject_sale_banner(
            _inject_amazon_link(post, product.get("amazon_url", ""))
        )

    # ── Template path ────────────────────────────────────────────────
    rs = product.get("review_summary", "")
    review_block = f"📝 レビューまとめ：\n{rs}\n\n" if rs else ""
    badge_block  = _build_badge_block(product)

    raw_price_str = product.get("price_str", "").replace("¥", "").replace(",", "")
    try:
        raw_price = int(raw_price_str)
    except ValueError:
        raw_price = 0

    opener = _get_opener(raw_price, account_name)

    if product.get("price_dropped"):
        drop_pct  = product.get("price_drop_pct", 0)
        old_price = product.get("price_old_str", "")
        return rank_badge + spu_block + _inject_sale_banner(_inject_amazon_link(
            f"📉 値下がりしてる！{drop_pct}%OFF\n\n"
            f"「{product['title']}」\n\n"
            f"{_description_block(product)}"
            f"元値 {old_price} → 今 {product['price_str']}\n\n"
            f"{badge_block}"
            f"{review_block}"
            f"{CTA}\n"
            f"{product['affiliate_url']}\n\n"
            f"{hashtags}",
            product.get("amazon_url", ""),
        ))

    if raw_price < 5000:
        templates = [
            lambda: (
                f"{opener}\n\n"
                f"「{product['title']}」\n\n"
                f"{_description_block(product)}"
                f"{badge_block}"
                f"{review_block}"
                f"なんと {product['price_str']} で買えちゃう😲\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
            lambda: (
                f"{opener}\n\n"
                f"「{product['title']}」\n\n"
                f"{_description_block(product)}"
                f"{badge_block}"
                f"{review_block}"
                f"価格: {product['price_str']} — この値段でこのクオリティはすごい！\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
            lambda: (
                f"{opener}\n\n"
                f"{product['title']}\n\n"
                f"{_description_block(product)}"
                f"✅ 価格: {product['price_str']}\n"
                f"✅ 手軽に試せるお手頃価格\n\n"
                f"{badge_block}"
                f"{review_block}"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
        ]
    elif raw_price <= 20000:
        templates = [
            lambda: (
                f"{opener}\n\n"
                f"「{product['title']}」\n\n"
                f"{_description_block(product)}"
                f"{badge_block}"
                f"{review_block}"
                f"{product['price_str']}で買えるよ！\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
            lambda: (
                f"{opener}\n\n"
                f"▶ {product['title']}\n\n"
                f"{_description_block(product)}"
                f"価格: {product['price_str']}\n\n"
                f"{badge_block}"
                f"{review_block}"
                f"品質・コスパともに満足度高め😊\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
            lambda: (
                f"{opener}\n\n"
                f"「{product['title']}」\n\n"
                f"{_description_block(product)}"
                f"価格: {product['price_str']}\n\n"
                f"{badge_block}"
                f"{review_block}"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
        ]
    else:
        templates = [
            lambda: (
                f"{opener}\n\n"
                f"「{product['title']}」\n\n"
                f"{_description_block(product)}"
                f"{badge_block}"
                f"{review_block}"
                f"価格: {product['price_str']}\n\n"
                f"長く使えるものへの投資として、ぜひ検討してみてください。\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
            lambda: (
                f"{opener}\n\n"
                f"{product['title']}\n\n"
                f"{_description_block(product)}"
                f"価格: {product['price_str']}\n\n"
                f"{badge_block}"
                f"{review_block}"
                f"上質な体験を提供してくれる逸品です。\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
            lambda: (
                f"{opener}\n\n"
                f"▶ {product['title']}\n\n"
                f"{_description_block(product)}"
                f"価格: {product['price_str']}\n\n"
                f"{badge_block}"
                f"{review_block}"
                f"品質・耐久性・満足度、どれも一流。\n"
                f"大切な方へのギフトにも最適です🎁\n\n"
                f"{CTA}\n"
                f"{product['affiliate_url']}\n\n"
                f"{hashtags}"
            ),
        ]

    return rank_badge + spu_block + _inject_sale_banner(
        _inject_amazon_link(random.choice(templates)(), product.get("amazon_url", ""))
    )


def generate_posts(products: list[dict], account_name: str | None = None) -> list[str]:
    return [generate_post(p, account_name=account_name) for p in products]


if __name__ == "__main__":
    print("Fetching and formatting top products...")
    all_products = get_trending_products()
    top_products = select_top_products(all_products)
    formatted = format_products(top_products)

    print(f"\n=== Sample Posts (top 3) ===\n")
    for i, product in enumerate(formatted[:3], start=1):
        post = generate_post(product)
        print(f"--- Post {i} ---")
        print(post)
        print()
