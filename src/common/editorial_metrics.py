"""Descriptive post-level cohorts; no automatic control changes or causal claims."""
import json
from collections import defaultdict
from statistics import median
from common.runtime import state_dir


def summarize(posts, snapshots):
    posts = {str(p['tweet_id']): p for p in posts if p.get('tweet_id')}
    latest = {}
    for row in sorted(snapshots, key=lambda r: r.get('metrics_collected_at', '')):
        key = (str(row.get('tweet_id')), row.get('stage'))
        if key[0] in posts and key[1] in ('1h', '6h', '24h') and row.get('status') == 'collected' and isinstance(row.get('impressions'), (int, float)):
            latest[key] = row
    groups = defaultdict(list)
    for (tid, stage), row in latest.items():
        post = posts[tid]
        cohort = 'editorial_v2' if post.get('editorial') else 'legacy'
        base = (cohort, stage)
        groups[base + ('all', 'all')].append(row)
        for dimension, value in (
            ('type', post.get('bot') or post.get('mode') or 'unknown'),
            ('time', post.get('posted_window') or post.get('time_slot') or post.get('posted_at', '')[11:13] or 'unknown'),
            ('topic', post.get('theme') or post.get('topic') or 'unknown')):
            groups[base + (dimension, str(value))].append(row)
    output = []
    for (cohort, stage, dimension, value), rows in sorted(groups.items()):
        rates = []
        for row in rows:
            # Missing engagement values are not silently treated as zero.
            keys = ('likes', 'reposts', 'replies', 'quotes')
            if row['impressions'] > 0 and all(isinstance(row.get(k), (int, float)) for k in keys):
                rates.append(sum(row[k] for k in keys) / row['impressions'])
        output.append({'cohort': cohort, 'stage': stage, 'dimension': dimension, 'value': value,
                       'n': len(rows), 'median_impressions': median(r['impressions'] for r in rows),
                       'reaction_rate_n': len(rates), 'median_reaction_rate': median(rates) if rates else None,
                       'late_count': sum(r.get('collection_timing') == 'late_catchup' for r in rows),
                       'median_observed_age_hours': median([r['post_age_hours'] for r in rows if isinstance(r.get('post_age_hours'), (int, float))]) if any(isinstance(r.get('post_age_hours'), (int, float)) for r in rows) else None,
                       'small_sample': len(rows) < 30})
    return {'groups': output, 'metric': '(likes+reposts+replies+quotes)/impressions, per-post median',
            'limitations': ['Descriptive only; not a causal effect or evidence of X bot classification.',
                            'Compare equal collection stages; topic/time/type and timing delays may confound results.',
                            'No post quota, budget, model or safety settings are automatically modified.']}


def daily_review():
    root = state_dir()
    posts = json.loads((root / 'posted_history.json').read_text(encoding='utf-8')) if (root / 'posted_history.json').exists() else []
    path = root / 'metrics_snapshots.jsonl'
    snapshots = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    result = {'status': 'quality_only', 'target': None, 'automatic_actions': [], **summarize(posts, snapshots)}
    (root / 'editorial_metrics_review.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
