"""Cost, quality and Windows-local health reports."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from common.runtime import JST, log_dir, output_dir, state_dir


def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _jsonl(path: Path) -> list[dict]:
    rows=[]
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value=json.loads(line)
                if isinstance(value,dict): rows.append(value)
            except json.JSONDecodeError: pass
    except OSError: pass
    return rows


def xai_roi_report(days: int = 30) -> dict:
    from common.metrics_collector import load_snapshots
    from common.xai_radar import cache_status, usage_summary
    cutoff=datetime.now(JST)-timedelta(days=max(1,days))
    posts=[row for row in _jsonl(state_dir()/"post_registry.jsonl")
           if str(row.get("posted_at") or row.get("timestamp") or "") >= cutoff.isoformat()]
    latest={}
    for row in load_snapshots():
        tid=str(row.get("tweet_id") or "")
        if row.get("stage")=="24h" and row.get("status")=="collected": latest[tid]=row
    influenced=[row for row in posts if row.get("radar_influenced") or row.get("xai_signal_used")]
    baseline=[row for row in posts if row not in influenced]
    def mean_impressions(group):
        values=[float(latest[str(row.get("tweet_id"))]["impressions"]) for row in group
                if str(row.get("tweet_id")) in latest and latest[str(row.get("tweet_id"))].get("impressions") is not None]
        return round(sum(values)/len(values),2) if values else None
    usage=usage_summary(days=days); cost=float(usage.get("total_effective_cost_usd") or 0)
    x_imp=mean_impressions(influenced); b_imp=mean_impressions(baseline)
    return {
        "days":days,"xai_influenced_posts":len(influenced),"baseline_posts":len(baseline),
        "xai_mean_24h_impressions":x_imp,"baseline_mean_24h_impressions":b_imp,
        "incremental_impressions":round(x_imp-b_imp,2) if x_imp is not None and b_imp is not None else None,
        "effective_cost_usd":round(cost,6),
        "cost_per_influenced_post_usd":round(cost/len(influenced),6) if influenced else None,
        "cost_per_1000_impressions_usd":round(cost/(x_imp*len(influenced))*1000,6) if x_imp and influenced else None,
        "search_to_post_conversion":round(len(influenced)/max(1,int(usage.get("successful_calls") or 0)*5),4),
        "cache":cache_status(days),
        "budget_recommendation":"review_or_reduce" if cost and not influenced else "keep_and_monitor",
        "data_quality_note":"24h実測のみ。follow conversionは現行X public_metricsでは取得不可。",
    }


def health_check() -> dict:
    now=datetime.now(JST); heartbeat=_json(state_dir()/"daemon_heartbeat.json",{})
    try:
        updated=datetime.fromisoformat(str(heartbeat.get("updated_at"))).astimezone(JST)
        heartbeat_age=round((now-updated).total_seconds()/60,1)
    except (TypeError,ValueError):
        heartbeat_age=None
    pid=heartbeat.get("pid"); process_alive=False
    if pid:
        try:
            if os.name=="nt":
                import ctypes
                handle=ctypes.windll.kernel32.OpenProcess(0x1000,False,int(pid))
                process_alive=bool(handle)
                if handle: ctypes.windll.kernel32.CloseHandle(handle)
            else:
                os.kill(int(pid),0); process_alive=True
        except (OSError,ValueError): pass
    disk=shutil.disk_usage(Path(__file__).anchor)
    from common.metrics_collector import metrics_status
    from common.operations_alerts import self_test
    from common.xai_radar import cache_status, usage_summary
    from fx_alert.monitor import configured_pairs, enabled as fx_enabled
    from fx_alert.providers import get_provider
    fx_provider=get_provider().status(probe=False)
    try:
        from market_data.monitor import market_status
        market_data_status=market_status()
    except Exception as exc:
        market_data_status={"enabled":False,"status":"unavailable","error_type":type(exc).__name__}
    runs=_jsonl(log_dir()/"run_history.jsonl")
    last_success={}
    for row in runs:
        if int(row.get("returncode",0) or 0)==0: last_success[str(row.get("bot","unknown"))]=row.get("finished_at") or row.get("timestamp")
    result={
        "status":"ok","checked_at":now.isoformat(),"daemon":{"heartbeat_age_minutes":heartbeat_age,"process_alive":process_alive,
        "state":heartbeat.get("status"),"pid":pid},"last_success":last_success,
        "disk":{"free_gb":round(disk.free/1024**3,2),"free_percent":round(disk.free/disk.total*100,2)},
        "xai":{"cache":cache_status(),"usage":usage_summary()},"metrics":metrics_status(),"alerts_write":self_test(),
        "fx_alert":{
            "enabled":fx_enabled(),
            "post_enabled":os.getenv("FX_POST_ENABLED","false").lower() in ("1","true","yes"),
            "pairs":configured_pairs(),
            "provider":fx_provider.to_dict(),
        },
        "market_data":market_data_status,
    }
    if not process_alive or heartbeat_age is None or heartbeat_age>10 or disk.free/disk.total<.05 or result["alerts_write"]["status"]!="ok":
        result["status"]="degraded"
    return result


def write_roi_report(days: int = 30) -> Path:
    report=xai_roi_report(days); folder=output_dir("reports"); folder.mkdir(parents=True,exist_ok=True)
    path=folder/f"xai_roi_{datetime.now(JST):%Y-%m-%d}.json"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return path
