import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from common.post_registry import hours_since_last_post, posting_inactive, record_post


class RegistryEnrichmentTests(unittest.TestCase):
    def test_posting_inactive_uses_latest_cross_bot_post(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone(timedelta(hours=9)))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}):
            self.assertTrue(posting_inactive(3, now))
            record_post("1", text="old", bot="narrative",
                        posted_at=(now - timedelta(hours=3, minutes=1)).isoformat())
            self.assertGreater(hours_since_last_post(now), 3)
            self.assertTrue(posting_inactive(3, now))
            record_post("2", text="new", bot="news",
                        posted_at=(now - timedelta(hours=2, minutes=59)).isoformat())
            self.assertFalse(posting_inactive(3, now))

    def test_metadata_update_is_appended_to_registry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}):
            record_post("123456", text="速報です", bot="news", mode="normal")
            record_post("123456", text="速報です", title="AI決算", source="Example",
                        url="https://example.com", bot="news", mode="normal",
                        extra={"post_value": 8})
            rows = [json.loads(line) for line in (Path(tmp) / "post_registry.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["event_type"] for r in rows], ["created", "metadata_update"])
            self.assertEqual(rows[-1]["post_value"], 8)
            self.assertEqual(rows[-1]["title"], "AI決算")


if __name__ == "__main__": unittest.main()
