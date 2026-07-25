import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "narrative_bot"))
sys.path.insert(0, str(ROOT / "src" / "common"))

from market_narrative import _normalize_candidate, build_caption
from safety import build_x_thread_text


class NarrativeCaptionCompletionTest(unittest.TestCase):
    def test_caption_keeps_complete_sentence_beyond_old_60_character_limit(self):
        ending = "政策金利の据え置きか先行きの示唆かで、金利・ドル・株式の反応を最後まで確認する必要があります。"
        candidate = _normalize_candidate({
            "title": "FOMC前の市場",
            "conclusion": "FOMCと物価・成長指標で相場の方向感が決まりやすい。",
            "what": "30日未明のFOMCに加え、同日にGDPとPCE、前週に耐久財受注が控えています。",
            "why": ending,
            "watch_points": ["会見後の米国債利回りと大型テックの反応を確認します。"],
        })

        caption = build_caption(candidate)

        self.assertIn(ending, caption)
        self.assertNotIn("…", caption)
        self.assertTrue(caption.endswith("確認します。"))

    def test_thread_split_preserves_the_full_caption(self):
        candidate = {
            "conclusion": "重要イベントが重なるため、値動きが大きくなる可能性があります。",
            "what": "FOMC、GDP、PCEなど複数の重要材料が同じ週に集中しています。",
            "why": "政策金利の据え置きか先行きの示唆かで、金利・ドル・株式の反応を最後まで確認する必要があります。",
            "watch_points": ["会見後の米国債利回りと大型テックの反応を確認します。"],
        }
        caption = build_caption(candidate)

        parent, replies = build_x_thread_text(caption)
        combined = "\n".join([parent, *replies])

        self.assertIn("最後まで確認する必要があります。", combined)
        self.assertIn("大型テックの反応を確認します。", combined)
        self.assertFalse(parent.endswith("…"))
        self.assertTrue(all(not reply.endswith("…") for reply in replies))


if __name__ == "__main__":
    unittest.main()
