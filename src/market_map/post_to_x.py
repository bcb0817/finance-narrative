"""X(Twitter)への投稿。既存Botと同じ認証情報を想定。

必要な環境変数(ワークフロー側で既存Secretをマッピングする):
    X_API_KEY
    X_API_SECRET
    X_ACCESS_TOKEN
    X_ACCESS_TOKEN_SECRET

メディア付き投稿は v1.1(media_upload)で画像をアップロードし、
v2(create_tweet)でツイートする方式。
"""
from __future__ import annotations

import logging
import os

import tweepy

logger = logging.getLogger(__name__)


def _clients():
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

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
    """
    api_v1, client_v2 = _clients()

    media_ids = None
    if image_path:
        media = api_v1.media_upload(filename=image_path)
        media_ids = [media.media_id]

    resp = client_v2.create_tweet(text=caption, media_ids=media_ids)
    tweet_id = resp.data.get("id")
    logger.info("投稿完了: tweet_id=%s", tweet_id)
    return str(tweet_id)
