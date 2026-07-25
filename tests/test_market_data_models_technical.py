import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_data.cross_asset import classify_cross_asset
from market_data.detectors import detect_movement, detect_volume_anomaly
from market_data.fixtures import (
    analyze_earnings_reaction,
    bars_fixture,
    cross_asset_fixture,
    earnings_fixture,
)
from market_data.models import MarketBar, MarketQuote, normalize_symbol
from market_data.technical import (
    atr,
    bollinger_bands,
    moving_average,
    percentage_change,
    relative_volume,
    rolling_correlation,
    rsi,
    vwap,
    z_score,
)


class MarketDataModelsTechnicalTests(unittest.TestCase):
    def test_normalizes_symbols(self):
        self.assertEqual(normalize_symbol("btc-usd"), "BTC/USD")
        self.assertEqual(normalize_symbol("nvda"), "NVDA")

    def test_rejects_invalid_symbols(self):
        for value in ("", "NVDA$", "A/B/C"):
            with self.assertRaises(ValueError):
                normalize_symbol(value)

    def test_quote_mid(self):
        now = datetime.now(timezone.utc)
        quote = MarketQuote("NVDA", "NVDA", "equity", 120, now, now, bid=119, ask=121)
        self.assertEqual(quote.mid, 120)
        self.assertEqual(quote.to_dict()["mid"], 120)

    def test_bar_round_trip(self):
        bar = bars_fixture(points=2)[0]
        self.assertEqual(MarketBar.from_dict(bar.to_dict()), bar)

    def test_percentage_change(self):
        self.assertAlmostEqual(percentage_change(100, 105), 5)
        self.assertEqual(percentage_change(0, 105), 0)

    def test_atr_is_positive(self):
        self.assertGreater(atr(bars_fixture()), 0)

    def test_z_score_is_nonnegative(self):
        self.assertGreaterEqual(z_score(bars_fixture()), 0)

    def test_relative_volume_detects_spike(self):
        self.assertGreater(relative_volume(bars_fixture()), 3)

    def test_vwap_is_available(self):
        self.assertIsNotNone(vwap(bars_fixture()))

    def test_rsi_is_bounded(self):
        value = rsi(bars_fixture(direction="up"))
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 0)
        self.assertLessEqual(value, 100)

    def test_bollinger_order(self):
        lower, middle, upper = bollinger_bands(bars_fixture())
        self.assertLessEqual(lower, middle)
        self.assertLessEqual(middle, upper)

    def test_moving_average_requires_enough_points(self):
        self.assertIsNone(moving_average(bars_fixture(points=5), 20))
        self.assertIsNotNone(moving_average(bars_fixture(), 20))

    def test_rolling_correlation(self):
        self.assertAlmostEqual(rolling_correlation([1, 2, 3], [2, 4, 6]), 1)
        self.assertIsNone(rolling_correlation([1, 2], [1, 2]))

    def test_megacap_fixture_triggers(self):
        movement = detect_movement(bars_fixture("NVDA"), asset_type="equity")
        self.assertIsNotNone(movement)
        self.assertEqual(movement.symbol, "NVDA")

    def test_etf_fixture_triggers(self):
        movement = detect_movement(bars_fixture("QQQ", asset_type="etf"), asset_type="etf")
        self.assertIsNotNone(movement)

    def test_flat_bars_do_not_trigger(self):
        bars = bars_fixture(volume_spike=False)
        flat = [
            MarketBar(
                bar.symbol, bar.interval, 100, 100.1, 99.9, 100,
                bar.timestamp, 1000, source="fixture",
            )
            for bar in bars
        ]
        self.assertIsNone(detect_movement(flat, asset_type="equity"))

    def test_volume_anomaly_requires_price_move(self):
        bars = bars_fixture()
        bars[-1] = replace(
            bars[-1], close=bars[-2].close * 1.02,
            high=bars[-2].close * 1.021, low=bars[-2].close * .999,
        )
        self.assertIsNotNone(detect_volume_anomaly(bars, asset_type="equity"))

    def test_cross_asset_patterns(self):
        for pattern in (
            "risk_off", "risk_on", "yield_shock", "dollar_strength",
            "semiconductor_specific", "unknown",
        ):
            signal = classify_cross_asset(cross_asset_fixture(pattern))
            self.assertTrue(signal.pattern_type)
            self.assertEqual(signal.source_confirmation_status, "unknown")

    def test_cross_asset_avoids_causal_claim(self):
        signal = classify_cross_asset(cross_asset_fixture("risk_off"))
        self.assertNotIn("原因", signal.likely_interpretation)

    def test_earnings_fixture_does_not_claim_beat_or_miss(self):
        result = analyze_earnings_reaction(earnings_fixture())
        self.assertFalse(result["uses_beat_miss_language"])
        self.assertIn("前年同期", result["summary"])


if __name__ == "__main__":
    unittest.main()
