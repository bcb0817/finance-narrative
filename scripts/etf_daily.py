"""Daily ETF refresh -> verified public snapshot -> three bounded X updates.

Uses the existing finance credentials and budgets; never sends Discord messages.
Ambiguous X responses stay uncertain rather than risk posting twice.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from common.runtime import load_env, state_dir, JST, post_enabled


def write(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def render(asset, now, holiday=False):
    symbol, proxy = asset['symbol'], asset['proxy']
    if asset['excluded']:
        direction, reason = '対象外', '価格・分割や欠損データの不整合で判定停止'
    elif asset['dataStatus'] != 'VALID' or asset['targetWeight'] is None or asset['decision'] == 'UNAVAILABLE':
        direction, reason = '判定保留', '有効なデータ・配分条件を確認できず判定停止'
    elif not asset['expiresAt'] or datetime.fromisoformat(asset['expiresAt']) <= now:
        direction, reason = '期限切れ', '保存された注文案は失効。次の確定日足を待ちます'
    else:
        direction = 'Buy方向' if asset['decision'] == 'LONG' else 'Sell方向'
        reason = f"{proxy}の確定終値{'＞' if asset['decision'] == 'LONG' else '≦'}200日平均"
    target = '保留' if asset['targetWeight'] is None else f"{asset['targetWeight'] * 100:.1f}%"
    text = (f"【{symbol} 日次更新】\n方向：{direction}\n目標比率：{target}（研究値）\n"
            f"根拠：{reason}\n基準日：{asset['decisionSession'] or '未確認'}\n"
            f"更新：{now.astimezone(JST):%m/%d %H:%M} JST\n"
            + ('対象市場日は休場。新しい確定足なし。\n' if holiday else '')
            + '保有未入力のため個別注文は判断しません。優位性未確認。')
    # Conservative X weighting: all non-ASCII characters count double.
    if sum(1 if ord(ch) < 128 else 2 for ch in text) > 280:
        raise ValueError('POST_TOO_LONG')
    return text


def command(args, cwd, timeout=180):
    result = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError('SUBPROCESS_FAILED')
    return result.stdout.strip()


def publish(config, summary):
    """Only the derived public snapshot is committed, in an isolated checkout."""
    site = Path(config['site_root'])
    node = shutil.which('node')
    npm = Path(shutil.which('npm') or '').parent / 'node_modules/npm/bin/npm-cli.js'
    if not node or not npm.exists():
        raise RuntimeError('NODE_NPM_UNAVAILABLE')
    with tempfile.TemporaryDirectory(prefix='etf-site-') as temp:
        checkout = Path(temp) / 'site'
        command(['git', 'clone', '--depth=1', config['git_url'], checkout], ROOT)
        if (checkout / 'package-lock.json').read_bytes() == (site / 'package-lock.json').read_bytes():
            command([node, '-e', "require('fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
                     site / 'node_modules', checkout / 'node_modules'], checkout)
        else:
            command([node, npm, 'ci'], checkout)
        command([node, npm, 'run', 'signals:sync', '--', '--source', summary], checkout)
        if not command(['git', 'diff', '--', 'src/data/etf-signals.json'], checkout):
            return
        for args in (['run', 'check'], ['run', 'validate'], ['exec', '--', 'tsx', '--test', 'tests/etf-signals.test.ts'], ['run', 'build']):
            command([node, npm, *args], checkout)
        command(['git', 'add', 'src/data/etf-signals.json'], checkout)
        command(['git', '-c', 'user.name=ETF Signal Scheduler', '-c', 'user.email=etf-scheduler@users.noreply.github.com',
                 'commit', '-m', 'Refresh daily leveraged ETF signals'], checkout)
        command(['git', 'push', 'origin', 'HEAD:main'], checkout)


def live_snapshot(config, generation):
    request = urllib.request.Request(config['site_url'] + '/signals/snapshot.json?generation=' + generation,
                                     headers={'User-Agent': 'FinanceBotETF/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read(100000))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # First deployment may still be building this endpoint.
        raise
    if data.get('version') != 1 or data.get('source') != 'Yahoo Finance / yfinance':
        raise ValueError('INVALID_LIVE_SNAPSHOT')
    if sorted(a['symbol'] for a in data['assets']) != ['1570', 'SOXL', 'TQQQ']:
        raise ValueError('INVALID_LIVE_SYMBOLS')
    if datetime.fromisoformat(data['generatedAt'].replace('Z', '+00:00')) < datetime.fromisoformat(generation):
        return None
    if datetime.fromisoformat(data['generatedAt'].replace('Z', '+00:00')) > datetime.now(timezone.utc):
        raise ValueError('FUTURE_LIVE_SNAPSHOT')
    return data


def run(preview=False):
    load_env()
    config = json.loads((ROOT / 'config/etf_daily.json').read_text(encoding='utf-8'))
    now, day = datetime.now(timezone.utc), datetime.now(JST).date().isoformat()
    lab = Path(config['lab_root'])
    python = lab / '.venv/Scripts/python.exe'
    if preview:
        data = json.loads((Path(config['site_root']) / 'src/data/etf-signals.json').read_text(encoding='utf-8'))
        return {'status': 'preview', 'posts': [render(a, now) for a in data['assets']]}
    if not config['enabled'] or not post_enabled():
        return {'status': 'disabled'}
    # A transaction serializes manual and scheduled runs without restarting the finance daemon.
    db = sqlite3.connect(state_dir() / 'etf_daily.sqlite', timeout=1)
    db.execute('CREATE TABLE IF NOT EXISTS posts (day TEXT, symbol TEXT, status TEXT, tweet_id TEXT, PRIMARY KEY(day,symbol))')
    db.execute('BEGIN IMMEDIATE')
    try:
        pending = []
        progress_path = state_dir() / 'etf_daily_progress.json'
        progress = json.loads(progress_path.read_text(encoding='utf-8')) if progress_path.exists() else {}
        slots = json.loads(command([python, '-m', 'lab.daily', '--slots'], lab))
        for group, symbols in [('US', ['TQQQ', 'SOXL']), ('JP', ['1570'])]:
            if all(db.execute('SELECT status FROM posts WHERE day=? AND symbol=?', (day, symbol)).fetchone() for symbol in symbols):
                continue
            key = day + '-' + group
            if now < datetime.fromisoformat(slots[group]['due']):
                continue
            entry = progress.get(key, {})
            if 'generation' not in entry:
                if entry.get('attempts', 0) >= 3 or now < datetime.fromisoformat(entry.get('retry_at', '2000-01-01T00:00:00+00:00')):
                    pending.append({'group': group, 'reason': 'REFRESH_FAILED' if entry.get('attempts', 0) >= 3 else 'REFRESH_RETRY_WAIT'})
                    continue
                from datetime import timedelta
                entry = {'attempts': entry.get('attempts', 0) + 1, 'retry_at': (now + timedelta(minutes=15)).isoformat()}
                progress[key] = entry
                write(progress_path, progress)
                summary = Path(command([python, '-m', 'lab.daily'], lab, 300).splitlines()[-1])
                generation = json.loads(summary.read_text(encoding='utf-8'))['calculated_at']
                publish(config, summary)
                progress[key] = {'generation': generation, 'holiday': slots[group]['holiday']}
                progress = {k: v for k, v in progress.items() if k.startswith(day)}
                write(progress_path, progress)
            data = live_snapshot(config, progress[key]['generation'])
            if data is None:
                pending.append({'group': group, 'reason': 'WAITING_FOR_PUBLICATION'})
                continue
            from common.x_client import get_tweepy_client
            from common.posting_policy import check_post
            from common.post_registry import record_post
            client = get_tweepy_client()
            fingerprint = hashlib.sha256('|'.join(os.environ[k] for k in ('API_KEY', 'API_KEY_SECRET', 'ACCESS_TOKEN', 'ACCESS_TOKEN_SECRET')).encode()).hexdigest()
            identity_path = state_dir() / 'etf_daily_identity.json'
            identity = json.loads(identity_path.read_text(encoding='utf-8')) if identity_path.exists() else {}
            if identity.get('fingerprint') != fingerprint or identity.get('account_id') != config['account_id']:
                account = client.get_me(user_auth=True).data
                if str(account.id) != config['account_id'] or account.username.lower() != config['account_username'].lower():
                    raise RuntimeError('WRONG_X_ACCOUNT')
                write(identity_path, {'fingerprint': fingerprint, 'account_id': str(account.id)})
            for symbol in symbols:
                if db.execute('SELECT status FROM posts WHERE day=? AND symbol=?', (day, symbol)).fetchone():
                    continue
                asset = next(a for a in data['assets'] if a['symbol'] == symbol)
                text = render(asset, datetime.now(timezone.utc), progress[key]['holiday'])
                policy = check_post(text)
                if not policy.allowed:
                    pending.append({'symbol': symbol, 'reason': policy.reason})
                    continue
                db.execute('INSERT INTO posts VALUES (?,?,?,?)', (day, symbol, 'uncertain', None))
                db.commit()  # Claim before the network request: no automatic retry after ambiguous success.
                try:
                    response = client.create_tweet(text=text)
                    tweet_id = str(response.data['id'])
                except Exception:
                    db.execute('BEGIN IMMEDIATE')
                    # Keep uncertain for operator reconciliation; never post a blind retry.
                    continue
                record_post(tweet_id, text=text, bot='etf-signal', mode='daily_signal',
                            extra={'symbol': symbol, 'snapshot_at': data['generatedAt']}, notify_discord=False)
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE posts SET status=?,tweet_id=? WHERE day=? AND symbol=?', ('posted', tweet_id, day, symbol))
                db.commit()
                db.execute('BEGIN IMMEDIATE')
        db.commit()
        rows = db.execute('SELECT symbol,status,tweet_id FROM posts WHERE day=?', (day,)).fetchall()
        status = 'attention' if any(v == 'uncertain' for _, v, _ in rows) else ('completed' if len(rows) == 3 else 'waiting')
        return {'status': status, 'day': day, 'pending': pending,
                'posts': [{'symbol': s, 'status': v, 'tweet_id': t} for s, v, t in rows]}
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    try:
        result = run(args.preview)
    except Exception as exc:
        result = {'status': 'failed', 'reason': type(exc).__name__}
    if not args.preview:
        out = state_dir() / 'etf_daily_last_run.json'
        write(out, result)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] == 'failed':
        sys.exit(1)
