"""Cross-process, fail-closed publication transaction shared by every X writer."""
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from common.runtime import JST, state_dir, post_enabled


def capacity_reason(history, reservations, now, *, breaking=False):
    from common.daily_post_goal import effective_int
    daily = effective_int('DAILY_POST_LIMIT', 10)
    hourly = effective_int('HOURLY_POST_LIMIT', 2)
    normal = max(0, min(6, int(os.getenv('EDITORIAL_NORMAL_DAILY_LIMIT', '6'))))
    gap = max(30, int(os.getenv('EDITORIAL_NORMAL_GAP_MINUTES', '30')))
    rows = list({str(r.get('tweet_id') or i): r for i, r in enumerate(history)}.values()) + reservations
    dated = []
    for row in rows:
        stamp = datetime.fromisoformat(row['posted_at'])
        stamp = stamp.replace(tzinfo=JST) if stamp.tzinfo is None else stamp.astimezone(JST)
        dated.append((row, stamp))
    today = [(r, t) for r, t in dated if t.date() == now.date()]
    if len(today) >= daily:
        return f'daily_total_limit:{daily}'
    if not breaking and sum(not r.get('editorial', {}).get('important_update', False) for r, _ in today) >= normal:
        return f'daily_normal_limit:{normal}'
    if sum(now - timedelta(hours=1) < t <= now for _, t in dated) >= hourly:
        return f'rolling_hour_limit:{hourly}'
    if not breaking and any(now - timedelta(minutes=gap) < t <= now for _, t in dated):
        return f'normal_gap:{gap}'
    return ''


@contextmanager
def process_lock():
    path = state_dir() / 'editorial_publish.lock'
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _journal():
    return state_dir() / 'editorial_publications.jsonl'


def append(row):
    with _journal().open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        handle.flush(); os.fsync(handle.fileno())


def outstanding(history):
    latest = {}
    if _journal().exists():
        for line in _journal().read_text(encoding='utf-8').splitlines():
            row = json.loads(line)  # Corrupt state must block, never reset counts.
            latest[row['reservation']] = row
    ids = {str(r.get('tweet_id')) for r in history}
    return [r for r in latest.values() if r.get('tweet_id') not in ids]


def publish(text, image_path=None):
    from common.editorial import PERMIT, digest, history, budget_ok, audit
    from common.safety import safety_check
    from common.x_client import get_tweepy_client, get_tweepy_api_v1
    from common.post_registry import record_post
    packet = PERMIT.get()
    if not post_enabled():
        return ''
    if not packet or packet['body_hash'] != digest(text):
        audit(decision='skip', reason='editorial_permit_missing_or_text_changed')
        return ''
    try:
        with process_lock():
            now = datetime.now(JST)
            if now - datetime.fromisoformat(packet['approved_at']) > timedelta(minutes=15):
                raise ValueError('editorial_permit_expired')
            if not budget_ok():
                raise ValueError('openai_budget_stop')
            safety_check(text)
            if 'http://' in text or 'https://' in text:
                raise ValueError('url_not_allowed')
            if image_path:
                import hashlib
                if hashlib.sha256(Path(image_path).read_bytes()).hexdigest() != packet.get('image_hash'):
                    raise ValueError('unreviewed_image')
            rows = history()
            pending = outstanding(rows)
            reason = capacity_reason(rows, pending, now, breaking=packet['important_update'])
            if reason:
                raise ValueError(reason)
            if any(r.get('editorial', {}).get('fact_id') == packet['fact_id'] for r in rows + pending):
                raise ValueError('same_fact')
            if packet.get('history_fingerprint') != digest(json.dumps(rows, ensure_ascii=False, sort_keys=True)):
                raise ValueError('editorial_context_changed_reassess_next_run')
            # Keep existing X monetary cap; no adaptive budget expansion.
            month_rows = [r for r in rows + pending if r.get('posted_at', '').startswith(now.strftime('%Y-%m'))]
            from common.daily_post_goal import effective_float
            if (len(month_rows) + 1) * float(os.getenv('X_CONTENT_CREATE_USD', '.015')) > effective_float('X_WRITE_MONTHLY_BUDGET_USD', 15):
                raise ValueError('monthly_x_write_budget')
            row = {'reservation': uuid.uuid4().hex, 'posted_at': now.isoformat(), 'text': text,
                   'editorial': packet, 'status': 'pending'}
            append(row)
            # Timeout/unknown outcome retains reservation. Never retry blindly.
            media_ids = None
            if image_path:
                media = get_tweepy_api_v1().media_upload(filename=image_path)
                media_ids = [media.media_id]
            kwargs = {'text': text}
            if media_ids:
                kwargs['media_ids'] = media_ids
            response = get_tweepy_client().create_tweet(**kwargs)
            tid = str(response.data['id'])
            append({**row, 'tweet_id': tid, 'status': 'posted'})
            record_post(tid, text=text, bot=packet['bot'], mode='editorial',
                        source=packet['sources'][0]['source'], url=packet['sources'][0]['url'],
                        extra={'editorial': packet, 'has_media': bool(image_path), 'with_image': bool(image_path),
                               'image_type': packet['brief'].get('image_kind', 'none') if image_path else None,
                               'post_type': 'breaking_news' if packet['important_update'] else 'explanation'}, notify_discord=False)
            return tid
    except Exception as exc:
        audit(decision_id=packet.get('decision_id'), decision='blocked_or_unknown', reason=type(exc).__name__, detail=str(exc) if isinstance(exc, ValueError) else 'see publication journal')
        return ''
