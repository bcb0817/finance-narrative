import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "news_bot"))

from news_bot.post import (
    _actual_post_attempted,
    _post_result_skip_reason,
    candidate_scan_limits,
    failed_market_enrichment_is_veto,
)
from common import daily_post_goal


class NewsGoalRecoveryTests(unittest.TestCase):
    def test_scan_pool_is_larger_than_new_candidate_assessment_limit(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            daily_post_goal, "state_dir", return_value=Path(temp)
        ), patch.dict(os.environ, {
            "NEWS_MAX_CANDIDATES": "15",
            "NEWS_CANDIDATE_POOL_SIZE": "75",
        }):
            self.assertEqual(candidate_scan_limits(), (15, 75))

    def test_optional_market_enrichment_cannot_veto_isolated_rss_editorial(self):
        self.assertFalse(failed_market_enrichment_is_veto(True))
        self.assertTrue(failed_market_enrichment_is_veto(False))

    def test_dry_run_is_not_recorded_as_an_actual_post_attempt(self):
        with patch("news_bot.post._post_enabled_now", return_value=True), \
             patch("news_bot.post.DRY_RUN", True):
            self.assertFalse(_actual_post_attempted())

    def test_missing_post_id_is_not_mislabeled_as_dry_run_when_live(self):
        with patch("news_bot.post.DRY_RUN", False):
            self.assertEqual(_post_result_skip_reason(""), "post_failed_no_tweet_id")
            self.assertEqual(_post_result_skip_reason("123"), "-")


if __name__ == "__main__":
    unittest.main()
