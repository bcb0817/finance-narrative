import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional
import urllib.request
import urllib.error

try:
    import feedparser
except ImportError:
    raise ImportError("feedparser が必要です: pip install feedparser")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

RSS_FEEDS: list[dict[str, str | int]] = [
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "market_news", "priority": 3},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "category": "market_news", "priority": 3},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news.rss", "category": "market_news", "priority": 3},
    {"name": "Fed Monetary Policy", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "category": "central_bank", "priority": 5},
    {"name": "Fed Speeches", "url": "https://www.federalreserve.gov/feeds/speeches.xml", "category": "central_bank", "priority": 4},
    {"name": "Seeking Alpha", "url": "https://seekingalpha.com/feed.xml", "category": "market_news", "priority": 3},
    {"name": "Benzinga", "url": "https://www.benzinga.com/feed", "category": "market_news", "priority": 3},
    {"name": "St. Louis Fed Blog", "url": "https://fredblog.stlouisfed.org/feed", "category": "macro_data", "priority": 4},
]

FINANCE_KEYWORDS: list[str] = [
    "株", "stock", "market", "Fed", "GDP", "inflation", "金利", "interest rate",
    "bitcoin", "crypto", "円", "yen", "dollar", "euro", "oil", "gold",
    "earnings", "決算", "recession", "利上げ", "利下げ", "bond", "yield",
    "nasdaq", "dow", "s&p", "nikkei", "日経", "rate", "policy", "monetary",
    "ECB", "BOJ", "CPI", "PPI", "employment", "jobs", "trade"
]


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: str
    category: str = "market_news"
    priority: int = 3

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": self.published,
            "category": self.category,
            "priority": self.priority,
        }


def fetch_feed(feed: dict) -> list[NewsItem]:
    """1つのRSSフィードからニュースを取得する"""
    items: list[NewsItem] = []
    name = feed["name"]
    url = feed["url"]
    category = str(feed.get("category", "market_news"))
    priority = int(feed.get("priority", 3))

    try:
        logger.info(f"{name} を取得中")
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
                published=published,
                category=category,
                priority=priority,
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


def is_recent(item: NewsItem, hours: int = 24) -> bool:
    """24時間以内のニュースかチェック"""
    if not item.published:
        return True
    try:
        pub_date = parsedate_to_datetime(item.published)
        pub_date = pub_date.astimezone(timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return pub_date >= cutoff
    except Exception:
        return True


def score_item(item: NewsItem) -> float:
    """優先度とキーワードでスコアリング"""
    score = float(item.priority)
    text = (item.title + " " + item.source).lower()
    for keyword in FINANCE_KEYWORDS:
        if keyword.lower() in text:
            score += 0.5
    return score


def select_best_item(
    items: list[NewsItem],
    posted_urls: set[str] | None = None,
) -> Optional[NewsItem]:
    """スコアが高いニュースの中からランダムに1件選ぶ"""
    if not items:
        return None

    posted_urls = posted_urls or set()
    available = [item for item in items if item.url not in posted_urls]
    if not available:
        logger.warning("未投稿のニュースがありません")
        return None

    excluded = len(items) - len(available)
    if excluded:
        logger.info(f"投稿済み {excluded} 件を除外しました")

    # 24時間以内に絞る
    recent = [item for item in available if is_recent(item, hours=24)]

    if not recent:
        logger.warning("24時間以内の未投稿ニュースなし。全未投稿件から選択します")
        recent = available

    scored = sorted(recent, key=lambda x: score_item(x), reverse=True)
    top = scored[:5]
    selected = random.choice(top)
    logger.info(f"選択: [{selected.source}] [{selected.category}] {selected.title}")
    return selected


def fetch_news(posted_urls: set[str] | None = None) -> Optional[NewsItem]:
    """全フィードからニュースを取得して1件返す（data/posted_history.json のURLは除外）"""
    if posted_urls is None:
        from posted_history import get_posted_urls
        posted_urls = get_posted_urls()

    all_items: list[NewsItem] = []

    for feed in RSS_FEEDS:
        items = fetch_feed(feed)
        all_items.extend(items)

    logger.info(f"合計取得件数: {len(all_items)}件")

    unique_items = deduplicate(all_items)
    logger.info(f"重複除去後: {len(unique_items)}件")

    return select_best_item(unique_items, posted_urls=posted_urls)


def main() -> None:
    item = fetch_news()
    if item:
        print(json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
    else:
        logger.error("ニュースを取得できませんでした")


if __name__ == "__main__":
    main()
