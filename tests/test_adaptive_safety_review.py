import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common import openai_client


def review(*, ok: bool, risk: str = "low", advice: bool = False) -> dict:
    return {
        "ok_to_post": ok,
        "risk_level": risk,
        "reason": "review",
        "contains_investment_advice": advice,
        "contains_buy_sell_recommendation": False,
        "contains_unverified_numbers": False,
        "contains_price_prediction": False,
        "too_aggressive": False,
    }


class AdaptiveSafetyReviewTests(unittest.TestCase):
    def _policy(self, root: Path, retry_limit: int) -> None:
        folder = root / "learning"
        folder.mkdir(parents=True)
        (folder / "daily_post_goal_policy.json").write_text(
            json.dumps({
                "effective_values": {"SAFETY_REVIEW_RETRY_LIMIT": retry_limit}
            }),
            encoding="utf-8",
        )

    def test_soft_rejection_gets_one_independent_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._policy(root, 1)
            with patch.dict(os.environ, {"STATE_DIR": temp}), patch.object(
                openai_client, "OpenAIService"
            ) as service:
                service.return_value.moderate.return_value = True
                service.return_value.structured.side_effect = [
                    review(ok=False),
                    review(ok=True),
                ]
                result = openai_client.review_tweet_with_openai(
                    "safe text", "title", "official source"
                )
            self.assertTrue(result["ok_to_post"])
            self.assertTrue(result["review_retried"])
            self.assertEqual(service.return_value.structured.call_count, 2)

    def test_hard_safety_rejection_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._policy(root, 1)
            with patch.dict(os.environ, {"STATE_DIR": temp}), patch.object(
                openai_client, "OpenAIService"
            ) as service:
                service.return_value.moderate.return_value = True
                service.return_value.structured.return_value = review(
                    ok=True, risk="high", advice=True
                )
                result = openai_client.review_tweet_with_openai(
                    "buy now", "title", "source"
                )
            self.assertFalse(result["ok_to_post"])
            self.assertEqual(service.return_value.structured.call_count, 1)


if __name__ == "__main__":
    unittest.main()
