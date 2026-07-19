import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common.runtime import JST
from common.report import fetch_metrics


class ReportMetricsScheduleTests(unittest.TestCase):
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
