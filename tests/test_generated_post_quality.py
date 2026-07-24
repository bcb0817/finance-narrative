import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "common"))

from safety import (
    generated_post_quality_error,
    normalize_generated_post_text,
    safety_check,
)


class GeneratedPostQualityTest(unittest.TestCase):
    def test_repairs_reversed_empty_brackets_at_sentence_end(self):
        broken = "次の確認：各国の対応と企業開示】【。"
        fixed = normalize_generated_post_text(broken)

        self.assertEqual(fixed, "次の確認：各国の対応と企業開示。")
        self.assertEqual(generated_post_quality_error(fixed), "")

    def test_rejects_unfinished_sentence_endings(self):
        for text in ("金利…", "次の確認：", "各国の対応、", "注目点【"):
            with self.subTest(text=text):
                self.assertTrue(generated_post_quality_error(text))
                with self.assertRaises(ValueError):
                    safety_check(text)

    def test_allows_normal_adjacent_labels(self):
        text = "【米国株】【決算】大型テックの利益率を確認。"
        self.assertEqual(normalize_generated_post_text(text), text)
        self.assertEqual(generated_post_quality_error(text), "")


if __name__ == "__main__":
    unittest.main()
