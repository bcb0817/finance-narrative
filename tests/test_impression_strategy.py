import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common import performance_learning as learning
from common.daily_log_analysis import redact


def _metrics():
    now = datetime.now(learning.JST)
    return [{
        "tweet_id": "123",
        "bot": "news",
        "title": "米国株ニュース",
        "text": "重要な変化を数字で解説",
        "posted_at": (now - timedelta(hours=8)).isoformat(),
        "metrics_collected_at": now.isoformat(),
        "stage": "6h",
        "impressions": 1000,
        "likes": 20,
    }]


def _review():
    return {
        "daily_summary": "数字を冒頭に置いた投稿が伸びた可能性。",
        "top_posts": [],
        "reusable_rules": [{"rule": "重要な数字を先に示す", "evidence": "imp/h"}],
        "rolling_rules": [{"rule": "結論から書く", "evidence": "上位投稿"}],
        "avoid_patterns": [{"rule": "根拠のない断定", "reason": "安全性"}],
        "impression_strategy": {
            "objective": "翌日のimp/hを改善する",
            "tomorrow_focus": ["大きな市場変動を優先"],
            "content_rules": [{"rule": "数字を冒頭に置く", "evidence": "上位投稿"}],
            "timing_rules": ["既存スケジュールを維持"],
            "experiments": [{
                "hypothesis": "数字始まりは初速が高い",
                "action": "対象1件で試す",
                "success_metric": "6h imp/hで比較",
            }],
            "avoid": ["煽り表現"],
            "confidence": "medium",
            "limitations": ["対象が1件"],
        },
    }


class ImpressionStrategyTests(unittest.TestCase):
    def test_discord_webhook_is_redacted(self):
        secret = "https://discord.com/api/webhooks/123456/secret-token"
        self.assertNotIn(secret, redact(f"webhook={secret}"))

    def test_daily_review_writes_strategy_and_injects_generation_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(learning, "_root", return_value=root), \
                 patch.object(learning, "_log_evidence", return_value={
                     "status": "ok", "summary": {"runs": 4}, "findings": [],
                     "error_samples": [], "secrets_redacted": True,
                 }), \
                 patch("common.openai_service.OpenAIService") as service:
                service.return_value.structured.return_value = _review()
                result = learning.update_daily_learning(_metrics())
                context = learning.load_learning_context(max_chars=10000)

            self.assertEqual(result["status"], "ok")
            payload = json.loads(
                (root / "latest_impression_strategy.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["safety_constraints"]["config_mutation_allowed"])
            self.assertIn("翌日のimp/hを改善", context)
            self.assertIn("事実確認、安全審査", context)

    def test_prompt_contains_redacted_log_evidence_and_strict_strategy_schema(self):
        evidence = {
            "status": "attention",
            "summary": {"errors": 1},
            "findings": [{"category": "network", "count": 1}],
            "error_samples": [{"detail": "<redacted>"}],
            "secrets_redacted": True,
        }
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(learning, "_root", return_value=Path(temp)), \
             patch.object(learning, "_log_evidence", return_value=evidence), \
             patch("common.openai_service.OpenAIService") as service:
            service.return_value.structured.return_value = _review()
            learning.update_daily_learning(_metrics())
            prompt = service.return_value.structured.call_args.args[0]
            schema = service.return_value.structured.call_args.args[1]

        self.assertIn("運用ログ分析", prompt)
        self.assertIn("<redacted>", prompt)
        self.assertIn("impression_strategy", schema["required"])
        self.assertFalse("OPENAI_API_KEY" in prompt)

    def test_context_is_bounded_and_generation_continues_without_strategy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "latest_patterns.md").write_text("A" * 10000, encoding="utf-8")
            with patch.object(learning, "_root", return_value=root), \
                 patch.dict(os.environ, {"PERFORMANCE_LEARNING_ENABLED": "true"}):
                context = learning.load_learning_context(max_chars=700)
                wrapped = learning.with_performance_learning("今回の事実")
            self.assertLessEqual(len(context), 700)
            self.assertIn("今回の事実", wrapped)


if __name__ == "__main__":
    unittest.main()
