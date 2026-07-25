import unittest

from news_bot.news import RSS_FEEDS, NewsItem, score_item


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


if __name__ == "__main__":
    unittest.main()
