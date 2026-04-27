import base64
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from database import insert_post_log

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

# INSTAGRAM_ACCESS_TOKEN is already a Page access token (obtained directly from
# the Facebook Page in Graph API Explorer). No dynamic page-token fetch needed.
_PAGE_TOKEN    = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
_IG_USER_ID    = os.getenv("INSTAGRAM_USER_ID", "")
_BASE          = "https://graph.facebook.com/v22.0"
_IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")

_RAKUTEN_IMAGE_HOSTS = ("thumbnail.image.rakuten.co.jp", "r.r10s.jp")
_FREEIMAGE_PUBLIC_KEY = "6d207e02198a847aa98d0a2a901485a5"

_DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def _download_image(image_url: str) -> tuple[bytes, str] | tuple[None, None]:
    """Download image bytes. Returns (bytes, content_type) or (None, None)."""
    try:
        resp = requests.get(image_url, timeout=15, headers=_DL_HEADERS)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return resp.content, content_type
    except Exception as e:
        print(f"[Instagram] Image download failed: {e}")
        return None, None


def _upload_imgbb(image_bytes: bytes) -> str | None:
    """Upload to imgbb.com via base64. Returns public URL or None."""
    api_key = _IMGBB_API_KEY or "free"
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": api_key, "image": b64},
            timeout=30,
        )
        resp.raise_for_status()
        url = resp.json().get("data", {}).get("url")
        if url:
            print(f"[Instagram] Image uploaded to imgbb: {url}")
            return url
        print(f"[Instagram] imgbb returned no URL: {resp.text[:120]}")
        return None
    except Exception as e:
        print(f"[Instagram] imgbb upload failed: {e}")
        return None


def _upload_freeimage(image_bytes: bytes) -> str | None:
    """Upload to freeimage.host via base64 (public key). Returns public URL or None."""
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        resp = requests.post(
            "https://freeimage.host/api/1/upload",
            data={"key": _FREEIMAGE_PUBLIC_KEY, "source": b64, "format": "json"},
            timeout=30,
        )
        resp.raise_for_status()
        url = resp.json().get("image", {}).get("url")
        if url:
            print(f"[Instagram] Image uploaded to freeimage.host: {url}")
            return url
        print(f"[Instagram] freeimage.host returned no URL: {resp.text[:120]}")
        return None
    except Exception as e:
        print(f"[Instagram] freeimage.host upload failed: {e}")
        return None


def _proxy_rakuten_image(image_url: str) -> str | None:
    """Download a Rakuten image and re-host on imgbb or freeimage.host.
    Returns a public URL Instagram can crawl, or None if all proxies fail."""
    image_bytes, _ = _download_image(image_url)
    if not image_bytes:
        print("[Instagram] Could not download source image — skipping Instagram post.")
        return None

    url = _upload_imgbb(image_bytes)
    if url:
        return url

    url = _upload_freeimage(image_bytes)
    if url:
        return url

    print("[Instagram] All image proxies failed — skipping Instagram post.")
    return None


def _resolve_image_url(image_url: str) -> str | None:
    """Return a publicly crawlable image URL. Proxies Rakuten URLs, passes others through."""
    if not image_url:
        return None
    if any(host in image_url for host in _RAKUTEN_IMAGE_HOSTS):
        return _proxy_rakuten_image(image_url)
    return image_url


def _create_container(caption: str, image_url: str,
                      page_token: str = None, ig_user_id: str = None) -> str:
    """Step 1: Create an image media container. Returns creation_id."""
    token   = page_token  or _PAGE_TOKEN
    user_id = ig_user_id or _IG_USER_ID
    resp = requests.post(
        f"{_BASE}/{user_id}/media",
        params={
            "media_type":   "IMAGE",
            "image_url":    image_url,
            "caption":      caption,
            "access_token": token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _publish_container(creation_id: str,
                       page_token: str = None, ig_user_id: str = None) -> str:
    """Step 2: Publish the container. Returns the published media id."""
    token   = page_token  or _PAGE_TOKEN
    user_id = ig_user_id or _IG_USER_ID
    resp = requests.post(
        f"{_BASE}/{user_id}/media_publish",
        params={"creation_id": creation_id, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def post_to_instagram(post: dict, account: dict | None = None,
                      max_retries: int = 3, retry_delay: int = 5) -> dict:
    """Post to Instagram using the same post dict as threads_poster.

    account: optional dict from accounts.json. Supports 'instagram_access_token_env'
             and 'instagram_user_id_env' to specify env var names per account.
             Falls back to INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID when absent.

    Returns a result dict with keys: status, post_id, error.
    Skips gracefully if credentials are missing or image cannot be resolved.
    """
    if account:
        token_env   = account.get("instagram_access_token_env", "INSTAGRAM_ACCESS_TOKEN")
        user_id_env = account.get("instagram_user_id_env",      "INSTAGRAM_USER_ID")
        page_token  = os.getenv(token_env, _PAGE_TOKEN)
        ig_user_id  = os.getenv(user_id_env, _IG_USER_ID)
    else:
        page_token = _PAGE_TOKEN
        ig_user_id = _IG_USER_ID

    if not page_token or not ig_user_id:
        print("[Instagram] INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID not set — skipping.")
        return {"status": "skipped", "post_id": None, "error": "credentials not set"}

    raw_image_url = post.get("image_url", "")
    image_url = _resolve_image_url(raw_image_url)

    if not image_url:
        print("[Instagram] No usable image_url — skipping (Instagram requires an image).")
        return {"status": "skipped", "post_id": None, "error": "no image_url"}

    caption = post.get("post_text", post.get("title", ""))
    result  = {
        "title":     post.get("title", "")[:60],
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "status":    None,
        "post_id":   None,
        "error":     None,
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            creation_id = _create_container(caption, image_url,
                                           page_token=page_token, ig_user_id=ig_user_id)
            time.sleep(2)
            media_id = _publish_container(creation_id,
                                          page_token=page_token, ig_user_id=ig_user_id)
            result["status"]  = "success"
            result["post_id"] = media_id
            print(f"[OK] [Instagram] Posted | media_id={media_id} (attempt {attempt}/{max_retries})")
            print(f"     {result['title']}")
            acc_name = account.get("name", "mono_zukan_jp") if account else "mono_zukan_jp"
            insert_post_log({
                "title":         post.get("title", "")[:60],
                "genre":         post.get("genre", ""),
                "price":         post.get("price", post.get("price_str", "")),
                "url":           post.get("url", ""),
                "affiliate_url": post.get("affiliate_url", ""),
                "ab_group":      post.get("ab_group", "A"),
                "account":       acc_name,
                "posted_at":     result["posted_at"],
                "status":        "success",
                "post_id":       media_id,
                "error":         None,
                "image_url":     post.get("image_url", ""),
                "platform":      "instagram",
            })
            return result
        except requests.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"[RETRY {attempt}/{max_retries}] [Instagram] {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[RETRY {attempt}/{max_retries}] [Instagram] {last_error}")
        if attempt < max_retries:
            time.sleep(retry_delay)

    result["status"] = "error"
    result["error"]  = last_error
    print(f"[FAIL] [Instagram] All {max_retries} attempts failed. Last error: {last_error}")
    return result


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    from post_manager import get_next_post
    post = get_next_post()
    if not post:
        print("No pending posts in queue.")
        sys.exit(0)
    print(f"Posting to Instagram: {post['title'][:60]}")
    r = post_to_instagram(post)
    print(f"Status: {r['status']}")
    if r["error"]:
        print(f"Error : {r['error']}")
