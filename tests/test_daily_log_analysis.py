import json, os, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common.daily_log_analysis import analyze_daily_logs, redact
from common.runtime import JST


class DailyLogAnalysisTests(unittest.TestCase):
    def test_redacts_keys_and_tokens(self):
        value=redact("API_KEY=xai-abcdefghijklmnopqrstuvwxyz token=secret-value")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz",value)
        self.assertNotIn("secret-value",value)
        self.assertIn("<redacted>",value)

    def test_collects_failures_and_writes_daily_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); logs=root/"logs"; state=root/"state"; outputs=root/"outputs"
            logs.mkdir(); (state/"openai").mkdir(parents=True); (state/"xai").mkdir(parents=True)
            now=datetime.now(JST); ts=now.isoformat()
            (logs/"run_history.jsonl").write_text(json.dumps({"ts":ts,"bot":"news","returncode":1})+"\n",encoding="utf-8")
            (logs/"errors.jsonl").write_text(json.dumps({"ts":ts,"error":"401 Unauthorized","token":"do-not-leak"})+"\n",encoding="utf-8")
            (state/"openai"/"api_usage.jsonl").write_text(json.dumps({"timestamp":ts,"success":False,"error_type":"APITimeoutError"})+"\n",encoding="utf-8")
            with patch.dict(os.environ,{"LOG_DIR":str(logs),"STATE_DIR":str(state),"OUTPUT_DIR":str(outputs),
                                        "DAILY_LOG_ANALYSIS_LOOKBACK_HOURS":"24"}):
                result=analyze_daily_logs(now,force=True)
                self.assertEqual(result["status"],"attention")
                self.assertEqual(result["summary"]["failed_runs"],1)
                self.assertTrue((outputs/"log_analysis"/f"{now.date()}.json").exists())
                serialized=json.dumps(result)
                self.assertNotIn("do-not-leak",serialized)

    def test_second_run_reuses_daily_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/"logs").mkdir(); (root/"state").mkdir()
            with patch.dict(os.environ,{"LOG_DIR":str(root/"logs"),"STATE_DIR":str(root/"state"),"OUTPUT_DIR":str(root/"outputs")}):
                first=analyze_daily_logs(force=True)
                second=analyze_daily_logs()
                self.assertEqual(first["generated_at"],second["generated_at"])


if __name__ == "__main__": unittest.main()
