"""Conservative posting recovery policy; descriptive metrics, never a reach diagnosis."""
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from statistics import median
from common.runtime import JST, state_dir

KINDS = {'price_move', 'earnings_calendar', 'regulation', 'ipo', 'economic_indicator', 'monetary_policy', 'earnings_result'}
DIRECTIONS = {'買い', '売り', '中立'}
LABELS = re.compile(r'(?:結論|理由|根拠|方向|目標比率|基準日|注目点|次の確認)\s*[:：]')
HOLD = ('判定保留', '優位性未確認', '保有未入力')
RULES = '''配信回復用の編集条件：価格変動、決算日程・結果、規制、IPO、経済指標、金融政策の一件だけを扱う。
3文以内、冒頭は20文字以内の具体的な事実。根拠に沿う編集判断を一文含める。
判断は架空の個人経験・売買推奨・断定的価格予測ではない。無理に原因を補わない。
X重み付き140〜220以内。結論・理由・注目点・次の確認等のラベルを使わない。
数値はis_primary=trueの根拠に明記されたものだけ。ハッシュタグは原則0、最大1、末尾不可。
定型文の言い換えによる救済は禁止。不合格なら無投稿。'''


def enabled():
    from common.collection import enabled as collection_enabled
    if collection_enabled():
        return True
    from common.editorial_policy import enabled as editorial_enabled
    return editorial_enabled() and os.getenv('SINGA_QUALITY_POLICY_ENABLED', 'true').lower() in ('true', '1', 'yes')


def state():
    path = state_dir() / 'distribution_recovery.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def start(now):
    """Called under the publication lock, never by a preview or quality check."""
    if not state().get('started_at'):
        path = state_dir() / 'distribution_recovery.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'started_at': now.isoformat()}), encoding='utf-8')
        os.replace(tmp, path)


def day_limit(now):
    began = state().get('started_at')
    # First seven complete 24-hour periods, shared between processes/restarts.
    return 2 if not began or now - datetime.fromisoformat(began) < timedelta(days=7) else 4


def capacity(rows, now, *, ticker='', image=False, signal=False):
    dated = []
    for row in rows:
        value = datetime.fromisoformat(row['posted_at'])
        value = value.replace(tzinfo=JST) if value.tzinfo is None else value.astimezone(JST)
        dated.append((row, value))
    today = [r for r, t in dated if t.date() == now.astimezone(JST).date()]
    if len(today) >= day_limit(now):
        return 'recovery_daily_limit'
    if any(now - timedelta(hours=3) < t <= now for _, t in dated):
        return 'recovery_gap_180_minutes'
    if ticker and any(ticker == r.get('symbol') or ticker == r.get('editorial', {}).get('brief', {}).get('ticker') for r in today):
        return 'ticker_already_posted_today'
    if image and 2 * (1 + sum(bool(r.get('with_image') or r.get('has_media') or r.get('editorial', {}).get('image_hash')) for r in today)) > len(today) + 1:
        return 'image_share_over_half'
    if signal:
        review = recovery_review(rows, now)
        signal_count = sum(r.get('bot') == 'etf-signal' or r.get('editorial', {}).get('bot') == 'etf-signal' for r in today)
        if signal_count >= review['signal_daily_limit']:
            return 'signal_daily_limit'
    return ''


def skeleton(text):
    value = unicodedata.normalize('NFKC', text)
    value = re.sub(r'\s*出典[:：].*$', '', value, flags=re.S)
    value = re.sub(r'\b[A-Z]{2,6}\b|\d+(?:[.,]\d+)*', '<value>', value)
    return re.sub(r'\s+', '', value)


def similar(text, last_posts):
    scores = []
    for row in last_posts[-5:]:
        old = row.get('text', '')
        if not old:
            continue
        scores += [SequenceMatcher(None, text[:40], old[:40]).ratio(),
                   SequenceMatcher(None, skeleton(text), skeleton(old)).ratio()]
    return max(scores, default=0.0)


def input_reason(event, ticker, direction, evidence, last_posts, now):
    if direction not in DIRECTIONS:
        return 'direction_on_hold'
    if not evidence:
        return 'evidence_empty'
    if not isinstance(event, dict) or event.get('kind') not in KINDS or not event.get('fact'):
        return 'single_supported_event_required'
    if event.get('is_new_fact') is not True:
        return 'no_new_fact'
    if any(not s.get('excerpt') or not s.get('url') or not s.get('source') for s in evidence):
        return 'evidence_incomplete'
    return capacity(last_posts, now, ticker=ticker, signal=event.get('signal', False))


def text_reason(text, last_posts, *, judgment_present, sources):
    from common.safety import weighted_len
    if LABELS.search(text) or any(p in text for p in HOLD):
        return 'label_or_hold_template'
    if re.search(r'https?://|www\.', text):
        return 'url_in_body'
    tags = re.findall(r'[#＃][\w]+', text)
    content = re.sub(r'\s*出典[:：].*$', '', text, flags=re.S).strip()
    if len(tags) > 1 or re.search(r'[#＃][\w]+\s*$', content):
        return 'hashtag_policy'
    sentences = [s for s in re.split(r'[。！？!?]+|\n+', content) if s.strip()]
    if not sentences or len(sentences) > 3:
        return 'max_three_sentences'
    if len(sentences[0]) > 20:
        return 'opening_over_20_characters'
    if not 140 <= weighted_len(text) <= 220:
        return 'weighted_length_140_220'
    if not judgment_present:
        return 'grounded_judgment_missing'
    if similar(text, last_posts) >= .8:
        return 'recent_similarity_80_percent'
    # Exact numeric spellings only: no invented calculations/conversions.
    numbers = set(re.findall(r'\d+(?:[.,]\d+)*', unicodedata.normalize('NFKC', content)))
    primary_numbers = set(re.findall(r'\d+(?:[.,]\d+)*', unicodedata.normalize('NFKC', ' '.join(s['excerpt'] for s in sources if s.get('is_primary') is True))))
    if not numbers <= primary_numbers:
        return 'number_without_primary_evidence'
    if not numbers and not any(s.get('source') in text for s in sources):
        return 'named_entity_or_number_missing'
    return ''


def recovery_review(posts, now):
    started = state().get('started_at')
    path = state_dir() / 'metrics_snapshots.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    latest = {}
    for row in sorted(rows, key=lambda r: r.get('metrics_collected_at', '')):
        if row.get('stage') == '24h' and row.get('status') == 'collected' and isinstance(row.get('impressions'), (int, float)):
            latest[str(row.get('tweet_id'))] = row
    observed, winners, groups = [], set(), defaultdict(list)
    for post in posts:
        if not started or str(post.get('posted_at', '')) < started:
            continue
        metric = latest.get(str(post.get('tweet_id')))
        if not metric:
            continue
        observed.append(metric['impressions'])
        kind = post.get('editorial', {}).get('brief', {}).get('event_type')
        if kind:
            groups[kind].append(metric['impressions'])
            if any(isinstance(metric.get(k), (int, float)) and metric[k] > 0 for k in ('likes', 'replies', 'reposts', 'quotes')):
                winners.add(kind)
    followers = max(0, int(os.getenv('RECOVERY_FOLLOWER_REFERENCE', '110')))
    mature = bool(started and now - datetime.fromisoformat(started) >= timedelta(days=7))
    med = median(observed) if observed else None
    low = mature and med is not None and med < followers / 2
    return {'status': 'observed' if observed else 'insufficient_data', 'n': len(observed),
            'median_24h_impressions': med, 'follower_reference': followers, 'follower_reference_is_live': False,
            'below_half_followers': low, 'signal_daily_limit': 1 if low else 2,
            'retained_event_types': sorted(winners), 'type_sample_sizes': {k: len(v) for k,v in groups.items()},
            'phase_mature': mature, 'target_impressions': 30, 'target_reactions': 1,
            'causality': 'not_established', 'automatic_budget_changes': False}
