import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fx_alert.context import UNKNOWN_CAUSE, classify_context
from fx_alert.detector import THRESHOLDS, configured_thresholds, detect_movements, strongest_movement
from fx_alert.fixture import movement_fixture
from fx_alert.models import FxMovement
from fx_alert.post import build_post
from fx_alert.state import check_alert_gate, remember_alert


def movement(identifier="m1", direction="up", change=1.0, when=None):
    return FxMovement(
        identifier, "USDJPY", when or datetime.now(timezone.utc), "15m",
        155.0, 155.0 + (change if direction == "up" else -change),
        change if direction == "up" else -change,
        (change / 155) * 100 * (1 if direction == "up" else -1),
        direction,
    )


class FxDetectorStatePostTests(unittest.TestCase):
    def test_thresholds_match_spec(self):
        self.assertEqual((THRESHOLDS["5m"].pct, THRESHOLDS["5m"].yen), (.30, .50))
        self.assertEqual((THRESHOLDS["24h"].pct, THRESHOLDS["24h"].yen), (1.50, 2.00))

    def test_thresholds_are_configurable(self):
        with patch.dict(os.environ, {"FX_MOVE_5M_PERCENT": "0.42", "FX_MOVE_5M_JPY": "0.66"}):
            self.assertEqual((configured_thresholds()["5m"].pct, configured_thresholds()["5m"].yen), (.42, .66))

    def test_fixture_detects_movement(self):
        detected = detect_movements(movement_fixture())
        self.assertTrue(detected)
        self.assertTrue(any("atr" in row.triggers or "z_score" in row.triggers for row in detected))

    def test_flat_series_does_not_trigger(self):
        bars = movement_fixture()
        for index, bar in enumerate(bars):
            bars[index] = type(bar)(bar.pair, bar.timestamp, bar.interval, 155, 155.01, 154.99, 155, bar.provider)
        self.assertEqual(detect_movements(bars), [])

    def test_strongest_is_returned(self):
        items = [movement("a", change=.5), movement("b", change=1.5)]
        self.assertEqual(strongest_movement(items).movement_id, "b")

    def test_unknown_context_is_exact(self):
        context = classify_context()
        self.assertEqual(context.confidence, "unknown")
        self.assertEqual(context.summary, UNKNOWN_CAUSE)

    def test_one_verified_source_is_possible(self):
        context = classify_context([{"verified": True, "summary": "米指標", "url": "https://example.com"}])
        self.assertEqual(context.confidence, "possible")

    def test_two_verified_sources_are_likely(self):
        context = classify_context([
            {"verified": True, "summary": "米指標", "url": "https://a.example"},
            {"verified": True, "summary": "米指標", "url": "https://b.example"},
        ])
        self.assertEqual(context.confidence, "likely")

    def test_intervention_requires_official_confirmation(self):
        context = classify_context([{"verified": True, "summary": "介入観測"}])
        self.assertNotIn("介入が確認", context.summary)
        official = classify_context([], official_mof_confirmation=True)
        self.assertTrue(official.official_intervention_confirmation)

    def test_post_is_within_x_limit(self):
        text = build_post(movement())
        self.assertLessEqual(len(text), 280)
        self.assertIn("USD/JPY", text)

    def test_post_styles(self):
        self.assertNotEqual(build_post(movement(), style="fx_breaking"), build_post(movement(), style="fx_misconception"))
        self.assertIn("注目", build_post(movement(), style="fx_what_to_watch"))

    def test_post_contains_no_advice(self):
        text = build_post(movement())
        for word in ("買い", "売り", "推奨", "予想"):
            self.assertNotIn(word, text)

    def test_first_alert_allowed_and_duplicate_blocked(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            row = movement()
            self.assertTrue(check_alert_gate(row).allowed)
            remember_alert(row, status="posted")
            self.assertEqual(check_alert_gate(row).reason, "duplicate_movement")

    def test_hourly_limit(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            remember_alert(movement("first"), status="posted")
            self.assertEqual(check_alert_gate(movement("second")).reason, "hourly_limit")

    def test_reversal_override_after_hour(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"STATE_DIR": temp, "FX_COOLDOWN_MINUTES": "90"}
        ):
            remember_alert(movement("first", when=now - timedelta(minutes=70)), status="posted")
            decision = check_alert_gate(movement("second", direction="down", change=1.2, when=now), now=now)
            self.assertTrue(decision.allowed)

    def test_same_direction_additional_half_percent_after_hour(self):
        now = datetime.now(timezone.utc)
        first = movement("first", change=.8, when=now - timedelta(minutes=70))
        second = movement("second", change=1.7, when=now)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"STATE_DIR": temp}):
            remember_alert(first, status="posted")
            self.assertTrue(check_alert_gate(second, now=now).allowed)

    def test_daily_limit(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"STATE_DIR": temp, "FX_MAX_ALERTS_PER_DAY": "2"}
        ):
            remember_alert(movement("one", when=now - timedelta(hours=4)), status="posted")
            remember_alert(movement("two", when=now - timedelta(hours=2)), status="posted")
            self.assertEqual(check_alert_gate(movement("three", when=now), now=now).reason, "daily_limit")


if __name__ == "__main__":
    unittest.main()
