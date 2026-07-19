import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.api_costs import monthly_openai_cost, record_openai_usage


class ApiCostTests(unittest.TestCase):
    def test_records_model_usage(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}):
            response = SimpleNamespace(usage=SimpleNamespace(
                prompt_tokens=1_000_000, completion_tokens=1_000_000))
            record_openai_usage(response, "gpt-5-mini")
            self.assertAlmostEqual(monthly_openai_cost(), 2.25)
            self.assertTrue((Path(tmp) / "api_cost_ledger.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
