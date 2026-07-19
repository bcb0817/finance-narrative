import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common.runtime import JST
from common.posting_policy import check_post, policy_status


class PostingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "STATE_DIR": str(self.state),
            "DAILY_POST_LIMIT": "30",
            "HOURLY_POST_LIMIT": "2",
            "TICKER_COOLDOWN_MINUTES": "180",
            "THEME_COOLDOWN_MINUTES": "90",
            "X_CONTENT_CREATE_USD": "0.015",
            "X_WRITE_MONTHLY_BUDGET_USD": "15",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _write(self, entries):
        (self.state / "posted_history.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def test_blocks_urls(self):
        self._write([])
        self.assertEqual(check_post("詳細 https://example.com").reason, "url_not_allowed")

    def test_blocks_third_post_in_hour(self):
        now = datetime(2026, 7, 19, 12, 40, tzinfo=JST)
        self._write([
            {"tweet_id": "1", "text": "one", "posted_at": (now - timedelta(minutes=30)).isoformat()},
            {"tweet_id": "2", "text": "two", "posted_at": (now - timedelta(minutes=10)).isoformat()},
        ])
        self.assertTrue(check_post("three", now=now).reason.startswith("hourly_limit"))

    def test_blocks_ticker_and_theme_cooldowns(self):
        now = datetime(2026, 7, 19, 12, 40, tzinfo=JST)
        self._write([{
            "tweet_id": "1", "text": "$NVDA 半導体の需要", "posted_at": (now - timedelta(hours=1)).isoformat()
        }])
        self.assertEqual(check_post("NVDAの決算", now=now).reason, "ticker_cooldown")
        self.assertEqual(check_post("半導体セクター", now=now).reason, "theme_cooldown")

    def test_status_reports_cost(self):
        now = datetime(2026, 7, 19, 12, 40, tzinfo=JST)
        self._write([{"tweet_id": "1", "text": "one", "posted_at": now.isoformat()}])
        status = policy_status(now=now)
        self.assertEqual(status["today_count"], 1)
        self.assertEqual(status["estimated_x_write_usd"], 0.015)


if __name__ == "__main__":
    unittest.main()
