import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import housekeeping


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo_patch = patch.object(housekeeping, "REPO_ROOT", self.root)
        self.repo_patch.start()

    def tearDown(self):
        self.repo_patch.stop()
        self.temp.cleanup()

    def _old_file(self, relative: str, *, days: int = 40) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")
        timestamp = time.time() - days * 86400
        os.utime(path, (timestamp, timestamp))
        return path

    def test_deletes_only_expired_known_generated_files(self):
        old_output = self._old_file("outputs/2026-01-01/chart.png")
        state = self._old_file("data/posted_history.json", days=400)
        fresh_output = self._old_file("outputs/today/chart.png", days=1)

        result = housekeeping.cleanup_generated()

        self.assertEqual(result["status"], "success")
        self.assertFalse(old_output.exists())
        self.assertTrue(state.exists())
        self.assertTrue(fresh_output.exists())

    def test_dry_run_reports_without_deleting(self):
        old_output = self._old_file("outputs/old/report.json")

        result = housekeeping.cleanup_generated(dry_run=True)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["deleted_files"], 1)
        self.assertTrue(old_output.exists())

    def test_removes_bytecode_but_preserves_virtual_environment(self):
        cache = self._old_file("src/common/__pycache__/module.pyc", days=1)
        venv_cache = self._old_file(".venv/lib/__pycache__/module.pyc", days=1)

        housekeeping.cleanup_generated()

        self.assertFalse(cache.exists())
        self.assertTrue(venv_cache.exists())


if __name__ == "__main__":
    unittest.main()
