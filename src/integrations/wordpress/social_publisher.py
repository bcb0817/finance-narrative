"""Publish newly created or modified WordPress articles to X and Threads."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

import requests

from common.runtime import load_env, state_dir
logger = logging.getLogger(__name__)
STATE_NAME = "wordpress_social_publications.json"


def _enabled(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}


def _state_path() -> Path:
    return state_dir() / STATE_NAME


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"initialized": False, "articles": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"initialized": False, "articles": {}}
    except (OSError, json.JSONDecodeError):
        return {"initialized": False, "articles": {}}


def _save_state(value: dict[str, Any]) -> None:
    path = _state_path(); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _wp_posts(limit: int = 20) -> list[dict[str, Any]]:
    base = os.environ["WORDPRESS_BASE_URL"].rstrip("/")
    response = requests.get(
        base + "/",
        params={"rest_route": "/wp/v2/posts", "status": "publish", "orderby": "modified", "order": "desc", "per_page": limit},
        auth=(os.environ["WORDPRESS_USERNAME"], os.environ["WORDPRESS_APP_PASSWORD"]),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("WordPress posts endpoint returned a non-list payload")
    return [post for post in payload if isinstance(post, dict)]


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _signature(post: dict[str, Any]) -> str:
    raw = f"{post.get('id')}|{post.get('modified_gmt')}|{post.get('link')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _texts(post: dict[str, Any]) -> tuple[str, str]:
    title = _plain(post.get("title", {}).get("rendered", ""))
    excerpt = _plain(post.get("excerpt", {}).get("rendered", ""))
    url = str(post.get("link") or "").strip()
    short_excerpt = excerpt[:120].rstrip("、。，,. ")
    x_text = f"【ブログ更新】\n{title}\n\n{url}\n\n#日本株 #米国株 #投資"
    if len(x_text) > 275:
        allowance = max(30, 275 - len(x_text) + len(title))
        x_text = f"【ブログ更新】\n{title[:allowance]}…\n\n{url}\n\n#投資"
    threads_text = f"【ブログ更新】\n{title}"
    if short_excerpt:
        threads_text += f"\n\n{short_excerpt}"
    threads_text += f"\n\n{url}\n\n#日本株 #米国株 #投資"
    return x_text, threads_text[:490]


def post_to_threads(text: str) -> str:
    user_id = os.getenv("THREADS_USER_ID", "").strip()
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    if not user_id or not token:
        raise RuntimeError("Threads credentials are not configured")
    version = os.getenv("THREADS_GRAPH_VERSION", "v1.0").strip() or "v1.0"
    base = f"https://graph.threads.net/{version}/{user_id}"
    create = requests.post(base + "/threads", data={"media_type": "TEXT", "text": text, "access_token": token}, timeout=30)
    create.raise_for_status(); creation_id = str(create.json().get("id") or "")
    if not creation_id:
        raise RuntimeError("Threads container creation returned no id")
    publish = requests.post(base + "/threads_publish", data={"creation_id": creation_id, "access_token": token}, timeout=30)
    publish.raise_for_status(); post_id = str(publish.json().get("id") or "")
    if not post_id:
        raise RuntimeError("Threads publish returned no id")
    return post_id


def post_to_x(text: str) -> str:
    from common.x_client import post_tweet
    return post_tweet(text)


def sync(*, initialize_only: bool = False) -> dict[str, Any]:
    load_env()
    if not _enabled("BLOG_SOCIAL_AUTO_POST_ENABLED"):
        return {"status": "disabled", "posted": 0}
    try:
        posts = _wp_posts()
    except (requests.RequestException, ValueError) as exc:
        # WordPress is an optional surface; an HTML or empty response must not
        # turn the two-minute scheduler into a daemon failure loop.
        logger.warning("WordPress article fetch unavailable: %s", type(exc).__name__)
        return {"status": "source_unavailable", "posted": 0, "reason": type(exc).__name__}
    state = _load_state(); articles = state.setdefault("articles", {})
    if not state.get("initialized") or initialize_only:
        for post in posts:
            articles[str(post["id"])] = {"signature": _signature(post), "baseline": True}
        state.update({"initialized": True, "initialized_at": datetime.now(timezone.utc).isoformat()}); _save_state(state)
        return {"status": "initialized", "baseline_count": len(posts), "posted": 0}
    posted = 0; pending = 0
    for post in reversed(posts):
        key = str(post["id"]); signature = _signature(post); row = articles.get(key, {})
        if row.get("signature") == signature and row.get("baseline"):
            continue
        if row.get("signature") != signature:
            row = {"signature": signature, "modified_gmt": post.get("modified_gmt"), "url": post.get("link"), "channels": {}}
        channels = row.setdefault("channels", {}); x_text, threads_text = _texts(post)
        if _enabled("BLOG_SOCIAL_X_ENABLED", True) and not channels.get("x_id"):
            try:
                x_id = post_to_x(x_text)
                if x_id: channels["x_id"] = x_id; posted += 1
                else: channels["x_error"] = "posting skipped by safety or runtime gate"; pending += 1
            except Exception as exc:
                channels["x_error"] = type(exc).__name__; pending += 1; logger.exception("WordPress article X publish failed")
        if _enabled("BLOG_SOCIAL_THREADS_ENABLED", True) and not channels.get("threads_id"):
            try:
                channels["threads_id"] = post_to_threads(threads_text); posted += 1
            except Exception as exc:
                channels["threads_error"] = type(exc).__name__; pending += 1; logger.warning("WordPress article Threads publish pending: %s", type(exc).__name__)
        row["checked_at"] = datetime.now(timezone.utc).isoformat(); articles[key] = row; _save_state(state)
    return {"status": "ok", "posted": posted, "pending": pending, "checked": len(posts)}


def status() -> dict[str, Any]:
    load_env(); state = _load_state()
    return {
        "enabled": _enabled("BLOG_SOCIAL_AUTO_POST_ENABLED"),
        "x_enabled": _enabled("BLOG_SOCIAL_X_ENABLED", True),
        "threads_enabled": _enabled("BLOG_SOCIAL_THREADS_ENABLED", True),
        "threads_credentials_configured": bool(os.getenv("THREADS_USER_ID", "").strip() and os.getenv("THREADS_ACCESS_TOKEN", "").strip()),
        "initialized": bool(state.get("initialized")), "tracked_articles": len(state.get("articles", {})),
    }
