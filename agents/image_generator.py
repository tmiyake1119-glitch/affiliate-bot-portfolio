"""楽天商品画像に割引率・価格・送料無料を重ねた投稿用画像を生成する。

入力: product dict（title, price_str, discount_rate, image_url, free_shipping）
出力: output/images/{sanitized_title}.png  (失敗時は None)
"""

import math
import os
import re
import sys
import io
import requests

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'output', 'images')

# 日本語フォント候補（Linux優先 → Windows → Pillow デフォルト）
_FONT_CANDIDATES = [
    # Linux (ConoHa VPS)
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJKjp-Regular.otf",
    # Windows
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\yugothb.ttc",
    r"C:\Windows\Fonts\yugothic.ttf",
    r"C:\Windows\Fonts\msgothic.ttc",
]

CANVAS_SIZE   = (1080, 1080)
BAR_HEIGHT    = 270          # 下部バー高さ（216 → +25%拡張）
PRODUCT_SIZE  = (CANVAS_SIZE[0], CANVAS_SIZE[1] - BAR_HEIGHT)  # (1080, 810)
BADGE_SIZE    = 198          # %OFFバッジ直径 (110 × 1.8)


def _get_font(size: int):
    """利用可能な日本語フォントを返す。見つからなければデフォルトを返す。"""
    try:
        from PIL import ImageFont
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()
    except ImportError:
        return None


def _upgrade_cloudinary_url(url: str) -> str:
    """Cloudinary URL の /upload/ 直後に高解像度変換パラメータを挿入して返す。

    例:
      変換前: https://res.cloudinary.com/xxx/image/upload/v123/abc.jpg
      変換後: https://res.cloudinary.com/xxx/image/upload/w_1080,h_1080,c_fill,q_auto:best/v123/abc.jpg
    """
    if "res.cloudinary.com" not in url:
        return url
    marker = "/upload/"
    idx = url.find(marker)
    if idx == -1:
        return url
    insert_pos = idx + len(marker)
    return url[:insert_pos] + "w_1080,h_1080,c_fill,q_auto:best/" + url[insert_pos:]


def _download_image(url: str):
    """URLから画像をダウンロードして PIL Image を返す。失敗時は None。

    Cloudinary URL の場合は高解像度版に変換してから取得する。
    """
    if not url:
        return None
    try:
        from PIL import Image
        fetch_url = _upgrade_cloudinary_url(url)
        resp = requests.get(fetch_url, timeout=10)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception:
        return None


def _sanitize_filename(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    return name[:max_len].strip("_. ")


def _extract_features(description: str) -> list:
    """product_descriptionから特徴を最大3つ抽出する。不足分はfallbackで補う。

    除外条件: 不要ワード含む文・10文字未満・数字記号のみ
    優先条件: 商品特徴らしいワード（できる/機能/素材 等）を含む文を先頭に
    """
    fallbacks = ["人気商品", "高評価", "売れ筋"]
    if not description:
        return fallbacks[:]

    _SKIP_WORDS     = ["ご了承", "注意", "※", "■", "詳細", "ページ",
                       "こちら", "場合", "お問い合わせ"]
    _PRIORITY_WORDS = ["できる", "性", "機能", "対応", "加工", "素材", "設計"]

    parts = re.split(r'[。\n\r！!]', description)
    parts = [p.strip() for p in parts]

    filtered = []
    for p in parts:
        if len(p) < 10:
            continue
        if any(kw in p for kw in _SKIP_WORDS):
            continue
        if re.fullmatch(r'[\d\s\W]+', p):
            continue
        filtered.append(p)

    priority = [p for p in filtered if any(kw in p for kw in _PRIORITY_WORDS)]
    others   = [p for p in filtered if p not in priority]
    ordered  = priority + others

    features = []
    for part in ordered:
        features.append(part[:15] if len(part) > 15 else part)
        if len(features) >= 3:
            break

    while len(features) < 3:
        features.append(fallbacks[len(features)])
    return features


def generate_image(product: dict) -> str | None:
    """商品画像に割引率・価格・送料無料バッジを重ねて PNG 保存。

    Args:
        product: キューエントリまたは formatted product dict

    Returns:
        保存したファイルパス、失敗時は None
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont as _IF
    except ImportError:
        print("[image_generator] Pillow がインストールされていません。pip install Pillow")
        return None

    title         = product.get("title", "product")
    price_str     = product.get("price", "") or product.get("price_str", "")
    discount_rate = product.get("discount_rate", 0.0) or 0.0
    image_url     = product.get("image_url", "")
    free_shipping = product.get("free_shipping", False) or product.get("postage_flag", 0) == 1
    review_avg    = float(product.get("review_average", 0.0) or 0.0)
    review_count  = int(product.get("review_count", 0) or 0)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = _sanitize_filename(title) + ".png"
    out_path = os.path.join(OUTPUT_DIR, filename)

    # ── キャンバス作成 ──────────────────────────────────────────
    canvas = Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 255))

    # ── 商品画像 ─────────────────────────────────────────────
    product_img = _download_image(image_url)
    if product_img:
        product_img = product_img.convert("RGBA").resize(PRODUCT_SIZE, Image.LANCZOS)
        product_img = product_img.filter(ImageFilter.SHARPEN)
        canvas.paste(product_img, (0, 0), product_img)
    else:
        # 画像なし: グレーの placeholder
        draw_ph = ImageDraw.Draw(canvas)
        draw_ph.rectangle([0, 0, CANVAS_SIZE[0], PRODUCT_SIZE[1]], fill=(230, 230, 230, 255))

    draw = ImageDraw.Draw(canvas)

    # ── 下部バー（ダーク背景） ───────────────────────────────
    bar_y = CANVAS_SIZE[1] - BAR_HEIGHT
    draw.rectangle([0, bar_y, CANVAS_SIZE[0], CANVAS_SIZE[1]], fill=(30, 30, 30, 230))

    # ── 商品名（1行・30文字以内） ──────────────────────────────
    raw_title = product.get("title", "")
    name_text = (raw_title[:29] + "…") if len(raw_title) > 30 else raw_title
    if name_text:
        font_name = _get_font(28)
        draw.text((36, bar_y + 12), name_text, font=font_name, fill=(255, 255, 255, 255))

    # ── 特徴3つ（product_descriptionから抽出） ────────────────
    features = _extract_features(product.get("product_description", ""))
    font_feat = _get_font(22)
    for i, feat in enumerate(features[:3]):
        fy = bar_y + 52 + i * 32
        draw.text((36, fy), f"・{feat}", font=font_feat, fill=(200, 200, 200, 255))

    # ── 価格テキスト ──────────────────────────────────────────
    price_val = product.get("price", 0) or 0
    if isinstance(price_val, str):
        price_val = price_val.replace("¥", "").replace(",", "").strip()
    try:
        price_int = int(price_val) if price_val else 0
    except (ValueError, TypeError):
        price_int = 0
    price_str_fmt = product.get("price_str", "")
    if not price_str_fmt and price_int:
        price_str_fmt = f"¥{price_int:,}"

    discount_pct_badge = int(round(discount_rate * 100))

    if price_str_fmt:
        if discount_rate > 0.05 and price_int > 0:
            # 定価を逆算して「定価¥X,XXX → ¥X,XXX（XX%OFF）」形式で表示
            original_price = math.ceil(price_int / (1 - discount_rate) / 100) * 100
            price_display  = f"定価¥{original_price:,} → {price_str_fmt}（{discount_pct_badge}%OFF）"
            font_price     = _get_font(36)
        else:
            price_display = price_str_fmt
            font_price    = _get_font(58)
        draw.text((36, bar_y + 156), price_display, font=font_price, fill=(255, 255, 255, 255))

    # ── レビュー評価 ──────────────────────────────────────────
    if review_avg >= 4.0 and review_count > 0:
        try:
            font_star = _get_font(32)
            star_text = f"★{review_avg:.1f}  ({review_count}件)"
            draw.text((36, bar_y + 218), star_text, font=font_star, fill=(255, 215, 0, 255))
        except Exception:
            pass

    # ── 送料無料バッジ ────────────────────────────────────────
    if free_shipping:
        font_ship = _get_font(32)
        ship_text = "送料無料"
        bbox = draw.textbbox((0, 0), ship_text, font=font_ship)
        tw = bbox[2] - bbox[0]
        tx = CANVAS_SIZE[0] - tw - 36
        ty = bar_y + 164
        draw.rounded_rectangle([tx - 14, ty - 7, tx + tw + 14, ty + (bbox[3] - bbox[1]) + 7],
                                radius=11, fill=(0, 180, 100, 255))
        draw.text((tx, ty), ship_text, font=font_ship, fill=(255, 255, 255, 255))

    # ── %OFF バッジ（右上） ───────────────────────────────────
    if discount_rate >= 0.05:
        discount_pct = int(round(discount_rate * 100))
        bx = CANVAS_SIZE[0] - BADGE_SIZE - 22
        by = 22
        draw.ellipse([bx, by, bx + BADGE_SIZE, by + BADGE_SIZE], fill=(220, 30, 30, 230))
        font_pct  = _get_font(50)
        font_off  = _get_font(32)
        pct_text  = f"{discount_pct}%"
        off_text  = "OFF"
        pb = draw.textbbox((0, 0), pct_text, font=font_pct)
        pw = pb[2] - pb[0]
        ph = pb[3] - pb[1]
        ob = draw.textbbox((0, 0), off_text, font=font_off)
        ow = ob[2] - ob[0]
        cx = bx + BADGE_SIZE // 2
        cy = by + BADGE_SIZE // 2
        draw.text((cx - pw // 2, cy - ph // 2 - 11), pct_text, font=font_pct, fill=(255, 255, 255, 255))
        draw.text((cx - ow // 2, cy + ph // 2 - 7), off_text, font=font_off, fill=(255, 255, 255, 255))

    # ── 保存 ─────────────────────────────────────────────────
    canvas.convert("RGB").save(out_path, "PNG", optimize=True)
    print(f"[image_generator] 保存: {out_path}")
    return out_path
