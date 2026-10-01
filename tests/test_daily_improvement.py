import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from common import daily_improvement as improvement


# Explicit rollback coverage; editorial-v2 expectations live in test_editorial_policy.
@patch.dict(os.environ, {'EDITORIAL_POLICY_ENABLED': 'false'})
class DailyImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"STATE_DIR": self.temp.name}, clear=False)
        self.env.start()
        self.now = datetime(2026, 8, 26, 22, 10, tzinfo=improvement.JST)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_runs_once_per_jst_day(self):
        active = {"status": "active", "date": "2026-08-26", "strategy_id": "s1"}
        with patch("common.daily_log_analysis.analyze_daily_logs", return_value={
            "status": "ok", "summary": {}, "findings": [],
        }), patch.object(improvement, "_metrics", return_value=[{"tweet_id": "1"}]), \
             patch("common.performance_learning.strategy_status", return_value=active), \
             patch("common.performance_learning.update_daily_learning") as learn, \
             patch.object(improvement, "log_run"):
            first = improvement.run_daily_improvement(now=self.now)
            second = improvement.run_daily_improvement(now=self.now)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["primary_objective"], "インプレッション最大化")
        self.assertEqual(second["status"], "already_completed")
        learn.assert_not_called()

    def test_retries_and_records_root_cause(self):
        statuses = [
            {"status": "not_generated"},
            {"status": "not_generated"},
            {"status": "active", "date": "2026-08-26", "strategy_id": "s2"},
            {"status": "active", "date": "2026-08-26", "strategy_id": "s2"},
        ]
        with patch("common.daily_log_analysis.analyze_daily_logs", side_effect=[
            json.JSONDecodeError("bad", "x", 0),
            {"status": "attention", "summary": {"errors": 1}, "findings": []},
            {"status": "attention", "summary": {"errors": 1}, "findings": []},
        ]), patch.object(improvement, "_metrics", return_value=[{"tweet_id": "1"}]), \
             patch("common.performance_learning.strategy_status", side_effect=statuses), \
             patch("common.performance_learning.update_daily_learning", return_value={"status": "ok"}), \
             patch.object(improvement, "log_error") as errors, \
             patch.object(improvement, "log_run"):
            result = improvement.run_daily_improvement(now=self.now)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"][0]["category"], "data_integrity")
        self.assertEqual(result["attempts"][-1]["action"], "regenerate_and_expand_lookback")
        self.assertEqual(errors.call_count, 2)

    def test_corrupt_state_is_quarantined(self):
        path = Path(self.temp.name) / "daily_improvement_state.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(improvement._read_state(), {})
        self.assertTrue(list(Path(self.temp.name).glob("daily_improvement_state.corrupt-*.json")))


if __name__ == "__main__":
    unittest.main()
