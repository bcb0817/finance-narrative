import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('etf_daily', Path(__file__).parents[1] / 'scripts/etf_daily.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)
ASSETS = [
    {'symbol': symbol, 'proxy': proxy, 'excluded': symbol == '1570',
     'dataStatus': 'VALID', 'targetWeight': weight, 'decision': 'LONG',
     'expiresAt': '2026-10-05T13:30:00+00:00', 'decisionSession': '2026-10-02'}
    for symbol, proxy, weight in [('TQQQ', 'QQQ', .232), ('SOXL', 'SOXX', .096), ('1570', '1321.T', None)]
]


def test_post_text_preserves_direction_and_cannot_be_a_personal_order():
    now = datetime(2026, 10, 5, 4, tzinfo=timezone.utc)
    for asset in ASSETS:
        text = job.render(asset, now, True)
        assert sum(1 if ord(c) < 128 else 2 for c in text) <= 280
        assert '基準日：' in text and '優位性未確認' in text
        assert '個別注文は判断しません' in text
        assert '新しい確定足なし' in text
    assert 'Buy方向' in job.render(ASSETS[0], now)
    assert '期限切れ' in job.render(ASSETS[0], now + timedelta(days=1))
    assert 'Buy方向' not in job.render({**ASSETS[0], 'dataStatus': 'STALE'}, now)
    assert '対象外' in job.render(ASSETS[2], now)


@pytest.mark.parametrize('scenario', ['success', 'ambiguous', 'policy_block', 'wrong_account'])
def test_one_post_per_symbol_and_day_even_after_ambiguous_response(tmp_path, monkeypatch, scenario):
    ambiguous = scenario == 'ambiguous'
    (tmp_path / 'config').mkdir()
    cfg = {'enabled': True, 'account_id': 'test-id', 'account_username': 'test-user', 'lab_root': str(tmp_path)}
    (tmp_path / 'config/etf_daily.json').write_text(json.dumps(cfg), encoding='utf-8')
    now = datetime.now(timezone.utc)
    summary = tmp_path / 'summary.json'
    summary.write_text(json.dumps({'calculated_at': now.isoformat()}), encoding='utf-8')
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, 'load_env', lambda: None)
    monkeypatch.setattr(job, 'post_enabled', lambda: True)
    monkeypatch.setattr(job, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(job, 'publish', lambda *_: None)
    monkeypatch.setattr(job, 'live_snapshot', lambda *_: {'assets': ASSETS, 'generatedAt': now.isoformat()})
    slots = {g: {'due': '2000-01-01T00:00:00+00:00', 'holiday': False} for g in ('US', 'JP')}
    monkeypatch.setattr(job, 'command', lambda args, *_: json.dumps(slots) if '--slots' in args else str(summary))
    for key in ('API_KEY', 'API_KEY_SECRET', 'ACCESS_TOKEN', 'ACCESS_TOKEN_SECRET'):
        monkeypatch.setenv(key, 'test-only')
    calls = []
    class Client:
        def get_me(self, **_):
            return SimpleNamespace(data=SimpleNamespace(id='wrong-id' if scenario == 'wrong_account' else 'test-id', username='test-user'))
        def create_tweet(self, **kwargs):
            calls.append(kwargs['text'])
            if ambiguous and len(calls) == 1:
                raise TimeoutError('simulated')
            return SimpleNamespace(data={'id': str(len(calls))})
    monkeypatch.setattr('common.x_client.get_tweepy_client', lambda: Client())
    monkeypatch.setattr('common.posting_policy.check_post', lambda _: SimpleNamespace(allowed=scenario != 'policy_block', reason='hourly_limit:2/2'))
    monkeypatch.setattr('common.post_registry.record_post', lambda *_, **__: None)
    if scenario == 'wrong_account':
        with pytest.raises(RuntimeError, match='WRONG_X_ACCOUNT'):
            job.run()
        assert not calls
        return
    first, second = job.run(), job.run()
    if scenario == 'policy_block':
        assert not calls and not first['posts'] and not second['posts']
        assert all(p['reason'] == 'hourly_limit:2/2' for p in second['pending'])
        return
    assert len(calls) == 3
    assert len(first['posts']) == len(second['posts']) == 3
    assert sum(p['status'] == 'uncertain' for p in second['posts']) == int(ambiguous)
