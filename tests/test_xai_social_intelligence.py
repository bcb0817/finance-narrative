import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.discord_schema import sanitize_payload
from common.operations_alerts import notify_xai_research_result
from common.runtime import JST
from common.xai_social_intelligence import (
    _schema,
    append_jsonl,
    adaptive_cost_policy,
    canonicalize,
    classify_account,
    cleanup,
    compute_delta,
    consume_event_trigger,
    cost_attribution,
    deduplicate_candidates,
    enqueue_fx_movement,
    exploration_status,
    gather_event_candidates,
    import_legacy_research,
    integrate_research_results,
    make_candidate,
    match_integrated_analysis,
    match_news_event,
    normalize_posts,
    normalize_tickers,
    pending_event_trigger,
    read_jsonl,
    record_content_opportunity_use,
    record_integrated_analysis_use,
    record_post_outcome,
    run,
    select_candidates,
    shadow_record_news,
    shadow_report,
    social_report,
    social_cache_status,
)


def event_result(candidate, *, confidence="possible", posts=None, channels=None):
    channels = channels or ["x", "note_free", "threads", "youtube_short", "youtube_long"]
    return {
        "event_id": candidate.candidate_id,
        "topic_summary": "確認済みイベントへの観測反応",
        "why_people_are_discussing_it": "市場への影響が注目されたため",
        "dominant_narrative": "影響の大きさを巡る議論",
        "alternative_narratives": ["影響は限定的という見方"],
        "strongest_dissent": "織り込み済みとの反論",
        "common_misconception": "X上の観測件数を市場全体とみなす誤解",
        "unanswered_questions": ["公式発表の追加情報はあるか"],
        "useful_expert_points": ["一次情報を優先する"],
        "market_implication_candidates": ["短期変動との時間的整合"],
        "facts_needing_confirmation": ["原因と価格変動の因果関係"],
        "potentially_false_claims": ["未確認の介入観測"],
        "content_angles": [
            {
                "angle": f"{channel}向けの論点整理",
                "why_useful": "誤解を訂正できる",
                "recommended_format": channel,
                "confidence": "medium",
            }
            for channel in channels
        ],
        "channel_fit": channels,
        "novelty_assessment": "既存報道への反応として新規",
        "confidence": confidence,
        "observed_posts": posts or [],
    }


class FakeResponses:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        details = SimpleNamespace(cached_tokens=12)
        output_details = SimpleNamespace(reasoning_tokens=4)
        usage = SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            input_tokens_details=details,
            output_tokens_details=output_details,
            cost_in_usd_ticks=125_000_000,
            num_server_side_tools_used=1,
        )
        return SimpleNamespace(
            output_text=json.dumps(self.payload, ensure_ascii=False),
            usage=usage,
            id="response-test",
        )


class FakeClient:
    def __init__(self, payload, error=None):
        self.responses = FakeResponses(payload, error=error)


class FakeDiscordResponse:
    def raise_for_status(self):
        return None


class FakeDiscordSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeDiscordResponse()


class XaiSocialIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime.now(JST).replace(microsecond=0)
        self.env = patch.dict(
            os.environ,
            {
                "STATE_DIR": self.temp.name,
                "OUTPUT_DIR": self.temp.name,
                "LOG_DIR": self.temp.name,
                "XAI_ENABLED": "true",
                "XAI_X_SEARCH_ENABLED": "true",
                "XAI_API_KEY": "fixture-only-key",
                "XAI_MODEL": "grok-4.5",
                "XAI_MONTHLY_BUDGET_USD": "20",
                "XAI_MAX_SEARCH_CALLS_PER_DAY": "2",
                "XAI_EVENT_MAX_SEARCH_CALLS_PER_DAY": "4",
                "XAI_EVENT_MODE": "false",
                "XAI_SCORE_BONUS_ENABLED": "false",
                "XAI_EXPLORATION_BUDGET_PERCENT": "20",
                "XAI_EVENT_RESEARCH_COOLDOWN_MINUTES": "60",
                "DISCORD_ALERTS_ENABLED": "false",
                "XAI_SAFE_DISABLED": "false",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def candidate(self, **overrides):
        values = {
            "source_type": "rss_news",
            "source_id": "source-1",
            "title": "Alphabet GOOGL earnings announcement",
            "entities": ["Google"],
            "tickers": ["GOOGL"],
            "urgency_score": 8,
            "market_impact_score": 8,
            "novelty_score": 7,
            "social_research_value": 9,
            "now": self.now,
        }
        values.update(overrides)
        return make_candidate(**values)

    def test_candidate_has_complete_contract_and_event_query(self):
        row = self.candidate()
        expected = {
            "candidate_id", "source_type", "source_id", "title", "canonical_topic",
            "entities", "tickers", "currencies", "countries", "event_type",
            "official", "reliability_tier", "published_at", "detected_at",
            "source_urls", "confirmed_facts", "unconfirmed_claims",
            "open_questions", "market_movement", "urgency_score",
            "market_impact_score", "novelty_score", "social_research_value",
            "xai_search_query", "xai_search_priority", "expires_at",
        }
        self.assertEqual(set(row.to_dict()), expected)
        self.assertIn("GOOGL", row.xai_search_query)
        self.assertIn("-giveaway", row.xai_search_query)

    def test_local_sources_generate_candidates_without_external_api(self):
        rows = gather_event_candidates(
            rss_items=[{
                "title": "SEC announces new disclosure rule",
                "url": "https://example.test/official",
                "source": "SEC",
                "source_group": "official_sec",
                "published_at": self.now.isoformat(),
            }],
            now=self.now,
        )
        self.assertTrue(rows)
        self.assertTrue(rows[0].official)

    def test_dedup_alias_ticker_and_maximum_concentration(self):
        first = self.candidate(source_id="a", title="Alphabet GOOGL earnings")
        duplicate = self.candidate(source_id="b", title="Google GOOG earnings")
        others = [
            self.candidate(
                source_id=f"x{index}",
                title=f"Company {index} earnings",
                entities=[f"Company {index}"],
                tickers=[f"X{index}"],
            )
            for index in range(8)
        ]
        deduped = deduplicate_candidates([first, duplicate, *others])
        self.assertLess(len(deduped), 10)
        self.assertLessEqual(len(select_candidates(deduped, maximum=9)), 5)
        self.assertEqual(normalize_tickers(["$goog", "GOOGL"]), ["GOOGL"])
        self.assertEqual(canonicalize("Alphabet"), "google")

    def test_post_dedup_quality_and_observed_scope_metrics(self):
        posts = [
            {"post_id": "1", "url": "https://x.com/a/1", "account": "a", "excerpt": "独自の分析です"},
            {"post_id": "1", "url": "https://x.com/a/1", "account": "a", "excerpt": "独自の分析です"},
            {"post_id": "2", "url": "https://x.com/b/2", "account": "b", "excerpt": "独自の分析です。"},
            {"post_id": "3", "url": "https://x.com/c/3", "account": "c", "excerpt": "join my telegram signal group"},
            {"post_id": "4", "url": "https://x.com/d/4", "account": "d", "excerpt": "別の見方", "is_reply": True},
        ]
        accepted, metrics = normalize_posts(posts)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(metrics["observed_result_count"], 5)
        self.assertEqual(metrics["unique_original_posts"], 2)
        self.assertEqual(metrics["unique_accounts"], 2)
        self.assertGreater(metrics["duplicate_ratio"], 0)
        self.assertIn("observed_", metrics["measurement_scope"])

    def test_same_thread_quote_source_and_account_burst_are_bounded(self):
        posts = [
            {"post_id": str(i), "account": "same", "excerpt": f"異なる論点 {i}",
             "thread_id": "thread" if i < 2 else "", "quoted_post_id": "root" if i in (2, 3) else ""}
            for i in range(5)
        ]
        accepted, _ = normalize_posts(posts)
        self.assertLessEqual(len([row for row in accepted if row["account"] == "same"]), 2)

    def test_account_classification_uses_type_not_followers(self):
        self.assertEqual(
            classify_account("authority", hint="official_government")["category"],
            "official_government",
        )
        self.assertEqual(
            classify_account("expert", hint="economist")["category"], "economist"
        )
        self.assertEqual(
            classify_account("seller", text="guaranteed profit affiliate")["category"],
            "promotional",
        )

    def test_first_observation_has_no_fabricated_velocity(self):
        result = compute_delta(
            {"observed_at": self.now.isoformat(), "metrics": {}}, None
        )
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["observed_velocity_score"])
        self.assertIsNone(result["observed_acceleration_score"])

    def test_two_and_three_stage_delta(self):
        first = {
            "observed_at": self.now.isoformat(),
            "metrics": {"unique_original_posts": 1, "unique_accounts": 1},
            "posts": [{"post_id": "1", "account": "a"}],
            "interpretation": {"dominant_narrative": "A", "alternative_narratives": []},
            "delta": {},
        }
        second = {
            "observed_at": (self.now + timedelta(hours=1.5)).isoformat(),
            "metrics": {"unique_original_posts": 2, "unique_accounts": 2},
            "posts": [{"post_id": "1", "account": "a"}, {"post_id": "2", "account": "b"}],
            "interpretation": {"dominant_narrative": "B", "alternative_narratives": ["C"]},
        }
        delta2 = compute_delta(second, first)
        self.assertTrue(delta2["comparable"])
        self.assertEqual(delta2["new_unique_posts"], 1)
        self.assertIsNone(delta2["observed_acceleration_score"])
        second["delta"] = delta2
        third = {
            **second,
            "observed_at": (self.now + timedelta(hours=3)).isoformat(),
            "posts": [*second["posts"], {"post_id": "3", "account": "c"}],
        }
        delta3 = compute_delta(third, second)
        self.assertIsNotNone(delta3["observed_acceleration_score"])

    def test_fixture_modes_content_channels_and_no_x_post(self):
        candidate = self.candidate()
        for mode in ("event_reaction", "movement_explanation", "expert_watch", "exploration"):
            with self.subTest(mode=mode):
                candidate = self.candidate(source_id=mode, title=f"{mode} GOOGL")
                payload = {"events": [event_result(candidate)]}
                result = run(
                    fixture_response=payload,
                    candidates=[candidate],
                    radar_mode=mode,
                    now=self.now,
                )
                self.assertEqual(result["status"], "success")
                self.assertFalse(result["x_post_called"])
                channels = {row["channel"] for row in result["opportunities"]}
                self.assertTrue({"x", "note_free", "threads", "youtube_short", "youtube_long"} <= channels)

    def test_fx_xai_cannot_confirm_cause(self):
        candidate = self.candidate(
            source_type="fx_movement",
            source_id="fx-1",
            title="USDJPY 15m +1.0% movement",
            tickers=["USDJPY"],
        )
        result = run(
            fixture_response={"events": [event_result(candidate, confidence="confirmed")]},
            candidates=[candidate],
            radar_mode="movement_explanation",
            now=self.now,
        )
        self.assertEqual(
            result["events"][0]["interpretation"]["confidence"], "likely"
        )
        self.assertIn(
            "未確認の介入観測",
            result["events"][0]["interpretation"]["potentially_false_claims"],
        )

    def test_real_client_uses_bounded_supported_parameters(self):
        candidate = self.candidate()
        client = FakeClient({"events": [event_result(candidate)]})
        result = run(candidates=[candidate], client=client, now=self.now)
        self.assertEqual(result["status"], "success")
        kwargs = client.responses.kwargs
        self.assertEqual(kwargs["reasoning"], {"effort": "low"})
        self.assertFalse(kwargs["parallel_tool_calls"])
        self.assertIn("prompt_cache_key", kwargs)
        self.assertIn("json_schema", kwargs["text"]["format"]["type"])
        self.assertNotIn("max_turns", kwargs)
        self.assertEqual(kwargs["max_tool_calls"], 1)
        self.assertRegex(kwargs["tools"][0]["from_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(kwargs["tools"][0]["to_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertFalse(kwargs["store"])

    def test_event_cache_prevents_duplicate_api_call_for_60_minutes(self):
        candidate = self.candidate(source_id="cached-event")
        first = run(
            fixture_response={"events": [event_result(candidate)]},
            candidates=[candidate],
            now=self.now,
        )
        self.assertEqual(first["status"], "success")
        client = FakeClient({}, error=AssertionError("API must not be called"))
        second = run(
            candidates=[candidate],
            client=client,
            now=self.now + timedelta(minutes=30),
        )
        self.assertEqual(second["status"], "cached")
        self.assertFalse(second["api_called"])
        self.assertGreater(social_cache_status()["hits"], 0)

    def test_adaptive_cost_policy_shrinks_and_recovers_after_pause(self):
        folder = Path(self.temp.name) / "xai"
        folder.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "timestamp": (self.now - timedelta(minutes=minutes)).isoformat(),
                "status": "success",
                "cost_usd": 0.7,
            }
            for minutes in (3, 2, 1)
        ]
        (folder / "runs.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        policy = adaptive_cost_policy()
        self.assertEqual(policy["level"], 3)
        self.assertEqual(policy["max_events"], 1)
        self.assertTrue(policy["temporary_pause"])
        old_rows = [{**row, "timestamp": (self.now - timedelta(hours=3)).isoformat()} for row in rows]
        (folder / "runs.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in old_rows), encoding="utf-8"
        )
        self.assertFalse(adaptive_cost_policy()["temporary_pause"])

    def test_exploration_requires_all_news_confirmation_gates(self):
        candidate = self.candidate(
            source_type="exploration",
            source_id="exploration-qualified",
            title="Early semiconductor movement",
            market_movement={"symbol": "SMH", "percentage_change": 2.1},
        )
        posts = [
            {
                "post_id": "official-1", "url": "https://x.com/official/1",
                "account": "official", "account_type": "official_company",
                "excerpt": "Official update with source", "thread_id": "",
                "is_reply": False, "is_quote": False, "is_repost": False,
                "source_url": "https://company.example/release",
            },
            {
                "post_id": "expert-1", "url": "https://x.com/expert/1",
                "account": "expert", "account_type": "analyst",
                "excerpt": "Independent market analysis", "thread_id": "",
                "is_reply": False, "is_quote": False, "is_repost": False,
                "source_url": "https://research.example/note",
            },
        ]
        result = run(
            fixture_response={"events": [event_result(candidate, posts=posts)]},
            candidates=[candidate],
            radar_mode="exploration",
            now=self.now,
        )
        self.assertEqual(result["usage"]["news_candidates_created"], 1)
        candidate_rows = read_jsonl("news_candidates.jsonl")
        self.assertTrue(candidate_rows[-1]["requires_rss_or_official_confirmation"])
        self.assertFalse(candidate_rows[-1]["posting_allowed"])

    def test_fx_trigger_context_and_consumption(self):
        movement = {
            "movement_id": "fx-trigger-1", "pair": "USDJPY", "window": "15m",
            "change_pct": 1.0, "detected_at": self.now.isoformat(),
        }
        queued = enqueue_fx_movement(movement)
        self.assertIsNotNone(pending_event_trigger(self.now))
        self.assertEqual(consume_event_trigger(self.now)["event_id"], queued["candidate_id"])
        self.assertIsNone(pending_event_trigger(self.now))
        candidate = make_candidate(
            source_type="fx_movement", source_id="fx-trigger-1",
            title="USDJPY 15m +1.0% movement", market_movement=movement,
            now=self.now,
        )
        run(
            fixture_response={"events": [event_result(candidate, confidence="confirmed")]},
            candidates=[candidate],
            radar_mode="movement_explanation",
            now=self.now,
        )
        context_path = Path(self.temp.name) / "fx" / "xai_context.jsonl"
        context = json.loads(context_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(context["confidence"], "likely")
        self.assertTrue(context["xai_only_not_confirmed"])

    def test_post_and_content_usage_are_measurable(self):
        usage = record_content_opportunity_use(
            run_id="run-usage", event_id="event-usage",
            use_type="news_candidate_match", reference_id="https://example.test/item",
        )
        duplicate = record_content_opportunity_use(
            run_id="run-usage", event_id="event-usage",
            use_type="news_candidate_match", reference_id="https://example.test/item",
        )
        self.assertEqual(usage["usage_id"], duplicate["usage_id"])
        self.assertEqual(len(read_jsonl("content_opportunity_usage.jsonl")), 1)
        outcome = record_post_outcome(
            run_id="run-usage", event_id="event-usage",
            tweet_id="123", title="test",
        )
        self.assertTrue(outcome["posted"])
        self.assertEqual(read_jsonl("outcomes.jsonl")[-1]["tweet_id"], "123")

    def test_usage_is_written_to_new_and_hard_budget_ledgers(self):
        candidate = self.candidate()
        client = FakeClient({"events": [event_result(candidate)]})
        result = run(candidates=[candidate], client=client, now=self.now)
        run_id = result["run_id"]
        self.assertEqual(read_jsonl("runs.jsonl")[-1]["run_id"], run_id)
        self.assertEqual(read_jsonl("api_usage.jsonl")[-1]["operation"], "x_social_intelligence")
        self.assertEqual(cost_attribution(run_id), 0.0125)

    def test_malformed_structured_output_and_timeout_fail_safely(self):
        candidate = self.candidate()
        malformed = run(
            fixture_response={"events": [{"event_id": candidate.candidate_id}]},
            candidates=[candidate],
            now=self.now,
        )
        self.assertEqual(malformed["status"], "failed")
        self.assertTrue(malformed["safe_failure"])
        timed_out = run(
            candidates=[self.candidate(source_id="timeout")],
            client=FakeClient({}, error=TimeoutError("timeout")),
            now=self.now,
        )
        self.assertEqual(timed_out["status"], "failed")

    def test_exploration_budget_cap(self):
        folder = Path(self.temp.name) / "xai"
        folder.mkdir(parents=True, exist_ok=True)
        rows = [
            {"timestamp": self.now.isoformat(), "status": "success",
             "radar_mode": "event_reaction", "cost_usd": 0.8},
            {"timestamp": self.now.isoformat(), "status": "success",
             "radar_mode": "exploration", "cost_usd": 0.2},
        ]
        (folder / "runs.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        status = exploration_status()
        self.assertEqual(status["budget_share"], 0.2)
        candidate = self.candidate(source_id="explore")
        result = run(
            fixture_response={"events": [event_result(candidate)]},
            candidates=[candidate],
            radar_mode="exploration",
            now=self.now,
        )
        self.assertEqual(result["reason"], "exploration_budget_limit")

    def test_news_event_match_and_shadow_do_not_apply_bonus(self):
        candidate = self.candidate()
        run(
            fixture_response={"events": [event_result(candidate)]},
            candidates=[candidate],
            now=self.now,
        )
        match = match_news_event(
            title="Google GOOGL earnings announcement", tickers=["GOOGL"]
        )
        self.assertIsNotNone(match)
        row = shadow_record_news(
            title="Google earnings", matched=match,
            original_rank=2, hypothetical_rank=1,
        )
        self.assertTrue(row["rank_changed"])
        self.assertFalse(shadow_report(14)["score_bonus_enabled"])

    def test_discord_allowlist_redaction_and_run_dedup(self):
        safe = sanitize_payload("xai_research", {
            "run_id": "r1",
            "event_count": 1,
            "authorization": "Bearer should-not-pass",
            "failure_reason": "token=secret-secret-secret",
        })
        self.assertNotIn("authorization", safe)
        self.assertIn("<redacted>", safe["failure_reason"])
        session = FakeDiscordSession()
        run_row = {
            "run_id": "r1", "status": "success", "radar_mode": "event_reaction",
            "events_researched": 1, "cost_usd": 0.1, "cache_hit": False,
        }
        with patch.dict(os.environ, {
            "DISCORD_ALERTS_ENABLED": "true",
            "DISCORD_XAI_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/test",
        }):
            first = notify_xai_research_result(run_row, [], [], session=session)
            second = notify_xai_research_result(run_row, [], [], session=session)
        self.assertTrue(first["sent"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(session.calls), 1)

    def test_corrupt_jsonl_quarantine_and_cleanup(self):
        folder = Path(self.temp.name) / "xai"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "posts.jsonl").write_text(
            "{broken\n"
            + json.dumps({
                "post_id": "old",
                "observed_at": (self.now - timedelta(days=31)).isoformat(),
            })
            + "\n",
            encoding="utf-8",
        )
        rows = read_jsonl("posts.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertTrue(list(folder.glob("quarantine_*.jsonl")))
        self.assertEqual(cleanup()["removed_rows"], 1)

    def test_safe_disable_and_budget_values_are_unchanged(self):
        candidate = self.candidate()
        with patch.dict(os.environ, {"XAI_SAFE_DISABLED": "true"}):
            result = run(
                fixture_response={"events": [event_result(candidate)]},
                candidates=[candidate],
                now=self.now,
            )
        self.assertEqual(result["reason"], "xai_safe_disabled")
        self.assertEqual(os.getenv("XAI_MONTHLY_BUDGET_USD"), "20")

    def test_report_contains_scope_cost_and_funnel_fields(self):
        candidate = self.candidate()
        run(
            fixture_response={"events": [event_result(candidate)]},
            candidates=[candidate],
            now=self.now,
        )
        report = social_report(30)
        self.assertEqual(report["measurement_scope"], "observed_search_results_not_all_of_x")
        self.assertIn("cost_per_event_usd", report)
        self.assertIn("xai_post_median_impressions", report)
        self.assertIn("remaining_budget_usd", report)
        self.assertEqual(report["integrated_analyses"], 1)

    def test_integrates_related_research_without_another_api_call(self):
        first = self.candidate(source_id="integration-1")
        second = self.candidate(
            source_id="integration-2",
            title="GOOGL Alphabet quarterly results reaction",
        )
        for candidate in (first, second):
            append_jsonl("events.jsonl", {
                **candidate.to_dict(), "status": "researched",
            })
            append_jsonl("observations.jsonl", {
                "observation_id": f"obs-{candidate.source_id}",
                "run_id": f"run-{candidate.source_id}",
                "event_id": candidate.candidate_id,
                "observed_at": self.now.isoformat(),
                "metrics": {
                    "independent_commentary_count": 2,
                    "official_account_participation": True,
                },
                "posts": [
                    {"account": f"account-{candidate.source_id}-1"},
                    {"account": f"account-{candidate.source_id}-2"},
                ],
                "interpretation": {
                    "dominant_narrative": "広告成長とAI投資負担の綱引き",
                    "alternative_narratives": [],
                    "useful_expert_points": [],
                    "market_implication_candidates": ["利益率の変化を確認する"],
                    "strongest_dissent": "投資負担は織り込み済み",
                    "common_misconception": "",
                    "facts_needing_confirmation": [],
                    "unanswered_questions": ["設備投資の回収時期"],
                    "potentially_false_claims": [],
                },
            })
        result = integrate_research_results(
            event_ids=[first.candidate_id, second.candidate_id],
            persist=False,
            now=self.now,
        )
        self.assertEqual(result["analysis_count"], 1)
        self.assertEqual(result["additional_api_calls"], 0)
        analysis = result["analyses"][0]
        self.assertEqual(len(analysis["corroborated_findings"]), 1)
        self.assertEqual(analysis["evidence"]["quality"], "high")
        self.assertEqual(analysis["posting_readiness"], "ready_for_draft")
        self.assertTrue(analysis["automatic_posting_allowed"])
        self.assertFalse(analysis["human_review_required"])

    def test_integrated_analysis_persistence_is_idempotent(self):
        candidate = self.candidate(source_id="integration-idempotent")
        run(
            fixture_response={"events": [event_result(candidate)]},
            candidates=[candidate],
            now=self.now,
        )
        first = integrate_research_results(
            event_ids=[candidate.candidate_id], persist=True, now=self.now,
        )
        second = integrate_research_results(
            event_ids=[candidate.candidate_id], persist=True, now=self.now,
        )
        self.assertEqual(first["created_count"], 0)
        self.assertEqual(second["created_count"], 0)
        self.assertEqual(len(read_jsonl("integrated_analyses.jsonl")), 1)

    def test_integration_versions_drafts_matches_and_usage(self):
        candidate = self.candidate(source_id="integration-version")
        payload = {"events": [event_result(candidate)]}
        first = run(
            fixture_response=payload, candidates=[candidate], now=self.now,
        )
        second = run(
            fixture_response=payload, candidates=[candidate],
            now=self.now + timedelta(minutes=61),
        )
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        analyses = read_jsonl("integrated_analyses.jsonl")
        self.assertEqual(len(analyses), 2)
        self.assertEqual(analyses[-1]["version"], 2)
        self.assertEqual(
            analyses[-1]["supersedes_analysis_id"], analyses[-2]["analysis_id"]
        )
        self.assertFalse(analyses[-1]["material_change"])
        drafts = read_jsonl("integrated_drafts.jsonl")
        self.assertEqual(drafts[-1]["status"], "blocked_pending_confirmation")
        self.assertFalse(drafts[-1]["automatic_posting_allowed"])
        matched = match_integrated_analysis(
            title=candidate.title, tickers=["GOOGL"],
            event_id=candidate.candidate_id,
        )
        self.assertEqual(matched["analysis_id"], analyses[-1]["analysis_id"])
        for _ in range(2):
            record_integrated_analysis_use(
                analysis_id=matched["analysis_id"],
                use_type="news_candidate_context",
                reference_id="news-1",
            )
        self.assertEqual(len(read_jsonl("integrated_analysis_usage.jsonl")), 1)

    def test_legacy_research_import_is_idempotent_and_unconfirmed(self):
        append_jsonl("topic_radar.jsonl", {
            "timestamp": self.now.isoformat(),
            "detected_at": self.now.isoformat(),
            "topic": "MSFT Azure growth discussion",
            "tickers": ["MSFT"],
            "summary": "X上でAzure成長への反応を観測",
            "observed_mention_count": 5,
            "velocity_score": 7,
            "acceleration_score": 6,
            "representative_accounts": ["one", "two"],
            "representative_posts": [{
                "post_id": "legacy-1",
                "url": "https://x.com/example/status/1",
                "username": "one",
                "excerpt": "Azure growth discussion",
            }],
            "source_reliability": "medium",
            "source_confirmation": "公式決算で再確認が必要",
            "radar_run_id": "legacy-run",
        })
        first = import_legacy_research()
        second = import_legacy_research()
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        imported = read_jsonl("observations.jsonl")[-1]
        self.assertEqual(imported["radar_mode"], "legacy_import")
        self.assertEqual(imported["interpretation"]["confidence"], "possible")
        self.assertTrue(
            imported["interpretation"]["facts_needing_confirmation"]
        )

    def test_schema_is_strict_and_utf8_safe(self):
        schema = _schema()["format"]
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        candidate = self.candidate(title="日銀 金融政策決定会合")
        self.assertIn("日銀", candidate.title)


if __name__ == "__main__":
    unittest.main()
