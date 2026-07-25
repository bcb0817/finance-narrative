import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_data.capabilities import check_capabilities
from market_data.fixtures import bars_fixture
from market_data.monitor import evaluate_bars, run_fixture, symbols_for_run
from market_data.posts import external_display_approved, market_post_enabled, publish_market
from market_data.provider import MarketDataUnavailable, TwelveDataMarketProvider, provider_status
from market_data.storage import append_jsonl, usage_summary
from market_data.symbols import enabled_symbols, load_watchlist, symbol_config


class FakeResponse:
    def __init__(self, value, headers=None):
        self.value = value
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class MarketDataSafetyProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "OUTPUT_DIR": str(Path(self.temp.name) / "outputs"),
            "TWELVE_DATA_API_KEY": "test-key-not-real",
            "MARKET_DATA_ENABLED": "true",
            "MARKET_DATA_POST_ENABLED": "false",
            "TWELVEDATA_EXTERNAL_DISPLAY_APPROVED": "false",
            "TWELVEDATA_MAX_CREDITS_PER_MINUTE": "8",
            "TWELVEDATA_MAX_CREDITS_PER_DAY": "760",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_watchlist_has_required_symbols(self):
        symbols = {row["symbol"] for row in load_watchlist()}
        self.assertTrue({"SPY", "QQQ", "SMH", "TLT", "GLD", "BTC/USD", "NVDA"} <= symbols)

    def test_usdjpy_owned_by_fx_monitor(self):
        self.assertIn("Existing FX monitor", symbol_config("USD/JPY")["notes"])

    def test_enabled_symbols_are_explicit(self):
        self.assertTrue(all(row["enabled"] for row in enabled_symbols()))

    def test_all_symbols_block_external_display_by_default(self):
        self.assertTrue(all(not row["external_display_allowed"] for row in load_watchlist()))

    def test_post_and_external_display_default_false(self):
        self.assertFalse(market_post_enabled())
        self.assertFalse(external_display_approved())

    def test_provider_status_never_returns_key(self):
        status = provider_status()
        self.assertNotIn("api_key", status)
        self.assertTrue(status["api_key_configured"])

    def test_provider_parses_bars(self):
        response = FakeResponse({
            "values": [{
                "datetime": "2026-07-25 00:00:00", "open": "100",
                "high": "102", "low": "99", "close": "101", "volume": "500",
            }]
        }, {"api-credits-used": "1"})
        provider = TwelveDataMarketProvider(session=Mock(get=Mock(return_value=response)))
        bars = provider.bars("NVDA", cache_seconds=0)
        self.assertEqual(bars[0].close, 101)
        self.assertEqual(usage_summary()["daily_credits"], 1)

    def test_provider_rejects_error_payload(self):
        response = FakeResponse({"status": "error", "message": "bad request"})
        provider = TwelveDataMarketProvider(session=Mock(get=Mock(return_value=response)))
        with self.assertRaises(MarketDataUnavailable):
            provider.quote("NVDA", cache_seconds=0)

    def test_provider_cache_prevents_second_credit(self):
        response = FakeResponse({"symbol": "NVDA", "close": "101", "timestamp": 1784937600})
        session = Mock(get=Mock(return_value=response))
        provider = TwelveDataMarketProvider(session=session)
        provider.quote("NVDA", cache_seconds=60)
        provider.quote("NVDA", cache_seconds=60)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(usage_summary()["daily_credits"], 1)
        self.assertGreaterEqual(usage_summary()["cache_hits"], 1)

    def test_hard_credit_limit_stops_request(self):
        with patch.dict(os.environ, {"TWELVEDATA_MAX_CREDITS_PER_DAY": "1"}):
            append_jsonl("provider_usage.jsonl", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "credits_used": 1, "success": True, "cache_hit": False,
            })
            provider = TwelveDataMarketProvider(session=Mock())
            with self.assertRaises(MarketDataUnavailable):
                provider.quote("NVDA", cache_seconds=0)

    def test_capabilities_infers_basic_and_stays_non_display(self):
        with patch.object(TwelveDataMarketProvider, "api_usage", return_value={
            "plan_limit": 8, "daily_usage": 12, "current_usage": 2,
        }):
            result = check_capabilities(refresh=True)
        self.assertIn("Basic", result["plan_name"])
        self.assertFalse(result["external_display_confirmed"])
        self.assertFalse(result["websocket_production_enabled"])
        self.assertFalse(result["earnings_available"])

    def test_fixture_is_dry_run(self):
        with patch("market_data.monitor.notify_market_preview", return_value={"status": "mocked"}):
            result = run_fixture("mega", send_preview=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["would_post"])
        self.assertEqual(result["movement"]["data_source"], "fixture")
        self.assertIn("TEST/FIXTURE", result["text"])

    def test_real_evaluation_blocks_stale_data(self):
        result = evaluate_bars(
            bars_fixture(delayed=True), asset_type="equity",
            dry_run=True, fixture=False,
        )
        self.assertEqual(result["status"], "quality_blocked")

    def test_fixture_allows_delayed_label(self):
        result = evaluate_bars(
            bars_fixture(delayed=True), asset_type="equity",
            dry_run=True, fixture=True,
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["movement"]["data_quality"], "delayed")

    def test_publish_is_license_blocked_before_openai_or_x(self):
        movement = evaluate_bars(
            bars_fixture(), asset_type="equity", dry_run=True, fixture=True,
        )["movement"]
        from market_data.models import MarketMovement
        movement = MarketMovement(
            **{**movement, "detected_at": datetime.fromisoformat(movement["detected_at"])}
        )
        with patch("market_data.posts.review_tweet_with_openai") as review, \
             patch("market_data.posts.post_tweet_with_image") as post:
            result = publish_market(movement, "never-used.png")
        self.assertEqual(result.status, "license_blocked")
        review.assert_not_called()
        post.assert_not_called()

    def test_weekend_rotation_excludes_equities(self):
        saturday = datetime(2026, 7, 25, 12, 5, tzinfo=timezone.utc)
        selected = symbols_for_run(saturday)
        self.assertTrue(all(row["asset_type"] == "crypto" for row in selected))

    def test_rotation_excludes_usdjpy(self):
        saturday = datetime(2026, 7, 25, 12, 5, tzinfo=timezone.utc)
        self.assertNotIn("USD/JPY", {row["symbol"] for row in symbols_for_run(saturday)})

    def test_global_usage_baseline_is_honored(self):
        folder = Path(self.temp.name) / "market_data"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "twelve_data_capabilities.json").write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "api_limits": {"daily_usage": 123, "current_usage": 2},
        }), encoding="utf-8")
        self.assertGreaterEqual(usage_summary()["daily_credits"], 123)


if __name__ == "__main__":
    unittest.main()
