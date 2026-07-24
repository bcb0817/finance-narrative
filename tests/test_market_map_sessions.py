import unittest
from datetime import datetime, timezone

import pandas as pd

from local_finance_bot import ET, next_run_utc
from market_map.generate_headline import make_caption, make_headline


class MarketMapSessionTests(unittest.TestCase):
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
