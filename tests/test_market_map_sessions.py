import unittest
from datetime import datetime, timezone

import pandas as pd

from local_finance_bot import ET, next_run_utc
from market_map.generate_headline import make_caption, make_headline
from market_map.run_market_map import _market_move_gate


class MarketMapSessionTests(unittest.TestCase):
    def test_quiet_market_never_passes_on_schedule_alone(self):
        gate = _market_move_gate(
            100e9, 0.2, 0.9,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
        )
        self.assertFalse(gate["pass"])

    def test_large_index_or_market_cap_move_passes(self):
        by_index = _market_move_gate(
            100e9, -1.1, 0.2,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
        )
        by_cap = _market_move_gate(
            550e9, 0.4, 0.2,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
        )
        self.assertTrue(by_index["pass"])
        self.assertTrue(by_cap["pass"])

    def test_sector_concentration_requires_meaningful_move(self):
        quiet = _market_move_gate(
            200e9, 0.2, 0.8,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
        )
        large = _market_move_gate(
            300e9, 0.2, 0.8,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
        )
        self.assertFalse(quiet["skew"])
        self.assertTrue(large["skew"])

    def test_rotation_requires_extreme_breadth_and_large_sector_move(self):
        rotation = _market_move_gate(
            100e9, 0.05, 0.4, 0.722, -4.25,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
            min_breadth=0.7, min_sector_pct=1.5,
        )
        quiet_sector = _market_move_gate(
            100e9, 0.05, 0.4, 0.722, -0.8,
            min_abs=500e9, min_pct=1.0, min_skew=0.7,
            min_breadth=0.7, min_sector_pct=1.5,
        )
        self.assertTrue(rotation["rotation"])
        self.assertTrue(rotation["pass"])
        self.assertFalse(quiet_sector["rotation"])

    def test_caption_explains_breadth_rotation(self):
        frame = pd.DataFrame([
            {"ticker": f"UP{i}", "percent_change": .01, "market_cap": 100.0}
            for i in range(7)
        ] + [
            {"ticker": f"DOWN{i}", "percent_change": -.02, "market_cap": 100.0}
            for i in range(3)
        ])
        sectors = pd.DataFrame([
            {"sector": "Information Technology", "market_cap_change": -20.0},
            {"sector": "Health Care", "market_cap_change": 21.0},
        ])
        caption = make_caption(frame, 1.0, sectors)
        self.assertIn("上昇 7（70.0%）／下落 3", caption)
        self.assertIn("セクターローテーション相場", caption)

    def test_pre_close_headline_and_caption(self):
        frame=pd.DataFrame([
            {"ticker":"AAA","percent_change":-.02},
            {"ticker":"BBB","percent_change":.01},
        ])
        sectors=pd.DataFrame([
            {"sector":"Information Technology","market_cap_change":-2e11},
            {"sector":"Health Care","market_cap_change":1e11},
        ])
        self.assertIn("near the close",make_headline(-1e12,session="pre_close"))
        self.assertIn("取引終了直前",make_caption(frame,-1e12,sectors,session="pre_close"))

    def test_market_map_has_pre_close_et_run(self):
        now=datetime(2026,7,24,14,0,tzinfo=ET).astimezone(timezone.utc)
        schedule={"market-map":{"enabled":True,"type":"et_times_business_days","times":["09:35","15:50"]}}
        result=next_run_utc("market-map",schedule,now).astimezone(ET)
        self.assertEqual(result.strftime("%H:%M"),"15:50")


if __name__=="__main__":
    unittest.main()
