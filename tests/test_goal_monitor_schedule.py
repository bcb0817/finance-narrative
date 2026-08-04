import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import local_finance_bot


class GoalMonitorScheduleTests(unittest.TestCase):
    def test_goal_monitor_is_scheduled_every_three_hours(self):
        with patch.dict(os.environ, {
            "DAILY_GOAL_3H_MONITOR_ENABLED": "true",
            "DAILY_GOAL_MONITOR_INTERVAL_MINUTES": "180",
        }):
            schedule = local_finance_bot.load_schedule()
        self.assertIn("goal-monitor", local_finance_bot.SCHED_BOTS)
        self.assertTrue(schedule["goal-monitor"]["enabled"])
        self.assertEqual(schedule["goal-monitor"]["type"], "interval_minutes")
        self.assertEqual(schedule["goal-monitor"]["every_minutes"], 180)


if __name__ == "__main__":
    unittest.main()
