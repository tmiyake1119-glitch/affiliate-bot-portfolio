import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from post_manager import get_next_post
from database import insert_post_log

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_DEFAULT_TOKEN   = os.getenv("THREADS_ACCESS_TOKEN")
_DEFAULT_USER_ID = os.getenv("THREADS_USER_ID", "me")


def _resolve(account: dict | None) -> tuple[str, str]:
    """Return (token, user_id) for the given account, falling back to env vars."""
    if account:
        token   = account.get("threads_token") or _DEFAULT_TOKEN
        user_id = account.get("threads_user_id") or _DEFAULT_USER_ID
    else:
        token, user_id = _DEFAULT_TOKEN, _DEFAULT_USER_ID
    return token, user_id


def _create_container(text: str, image_url: str = "",
                      token: str = None, user_id: str = None) -> str:
    """Step 1: Create a media container and return its creation_id."""
    token   = token   or _DEFAULT_TOKEN
    user_id = user_id or _DEFAULT_USER_ID
    url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    if image_url:
        resp = requests.post(url, params={
            "media_type":   "IMAGE",
            "image_url":    image_url,
            "text":         text,
            "access_token": token,
        }, timeout=15)
        resp.raise_for_status()
    else:
        resp = requests.post(url, params={
            "media_type":   "TEXT",
            "text":         text,
            "access_token": token,
        }, timeout=15)
        resp.raise_for_status()
    return resp.json()["id"]


def _publish_container(creation_id: str,
                       token: str = None, user_id: str = None) -> str:
    """Step 2: Publish the container and return the published post ID."""
    token   = token   or _DEFAULT_TOKEN
    user_id = user_id or _DEFAULT_USER_ID
    url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
    resp = requests.post(url, params={
        "creation_id":  creation_id,
        "access_token": token,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def _attempt_post(post: dict, token: str, user_id: str) -> str:
    """Try to create and publish a container. Returns post_id on success."""
    image_url = post.get("image_url", "")
    if image_url:
        try:
            creation_id = _create_container(post["post_text"], image_url,
                                            token=token, user_id=user_id)
        except requests.HTTPError as img_err:
            print(f"[WARN] Image post failed ({img_err.response.status_code}), falling back to text-only")
            creation_id = _create_container(post["post_text"],
                                            token=token, user_id=user_id)
    else:
        creation_id = _create_container(post["post_text"],
                                        token=token, user_id=user_id)
    return _publish_container(creation_id, token=token, user_id=user_id)


def post_to_threads(post: dict, max_retries: int = 3, retry_delay: int = 5,
                    account: dict | None = None) -> dict:
    """Post a queued item to Threads with retry logic. Returns a log entry.

    account: optional dict with keys 'name', 'threads_token', 'threads_user_id'.
             Falls back to env-var credentials when None or fields are empty.
    """
    token, user_id = _resolve(account)
    account_name   = account["name"] if account else "main"

    log_entry = {
        "title":        post["title"][:60],
        "genre":        post["genre"],
        "price":        post["price"],
        "url":          post["url"],
        "image_url":    post.get("image_url", ""),
        "ab_group":     post.get("ab_group", "A"),
        "account":      account_name,
        "posted_at":    datetime.now(timezone.utc).isoformat(),
        "status":       None,
        "post_id":      None,
        "error":        None,
    }
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            post_id = _attempt_post(post, token=token, user_id=user_id)
            log_entry["status"]  = "success"
            log_entry["post_id"] = post_id
            print(f"[OK] [{account_name}] Posted | post_id={post_id} (attempt {attempt}/{max_retries})")
            print(f"     {post['title'][:60]}")
            break
        except requests.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"[RETRY {attempt}/{max_retries}] [{account_name}] HTTP error: {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[RETRY {attempt}/{max_retries}] [{account_name}] {last_error}")
        if attempt < max_retries:
            time.sleep(retry_delay)
    else:
        log_entry["status"] = "error"
        log_entry["error"]  = last_error
        print(f"[FAIL] [{account_name}] All {max_retries} attempts failed. Last error: {last_error}")

    insert_post_log(log_entry)
    return log_entry


if __name__ == "__main__":
    post = get_next_post()
    if not post:
        print("No pending posts in queue.")
        sys.exit(0)

    print(f"Posting: [{post['genre']}] {post['title'][:60]}")
    print(f"Price  : {post['price']}")
    print()
    result = post_to_threads(post)
    print()
    print(f"Status : {result['status']}")
    if result["error"]:
        print(f"Error  : {result['error']}")
