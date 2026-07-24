import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "common"))

from metrics_collector import enrich_metrics, load_snapshots, save_snapshot, select_stage_snapshot, collect_metrics, due_stage
from media_intelligence import rank_posts, build_daily_summary, generate_shorts_drafts


class MediaIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "metrics.jsonl"

    def tearDown(self): self.tmp.cleanup()

    def test_snapshot_save_and_duplicate_prevention(self):
        row = {"tweet_id": "1", "metrics_collected_at": "2026-01-01T00:00:00+09:00"}
        self.assertTrue(save_snapshot(row, self.path))
        self.assertFalse(save_snapshot(row, self.path))
        self.assertEqual(len(load_snapshots(self.path)), 1)

    def test_stage_selection_1h_6h_24h(self):
        rows = [{"tweet_id": "1", "post_age_hours": h, "stage": f"{h}h"} for h in (1.1, 6.2, 24.3)]
        self.assertEqual(select_stage_snapshot(rows, "1", 1)["post_age_hours"], 1.1)
        self.assertEqual(select_stage_snapshot(rows, "1", 6)["post_age_hours"], 6.2)
        self.assertEqual(select_stage_snapshot(rows, "1", 24)["post_age_hours"], 24.3)

    def test_missed_stages_are_not_backfilled_with_current_metrics(self):
        self.assertEqual(due_stage(1.2, set()), "1h")
        self.assertEqual(due_stage(7.0, set()), "6h")
        self.assertEqual(due_stage(25.0, set()), "24h")
        self.assertIsNone(due_stage(49.0, set()))
        self.assertIsNone(due_stage(7.0, {"6h"}))

    def test_impressions_per_hour_and_engagement(self):
        now = datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=9)))
        row = enrich_metrics({"impressions": 240, "likes": 12, "reposts": 6, "replies": 3, "quotes": 3},
                             (now - timedelta(hours=24)).isoformat(), now)
        self.assertEqual(row["impressions_per_hour"], 10)
        self.assertEqual(row["engagement_rate"], 0.1)

    def test_top_bottom_and_age_correction(self):
        posts = [{"tweet_id": str(i), "text": str(i)} for i in range(1, 5)]
        snaps = [{"tweet_id": str(i), "post_age_hours": 24, "impressions": i * 100,
                  "impressions_per_hour": i, "stage": "24h"} for i in range(1, 5)]
        ranked = rank_posts(posts, snaps, count=2)
        self.assertEqual([r["tweet_id"] for r in ranked["top"]], ["4", "3"])
        self.assertEqual([r["tweet_id"] for r in ranked["bottom"]], ["1", "2"])
        self.assertEqual(rank_posts(posts, [{**snaps[0], "post_age_hours": 6}], count=1)["eligible"], 0)

    def test_rank_does_not_substitute_a_different_stage(self):
        posts = [{"tweet_id": "1", "text": "sample"}]
        snapshots = [{"tweet_id": "1", "stage": "24h", "post_age_hours": 24,
                      "impressions": 1000, "metrics_collected_at": "2026-01-02T00:00:00+09:00"}]
        self.assertEqual(rank_posts(posts, snapshots, stage="6h")["eligible"], 0)
        self.assertEqual(rank_posts(posts, snapshots, stage="24h")["eligible"], 1)

    def test_insufficient_data(self):
        summary = build_daily_summary([], [], datetime.now(timezone(timedelta(hours=9))))
        self.assertEqual(summary["data_status"], "データ不足")

    def test_daily_total_uses_all_unique_eligible_posts(self):
        now = datetime.now(timezone(timedelta(hours=9)))
        posts = [{"tweet_id": str(i), "posted_at": (now - timedelta(hours=25)).isoformat()} for i in range(1, 5)]
        snaps = [{"tweet_id": str(i), "stage": "24h", "post_age_hours": 24,
                  "impressions": i * 10, "impressions_per_hour": i,
                  "metrics_collected_at": now.isoformat()} for i in range(1, 5)]
        self.assertEqual(build_daily_summary(posts, snaps, now)["known_impressions"], 100)

    def test_shorts_json_utf8_and_windows_space_path(self):
        old = os.environ.get("OUTPUT_DIR")
        try:
            os.environ["OUTPUT_DIR"] = str(Path(self.tmp.name) / "path with space")
            paths = generate_shorts_drafts([{"tweet_id": "1", "text": "日本語ニュース"}], "2026-01-01")
            data = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertIn("日本語", data["source_text"])
        finally:
            if old is None: os.environ.pop("OUTPUT_DIR", None)
            else: os.environ["OUTPUT_DIR"] = old

    def test_api_error_does_not_raise(self):
        class Bad:
            def get_tweets(self, **kwargs): raise RuntimeError("offline")
        # No local due records is still a successful no-op and never stops daemon.
        self.assertIn(collect_metrics(client=Bad())["status"], ("ok", "error"))


if __name__ == "__main__": unittest.main()
