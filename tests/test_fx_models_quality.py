import unittest
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fx_alert.aggregation import aggregate_bars, quotes_to_minute_bars
from fx_alert.fixture import movement_fixture
from fx_alert.models import FxBar, FxMovement, FxQuote, normalize_pair
from fx_alert.quality import validate_bars, validate_quotes


class FxModelsQualityTests(unittest.TestCase):
    def test_normalizes_pair_variants(self):
        for value in ("USD/JPY", "usd-jpy", "USD_JPY", "USDJPY"):
            self.assertEqual(normalize_pair(value), "USDJPY")

    def test_rejects_invalid_pair(self):
        with self.assertRaises(ValueError):
            normalize_pair("USD")

    def test_quote_round_trip(self):
        quote = FxQuote("USD/JPY", datetime.now(timezone.utc), 155.2, 155.19, 155.21, "fixture")
        self.assertEqual(FxQuote.from_dict(quote.to_dict()), quote)

    def test_bar_round_trip(self):
        bar = movement_fixture()[0]
        self.assertEqual(FxBar.from_dict(bar.to_dict()), bar)

    def test_movement_round_trip_and_direction(self):
        now = datetime.now(timezone.utc)
        movement = FxMovement("id", "USDJPY", now, "5m", 155, 156, 1, .64, "up")
        self.assertEqual(movement.direction_ja, "円安")
        self.assertEqual(FxMovement.from_dict(movement.to_dict()).movement_id, "id")

    def test_usdjpy_up_is_yen_weakening(self):
        movement = FxMovement("up", "USDJPY", datetime.now(timezone.utc), "5m", 155, 156, 1, .64, "up")
        self.assertEqual(movement.movement_direction, "yen_weakening")
        self.assertEqual(movement.direction_ja, "円安")

    def test_usdjpy_down_is_yen_strengthening(self):
        movement = FxMovement("down", "USDJPY", datetime.now(timezone.utc), "5m", 156, 155, -1, -.64, "down")
        self.assertEqual(movement.movement_direction, "yen_strengthening")
        self.assertEqual(movement.direction_ja, "円高")

    def test_quotes_to_minute_bars(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        quotes = [
            FxQuote("USDJPY", now + timedelta(seconds=index * 10), 155 + index * .01)
            for index in range(6)
        ]
        bars = quotes_to_minute_bars(quotes)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].open, 155)
        self.assertAlmostEqual(bars[0].close, 155.05)

    def test_aggregate_bars(self):
        bars = movement_fixture(points=20)
        result = aggregate_bars(bars, minutes=5)
        self.assertGreaterEqual(len(result), 4)
        self.assertEqual(result[0].interval, "5min")

    def test_aggregate_15_minute_bars(self):
        self.assertEqual(aggregate_bars(movement_fixture(points=60), minutes=15)[0].interval, "15min")

    def test_aggregate_60_minute_bars(self):
        self.assertEqual(aggregate_bars(movement_fixture(points=180), minutes=60)[0].interval, "60min")

    def test_good_bars(self):
        self.assertTrue(validate_bars(movement_fixture()).good)

    def test_insufficient_bars(self):
        result = validate_bars(movement_fixture(points=5))
        self.assertFalse(result.good)
        self.assertIn("insufficient_points", result.reasons)

    def test_invalid_ohlc(self):
        bars = movement_fixture()
        bars[3] = replace(bars[3], high=bars[3].low - 1)
        self.assertIn("invalid_ohlc", validate_bars(bars).reasons)

    def test_duplicate_bar_timestamp(self):
        bars = movement_fixture()
        bars[2] = replace(bars[2], timestamp=bars[1].timestamp)
        self.assertIn("duplicate_timestamp", validate_bars(bars).reasons)

    def test_stale_quotes(self):
        now = datetime.now(timezone.utc)
        quotes = [FxQuote("USDJPY", now - timedelta(minutes=3, seconds=i), 155) for i in range(12)]
        self.assertIn("stale", validate_quotes(quotes, now=now).reasons)

    def test_stale_bars(self):
        bars = movement_fixture(now=datetime.now(timezone.utc) - timedelta(minutes=5))
        self.assertIn("stale", validate_bars(bars).reasons)

    def test_wide_spread(self):
        now = datetime.now(timezone.utc)
        quotes = [
            FxQuote("USDJPY", now - timedelta(seconds=11 - i), 155, 150, 160)
            for i in range(12)
        ]
        self.assertIn("wide_spread", validate_quotes(quotes, now=now).reasons)

    def test_inverted_spread(self):
        now = datetime.now(timezone.utc)
        quotes = [
            FxQuote("USDJPY", now - timedelta(seconds=11 - i), 155, 156, 155)
            for i in range(12)
        ]
        self.assertIn("inverted_spread", validate_quotes(quotes, now=now).reasons)

    def test_quote_outlier(self):
        now = datetime.now(timezone.utc)
        quotes = [FxQuote("USDJPY", now - timedelta(seconds=11 - i), 155) for i in range(11)]
        quotes.append(FxQuote("USDJPY", now, 170))
        self.assertIn("outlier", validate_quotes(quotes, now=now).reasons)


if __name__ == "__main__":
    unittest.main()
