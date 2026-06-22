"""
common/x_client.py
X(Twitter)投稿の共通処理（tweepyクライアント生成・テキスト投稿・画像つき投稿）。
"""

import os
import logging

import tweepy

logger = logging.getLogger(__name__)


def get_tweepy_client() -> tweepy.Client:
    required_envs = [
        "API_KEY",
        "API_KEY_SECRET",
        "ACCESS_TOKEN",
        "ACCESS_TOKEN_SECRET",
    ]
    for key in required_envs:
        if not os.getenv(key):
            raise RuntimeError(f"環境変数が未設定です: {key}")

    return tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )


def get_tweepy_api_v1() -> tweepy.API:
    """画像アップロード用の v1.1 クライアント（OAuth 1.0a）"""
    auth = tweepy.OAuth1UserHandler(
        os.environ["API_KEY"], os.environ["API_KEY_SECRET"],
        os.environ["ACCESS_TOKEN"], os.environ["ACCESS_TOKEN_SECRET"],
    )
    return tweepy.API(auth)


def post_tweet(text: str) -> str:
    client = get_tweepy_client()
    try:
        response = client.create_tweet(text=text)
        tweet_id = str(response.data["id"])
        logger.info(f"投稿成功: {tweet_id}")
        logger.info(f"内容: {text}")
        return tweet_id
    except tweepy.TweepyException as e:
        logger.error(f"投稿失敗: {e}")
        raise


def post_tweet_with_image(text: str, image_path: str) -> str:
    api_v1 = get_tweepy_api_v1()
    media = api_v1.media_upload(filename=image_path)   # v1.1 でアップ
    client = get_tweepy_client()                       # v2 で投稿
    response = client.create_tweet(text=text, media_ids=[media.media_id])
    tweet_id = str(response.data["id"])
    logger.info(f"画像つき投稿成功: {tweet_id}")
    logger.info(f"キャプション: {text}")
    return tweet_id
