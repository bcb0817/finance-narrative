"""X API client with shared posting and budget guardrails."""
from __future__ import annotations

import logging
import os

import tweepy

try:
    from runtime import post_enabled
    from post_registry import record_post
    from posting_policy import check_post
except ImportError:  # pragma: no cover
    from common.runtime import post_enabled
    from common.post_registry import record_post
    from common.posting_policy import check_post

logger = logging.getLogger(__name__)


def _required_credentials() -> None:
    for key in ("API_KEY", "API_KEY_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET"):
        if not os.getenv(key):
            raise RuntimeError(f"Missing environment variable: {key}")


def get_tweepy_client() -> tweepy.Client:
    _required_credentials()
    return tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )


def get_tweepy_api_v1() -> tweepy.API:
    _required_credentials()
    auth = tweepy.OAuth1UserHandler(
        os.environ["API_KEY"], os.environ["API_KEY_SECRET"],
        os.environ["ACCESS_TOKEN"], os.environ["ACCESS_TOKEN_SECRET"],
    )
    return tweepy.API(auth)


def _approved(text: str) -> bool:
    decision = check_post(text)
    if not decision.allowed:
        logger.warning("X post blocked by policy: %s", decision.reason)
    return decision.allowed


def post_tweet(text: str) -> str:
    if not post_enabled():
        logger.info("POST_ENABLED=false -> X posting skipped")
        return ""
    if not _approved(text):
        return ""
    try:
        response = get_tweepy_client().create_tweet(text=text)
        tweet_id = str(response.data["id"])
        record_post(tweet_id, text=text)
        logger.info("X post created: %s", tweet_id)
        return tweet_id
    except tweepy.TweepyException:
        logger.exception("X post failed")
        raise


def post_tweet_with_image(text: str, image_path: str) -> str:
    if not post_enabled():
        logger.info("POST_ENABLED=false -> X image posting skipped")
        return ""
    if not _approved(text):
        return ""
    try:
        media = get_tweepy_api_v1().media_upload(filename=image_path)
        response = get_tweepy_client().create_tweet(text=text, media_ids=[media.media_id])
        tweet_id = str(response.data["id"])
        record_post(tweet_id, text=text, extra={"has_media": True})
        logger.info("X image post created: %s", tweet_id)
        return tweet_id
    except tweepy.TweepyException:
        logger.exception("X image post failed")
        raise


def post_tweet_thread_with_image(
    first_text: str,
    image_path: str,
    reply_texts: list[str],
) -> list[str]:
    """Post an image parent; replies are disabled by default to protect budget."""
    try:
        from safety import safety_check
    except ImportError:  # pragma: no cover
        from common.safety import safety_check

    if not post_enabled():
        logger.info("POST_ENABLED=false -> X thread posting skipped")
        return []
    try:
        safety_check(first_text)
    except ValueError as exc:
        logger.error("Thread parent failed safety check: %s", exc)
        return []
    if not _approved(first_text):
        return []

    media = get_tweepy_api_v1().media_upload(filename=image_path)
    client = get_tweepy_client()
    response = client.create_tweet(text=first_text, media_ids=[media.media_id])
    parent_id = str(response.data["id"])
    record_post(parent_id, text=first_text, extra={"has_media": True, "thread_parent": True})
    ids = [parent_id]

    threads_enabled = os.getenv("THREADS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    if not threads_enabled:
        logger.info("Thread replies omitted because THREADS_ENABLED=false")
        return ids

    previous_id = parent_id
    for index, reply in enumerate(reply_texts, 1):
        try:
            safety_check(reply)
        except ValueError as exc:
            logger.warning("Reply %d failed safety check: %s", index, exc)
            continue
        if not _approved(reply):
            break
        try:
            result = client.create_tweet(text=reply, in_reply_to_tweet_id=previous_id)
            reply_id = str(result.data["id"])
            record_post(reply_id, text=reply, extra={"reply_to": previous_id})
            ids.append(reply_id)
            previous_id = reply_id
        except tweepy.TweepyException as exc:
            logger.warning("Reply %d failed: %s", index, exc)
    return ids
