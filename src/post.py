import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import tweepy
from openai import OpenAI

from news import fetch_news, NewsItem
from posted_history import add_posted_entry, get_posted_urls


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


# =========================
# 設定
# =========================

OPENAI_GENERATE_MODEL = os.getenv("OPENAI_GENERATE_MODEL", "gpt-5-mini")
OPENAI_REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5-nano")

MAX_POST_LENGTH = 280

JST = timezone(timedelta(hours=9))

NG_WORDS: list[str] = [
    "今すぐ買え",
    "今すぐ売れ",
]

PROMPT_SAFETY_RULES = """
- ニュースにない数字や事実は作らない"""


# =========================
# クライアント
# =========================

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


def get_openai_client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("環境変数が未設定です: OPENAI_API_KEY")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


# =========================
# 深夜投稿ガード
# =========================

def is_night_time_jst() -> bool:
    """JST 00:00〜04:29 は投稿禁止時間帯"""
    now_jst = datetime.now(JST)
    minutes = now_jst.hour * 60 + now_jst.minute
    return 0 <= minutes < (4 * 60 + 30)


# =========================
# テキスト処理
# =========================

def clean_text(text: str) -> str:
    text = text.strip()
    if text.startswith("「") and text.endswith("」"):
        text = text[1:-1].strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    return text


def safety_check(text: str) -> None:
    if not text or not text.strip():
        raise ValueError("投稿本文が空です")
    if len(text) > MAX_POST_LENGTH:
        raise ValueError(f"投稿本文が長すぎます: {len(text)}文字")
    for word in NG_WORDS:
        if word in text:
            raise ValueError(f"NGワードを検出しました: {word}")


# =========================
# 生成（OpenAI）
# =========================

def generate_by_openai(prompt: str, max_tokens: int = 500) -> str:
    client = get_openai_client()
    response = client.chat.completions.create(
        model=OPENAI_GENERATE_MODEL,
        messages=
