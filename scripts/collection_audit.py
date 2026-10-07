"""Read-only production diagnosis or point-in-time replay into an isolated state DB."""
import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from common import collection as c


def diagnose(root):
    from news_bot.news import RSS_FEEDS
    data = root/'data'
    health = json.loads((data/'rss_health.json').read_text(encoding='utf-8'))
    groups = defaultdict(lambda: {'configured_sources':0,'last_observed_items':0})
    active = []
    for name, feed in RSS_FEEDS.items():
        current = health.get(name,{})
        groups[feed['group']]['configured_sources'] += 1
        groups[feed['group']]['last_observed_items'] += current.get('item_count',0)
        active.append({'name':name,'url':feed['url'],'group':feed['group'],**current})
    files = {}
    for name in ('posted_history.json','evaluated_history.json','xai/events.jsonl','xai/observations.jsonl','xai/usage.jsonl','fx/movements.jsonl','market_data/movements.jsonl'):
        path=data/name
        if path.exists():
            value=json.loads(path.read_text(encoding='utf-8')) if path.suffix=='.json' else [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
            files[name]=len(value)
    return {'checked_at':datetime.now(timezone.utc).isoformat(), 'scope':'latest health snapshot, not longitudinal success rate',
        'configured_sources':len(active),'status_counts':dict(Counter(r.get('status','unmeasured') for r in active)),
        'group_counts':dict(groups),'sources':active,'history_rows':files,
        'primary_candidate_ratio':None,'event_duplicate_rate':None,'capture_latency':None,
        'collection_to_candidate_latency':None,'important_event_capture_rate':None,
        'unmeasured_reason':'Legacy records lack item-level first-seen, event keys and independent calendar denominator.'}


def replay(path, as_of):
    state = Path(os.environ.get('STATE_DIR','')).resolve()
    if not os.environ.get('STATE_DIR') or state in ((ROOT/'data').resolve(), Path('D:/SNS Bot/finance-narrative/data').resolve()):
        raise ValueError('explicit_isolated_STATE_DIR_required')
    contents=path.read_text(encoding='utf-8')
    rows=json.loads(contents) if path.suffix=='.json' else [json.loads(line) for line in contents.splitlines() if line.strip()]
    accepted, skipped = 0, Counter()
    for row in sorted(rows,key=lambda r:r.get('retrieved_at','')):
        seen=c.stamp(row.get('retrieved_at'))
        # An original capture time is mandatory. No reconstruction from later articles.
        if not seen or seen > as_of:
            skipped['not_available_at_replay_time']+=1
            continue
        source=c.source_for(row.get('source'))
        if not source:
            skipped['unknown_source']+=1
            continue
        result=c.ingest(row,source,now=seen)
        accepted+=int(result['accepted'])
        if not result['accepted']:skipped[result['reason']]+=1
    return {'mode':'point_in_time_replay','as_of':as_of.isoformat(),'input_rows':len(rows),'accepted':accepted,
        'skipped':dict(skipped),'x_post_called':False,'llm_called':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--diagnose-root',type=Path)
    parser.add_argument('--replay',type=Path)
    parser.add_argument('--as-of')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.diagnose_root:
        result=diagnose(args.diagnose_root)
    elif args.replay and c.stamp(args.as_of):
        result=replay(args.replay,c.stamp(args.as_of))
    else:
        parser.error('Choose --diagnose-root or --replay with timezone-aware --as-of')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='sources'},ensure_ascii=False))
