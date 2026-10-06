import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.xai_sentiment import analyze_x_sentiment, validate_sentiment
from narrative_bot import market_narrative


def valid_result():
    return {
        "topic": "NVIDIA earnings",
        "sentiment": {"bullish": 62, "neutral": 18, "bearish": 20},
        "sentiment_score": 42,
        "momentum": "rising",
        "confidence": 0.78,
        "dominant_narratives": [{
            "theme": "AI demand remains strong", "stance": "bullish",
            "importance": 0.91,
        }],
        "contrarian_view": "Some traders expect a sell-the-news reaction.",
        "summary": "Bullish discussion with valuation concerns.",
        "post_angle": "市場は決算よりAI需要の持続性を見ている",
    }


class XaiSentimentTests(unittest.TestCase):
    def client(self, payload=None, error=None):
        responses = Mock()
        if error:
            responses.create.side_effect = error
        else:
            responses.create.return_value = SimpleNamespace(
                output_text=json.dumps(payload if payload is not None else valid_result())
            )
        return SimpleNamespace(responses=responses)

    def test_valid_json_and_x_search_request(self):
        client = self.client()
        with patch.dict(os.environ, {"XAI_SENTIMENT_ENABLED": "true"}):
            result = analyze_x_sentiment("NVIDIA earnings", client=client)
        self.assertEqual(result["sentiment_score"], 42)
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["tools"], [{"type": "x_search"}])
        self.assertEqual(kwargs["max_tool_calls"], 1)
        self.assertTrue(kwargs["text"]["format"]["strict"])

    def test_schema_ranges_and_enums(self):
        for field, value in (
            ("sentiment_score", 101), ("momentum", "surging"), ("confidence", 1.1),
        ):
            row = valid_result()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_sentiment(row)
        row = valid_result()
        row["sentiment"]["bullish"] = 61
        with self.assertRaises(ValueError):
            validate_sentiment(row)

    def test_feature_flag_false_does_not_call_api(self):
        client = self.client()
        with patch.dict(os.environ, {"XAI_SENTIMENT_ENABLED": "false"}):
            self.assertIsNone(analyze_x_sentiment("topic", client=client))
        client.responses.create.assert_not_called()

    def test_missing_key_falls_back(self):
        with patch.dict(os.environ, {"XAI_SENTIMENT_ENABLED": "true", "XAI_API_KEY": ""}):
            self.assertIsNone(analyze_x_sentiment("topic"))

    def test_api_error_and_malformed_response_fall_back(self):
        with patch.dict(os.environ, {"XAI_SENTIMENT_ENABLED": "true"}):
            self.assertIsNone(analyze_x_sentiment("topic", client=self.client(error=TimeoutError())))
            self.assertIsNone(analyze_x_sentiment("topic", client=self.client(payload={"bad": True})))

    def test_narrative_signal_flow_calls_sentiment_once(self):
        lead = [{
            "title": "NVIDIA earnings", "source": "Example News",
            "group": "market_news", "published_at": "2026-08-25T00:00:00Z",
        }]
        with (
            patch.object(market_narrative, "_gather_news", return_value=lead),
            patch.object(market_narrative, "_gather_events", return_value=[]),
            patch.object(market_narrative, "_gather_reddit", return_value=[]),
            patch("common.xai_sentiment.analyze_x_sentiment", return_value=valid_result()) as call,
        ):
            signals = market_narrative.gather_signals()
        self.assertEqual(signals["x_sentiment"]["sentiment_score"], 42)
        call.assert_called_once()

    def test_narrative_prompt_labels_estimate_and_contrarian_context(self):
        prompt = market_narrative._build_prompt({
            "news": [{"title": "NVIDIA earnings", "source": "Example", "group": "market_news"}],
            "events": [], "reddit": [], "x_sentiment": valid_result(),
        })
        self.assertIn("X Sentiment Estimate", prompt)
        self.assertIn("contrarian_view", prompt)
        self.assertIn("X全体を代表する統計ではない", prompt)
