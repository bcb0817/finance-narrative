import unittest
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from common.openai_config import OpenAIRole
from common.openai_service import OpenAIService
from common.openai_service import DailyLimitError
from common import performance_learning
from market_map import generate_market_map_post


class DailyReviewRegressionTests(unittest.TestCase):
    def test_legacy_minimal_reasoning_is_sent_as_low(self):
        client = Mock()
        client.responses.create.return_value = Mock(output_text="ok", usage=None)
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"STATE_DIR": state}):
            service = OpenAIService(client=client)
            self.assertEqual(service.text("test", role=OpenAIRole.GENERATE, reasoning="minimal"), "ok")

        self.assertEqual(client.responses.create.call_args.kwargs["reasoning"], {"effort": "low"})

    def test_market_map_sector_skew_uses_numeric_column_only(self):
        frame = pd.DataFrame({"market_cap": [100.0, 200.0]})
        sectors = pd.DataFrame({"sector": ["Tech", "Energy"], "market_cap_change": [20.0, -10.0]})

        with patch("market_map.fetch_market_data", return_value=frame), \
             patch("market_map.calculate_market_cap_move", return_value=(frame, 10.0, sectors)), \
             patch("market_map.make_headline", return_value="headline"), \
             patch("market_map.make_caption", return_value="caption"), \
             patch("market_map.build_treemap", return_value="map.png"):
            result = generate_market_map_post()

        self.assertAlmostEqual(result["sector_skew"], 2 / 3)
        self.assertEqual(result["top_sector"], "Tech")

    def test_daily_analyze_limit_is_normal_skip_not_error(self):
        now=datetime.now(performance_learning.JST)
        metrics=[{
            "tweet_id":"1","bot":"news","title":"test","text":"test",
            "posted_at":(now-timedelta(hours=8)).isoformat(),
            "metrics_collected_at":now.isoformat(),"stage":"6h",
            "impressions":100,
        }]
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(performance_learning,"_root",return_value=Path(temp)), \
             patch.object(performance_learning.logger,"exception") as logged_exception:
            with patch("common.openai_service.OpenAIService.structured",
                       side_effect=DailyLimitError("analyze daily limit reached")):
                result=performance_learning.update_daily_learning(metrics)
        self.assertEqual(result["status"],"skipped")
        self.assertEqual(result["message"],
                         "上位1件を保存し、AIレビューは日次上限のため正常スキップしました")
        logged_exception.assert_not_called()
