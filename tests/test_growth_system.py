import json, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common.growth_config import effective_radar_status
from common.metrics_collector import enrich_metrics, missed_stages
from common.operations_alerts import (
    flush_discord_logs,
    notify_x_post,
    queue_discord_log,
    send_discord_alerts,
    write_alerts,
)
from common.xai_radar import topic_velocity
from common.experiments import load_experiments
from local_finance_bot import JST, next_run_utc


class GrowthSystemTests(unittest.TestCase):
    def test_schedule_has_six_priority_windows(self):
        now=datetime(2026,7,22,8,1,tzinfo=JST)
        conf={"enabled":True,"type":"daily_jst_times","times":["00:00","06:00","08:00","17:00","21:00","22:30"]}
        self.assertEqual(next_run_utc("radar",{"radar":conf},now.astimezone(timezone.utc)).astimezone(JST).strftime("%H:%M"),"17:00")

    def test_radar_effective_is_and_condition(self):
        with patch.dict(os.environ,{"XAI_ENABLED":"true","XAI_X_SEARCH_ENABLED":"true","XAI_API_KEY":"dummy",
                                    "XAI_MAX_SEARCH_CALLS_PER_DAY":"6","STATE_DIR":tempfile.gettempdir()}):
            self.assertFalse(effective_radar_status({"enabled":False})["effective_enabled"])

    def test_observed_metrics_are_explicit_and_compatible(self):
        row=topic_velocity(12,18)
        self.assertEqual(row["observed_velocity_60m"],row["velocity_60m"])
        self.assertIn("observed_acceleration_score",row)

    def test_missed_windows_and_null_growth_inputs(self):
        self.assertEqual(missed_stages(8,set()),["1h","6h"])
        now=datetime.now(JST)
        row=enrich_metrics({"impressions":100,"reposts":5,"profile_clicks":None,"follows":None},
                           (now-timedelta(hours=1)).isoformat(),now)
        self.assertIsNone(row["follow_conversion"])
        self.assertIsNotNone(row["growth_score"])

    def test_maximum_three_active_experiments(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"experiments.json"
            path.write_text(json.dumps({"experiments":[{"experiment_id":str(i),"status":"active"} for i in range(5)]}),encoding="utf-8")
            with patch.dict(os.environ,{"MAX_ACTIVE_EXPERIMENTS":"3"}):
                self.assertEqual(len(load_experiments(path)),3)

    def test_stale_heartbeat_creates_local_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/"state").mkdir(); (root/"logs").mkdir()
            old=(datetime.now(JST)-timedelta(minutes=30)).isoformat()
            (root/"state"/"daemon_heartbeat.json").write_text(json.dumps({"updated_at":old}),encoding="utf-8")
            with patch.dict(os.environ,{"STATE_DIR":str(root/"state"),"LOG_DIR":str(root/"logs"),
                                        "OUTPUT_DIR":str(root/"outputs"),"ALERTS_ENABLED":"true",
                                        "ALERT_HEARTBEAT_STALE_MINUTES":"10"}):
                path,rows=write_alerts()
                self.assertTrue(path.exists())
                self.assertIn("heartbeat_stale",[r["code"] for r in rows])

    def test_repeated_fx_quality_blocks_create_high_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/"state"/"fx").mkdir(parents=True); (root/"logs").mkdir()
            (root/"state"/"daemon_heartbeat.json").write_text(
                json.dumps({"updated_at":datetime.now(JST).isoformat()}),encoding="utf-8"
            )
            (root/"state"/"fx"/"state.json").write_text(json.dumps({
                "quality_health":{"status":"degraded","consecutive_blocked_runs":3}
            }),encoding="utf-8")
            env={"STATE_DIR":str(root/"state"),"LOG_DIR":str(root/"logs"),
                 "OUTPUT_DIR":str(root/"outputs"),"ALERTS_ENABLED":"true",
                 "FX_ENABLED":"true","FX_QUALITY_ALERT_CONSECUTIVE_RUNS":"3",
                 "XAI_ENABLED":"false","MARKET_DATA_ENABLED":"false"}
            provider=type("Provider",(),{"available":True})()
            with patch.dict(os.environ,env), patch(
                "fx_alert.providers.get_provider",
                return_value=type("Factory",(),{"status":lambda self,probe=False:provider})(),
            ):
                _,rows=write_alerts()
            alert=next(row for row in rows if row["code"]=="fx_data_quality_degraded")
            self.assertEqual(alert["severity"],"high")

    def test_discord_sends_only_alert_state_changes(self):
        class Response:
            def raise_for_status(self): return None

        class Session:
            def __init__(self): self.calls=[]
            def post(self,url,**kwargs):
                self.calls.append((url,kwargs))
                return Response()

        with tempfile.TemporaryDirectory() as temp:
            session=Session()
            env={"STATE_DIR":temp,"DISCORD_ALERTS_ENABLED":"true",
                 "DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/123/test-token"}
            rows=[{"code":"heartbeat_stale","severity":"high","detail":"heartbeat older than 10 minutes"}]
            with patch.dict(os.environ,env,clear=False):
                first=send_discord_alerts(rows,session=session)
                second=send_discord_alerts(rows,session=session)
                resolved=send_discord_alerts([],session=session)
            self.assertEqual(first["status"],"sent")
            self.assertEqual(second["status"],"unchanged")
            self.assertEqual(resolved["resolved"],1)
            self.assertEqual(len(session.calls),2)
            self.assertNotIn("test-token",json.dumps(first))

    def test_discord_failure_does_not_persist_delivered_state(self):
        class Session:
            def post(self,*args,**kwargs):
                import requests
                raise requests.ConnectionError("offline")

        with tempfile.TemporaryDirectory() as temp:
            env={"STATE_DIR":temp,"DISCORD_ALERTS_ENABLED":"true",
                 "DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/123/test-token"}
            rows=[{"code":"heartbeat_stale","severity":"high","detail":"stale"}]
            with patch.dict(os.environ,env,clear=False):
                result=send_discord_alerts(rows,session=Session())
            self.assertEqual(result["status"],"delivery_failed")
            self.assertFalse((Path(temp)/"discord_alert_state.json").exists())

    def test_successful_x_post_is_mirrored_once(self):
        class Response:
            def raise_for_status(self): return None

        class Session:
            def __init__(self): self.calls=[]
            def post(self,url,**kwargs):
                self.calls.append(kwargs["json"])
                return Response()

        with tempfile.TemporaryDirectory() as temp:
            session=Session()
            env={"STATE_DIR":temp,"DISCORD_POST_NOTIFICATIONS_ENABLED":"true",
                 "DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/123/test-token"}
            post={"tweet_id":"987654","bot":"news","mode":"diagram",
                  "text":"投稿本文です"}
            with patch.dict(os.environ,env,clear=False):
                first=notify_x_post(post,session=session)
                second=notify_x_post(post,session=session)
            self.assertEqual(first["status"],"sent")
            self.assertEqual(second["status"],"duplicate")
            self.assertIn("投稿本文です",session.calls[0]["content"])
            self.assertIn("https://x.com/i/web/status/987654",session.calls[0]["content"])
            self.assertEqual(len(session.calls),1)

    def test_all_logs_are_batched_and_secrets_are_redacted(self):
        class Response:
            def raise_for_status(self): return None

        class Session:
            def __init__(self): self.calls=[]
            def post(self,url,**kwargs):
                self.calls.append(kwargs["json"]["content"])
                return Response()

        with tempfile.TemporaryDirectory() as temp:
            session=Session()
            env={"STATE_DIR":temp,"DISCORD_LOGS_ENABLED":"true",
                 "DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/123/test-token",
                 "API_KEY":"super-secret-value"}
            with patch.dict(os.environ,env,clear=False):
                self.assertTrue(queue_discord_log(
                    "test","API_KEY=super-secret-value completed",level="INFO"))
                result=flush_discord_logs(session=session,max_batches=5)
            self.assertEqual(result["status"],"sent")
            self.assertEqual(result["remaining_rows"],0)
            delivered="\n".join(session.calls)
            self.assertIn("<redacted>",delivered)
            self.assertNotIn("super-secret-value",delivered)


if __name__ == "__main__": unittest.main()
