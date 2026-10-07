"""Atomic conservative reservations. No promise of a provider-enforced dollar cap."""
import json
import os
from common import collection as c
from common.runtime import state_dir, JST


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def usage(response):
    u = value(response, 'usage', {})
    details = value(u, 'server_side_tool_usage_details', {})
    ticks = value(u, 'cost_in_usd_ticks')
    posts, users = value(details,'x_posts_fetched'), value(details,'x_users_fetched')
    return {'reported_cost_usd': ticks / 1e10 if isinstance(ticks,(int,float)) and ticks >= 0 else None,
        'x_posts_fetched':posts, 'x_users_fetched':users,
        'x_search_calls':value(details,'x_search_calls'),
        'input_tokens':value(u,'input_tokens'), 'output_tokens':value(u,'output_tokens'),
        'fetched_item_cost_usd': posts*.005 + users*.01 if isinstance(posts,int) and isinstance(users,int) else None,
        'cost_status':'provider_reported' if ticks is not None else 'unknown_total'}


def reserve(run_id, fingerprint, now, *, exploration=False):
    month = now.astimezone(JST).strftime('%Y-%m')
    day = now.astimezone(JST).date().isoformat()
    amount = max(.5, float(os.getenv('XAI_RESERVATION_USD','1')))
    cap = float(os.getenv('XAI_MONTHLY_BUDGET_USD','20'))
    margin = max(.5, float(os.getenv('XAI_BUDGET_SAFETY_MARGIN_USD','1')))
    with c.database() as db:
        db.execute('CREATE TABLE IF NOT EXISTS xai_reservations(id TEXT PRIMARY KEY, fingerprint TEXT, month TEXT, day TEXT, amount REAL, status TEXT, exploration INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT * FROM xai_reservations WHERE month=?', (month,)).fetchall()
        ids = {r['id'] for r in rows}
        legacy, legacy_today = 0., 0
        ledger = state_dir() / 'xai/api_usage.jsonl'
        if ledger.exists():
            for line in ledger.read_text(encoding='utf-8').splitlines():
                r = json.loads(line)  # corrupted accounting blocks, not zero spend
                if r.get('run_id') in ids or not str(r.get('timestamp','')).startswith(month):
                    continue
                cost = r.get('reported_cost_usd')
                if cost is None:
                    cost = r.get('estimated_cost_usd')
                if cost is None:
                    raise ValueError('xai_historical_cost_unknown')
                legacy += max(0,float(cost))
                legacy_today += str(r.get('timestamp','')).startswith(day)
        if any(r['status'] in ('pending','uncertain') for r in rows):
            raise ValueError('xai_pending_or_unreconciled_request')
        if any(r['fingerprint'] == fingerprint for r in rows):
            raise ValueError('xai_same_fact_already_searched')
        from common.xai_radar_v2 import daily_limit, _event_mode
        if legacy_today + sum(r['day']==day for r in rows) >= min(4 if _event_mode(now) else 2, daily_limit(now)):
            raise ValueError('xai_daily_limit')
        if legacy + sum(r['amount'] for r in rows) + amount + margin > cap:
            raise ValueError('xai_reservation_budget')
        if exploration and sum(r['amount'] for r in rows if r['exploration']) + amount > min(1., cap*.05):
            raise ValueError('xai_exploration_budget')
        db.execute('INSERT INTO xai_reservations VALUES(?,?,?,?,?,?,?)', (run_id,fingerprint,month,day,amount,'pending',int(exploration)))
    return amount


def settle(run_id, response):
    measured = usage(response)
    actual = measured['reported_cost_usd']
    with c.database() as db:
        if actual is None:
            db.execute("UPDATE xai_reservations SET status='uncertain' WHERE id=?", (run_id,))
        else:
            db.execute("UPDATE xai_reservations SET status='settled',amount=? WHERE id=?", (actual,run_id))
    return measured
