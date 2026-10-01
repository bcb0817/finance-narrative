import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.wordpress import social_publisher as social


class WordPressSocialPublisherTests(unittest.TestCase):
    def test_text_contains_title_url_and_tags(self):
        x, threads = social._texts({"title":{"rendered":"日本株 &amp; 米国株"},"excerpt":{"rendered":"<p>要点です。</p>"},"link":"https://example.com/p/1"})
        self.assertIn("日本株 & 米国株", x); self.assertIn("https://example.com/p/1", x)
        self.assertIn("要点です", threads); self.assertLessEqual(len(x), 280)

    def test_first_run_is_baseline_without_posting(self):
        post={"id":27,"modified_gmt":"2026-08-28T01:00:00","link":"https://example.com/?p=27","title":{"rendered":"記事"},"excerpt":{"rendered":"要約"}}
        with tempfile.TemporaryDirectory() as temp, patch.object(social,"state_dir",return_value=Path(temp)), patch.object(social,"_wp_posts",return_value=[post]), patch.dict(os.environ,{"BLOG_SOCIAL_AUTO_POST_ENABLED":"true"}), patch.object(social,"post_to_x") as x:
            result=social.sync(); self.assertEqual(result["status"],"initialized"); x.assert_not_called()
            result=social.sync(); self.assertEqual(result["posted"],0); x.assert_not_called()

    def test_modified_article_posts_once_per_channel(self):
        old={"id":27,"modified_gmt":"2026-08-28T01:00:00","link":"https://example.com/?p=27","title":{"rendered":"記事"},"excerpt":{"rendered":"要約"}}
        new={**old,"modified_gmt":"2026-08-28T02:00:00"}
        env={"BLOG_SOCIAL_AUTO_POST_ENABLED":"true","BLOG_SOCIAL_X_ENABLED":"true","BLOG_SOCIAL_THREADS_ENABLED":"true"}
        with tempfile.TemporaryDirectory() as temp, patch.object(social,"state_dir",return_value=Path(temp)), patch.dict(os.environ,env), patch.object(social,"_wp_posts",return_value=[old]):
            social.sync()
            with patch.object(social,"_wp_posts",return_value=[new]), patch.object(social,"post_to_x",return_value="x1") as x, patch.object(social,"post_to_threads",return_value="t1") as threads:
                result=social.sync(); self.assertEqual(result["posted"],2); x.assert_called_once(); threads.assert_called_once()
                result=social.sync(); self.assertEqual(result["posted"],0); x.assert_called_once(); threads.assert_called_once()

    def test_invalid_wordpress_response_is_a_nonfatal_unavailable_result(self):
        with patch.dict(os.environ, {"BLOG_SOCIAL_AUTO_POST_ENABLED":"true"}), patch.object(social, "_wp_posts", side_effect=ValueError("empty response")):
            result = social.sync()
        self.assertEqual(result["status"], "source_unavailable")
        self.assertEqual(result["posted"], 0)
        self.assertEqual(result["reason"], "ValueError")


if __name__ == "__main__": unittest.main()
