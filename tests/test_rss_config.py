import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from news_bot.news import (
    RSS_FEEDS,
    NewsItem,
    diversify_ranked_items,
    fetch_feed,
    matches_feed_filter,
    score_item,
)


class RssConfigTests(unittest.TestCase):
    def test_rss_urls_are_unique_and_https(self):
        urls = [feed["url"] for feed in RSS_FEEDS.values()]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(url.startswith("https://") for url in urls))

    def test_required_primary_sources_are_present(self):
        self.assertIn("Fed Monetary Policy", RSS_FEEDS)
        self.assertIn("BLS Latest Indicators", RSS_FEEDS)
        self.assertIn("SEC Press Releases", RSS_FEEDS)
        self.assertIn("White House News", RSS_FEEDS)

    def test_news_expansion_has_nineteen_feeds(self):
        self.assertEqual(len(RSS_FEEDS), 19)
        for name in (
            "WSJ Markets", "Financial Times Markets", "NYT Business",
            "Fortune", "TechCrunch AI", "CoinDesk",
        ):
            self.assertIn(name, RSS_FEEDS)

    def test_removed_noisy_or_broken_feeds_do_not_return(self):
        removed = {"Yahoo Finance", "Benzinga", "FRED Blog", "BLS", "U.S. Treasury"}
        self.assertTrue(removed.isdisjoint(RSS_FEEDS))

    def test_broad_white_house_feed_is_not_treated_as_macro(self):
        self.assertEqual(RSS_FEEDS["White House News"]["group"], "official_policy")
        unrelated_policy = NewsItem(
            title="Restoring Trust in the Smithsonian Institution",
            url="https://example.com/policy",
            source="White House News",
            published="",
            source_group="official_policy",
            priority=4,
        )
        monetary_policy = NewsItem(
            title="Federal Reserve monetary policy decision",
            url="https://example.com/fed",
            source="Fed Monetary Policy",
            published="",
            source_group="official_macro",
            priority=10,
        )
        self.assertGreater(score_item(monetary_policy), score_item(unrelated_policy))

    def test_sec_is_regulatory_not_macro(self):
        self.assertEqual(RSS_FEEDS["SEC Press Releases"]["group"], "official_regulatory")

    def test_candidate_diversity_caps_publisher_family(self):
        rows = [
            NewsItem(f"CNBC {index}", f"https://example.com/cnbc/{index}", f"CNBC Feed {index}", "", priority=10)
            for index in range(5)
        ]
        rows += [
            NewsItem("Fed decision", "https://example.com/fed", "Fed Monetary Policy", "", priority=9),
            NewsItem("BLS jobs", "https://example.com/bls", "BLS Latest Indicators", "", priority=9),
        ]
        selected = diversify_ranked_items(rows, limit=5, max_per_publisher=2)
        self.assertEqual(sum(row.source.startswith("CNBC") for row in selected), 2)
        self.assertIn("Fed Monetary Policy", {row.source for row in selected})

    def test_specialist_feeds_require_market_relevance(self):
        tech = RSS_FEEDS["TechCrunch AI"]
        crypto = RSS_FEEDS["CoinDesk"]
        self.assertTrue(matches_feed_filter("Nvidia unveils a new AI chip", tech))
        self.assertFalse(matches_feed_filter("AI improves household recipes", tech))
        self.assertTrue(matches_feed_filter("Bitcoin ETF sees record demand", crypto))
        self.assertFalse(matches_feed_filter("Conference schedule announced", crypto))

    def test_feed_download_uses_bounded_timeout(self):
        response = MagicMock()
        response.__enter__.return_value.iter_content.return_value = [(
            b'<?xml version="1.0"?><rss><channel><item>'
            b'<title>Fed market update</title><link>https://example.com/1</link>'
            b'</item></channel></rss>'
        )]
        response.__enter__.return_value.status_code = 200
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp,
            "RSS_FETCH_TIMEOUT_SECONDS": "3",
            "RSS_MAX_RESPONSE_BYTES": "64000",
        }, clear=False), patch(
            "news_bot.news.requests.get", return_value=response
        ) as opened:
            rows = fetch_feed("Fixture", {
                "url": "https://example.com/feed.xml",
                "group": "market_news",
                "priority": 5,
            })
        self.assertEqual(len(rows), 1)
        self.assertEqual(opened.call_args.kwargs["timeout"], (3.0, 3.0))


if __name__ == "__main__":
    unittest.main()
