import os
import sys
import tempfile
from datetime import datetime, timezone

import requests
import tweepy
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_BEARER_TOKEN        = os.getenv("X_BEARER_TOKEN", "")
_CONSUMER_KEY        = os.getenv("X_CONSUMER_KEY", "")
_CONSUMER_SECRET     = os.getenv("X_CONSUMER_SECRET", "")
_ACCESS_TOKEN        = os.getenv("X_ACCESS_TOKEN", "")
_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

MAX_TEXT_LEN = 280


def _get_clients() -> tuple[tweepy.Client, tweepy.API]:
    """Return (tweepy.Client for v2, tweepy.API for v1.1 media upload)."""
    auth = tweepy.OAuth1UserHandler(
        _CONSUMER_KEY, _CONSUMER_SECRET,
        _ACCESS_TOKEN, _ACCESS_TOKEN_SECRET,
    )
    api_v1 = tweepy.API(auth)
    client  = tweepy.Client(
        bearer_token=_BEARER_TOKEN,
        consumer_key=_CONSUMER_KEY,
        consumer_secret=_CONSUMER_SECRET,
        access_token=_ACCESS_TOKEN,
        access_token_secret=_ACCESS_TOKEN_SECRET,
    )
    return client, api_v1


def _upload_image(api_v1: tweepy.API, image_url: str) -> int | None:
    """Download image and upload via v1.1 media/upload. Returns media_id or None."""
    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/gif": ".gif", "image/webp": ".webp"}.get(content_type, ".jpg")

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        media = api_v1.media_upload(filename=tmp_path)
        os.unlink(tmp_path)
        return media.media_id
    except Exception as e:
        print(f"[X] Image upload failed: {e} — posting without image.")
        return None


def post_to_x(post: dict, max_retries: int = 3,
              retry_delay: int = 5) -> dict:
    """Post to X (Twitter) using the same post dict as threads_poster.

    Returns a result dict with keys: status, post_id, error.
    Falls back gracefully if credentials are not set.
    """
    if not all([_CONSUMER_KEY, _CONSUMER_SECRET, _ACCESS_TOKEN, _ACCESS_TOKEN_SECRET]):
        print("[X] Credentials not set — skipping.")
        return {"status": "skipped", "post_id": None, "error": "credentials not set"}

    text      = post.get("post_text", post.get("title", ""))[:MAX_TEXT_LEN]
    image_url = post.get("image_url", "")
    result    = {
        "title":     post.get("title", "")[:60],
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "status":    None,
        "post_id":   None,
        "error":     None,
    }

    import time

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            client, api_v1 = _get_clients()

            media_ids = None
            if image_url:
                media_id = _upload_image(api_v1, image_url)
                if media_id:
                    media_ids = [media_id]

            response = client.create_tweet(text=text, media_ids=media_ids)
            tweet_id = response.data["id"]
            result["status"]  = "success"
            result["post_id"] = tweet_id
            print(f"[OK] [X] Posted | tweet_id={tweet_id} (attempt {attempt}/{max_retries})")
            print(f"     {result['title']}")
            return result
        except tweepy.TweepyException as e:
            last_error = str(e)
            print(f"[RETRY {attempt}/{max_retries}] [X] TweepyException: {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[RETRY {attempt}/{max_retries}] [X] {last_error}")
        if attempt < max_retries:
            time.sleep(retry_delay)

    result["status"] = "error"
    result["error"]  = last_error
    print(f"[FAIL] [X] All {max_retries} attempts failed. Last error: {last_error}")
    return result


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    from post_manager import get_next_post
    post = get_next_post()
    if not post:
        print("No pending posts in queue.")
        sys.exit(0)
    print(f"Posting to X: {post['title'][:60]}")
    r = post_to_x(post)
    print(f"Status: {r['status']}")
    if r["error"]:
        print(f"Error : {r['error']}")
