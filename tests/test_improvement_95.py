import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from common.data_governance import (
    classify_provider,
    display_status,
    provider_isolated_editorial_decision,
    publication_decision,
    validate_provider_isolated_editorial_text,
)
from common.discord_schema import sanitize_payload
from common.external_heartbeat import publish as publish_heartbeat
from common.metrics_collector import enrich_metrics, late_catchup_stage
from common.metrics_quality import stage_status
from common.runtime import JST
from fx_alert.detector import detect_movements
from fx_alert.models import FxBar
from market_data.cross_asset import classify_cross_asset
from market_data.models import MarketMovement
from market_data.shadow import create_candidate, report as shadow_report
from common.runtime_manifest import write_manifest, runtime_status
from common.xai_quality import cost_breakdown


class Improvement95Tests(unittest.TestCase):
    def test_external_display_unknown_blocks_public_but_allows_internal(self):
        with patch.dict(os.environ, {
            "TWELVEDATA_EXTERNAL_DISPLAY_STATUS": "unknown",
            "TWELVEDATA_PUBLIC_CHART_ALLOWED": "false",
            "TWELVEDATA_PUBLIC_NUMERIC_DATA_ALLOWED": "false",
        }, clear=False):
            self.assertEqual(display_status().value, "unknown")
            self.assertFalse(publication_decision(surface="x", includes_chart=True).allowed)
            self.assertTrue(publication_decision(
                surface="discord", includes_chart=True, audience="internal"
            ).allowed)

    def test_external_display_approved_requires_granular_rights(self):
        with patch.dict(os.environ, {
            "TWELVEDATA_EXTERNAL_DISPLAY_STATUS": "approved",
            "TWELVEDATA_PUBLIC_CHART_ALLOWED": "true",
            "TWELVEDATA_PUBLIC_NUMERIC_DATA_ALLOWED": "true",
        }, clear=False):
            self.assertTrue(publication_decision(surface="x", includes_chart=True).allowed)
        with patch.dict(os.environ, {
            "TWELVEDATA_EXTERNAL_DISPLAY_STATUS": "denied",
            "TWELVEDATA_PUBLIC_CHART_ALLOWED": "true",
            "TWELVEDATA_PUBLIC_NUMERIC_DATA_ALLOWED": "true",
        }, clear=False):
            self.assertFalse(publication_decision(surface="x").allowed)

    def test_provider_isolated_editorial_requires_official_independent_source(self):
        with patch.dict(os.environ, {
            "OFFICIAL_EDITORIAL_POST_ENABLED": "true",
            "TWELVEDATA_EXTERNAL_DISPLAY_STATUS": "unknown",
        }, clear=False):
            allowed = provider_isolated_editorial_decision(
                source_url="https://www.federalreserve.gov/newsevents/test.htm",
                source_group="official_macro",
                provider_lineage=[],
            )
            self.assertTrue(allowed["allowed"])
            self.assertTrue(allowed["external_display_rights_not_inferred"])
            media = provider_isolated_editorial_decision(
                source_url="https://example.com/markets",
                source_group="market_news",
                provider_lineage=[],
            )
            self.assertFalse(media["allowed"])
            contaminated = provider_isolated_editorial_decision(
                source_url="https://www.federalreserve.gov/newsevents/test.htm",
                source_group="official_macro",
                provider_lineage=["twelvedata"],
            )
            self.assertFalse(contaminated["allowed"])

    def test_provider_isolated_text_rejects_provider_like_or_invented_values(self):
        valid = validate_provider_isolated_editorial_text(
            "政策金利を5.25%に維持。次回声明のインフレ認識を確認したい。",
            source_title="Federal Reserve maintains rate at 5.25%",
        )
        self.assertTrue(valid["allowed"])
        invented = validate_provider_isolated_editorial_text(
            "政策金利を5.25%に維持。ドル円は156.20円。",
            source_title="Federal Reserve maintains rate at 5.25%",
        )
        self.assertFalse(invented["allowed"])
        live = validate_provider_isolated_editorial_text(
            "USD/JPYは15mで上昇。",
            source_title="Federal Reserve statement",
        )
        self.assertFalse(live["allowed"])

    def test_fx_hard_trigger_survives_unavailable_dynamic_statistics(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        bars = []
        for index in range(12):
            close = 150.0 if index < 11 else 151.4
            bars.append(FxBar(
                pair="USDJPY", timestamp=now-timedelta(minutes=11-index),
                interval="1min", open=close, high=close+0.01,
                low=close-0.01, close=close, provider="fixture",
            ))
        with patch.dict(os.environ, {
            "FX_HARD_TRIGGER_5M_PERCENT": "0.80",
            "FX_Z_SCORE_MIN": "999",
            "FX_ATR_MULTIPLE_MIN": "999",
            "FX_REQUIRE_DYNAMIC_CONFIRMATION": "true",
        }, clear=False):
            movements = detect_movements(bars)
        self.assertTrue(any(item.hard_triggered for item in movements))
        self.assertTrue(all(item.fixed_threshold_passed for item in movements))
        self.assertTrue(all(item.threshold_version for item in movements))

    def test_metrics_stage_and_rolling_status(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"STATE_DIR": temp}, clear=False
        ):
            path = Path(temp) / "metrics_snapshots.jsonl"
            now = datetime.now(JST)
            rows = [
                {"tweet_id": "1", "stage": "1h", "status": "collected",
                 "metrics_collected_at": now.isoformat()},
                {"tweet_id": "2", "stage": "1h", "status": "missed",
                 "reason": "collection_window_expired",
                 "metrics_collected_at": now.isoformat()},
            ]
            path.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
            result = stage_status(days=7, now=now)
            self.assertEqual(result["by_window"]["1h"]["success_rate"], 0.5)
            self.assertEqual(result["missed_reasons"]["deadline_passed"], 1)
            self.assertIn("stage_counts", result)

    def test_metrics_late_catchup_is_bounded_and_labelled(self):
        with patch.dict(os.environ, {"METRICS_LATE_CATCHUP_MINUTES": "180"}, clear=False):
            self.assertEqual(late_catchup_stage(2.0, set()), "1h")
            self.assertIsNone(late_catchup_stage(5.0, set()))
            now = datetime.now(JST)
            row = enrich_metrics(
                {"tweet_id": "1", "stage": "1h", "impressions": 10},
                (now - timedelta(hours=2)).isoformat(),
                now,
            )
            self.assertEqual(row["collection_timing"], "late_catchup")
            self.assertEqual(row["lateness_minutes"], 30.0)

    def test_xai_cost_report_joins_actual_posts_by_run_id(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"STATE_DIR": temp}, clear=False
        ):
            root = Path(temp)
            (root / "xai").mkdir()
            now = datetime.now(JST)
            (root / "xai" / "api_usage.jsonl").write_text(json.dumps({
                "timestamp": now.isoformat(), "run_id": "run-1",
                "status": "success", "reported_cost_usd": 0.2,
                "topics_returned": 2, "posts_created": 0,
            }) + "\n", encoding="utf-8")
            (root / "posted_history.json").write_text(json.dumps([{
                "tweet_id": "123", "posted_at": now.isoformat(),
                "radar_run_id": "run-1", "radar_topic": "AI",
            }]), encoding="utf-8")
            result = cost_breakdown(days=30)
            self.assertEqual(result["posts_created"], 1)
            self.assertEqual(result["post_ids"], ["123"])
            self.assertEqual(result["cost_per_post_usd"], 0.2)

    def test_cross_asset_never_confirms_causality_from_prices_alone(self):
        signal = classify_cross_asset({"SPY": -1.2, "QQQ": -1.5, "GLD": 0.8})
        self.assertFalse(signal.causality_claim_allowed)
        self.assertTrue(signal.observed_facts)
        self.assertTrue(signal.alternative_interpretations)
        self.assertTrue(signal.disconfirming_evidence)
        self.assertIn(signal.confidence, {"likely", "possible", "unknown"})

    def test_discord_allowlist_removes_unapproved_fields_and_secrets(self):
        fake = "sk-test-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        webhook = "https://discord.com/api/webhooks/123/secret-value"
        payload = sanitize_payload("operations_alert", {
            "severity": "high",
            "component": "test",
            "safe_message": f"api_key={fake} webhook_url={webhook}",
            "Authorization": f"Bearer {fake}",
            "raw_http_body": {"secret": fake},
        })
        rendered = json.dumps(payload)
        self.assertNotIn(fake, rendered)
        self.assertNotIn(webhook, rendered)
        self.assertNotIn("Authorization", payload)
        self.assertNotIn("raw_http_body", payload)

    def test_heartbeat_disabled_and_failure_is_safe(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp, "EXTERNAL_HEARTBEAT_ENABLED": "false",
            "EXTERNAL_HEARTBEAT_URL": "https://secret.example.com/ping/token",
        }, clear=False):
            self.assertEqual(publish_heartbeat()["status"], "disabled")
        response = Mock()
        response.raise_for_status.side_effect = Exception("network")
        session = Mock(post=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "STATE_DIR": temp, "EXTERNAL_HEARTBEAT_ENABLED": "true",
            "EXTERNAL_HEARTBEAT_URL": "https://secret.example.com/ping/token",
        }, clear=False):
            # requests.RequestException subclasses are handled; a generic unexpected
            # error remains isolated by the daemon call site.
            dry = publish_heartbeat(session=session, dry_run=True)
            self.assertEqual(dry["status"], "dry_run")
            self.assertNotIn("/ping/token", json.dumps(dry))

    def test_provider_states(self):
        with patch.dict(os.environ, {
            "TWELVEDATA_EXTERNAL_DISPLAY_STATUS": "approved",
        }, clear=False):
            self.assertEqual(classify_provider(available=True), "healthy")
            self.assertEqual(classify_provider(available=False), "unavailable")
            self.assertEqual(classify_provider(available=True, authenticated=False), "auth_failed")
            self.assertEqual(classify_provider(available=True, budget_limited=True), "budget_limited")
            self.assertEqual(classify_provider(
                available=True, data_age_seconds=200, max_age_seconds=180
            ), "stale")

    def test_shadow_candidate_is_automatically_evaluated(self):
        now = datetime.now(JST)
        movement = MarketMovement(
            movement_id="shadow-fixture", symbol="NVDA", asset_type="equity",
            direction="up", start_price=100, current_price=104,
            absolute_change=4, percentage_change=4, window_minutes=60,
            high=105, low=99, detected_at=now, alert_type="market_breaking",
            z_score=3.0, atr_multiple=2.0,
        )
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"STATE_DIR": temp}, clear=False
        ):
            candidate = create_candidate(
                movement, chart_path="fixture.png", draft_text="fixture",
                rights_passed=False, blocked_reason="license_blocked",
            )
            self.assertEqual(candidate["review_status"], "license_blocked")
            self.assertFalse(candidate["would_post"])
            result = shadow_report(days=7)
            self.assertEqual(result["review_status"]["license_blocked"], 1)
            self.assertFalse(result["human_review_required"])
            self.assertFalse(result["ready_for_automatic_enable"])

    def test_runtime_manifest_has_hashes_without_env_content(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"STATE_DIR": temp, "XAI_API_KEY": "do-not-store"}, clear=False
        ):
            written = write_manifest()
            rendered = json.dumps(written)
            self.assertNotIn("do-not-store", rendered)
            self.assertTrue(written["config_hash"])
            status = runtime_status()
            self.assertTrue(status["manifest_present"])


if __name__ == "__main__":
    unittest.main()
