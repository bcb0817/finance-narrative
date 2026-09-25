import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from common.openai_config import OpenAIRole, model_for


class NoAstraGenerationTests(unittest.TestCase):
    def test_default_is_mini(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(model_for(OpenAIRole.GENERATE), 'gpt-5-mini')

    def test_stale_astra_override_and_snapshot_cannot_generate(self):
        for role, key in ((OpenAIRole.GENERATE, 'OPENAI_GENERATE_MODEL'), (OpenAIRole.FALLBACK, 'OPENAI_FALLBACK_MODEL')):
            for model in ('gpt-6-astra', 'gpt-6-astra-2026-09-01'):
                with self.subTest(role=role, model=model), patch.dict(os.environ, {key: model}):
                    self.assertEqual(model_for(role), 'gpt-5-mini')

    def test_analysis_setting_is_preserved(self):
        with patch.dict(os.environ, {'OPENAI_ANALYSIS_MODEL': 'gpt-6-astra'}):
            self.assertEqual(model_for(OpenAIRole.ANALYZE), 'gpt-6-astra')

if __name__ == '__main__':
    unittest.main()
