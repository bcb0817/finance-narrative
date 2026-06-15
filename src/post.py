import os
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


def get_tweepy_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )


def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def generate_tweet_with_link(item: NewsItem) -> str:
    """リンクあり投稿を生成"""
    client = get_anthropic_client()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下の金融ニュースを元に、Xに投稿する日本語のツイートを1つ作成してください。

ニュース：{item.title}
ソース：{item.source}

条件：
- 100文字から250文字の間で、内容に適した文字数で書く（URLは別途追加されるため本文のみ）
- 金融クラスタ向けに専門的かつ簡潔に
- 数字・データがあれば積極的に使う
- ハッシュタグを2個つける（例：#株式市場 #米国株）
- ツイート本文のみ返答すること（URLは含めない）"""
        }]
    )
    text = message.content[0].text.strip()
    return f"{text}\n{item.url}"


def generate_tweet_without_link(item: NewsItem) -> str:
    """リンクなし投稿を生成"""
    client = get_anthropic_client()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下の金融ニュースを元に、Xに投稿する日本語のツイートを1つ作成してください。

ニュース：{item.title}
ソース：{item.source}

条件：
- 100文字から300文字の間で、内容に適した文字数で書く
- 金融クラスタ向けに専門的かつ簡潔に
- 数字・データがあれば積極的に使う
- ハッシュタグを2個つける（例：#株式市場 #米国株）
- ツイート本文のみ返答すること"""
        }]
    )
    return message.content[0].text.strip()


def generate_tweet_diagram(item: NewsItem) -> str:
    """図解形式の投稿を生成"""
    client = get_anthropic_client()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""以下の金融ニュースを元に、Xに投稿する図解形式のツイートを1つ作成してください。

ニュース：{item.title}
ソース：{item.source}

条件：
- 200文字〜500文字
- 以下のような図解・矢印・箇条書きを使って視覚的にわかりやすく
  例：
  【タイトル】
  原因 → 結果
  　↓
  影響① 〇〇
  影響② 〇〇
  　↓
  結論：〇〇
- 金融市場に対する影響を専門的に
- ツイート本文のみ返答すること"""
        }]
    )
    return message.content[0].text.strip()


def post_tweet(text: str) -> None:
    client = get_tweepy_client()
    try:
        response = client.create_tweet(text=text)
        logger.info(f"投稿成功: {response.data['id']}")
        logger.info(f"内容: {text}")
    except tweepy.TweepyException as e:
        logger.error(f"投稿失敗: {e}")
        raise


def main(mode: str = "test") -> None:
    if mode == "test":
        logger.info("テストモードで投稿中...")
        post_tweet("世界が平和になりますように🕊️")
        return

    item = fetch_news()
    if not item:
        logger.error("ニュース取得失敗")
        return

    if mode == "link":
        logger.info("リンクあり投稿を生成中...")
        tweet = generate_tweet_with_link(item)
    elif mode == "diagram":
        logger.info("図解形式の投稿を生成中...")
        tweet = generate_tweet_diagram(item)
    else:
        logger.info("リンクなし投稿を生成中...")
        tweet = generate_tweet_without_link(item)

    post_tweet(tweet)


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    main(mode)
