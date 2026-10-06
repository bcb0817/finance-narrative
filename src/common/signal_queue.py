"""Daily ETF snapshots are internal observations, not seven automatic X posts."""
import hashlib
import json
import sqlite3
from datetime import datetime
from common.runtime import state_dir, JST
from common.distribution_quality import input_reason


def _db():
    db = sqlite3.connect(state_dir() / 'signal_editorial_queue.sqlite', timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, payload TEXT, expires_at TEXT, status TEXT)')
    return db


def observe(snapshot, now, *, holiday=False, preview=False):
    from common.editorial import audit, history
    recent = history()
    decisions = []
    for asset in snapshot.get('assets', []):
        direction = {'LONG': '買い', 'SHORT': '売り', 'CASH': '中立', 'NEUTRAL': '中立'}.get(asset.get('decision'), '保留')
        event = asset.get('editorialEvent') or {}
        evidence = asset.get('evidence') or []
        reason = ''
        if holiday:
            reason = 'no_new_session'
        elif asset.get('excluded') or asset.get('dataStatus') != 'VALID':
            reason = 'invalid_or_excluded'
        else:
            try:
                if datetime.fromisoformat(asset.get('expiresAt') or '') <= now:
                    reason = 'expired'
            except (ValueError, TypeError):
                reason = 'expiry_missing'
        if not reason:
            reason = input_reason(event, asset['symbol'], direction, evidence, recent, now)
        payload = {'event': {**event, 'signal': True}, 'ticker': asset['symbol'], 'direction': direction,
                   'evidence': evidence, 'expires_at': asset.get('expiresAt'), 'snapshot_at': snapshot.get('generatedAt')}
        # No snapshot timestamp in the key: identical daily signals are not new facts.
        stable_evidence = [{k: v for k, v in source.items() if k != 'retrieved_at'} for source in evidence]
        key = hashlib.sha256(json.dumps([event, asset['symbol'], direction, stable_evidence], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        row = {'symbol': asset['symbol'], 'direction': direction, 'skip_reason': reason,
               'status': 'internal_only' if reason else 'queued', 'queue_id': key}
        if not preview:
            audit(bot='etf-signal', decision=row['status'], reason=reason or 'evidenced_event',
                  ticker=asset['symbol'], direction=direction, evidence=evidence, event=event)
            if not reason:
                with _db() as db:
                    db.execute('INSERT OR IGNORE INTO candidates VALUES (?,?,?,?)',
                               (key, json.dumps(payload, ensure_ascii=False), asset['expiresAt'], 'pending'))
                db.close()
        decisions.append(row)
    return decisions


def pending(now=None):
    now = now or datetime.now(JST)
    path = state_dir() / 'signal_editorial_queue.sqlite'
    if not path.exists():
        return []
    from common.editorial import history
    seen = {r.get('editorial', {}).get('signal_queue_id') for r in history()}
    with _db() as db:
        result = []
        for key, payload, expires in db.execute("SELECT id,payload,expires_at FROM candidates WHERE status='pending'"):
            if key in seen:
                db.execute("UPDATE candidates SET status='posted' WHERE id=?", (key,))
            elif datetime.fromisoformat(expires) <= now:
                db.execute("UPDATE candidates SET status='expired' WHERE id=?", (key,))
            else:
                result.append({**json.loads(payload), 'signal_queue_id': key})
    db.close()
    return result[:5]
