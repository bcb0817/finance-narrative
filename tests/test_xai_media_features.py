import json,os,sys,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT/"src"/"common"))
from xai_radar import _parse_topics,load_cache,refresh,status,topic_velocity,usage_summary
from post_style import STYLES,choose_style,enforce_hashtag_limit,load_weights
from dynamic_posting import posting_window
from quote_queue import enqueue_from_topics,list_queue
from experiments import variant_summary
from market_map.calculate_market_cap_move import calculate_market_cap_move
from news_bot.post import idle_fallback_allowed
import pandas as pd

JST=timezone(timedelta(hours=9))
class FakeResponses:
    def __init__(self,text): self.text=text; self.calls=[]
    def create(self,**kwargs):
        self.calls.append(kwargs); return SimpleNamespace(output_text=self.text,usage=SimpleNamespace(input_tokens=10,output_tokens=5,cost_in_usd_ticks=10000000,num_server_side_tools_used=1))
class FakeClient:
    def __init__(self,text): self.responses=FakeResponses(text)

class XaiMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.env=patch.dict(os.environ,{"STATE_DIR":self.tmp.name,"OUTPUT_DIR":self.tmp.name,"XAI_ENABLED":"true","XAI_X_SEARCH_ENABLED":"true","XAI_API_KEY":"test-key","XAI_MODEL":"grok-test","XAI_MAX_SEARCH_CALLS_PER_DAY":"6","XAI_MONTHLY_BUDGET_USD":"10","XAI_CACHE_TTL_MINUTES":"60","QUOTE_DRAFT_AI_ENABLED":"false"},clear=False); self.env.start()
    def tearDown(self): self.env.stop(); self.tmp.cleanup()
    def sample(self): return json.dumps({"topics":[{"topic":"AI chips","tickers":["NVDA"],"category":"semiconductor","mention_count":12,"mention_count_60m":12,"mention_count_6h":18,"representative_posts":[{"post_id":"1","url":"https://x.com/example/status/1","username":"example","excerpt":"short"}],"representative_accounts":["example"],"consensus_view":"strong","dissenting_view":"valuation","possible_misconception":"demand equals revenue","source_reliability":"medium","primary_source_available":False}]})
    def test_parse_and_velocity(self):
        row=_parse_topics(self.sample(),datetime(2026,7,21,tzinfo=JST))[0]
        self.assertEqual(row["acceleration_score"],4.0); self.assertEqual(row["news_confirmation_status"],"unverified")
    def test_refresh_cache_usage_and_tool(self):
        client=FakeClient(self.sample()); result=refresh(client=client)
        self.assertEqual(result["status"],"ok"); self.assertEqual(len(load_cache()),1)
        self.assertEqual(client.responses.calls[0]["tools"][0]["type"],"x_search")
        self.assertEqual(usage_summary()["daily_calls"],1)
    def test_cache_expires(self):
        refresh(client=FakeClient(self.sample()))
        future=datetime.now(JST)+timedelta(hours=2); self.assertEqual(load_cache(future),[])
    def test_missing_key_is_safe_skip(self):
        with patch.dict(os.environ,{"XAI_API_KEY":""}): self.assertEqual(refresh()["status"],"skipped")
    def test_daily_limit_and_budget(self):
        with patch.dict(os.environ,{"XAI_MAX_SEARCH_CALLS_PER_DAY":"1",
                                    "XAI_EVENT_BURST_ENABLED":"false"}):
            refresh(client=FakeClient(self.sample())); self.assertEqual(refresh(client=FakeClient(self.sample()))["reason"],"daily_limit")
    def test_daily_unique_post_cap_reduces_second_search_output(self):
        topics=[]
        for index in range(5):
            topics.append({"topic":f"topic-{index}","tickers":[],"category":"other","summary":"",
                "observed_mention_count":1,"velocity_score":1,"acceleration_score":1,
                "representative_posts":[
                    {"post_id":f"{index}-a","url":"https://x.com/a","username":"a","excerpt":"a"},
                    {"post_id":f"{index}-b","url":"https://x.com/b","username":"b","excerpt":"b"},
                ],"representative_accounts":[],"source_reliability":"unknown",
                "primary_source_available":False,"source_confirmation":"x_discussion"})
        payload=json.dumps({"topics":topics})
        with patch.dict(os.environ,{"XAI_MAX_SEARCH_CALLS_PER_DAY":"2","XAI_MAX_UNIQUE_POSTS_PER_DAY":"15"}):
            first=FakeClient(payload); second=FakeClient(payload)
            self.assertEqual(refresh(client=first)["status"],"ok")
            self.assertEqual(refresh(client=second)["status"],"ok")
            self.assertEqual(second.responses.calls[0]["text"]["format"]["schema"]["properties"]["topics"]["items"]["properties"]["representative_posts"]["maxItems"],1)
            self.assertEqual(usage_summary()["daily_unique_posts"],15)
    def test_style_weights_and_no_repeat(self):
        self.assertEqual(set(load_weights()),set(STYLES)); self.assertNotEqual(choose_style(suggested="comparison",recent_styles=["comparison"]),"comparison")
        self.assertEqual(enforce_hashtag_limit("本文 #AI #株").count("#"),1)
    def test_dynamic_quiet_gap(self):
        with patch("dynamic_posting.hours_since_last_post",return_value=0.5): self.assertFalse(posting_window(0)["allow"])
        with patch("dynamic_posting.hours_since_last_post",return_value=0.1): self.assertTrue(posting_window(9)["allow"])
    def test_idle_fallback_requires_quality_and_signal(self):
        good={"post_value":6,"us_equity_relevance":5,"has_independent_angle":True,"x_topic_acceleration":2,"primary_source_importance":1}
        self.assertTrue(idle_fallback_allowed(good,"AI chip update"))
        self.assertFalse(idle_fallback_allowed({**good,"post_value":5},"AI chip update"))
        self.assertFalse(idle_fallback_allowed({**good,"x_topic_acceleration":0},"AI chip update"))
    def test_quote_queue_is_pending_and_has_three_drafts(self):
        topic=_parse_topics(self.sample())[0]; rows=enqueue_from_topics([topic])
        self.assertEqual(rows[0]["status"],"pending"); self.assertEqual(len(rows[0]["comment_drafts"]),3); self.assertEqual(len(list_queue(pending=True)),1)
    def test_variant_summary_and_missing_data(self):
        posts=[{"tweet_id":"1","experiment_variant":"comparison"},{"tweet_id":"2","experiment_variant":"comparison"}]
        metrics=[{"tweet_id":"1","stage":"24h","impressions_per_hour":10}]
        row=variant_summary(posts,metrics)[0]; self.assertEqual(row["sample_size"],1); self.assertEqual(row["confidence"],"insufficient")
    def test_market_map_coerces_numeric_strings(self):
        df=pd.DataFrame([{"current_price":"110","prev_close":"100","market_cap":"1000","sector":"Tech"}])
        out,total,sectors=calculate_market_cap_move(df); self.assertAlmostEqual(total,100); self.assertAlmostEqual(float(sectors.iloc[0]["market_cap_change"]),100)

if __name__=="__main__": unittest.main()
