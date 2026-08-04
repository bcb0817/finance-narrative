import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common import daily_post_goal as goal
from common.dynamic_posting import posting_window


class DailyPostGoalTests(unittest.TestCase):
    def _write_posts(self, root: Path, now: datetime, count: int) -> None:
        rows = [
            {
                "tweet_id": str(index),
                "posted_at": (now - timedelta(days=1)).replace(
                    hour=index % 24, minute=0
                ).isoformat(),
            }
            for index in range(count)
        ]
        (root / "posted_history.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )

    def test_missed_target_applies_only_bounded_volume_tuning_once(self):
        now = datetime(2026, 8, 4, 22, 0, tzinfo=goal.JST)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_posts(root, now, 12)
            env = {
                "DAILY_POST_TARGET": "20",
                "DAILY_GOAL_AUTO_TUNE_ENABLED": "true",
                "NEWS_IDLE_FALLBACK_HOURS": "3",
                "QUIET_MIN_GAP_MINUTES": "60",
                "QUIET_MAX_GAP_MINUTES": "120",
            }
            with patch.object(goal, "state_dir", return_value=root), patch.dict(
                os.environ, env
            ):
                first = goal.review_daily_goal(now=now)
                second = goal.review_daily_goal(now=now)
                policy = json.loads(
                    (root / "learning" / "daily_post_goal_policy.json").read_text(
                        encoding="utf-8"
                    )
                )
            self.assertEqual(first["status"], "missed")
            self.assertEqual(first["shortfall"], 8)
            self.assertEqual(first["program_adjustment"]["status"], "applied")
            self.assertEqual(second["program_adjustment"]["status"], "already_applied")
            self.assertEqual(policy["effective_values"]["NEWS_IDLE_FALLBACK_HOURS"], 2)
            self.assertEqual(policy["effective_values"]["QUIET_MIN_GAP_MINUTES"], 50)
            self.assertEqual(policy["effective_values"]["QUIET_MAX_GAP_MINUTES"], 105)
            self.assertEqual(policy["effective_values"]["NEWS_POST_VALUE_THRESHOLD"], 6)
            self.assertEqual(policy["effective_values"]["DAILY_POST_LIMIT"], 32)
            self.assertEqual(policy["effective_values"]["HOURLY_POST_LIMIT"], 3)
            self.assertEqual(policy["effective_values"]["X_WRITE_MONTHLY_BUDGET_USD"], 16)
            self.assertEqual(policy["effective_values"]["SAFETY_REVIEW_RETRY_LIMIT"], 1)
            self.assertIn("fact_confirmation", policy["protected_controls_unchanged"])
            self.assertFalse(policy["arbitrary_source_editing"])

    def test_achieved_target_does_not_change_program(self):
        now = datetime(2026, 8, 4, 22, 0, tzinfo=goal.JST)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_posts(root, now, 20)
            with patch.object(goal, "state_dir", return_value=root), patch.dict(
                os.environ, {"DAILY_POST_TARGET": "20"}
            ):
                result = goal.review_daily_goal(now=now)
            self.assertEqual(result["status"], "achieved")
            self.assertEqual(result["program_adjustment"]["status"], "not_needed")
            self.assertFalse((root / "learning" / "daily_post_goal_policy.json").exists())

    def test_status_is_read_only_and_does_not_consume_daily_adjustment(self):
        now = datetime(2026, 8, 4, 12, 0, tzinfo=goal.JST)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_posts(root, now, 0)
            with patch.object(goal, "state_dir", return_value=root):
                goal.goal_status(now)
            self.assertFalse((root / "learning" / "daily_post_goal_reviews.jsonl").exists())

    def test_dynamic_window_reads_bounded_policy(self):
        now = datetime(2026, 8, 4, 12, 0, tzinfo=goal.JST)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            learning = root / "learning"
            learning.mkdir()
            (learning / "daily_post_goal_policy.json").write_text(
                json.dumps({"effective_values": {
                    "QUIET_MIN_GAP_MINUTES": 45,
                    "QUIET_MAX_GAP_MINUTES": 75,
                    "NEWS_IDLE_FALLBACK_HOURS": 1,
                }}), encoding="utf-8"
            )
            with patch.object(goal, "state_dir", return_value=root), patch(
                "common.dynamic_posting.hours_since_last_post", return_value=None
            ):
                result = posting_window(0.0, now)
            self.assertGreaterEqual(result["required_gap_minutes"], 45)
            self.assertLessEqual(result["required_gap_minutes"], 75)

    def test_catch_up_requests_one_extra_run_only_when_behind(self):
        now = datetime(2026, 8, 4, 12, 0, tzinfo=goal.JST)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                {
                    "tweet_id": str(index),
                    "posted_at": now.replace(hour=index, minute=0).isoformat(),
                }
                for index in range(7)
            ]
            (root / "posted_history.json").write_text(
                json.dumps(rows), encoding="utf-8"
            )
            env = {
                "DAILY_POST_TARGET": "20",
                "DAILY_GOAL_CATCH_UP_ENABLED": "true",
                "DAILY_GOAL_MAX_EXTRA_NEWS_RUNS": "1",
            }
            with patch.object(goal, "state_dir", return_value=root), patch.dict(
                os.environ, env
            ):
                self.assertEqual(goal.catch_up_runs(now), 1)
                rows.extend(
                    {
                        "tweet_id": str(index),
                        "posted_at": now.replace(hour=index, minute=0).isoformat(),
                    }
                    for index in range(7, 11)
                )
                (root / "posted_history.json").write_text(
                    json.dumps(rows), encoding="utf-8"
                )
                self.assertEqual(goal.catch_up_runs(now), 0)

    def test_target_pace_reaches_twenty_by_23_jst(self):
        with patch.dict(os.environ, {
            "DAILY_POST_TARGET": "20",
            "DAILY_GOAL_TARGET_DEADLINE_HOUR": "23",
        }):
            self.assertEqual(
                goal.expected_post_count(
                    datetime(2026, 8, 4, 23, 0, tzinfo=goal.JST)
                ),
                20,
            )


if __name__ == "__main__":
    unittest.main()
