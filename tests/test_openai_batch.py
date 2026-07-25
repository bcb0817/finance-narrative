import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import openai_batch


class FakeFiles:
    def create(self, **kwargs):
        self.purpose = kwargs["purpose"]
        return SimpleNamespace(id="file-input")

    def content(self, _file_id):
        return SimpleNamespace(content=b'{"custom_id":"one","response":{"status_code":200}}\n')


class FakeBatches:
    def create(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(id="batch-one", status="validating")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, status="completed", output_file_id="file-output")

    def cancel(self, batch_id):
        return SimpleNamespace(id=batch_id, status="cancelling")


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()


class OpenAIBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "OPENAI_BATCH_ENABLED": "true",
            "OPENAI_BATCH_MAX_REQUESTS": "10",
            "OPENAI_BATCH_MAX_SUBMISSIONS_PER_DAY": "2",
            "OPENAI_ANALYSIS_MODEL": "gpt-5.6-terra",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_builds_responses_jsonl(self):
        path = openai_batch.build_request_file([{"custom_id": "one", "input": "分析して"}], operation="test")
        row = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(row["url"], "/v1/responses")
        self.assertEqual(row["body"]["model"], "gpt-5.6-terra")
        self.assertFalse(row["body"]["store"])
        self.assertEqual(openai_batch.validate_request_file(path), 1)

    def test_submit_and_duplicate_guard(self):
        path = openai_batch.build_request_file([{"custom_id": "one", "input": "分析"}], operation="test")
        client = FakeClient()
        result = openai_batch.submit(path, operation="test", client=client)
        self.assertEqual(result["batch_id"], "batch-one")
        self.assertEqual(client.files.purpose, "batch")
        self.assertEqual(client.batches.created["endpoint"], "/v1/responses")
        with self.assertRaises(ValueError):
            openai_batch.submit(path, operation="test", client=client)

    def test_disabled_is_fail_closed(self):
        path = openai_batch.build_request_file([{"custom_id": "one", "input": "分析"}], operation="test")
        with patch.dict(os.environ, {"OPENAI_BATCH_ENABLED": "false"}):
            with self.assertRaises(openai_batch.BatchDisabledError):
                openai_batch.submit(path, operation="test", client=FakeClient())

    def test_collect_writes_result(self):
        result = openai_batch.collect("batch-one", client=FakeClient())
        target = Path(result["result_path"])
        self.assertTrue(target.exists())
        self.assertIn('"custom_id":"one"', target.read_text(encoding="utf-8"))

    def test_duplicate_custom_id_rejected(self):
        with self.assertRaises(ValueError):
            openai_batch.build_request_file([
                {"custom_id": "same", "input": "a"},
                {"custom_id": "same", "input": "b"}], operation="test")


if __name__ == "__main__":
    unittest.main()
