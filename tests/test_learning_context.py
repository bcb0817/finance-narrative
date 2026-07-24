import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "common"))
import performance_learning as pl


class LearningContextTests(unittest.TestCase):
    def test_patterns_are_inserted_into_prompt(self):
        with patch.object(pl, "load_learning_context", return_value="- 結論を先に置く"):
            out = pl.with_performance_learning("ニュースを要約")
        self.assertIn("結論を先に置く", out)
        self.assertIn("ニュースを要約", out)

    def test_latest_markdown_contains_abstract_rules(self):
        text = pl._render_latest_markdown({"rolling_rules": [{"rule": "企業名を冒頭に置く"}],
                                           "avoid_patterns": ["無意味な矢印"]}, "2026-01-01")
        self.assertIn("企業名を冒頭に置く", text)
        self.assertIn("無意味な矢印", text)


if __name__ == "__main__": unittest.main()
