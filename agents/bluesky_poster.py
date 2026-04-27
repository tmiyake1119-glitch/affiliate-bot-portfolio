import os
import re
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_HANDLE       = os.getenv("BLUESKY_HANDLE", "")
_APP_PASSWORD = os.getenv("BLUESKY_APP_PASSWORD", "")
_BASE         = "https://bsky.social/xrpc"
MAX_TEXT_LEN  = 280  # leave room for hashtags

# Casual, conversational openers suited to Bluesky's tone
_OPENERS = [
    "ちょっとこれ見て😳",
    "これ、マジでよかった",
    "買って正解だったやつ紹介します",
    "正直かなりいい👀",
    "使ってみてびっくりした話",
    "これは買い👇",
    "今日のおすすめはこれ！",
    "気になってた人、見て",
    "こういうの探してたって人に",
    "試したら普通に最高だった件",
]


def _format_for_bluesky(post_text: str) -> str:
    """Reformat post_text for Bluesky style:
    - Replace the first line with a casual Japanese opener
    - Keep only 2 hashtags
    - Trim to MAX_TEXT_LEN chars
    """
    import hashlib
    import re

    lines = post_text.splitlines()

    # Replace opener (first non-empty line) with a casual Bluesky opener
    opener_idx = next((i for i, l in enumerate(lines) if l.strip()), 0)
    # Pick opener deterministically based on post content so reruns are stable
    seed = int(hashlib.md5(post_text.encode()).hexdigest(), 16)
    opener = _OPENERS[seed % len(_OPENERS)]
    lines[opener_idx] = opener

    # Reduce hashtags to 2: find the hashtag line and trim it
    for i, line in enumerate(lines):
        tags = re.findall(r'#\S+', line)
        if len(tags) >= 2:
            lines[i] = " ".join(tags[:2])
            break

    text = "\n".join(lines).strip()
    return text[:MAX_TEXT_LEN]


def _create_session() -> tuple[str, str]:
    """Authenticate and return (access_jwt, did)."""
    resp = requests.post(
        f"{_BASE}/com.atproto.server.createSession",
        json={"identifier": _HANDLE, "password": _APP_PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["accessJwt"], data["did"]


def _upload_image(access_jwt: str, image_url: str) -> dict | None:
    """Download image from URL and upload to Bluesky. Returns blob ref or None."""
    try:
        img_resp = requests.get(image_url, timeout=15)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

        upload_resp = requests.post(
            f"{_BASE}/com.atproto.repo.uploadBlob",
            headers={
                "Authorization": f"Bearer {access_jwt}",
                "Content-Type":  content_type,
            },
            data=img_resp.content,
            timeout=30,
        )
        upload_resp.raise_for_status()
        return upload_resp.json()["blob"]
    except Exception as e:
        print(f"[Bluesky] Image upload failed: {e} — posting without image.")
        return None


def _fetch_ogp(url: str) -> dict | None:
    """Fetch OGP metadata from a URL. Returns dict with title/description/image or None."""
    try:
        resp = requests.get(url, timeout=10, allow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; AffiliateBotOGP/1.0)"})
        resp.raise_for_status()
        html = resp.text

        def _meta(prop: str) -> str:
            m = re.search(
                rf'<meta[^>]+(?:property|name)=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE
            ) or re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:{prop}["\']',
                html, re.IGNORECASE
            )
            return m.group(1).strip() if m else ""

        title       = _meta("title")
        description = _meta("description")
        image       = _meta("image")
        if not title and not description:
            return None
        return {"title": title, "description": description, "image": image}
    except Exception as e:
        print(f"[Bluesky] OGP fetch failed for {url}: {e}")
        return None


def _build_record(text: str, blob: dict | None, affiliate_url: str = "",
                  ogp: dict | None = None, access_jwt: str = "") -> dict:
    """Build the app.bsky.feed.post record dict.

    Priority:
    1. If affiliate_url + OGP data available → app.bsky.embed.external (link card)
    2. Else if blob available → app.bsky.embed.images
    3. Else no embed
    """
    record: dict = {
        "$type":     "app.bsky.feed.post",
        "text":      text,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    if affiliate_url and ogp:
        thumb_blob = None
        if ogp.get("image") and access_jwt:
            thumb_blob = _upload_image(access_jwt, ogp["image"])
        external: dict = {
            "uri":         affiliate_url,
            "title":       ogp.get("title", "")[:300],
            "description": ogp.get("description", "")[:1000],
        }
        if thumb_blob:
            external["thumb"] = thumb_blob
        record["embed"] = {
            "$type":    "app.bsky.embed.external",
            "external": external,
        }
    elif blob:
        record["embed"] = {
            "$type":  "app.bsky.embed.images",
            "images": [{"image": blob, "alt": ""}],
        }
    return record


def post_to_bluesky(post: dict, max_retries: int = 3,
                    retry_delay: int = 5) -> dict:
    """Post to Bluesky using the same post dict as threads_poster.

    Returns a result dict with keys: status, post_id, error.
    Falls back gracefully if BLUESKY_HANDLE / BLUESKY_APP_PASSWORD are not set.
    """
    if not _HANDLE or not _APP_PASSWORD:
        print("[Bluesky] BLUESKY_HANDLE / BLUESKY_APP_PASSWORD not set — skipping.")
        return {"status": "skipped", "post_id": None, "error": "credentials not set"}

    text      = _format_for_bluesky(post.get("post_text", post.get("title", "")))
    image_url = post.get("image_url", "")
    result    = {
        "title":     post.get("title", "")[:60],
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "status":    None,
        "post_id":   None,
        "error":     None,
    }

    import time

    affiliate_url = post.get("affiliate_url", "")
    ogp = _fetch_ogp(affiliate_url) if affiliate_url else None

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            access_jwt, did = _create_session()

            # Use image embed only when no link card is available
            blob = None
            if not ogp and image_url:
                blob = _upload_image(access_jwt, image_url)

            record = _build_record(text, blob, affiliate_url=affiliate_url,
                                   ogp=ogp, access_jwt=access_jwt)

            resp = requests.post(
                f"{_BASE}/com.atproto.repo.createRecord",
                headers={"Authorization": f"Bearer {access_jwt}"},
                json={
                    "repo":       did,
                    "collection": "app.bsky.feed.post",
                    "record":     record,
                },
                timeout=15,
            )
            resp.raise_for_status()
            uri = resp.json().get("uri", "")
            result["status"]  = "success"
            result["post_id"] = uri
            print(f"[OK] [Bluesky] Posted | uri={uri} (attempt {attempt}/{max_retries})")
            print(f"     {result['title']}")
            return result
        except requests.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"[RETRY {attempt}/{max_retries}] [Bluesky] {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[RETRY {attempt}/{max_retries}] [Bluesky] {last_error}")
        if attempt < max_retries:
            time.sleep(retry_delay)

    result["status"] = "error"
    result["error"]  = last_error
    print(f"[FAIL] [Bluesky] All {max_retries} attempts failed. Last error: {last_error}")
    return result


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from post_manager import get_next_post
    post = get_next_post()
    if not post:
        print("No pending posts in queue.")
        sys.exit(0)
    print(f"Posting to Bluesky: {post['title'][:60]}")
    r = post_to_bluesky(post)
    print(f"Status: {r['status']}")
    if r["error"]:
        print(f"Error : {r['error']}")
