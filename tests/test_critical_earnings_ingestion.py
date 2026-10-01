import unittest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from news_bot.news import (
    NewsItem, RSS_FEEDS, deduplicate_earnings_events,
    is_market_critical_earnings, score_item,
)
from news_bot.post import (
    assess_market_impact, dynamic_timing_blocks,
    record_evaluation_before_publication,
)
from common import top100_earnings
from common.runtime import JST


def _nvidia_results() -> NewsItem:
    return NewsItem(
        title="NVIDIA Announces Financial Results for Second Quarter Fiscal 2027",
        url="https://investor.nvidia.com/results",
        source="NVIDIA Investor Relations",
        published="Wed, 26 Aug 2026 20:20:00 GMT",
        source_group="company_earnings",
        priority=20,
    )


class CriticalEarningsIngestionTests(unittest.TestCase):
    def test_nvidia_official_press_release_feed_is_configured(self):
        feed = RSS_FEEDS["NVIDIA Investor Relations"]
        self.assertEqual(feed["group"], "company_earnings")
        self.assertIn("nvidianews.nvidia.com", feed["url"])
        self.assertTrue(feed["critical_monitor"])

    def test_official_nvidia_results_are_market_critical_and_rank_first(self):
        critical = _nvidia_results()
        ordinary = NewsItem(
            title="Fed market update", url="https://example.com/fed",
            source="Example", published="", source_group="official_macro", priority=10,
        )
        self.assertTrue(is_market_critical_earnings(critical))
        self.assertGreater(score_item(critical), score_item(ordinary))

    def test_conference_call_notice_is_not_mislabeled_as_released_results(self):
        notice = _nvidia_results()
        notice.title = "NVIDIA Sets Conference Call for Second-Quarter Financial Results"
        self.assertFalse(is_market_critical_earnings(notice))

    def test_ai_cannot_downgrade_official_nvidia_results(self):
        response = Mock()
        response.choices = [Mock(message=Mock(content='''{
          "post_value": 1, "us_equity_relevance": 1,
          "social_buzz_score": 1, "narrative_value": 1,
          "theme_relevance": 1, "market_scope": "none",
          "post_type": "miscellaneous", "has_independent_angle": false,
          "primary_source_importance": 1
        }'''))]
        client = Mock()
        client.chat.completions.create.return_value = response
        with patch("news_bot.post.get_openai_client", return_value=client):
            result = assess_market_impact(_nvidia_results())
        self.assertTrue(result["should_post"])
        self.assertEqual(result["post_value"], 10)
        self.assertEqual(result["critical_event"], "top100_company_earnings")

    def test_ai_failure_uses_bounded_critical_fallback(self):
        with patch("news_bot.post.get_openai_client", side_effect=RuntimeError("offline")):
            result = assess_market_impact(_nvidia_results())
        self.assertTrue(result["should_post"])
        self.assertEqual(result["pass_path"], "critical_official_earnings_fallback")

    def test_critical_results_remain_retryable_and_bypass_only_soft_gap(self):
        item = _nvidia_results()
        self.assertFalse(record_evaluation_before_publication(item))
        self.assertFalse(dynamic_timing_blocks(item, {"allow": False}))

    def test_any_company_in_dynamic_top100_gets_critical_treatment(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "universe_path", return_value=Path(temp) / "top100.json"
        ):
            top100_earnings.save_universe([
                {"ticker": "AAPL", "company_name": "Apple Inc.", "market_cap": 3e12},
                *[
                    {"ticker": f"T{i}", "company_name": f"Test Company {i}",
                     "market_cap": 2e12 - i}
                    for i in range(1, 100)
                ],
            ])
            item = NewsItem(
                title="Apple reports quarterly results",
                url="https://example.com/apple-results", source="Business Wire",
                published="", source_group="company_earnings", priority=18,
            )
            self.assertTrue(is_market_critical_earnings(item))
            self.assertFalse(record_evaluation_before_publication(item))

    def test_share_classes_count_as_one_company(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "universe_path", return_value=Path(temp) / "top100.json"
        ):
            payload = top100_earnings.save_universe([
                {"ticker": "GOOGL", "company_name": "Alphabet Inc. (Class A)", "market_cap": 3},
                {"ticker": "GOOG", "company_name": "Alphabet Inc. (Class C)", "market_cap": 2.9},
                {"ticker": "MSFT", "company_name": "Microsoft Corporation", "market_cap": 2},
            ])
        alphabet = [
            row for row in payload["companies"]
            if row["ticker"] in {"GOOG", "GOOGL"}
        ]
        self.assertEqual(len(alphabet), 1)

    def test_transient_missing_quote_retains_previous_company(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "universe_path", return_value=Path(temp) / "top100.json"
        ):
            top100_earnings.save_universe([
                {"ticker": "NVDA", "company_name": "Nvidia", "market_cap": 4},
                {"ticker": "AAPL", "company_name": "Apple Inc.", "market_cap": 3},
            ])
            payload = top100_earnings.save_universe([
                {"ticker": "AAPL", "company_name": "Apple Inc.", "market_cap": 3.1},
            ])
        self.assertIn("NVDA", [row["ticker"] for row in payload["companies"]])

    def test_protected_large_cap_never_disappears_without_a_quote(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "universe_path", return_value=Path(temp) / "top100.json"
        ):
            payload = top100_earnings.save_universe([
                {"ticker": f"T{i}", "company_name": f"Test Company {i}",
                 "market_cap": 1000 - i} for i in range(120)
            ], require_count=100)
        tickers = {row["ticker"] for row in payload["companies"]}
        self.assertIn("NVDA", tickers)
        self.assertIn("MSFT", tickers)

    def test_same_company_earnings_from_multiple_sources_posts_once(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "universe_path", return_value=Path(temp) / "top100.json"
        ):
            top100_earnings.save_universe([
                {"ticker": "NVDA", "company_name": "Nvidia", "market_cap": 4},
            ])
            official = _nvidia_results()
            media = NewsItem(
                title="Nvidia Q2 quarterly earnings beat expectations",
                url="https://example.com/nvda", source="Media", published="",
                source_group="company_earnings", priority=18,
            )
            result = deduplicate_earnings_events([official, media])
        self.assertEqual(result, [official])

    def test_detected_event_stays_queued_until_posted(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "queue_path", return_value=Path(temp) / "queue.json"
        ):
            top100_earnings.remember_events([
                {"title": "Apple reports quarterly results", "url": "https://example.com/a"}
            ])
            self.assertEqual(len(top100_earnings.pending_events(set())), 1)
            self.assertEqual(
                top100_earnings.pending_events({"https://example.com/a"}), []
            )

    def test_official_source_replaces_media_duplicate_in_queue(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "queue_path", return_value=Path(temp) / "queue.json"
        ):
            top100_earnings.remember_events([{
                "ticker": "NVDA", "title": "Nvidia Q2 FY2027 earnings beat",
                "url": "https://media.example/nvda", "priority": 18,
            }])
            top100_earnings.remember_events([{
                "ticker": "NVDA",
                "title": "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027",
                "url": "https://nvidia.example/results", "priority": 20,
            }])
            rows = top100_earnings.pending_events(set())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://nvidia.example/results")

    def test_status_detects_stale_incomplete_universe_and_delayed_queue(self):
        now = datetime(2026, 8, 27, 12, tzinfo=JST)
        with tempfile.TemporaryDirectory() as temp, patch.object(
            top100_earnings, "universe_path", return_value=Path(temp) / "top100.json"
        ), patch.object(
            top100_earnings, "queue_path", return_value=Path(temp) / "queue.json"
        ), patch(
            "news_bot.posted_history.get_posted_urls", return_value=set()
        ):
            top100_earnings.save_universe([{
                "ticker": "NVDA", "company_name": "Nvidia", "market_cap": 4,
            }], now=now - timedelta(hours=72))
            top100_earnings.remember_events([{
                "ticker": "NVDA", "title": "Nvidia Q2 FY2027 earnings beat",
                "url": "https://example.com/nvda", "priority": 20,
            }])
            queue = top100_earnings.pending_events(set())
            queue[0]["detected_at"] = (now - timedelta(hours=7)).isoformat()
            top100_earnings._write_queue(queue)
            result = top100_earnings.status(now=now)
        self.assertEqual(result["health"], "attention_required")
        self.assertIn("universe_incomplete", result["issues"])
        self.assertIn("universe_stale", result["issues"])
        self.assertIn("publication_pending_too_long", result["issues"])
        self.assertEqual(result["publication_queue"]["pending_count"], 1)

    def test_operations_alerts_include_critical_earnings_health(self):
        from common.operations_alerts import evaluate
        unhealthy = {
            "issues": [
                "universe_incomplete", "universe_stale",
                "publication_pending_too_long",
            ],
            "universe": {"count": 80, "age_hours": 72},
            "publication_queue": {"pending_count": 2, "oldest_pending_hours": 7},
        }
        with patch("common.top100_earnings.status", return_value=unhealthy), patch.dict(
            "os.environ", {"FX_ENABLED": "false", "MARKET_DATA_ENABLED": "false",
                           "XAI_ENABLED": "false"}, clear=False,
        ):
            codes = {row["code"] for row in evaluate()}
        self.assertIn("top100_earnings_universe_incomplete", codes)
        self.assertIn("top100_earnings_universe_stale", codes)
        self.assertIn("top100_earnings_publication_delayed", codes)


if __name__ == "__main__":
    unittest.main()
