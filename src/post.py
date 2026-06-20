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


OPENAI_GENERATE_MODEL = os.getenv("OPENAI_GENERATE_MODEL", "gpt-5-mini")
OPENAI_REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5-nano")

MAX_POST_LENGTH = 280

JST = timezone(timedelta(hours=9))

NG_WORDS: list[str] = [
    "絶対",
    "確実",
    "爆益",
    "爆上げ",
    "暴落確定",
    "急騰確定",
    "今すぐ買え",
    "今すぐ売れ",
    "買い一択",
    "売り一択",
    "買うべき",
    "売るべき",
    "必ず上がる",
    "必ず下がる",
    "テンバガー確定",
]

PROMPT_SAFETY_RULES = """
- ニュースにない数字や事実は作らない"""


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


def is_night_time_jst() -> bool:
    """JST 00:00〜04:29 は投稿禁止時間帯"""
    now_jst = datetime.now(JST)
    minutes = now_jst.hour * 60 + now_jst.minute
    return 0 <= minutes < (4 * 60 + 30)


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


def generate_by_openai(prompt: str, max_tokens: int = 500) -> str:
    client = get_openai_client()
    response = client.chat.completions.create(
        model=OPENAI_GENERATE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=max_tokens,
    )
    text = response.choices[0].message.content or ""
    return clean_text(text)


def build_finance_prompt(
    item: NewsItem,
    *,
    with_link: bool = False,
    diagram: bool = False,
) -> str:
    if diagram:
        return f"""以下の金融ニュースを元に、Xに投稿する日本語の図解風ポストを1つ作成してください。

ニュース：
{item.title}

ソース：
{item.source}

条件：
- 180文字から240文字以内
- 金融クラスタ向けに専門的かつ簡潔に
- 図解風に、矢印・箇条書き・改行を使ってわかりやすく
- 数字・データがニュースタイトルに含まれる場合のみ使う
{PROMPT_SAFETY_RULES}
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

型の例：
【市場メモ】
材料：〇〇
　↓
市場の見方：〇〇
　↓
注目点：〇〇

#株式市場 #米国株
"""

    if with_link:
        length_rule = "100文字から180文字以内（URLは別行で付けるため短めに）"
    else:
        length_rule = "120文字から240文字以内"

    return f"""以下の金融ニュースを元に、Xに投稿する日本語のポストを1つ作成してください。

ニュース：
{item.title}

ソース：
{item.source}

条件：
- {length_rule}
- 日本の個人投資家・金融クラスタ向け
- 専門的だが、読みやすく簡潔に
- 株式市場、金利、為替、マクロ経済への影響を中立的に説明
- 数字・データがニュースタイトルに含まれる場合のみ使う
{PROMPT_SAFETY_RULES}
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

おすすめの型：
【市場メモ】
本文

注目点：〇〇
"""


def review_tweet_with_openai(text: str, news_title: str, source: str) -> dict:
    """投稿前にAIで内容をレビューし、投稿可否をJSONで返す"""
    client = get_openai_client()

    review_prompt = f"""あなたは金融SNS投稿のコンプライアンス審査担当です。
以下のX投稿文を審査し、投稿してよいか判断してください。

【元ニュース】
タイトル: {news_title}
ソース: {source}

【審査対象の投稿文】
{text}

【審査基準】
- ニュースにない数字や事実を捏造していないか
- 誤解を招く内容でないか

以下のJSON形式のみで返答してください。説明文は不要です。
{{
  "ok_to_post": true,
  "risk_level": "low",
  "reason": "投稿してよい理由またはNG理由",
  "contains_investment_advice": false,
  "contains_buy_sell_recommendation": false,
  "contains_unverified_numbers": false,
  "too_aggressive": false
}}

risk_level は "low" / "medium" / "high" のいずれかにしてください。"""

    try:
        response = client.chat.completions.create(
            model=OPENAI_REVIEW_MODEL,
            messages=[{"role": "user", "content": review_prompt}],
            max_completion_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"レビュー結果のJSONパース失敗: {e}")
        return {
            "ok_to_post": False,
            "risk_level": "high",
            "reason": f"レビュー結果のパースに失敗: {e}",
            "contains_investment_advice": False,
            "contains_buy_sell_recommendation": False,
            "contains_unverified_numbers": False,
            "too_aggressive": False,
        }
    except Exception as e:
        logger.error(f"レビューAPI呼び出し失敗: {e}")
        return {
            "ok_to_post": False,
            "risk_level": "high",
            "reason": f"レビューAPIエラー: {e}",
            "contains_investment_advice": False,
            "contains_buy_sell_recommendation": False,
            "contains_unverified_numbers": False,
            "too_aggressive": False,
        }

    return result


def generate_tweet_with_link(item: NewsItem) -> str:
    prompt = build_finance_prompt(item, with_link=True)
    text = generate_by_openai(prompt, max_tokens=500)
    return f"{text}\n{item.url}"


def generate_tweet_without_link(item: NewsItem) -> str:
    prompt = build_finance_prompt(item, with_link=False)
    return generate_by_openai(prompt, max_tokens=500)


def generate_tweet_diagram(item: NewsItem) -> str:
    prompt = build_finance_prompt(item, diagram=True)
    return generate_by_openai(prompt, max_tokens=600)


def create_tweet(mode: str, item: NewsItem) -> str:
    if mode == "link":
        logger.info("リンクあり投稿を生成中...")
        return generate_tweet_with_link(item)
    if mode == "diagram":
        logger.info("図解形式の投稿を生成中...")
        return generate_tweet_diagram(item)
    logger.info("リンクなし投稿を生成中...")
    return generate_tweet_without_link(item)


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


def main(mode: str = "dry-run") -> None:
    logger.info(f"mode: {mode}")

    if mode != "dry-run" and is_night_time_jst():
        now_jst = datetime.now(JST).strftime("%H:%M")
        logger.info(f"深夜帯（JST {now_jst}）のため投稿をスキップします（00:00〜04:29は禁止）")
        return

    posted_urls = get_posted_urls()
    logger.info(f"投稿済みURL数: {len(posted_urls)}")

    item: Optional[NewsItem] = fetch_news(posted_urls=posted_urls)
    if not item:
        logger.error("ニュース取得失敗")
        return

    logger.info(f"取得ニュース: {item.title}")
    logger.info(f"ソース: {item.source}")

    tweet = create_tweet(mode, item)

    try:
        safety_check(tweet)
    except ValueError as e:
        logger.error(f"safety_check NG: {e}")
        logger.info(f"投稿スキップ。生成文:\n{tweet}")
        return

    review = review_tweet_with_openai(tweet, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")

    if not review.get("ok_to_post", False):
        logger.warning(f"AIレビューにより投稿中止: {review.get('reason', '理由なし')}")
        logger.info(f"投稿スキップ。生成文:\n{tweet}")
        return

    if mode == "dry-run":
        logger.info("=== DRY RUN: 投稿はしません ===")
        logger.info(f"文字数: {len(tweet)}")
        logger.info(f"内容:\n{tweet}")
        return

    if mode in ["post", "normal", "link", "diagram"]:
        tweet_id = post_tweet(tweet)
        add_posted_entry(item, tweet_id=tweet_id, mode=mode)
        return

    logger.error(f"不明なmodeです: {mode}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry-run"
    main(mode)
