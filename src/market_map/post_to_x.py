"""X(Twitter)への投稿。他Botと同じ認証情報を使う。

標準の環境変数（.env / README 準拠）:
    API_KEY / API_KEY_SECRET / ACCESS_TOKEN / ACCESS_TOKEN_SECRET

互換のため、旧 X_API_KEY 系もフォールバックとして読む。

メディア付き投稿は v1.1(media_upload)で画像をアップロードし、
v2(create_tweet)でツイートする方式。
POST_ENABLED=true のときだけ実投稿する（ローカル運用の安全弁）。
"""
from __future__ import annotations

import logging
import os

import tweepy

logger = logging.getLogger(__name__)


def _post_enabled() -> bool:
    return os.environ.get("POST_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _env(primary: str, fallback: str) -> str:
    """標準名を優先し、旧 X_ 系をフォールバックで読む。どちらも無ければ明示エラー。"""
    val = os.environ.get(primary) or os.environ.get(fallback)
    if not val:
        raise RuntimeError(f"環境変数が未設定です: {primary}（旧 {fallback} も可）")
    return val


def _clients():
    api_key = _env("API_KEY", "X_API_KEY")
    api_secret = _env("API_KEY_SECRET", "X_API_SECRET")
    access_token = _env("ACCESS_TOKEN", "X_ACCESS_TOKEN")
    access_secret = _env("ACCESS_TOKEN_SECRET", "X_ACCESS_TOKEN_SECRET")

    # メディアアップロードは v1.1 API が必要
    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    api_v1 = tweepy.API(auth)

    client_v2 = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    return api_v1, client_v2


def post_to_x(caption: str, image_path: str | None = None) -> str:
    """caption(と任意で画像)を投稿し、tweet_id を返す。

    image_path が None の場合はテキストのみ投稿する。
    POST_ENABLED=false の場合は投稿直前で止め、空文字を返す（履歴保存もしない前提）。
    """
    if not _post_enabled():
        logger.info("[INFO] POST_ENABLED=false -> X posting skipped")
        logger.info("（未投稿のキャプション）: %s / image=%s", caption, image_path)
        return ""

    api_v1, client_v2 = _clients()

    media_ids = None
    if image_path:
        media = api_v1.media_upload(filename=image_path)
        media_ids = [media.media_id]

    resp = client_v2.create_tweet(text=caption, media_ids=media_ids)
    tweet_id = resp.data.get("id")
    logger.info("投稿完了: tweet_id=%s", tweet_id)
    return str(tweet_id)
