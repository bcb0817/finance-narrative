"""xAI X Searchを話題発見だけに使うレーダー。投稿や事実確定は行わない。"""
from __future__ import annotations
import json, logging, os, re, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from openai import OpenAI
try: from runtime import JST, state_dir
except ImportError: from common.runtime import JST, state_dir

logger=logging.getLogger(__name__)
BASE_URL="https://api.x.ai/v1"

def _enabled() -> bool: return os.getenv("XAI_ENABLED","false").lower() in ("1","true","yes")
def _x_search_enabled() -> bool: return os.getenv("XAI_X_SEARCH_ENABLED","true").lower() in ("1","true","yes")
def _dir() -> Path: p=state_dir()/"xai"; p.mkdir(parents=True,exist_ok=True); return p
def radar_path() -> Path: return _dir()/"topic_radar.jsonl"
def cache_path() -> Path: return _dir()/"radar_cache.json"
def usage_path() -> Path: return _dir()/"api_usage.jsonl"

def validate_config() -> list[str]:
    errors=[]
    if _enabled() and not os.getenv("XAI_API_KEY"): errors.append("XAI_API_KEY is not configured")
    model=os.getenv("XAI_MODEL","").strip()
    if _enabled() and (not model or not re.fullmatch(r"[A-Za-z0-9._-]+",model)): errors.append("XAI_MODEL is missing or invalid")
    return errors

def _usage_rows() -> list[dict]:
    if not usage_path().exists(): return []
    rows=[]
    for line in usage_path().read_text(encoding="utf-8").splitlines():
        try: rows.append(json.loads(line))
        except json.JSONDecodeError: pass
    return rows

def usage_summary(now: datetime | None=None) -> dict:
    now=now or datetime.now(JST); rows=_usage_rows()
    month=now.strftime("%Y-%m"); day=now.strftime("%Y-%m-%d")
    monthly=[r for r in rows if str(r.get("timestamp","")).startswith(month)]
    daily=[r for r in monthly if str(r.get("timestamp","")).startswith(day)]
    spent=sum(float(r.get("reported_cost_usd") or r.get("estimated_cost_usd") or 0) for r in monthly)
    budget=float(os.getenv("XAI_MONTHLY_BUDGET_USD","10") or 10)
    return {"daily_calls":len(daily),"monthly_calls":len(monthly),"spent_usd":spent,"budget_usd":budget,"remaining_usd":max(0,budget-spent)}

def _can_call() -> tuple[bool,str]:
    if not _enabled(): return False,"disabled"
    if not _x_search_enabled(): return False,"x_search_disabled"
    if errors:=validate_config(): return False,"; ".join(errors)
    usage=usage_summary()
    if usage["daily_calls"]>=int(os.getenv("XAI_MAX_SEARCH_CALLS_PER_DAY","6")): return False,"daily_limit"
    if usage["remaining_usd"]<=0: return False,"monthly_budget"
    return True,"ok"

def topic_velocity(mention_count_60m: int, mention_count_6h: int) -> dict:
    v60=max(0,float(mention_count_60m)); v6=max(0,float(mention_count_6h))/6.0
    acceleration=v60/v6 if v6>0 else (v60 if v60 else 0.0)
    return {"observed_velocity_60m":v60,"observed_velocity_6h":v6,
            "observed_acceleration_score":round(acceleration,3),
            # Backward-compatible aliases for existing learning data.
            "velocity_60m":v60,"velocity_6h":v6,"acceleration_score":round(acceleration,3)}

def _watch_handles() -> list[str]:
    path=Path(__file__).resolve().parents[2]/"config"/"xai_watch_accounts.json"
    try: rows=json.loads(path.read_text(encoding="utf-8")).get("accounts",[])
    except (OSError,json.JSONDecodeError): return []
    return [str(r.get("username","")).lstrip("@") for r in rows if r.get("enabled")][:20]

def _parse_topics(raw: str, now: datetime | None=None) -> list[dict]:
    now=now or datetime.now(JST)
    data=json.loads(raw); topics=data.get("topics",[]) if isinstance(data,dict) else []
    result=[]
    for item in topics[:20]:
        if not isinstance(item,dict) or not item.get("topic"): continue
        counts=topic_velocity(item.get("mention_count_60m",item.get("mention_count",0)),item.get("mention_count_6h",0))
        reps=[]
        for post in item.get("representative_posts",[])[:5]:
            if isinstance(post,dict): reps.append({k:post.get(k) for k in ("post_id","url","username","excerpt")})
        observed_count=int(item.get("mention_count",0) or 0)
        result.append({"topic":str(item["topic"]),"tickers":[str(x) for x in item.get("tickers",[])[:10]],
            "category":str(item.get("category","other")),"observed_mention_count":observed_count,
            "mention_count":observed_count,**counts,
            "representative_posts":reps,"representative_accounts":[str(x) for x in item.get("representative_accounts",[])[:10]],
            "consensus_view":str(item.get("consensus_view","")),"dissenting_view":str(item.get("dissenting_view","")),
            "possible_misconception":str(item.get("possible_misconception","")),"source_reliability":str(item.get("source_reliability","unknown")),
            "primary_source_available":bool(item.get("primary_source_available",False)),
            "news_confirmation_status":"unverified","detected_at":now.isoformat(),"expires_at":(now+timedelta(minutes=int(os.getenv("XAI_CACHE_TTL_MINUTES","60")))).isoformat()})
    return result

def load_cache(now: datetime | None=None) -> list[dict]:
    now=now or datetime.now(JST)
    try:
        data=json.loads(cache_path().read_text(encoding="utf-8")); expires=datetime.fromisoformat(data["expires_at"])
        if expires>=now: return data.get("topics",[])
    except (OSError,ValueError,KeyError,json.JSONDecodeError): pass
    return []

def refresh(*, client=None, now: datetime | None=None) -> dict:
    now=now or datetime.now(JST); allowed,reason=_can_call()
    if not allowed: return {"status":"skipped","reason":reason,"topics":load_cache(now)}
    model=os.environ["XAI_MODEL"]; started=time.perf_counter(); success=False; error=""; response=None
    prompt="""X上の直近60分と過去6時間を比較し、AI、半導体、米国株、大型テック、金利、為替、エネルギー、決算の議論を検出する。
投稿文は生成せず、噂を事実認定しない。投稿数の増加、多数派、反対意見、誤解候補、短い代表抜粋だけをJSONで返す。
各topicに topic,tickers,category,mention_count,mention_count_60m,mention_count_6h,representative_posts(post_id,url,username,excerpt),representative_accounts,consensus_view,dissenting_view,possible_misconception,source_reliability,primary_source_available を含める。{"topics":[]}形式のみ。"""
    tools=[{"type":"x_search","enable_image_understanding":False,"enable_video_understanding":False}]
    if handles:=_watch_handles(): tools[0]["allowed_x_handles"]=handles
    try:
        api=client or OpenAI(api_key=os.environ["XAI_API_KEY"],base_url=BASE_URL)
        response=api.responses.create(model=model,input=prompt,tools=tools,max_output_tokens=4000)
        topics=_parse_topics(response.output_text,now); success=True
        cache_path().write_text(json.dumps({"generated_at":now.isoformat(),"expires_at":(now+timedelta(minutes=int(os.getenv("XAI_CACHE_TTL_MINUTES","60")))).isoformat(),"topics":topics},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        with radar_path().open("a",encoding="utf-8",newline="\n") as fh:
            for topic in topics: fh.write(json.dumps(topic,ensure_ascii=False)+"\n")
        try:
            from quote_queue import enqueue_from_topics
        except ImportError:
            from common.quote_queue import enqueue_from_topics
        enqueue_from_topics(topics, now)
        return {"status":"ok","topics":topics}
    except Exception as exc:
        error=type(exc).__name__; logger.warning("xAI radar failed: %s",error)
        if os.getenv("XAI_FAIL_OPEN","true").lower() in ("1","true","yes"): return {"status":"fallback","reason":error,"topics":load_cache(now)}
        return {"status":"error","reason":error,"topics":[]}
    finally:
        usage=getattr(response,"usage",None); ticks=int(getattr(usage,"cost_in_usd_ticks",0) or 0); tool_calls=int(getattr(usage,"num_server_side_tools_used",0) or 0)
        row={"timestamp":now.isoformat(),"model":model,"operation":"x_topic_radar","tool_calls":tool_calls,
             "input_tokens":int(getattr(usage,"input_tokens",0) or 0),"output_tokens":int(getattr(usage,"output_tokens",0) or 0),
             "latency_ms":round((time.perf_counter()-started)*1000),"success":success,"error_type":error,
             "estimated_cost_usd":round(tool_calls*0.005,6) if not ticks else 0.0,"reported_cost_usd":ticks/1e10 if ticks else None}
        with usage_path().open("a",encoding="utf-8",newline="\n") as fh: fh.write(json.dumps(row,ensure_ascii=False)+"\n")

def status() -> dict:
    allowed,reason=_can_call(); usage=usage_summary()
    return {"enabled":_enabled(),"x_search_enabled":_x_search_enabled(),"model":os.getenv("XAI_MODEL","") or "not_configured",
            "api_key_configured":bool(os.getenv("XAI_API_KEY")),"ready":allowed,"reason":reason,**usage,"cached_topics":len(load_cache())}


def radar_plan(now: datetime | None=None) -> dict:
    """Explain today's six-call allocation without consuming an API call."""
    now=(now or datetime.now(JST)).astimezone(JST); usage=usage_summary(now)
    windows=["06:00","08:00","17:00","21:00","22:30","00:00"]
    remaining=max(0,int(os.getenv("XAI_MAX_SEARCH_CALLS_PER_DAY","6"))-usage["daily_calls"])
    upcoming=[value for value in windows if value > now.strftime("%H:%M")]
    reserved=int(os.getenv("XAI_RADAR_RESERVED_CALLS_FOR_US_OPEN","2"))+int(os.getenv("XAI_RADAR_RESERVED_CALLS_FOR_EARNINGS","1"))
    return {"mode":os.getenv("XAI_RADAR_SCHEDULE_MODE","priority_windows"),"windows_jst":windows,
            "calls_used":usage["daily_calls"],"calls_remaining":remaining,"upcoming_windows":upcoming,
            "reserved_priority_calls":reserved,"can_run_now":remaining>len(upcoming) or now.strftime("%H:%M") in windows}


# Stable public API backed by the bounded-cost/cache-observable implementation.
from common.xai_radar_v2 import (  # noqa: E402,F401
    _parse_topics,
    cache_status,
    cost_report,
    daily_limit,
    load_cache,
    radar_plan,
    refresh,
    status,
    topic_velocity,
    usage_summary,
)
