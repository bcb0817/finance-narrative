import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from common import performance_learning as learning
from common.daily_log_analysis import redact
from common.operations_alerts import notify_impression_strategy


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

    def test_model_output_is_bounded_before_reuse(self):
        review = _review()
        review["impression_strategy"]["tomorrow_focus"] = ["X" * 500] * 20
        review["impression_strategy"]["experiments"] = [{
            "hypothesis": "H" * 500,
            "action": "A" * 500,
            "success_metric": "M" * 500,
        }] * 10
        review["impression_strategy"]["confidence"] = "certain"
        normalized = learning._normalize_review(review)
        strategy = normalized["impression_strategy"]
        self.assertEqual(len(strategy["tomorrow_focus"]), 6)
        self.assertLessEqual(len(strategy["tomorrow_focus"][0]), 240)
        self.assertEqual(len(strategy["experiments"]), 3)
        self.assertEqual(strategy["confidence"], "low")

    def test_stale_strategy_is_not_injected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = datetime.now(learning.JST) - timedelta(hours=48)
            payload = {
                "strategy_id": "old-strategy",
                "date": old.date().isoformat(),
                "generated_at": old.isoformat(),
                "strategy": _review()["impression_strategy"],
            }
            (root / "latest_patterns.md").write_text("通常ルール", encoding="utf-8")
            (root / "latest_impression_strategy.md").write_text(
                "古い戦略を適用しない", encoding="utf-8")
            (root / "latest_impression_strategy.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(learning, "_root", return_value=root), \
                 patch.dict(os.environ, {
                     "STATE_DIR": str(root / "state"),
                     "PERFORMANCE_STRATEGY_MAX_AGE_HOURS": "36",
                 }):
                status = learning.strategy_status()
                context = learning.load_learning_context(max_chars=2000)
            self.assertEqual(status["status"], "stale")
            self.assertNotIn("古い戦略を適用しない", context)
            self.assertIn("通常ルール", context)

    def test_active_strategy_application_records_hash_not_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            now = datetime.now(learning.JST)
            payload = {
                "strategy_id": "active-strategy",
                "date": now.date().isoformat(),
                "generated_at": now.isoformat(),
                "strategy": _review()["impression_strategy"],
            }
            (root / "latest_patterns.md").write_text("通常ルール", encoding="utf-8")
            (root / "latest_impression_strategy.md").write_text(
                "有効な戦略", encoding="utf-8")
            (root / "latest_impression_strategy.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(learning, "_root", return_value=root), \
                 patch.dict(os.environ, {"STATE_DIR": str(root / "state")}):
                wrapped = learning.with_performance_learning("秘密を含まない生成対象")
                status = learning.strategy_status()
                history = (
                    root / "state" / "learning" / "strategy_applications.jsonl"
                ).read_text(encoding="utf-8")
            self.assertIn("有効な戦略", wrapped)
            self.assertEqual(status["application_count"], 1)
            self.assertNotIn("秘密を含まない生成対象", history)
            self.assertIn("prompt_hash", history)

    def test_discord_strategy_notification_is_concise_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp:
            response = Mock()
            response.raise_for_status.return_value = None
            session = Mock()
            session.post.return_value = response
            payload = {
                "strategy_id": "strategy-1",
                "strategy": _review()["impression_strategy"],
            }
            with patch.dict(os.environ, {
                "STATE_DIR": temp,
                "DISCORD_ALERTS_ENABLED": "true",
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/fake",
            }):
                first = notify_impression_strategy(payload, session=session)
                second = notify_impression_strategy(payload, session=session)
            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(session.post.call_count, 1)
            message = session.post.call_args.kwargs["json"]["content"]
            self.assertIn("改善方針を更新", message)
            self.assertNotIn("error_samples", message)
            self.assertNotIn("運用ログ", message)


if __name__ == "__main__":
    unittest.main()
