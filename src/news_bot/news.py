import os
import re
import json
import difflib
import logging
import random
from dataclasses import dataclass, field
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


# =========================================================
# User-Agent 設定
# 一部サイト（特に SEC EDGAR）はUA未指定だと 403 を返す。
# SEC は「会社名/ボット名 連絡先メール」形式のUAを要求する。
# 本番では Secrets/環境変数 SEC_USER_AGENT に自分の連絡先を入れること。
# =========================================================
DEFAULT_AGENT = os.getenv(
    "RSS_USER_AGENT",
    "Mozilla/5.0 (compatible; singa9999-finance-bot/1.0)",
)
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "singa9999-finance-bot REPLACE_WITH_YOUR_EMAIL@example.com",  # ← 要差し替え
)


# =========================================================
# RSS_FEEDS: name をキーにした dict。各feedが url / group / priority を持つ。
# group: market_news / official_macro / company_filings
# =========================================================
RSS_FEEDS: dict[str, dict] = {
    # --- 1. market_news ---
    "Yahoo Finance":          {"url": "https://finance.yahoo.com/news/rssindex",                          "group": "market_news",     "priority": 5},
    "MarketWatch":            {"url": "https://feeds.marketwatch.com/marketwatch/topstories/",            "group": "market_news",     "priority": 5},
    "CNBC Markets":           {"url": "https://www.cnbc.com/id/15839069/device/rss/rss.html",             "group": "market_news",     "priority": 6},
    "CNBC Economy":           {"url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",             "group": "market_news",     "priority": 6},
    "CNBC Earnings":          {"url": "https://www.cnbc.com/id/15839135/device/rss/rss.html",             "group": "market_news",     "priority": 6},
    "Investing.com":          {"url": "https://www.investing.com/rss/news.rss",                           "group": "market_news",     "priority": 4},
    "Seeking Alpha Currents": {"url": "https://seekingalpha.com/market_currents.xml",                     "group": "market_news",     "priority": 5},
    "Benzinga":               {"url": "https://www.benzinga.com/feed",                                    "group": "market_news",     "priority": 4},

    # --- 2. official_macro（公式マクロ：高めの priority） ---
    "Fed Press Releases":     {"url": "https://www.federalreserve.gov/feeds/press_all.xml",               "group": "official_macro",  "priority": 9},
    "Fed Speeches":           {"url": "https://www.federalreserve.gov/feeds/speeches.xml",                "group": "official_macro",  "priority": 8},
    "FRED Blog":              {"url": "https://fredblog.stlouisfed.org/feed/",                            "group": "official_macro",  "priority": 7},
    "BEA":                    {"url": "https://apps.bea.gov/rss/rss.xml",                                 "group": "official_macro",  "priority": 8},
    "BLS":                    {"url": "https://www.bls.gov/feed/news_release.rss",                        "group": "official_macro",  "priority": 8},
    "EIA":                    {"url": "https://www.eia.gov/rss/todayinenergy.xml",                        "group": "official_macro",  "priority": 7},
    "U.S. Treasury":          {"url": "https://home.treasury.gov/rss/press.xml",                          "group": "official_macro",  "priority": 8},
}

# group 単位の加点（official_macro / company_filings を高めに）
GROUP_SCORE: dict[str, float] = {
    "market_news":     0.0,
    "official_macro":  4.0,
    "company_filings": 3.0,
}


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
    source_group: str = "market_news"
    priority: int = 3
    category: str = ""   # 後方互換（source_group と同じ値が入る）

    def __post_init__(self):
        # 旧コードの category 互換: 未指定なら source_group を入れる
        if not self.category:
            self.category = self.source_group

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": self.published,
            "source_group": self.source_group,
            "category": self.category,   # 後方互換
            "priority": self.priority,
        }


def _agent_for(url: str) -> str:
    """SEC は専用UA、それ以外はデフォルトUA"""
    if "sec.gov" in url:
        return SEC_USER_AGENT
    return DEFAULT_AGENT


def fetch_feed(name: str, cfg: dict) -> list[NewsItem]:
    """1つのRSSフィードからニュースを取得する。失敗してもBot全体は止めない。"""
    items: list[NewsItem] = []
    url = str(cfg["url"])
    group = str(cfg.get("group", "market_news"))
    priority = int(cfg.get("priority", 3))

    try:
        logger.info(f"{name} [{group}] prio={priority} を取得中")
        parsed = feedparser.parse(url, agent=_agent_for(url))

        if parsed.bozo:
            logger.warning(f"{name} [{group}]: フィードの解析に問題があります（部分取得を試行）")

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
                source_group=group,
                priority=priority,
            ))

        logger.info(f"{name} [{group}]: {len(items)}件取得")

    except Exception as e:
        # 1フィードの失敗は warning に留め、全体は継続する
        logger.warning(f"{name} [{group}] の取得に失敗しました（スキップ）: {e}")

    return items


def _normalize_title(title: str) -> str:
    """類似タイトル比較用に正規化（小文字化・空白/記号除去）"""
    t = title.lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[^\w]", "", t, flags=re.UNICODE)  # 記号除去（CJK・英数は残す）
    return t


def deduplicate(items: list[NewsItem], sim_threshold: float = 0.85) -> list[NewsItem]:
    """URL重複に加え、似たタイトルの重複も排除する。
    items は事前にスコア降順で渡すと、重複時に高スコア側が残る。"""
    seen_urls: set[str] = set()
    kept: list[NewsItem] = []
    kept_norms: list[str] = []

    for item in items:
        if item.url in seen_urls:
            continue

        nt = _normalize_title(item.title)
        is_dup = False
        if nt:
            for kn in kept_norms:
                if not kn:
                    continue
                if difflib.SequenceMatcher(None, nt, kn).ratio() >= sim_threshold:
                    is_dup = True
                    break
        if is_dup:
            continue

        seen_urls.add(item.url)
        kept.append(item)
        kept_norms.append(nt)

    return kept


def is_recent(item: NewsItem, hours: int = 24) -> bool:
    """24時間以内のニュースかチェック（日付不明は通す）"""
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
    """priority + group加点 + キーワード一致でスコアリング"""
    score = float(item.priority) + GROUP_SCORE.get(item.source_group, 0.0)
    text = (item.title + " " + item.source).lower()
    for keyword in FINANCE_KEYWORDS:
        if keyword.lower() in text:
            score += 0.5
    return score


def select_best_item(
    items: list[NewsItem],
    posted_urls: set[str] | None = None,
) -> Optional[NewsItem]:
    """スコアが高いニュースの上位からランダムに1件選ぶ"""
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

    recent = [item for item in available if is_recent(item, hours=24)]
    if not recent:
        logger.warning("24時間以内の未投稿ニュースなし。全未投稿件から選択します")
        recent = available

    scored = sorted(recent, key=score_item, reverse=True)
    top = scored[:5]
    selected = random.choice(top)
    logger.info(
        f"選択: [{selected.source}] [{selected.source_group}] "
        f"prio={selected.priority} {selected.title}"
    )
    return selected


def fetch_news(posted_urls: set[str] | None = None) -> Optional[NewsItem]:
    """全フィードからニュースを取得して1件返す（data/posted_history.json のURLは除外）"""
    if posted_urls is None:
        from posted_history import get_posted_urls
        posted_urls = get_posted_urls()

    all_items: list[NewsItem] = []
    for name, cfg in RSS_FEEDS.items():
        all_items.extend(fetch_feed(name, cfg))

    logger.info(f"合計取得件数: {len(all_items)}件")

    # スコア降順に並べてから重複排除（似タイトルは高スコア側を残す）
    all_items.sort(key=score_item, reverse=True)
    unique_items = deduplicate(all_items)
    logger.info(f"重複除去後（URL+類似タイトル）: {len(unique_items)}件")

    return select_best_item(unique_items, posted_urls=posted_urls)


def main() -> None:
    item = fetch_news()
    if item:
        print(json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
    else:
        logger.error("ニュースを取得できませんでした")


if __name__ == "__main__":
    main()
