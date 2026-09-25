import json
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from common.budget_watchdog import inspect
from common.runtime import JST


class BudgetWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.values = {'OPENAI_MONTHLY_BUDGET_USD': 50, 'XAI_MONTHLY_BUDGET_USD': 20, 'X_WRITE_MONTHLY_BUDGET_USD': 15}
        (self.root / 'config/approved_budgets.json').write_text(json.dumps(self.values))
        self.env = {key: str(value) for key, value in self.values.items()}
        self.env['POST_ENABLED'] = 'true'
        self.write_env()
        self.now = datetime.now(JST)
        self.history = [{'posted_at': self.now.isoformat()}]

    def write_env(self):
        (self.root / '.env').write_text('\n'.join(f'{k}={v}' for k, v in self.env.items()) + '\nAPI_KEY=secret-not-for-logs')

    def inspect(self, spent=17):
        return inspect(self.root, self.env, spent, self.history, self.now)

    def test_healthy(self):
        self.assertEqual(self.inspect()['issues'], [])

    def test_disk_drift_and_no_secret(self):
        self.env['OPENAI_MONTHLY_BUDGET_USD'] = '5'
        self.write_env()
        self.env['OPENAI_MONTHLY_BUDGET_USD'] = '50'
        result = self.inspect()
        self.assertEqual(len(result['issues']), 1)
        self.assertNotIn('secret-not-for-logs', json.dumps(result))
        self.assertIn('=5', (self.root / '.env').read_text())

    def test_runtime_drift(self):
        self.env['OPENAI_MONTHLY_BUDGET_USD'] = '5'
        self.assertEqual(len(self.inspect()['issues']), 2)

    def test_exhausted(self):
        self.assertIn('予算停止', self.inspect(50)['issues'][0])

    def test_inactive(self):
        self.history = [{'posted_at': (self.now - timedelta(hours=25)).isoformat()}]
        self.assertIn('24時間', self.inspect()['issues'][0])

    def test_invalid_budget(self):
        self.env['OPENAI_MONTHLY_BUDGET_USD'] = 'nan'
        self.assertEqual(len(self.inspect()['issues']), 2)

    def test_notification_dedup_retry_and_recovery(self):
        from common.budget_watchdog import check
        import requests
        (self.root / 'posted_history.json').write_text(json.dumps(self.history))
        session = Mock()
        self.env['OPENAI_MONTHLY_BUDGET_USD'] = '5'
        self.env['DISCORD_ALERTS_ENABLED'] = 'true'
        with patch('common.budget_watchdog.state_dir', return_value=self.root), patch('common.budget_watchdog.REPO_ROOT', self.root), patch.dict('os.environ', self.env), patch('common.api_costs.monthly_openai_cost', return_value=17), patch('common.operations_alerts._discord_webhook_url', return_value='https://example.invalid'):
            session.post.side_effect = requests.RequestException('failed')
            self.assertEqual(check(now=self.now, session=session)['delivery'], 'delivery_failed')
            session.post.side_effect = None
            self.assertEqual(check(now=self.now+timedelta(minutes=5), session=session)['delivery'], 'sent')
            self.assertEqual(check(now=self.now+timedelta(minutes=10), session=session)['delivery'], 'unchanged')
            self.assertEqual(session.post.call_count, 2)
            with patch.dict('os.environ', {'OPENAI_MONTHLY_BUDGET_USD': '50'}):
                self.assertEqual(check(now=self.now+timedelta(minutes=15), session=session)['delivery'], 'sent')


if __name__ == '__main__':
    unittest.main()
