"""Conditional, bounded RSS acquisition; HTTP success is distinct from novelty."""
import json
import os
import random
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import requests
import feedparser
from common import collection as c


def interval(source, now):
    base = source['interval_seconds']
    # Calendar entries must come from independently verified release schedules.
    for event in c.settings()['calendar']:
        known = c.stamp(event.get('known_at'))
        release = c.stamp(event.get('release_at'))
        if (event.get('verified') is True and event.get('calendar_url') and known and known <= now
            and release and event.get('status') == 'scheduled' and source['source_id'] in event.get('source_ids', [])
            and release - timedelta(minutes=30) <= now <= release + timedelta(minutes=60)):
            return max(source['minimum_interval_seconds'], 300)
    return base


def collect_feed(source, *, now=None, get=None):
    now = now or datetime.now(timezone.utc)
    old = c.health(source['source_id'])
    if not source['enabled']:
        return {'status':'disabled'}
    if reason := c.rights_reason(source, source['url'], 'internal'):
        # Missing permission is configuration, not a failing server.
        return {'status':'rights_unreviewed', 'reason':reason, 'source_id':source['source_id']}
    if c.stamp(old.get('next_attempt_at')) and now < c.stamp(old['next_attempt_at']):
        return {'status':'backoff_or_not_due'}
    checked = c.stamp(old.get('checked_at'))
    if checked and now < checked + timedelta(seconds=interval(source, now)):
        return {'status':'not_due'}
    from news_bot.news import _agent_for, feed_excerpt
    agent = _agent_for(source['url'])
    if urlsplit(source['url']).hostname == 'www.sec.gov' and ('example.com' in agent or '@' not in agent):
        return {'status':'sec_contact_user_agent_required'}
    with c.database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT payload FROM health WHERE id=?', (source['source_id'],)).fetchone()
        latest = json.loads(current[0]) if current else {}
        if c.stamp(latest.get('lease_until')) and c.stamp(latest['lease_until']) > now:
            return {'status':'collection_already_running'}
        if latest.get('checked_at') != old.get('checked_at'):
            return {'status':'collection_state_changed'}
        db.execute('INSERT OR REPLACE INTO health VALUES(?,?)', (source['source_id'], json.dumps({**latest,'lease_until':(now+timedelta(minutes=5)).isoformat()})))
    headers = {'User-Agent':agent, 'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5'}
    if old.get('etag'):
        headers['If-None-Match'] = old['etag']
    if old.get('last_modified'):
        headers['If-Modified-Since'] = old['last_modified']
    status = 'fetch_failed'
    http_status, retry_after = None, 0
    try:
        with (get or requests.get)(source['url'], headers=headers, timeout=(5,12), stream=True, allow_redirects=False) as response:
            http_status = response.status_code
            if http_status == 304:
                c.record_fetch(source, now, 'not_modified')
                return {'status':'not_modified', 'new_count':0}
            if http_status == 429:
                value = response.headers.get('Retry-After', '')
                try:
                    retry_after = max(0, int(value))
                except ValueError:
                    dt = c.stamp(value)
                    retry_after = max(0, (dt-now).total_seconds()) if dt else 0
            if 300 <= http_status < 400:
                raise ValueError('redirect_requires_registry_review')
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError('response_too_large')
                chunks.append(chunk)
            payload = b''.join(chunks)
            parsed = feedparser.parse(payload)
            if parsed.bozo or not parsed.get('version'):
                status = 'parse_failed'
                raise ValueError('invalid_feed')
            count = 0
            for entry in parsed.entries:
                if not entry.get('link') or not entry.get('title'):
                    continue
                # Feed text is untrusted data. Never evaluate commands or fetch linked content here.
                result = c.ingest({'url':entry.link, 'title':feed_excerpt(entry.title),
                    'excerpt':feed_excerpt(entry.get('summary') or entry.get('description') or entry.title),
                    'published':entry.get('published') or entry.get('updated'), 'updated':entry.get('updated'),
                    'retrieved_at':now.isoformat(), 'bootstrap':not bool(old.get('last_success_at'))}, source, now=now)
                count += bool(result.get('new_item'))
            status = 'new_items' if count else 'no_new_items' if parsed.entries else 'empty_feed'
            dates = [c.stamp(e.get('updated') or e.get('published')) for e in parsed.entries]
            if dates and all(d and d < now-timedelta(hours=c.settings()['max_age_hours']) for d in dates):
                status = 'stale_content'
            c.record_fetch(source, now, status, raw_count=len(parsed.entries), new_count=count,
                etag=response.headers.get('ETag'), last_modified=response.headers.get('Last-Modified'),
                http_status=http_status, next_attempt_at=None)
            return {'source_id':source['source_id'], 'status':status, 'raw_count':len(parsed.entries), 'new_count':count}
    except Exception as exc:
        if http_status == 429:
            status = 'rate_limited'
        delay = max(retry_after, min(21600, 60 * 2 ** min(old.get('consecutive_failures', 0), 8)) + random.uniform(0,30))
        c.record_fetch(source, now, status, http_status=http_status, error_type=type(exc).__name__,
            next_attempt_at=(now + timedelta(seconds=delay)).isoformat())
        return {'source_id':source['source_id'], 'status':status, 'error_type':type(exc).__name__}


def collect_all(now=None):
    now = now or datetime.now(timezone.utc)
    results = []
    for source in c.registry():
        try:
            results.append(collect_feed(source, now=now))
        except Exception as exc:
            results.append({'source_id':source['source_id'], 'status':'state_error', 'error_type':type(exc).__name__})
    c.prune(now)
    return results


def candidates(limit=10, *, now=None):
    from news_bot.news import NewsItem
    now = now or datetime.now(timezone.utc)
    with c.database() as db:
        rows = db.execute('''SELECT i.payload,c.score FROM items i JOIN candidates c ON i.id=c.item_id
            WHERE c.accepted=1 AND NOT EXISTS(SELECT 1 FROM publications p WHERE p.item_id=i.id)
            ORDER BY c.score DESC,i.first_seen DESC''').fetchall()
    result = []
    for row in rows:
        item = json.loads(row['payload'])
        dt = c.stamp(item['updated_at'] or item['published_at'])
        if not dt or now-dt > timedelta(hours=c.settings()['max_age_hours']) or c.stamp(item['expires_at']) <= now:
            continue
        source = c.source_for(item['source'])
        if c.rights_reason(source, item['canonical_url'], 'public_llm'):
            continue
        result.append(NewsItem(item['title'], item['canonical_url'], item['source'], item['published_at'],
            source_group=item['domain'], excerpt=item['excerpt'], retrieved_at=item['first_seen_at'],
            collection_item_id=item['id'], collection_event_id=item['event_id']))
        if len(result) >= limit:
            break
    return result
