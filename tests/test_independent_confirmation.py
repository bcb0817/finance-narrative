import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from common.data_governance import independent_confirmation_decision
from market_data.editorial_bridge import (
    enqueue_internal_trigger,
    match_candidate,
    prioritize_candidates,
    recent_triggers,
)
from news_bot.news import NewsItem, fetch_news_candidates


class IndependentConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "INDEPENDENT_CONFIRMATION_ENABLED": "true",
            "INDEPENDENT_CONFIRMATION_LOOKBACK_HOURS": "24",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_trigger_record_excludes_provider_values_and_chart(self):
        result = enqueue_internal_trigger(
            trigger_id="trigger-1",
            symbol="USD/JPY",
            asset_type="forex",
            provider="twelvedata",
            detected_at=datetime.now(timezone.utc),
        )
        self.assertEqual(result["status"], "queued")
        row = recent_triggers()[0]
        self.assertFalse(row["contains_provider_values"])
        self.assertFalse(row["contains_provider_chart"])
        self.assertNotIn("price", row)
        self.assertNotIn("percentage", row)
        self.assertNotIn("direction", row)

    def test_matching_independent_source_is_prioritized(self):
        enqueue_internal_trigger(
            trigger_id="trigger-2",
            symbol="USD/JPY",
            asset_type="forex",
            provider="twelvedata",
        )
        unrelated = SimpleNamespace(
            title="Oil inventories update",
            url="https://www.eia.gov/test",
            source="EIA",
            source_group="official_macro",
        )
        matched = SimpleNamespace(
            title="Federal Reserve publishes foreign exchange rates for Japanese yen",
            url="https://www.federalreserve.gov/releases/h10/",
            source="Federal Reserve",
            source_group="official_macro",
        )
        ordered = prioritize_candidates([unrelated, matched])
        self.assertIs(ordered[0], matched)
        confirmation = match_candidate(matched)
        self.assertEqual(confirmation["internal_trigger_provider"], "twelvedata")
        self.assertFalse(confirmation["provider_values_exposed"])

    def test_governance_allows_trigger_only_but_blocks_contamination(self):
        allowed = independent_confirmation_decision(
            source_url="https://www.federalreserve.gov/releases/h10/",
            source_group="official_macro",
            publication_provider_lineage=[],
            internal_trigger_providers=["twelvedata"],
            includes_trigger_values=False,
            includes_trigger_chart=False,
        )
        self.assertTrue(allowed["allowed"])
        self.assertTrue(allowed["twelve_data_is_internal_trigger_only"])

        provider_as_source = independent_confirmation_decision(
            source_url="https://api.twelvedata.com/time_series",
            source_group="market_news",
            publication_provider_lineage=["twelvedata"],
            internal_trigger_providers=["twelvedata"],
        )
        self.assertFalse(provider_as_source["allowed"])

        copied_values = independent_confirmation_decision(
            source_url="https://www.ecb.europa.eu/rss/fxref-jpy.html",
            source_group="official_fx",
            publication_provider_lineage=[],
            internal_trigger_providers=["twelvedata"],
            includes_trigger_values=True,
        )
        self.assertFalse(copied_values["allowed"])

    @patch("news_bot.news.fetch_feed")
    def test_official_fx_feed_is_blocked_without_real_trigger(self, fetch_feed):
        item = NewsItem(
            title="Japanese Yen exchange rate",
            url="https://www.federalreserve.gov/releases/h10/",
            source="Fed H10 Japanese Yen",
            published="",
            source_group="official_fx",
            priority=10,
        )
        fetch_feed.return_value = [item]
        self.assertEqual(fetch_news_candidates(posted_urls=set(), limit=5), [])


if __name__ == "__main__":
    unittest.main()
