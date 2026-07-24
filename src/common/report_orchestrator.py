"""Run daily report subtasks independently and persist explicit partial status."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from common.runtime import JST, output_dir
from common.json_utils import make_json_safe


def _json_safe(value):
    """Compatibility wrapper around the shared bounded JSON normalizer."""
    return make_json_safe(value)


def _run(name, function):
    try:
        value=function()
        status="data_insufficient" if value in (None,[],"") else "success"
        return {"name":name,"status":status,"result":_json_safe(value)}
    except Exception as exc:
        return {"name":name,"status":"failed","error_type":type(exc).__name__,"error":str(exc)[:240]}


def run_daily_report(days: int = 1) -> dict:
    from common.daily_log_analysis import analyze_daily_logs
    from common.operations_alerts import write_alerts
    from common.ops_quality import write_roi_report
    from common.report import build_report
    tasks=[
        _run("daily_log_analysis",analyze_daily_logs),
        _run("performance_report",lambda:build_report(days=days)),
        _run("operations_alerts",write_alerts),
        _run("xai_roi",lambda:write_roi_report(30)),
    ]
    successful=[task for task in tasks if task["status"] in {"success","data_insufficient","skipped"}]
    failed=[task for task in tasks if task["status"]=="failed"]
    overall="failed" if not successful else ("partial_success" if failed else "success")
    exit_code=1 if overall=="failed" else (2 if overall=="partial_success" else 0)
    payload={"date":datetime.now(JST).date().isoformat(),"status":overall,"exit_code":exit_code,"tasks":tasks}
    folder=output_dir("reports"); folder.mkdir(parents=True,exist_ok=True)
    path=folder/f"report_run_status_{payload['date']}.json"
    descriptor,temp=tempfile.mkstemp(prefix=".report-status-",suffix=".tmp",dir=folder)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8") as handle:
            json.dump(make_json_safe(payload),handle,ensure_ascii=False,indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    payload["status_file"]=str(path)
    return payload
