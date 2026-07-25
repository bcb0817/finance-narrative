import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common.runtime import JST
from common.report import _engagement, _post_title, fetch_metrics
from common.x_client import get_metrics_client


class ReportMetricsScheduleTests(unittest.TestCase):
    def test_missing_metrics_do_not_break_engagement_totals(self):
        self.assertEqual(_engagement({"likes": None, "retweets": None, "replies": None}), 0)
        self.assertEqual(_engagement({"likes": "2", "retweets": 1, "replies": None}), 3)

    def test_missing_title_uses_text_or_placeholder(self):
        self.assertEqual(_post_title({"text": "metrics-only post"}, 60), "metrics-only post")
        self.assertEqual(_post_title({}, 60), "(本文なし)")

    def test_metrics_client_uses_bearer_token(self):
        with patch.dict(os.environ, {"BEARER_TOKEN": "test-token"}, clear=True):
            with patch("common.x_client.tweepy.Client") as client:
                get_metrics_client()
        client.assert_called_once_with(bearer_token="test-token")

    def test_recent_post_uses_cache_without_x_refresh(self):
        now = datetime.now(JST)
        cached = [{"tweet_id": "1", "likes": 3, "fetched_at": now.isoformat()}]
        history = [{
            "tweet_id": "1",
            "posted_at": (now - timedelta(hours=2)).isoformat(),
            "text": "recent",
        }]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}):
            root = Path(tmp)
            (root / "posted_history.json").write_text(json.dumps(history), encoding="utf-8")
            (root / "metrics_history.json").write_text(json.dumps(cached), encoding="utf-8")
            result = fetch_metrics()
            self.assertEqual(result, cached)


if __name__ == "__main__":
    unittest.main()
