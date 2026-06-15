import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import urllib.request
import urllib.error

# feedparserはrequirements.txtに追加必要
try:
    import feedparser
except ImportError:
    raise ImportError("feedparser が必要です: pip install feedparser")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# RSSフィード一覧
RSS_FEEDS: list[dict] = [
    {
        "name": "Reuters Markets",
        "url": "https://feeds.reuters.com/reuters/businessNews"
    },
    {
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rssindex"
    },
    {
        "name": "MarketWatch",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/"
    },
    {
        "name": "CNBC Markets",
        "url": "https://search.cnbc.com/rs/search/combinedcombined/view/ajaxData.aspx?partnerId=1&categorytype=type&type=rss&rss=1"
    },
]

# 金融クラスタ向けキーワード
FINANCE_KEYWORDS: list[str] = [
    "株", "stock", "market", "Fed", "GDP", "inflation", "金利", "interest rate",
    "bitcoin", "crypto", "円", "yen", "dollar", "euro", "oil", "gold",
    "earnings", "決算", "recession", "利上げ", "利下げ", "bond", "yield",
    "nasdaq", "dow", "s&p", "nikkei", "日経"
]


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: str

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": self.published
        }


def fetch_feed(feed: dict) -> list[NewsItem]:
    """1つのRSSフィードからニュースを取得する"""
    items: list[NewsItem] = []
    name = feed["name"]
    url = feed["url"]

    try:
        logger.info(f"{name} を取得中: {url}")
        parsed = feedparser.parse(url)

        if parsed.bozo:
            logger.warning(f"{name}: フィードの解析に問題があります")

        for entry in parsed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            published = entry.get("published", "") or entry.get("updated", "")

            if not title or not link:
                continue

            items.append(NewsItem(
                title=title,
                url=link,
                source=name,
                published=published
            ))

        logger.info(f"{name}: {len(items)}件取得")

    except Exception as e:
        logger.error(f"{name} の取得に失敗しました: {e}")

    return items


def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """URLで重複除去する"""
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            unique.append(item)
    return unique


def score_item(item: NewsItem) -> int:
    """金融クラスタ向けスコアリング"""
    score = 0
    text = (item.title + " " + item.source).lower()
    for keyword in FINANCE_KEYWORDS:
        if keyword.lower() in text:
            score += 1
    return score


def select_best_item(items: list[NewsItem]) -> Optional[NewsItem]:
    """スコアが高いニュースの中からランダムに1件選ぶ"""
    if not items:
        return None

    # スコアでソート
    scored = sorted(items, key=lambda x: score_item(x), reverse=True)

    # 上位5件からランダムに選ぶ
    top = scored[:5]
    selected = random.choice(top)
    logger.info(f"選択: [{selected.source}] {selected.title}")
    return selected


def fetch_news() -> Optional[NewsItem]:
    """全フィードからニュースを取得して1件返す"""
    all_items: list[NewsItem] = []

    for feed in RSS_FEEDS:
        items = fetch_feed(feed)
        all_items.extend(items)

    logger.info(f"合計取得件数: {len(all_items)}件")

    # 重複除去
    unique_items = deduplicate(all_items)
    logger.info(f"重複除去後: {len(unique_items)}件")

    # 最良の1件を選ぶ
    return select_best_item(unique_items)


def main() -> None:
    item = fetch_news()
    if item:
        print(json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
    else:
        logger.error("ニュースを取得できませんでした")


if __name__ == "__main__":
    main()
