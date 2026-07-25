import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_data.aggregation import aggregate_bars
from market_data.chart import create_market_chart
from market_data.cross_asset import classify_cross_asset
from market_data.events import classify_official_release, release_fact_record
from market_data.fixtures import bars_fixture
from market_data.models import MarketMovement
from market_data.monitor import evaluate_bars, market_status
from market_data.posts import publish_market
from market_data.provider import MarketDataUnavailable, TwelveDataMarketProvider
from market_data.state import check_gate, remember
from market_data.storage import append_jsonl, cleanup, market_data_dir, read_jsonl


class MarketDataIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "OUTPUT_DIR": str(Path(self.temp.name) / "outputs"),
            "TWELVE_DATA_API_KEY": "test-key-not-real",
            "MARKET_DATA_POST_ENABLED": "false",
            "TWELVEDATA_EXTERNAL_DISPLAY_APPROVED": "false",
            "POST_ENABLED": "false",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def movement(self, suffix="a"):
        now = datetime.now(timezone.utc)
        return MarketMovement(
            f"movement-{suffix}", "NVDA", "equity", "down", 100, 95, -5, -5,
            15, 101, 94, now, relative_volume=3,
        )

    def test_aggregate_five_minute_ohlc(self):
        bars = bars_fixture(points=10)
        result = aggregate_bars(bars, minutes=5)
        self.assertGreaterEqual(len(result), 2)
        self.assertEqual(result[-1].interval, "5min")
        self.assertEqual(result[-1].close, bars[-1].close)
        self.assertGreaterEqual(result[-1].high, result[-1].low)

    def test_aggregate_rejects_invalid_interval(self):
        with self.assertRaises(ValueError):
            aggregate_bars(bars_fixture(), minutes=0)

    def test_official_release_classification(self):
        cases = {
            "Quarterly earnings results": "earnings",
            "New product launch": "product",
            "CEO appointment": "management",
            "Updated guidance": "guidance",
            "Acquisition announced": "acquisition",
            "Share buyback": "capital_allocation",
            "Strategic partnership": "partnership",
            "Regulatory lawsuit update": "regulation",
            "General corporate update": "other",
        }
        for title, expected in cases.items():
            self.assertEqual(classify_official_release(title), expected)

    def test_release_record_is_bounded(self):
        record = release_fact_record(
            symbol="NVDA", title="x" * 500, summary="y" * 1000,
            url="https://example.com", published_at="2026-07-25T00:00:00Z",
        )
        self.assertLessEqual(len(record["title"]), 240)
        self.assertLessEqual(len(record["summary"]), 500)

    def test_cross_asset_additional_patterns(self):
        cases = [
            ({"USD/JPY": -1, "GLD": 1}, "dollar_weakness"),
            ({"USO": 3, "TLT": -1}, "inflation_shock"),
            ({"USO": -4}, "commodity_shock"),
            ({"BTC/USD": 5, "QQQ": .2}, "crypto_specific"),
            ({"BTC/USD": 4, "QQQ": -2}, "divergence"),
        ]
        for changes, expected in cases:
            self.assertEqual(classify_cross_asset(changes).pattern_type, expected)

    def test_chart_is_1600_by_900_and_metadata_matches(self):
        bars = bars_fixture()
        result = evaluate_bars(bars, asset_type="equity", dry_run=True, fixture=True)
        metadata = json.loads(Path(result["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual((metadata["width"], metadata["height"]), (1600, 900))
        self.assertEqual(metadata["movement"]["current_price"], bars[-1].close)
        self.assertEqual(metadata["source"], "fixture")

    def test_chart_y_axis_has_minimum_two_percent_span(self):
        bars = bars_fixture(volume_spike=False)
        flat = [replace(bar, open=100, high=100.01, low=99.99, close=100) for bar in bars]
        movement = self.movement()
        movement.start_price = movement.current_price = 100
        movement.absolute_change = movement.percentage_change = 0
        image, metadata_path = create_market_chart(flat, movement)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(metadata["y_axis_max"] - metadata["y_axis_min"], 2)
        self.assertTrue(image.exists())

    def test_suspicious_outlier_is_blocked(self):
        bars = bars_fixture()
        bars[-1] = replace(bars[-1], close=bars[-2].close * 1.4, high=bars[-2].close * 1.41)
        result = evaluate_bars(bars, asset_type="equity", dry_run=True)
        self.assertEqual(result["data_quality"], "suspicious")

    def test_status_recomputes_delayed_quality_from_age(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        (market_data_dir() / "last_market_data.json").write_text(json.dumps({
            "kind": "bar", "symbol": "NVDA", "timestamp": old,
            "data_quality": "good",
        }), encoding="utf-8")
        self.assertEqual(market_status()["last_quote"]["data_quality"], "delayed")

    def test_missing_api_key_safe_stop(self):
        with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": ""}):
            with self.assertRaises(MarketDataUnavailable):
                TwelveDataMarketProvider(api_key="").quote("NVDA", cache_seconds=0)

    def test_market_post_flag_blocks_before_review(self):
        with patch.dict(os.environ, {"TWELVEDATA_EXTERNAL_DISPLAY_APPROVED": "true"}), \
             patch("market_data.posts.review_tweet_with_openai") as review:
            result = publish_market(self.movement(), "unused.png")
        self.assertEqual(result.status, "disabled")
        review.assert_not_called()

    def test_global_post_flag_cannot_force_a_post(self):
        with patch.dict(os.environ, {
            "TWELVEDATA_EXTERNAL_DISPLAY_APPROVED": "true",
            "MARKET_DATA_POST_ENABLED": "true", "POST_ENABLED": "false",
        }), patch("market_data.posts.review_tweet_with_openai", return_value={"ok_to_post": True}), \
             patch("market_data.posts.post_tweet_with_image", return_value="") as post:
            result = publish_market(self.movement(), "unused.png")
        self.assertEqual(result.status, "global_disabled")
        post.assert_not_called()

    def test_duplicate_gate(self):
        movement = self.movement()
        remember(movement, status="license_blocked")
        self.assertEqual(check_gate(movement).reason, "duplicate_movement")

    def test_symbol_cooldown_gate(self):
        remember(self.movement("a"), status="license_blocked")
        self.assertEqual(check_gate(self.movement("b")).reason, "hourly_limit")
        with patch.dict(os.environ, {"MARKET_MAX_ALERTS_PER_HOUR": "10"}):
            self.assertEqual(check_gate(self.movement("b")).reason, "symbol_cooldown")

    def test_hourly_limit_gate(self):
        remember(self.movement("a"), status="license_blocked")
        self.assertEqual(check_gate(self.movement("b")).reason, "hourly_limit")

    def test_daily_limit_gate(self):
        with patch.dict(os.environ, {
            "MARKET_MAX_ALERTS_PER_HOUR": "99", "MARKET_MAX_ALERTS_PER_DAY": "1",
            "MARKET_ALERT_COOLDOWN_MINUTES": "0",
        }):
            remember(self.movement("a"), status="license_blocked")
            other = replace(self.movement("b"), symbol="MSFT")
            self.assertEqual(check_gate(other).reason, "daily_limit")

    def test_jsonl_corruption_is_quarantined(self):
        path = market_data_dir() / "movements.jsonl"
        path.write_text('{"ok":1}\nnot-json\n', encoding="utf-8")
        self.assertEqual(read_jsonl("movements.jsonl"), [{"ok": 1}])
        self.assertTrue(list(market_data_dir().glob("quarantine_*.jsonl")))

    def test_cleanup_removes_expired_rows(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        append_jsonl("bars_1m.jsonl", {"timestamp": old, "value": 1})
        append_jsonl("bars_1m.jsonl", {"timestamp": datetime.now(timezone.utc).isoformat(), "value": 2})
        self.assertEqual(cleanup(), 1)
        self.assertEqual(read_jsonl("bars_1m.jsonl")[0]["value"], 2)

    def test_japanese_fixture_text_is_utf8(self):
        result = evaluate_bars(
            bars_fixture(), asset_type="equity", dry_run=True, fixture=True,
        )
        self.assertIn("架空データ", result["text"])
        result["text"].encode("utf-8").decode("utf-8")

    def test_feature_flag_disables_megacap_detection(self):
        with patch.dict(os.environ, {
            "MEGACAP_ALERT_ENABLED": "false", "VOLUME_ALERT_ENABLED": "false",
        }):
            result = evaluate_bars(
                bars_fixture(), asset_type="equity", dry_run=True, fixture=True,
            )
        self.assertEqual(result["status"], "no_material_movement")


if __name__ == "__main__":
    unittest.main()
