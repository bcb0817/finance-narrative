import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from market_data import evidence_flow as flow


class FakeStructuredService:
    def __init__(self, result):
        self.result = result
        self.prompt = ""

    def structured(self, prompt, schema, **kwargs):
        self.prompt = prompt
        return self.result


class MarketTriggerEvidenceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "INDEPENDENT_CONFIRMATION_ENABLED": "true",
            "MARKET_TRIGGER_RECHECK_ENABLED": "true",
            "MARKET_TRIGGER_RECHECK_MINUTES": "15,30,60",
            "MARKET_TRIGGER_MAX_AGE_MINUTES": "120",
        }, clear=False)
        self.env.start()
        self.trigger = flow.create_trigger_evidence(
            provider="twelvedata",
            symbol="NVDA",
            asset_type="equity",
            detected_at=self.now,
            movement_window="15m",
            internal_movement_class="large_move",
            data_quality="fresh",
            movement_id="move-1",
        )

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def item(self, **overrides):
        values = {
            "title": "Nvidia reports earnings and raises guidance",
            "url": "https://example.com/nvidia-earnings",
            "source": "Major Financial News",
            "published": self.now.isoformat(),
            "source_group": "market_news",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def event(self, **overrides):
        return flow.build_event_evidence(self.item(**overrides), self.trigger)

    def test_internal_trigger_has_no_provider_market_values(self):
        forbidden = {
            "price", "current_price", "change_pct", "percentage_change",
            "volume", "high", "low", "chart", "chart_path",
        }
        self.assertFalse(forbidden.intersection(self.trigger))
        self.assertEqual(self.trigger["provider"], "twelvedata")

    def test_bundle_has_no_price_change_or_chart_fields(self):
        event = self.event()
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        bundle = flow.build_public_evidence_bundle(self.trigger, event, causal)
        text = str(bundle).lower()
        self.assertNotIn("current_price", text)
        self.assertNotIn("change_pct", text)
        self.assertNotIn("chart_path", text)
        self.assertTrue(bundle["validation"]["allowed"])

    def test_one_official_event_is_confirmed(self):
        event = self.event(
            title="SEC publishes NVDA 8-K acquisition filing",
            url="https://www.sec.gov/Archives/edgar/data/1/test.htm",
            source="SEC",
            source_group="official_regulatory",
        )
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        self.assertEqual(causal["causal_confidence"], "confirmed")

    def test_two_independent_major_reports_are_confirmed(self):
        first = self.event(
            title="Nvidia earnings exceed estimates",
            url="https://one.example/nvidia",
            source="Outlet One",
        )
        second = self.event(
            title="NVDA raises guidance after quarterly results",
            url="https://two.example/nvda",
            source="Outlet Two",
        )
        causal = flow.evaluate_causal_evidence(
            self.trigger, second, related_events=[first],
        )
        self.assertEqual(causal["cross_source_confirmation"], 2)
        self.assertEqual(causal["causal_confidence"], "confirmed")

    def test_same_wire_republication_counts_once(self):
        first = self.event(
            title="Reuters: Nvidia reports earnings",
            url="https://one.example/reuters-nvidia",
            source="Reuters",
        )
        second = self.event(
            title="Reuters: Nvidia reports earnings",
            url="https://two.example/reuters-nvidia",
            source="Partner republishes Reuters",
        )
        result = flow.source_independence([first, second])
        self.assertEqual(result["independent_source_count"], 1)
        self.assertEqual(result["duplicate_republication_count"], 1)

    def test_one_major_report_is_likely(self):
        causal = flow.evaluate_causal_evidence(self.trigger, self.event())
        self.assertEqual(causal["causal_confidence"], "likely")

    def test_specialist_media_only_is_possible(self):
        event = self.event(source_group="sector_news")
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        self.assertEqual(causal["causal_confidence"], "possible")

    def test_x_only_source_cannot_be_confirmed(self):
        event = self.event(source_group="social")
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        self.assertNotEqual(causal["causal_confidence"], "confirmed")

    def test_ticker_match_alone_does_not_confirm_cause(self):
        event = self.event(title="NVDA market update")
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        self.assertFalse(causal["event_type_match"])
        self.assertNotEqual(causal["causal_confidence"], "confirmed")

    def test_event_type_match_is_recorded(self):
        event = self.event()
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        self.assertTrue(causal["event_type_match"])

    def test_close_timestamp_is_high_confidence(self):
        timing = flow.timestamp_proximity(self.trigger, self.event())
        self.assertEqual(timing["timing_confidence"], "high")

    def test_old_event_is_stale_and_unknown(self):
        event = self.event(published=(self.now - timedelta(days=2)).isoformat())
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        self.assertTrue(causal["stale_event"])
        self.assertEqual(causal["causal_confidence"], "unknown")

    def test_event_before_movement(self):
        event = self.event(published=(self.now - timedelta(minutes=10)).isoformat())
        self.assertTrue(flow.timestamp_proximity(self.trigger, event)["event_before_movement"])

    def test_event_after_movement(self):
        event = self.event(published=(self.now + timedelta(minutes=10)).isoformat())
        self.assertTrue(flow.timestamp_proximity(self.trigger, event)["event_after_movement"])

    def test_ai_output_cannot_upgrade_bundle_confidence(self):
        event = self.event(source_group="sector_news")
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        bundle = flow.build_public_evidence_bundle(self.trigger, event, causal)
        self.assertEqual(bundle["causal_confidence"], "possible")
        self.assertFalse(bundle["causal_claim_allowed"])

    def test_unconfirmed_intervention_is_not_causal(self):
        fx_trigger = dict(self.trigger, symbol="USD/JPY", asset_type="forex")
        event = flow.build_event_evidence(self.item(
            title="Media reports possible yen currency intervention",
            url="https://example.com/yen",
        ), fx_trigger)
        causal = flow.evaluate_causal_evidence(fx_trigger, event)
        self.assertFalse(causal["causal_claim_allowed"])

    def test_official_intervention_can_be_confirmed(self):
        fx_trigger = dict(self.trigger, symbol="USD/JPY", asset_type="forex")
        event = flow.build_event_evidence(self.item(
            title="Japan confirms yen currency intervention",
            url="https://www.mof.go.jp/english/policy/international_policy/test.html",
            source="Japan MOF",
            source_group="official_fx",
        ), fx_trigger)
        causal = flow.evaluate_causal_evidence(fx_trigger, event)
        self.assertEqual(causal["causal_confidence"], "confirmed")
        self.assertTrue(causal["causal_claim_allowed"])

    def test_h10_is_reference_only_and_unknown(self):
        fx_trigger = dict(self.trigger, symbol="USD/JPY", asset_type="forex")
        event = flow.build_event_evidence(self.item(
            title="Federal Reserve H.10 Japanese yen exchange rate",
            url="https://www.federalreserve.gov/releases/h10/current/",
            source="Fed H10 Japanese Yen",
            source_group="official_fx",
        ), fx_trigger)
        causal = flow.evaluate_causal_evidence(fx_trigger, event)
        self.assertEqual(event["source_purpose"], "reference_only")
        self.assertEqual(causal["causal_confidence"], "unknown")

    def test_ecb_reference_rate_is_not_realtime_cause(self):
        fx_trigger = dict(self.trigger, symbol="USD/JPY", asset_type="forex")
        event = flow.build_event_evidence(self.item(
            title="ECB JPY reference exchange rate",
            url="https://www.ecb.europa.eu/rss/fxref-jpy.html",
            source="ECB Reference Rate",
            source_group="official_fx",
        ), fx_trigger)
        causal = flow.evaluate_causal_evidence(fx_trigger, event)
        self.assertEqual(event["source_purpose"], "reference_only")
        self.assertFalse(causal["causal_claim_allowed"])

    def test_sec_8k_company_event(self):
        event = self.event(
            title="NVDA files 8-K with acquisition results",
            url="https://www.sec.gov/Archives/edgar/data/1/test.htm",
            source="SEC", source_group="company_filings",
        )
        self.assertEqual(event["event_type"], "earnings")
        self.assertTrue(event["official"])

    def test_tdnet_company_event_fixture(self):
        jp_trigger = dict(self.trigger, symbol="7203", asset_type="jp_equity")
        event = flow.build_event_evidence(self.item(
            title="7203 earnings results and revenue guidance",
            url="https://www.release.tdnet.info/inbs/test.html",
            source="TDnet", source_group="company_filings",
        ), jp_trigger)
        causal = flow.evaluate_causal_evidence(jp_trigger, event)
        self.assertTrue(event["official"])
        self.assertEqual(causal["causal_confidence"], "confirmed")

    def test_recheck_schedule_is_15_30_60(self):
        pending = flow.pending_confirmations()[0]
        self.assertEqual(pending["recheck_minutes"], [15, 30, 60])
        first = flow._update_pending(dict(pending), now=self.now)
        second = flow._update_pending(dict(first), now=self.now + timedelta(minutes=15))
        third = flow._update_pending(dict(second), now=self.now + timedelta(minutes=30))
        self.assertTrue(first["next_check_at"].startswith(
            (self.now + timedelta(minutes=15)).isoformat()
        ))
        self.assertTrue(second["next_check_at"].startswith(
            (self.now + timedelta(minutes=30)).isoformat()
        ))
        self.assertTrue(third["next_check_at"].startswith(
            (self.now + timedelta(minutes=60)).isoformat()
        ))

    def test_pending_expires_safely(self):
        result = flow.process_recheck(
            "move-1", dry_run=False, candidate_items=[],
            now=self.now + timedelta(minutes=121),
        )
        self.assertEqual(result["status"], "expired")
        self.assertFalse(result["x_post_attempted"])

    def test_later_source_found(self):
        result = flow.process_recheck(
            "move-1", dry_run=False, candidate_items=[self.item()],
            now=self.now + timedelta(minutes=15),
        )
        self.assertEqual(result["status"], "likely")
        self.assertEqual(result["evaluated_count"], 1)
        self.assertFalse(result["x_post_attempted"])

    def test_publication_modes(self):
        event = {"official": True}
        confirmed = {
            "causal_confidence": "confirmed", "causal_claim_allowed": True,
        }
        likely = {
            "causal_confidence": "likely", "causal_claim_allowed": True,
        }
        possible = {
            "causal_confidence": "possible", "causal_claim_allowed": False,
        }
        unknown = {
            "causal_confidence": "unknown", "causal_claim_allowed": False,
        }
        self.assertEqual(flow.choose_publication_mode(event, confirmed), "verified_event")
        self.assertEqual(
            flow.choose_publication_mode(
                event, confirmed, public_market_data_rights=True,
            ),
            "verified_market_reaction",
        )
        self.assertEqual(
            flow.choose_publication_mode({"official": False}, likely),
            "causal_explainer",
        )
        self.assertEqual(
            flow.choose_publication_mode(event, possible),
            "background_explainer",
        )
        self.assertEqual(
            flow.choose_publication_mode(event, unknown), "unknown_cause",
        )

    def test_claim_without_evidence_is_stopped(self):
        bundle = {
            "content_mode": "causal_explainer",
            "claims": [{
                "claim_id": "c1", "factual": True, "causal": False,
                "evidence_ids": [],
            }],
        }
        result = flow.validate_public_bundle(bundle)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "claim_without_evidence")

    def test_structured_output_blocks_price_and_percentage(self):
        event = self.event(
            title="SEC publishes NVDA earnings results",
            url="https://www.sec.gov/test",
            source="SEC", source_group="official_regulatory",
        )
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        bundle = flow.build_public_evidence_bundle(self.trigger, event, causal)
        evidence_id = bundle["evidence_ids"][0]
        result = {
            "post_value": 8,
            "recommended_mode": bundle["content_mode"],
            "draft_text": "NVDA surged 9% after the filing.",
            "claims": [{
                "claim_text": "NVDA surged 9%",
                "evidence_ids": [evidence_id],
                "factual": True,
                "causal": True,
            }],
            "evidence_mapping": [],
            "causal_language": "confirmed",
            "uncertainty_statement": "",
            "attribution": "SEC",
            "safety_flags": [],
            "rejection_reason": "",
        }
        validation = flow.validate_structured_output(result, bundle)
        self.assertFalse(validation["allowed"])
        self.assertTrue(validation["unlicensed_market_movement_claim"])

    def test_structured_prompt_contains_no_provider_values(self):
        event = self.event(
            title="SEC publishes NVDA earnings results",
            url="https://www.sec.gov/test",
            source="SEC", source_group="official_regulatory",
        )
        causal = flow.evaluate_causal_evidence(self.trigger, event)
        bundle = flow.build_public_evidence_bundle(self.trigger, event, causal)
        evidence_id = bundle["evidence_ids"][0]
        output = {
            "post_value": 8,
            "recommended_mode": bundle["content_mode"],
            "draft_text": "SEC published Nvidia earnings results.",
            "claims": [{
                "claim_text": "SEC published Nvidia earnings results.",
                "evidence_ids": [evidence_id],
                "factual": True,
                "causal": False,
            }],
            "evidence_mapping": [],
            "causal_language": "",
            "uncertainty_statement": "",
            "attribution": "SEC",
            "safety_flags": [],
            "rejection_reason": "",
        }
        service = FakeStructuredService(output)
        generated = flow.generate_structured_publication(bundle, service=service)
        self.assertEqual(generated["status"], "ready")
        self.assertNotIn("twelvedata", service.prompt.lower())
        self.assertNotIn("current_price", service.prompt.lower())

    def test_asset_routes(self):
        self.assertIn("boj.or.jp", flow.source_route("forex")["priority"])
        self.assertIn("sec.gov", flow.source_route("equity")["priority"])
        self.assertIn("tdnet", flow.source_route("jp_equity")["priority"])
        self.assertIn("eia.gov", flow.source_route("energy")["priority"])
        self.assertIn("specialist_media", flow.source_route("crypto")["supplemental"])

    def test_available_candidates_are_ranked_by_asset_route(self):
        sec = self.item(
            url="https://www.sec.gov/test",
            source="SEC",
            source_group="official_regulatory",
        )
        specialist = self.item(
            url="https://specialist.example/test",
            source="Specialist",
            source_group="sector_news",
        )
        self.assertGreater(
            flow.source_route_rank(sec, self.trigger)["score"],
            flow.source_route_rank(specialist, self.trigger)["score"],
        )

    def test_metrics_and_later_outcome(self):
        flow.record_publication_result(
            "move-1", posted=False, mode="causal_explainer",
            reason="test_stop", post_value=4,
        )
        row = flow.record_later_outcome(
            "move-1",
            later_confirmed_cause="earnings",
            later_sources=["SEC"],
            decision_correct=True,
            missed_opportunity=False,
            false_causal_candidate=False,
        )
        self.assertEqual(row["later_confirmed_cause"], "earnings")
        self.assertEqual(flow.later_review_report(days=1)["count"], 1)

    def test_utf8_japanese_round_trip(self):
        event = self.event(
            title="エヌビディアが決算を発表",
            url="https://example.com/japanese",
        )
        flow.evaluate_causal_evidence(self.trigger, event)
        stored = flow.evidence_for("move-1")
        self.assertIn(
            "エヌビディア",
            next(
                row["title"] for row in stored["event_evidence"]
                if row["evidence_id"] == event["evidence_id"]
            ),
        )


if __name__ == "__main__":
    unittest.main()
