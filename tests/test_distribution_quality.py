import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from common import distribution_quality as q
from common import editorial as ed
from common.runtime import JST

NOW = datetime(2026, 10, 6, 20, tzinfo=JST)
BODY = 'SECが新規則を公表。\n企業の開示義務を対象にした規則で、適用範囲と実施時期の確認が必要になる。\n今の資料だけで個々の企業への影響を決めつけず、条文に沿って対象を分けて読む。 出典：SEC'
SOURCE = {'source': 'SEC', 'url': 'https://www.sec.gov/rule', 'excerpt': BODY,
          'period': '2026-10-06', 'retrieved_at': '2026-10-06T12:00:00+09:00',
          'public_allowed': True, 'is_primary': True, 'provider': 'official'}
EVENT = {'kind': 'regulation', 'fact': '新規則公表', 'is_new_fact': True, 'signal': True}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k,v in {'STATE_DIR': str(tmp_path), 'OUTPUT_DIR': str(tmp_path), 'POST_ENABLED': 'true',
                'EDITORIAL_POLICY_ENABLED': 'true', 'SINGA_QUALITY_POLICY_ENABLED': 'true',
                'OPENAI_MONTHLY_BUDGET_USD': '5', 'RECOVERY_FOLLOWER_REFERENCE': '110'}.items():
        monkeypatch.setenv(k,v)
    monkeypatch.setattr('socket.socket.connect', lambda *a: pytest.fail('real network forbidden'))
    monkeypatch.setattr('common.openai_client.review_tweet_with_openai', lambda *a: {'ok_to_post': True})


def row(hours=4, **extra):
    return {'tweet_id': str(hours), 'posted_at': (NOW-timedelta(hours=hours)).isoformat(), 'text': '別の事実', **extra}


def service(**brief_override):
    brief = dict(accept=True, new_fact='新規則', change='規則公表', reader_value='適用範囲の確認',
        angle='規則の範囲', difference='初報', interpretation='', limits='個別影響未確認',
        skip_reason='', event_id='sec-rule', fact_id='sec-rule-new', evidence_quote=SOURCE['excerpt'],
        source_index=0, important_update=False, duplicate=False, image_kind='none', image_value='',
        event_type='regulation', ticker='FAS', direction='中立')
    checked = dict(supported=True, unchanged=True, safe=True, reason='', formulaic=False,
                   style_reason='', judgment_present=True)
    model = Mock()
    model.structured.side_effect = [{**brief, **brief_override}, checked]
    model.text.return_value = BODY
    return model


def test_first_seven_days_two_then_four_and_preview_does_not_start(tmp_path):
    assert q.day_limit(NOW) == 2
    assert not (tmp_path/'distribution_recovery.json').exists()
    q.start(NOW)
    assert q.day_limit(NOW+timedelta(days=6, hours=23)) == 2
    assert q.day_limit(NOW+timedelta(days=7)) == 4
    q.start(NOW+timedelta(days=8))
    assert q.state()['started_at'] == NOW.isoformat()


def test_all_caps_gap_ticker_and_images():
    assert q.capacity([row(4),row(8)], NOW) == 'recovery_daily_limit'
    assert q.capacity([row(2)], NOW) == 'recovery_gap_180_minutes'
    assert q.capacity([row(3)], NOW) == ''
    assert q.capacity([row(4,symbol='FAZ')], NOW,ticker='FAZ') == 'ticker_already_posted_today'
    assert q.capacity([],NOW,image=True) == 'image_share_over_half'
    assert q.capacity([row(4)],NOW,image=True) == ''


@pytest.mark.parametrize('direction', ['保留','UNAVAILABLE','判定保留',''])
def test_hold_never_calls_generator(direction):
    model=service()
    result=ed.generate_post(EVENT,'FAS',direction,[SOURCE],[],service=model)
    assert result['skip_reason']=='direction_on_hold'
    model.text.assert_not_called(); model.structured.assert_not_called()


def test_empty_evidence_no_event_or_no_new_fact():
    assert q.input_reason(EVENT,'FAS','中立',[],[],NOW)=='evidence_empty'
    assert q.input_reason({},'FAS','中立',[SOURCE],[],NOW)=='single_supported_event_required'
    assert q.input_reason({**EVENT,'is_new_fact':False},'FAS','中立',[SOURCE],[],NOW)=='no_new_fact'


def test_full_contract_passes_safety_and_returns_no_x_call(monkeypatch):
    x=Mock(); monkeypatch.setattr('common.x_client.get_tweepy_client',x)
    result=ed.generate_post(EVENT,'FAS','中立',[SOURCE],[],service=service())
    assert result.get('post_text')==BODY
    x.assert_not_called()


def test_direction_cannot_be_changed_to_buy():
    assert 'skip_reason' in ed.generate_post(EVENT,'FAS','中立',[SOURCE],[],service=service(direction='買い'))


def test_quality_gates():
    assert q.text_reason(BODY,[],judgment_present=True,sources=[SOURCE])==''
    assert q.text_reason(BODY,[],judgment_present=False,sources=[SOURCE])=='grounded_judgment_missing'
    assert q.text_reason('結論：'+BODY,[],judgment_present=True,sources=[SOURCE])=='label_or_hold_template'
    assert q.text_reason('判定保留',[],judgment_present=True,sources=[SOURCE])=='label_or_hold_template'
    assert q.text_reason(BODY,[{'text':BODY}],judgment_present=True,sources=[SOURCE])=='recent_similarity_80_percent'


def test_same_skeleton_different_ticker_is_rejected():
    a='FAZの確認項目は変わらない。新しい価格も確認できない。次の資料を待つ。'
    b=a.replace('FAZ','SOXS')
    assert q.similar(a,[{'text':b}])>=.8


def test_secondary_numbers_are_not_published():
    body=BODY.replace('新規則','2規則')
    assert q.text_reason(body,[],judgment_present=True,sources=[{**SOURCE,'excerpt':body,'is_primary':False}])=='number_without_primary_evidence'


def test_rejected_template_is_not_rewritten():
    model=service(); model.text.return_value='結論：'+BODY
    assert 'skip_reason' in ed.generate_post(EVENT,'FAS','中立',[SOURCE],[],service=model)
    assert model.text.call_count==1


def test_seven_day_review_halves_only_signal_quota(tmp_path):
    start=NOW-timedelta(days=8)
    q.start(start)
    (tmp_path/'metrics_snapshots.jsonl').write_text(json.dumps({'tweet_id':'1','stage':'24h','status':'collected','impressions':6,'likes':0}),encoding='utf-8')
    report=q.recovery_review([{'tweet_id':'1','posted_at':(NOW-timedelta(days=2)).isoformat(), 'editorial':{'brief':{'event_type':'price_move'}}}],NOW)
    assert report['signal_daily_limit']==1
    assert report['n']==1 and report['median_24h_impressions']==6
    assert report['automatic_budget_changes'] is False
    assert report['retained_event_types']==[]


def test_missing_metrics_are_not_zero():
    q.start(NOW-timedelta(days=8))
    result=q.recovery_review([],NOW)
    assert result['median_24h_impressions'] is None
    assert result['signal_daily_limit']==2


def test_daily_snapshot_logs_hold_and_does_not_queue(tmp_path):
    from common.signal_queue import observe,pending
    asset={'symbol':'FAZ','decision':'UNAVAILABLE','dataStatus':'STALE','expiresAt':(NOW+timedelta(days=1)).isoformat()}
    rows=observe({'assets':[asset]},NOW)
    assert rows[0]['status']=='internal_only'
    assert pending(NOW)==[]


def test_queue_is_idempotent_and_expires(tmp_path):
    from common.signal_queue import observe,pending
    asset={'symbol':'FAS','decision':'CASH','dataStatus':'VALID','expiresAt':(NOW+timedelta(days=1)).isoformat(), 'editorialEvent':EVENT,'evidence':[SOURCE]}
    observe({'assets':[asset]},NOW);observe({'assets':[asset]},NOW)
    assert len(pending(NOW))==1
    assert pending(NOW)[0]['direction']=='中立'  # CASH is not a sell order.
    assert pending(NOW+timedelta(days=2))==[]


def test_switch_restores_previous_editorial_policy(monkeypatch):
    monkeypatch.setenv('SINGA_QUALITY_POLICY_ENABLED','false')
    assert not q.enabled()


def test_shared_caps_include_reservations_and_breaking():
    from common.editorial_delivery import capacity_reason
    assert capacity_reason([row(4)], [row(8)], NOW, breaking=True) == 'recovery_daily_limit'
    assert capacity_reason([], [row(1)], NOW, breaking=True) == 'recovery_gap_180_minutes'


def test_expiry_is_rechecked_before_network(monkeypatch):
    from common.editorial_delivery import publish
    result = ed.generate_post(EVENT, 'FAS', '中立', [SOURCE], [], service=service())
    packet = result['packet']
    packet['signal_expires_at'] = '2000-01-01T00:00:00+09:00'
    client = Mock()
    monkeypatch.setattr('common.x_client.get_tweepy_client', client)
    token = ed.PERMIT.set(packet)
    try:
        assert publish(packet['text']) == ''
    finally:
        ed.PERMIT.reset(token)
    client.assert_not_called()


def test_new_daily_batch_never_constructs_x_client(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('daily_recovery_test', Path(__file__).parents[1] / 'scripts/etf_daily.py')
    job = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/etf_daily.json').write_text(json.dumps({'enabled': True, 'lab_root': str(tmp_path)}), encoding='utf-8')
    summary = tmp_path / 'summary.json'
    summary.write_text(json.dumps({'calculated_at': NOW.isoformat()}), encoding='utf-8')
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, 'load_env', lambda: None)
    monkeypatch.setattr(job, 'post_enabled', lambda: True)
    monkeypatch.setattr(job, 'publish', lambda *_: None)
    assets = [{'symbol': symbol, 'decision': 'UNAVAILABLE', 'dataStatus': 'STALE'} for symbol in ['TQQQ','SOXL','1570','FAS','SQQQ','SOXS','FAZ']]
    monkeypatch.setattr(job, 'live_snapshot', lambda *_: {'assets': assets, 'generatedAt': NOW.isoformat()})
    slots = {g: {'due': '2000-01-01T00:00:00+00:00', 'holiday': False} for g in ('US','JP')}
    monkeypatch.setattr(job, 'command', lambda args, *_: json.dumps(slots) if '--slots' in args else str(summary))
    client = Mock(side_effect=AssertionError('X client must not be created'))
    monkeypatch.setattr('common.x_client.get_tweepy_client', client)
    result = job.run()
    assert result['posts'] == []
    client.assert_not_called()
