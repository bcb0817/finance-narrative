import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fx_alert.monitor import effective_max_age_seconds, evaluate, run_monitor
from fx_alert.fixture import movement_fixture
from fx_alert.providers import PolygonProvider, TwelveDataProvider, get_provider, provider_symbol
from fx_alert.storage import append_jsonl, cleanup, read_jsonl


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)


class FxProviderMonitorTests(unittest.TestCase):
    def test_freshness_limit_accounts_for_polling_and_bar_finalization(self):
        with patch.dict(os.environ, {
            "FX_DATA_MAX_AGE_SECONDS": "90",
            "FX_POLL_INTERVAL_MINUTES": "5",
            "FX_BAR_FINALIZATION_ALLOWANCE_SECONDS": "120",
        }):
            self.assertEqual(effective_max_age_seconds(), 420)

    def test_two_minute_bar_delay_does_not_block_five_minute_monitor(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp,
            "FX_DATA_MAX_AGE_SECONDS": "90",
            "FX_POLL_INTERVAL_MINUTES": "5",
            "FX_BAR_FINALIZATION_ALLOWANCE_SECONDS": "120",
        }):
            bars = movement_fixture()
            result = evaluate(bars, dry_run=True)
            self.assertNotEqual(result["status"], "quality_blocked")
            self.assertNotEqual(result["status"], "quality_degraded")

    def test_repeated_stale_data_escalates_to_quality_degraded(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp,
            "FX_DATA_MAX_AGE_SECONDS": "600",
            "FX_QUALITY_ALERT_CONSECUTIVE_RUNS": "3",
        }), patch(
            "common.operations_alerts.evaluate",
            return_value=[{"code": "fx_data_quality_degraded"}],
        ), patch("common.operations_alerts.send_discord_alerts") as notify:
            bars = [
                replace(item, timestamp=item.timestamp - timedelta(hours=2))
                for item in movement_fixture()
            ]
            self.assertEqual(evaluate(bars, dry_run=True)["status"], "quality_blocked")
            self.assertEqual(evaluate(bars, dry_run=True)["status"], "quality_blocked")
            third = evaluate(bars, dry_run=True)
            self.assertEqual(third["status"], "quality_degraded")
            self.assertEqual(third["health"]["consecutive_blocked_runs"], 3)
            notify.assert_called_once_with([{"code": "fx_data_quality_degraded"}])

    def test_provider_symbol(self):
        self.assertEqual(provider_symbol("USDJPY"), "USD/JPY")

    def test_missing_key_is_safe(self):
        provider = TwelveDataProvider(api_key="")
        status = provider.status()
        self.assertFalse(status.configured)
        self.assertFalse(status.available)

    def test_status_does_not_contain_key(self):
        provider = TwelveDataProvider(api_key="secret-test-key")
        self.assertNotIn("secret-test-key", json.dumps(provider.status().to_dict()))

    def test_quote_parsing(self):
        session = Session({"close": "155.25", "bid": "155.24", "ask": "155.26", "timestamp": "1700000000"})
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            quote = TwelveDataProvider(session=session, api_key="dummy").get_quote("USDJPY")
        self.assertEqual(quote.price, 155.25)
        self.assertEqual(quote.pair, "USDJPY")

    def test_bars_parsing_and_sorting(self):
        session = Session({"values": [
            {"datetime": "2026-07-25 00:01:00", "open": "155", "high": "156", "low": "154", "close": "155.5"},
            {"datetime": "2026-07-25 00:00:00", "open": "154", "high": "155", "low": "153", "close": "154.5"},
        ]})
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            bars = TwelveDataProvider(session=session, api_key="dummy").get_bars("USDJPY", interval="1min", outputsize=12)
        self.assertLess(bars[0].timestamp, bars[1].timestamp)

    def test_polygon_is_explicitly_unavailable(self):
        self.assertFalse(PolygonProvider().status().available)
        with self.assertRaises(RuntimeError):
            PolygonProvider().get_quote("USDJPY")

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            get_provider("unknown")

    def test_daily_call_budget_blocks_request(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp,
            "FX_DATA_MAX_REST_CALLS_PER_DAY": "0",
        }):
            with self.assertRaises(RuntimeError):
                TwelveDataProvider(session=Session({}), api_key="dummy").get_quote("USDJPY")

    def test_monthly_budget_blocks_when_reported_usage_reaches_limit(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp,
            "FX_DATA_MONTHLY_BUDGET_USD": "5",
            "FX_DATA_REPORTED_MONTHLY_COST_USD": "5",
        }):
            with self.assertRaises(RuntimeError):
                TwelveDataProvider(session=Session({}), api_key="dummy").get_quote("USDJPY")

    def test_jsonl_corruption_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            append_jsonl("movements.jsonl", {"ok": 1})
            path = Path(temp) / "fx" / "movements.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{bad\n")
            self.assertEqual(read_jsonl("movements.jsonl"), [{"ok": 1}])
            self.assertTrue(list((Path(temp) / "fx").glob("quarantine_*.jsonl")))

    def test_cleanup_prunes_expired_quotes(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            append_jsonl("quotes.jsonl", {"timestamp": "2020-01-01T00:00:00+00:00", "price": 100})
            append_jsonl("quotes.jsonl", {"timestamp": "2999-01-01T00:00:00+00:00", "price": 155})
            self.assertEqual(cleanup(), 1)
            self.assertEqual(len(read_jsonl("quotes.jsonl")), 1)

    def test_fixture_monitor_creates_chart_without_posting(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": str(Path(temp) / "data"),
            "OUTPUT_DIR": str(Path(temp) / "outputs"),
            "FX_ENABLED": "true",
            "FX_POST_ENABLED": "false",
            "DISCORD_ALERTS_ENABLED": "false",
        }):
            result = run_monitor(dry_run=True, fixture=True)
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse(result["would_post"])
            image_path = Path(result["chart"])
            self.assertTrue(image_path.exists())
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (1600, 900))
            metadata = json.loads(Path(result["metadata"]).read_text(encoding="utf-8"))
            self.assertEqual(metadata["movement_id"], result["movement"]["movement_id"])
            self.assertEqual(metadata["current_price"], result["movement"]["end_price"])
            self.assertTrue(metadata["content_hash"])

    def test_missing_provider_key_does_not_crash_existing_bot(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": str(Path(temp) / "data"),
            "OUTPUT_DIR": str(Path(temp) / "outputs"),
            "TWELVE_DATA_API_KEY": "",
            "FX_DATA_PROVIDER": "twelvedata",
        }), patch("fx_alert.monitor.log_error"):
            result = run_monitor(dry_run=True)
            self.assertEqual(result["status"], "provider_unavailable")
            self.assertTrue(result["safe_failure"])

    def test_provider_failure_logs_redacted_error_detail(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": str(Path(temp) / "data"),
            "OUTPUT_DIR": str(Path(temp) / "outputs"),
            "LOG_DIR": str(Path(temp) / "logs"),
            "FX_ENABLED": "true",
        }), patch("fx_alert.monitor.get_provider", side_effect=RuntimeError(
            "API_KEY=secret-value provider unavailable"
        )), patch("fx_alert.monitor.log_error") as log_error:
            result = run_monitor(dry_run=True)
            self.assertEqual(result["status"], "provider_unavailable")
            error = log_error.call_args.args[0]
            self.assertEqual(error["error_type"], "RuntimeError")
            self.assertIn("<redacted>", error["error_detail"])
            self.assertNotIn("secret-value", error["error_detail"])


if __name__ == "__main__":
    unittest.main()
