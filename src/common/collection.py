"""Evidence collection ledger. No model calls, credentials, X writes or rights inference."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from common.runtime import state_dir

UTC = timezone.utc


def enabled():
    return os.getenv('COLLECTION_QUALITY_ENABLED', 'true').lower() in ('true', '1', 'yes')


def market_internal_reason(dataset='market_data', now=None):
    if not enabled():
        return ''
    rights = settings().get('market_internal_license', {})
    until = stamp(rights.get('valid_until'))
    if (rights.get('approved') is not True or not rights.get('contract_reference')
        or dataset not in rights.get('datasets', []) or not isinstance(rights.get('retention_days'), int)
        or rights['retention_days'] <= 0 or not until or until <= (now or datetime.now(UTC))):
        return 'twelvedata_internal_contract_unverified'
    return ''


def settings():
    path = Path(os.getenv('COLLECTION_POLICY_PATH') or Path(__file__).resolve().parents[2] / 'config/collection_policy.json')
    return json.loads(path.read_text(encoding='utf-8'))


def stamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return dt.astimezone(UTC) if dt.tzinfo else None


def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('invalid_public_url')
    params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', urlencode(sorted(params)), ''))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def registry(feeds=None):
    if feeds is None:
        from news_bot.news import RSS_FEEDS
        feeds = RSS_FEEDS
    cfg = settings()
    result = []
    for name, feed in {**feeds, **cfg['additional_feeds']}.items():
        extra = cfg['source_overrides'].get(name, {})
        rights = cfg['rights'].get(extra.get('policy'), {})
        result.append({'source_id': digest(feed['url'])[:20], 'name': name,
            'operator': extra.get('operator', 'unverified'), 'url': feed['url'], 'method': 'rss_atom',
            'domains': [feed['group']], 'region': extra.get('region', 'unverified'),
            'language': extra.get('language', 'unverified'), 'primary': extra.get('primary', False),
            'interval_seconds': extra.get('interval_seconds', 1800), 'minimum_interval_seconds': 300,
            'terms_basis': rights.get('basis'), 'last_verified_on': rights.get('checked_on'),
            'rights': rights, 'enabled': feed.get('enabled', True)})
    return result


def source_for(name):
    return next((s for s in registry() if s['name'] == name), None)


def rights_reason(source, url, action):
    if not source:
        return 'unknown_source'
    rights = source.get('rights', {})
    if not rights.get('basis') or not rights.get('checked_on') or rights.get(action) is not True:
        return 'rights_unknown_or_denied:' + action
    if urlsplit(url).hostname not in rights.get('hosts', []):
        return 'origin_outside_rights_scope'
    return ''


@contextmanager
def database():
    db = sqlite3.connect(state_dir() / 'collection.sqlite', timeout=10)
    db.row_factory = sqlite3.Row
    # Default rollback journal avoids journal-mode changes racing at first startup.
    db.execute('PRAGMA busy_timeout=10000')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS health (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS fetches (id INTEGER PRIMARY KEY, source_id TEXT, at TEXT, status TEXT, raw_count INTEGER, new_count INTEGER, detail TEXT);
      CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, source_id TEXT, event_id TEXT, first_seen TEXT, published_at TEXT, content_hash TEXT, payload TEXT);
      CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT);
      CREATE TABLE IF NOT EXISTS candidates (item_id TEXT PRIMARY KEY, event_id TEXT, at TEXT, accepted INTEGER, reason TEXT, score REAL, payload TEXT);
      CREATE TABLE IF NOT EXISTS publications (tweet_id TEXT PRIMARY KEY, event_id TEXT, item_id TEXT);
      CREATE TABLE IF NOT EXISTS ingest_decisions (at TEXT, item_id TEXT, relation TEXT, accepted INTEGER, reason TEXT);
      CREATE TABLE IF NOT EXISTS gates (at TEXT, stage TEXT, reason TEXT);
    ''')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def health(source_id):
    with database() as db:
        row = db.execute('SELECT payload FROM health WHERE id=?', (source_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def record_fetch(source, now, status, *, raw_count=0, new_count=0, **detail):
    success = status in ('new_items', 'no_new_items', 'empty_feed', 'not_modified', 'stale_content')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM health WHERE id=?', (source['source_id'],)).fetchone()
        old = json.loads(row[0]) if row else {}
        value = {**old, **detail, 'status': status, 'checked_at': now.isoformat(),
            'lease_until': None,
            'last_success_at': now.isoformat() if success else old.get('last_success_at'),
            'consecutive_failures': 0 if success else old.get('consecutive_failures', 0) + 1}
        db.execute('INSERT OR REPLACE INTO sources VALUES (?,?)', (source['source_id'], json.dumps(source)))
        db.execute('INSERT OR REPLACE INTO health VALUES (?,?)', (source['source_id'], json.dumps(value)))
        db.execute('INSERT INTO fetches(source_id,at,status,raw_count,new_count,detail) VALUES(?,?,?,?,?,?)',
                   (source['source_id'], now.isoformat(), status, raw_count, new_count, json.dumps(detail)))


def claim_reason(claim, item, now):
    quote = claim.get('quote', '')
    evidence = item.get('excerpt', '')
    if not quote or quote not in evidence or claim.get('text') != quote:
        return 'claim_not_exactly_grounded'
    if claim.get('status') != 'confirmed' or not item.get('primary'):
        return 'unverified_claim'
    if canonical(claim.get('source_url', '')) != item['canonical_url']:
        return 'claim_source_mismatch'
    if re.search(r'\d', quote):
        if not claim.get('quantities'):
            return 'numeric_context_missing'
        for number in claim['quantities']:
            if not all(number.get(k) for k in ('value', 'unit', 'period', 'comparison', 'role')):
                return 'numeric_context_missing'
            if number['role'] not in ('actual', 'previous', 'revised', 'consensus'):
                return 'unknown_numeric_role'
            if any(str(number[k]) not in quote for k in ('value', 'unit', 'period', 'comparison')):
                return 'numeric_context_not_in_evidence'
            if number['role'] == 'consensus' and not number.get('consensus_source'):
                return 'consensus_source_missing'
        tokens = set(re.findall(r'\d+(?:[.,]\d+)*', quote))
        covered = set(re.findall(r'\d+(?:[.,]\d+)*', json.dumps(claim['quantities'], ensure_ascii=False)))
        if not tokens <= covered:
            return 'unmapped_numeric_value'
    return ''


def ingest(raw, source, *, now=None):
    """Preserve revisions; conservative URL/event-key grouping, never title-only merge.

    event_key/origin_url/claims are typed adapter metadata, NOT commands from a feed.
    Plain RSS supplies no inferred units/periods: numeric claims wait for a parser.
    """
    now = now or datetime.now(UTC)
    lineage = [raw.get('provider','')] + list(raw.get('provider_lineage') or [])
    if any('twelvedata' in re.sub(r'\W','',str(v).lower()) for v in lineage):
        return {'accepted':False, 'reason':'restricted_provider_lineage', 'score':0, 'claims':[]}
    url = canonical(raw['url'])
    reason = rights_reason(source, url, 'internal')
    if reason:
        return {'accepted': False, 'reason': reason, 'score': 0, 'claims': []}
    first_seen = stamp(raw.get('retrieved_at')) or now
    published = stamp(raw.get('published'))
    updated = stamp(raw.get('updated'))
    if first_seen > now or (published and published > first_seen + timedelta(minutes=5)) or (updated and updated > first_seen + timedelta(minutes=5)) or (published and updated and updated < published):
        return {'accepted': False, 'reason': 'time_inconsistent_or_future', 'score': 0, 'claims': []}
    text = str(raw.get('excerpt') or raw.get('title') or '')[:4000]
    content_hash = digest(text)
    iid = digest([source['source_id'], url, content_hash])
    event_key = raw.get('event_key')
    # Only a complete typed key is usable across URLs. Missing identity stays separate.
    key = [event_key[k] for k in ('entity','kind','period')] if isinstance(event_key, dict) and all(event_key.get(k) for k in ('entity','kind','period')) else [canonical(raw.get('origin_url') or url)]
    eid = digest(key)
    item = {'id': iid, 'canonical_url': url, 'original_url': raw['url'], 'title': raw.get('title', ''),
        'source_id': source['source_id'], 'source': source['name'], 'language': source['language'],
        'primary': source['primary'], 'published_at': published.isoformat() if published else None,
        'updated_at': updated.isoformat() if updated else None, 'first_seen_at': first_seen.isoformat(),
        'entities': raw.get('entities', []), 'excerpt': text, 'content_hash': content_hash,
        'origin_family': source['operator'] if source['primary'] else raw.get('origin_family', 'unverified_syndication'),
        'event_id': eid, 'domain': source['domains'][0], 'rights_basis': source.get('terms_basis'),
        'bootstrap':raw.get('bootstrap',False),
        'expires_at': (first_seen + timedelta(days=source['rights']['retention_days'])).isoformat()}
    claims = raw.get('claims') or [{'text': text, 'quote': text, 'source_url': url,
        'status': 'confirmed' if source['primary'] else 'unverified', 'quantities': []}]
    public_reason = rights_reason(source, url, 'public_text') or rights_reason(source, url, 'public_llm')
    valid_claims, rejected = [], []
    for claim in claims:
        blocked = public_reason or claim_reason(claim, item, now)
        if not blocked and re.search(r'\d', claim['quote']):
            blocked = rights_reason(source, url, 'public_numbers')
        if blocked:
            rejected.append(blocked)
        else:
            valid_claims.append({**claim, 'public_allowed': True})
    fact_hash = digest(valid_claims)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if not event_key and len(text) >= 80:
            copied = db.execute('SELECT event_id FROM items WHERE content_hash=? AND first_seen<=? LIMIT 1', (content_hash,now.isoformat())).fetchone()
            if copied:
                eid = copied['event_id']
                item['event_id'] = eid
        prior = db.execute('SELECT payload FROM items WHERE event_id=? ORDER BY first_seen DESC', (eid,)).fetchall()
        old_items = [json.loads(r[0]) for r in prior]
        exact = next((r for r in old_items if r['content_hash'] == content_hash), None)
        relation = 'initial' if not prior else 'followup'
        if exact:
            relation = 'duplicate'
        elif raw.get('correction') is True:
            relation = 'correction'
        elif old_items:
            quantities = [q for claim in valid_claims for q in claim.get('quantities', [])]
            old_quantities = [[q for claim in r.get('claims', []) for q in claim.get('quantities', [])] for r in old_items]
            if not quantities or quantities in old_quantities or not valid_claims:
                relation = 'text_revision'
        # Preserve publisher versions but count syndication against the same origin.
        item.update(claims=valid_claims, rejected_claims=rejected, fact_hash=fact_hash, relation=relation)
        fresh = bool(published and now - (updated or published) <= timedelta(hours=settings()['max_age_hours']))
        values = {'importance': 1.0 if source['primary'] and source['domains'][0].startswith('official') else .5,
                  'novelty': 1.0 if relation in ('initial','followup','correction') else 0,
                  'relevance': 1.0 if source['domains'][0] in ('official_macro','official_fx','company_earnings','official_regulatory') else .5,
                  'timeliness': 1.0 if fresh else 0, 'explanation': 1.0 if len(text) >= 100 else .5}
        score = sum(settings()['weights'][k] * v for k, v in values.items())
        why = public_reason or (rejected[0] if not valid_claims and rejected else '') or ('missing_or_stale_publication_time' if not fresh else '') or ('no_new_fact' if not values['novelty'] else '') or ('below_threshold' if score < settings()['adoption_threshold'] else '')
        decision = {'accepted': not bool(why), 'reason': why or 'grounded_new_fact', 'score': score,
            'score_components': values, 'item_id': iid, 'event_id': eid, 'relation': relation, 'claims': valid_claims,
            'new_item': not any(r['id'] == iid for r in old_items)}
        db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?,?)', (iid, source['source_id'], eid, first_seen.isoformat(), item['published_at'], content_hash, json.dumps(item, ensure_ascii=False)))
        families = {r['origin_family'] for r in old_items if r['primary']} | ({item['origin_family']} if item['primary'] else set())
        event = {'event_id': eid, 'item_ids': sorted({r['id'] for r in old_items} | {iid}), 'verified_origin_count': len(families), 'latest_relation': relation}
        db.execute('INSERT OR REPLACE INTO events VALUES(?,?)', (eid, json.dumps(event)))
        db.execute('INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?)', (iid, eid, now.isoformat(), int(decision['accepted']), decision['reason'], score, json.dumps(decision, ensure_ascii=False)))
        if decision['accepted'] and relation == 'correction':
            db.execute("UPDATE candidates SET accepted=0,reason='superseded_by_correction' WHERE event_id=? AND item_id<>?", (eid, iid))
        db.execute('INSERT INTO ingest_decisions VALUES(?,?,?,?,?)', (now.isoformat(),iid,relation,int(decision['accepted']),decision['reason']))
    return decision


def public_evidence(sources, *, now=None):
    """Re-evaluate provenance outside LLM, before any public generation input."""
    now = now or datetime.now(UTC)
    cleaned = []
    for raw in sources:
        lineage = [raw.get('provider','')] + list(raw.get('provider_lineage') or [])
        if any('twelvedata' in re.sub(r'\W','',str(v).lower()) for v in lineage):
            return [], 'restricted_provider_lineage'
        source = source_for(raw.get('collection_source_name') or raw.get('source'))
        reason = rights_reason(source, raw.get('url', ''), 'public_llm')
        if reason:
            return [], reason
        iid = raw.get('collection_item_id')
        with database() as db:
            found = db.execute('SELECT payload FROM items WHERE id=?', (iid,)).fetchone()
            candidate = db.execute('SELECT accepted,reason FROM candidates WHERE item_id=?', (iid,)).fetchone()
        if not found:
            return [], 'collection_evidence_missing'
        if not candidate or not candidate['accepted']:
            return [], candidate['reason'] if candidate else 'collection_candidate_missing'
        item = json.loads(found[0])
        if item['source_id'] != source['source_id'] or canonical(raw.get('url','')) != item['canonical_url']:
            return [], 'collection_provenance_mismatch'
        published = stamp(item['updated_at'] or item['published_at'])
        if not published or published > now + timedelta(minutes=5) or now - published > timedelta(hours=settings()['max_age_hours']) or stamp(item['expires_at']) <= now:
            return [], 'collection_evidence_expired'
        if not item['claims']:
            return [], 'no_verified_claims'
        for claim in item['claims']:
            reason = claim_reason(claim, item, now)
            if reason or rights_reason(source, raw['url'], 'public_text') or (re.search(r'\d', claim['quote']) and rights_reason(source, raw['url'], 'public_numbers')):
                return [], reason or 'rights_changed'
        # Never forward unverified titles, cached xAI narrative, numeric charts, or caller rights overrides.
        cleaned.append({'source': source['rights']['attribution'], 'url': item['canonical_url'],
            'excerpt': '\n'.join(c['quote'] for c in item['claims']), 'period': item['published_at'],
            'period_basis': 'source_publication_time', 'retrieved_at': item['first_seen_at'], 'provider': 'official',
            'public_allowed': True, 'is_primary': source['primary'], 'image_public_allowed': False,
            'visual_points': [], 'collection_item_id': iid, 'collection_event_id': item['event_id'],
            'collection_source_name': source['name']})
    return cleaned, ''


def daily_metrics(now=None):
    now = now or datetime.now(UTC)
    begin = (now - timedelta(days=1)).isoformat()
    with database() as db:
        items = [json.loads(r[0]) for r in db.execute('SELECT payload FROM items WHERE first_seen>=?', (begin,))]
        candidates = [dict(r) for r in db.execute('SELECT * FROM candidates WHERE at>=?', (begin,))]
        fetches = [dict(r) for r in db.execute('SELECT * FROM fetches WHERE at>=?', (begin,))]
        attempts = [dict(r) for r in db.execute('SELECT * FROM ingest_decisions WHERE at>=?', (begin,))]
        blocked = db.execute('SELECT COUNT(*) FROM gates WHERE at>=?', (begin,)).fetchone()[0]
    latencies = sorted((stamp(r['first_seen_at'])-stamp(r['published_at'])).total_seconds() for r in items if not r.get('bootstrap') and stamp(r['published_at']) and stamp(r['first_seen_at']) >= stamp(r['published_at']))
    from statistics import median
    import math
    sources, domains = {}, {}
    for f in fetches:
        s = sources.setdefault(f['source_id'], {'attempts':0,'successful':0,'parse_failures':0})
        s['attempts'] += 1
        s['successful'] += f['status'] in ('new_items','no_new_items','empty_feed','not_modified','stale_content')
        s['parse_failures'] += f['status'] == 'parse_failed'
    for s in sources.values():
        s['success_rate'] = s['successful']/s['attempts']
    accepted = {c['item_id'] for c in candidates if c['accepted']}
    for i in items:
        d = domains.setdefault(i['domain'], {'collected':0,'accepted':0})
        d['collected'] += 1
        d['accepted'] += i['id'] in accepted
    candidate_delays = [(stamp(d['at'])-stamp(i['first_seen_at'])).total_seconds() for d in candidates for i in items if d['item_id']==i['id']]
    return {'window_hours':24, 'sources':sources, 'domains':domains, 'items':len(items),
        'bootstrap_items':sum(bool(i.get('bootstrap')) for i in items),
        'collection_to_candidate_seconds_median':median(candidate_delays) if candidate_delays else None,
        'capture_latency_seconds_median':median(latencies) if latencies else None,
        'capture_latency_seconds_p95':latencies[math.ceil(len(latencies)*.95)-1] if latencies else None,
        'latency_n':len(latencies), 'primary_candidate_ratio':sum(i['primary'] for i in items if i['id'] in accepted)/len(accepted) if accepted else None,
        'event_duplicate_rate':sum(d['relation'] in ('duplicate','text_revision') for d in attempts)/len(attempts) if attempts else None,
        'event_duplicate_denominator':'ingestion_observations_including_repeated_fetches',
        'important_event_capture_rate':None, 'capture_denominator_status':'independent_calendar_not_loaded',
        'collection_cost_per_accepted_candidate':None, 'cost_status':'unmeasured_not_zero',
        'unverified_blocked':sum(c['reason'] in ('unverified_claim','claim_not_exactly_grounded','numeric_context_missing') for c in candidates),
        'publication_evidence_gate_blocks':blocked}


def record_gate(stage, reason):
    with database() as db:
        db.execute('INSERT INTO gates VALUES(?,?,?)', (datetime.now(UTC).isoformat(),stage,reason))


def prune(now=None):
    """Remove expired excerpts, keep non-content audit hashes and decisions."""
    now = now or datetime.now(UTC)
    with database() as db:
        for r in db.execute('SELECT id,payload FROM items').fetchall():
            item = json.loads(r['payload'])
            if stamp(item['expires_at']) <= now:
                item.update(excerpt='', title='', claims=[], retention_expired=True)
                db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item), r['id']))
                db.execute('UPDATE candidates SET payload=? WHERE item_id=?', (json.dumps({'retention_expired':True}), r['id']))
