import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.metrics_collector import save_snapshot
from common.xai_integration import (
    match_topic,
    prioritize_candidates,
    record_downstream_event,
)
from common.xai_quality import cost_breakdown, funnel
from common import xai_radar_v2 as radar
import local_finance_bot

JST = timezone(timedelta(hours=9))


class Candidate:
    def __init__(self, title):
        self.title = title


class FakeResponses:
    def __init__(self, text, *, fail=False):
        self.text = text
        self.fail = fail

    def create(self, **_kwargs):
        if self.fail:
            raise RuntimeError("network")
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cost_in_usd_ticks=1_000_000,
            num_server_side_tools_used=1,
        )
        return SimpleNamespace(output_text=self.text, usage=usage, id="response-1")


class FakeClient:
    def __init__(self, text, *, fail=False):
        self.responses = FakeResponses(text, fail=fail)


def topic_payload():
    return json.dumps({"topics": [{
        "topic": "AI chips",
        "tickers": ["NVDA"],
        "category": "semiconductor",
        "summary": "discussion",
        "observed_mention_count": 12,
        "velocity_score": 8,
        "acceleration_score": 9,
        "representative_posts": [],
        "representative_accounts": [],
        "source_reliability": "medium",
        "primary_source_available": False,
        "source_confirmation": "x_discussion",
    }]})


class XaiCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "OUTPUT_DIR": self.temp.name,
            "XAI_ENABLED": "true",
            "XAI_X_SEARCH_ENABLED": "true",
            "XAI_API_KEY": "test-key",
            "XAI_MODEL": "grok-test",
            "XAI_MONTHLY_BUDGET_USD": "20",
            "XAI_MAX_SEARCH_CALLS_PER_DAY": "2",
            "XAI_EVENT_MAX_SEARCH_CALLS_PER_DAY": "4",
            "XAI_EVENT_BURST_ENABLED": "true",
            "XAI_EVENT_MODE": "false",
            "XAI_TARGET_COST_PER_CALL_USD": "0.10",
            "QUOTE_QUEUE_ENABLED": "true",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_x_search_flag_stops_manual_refresh_too(self):
        with patch.dict(os.environ, {"XAI_X_SEARCH_ENABLED": "false"}):
            result = radar.refresh(client=FakeClient(topic_payload()))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "x_search_disabled")

    def test_budget_uses_calendar_month_and_reserves_next_call(self):
        path = Path(self.temp.name) / "xai"
        path.mkdir()
        now = datetime(2026, 7, 31, 12, tzinfo=JST)
        rows = [
            {"timestamp": "2026-06-30T23:00:00+09:00", "reported_cost_usd": 19.9,
             "status": "success"},
            {"timestamp": now.isoformat(), "reported_cost_usd": 0.05,
             "status": "success"},
        ]
        (path / "api_usage.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        usage = radar.usage_summary(now)
        self.assertEqual(usage["monthly_calls"], 1)
        self.assertEqual(usage["spent_usd"], 0.05)
        with patch.dict(os.environ, {"XAI_MONTHLY_BUDGET_USD": "0.10"}):
            allowed, reason = radar._can_call(now)
        self.assertFalse(allowed)
        self.assertEqual(reason, "monthly_budget_reserve")

    def test_official_fomc_day_automatically_enables_four_windows(self):
        now = datetime(2026, 7, 29, 21, 0, tzinfo=JST)
        status = radar.important_event_status(now)
        plan = radar.radar_plan(now)
        self.assertTrue(status["active"])
        self.assertEqual(plan["mode"], "event")
        self.assertEqual(plan["daily_limit"], 4)
        self.assertEqual(set(plan["windows_jst"]), {"00:00", "06:00", "21:00", "22:30"})

    def test_daemon_schedule_uses_event_windows(self):
        with patch("common.xai_radar.radar_plan", return_value={
            "windows_jst": ["00:00", "06:00", "21:00", "22:30"],
        }):
            schedule = local_finance_bot.load_schedule()
        self.assertTrue(schedule["radar"]["enabled"])
        self.assertEqual(
            schedule["radar"]["times"],
            ["00:00", "06:00", "21:00", "22:30"],
        )

    def test_xai_reorders_but_does_not_create_candidates(self):
        topics = [{
            "topic": "AI chips", "tickers": ["NVDA"],
            "velocity_score": 8, "acceleration_score": 9,
        }]
        ordinary = Candidate("General market recap")
        matched = Candidate("NVDA AI chips demand rises")
        with patch.dict(os.environ, {"XAI_SCORE_BONUS_ENABLED": "true"}):
            result = prioritize_candidates([ordinary, matched], topics)
        self.assertIs(result[0], matched)
        self.assertEqual(match_topic(ordinary.title, topics), None)

    def test_xai_score_bonus_is_disabled_by_default(self):
        topics = [{
            "topic": "AI chips", "tickers": ["NVDA"],
            "velocity_score": 8, "acceleration_score": 9,
        }]
        ordinary = Candidate("General market recap")
        matched = Candidate("NVDA AI chips demand rises")
        with patch.dict(os.environ, {"XAI_SCORE_BONUS_ENABLED": "false"}):
            result = prioritize_candidates([ordinary, matched], topics)
        self.assertEqual(result, [ordinary, matched])

    def test_downstream_funnel_counts_candidate_post_and_metrics(self):
        root = Path(self.temp.name)
        (root / "xai").mkdir(exist_ok=True)
        now = datetime.now(JST)
        (root / "xai" / "api_usage.jsonl").write_text(json.dumps({
            "timestamp": now.isoformat(), "run_id": "run-1", "status": "success",
            "reported_cost_usd": 0.2, "topics_returned": 2,
        }) + "\n", encoding="utf-8")
        (root / "posted_history.json").write_text(json.dumps([{
            "tweet_id": "123", "posted_at": now.isoformat(),
            "radar_run_id": "run-1", "radar_topic": "AI chips",
        }]), encoding="utf-8")
        record_downstream_event(
            "run-1", "news_candidate", candidate_id="source|title", now=now)
        record_downstream_event(
            "run-1", "post_created", tweet_id="123", now=now)
        for stage, hours in (("1h", 1), ("24h", 24)):
            save_snapshot({
                "tweet_id": "123", "stage": stage, "status": "collected",
                "metrics_collected_at": (now + timedelta(hours=hours)).isoformat(),
                "impressions": 100,
            }, root / "metrics_snapshots.jsonl")
        result = cost_breakdown(30)
        flow = funnel(30)
        self.assertEqual(result["news_candidates_created"], 1)
        self.assertEqual(result["posts_created"], 1)
        self.assertEqual(flow["metrics_1h"], 1)
        self.assertEqual(flow["metrics_24h"], 1)

    def test_fail_closed_option_discards_stale_cache_on_error(self):
        with patch.dict(os.environ, {"XAI_FAIL_OPEN": "false"}):
            result = radar.refresh(
                client=FakeClient(topic_payload(), fail=True), force=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["topics"], [])


if __name__ == "__main__":
    unittest.main()
