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
    "Mozilla/5.0 (compatible; example-finance-bot/1.0)",
)
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "example-finance-bot bot-contact@example.com",  # 公開用ダミー。運用時は要差し替え
)


# =========================================================
# RSS_FEEDS: name をキーにした dict。各feedが url / group / priority を持つ。
# group: market_news / official_macro / company_filings
# =========================================================
RSS_FEEDS: dict[str, dict] = {
    # --- 1. market_news ---
    "MarketWatch":            {"url": "https://feeds.marketwatch.com/marketwatch/topstories/",            "group": "market_news",     "priority": 5},
    "CNBC Markets":           {"url": "https://www.cnbc.com/id/15839069/device/rss/rss.html",             "group": "market_news",     "priority": 6},
    "CNBC Economy":           {"url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",             "group": "market_news",     "priority": 6},
    "CNBC Earnings":          {"url": "https://www.cnbc.com/id/15839135/device/rss/rss.html",             "group": "market_news",     "priority": 6},
    "Investing.com":          {"url": "https://www.investing.com/rss/news.rss",                           "group": "market_news",     "priority": 4},
    "Seeking Alpha Currents": {"url": "https://seekingalpha.com/market_currents.xml",                     "group": "market_news",     "priority": 5},

    # --- 2. official_macro（公式マクロ：高めの priority） ---
    "Fed Monetary Policy":    {"url": "https://www.federalreserve.gov/feeds/press_monetary.xml",          "group": "official_macro",  "priority": 10},
    "Fed Speeches":           {"url": "https://www.federalreserve.gov/feeds/speeches.xml",                "group": "official_macro",  "priority": 8},
    "BEA":                    {"url": "https://apps.bea.gov/rss/rss.xml",                                 "group": "official_macro",  "priority": 8},
    "BLS Latest Indicators":  {"url": "https://www.bls.gov/feed/bls_latest.rss",                          "group": "official_macro",  "priority": 9},
    "EIA":                    {"url": "https://www.eia.gov/rss/todayinenergy.xml",                        "group": "official_macro",  "priority": 7},
    "SEC Press Releases":     {"url": "https://www.sec.gov/news/pressreleases.rss",                       "group": "official_macro",  "priority": 8},
    "White House News":       {"url": "https://www.whitehouse.gov/news/feed/",                            "group": "official_macro",  "priority": 7},
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


# =========================================================
# 優先テーマ（米国株インパクト最重視）。一致したものは強めに加点。
# FRB/金利/ドル/PCE・CPI・雇用・GDP・ISM/指数/半導体・AI/大型テック/
# 主要決算/原油・エネルギー/地政学
# =========================================================
PRIORITY_THEME_KEYWORDS: list[str] = [
    "fed", "frb", "fomc", "powell", "利上げ", "利下げ", "rate cut", "rate hike",
    "金利", "yield", "国債", "treasury", "ドル", "dollar", "dxy", "為替",
    "pce", "cpi", "ppi", "inflation", "雇用", "payroll", "jobs", "unemployment",
    "gdp", "ism", "pmi", "retail sales",
    "nasdaq", "ナスダック", "s&p", "sp500", "s&p500", "dow", "ダウ", "指数",
    "semiconductor", "半導体", "chip", "ai", "人工知能",
    "nvidia", "nvda", "amd", "avgo", "broadcom", "micron", "tsmc", "tsm", "asml",
    "apple", "aapl", "microsoft", "msft", "google", "googl", "alphabet",
    "amazon", "amzn", "meta", "tesla", "tsla", "大型テック", "megacap", "メガキャップ",
    "earnings", "決算", "guidance", "ガイダンス",
    "oil", "crude", "原油", "opec", "energy", "エネルギー", "天然ガス", "natural gas",
    "地政学", "geopolitic", "中東", "ロシア", "中国", "台湾", "関税", "tariff", "制裁",
]

# 加点重み（priority + group + finance + theme）
PRIORITY_THEME_BONUS = 3.0


# =========================================================
# 除外フィルタ：米国株への影響が薄い / 出所不明 / 中身が薄いものを落とす。
#   - 東京都区部CPI、東京市場コメント（日本ローカル）
#   - 小型株の単独 8-K / SEC提出通知だけ
#   - 低インパクトIR、出所不明の市場コメント
#   - 「〇〇が発表」「市場は注目」だけのフィラー見出し
# =========================================================
import re as _re

# タイトルに含まれていたら即除外（日本ローカル・出所不明系）
EXCLUDE_TITLE_PATTERNS: list[str] = [
    r"東京都区部",                 # 東京都区部CPI
    r"東京.*(市場|相場).*(コメント|まとめ|寄り付き|大引け)",
    r"(tokyo)\s+(cpi|core cpi)",
    r"日経平均(寄り|大引|前場|後場)",
    r"出所不明", r"うわさ", r"噂",
]

# 単独提出通知だけ（背景説明が無い 8-K/SEC filing のみ）を示す型
_FILING_ONLY = _re.compile(
    r"(8-?k|6-?k|s-?1|424b|13[dgf]|form\s+\d|sec\s+filing|提出(を|し|の)?(通知|完了)?)",
    _re.IGNORECASE,
)
# フィラー（中身が薄い）見出しの型
_FILLER = _re.compile(
    r"(が発表|を発表|発表した|market\s+(eyes|watch(es)?|focus)|"
    r"市場は注目|注目集める|話題に|まとめ|ランキング速報)",
    _re.IGNORECASE,
)


def is_excluded(title: str, source_group: str = "market_news") -> tuple[bool, str]:
    """米国株への価値が低い見出しを除外する。戻り値: (除外するか, 理由)。"""
    t = (title or "").strip()
    low = t.lower()
    for pat in EXCLUDE_TITLE_PATTERNS:
        if _re.search(pat, t, _re.IGNORECASE):
            return True, f"excluded_local_or_unsourced({pat})"
    # 提出通知だけ、かつ背景語が薄い → 除外（company_filings に多い）
    if _FILING_ONLY.search(low):
        return True, "excluded_bare_filing(8-K/SEC等の提出通知のみ)"
    if _FILLER.search(low):
        return True, "excluded_filler(発表/注目だけの薄い見出し)"
    return False, ""


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

            excluded, ex_reason = is_excluded(title, group)
            if excluded:
                logger.info(f"除外: {ex_reason} :: {title[:60]}")
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
    """priority + group加点 + キーワード一致 + 優先テーマ加点でスコアリング"""
    score = float(item.priority) + GROUP_SCORE.get(item.source_group, 0.0)
    text = (item.title + " " + item.source).lower()
    for keyword in FINANCE_KEYWORDS:
        if keyword.lower() in text:
            score += 0.5
    # 優先テーマ（FRB/金利/ドル/指標/指数/半導体・AI/大型テック/決算/原油/地政学）
    if any(k in text for k in PRIORITY_THEME_KEYWORDS):
        score += PRIORITY_THEME_BONUS
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
    candidates = fetch_news_candidates(posted_urls, limit=1)
    return candidates[0] if candidates else None


def fetch_news_candidates(
    posted_urls: set[str] | None = None,
    limit: int = 10,
) -> list[NewsItem]:
    """全フィードから取得し、スコア上位の候補を limit 件返す。
    呼び出し側は先頭から順に評価し、評価済み(evaluated_history)なら次候補へ進める。
    """
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

    available = [it for it in unique_items if it.url not in (posted_urls or set())]
    excluded = len(unique_items) - len(available)
    if excluded:
        logger.info(f"投稿済み {excluded} 件を除外しました")

    recent = [it for it in available if is_recent(it, hours=24)]
    if not recent:
        logger.warning("24時間以内の未投稿ニュースなし。全未投稿件から選択します")
        recent = available

    ranked = sorted(recent, key=score_item, reverse=True)[:limit]
    for it in ranked[:3]:
        logger.info(f"候補: [{it.source}] prio={it.priority} {it.title[:60]}")
    return ranked


def main() -> None:
    item = fetch_news()
    if item:
        print(json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
    else:
        logger.error("ニュースを取得できませんでした")


if __name__ == "__main__":
    main()
