import os
import sys
import logging
from typing import Optional

import anthropic
import tweepy

from news import fetch_news, NewsItem


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_POST_LENGTH = 1000
NG_WORDS: list[str] = []


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


def get_anthropic_client() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("環境変数が未設定です: ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


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


def generate_by_claude(prompt: str, max_tokens: int = 400) -> str:
    client = get_anthropic_client()
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text
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
- 180文字から260文字以内
- 金融クラスタ向けに専門的かつ簡潔に
- 図解風に、矢印・箇条書き・改行を使ってわかりやすく
- 数字・データがニュースタイトルに含まれる場合のみ使う
- ニュースにない数字は作らない
- 踏み込んだ予測もOK
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
        length_rule = "100文字から210文字以内"
    else:
        length_rule = "120文字から260文字以内"

    return f"""以下の金融ニュースを元に、Xに投稿する日本語のポストを1つ作成してください。

ニュース：
{item.title}

ソース：
{item.source}

条件：
- {length_rule}
- 日本の個人投資家・金融クラスタ向け
- 専門的だが、読みやすく簡潔に
- 株式市場、金利、為替、マクロ経済への影響を一言で説明
- 数字・データがニュースタイトルに含まれる場合のみ使う
- ニュースにない数字は作らない
- 断定的な予測もOK
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

おすすめの型：
【市場メモ】
本文

注目点：〇〇
"""


def generate_tweet_with_link(item: NewsItem) -> str:
    prompt = build_finance_prompt(item, with_link=True)
    text = generate_by_claude(prompt, max_tokens=400)
    safety_check(text)
    return f"{text}\n{item.url}"


def generate_tweet_without_link(item: NewsItem) -> str:
    prompt = build_finance_prompt(item, with_link=False)
    text = generate_by_claude(prompt, max_tokens=400)
    safety_check(text)
    return text


def generate_tweet_diagram(item: NewsItem) -> str:
    prompt = build_finance_prompt(item, diagram=True)
    text = generate_by_claude(prompt, max_tokens=500)
    safety_check(text)
    return text


def post_tweet(text: str) -> None:
    client = get_tweepy_client()
    try:
        response = client.create_tweet(text=text)
        logger.info(f"投稿成功: {response.data['id']}")
        logger.info(f"内容: {text}")
    except tweepy.TweepyException as e:
        logger.error(f"投稿失敗: {e}")
        raise


def create_tweet(mode: str, item: NewsItem) -> str:
    if mode == "link":
        logger.info("リンクあり投稿を生成中...")
        return generate_tweet_with_link(item)
    if mode == "diagram":
        logger.info("図解形式の投稿を生成中...")
        return generate_tweet_diagram(item)
    logger.info("リンクなし投稿を生成中...")
    return generate_tweet_without_link(item)


def main(mode: str = "diagram") -> None:
    logger.info(f"mode: {mode}")

    item: Optional[NewsItem] = fetch_news()
    if not item:
        logger.error("ニュース取得失敗")
        return

    logger.info(f"取得ニュース: {item.title}")
    logger.info(f"ソース: {item.source}")

    tweet = create_tweet(mode, item)

    if mode in ["post", "normal", "link", "diagram"]:
        post_tweet(tweet)
        return

    logger.error(f"不明なmodeです: {mode}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "diagram"
    main(mode)
