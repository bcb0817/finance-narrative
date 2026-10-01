import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.api_costs import ensure_openai_budget, monthly_openai_cost, openai_budget_limit, record_openai_usage


class ApiCostTests(unittest.TestCase):
    def test_records_model_usage(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}):
            response = SimpleNamespace(usage=SimpleNamespace(
                prompt_tokens=1_000_000, completion_tokens=1_000_000))
            record_openai_usage(response, "gpt-5-mini")
            self.assertAlmostEqual(monthly_openai_cost(), 2.25)
            self.assertTrue((Path(tmp) / "api_cost_ledger.jsonl").exists())

    def test_budget_reserves_requested_output_before_call(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "STATE_DIR": tmp,
            "OPENAI_MONTHLY_BUDGET_USD": "5.0",
        }):
            Path(tmp, "api_cost_ledger.jsonl").write_text(
                '{"ts":"2026-09-09T00:00:00+09:00","estimated_usd":4.9}\n',
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                ensure_openai_budget(
                    model="gpt-6-astra", prompt="x" * 4_000, max_output_tokens=4_000,
                )

    def test_reservation_blocks_parallel_preflight_until_settled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "STATE_DIR": tmp, "OPENAI_MONTHLY_BUDGET_USD": "0.10",
        }):
            reservation = ensure_openai_budget(
                model="gpt-6-astra", prompt="x" * 4_000, max_output_tokens=1_000,
            )
            self.assertTrue(reservation)
            with self.assertRaises(RuntimeError):
                ensure_openai_budget(
                    model="gpt-6-astra", prompt="x" * 4_000, max_output_tokens=1_000,
                )

    def test_usage_settles_reservation_instead_of_double_counting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "STATE_DIR": tmp, "OPENAI_MONTHLY_BUDGET_USD": "5.0",
        }):
            reservation = ensure_openai_budget(model="gpt-6-astra", prompt="x" * 400, max_output_tokens=1_000)
            response = SimpleNamespace(usage=SimpleNamespace(input_tokens=400, output_tokens=100))
            record_openai_usage(response, "gpt-6-astra", reservation_id=reservation)
            self.assertAlmostEqual(monthly_openai_cost(), 0.009)

    def test_excludes_other_provider_costs_from_openai_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}):
            Path(tmp, "api_cost_ledger.jsonl").write_text(
                '{"ts":"2026-09-09T00:00:00+09:00","kind":"usage","provider":"openai","estimated_usd":1.25}\n'
                '{"ts":"2026-09-09T00:00:00+09:00","kind":"usage","provider":"xai","estimated_usd":9.75}\n',
                encoding="utf-8",
            )
            self.assertAlmostEqual(monthly_openai_cost(), 1.25)

    def test_approval_file_caps_stale_larger_runtime_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "STATE_DIR": tmp, "OPENAI_MONTHLY_BUDGET_USD": "50.0",
        }):
            self.assertEqual(openai_budget_limit(), 5.0)


if __name__ == "__main__":
    unittest.main()
