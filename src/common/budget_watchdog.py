"""Read-only budget drift monitor. Never copy or log secrets from .env."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path

import requests

from common.runtime import JST, REPO_ROOT, state_dir


def inspect(root: Path, environment, spent: float, history: list, now: datetime) -> dict:
    approved = json.loads((root / 'config/approved_budgets.json').read_text(encoding='utf-8'))
    keys = ('OPENAI_MONTHLY_BUDGET_USD', 'XAI_MONTHLY_BUDGET_USD', 'X_WRITE_MONTHLY_BUDGET_USD')
    disk = {}
    for line in (root / '.env').read_text(encoding='utf-8').splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key.strip() in keys:
            disk[key.strip()] = value.strip().strip('\"\'')
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) and result > 0 else None
        except (ValueError, TypeError):
            return None
    observed, issues = {}, []
    for key in keys:
        target = number(approved[key])
        if target is None:
            raise ValueError('invalid approved budget')
        actual = {'approved': target, 'file': number(disk.get(key)), 'runtime': number(environment.get(key))}
        observed[key] = actual
        if actual['file'] != target or actual['runtime'] != target:
            issues.append(f"{key}: 承認値={target}, .env={actual['file']}, 実行値={actual['runtime']}")
    limit = observed['OPENAI_MONTHLY_BUDGET_USD']['runtime']
    if limit is None or spent >= limit:
        issues.append(f'OpenAI予算停止: 利用額=${spent:.2f}, 上限={limit}')
    times = []
    for row in history:
        try:
            stamp = datetime.fromisoformat(row['posted_at'])
            times.append(stamp.replace(tzinfo=JST) if stamp.tzinfo is None else stamp)
        except (ValueError, KeyError, TypeError):
            continue
    if environment.get('POST_ENABLED', '').lower() in ('true', '1', 'yes'):
        if not times or (now - max(times)).total_seconds() >= 86400:
            issues.append('24時間以上、投稿成功の記録がありません')
    return {'budgets': observed, 'issues': issues}


def check(*, now=None, session=requests) -> dict:
    from common.api_costs import monthly_openai_cost
    from common.operations_alerts import _atomic_write, _discord_webhook_url
    now = now or datetime.now(JST)
    path = state_dir() / 'budget_watchdog.json'
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    try:
        if (now - datetime.fromisoformat(previous['checked_at'])).total_seconds() < 300:
            return {'status': 'not_due'}
    except (KeyError, ValueError, TypeError):
        pass
    try:
        history = json.loads((state_dir() / 'posted_history.json').read_text(encoding='utf-8'))
        result = inspect(REPO_ROOT, os.environ, monthly_openai_cost(), history, now)
    except (OSError, ValueError, TypeError, KeyError):
        result = {'budgets': {}, 'issues': ['予算監視設定または投稿履歴を読み取れません']}
    if previous.get('budgets') != result['budgets']:
        with (state_dir() / 'budget_changes.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': now.isoformat(), 'before': previous.get('budgets'), 'after': result['budgets']}) + '\n')
    # Fingerprint stable categories/values, not the changing spend amount.
    fingerprint = json.dumps([result['budgets'], [s.split(':')[0] for s in result['issues']]], sort_keys=True)
    notified = previous.get('notified')
    delivery = 'unchanged'
    if fingerprint != notified and (result['issues'] or previous.get('issues') or notified is not None):
        url = _discord_webhook_url()
        delivery = 'disabled_or_unconfigured'
        if url and os.getenv('DISCORD_ALERTS_ENABLED', '').lower() in ('true', '1', 'yes', 'on'):
            message = 'finance-narrative 運用監視\n' + ('\n'.join(result['issues']) or '予算・投稿監視の異常が解消しました')
            try:
                response = session.post(url, json={'content': message[:1900], 'allowed_mentions': {'parse': []}}, timeout=10)
                response.raise_for_status()
                notified, delivery = fingerprint, 'sent'
            except requests.RequestException:
                delivery = 'delivery_failed'
    elif not result['issues']:
        notified = fingerprint
    state = {**result, 'checked_at': now.isoformat(), 'notified': notified, 'delivery': delivery}
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))
    return state
