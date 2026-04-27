"""image_uploader.py — Cloudinary への画像アップロード。

ローカル生成画像を Cloudinary にアップロードし、
Instagram が直接参照できる https URL を返す。
"""

import os

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'),
            override=False)
load_dotenv(override=False)

cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.getenv("CLOUDINARY_API_KEY"),
    api_secret = os.getenv("CLOUDINARY_API_SECRET"),
)


def upload_image(image_path: str | None) -> str | None:
    """ローカル画像を Cloudinary にアップロードし secure_url を返す。

    失敗時は None を返す（呼び出し元でスキップ判定）。
    Cloudinary 未設定（環境変数なし）の場合も None を返す。
    """
    if not image_path or not os.path.exists(image_path):
        print(f"[Cloudinary] 画像ファイルが存在しない: {image_path}")
        return None

    if not os.getenv("CLOUDINARY_CLOUD_NAME"):
        print("[Cloudinary] CLOUDINARY_CLOUD_NAME 未設定 → スキップ")
        return None

    try:
        result = cloudinary.uploader.upload(image_path)
        url = result.get("secure_url")
        print(f"[Cloudinary] アップロード成功: {url}")
        return url
    except Exception as e:
        print(f"[Cloudinary] アップロードエラー: {e}")
        return None
