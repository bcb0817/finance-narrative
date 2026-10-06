from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from common.runtime import JST
from integrations.wordpress.client import WordPressRadarClient, WordPressRadarError
from integrations.wordpress.fixture import radar_fixture
from integrations.wordpress.queue import RadarPublishQueue
from integrations.wordpress.radar_models import DISCLAIMER, attention_level, build_snapshot
from integrations.wordpress.radar_publisher import publish, retry_queue


class Response:
    def __init__(self, code, data=None): self.status_code, self.data = code, data or {}
    def json(self): return self.data


class WordPressRadarTests(unittest.TestCase):
    def payload(self): return build_snapshot(radar_fixture(datetime(2026, 8, 28, 21, 0, tzinfo=JST))).to_dict()

    def test_fixture_to_allowlisted_public_snapshot(self):
        value = self.payload()
        self.assertEqual(1, value["schema_version"]); self.assertEqual(5, len(value["topics"]))
        self.assertEqual(DISCLAIMER, value["disclaimer"]); self.assertTrue(value["observed_at"].endswith("+09:00"))
        self.assertEqual({"schema_version","snapshot_id","run_id","observed_at","generated_at","radar_type","disclaimer","topics"}, set(value))
        self.assertNotIn("excerpt", json.dumps(value)); self.assertNotIn("cost", json.dumps(value))

    def test_limits_sanitize_xss_urls_and_trend(self):
        source = radar_fixture(); source["topics"] *= 2
        source["topics"][0]["title"] = '<script>alert(1)</script>NVIDIA'
        source["topics"][0]["representative_posts"] = [
            {"url": "javascript:alert(1)", "handle": "bad"},
            *source["topics"][0]["representative_posts"] * 3]
        value = build_snapshot(source).to_dict()
        self.assertEqual(5, len(value["topics"])); self.assertNotIn("<script", value["topics"][0]["title"])
        self.assertLessEqual(len(value["topics"][0]["representative_posts"]), 2)
        self.assertTrue(all(p["url"].startswith("https://x.com/") for p in value["topics"][0]["representative_posts"]))
        self.assertEqual("up", value["topics"][0]["trend_direction"])

    def test_attention_thresholds(self):
        with patch.dict(os.environ, {"X_RADAR_ATTENTION_HIGH":"70", "X_RADAR_ATTENTION_VERY_HIGH":"85"}):
            self.assertEqual("LOW", attention_level(1, 2)); self.assertEqual("NORMAL", attention_level(40, 20))
            self.assertEqual("HIGH", attention_level(70, 1)); self.assertEqual("VERY_HIGH", attention_level(1, 85))

    def test_secret_shaped_internal_data_not_serialized(self):
        fixture = radar_fixture(); fixture["XAI_API_KEY"] = "secret"; fixture["raw_headers"] = {"Authorization":"Bearer secret"}
        text = json.dumps(build_snapshot(fixture).to_dict())
        self.assertNotIn("secret", text); self.assertNotIn("Authorization", text)

    def test_https_and_basic_auth_without_logging_secret(self):
        session = Mock(); session.post.return_value = Response(201, {"status":"created"})
        client = WordPressRadarClient("https://example.com", "radar", "app secret", session=session)
        with self.assertLogs("integrations.wordpress.client", level="INFO") as logs:
            # Ensure logger exists without exposing request headers.
            import logging; logging.getLogger("integrations.wordpress.client").info("publish started")
            result = client.publish(self.payload())
        self.assertEqual("created", result["status"]); self.assertNotIn("app secret", "".join(logs.output))
        with self.assertRaises(WordPressRadarError): WordPressRadarClient("http://example.com", "u", "p").validate()

    def test_http_status_retry_policy(self):
        for status, calls in [(401,1),(403,1),(404,1),(429,3),(500,3)]:
            session=Mock(); session.post.return_value=Response(status)
            with patch("integrations.wordpress.client.time.sleep"):
                with self.assertRaises(WordPressRadarError): WordPressRadarClient("https://example.com","u","p",session=session).publish(self.payload())
            self.assertEqual(calls, session.post.call_count)
        session=Mock(); session.post.side_effect=requests.Timeout()
        with patch("integrations.wordpress.client.time.sleep"):
            with self.assertRaises(WordPressRadarError): WordPressRadarClient("https://example.com","u","p",session=session).publish(self.payload())
        self.assertEqual(3, session.post.call_count)

    def test_queue_dedup_and_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            queue=RadarPublishQueue(Path(temp)/"queue.jsonl"); payload=self.payload()
            queue.enqueue(payload); queue.enqueue(payload); self.assertEqual(1,len(queue.rows()))
            client=Mock(); client.publish.return_value={"status":"created"}
            with patch("integrations.wordpress.radar_publisher.RadarPublishQueue", return_value=queue):
                result=retry_queue(client=client)
            self.assertEqual("published", result["processed"][0]["status"])

    def test_feature_gates_do_not_call_wordpress(self):
        client=Mock()
        with patch.dict(os.environ,{"WORDPRESS_RADAR_ENABLED":"false","WORDPRESS_RADAR_PUBLIC_ENABLED":"false"}):
            self.assertEqual("disabled",publish(fixture=True,client=client)["status"])
        client.publish.assert_not_called()

    def test_public_disabled_queues(self):
        with tempfile.TemporaryDirectory() as temp, patch("integrations.wordpress.radar_publisher.RadarPublishQueue", return_value=RadarPublishQueue(Path(temp)/"q.jsonl")), patch.dict(os.environ,{"WORDPRESS_RADAR_ENABLED":"true","WORDPRESS_RADAR_PUBLIC_ENABLED":"false"}):
            self.assertEqual("public_disabled",publish(fixture=True)["status"])

    def test_discord_success_and_failure_messages_are_safe(self):
        from integrations.wordpress.radar_publisher import _notify
        payload=self.payload()
        with patch("common.operations_alerts.queue_discord_log") as notify:
            _notify(True,payload,"https://example.com/x-radar/")
            self.assertIn("同期しました",notify.call_args.args[1])
            _notify(False,payload,"WordPressRadarError retry=1")
            self.assertIn("同期失敗",notify.call_args.args[1])
            self.assertNotIn("Authorization",notify.call_args.args[1])


class PluginStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.php = (Path(__file__).parents[1]/"integrations/wordpress/finance-x-radar/finance-x-radar.php").read_text(encoding="utf-8")
        cls.css = (Path(__file__).parents[1]/"integrations/wordpress/finance-x-radar/assets/radar.css").read_text(encoding="utf-8")
        cls.share_css = (Path(__file__).parents[1]/"integrations/wordpress/finance-x-radar/assets/share.css").read_text(encoding="utf-8")
        cls.share_js = (Path(__file__).parents[1]/"integrations/wordpress/finance-x-radar/assets/share.js").read_text(encoding="utf-8")
        cls.article_css = (Path(__file__).parents[1]/"integrations/wordpress/finance-x-radar/assets/article.css").read_text(encoding="utf-8")

    def test_rest_auth_schema_cpt_shortcode_latest_archive(self):
        for token in ["finance_radar_snap","finance-radar/v1","can_write","publish_posts","validate_payload","sanitize_payload","/snapshots/latest","shortcode","finance_x_radar"]:
            self.assertIn(token,self.php)
    def test_security_retention_noindex_mobile_utf8(self):
        for token in ["MAX_BYTES","wp_strip_all_tags","esc_html","x_url","cleanup","noindex","@media","X Searchで観測された金融テーマ"]:
            self.assertIn(token,self.php+self.css)
    def test_article_social_share_bar(self):
        for token in ["social_share_bar","twitter.com/intent/tweet","facebook.com/sharer","threads.net/intent/post","linkedin.com/sharing/share-offsite","social-plugins.line.me","b.hatena.ne.jp","data-fxr-copy-url","singa9999"]:
            self.assertIn(token,self.php)
        self.assertIn("navigator.clipboard",self.share_js)
        self.assertIn("@media(max-width:520px)",self.share_css)
    def test_article_view_counter(self):
        for token in ["wp_ajax_fxr_count_view","wp_ajax_nopriv_fxr_count_view","_fxr_view_count","get_transient","30*MINUTE_IN_SECONDS","data-fxr-view-count"]:
            self.assertIn(token,self.php)
        for token in ["fxr_count_view","fetch(","toLocaleString"]:
            self.assertIn(token,self.share_js)
    def test_article_structure_and_typography(self):
        for token in ["structure_article","fxr-article-toc","fxr-section-number","article-section-","finance-author-socials","threads.net/@singa9999"]:
            self.assertIn(token,self.php)
        for token in ["Noto Sans JP","wp-block-post-content","market-lead","finance-author-profile","prefers-color-scheme:dark","entry-content.alignfull","margin-left:auto"]:
            self.assertIn(token,self.article_css)


if __name__ == "__main__": unittest.main()
