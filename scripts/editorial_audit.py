"""Offline editorial audit. Reads supplied state only; never calls APIs or loads .env."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from common.editorial_metrics import summarize


def audit(root, limit=100):
    posts = json.loads((root / 'posted_history.json').read_text(encoding='utf-8'))
    posts = sorted({str(p['tweet_id']): p for p in posts if p.get('tweet_id')}.values(), key=lambda p: p.get('posted_at', ''))[-limit:]
    metrics_path = root / 'metrics_snapshots.jsonl'
    metrics = [json.loads(line) for line in metrics_path.read_text(encoding='utf-8').splitlines()] if metrics_path.exists() else []
    lengths = [len(p.get('text', '')) for p in posts]
    def duplicates(values):
        return sum(n - 1 for value, n in Counter(values).items() if value and n > 1)
    return {'sample_n': len(posts), 'from': posts[0]['posted_at'] if posts else None,
            'to': posts[-1]['posted_at'] if posts else None,
            'length': {'median': median(lengths), 'min': min(lengths), 'max': max(lengths)} if lengths else {},
            'phrases': {p: sum(p in row.get('text', '') for row in posts) for p in ('次の確認', '注目', '可能性', '結論', '背景')},
            'line_counts': dict(Counter(len(p.get('text', '').splitlines()) for p in posts)),
            'modes': dict(Counter(p.get('mode', 'unknown') for p in posts)),
            'exact_text_duplicates': duplicates(p.get('text') for p in posts),
            'exact_url_duplicates': duplicates(p.get('url') for p in posts),
            'performance': summarize(posts, metrics)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('limit must be positive')
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(audit(args.state_dir, args.limit), ensure_ascii=False, indent=2))
