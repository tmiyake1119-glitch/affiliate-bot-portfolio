"""
video_generator.py — 縦型ショート動画生成（1080x1920）

依存: moviepy 2.x, Pillow, ffmpeg
moviepy / ffmpeg 未インストール時は安全にスキップする。

エフェクト:
  - Scene 1/4: テキスト左スライドイン（0.3秒）
  - Scene 2  : ケン・バーンズズームイン（1.0→1.05倍 / 4秒）＋固定バーオーバーレイ
  - 全シーン : フェードイン・フェードアウト（0.3秒）
  - BGM      : data/bgm.mp3 が存在する場合のみ追加（vol=0.3）

フォント: NotoSansJP-VF.ttf → YuGothB.ttc → meiryob.ttc → msgothic.ttc（優先順）
デザイン:
  - テキスト影: 2px右下、透明度50%
  - タイトルバー: alpha=240（ほぼ不透明）、フォント65px、stroke_width=3
  - 下部バー: alpha100→240 グラデーション
  - 価格表示: 「今だけ」57px クリーム黄 stroke=3 / 価格100px stroke=2 / 割引62px クリーム黄 stroke=3
  - Scene1/4: 白文字 + stroke=3 + 帯背景 alpha=200（Scene4: フォント88px）
"""

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 日本語フォント候補（Linux優先 → Windows → None）
_FONT_CANDIDATES = [
    # Linux (ConoHa VPS)
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJKjp-Regular.otf",
    # Windows
    "C:/Windows/Fonts/NotoSansJP-VF.ttf",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]
FONT_PATH = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
ICON_PATH_V = os.path.join(os.path.dirname(__file__), '..', 'assets', 'zukkapon_vertical.png')
ICON_PATH   = os.path.join(os.path.dirname(__file__), '..', 'assets', 'zukkapon.png')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'videos')
BGM_PATH   = os.path.join(os.path.dirname(__file__), '..', 'data', 'bgm.mp3')
VIDEO_W, VIDEO_H = 1080, 1920
FPS       = 24
FADE_DUR  = 0.3


# ---------------------------------------------------------------------------
# PIL フレーム生成
# ---------------------------------------------------------------------------

_TITLE_STOP_PATTERNS = [
    "【", "（", "(", "★", "◆", "／", "/", "＼", "\u3000",  # 全角スペース
    "送料", "クーポン", "レビュー", "ポイント", "セール",
]


def _clean_title(title: str, max_len: int = 20) -> str:
    """楽天タイトルから余計な情報を除去し max_len 文字に制限する。"""
    import re
    original = title
    # 先頭の ＼...／ パターンを除去（例: ＼送料無料／、＼着後レビュー贈呈／）
    title = re.sub(r'^＼[^／]*／\s*', '', title).strip()
    # 先頭の【...】や（...）などのブラケットを除去
    title = re.sub(r'^[【〔（(][^】〕）)]*[】〕）)]\s*', '', title).strip()
    # 除去パターン以降を切り捨て
    cut = len(title)
    for pat in _TITLE_STOP_PATTERNS:
        idx = title.find(pat)
        if idx > 0:
            cut = min(cut, idx)
    title = title[:cut].strip()
    # 空になった場合は【...】の中身を抽出して再試行
    if not title:
        m = re.search(r'[【〔]([^】〕]{2,})[】〕]', original)
        if m:
            title = m.group(1).strip()
            cut2 = len(title)
            for pat in _TITLE_STOP_PATTERNS:
                idx = title.find(pat)
                if idx > 0:
                    cut2 = min(cut2, idx)
            title = title[:cut2].strip()
    if not title:
        return original[:max_len]
    return (title[:max_len] + "...") if len(title) > max_len else title


def _load_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def _draw_text_centered(draw, text: str, font, y: int,
                         color, shadow_color=(0, 0, 0, 128),
                         stroke_width: int = 0, stroke_fill=None):
    """1行テキストを水平中央に描画（影: 2px右下、透明度50%）。
    stroke_width > 0 の場合は黒縁取りを付ける。
    """
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (VIDEO_W - tw) // 2
    # 影
    draw.text((x + 2, y + 2), text, font=font, fill=shadow_color)
    # 本文（縁取りオプション）
    kwargs = {}
    if stroke_width > 0:
        kwargs["stroke_width"] = stroke_width
        kwargs["stroke_fill"]  = stroke_fill or (0, 0, 0, 255)
    draw.text((x, y), text, font=font, fill=color, **kwargs)


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    """テキストを max_width に収まるよう文字単位で折り返す。"""
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines, current = [], ""
    for ch in text:
        test = current + ch
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def _make_frame(
    bg_color: tuple,
    text_lines: list[str],
    font_size: int = 60,
    text_color: tuple = (255, 255, 255),
    image_path: str | None = None,
    stroke_width: int = 0,
    band_bg_alpha: int = 0,
) -> np.ndarray:
    """Scene 1 / 3 / 4 用の静止フレームを生成。
    stroke_width  > 0: テキストに黒縁取り
    band_bg_alpha > 0: 各テキスト行の背後に半透明帯を描画
    """
    from PIL import Image, ImageDraw

    frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (*bg_color, 255))

    if image_path and os.path.exists(image_path):
        try:
            img = Image.open(image_path).convert("RGB")
            new_w = VIDEO_W
            new_h = int(img.height * new_w / img.width)
            img = img.resize((new_w, min(new_h, VIDEO_H)), Image.LANCZOS)
            y_off = max(0, (VIDEO_H - img.height) // 2)
            frame.paste(img.convert("RGBA"), (0, y_off))
            ov = Image.new("RGBA", frame.size, (0, 0, 0, 140))
            frame = Image.alpha_composite(frame, ov)
        except Exception:
            pass

    draw   = ImageDraw.Draw(frame)
    font   = _load_font(font_size)
    pad    = 20                          # 帯の上下パディング
    line_h = font_size + 16
    total_h = line_h * len(text_lines)
    y = (VIDEO_H - total_h) // 2

    for line in text_lines:
        if band_bg_alpha > 0:
            # テキスト行ごとに横幅いっぱいの帯を描画
            draw.rectangle(
                [(0, y - pad), (VIDEO_W, y + font_size + pad)],
                fill=(0, 0, 0, band_bg_alpha),
            )
        _draw_text_centered(draw, line, font, y,
                            color=(*text_color, 255),
                            stroke_width=stroke_width)
        y += line_h

    return np.array(frame.convert("RGB"))


def _cover_crop(img, w: int, h: int):
    """アスペクト比を維持したまま w×h を完全に覆うようリサイズ＆中央クロップ。"""
    from PIL import Image
    scale = max(w / img.width, h / img.height)
    nw    = int(img.width  * scale)
    nh    = int(img.height * scale)
    img   = img.resize((nw, nh), Image.LANCZOS)
    x0    = (nw - w) // 2
    y0    = (nh - h) // 2
    return img.crop((x0, y0, x0 + w, y0 + h))


def _make_scene1_frame(icon_path: str | None) -> np.ndarray:
    """Scene 1: 背景画像全画面(cover crop) + 中央半透明帯 + 2行テキスト。"""
    from PIL import Image, ImageDraw

    if icon_path and os.path.exists(icon_path):
        try:
            img = Image.open(icon_path).convert("RGB")
            img = _cover_crop(img, VIDEO_W, VIDEO_H)
            frame = img.convert("RGBA")
        except Exception:
            frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (26, 26, 46, 255))
    else:
        frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (26, 26, 46, 255))

    draw = ImageDraw.Draw(frame)

    font1  = _load_font(80)
    font2  = _load_font(64)
    LINE1H = 80 + 20
    LINE2H = 64 + 20
    BAND_H = LINE1H + LINE2H + 60   # 上下パディング30px ずつ

    by = (VIDEO_H - BAND_H) // 2
    draw.rectangle([(0, by), (VIDEO_W, by + BAND_H)], fill=(0, 0, 0, 180))

    ty = by + 30
    _draw_text_centered(draw, "買って良かったモノ図鑑", font1, ty,
                        color=(255, 255, 255, 255), stroke_width=4)
    ty += LINE1H
    _draw_text_centered(draw, "ずかぽんの図鑑", font2, ty,
                        color=(255, 255, 255, 255), stroke_width=4)

    return np.array(frame.convert("RGB"))


def _make_scene4_frame(icon_path: str | None) -> np.ndarray:
    """Scene 4: 背景画像全画面(cover crop・暗め) + 中央半透明帯 + CTAテキスト。"""
    from PIL import Image, ImageDraw

    if icon_path and os.path.exists(icon_path):
        try:
            img = Image.open(icon_path).convert("RGB")
            img = _cover_crop(img, VIDEO_W, VIDEO_H)
            frame = img.convert("RGBA")
            dark_ov = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 120))
            frame = Image.alpha_composite(frame, dark_ov)
        except Exception:
            frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (20, 10, 30, 255))
    else:
        frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (20, 10, 30, 255))

    draw = ImageDraw.Draw(frame)

    FONT_SIZE = 80
    font      = _load_font(FONT_SIZE)
    lines     = ["詳細はプロフィールの", "リンクから！"]   # 手動改行

    LINE_H    = FONT_SIZE + 20
    BAND_H    = LINE_H * len(lines) + 60
    by        = (VIDEO_H - BAND_H) // 2
    draw.rectangle([(0, by), (VIDEO_W, by + BAND_H)], fill=(0, 0, 0, 200))

    ty = by + 30
    for line in lines:
        _draw_text_centered(draw, line, font, ty,
                            color=(255, 255, 255, 255), stroke_width=4)
        ty += LINE_H

    return np.array(frame.convert("RGB"))


_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _fetch_image_pil(image_path: str | None, image_url: str | None):
    """ローカルパス → URL の順で PIL Image を返す。取得失敗時は None。"""
    from PIL import Image
    if image_path and os.path.exists(image_path):
        try:
            return Image.open(image_path).convert("RGB")
        except Exception as e:
            print(f"[VideoGen] ローカル画像���み��み失敗: {e}")
    if image_url:
        try:
            import requests, io
            resp = requests.get(image_url, timeout=10, headers=_UA)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            print(f"[VideoGen] 画像取得成功: {image_url[:60]} ({img.size})")
            return img
        except Exception as e:
            print(f"[VideoGen] 画像URL取得失敗: {e}")
    return None


def _fit_image(img, w: int, h: int):
    """アスペクト比維持で w×h に収まるよ���リサイズ。余白���黒。"""
    from PIL import Image
    scale = min(w / img.width, h / img.height)
    nw    = int(img.width  * scale)
    nh    = int(img.height * scale)
    img   = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    x0 = (w - nw) // 2
    y0 = (h - nh) // 2
    canvas.paste(img, (x0, y0))
    return canvas


def _make_scene3_frame(
    image_path: str | None,
    image_url: str | None,
    review_avg: float,
    review_count: int,
    free_ship: bool,
) -> np.ndarray:
    """Scene 3: 商品画像全画面 + レビュー情報オーバーレイ。"""
    from PIL import Image, ImageDraw

    # 背景: 商品画像 fit（黒帯あり・中央配置）、取得失敗時は濃紺
    img = _fetch_image_pil(image_path, image_url)
    if img:
        fitted = _fit_image(img, VIDEO_W, VIDEO_H)
        frame  = fitted.convert("RGBA")
        # 軽く暗めにして文字を読みやすく
        dark_ov = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 100))
        frame   = Image.alpha_composite(frame, dark_ov)
    else:
        frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (20, 20, 50, 255))

    draw = ImageDraw.Draw(frame)

    # 上部バー: 「レビュー」ラベル
    TOP_H = 140
    draw.rectangle([(0, 0), (VIDEO_W, TOP_H)], fill=(0, 0, 0, 200))
    font_label = _load_font(56)
    _draw_text_centered(draw, "レビュー", font_label,
                        (TOP_H - 56) // 2,
                        color=(255, 255, 255, 255), stroke_width=2)

    # 中央: 星評価 100px（オレンジ）
    font_star  = _load_font(100)
    font_count = _load_font(60)
    font_ship  = _load_font(60)

    full  = int(review_avg)
    half  = 1 if (review_avg - full) >= 0.3 else 0
    empty = 5 - full - half
    stars = "★" * full + ("½" if half else "") + "☆" * empty
    star_text = f"{stars}  {review_avg:.1f}"

    # 星 + 評価値
    cy = VIDEO_H // 2 - 80
    _draw_text_centered(draw, star_text, font_star, cy,
                        color=(255, 165, 0, 255), stroke_width=3)
    cy += 120

    # レビュー件数
    if review_count > 0:
        count_text = f"({review_count:,}件)"
        _draw_text_centered(draw, count_text, font_count, cy,
                            color=(255, 255, 255, 255), stroke_width=2)
        cy += 80

    # 送料無料
    if free_ship:
        _draw_text_centered(draw, "送料無料", font_ship, cy,
                            color=(80, 220, 80, 255), stroke_width=2)

    return np.array(frame.convert("RGB"))


def _make_product_bg_frame(image_path: str | None,
                            image_url: str | None = None) -> np.ndarray:
    """Scene 2 用: 商品画像のみのフレーム（ズーム対象）。fit（黒帯あり）で中央配置。"""
    img = _fetch_image_pil(image_path, image_url)
    if img:
        frame = _fit_image(img, VIDEO_W, VIDEO_H)
    else:
        from PIL import Image
        frame = Image.new("RGB", (VIDEO_W, VIDEO_H), (18, 18, 18))
    return np.array(frame)


def _make_product_overlay(
    title: str,
    price: str,
    price_old: int,
    price_drop_pct: float,
    price_dropped: bool,
    free_ship: bool,
) -> np.ndarray:
    """Scene 2 用: 上部タイトルバー + 下部グラデーション価格バー（固定オーバーレイ、RGBA）。"""
    from PIL import Image, ImageDraw

    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    TITLE_SIZE  = 65          # 54 × 1.2 ≒ 65
    font_title  = _load_font(TITLE_SIZE)
    font_imdake = _load_font(57)   # 48 × 1.2 ≒ 57（黄色テキスト拡大）
    font_price  = _load_font(100)
    font_drop   = _load_font(62)   # 52 × 1.2 ≒ 62（黄色テキスト拡大）
    font_ship   = _load_font(52)   # 送料無料は据え置き

    # 上部タイトルバー（ほぼ不透明: alpha=240）
    TOP_H = 200
    draw.rectangle([(0, 0), (VIDEO_W, TOP_H)], fill=(0, 0, 0, 240))
    title_lines = _wrap_text(_clean_title(title), font_title, VIDEO_W - 40)
    line_step = TITLE_SIZE + 14
    ty = (TOP_H - len(title_lines) * line_step) // 2
    for line in title_lines[:2]:
        _draw_text_centered(draw, line, font_title, ty,
                            color=(255, 255, 255, 255),
                            stroke_width=3, stroke_fill=(0, 0, 0, 255))
        ty += line_step

    # 下部価格バー — 上→下グラデーション（alpha 100 → 240）
    BOT_H = 360
    by    = VIDEO_H - BOT_H
    for i in range(BOT_H):
        alpha = int(100 + 140 * (i / BOT_H))   # 100 → 240
        draw.line([(0, by + i), (VIDEO_W, by + i)], fill=(0, 0, 0, alpha))

    # 「今だけ」ラベル（クリーム黄色・stroke=3）
    py = by + 16
    _draw_text_centered(draw, "今だけ", font_imdake, py, color=(255, 240, 50, 255),
                        stroke_width=3)
    py += 68

    # 価格（大）
    _draw_text_centered(draw, price, font_price, py, color=(255, 255, 255, 255),
                        stroke_width=2)
    py += 116

    if price_dropped and price_old:
        try:
            price_raw = int(str(price).replace("¥", "").replace(",", ""))
            diff      = price_old - price_raw
            drop_text = f"前回より ¥{diff:,}お得（{int(price_drop_pct)}%DOWN）"
        except Exception:
            drop_text = f"前回より {int(price_drop_pct)}%DOWN"
        _draw_text_centered(draw, drop_text, font_drop, py, color=(255, 240, 50, 255),
                            stroke_width=3)
        py += 72

    if free_ship:
        _draw_text_centered(draw, "送料無料", font_ship, py, color=(80, 220, 80, 255),
                            stroke_width=2)

    return np.array(overlay)


# ---------------------------------------------------------------------------
# moviepy クリップ生成
# ---------------------------------------------------------------------------

def _slide_clip(frame: np.ndarray, duration: float):
    """全フレームを左からスライドインさせる CompositeVideoClip を返す。"""
    from moviepy import ImageClip, CompositeVideoClip

    clip = ImageClip(frame, duration=duration)
    clip = clip.with_position(
        lambda t: (int(-VIDEO_W * max(0.0, 1.0 - t / FADE_DUR)), 0)
    )
    return CompositeVideoClip([clip], size=(VIDEO_W, VIDEO_H))


def _zoom_clip_with_overlay(
    bg_frame: np.ndarray,
    overlay_frame: np.ndarray,
    duration: float,
):
    """ケン・バーンズズームイン（1.0→1.05倍）+ 固定オーバーレイ合成。
    bg_frame    : 商品画像のみ（ズーム対象）
    overlay_frame: タイトル/価格バー RGBA（固定）
    """
    from moviepy import VideoClip
    from PIL import Image

    bg_base  = Image.fromarray(bg_frame)
    ov_img   = Image.fromarray(overlay_frame)   # RGBA

    def make_frame(t: float) -> np.ndarray:
        scale = 1.0 + 0.05 * (t / duration)    # 1.0 → 1.05
        nw    = int(VIDEO_W * scale)
        nh    = int(VIDEO_H * scale)
        zoomed = bg_base.resize((nw, nh), Image.LANCZOS)
        x0    = (nw - VIDEO_W) // 2
        y0    = (nh - VIDEO_H) // 2
        cropped = zoomed.crop((x0, y0, x0 + VIDEO_W, y0 + VIDEO_H)).convert("RGBA")
        # 固定オーバーレイを合成
        composite = Image.alpha_composite(cropped, ov_img)
        return np.array(composite.convert("RGB"))

    return VideoClip(make_frame, duration=duration)


def _with_fade(clip):
    """フェードイン・フェードアウトを適用する。"""
    import moviepy.video.fx as vfx
    return clip.with_effects([vfx.FadeIn(FADE_DUR), vfx.FadeOut(FADE_DUR)])


def _add_bgm(final_clip):
    """data/bgm.mp3 が存在する場合のみ BGM を追加して返す。"""
    if not os.path.exists(BGM_PATH):
        return final_clip

    try:
        from moviepy import AudioFileClip, concatenate_audioclips
        import moviepy.audio.fx as afx

        bgm      = AudioFileClip(BGM_PATH)
        bgm      = bgm.with_effects([afx.MultiplyVolume(0.3)])
        total    = final_clip.duration

        if bgm.duration < total:
            loops = int(total / bgm.duration) + 1
            bgm   = concatenate_audioclips([bgm] * loops).subclipped(0, total)
        else:
            bgm = bgm.subclipped(0, total)

        print(f"[VideoGen] BGM追加: {os.path.basename(BGM_PATH)}")
        return final_clip.with_audio(bgm)

    except Exception as e:
        print(f"[VideoGen] BGM追加エラー → スキップ: {e}")
        return final_clip


# ---------------------------------------------------------------------------
# メイン関数
# ---------------------------------------------------------------------------

def generate_video(product: dict) -> str | None:
    """商品 dict からショート動画を生成。成功時はファイルパスを返す。失敗時は None。"""
    try:
        try:
            from moviepy import concatenate_videoclips          # moviepy 2.x
        except ImportError:
            from moviepy.editor import concatenate_videoclips   # moviepy 1.x
    except ImportError:
        print("[VideoGen] moviepy 未インストール → スキップ")
        return None

    try:
        import time as _time
        _start = _time.time()

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # ── 商品データ解析 ────────────────────────────────────────────────
        title      = (product.get("title") or "")[:50]
        _praw      = product.get("price_str") or product.get("price") or ""
        price      = str(_praw) if not isinstance(_praw, str) else _praw
        if price and not price.startswith("¥"):
            try:
                price = f"¥{int(price):,}"
            except ValueError:
                pass
        elif isinstance(_praw, int):
            price = f"¥{_praw:,}"

        price_old      = int(product.get("price_old") or 0)
        price_dropped  = bool(product.get("price_dropped"))
        price_drop_pct = float(product.get("price_drop_pct") or 0)
        review_avg     = float(product.get("review_average") or product.get("review_score") or 0)
        review_count   = int(product.get("review_count") or 0)
        free_ship      = bool(product.get("free_shipping") or product.get("shipping") == "free")
        local_img      = product.get("local_image_path") or None
        image_url      = product.get("image_url") or None
        product_id     = (product.get("url") or "item")[-20:].replace("/", "_").replace("?", "_")

        ts       = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        out_path = os.path.join(OUTPUT_DIR, f"{product_id}_{ts}.mp4")

        # ── Scene 1: オープニング (1秒) — スライドイン ───────────────────
        # 元の横長画像を優先、なければ縦型をフォールバック
        if os.path.exists(ICON_PATH):
            icon_path = ICON_PATH
        elif os.path.exists(ICON_PATH_V):
            icon_path = ICON_PATH_V
        else:
            icon_path = None
        f1 = _make_scene1_frame(icon_path)
        s1 = _with_fade(_slide_clip(f1, duration=1.0))

        # ── Scene 2: 商品画像(ズーム) + 固定バーオーバーレイ (4秒) ─────────
        f2_bg = _make_product_bg_frame(image_path=local_img, image_url=image_url)
        f2_ov = _make_product_overlay(
            title=title, price=price,
            price_old=price_old, price_drop_pct=price_drop_pct,
            price_dropped=price_dropped, free_ship=free_ship,
        )
        s2 = _with_fade(_zoom_clip_with_overlay(f2_bg, f2_ov, duration=4.0))

        # ── Scene 3: レビュー・バッジ (2秒) — review_avg=0かつcount=0はスキップ ──
        has_review = review_avg > 0 or review_count > 0
        if has_review:
            f3 = _make_scene3_frame(
                image_path=local_img,
                image_url=image_url,
                review_avg=review_avg,
                review_count=review_count,
                free_ship=free_ship,
            )
            s3 = _with_fade(_make_static_clip(f3, duration=2.0))
        else:
            print("[VideoGen] レビューなし → Scene3 スキップ")

        # ── Scene 4: CTA (1秒) — スライドイン ────────────────────────────
        f4 = _make_scene4_frame(icon_path)
        s4 = _with_fade(_slide_clip(f4, duration=1.0))

        # ── 結合・BGM・書き出し ───────────────────────────────────────────
        from moviepy import CompositeVideoClip
        clips = [s1, s2, s3, s4] if has_review else [s1, s2, s4]
        final = concatenate_videoclips(clips, method="compose")
        # サイズを明示的に固定して黒枠を防止
        final = CompositeVideoClip([final], size=(VIDEO_W, VIDEO_H))
        final = _add_bgm(final)

        final.write_videofile(
            out_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            audio=True,
            logger=None,
        )

        elapsed = _time.time() - _start
        size_kb = os.path.getsize(out_path) // 1024
        print(f"[VideoGen] 完了: {out_path}  ({size_kb}KB, {elapsed:.1f}秒)")
        return out_path

    except Exception as e:
        import traceback
        print(f"[VideoGen] 動画生成エラー → スキップ: {e}")
        traceback.print_exc()
        return None


def _make_static_clip(frame: np.ndarray, duration: float):
    """静止フレームの ImageClip を返す（Scene 3 用）。"""
    from moviepy import ImageClip
    return ImageClip(frame, duration=duration)
