import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_finance_bot as bot


class DaemonLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "daemon.lock"
        self.env = patch.dict(os.environ, {"LOCK_FILE": str(self.lock)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_creates_json_lock_atomically(self):
        acquired = bot._acquire_lock()
        self.assertEqual(acquired, self.lock)
        data = json.loads(self.lock.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertIn("started_at", data)

    def test_replaces_stale_legacy_lock(self):
        self.lock.write_text("99999999", encoding="utf-8")
        with patch.object(bot, "_pid_is_running", return_value=False):
            acquired = bot._acquire_lock()
        self.assertEqual(acquired, self.lock)
        self.assertEqual(json.loads(self.lock.read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_refuses_live_lock(self):
        self.lock.write_text(str(os.getpid()), encoding="utf-8")
        acquired = bot._acquire_lock()
        self.assertIsNone(acquired)
        self.assertEqual(self.lock.read_text(encoding="utf-8"), str(os.getpid()))


class ChildOutputEncodingTests(unittest.TestCase):
    def test_decodes_utf8_japanese_without_mojibake(self):
        message = "次回: ニュース取得 正常終了"
        self.assertEqual(bot._decode_child_output(message.encode("utf-8")), message)

    def test_accepts_cp932_from_legacy_external_tool(self):
        message = "取得完了"
        self.assertEqual(bot._decode_child_output(message.encode("cp932")), message)


if __name__ == "__main__":
    unittest.main()
