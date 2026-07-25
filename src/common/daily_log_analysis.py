"""Daily local log/error analysis with secret redaction and deterministic advice."""
from __future__ import annotations
import json, os, re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
try: from runtime import JST, log_dir, output_dir, state_dir
except ImportError: from common.runtime import JST, log_dir, output_dir, state_dir

SECRET_PATTERNS=(
    re.compile(r"(?i)(api[_ -]?key|token|secret|authorization)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\b(?:sk|xai|gho)-[A-Za-z0-9_-]{12,}\b"),
)


def redact(value: str) -> str:
    text=value
    text=SECRET_PATTERNS[0].sub(lambda m:f"{m.group(1)}{m.group(2)}<redacted>",text)
    return SECRET_PATTERNS[1].sub("<redacted>",text)


def _safe_record(row: dict) -> dict:
    safe={}
    for key,value in row.items():
        if any(word in str(key).lower() for word in ("key","token","secret","authorization")):
            safe[key]="<redacted>"
        elif isinstance(value,dict): safe[key]=_safe_record(value)
        elif isinstance(value,list): safe[key]=[_safe_record(v) if isinstance(v,dict) else redact(str(v)) for v in value]
        elif isinstance(value,str): safe[key]=redact(value)
        else: safe[key]=value
    return safe


def _parse_dt(value: Any) -> datetime | None:
    if not value: return None
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=JST)).astimezone(JST)
    except ValueError: return None


def _jsonl(path: Path, cutoff: datetime) -> tuple[list[dict],int]:
    rows=[]; corrupt=0
    if not path.exists(): return rows,corrupt
    for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
        try: row=json.loads(line)
        except json.JSONDecodeError: corrupt+=1; continue
        if not isinstance(row,dict): continue
        dt=_parse_dt(row.get("ts") or row.get("timestamp") or row.get("started_at"))
        if dt is None or dt>=cutoff: rows.append(row)
    return rows,corrupt


def _category(text: str) -> str:
    lower=text.lower()
    if "401" in lower or "unauthorized" in lower or "authentication" in lower: return "authentication"
    if "429" in lower or "rate limit" in lower: return "rate_limit"
    if "timeout" in lower or "connection" in lower: return "network"
    if "permission" in lower or "access is denied" in lower: return "filesystem"
    if "json" in lower and ("decode" in lower or "corrupt" in lower): return "data_integrity"
    if "budget" in lower or "daily_limit" in lower: return "budget_or_limit"
    if "moderation" in lower or "safety" in lower: return "safety_gate"
    return "application"


ADVICE={
    "authentication":"認証情報の有効性と権限を確認する。キー値はログへ出力しない。",
    "rate_limit":"再試行間隔、日次上限、呼び出し配分を確認する。",
    "network":"接続タイムアウトと再試行後の回復状況を確認する。",
    "filesystem":"対象パス、ACL、他プロセスによるロックを確認する。",
    "data_integrity":"破損行を隔離し、直前の書き込み処理と原子的保存を確認する。",
    "budget_or_limit":"予算・回数制限が意図どおりか確認し、重要時間帯を優先する。",
    "safety_gate":"安全側停止として正常。拒否理由の偏りだけを監視する。",
    "application":"スタックトレースと直前ジョブを確認し、再現テストを追加する。",
}


def analyze_daily_logs(now: datetime | None=None, *, force: bool=False) -> dict:
    now=(now or datetime.now(JST)).astimezone(JST); day=now.date().isoformat(); folder=output_dir("log_analysis")
    json_path=folder/f"{day}.json"; md_path=folder/f"{day}.md"
    if json_path.exists() and not force:
        try: return json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError): pass
    cutoff=now-timedelta(hours=int(os.getenv("DAILY_LOG_ANALYSIS_LOOKBACK_HOURS","24")))
    errors,corrupt_errors=_jsonl(log_dir()/"errors.jsonl",cutoff)
    runs,corrupt_runs=_jsonl(log_dir()/"run_history.jsonl",cutoff)
    openai,corrupt_openai=_jsonl(state_dir()/"openai"/"api_usage.jsonl",cutoff)
    xai,corrupt_xai=_jsonl(state_dir()/"xai"/"api_usage.jsonl",cutoff)
    failure_rows=[]
    for row in errors: failure_rows.append({"source":"errors","detail":json.dumps(_safe_record(row),ensure_ascii=False)[:800]})
    for row in runs:
        if int(row.get("returncode",0) or 0)!=0 or row.get("status") in ("error","failed"):
            failure_rows.append({"source":"run_history","detail":json.dumps(_safe_record(row),ensure_ascii=False)[:800]})
    for source,rows in (("openai",openai),("xai",xai)):
        for row in rows:
            if row.get("success") is False:
                failure_rows.append({"source":source,"detail":json.dumps(_safe_record(row),ensure_ascii=False)[:800]})
    for row in failure_rows: row["category"]=_category(row["detail"])
    counts=Counter(row["category"] for row in failure_rows)
    run_counts=Counter(str(r.get("bot","unknown")) for r in runs)
    failed_runs=sum(int(r.get("returncode",0) or 0)!=0 or r.get("status") in ("error","failed") for r in runs)
    corrupt={"errors.jsonl":corrupt_errors,"run_history.jsonl":corrupt_runs,
             "openai/api_usage.jsonl":corrupt_openai,"xai/api_usage.jsonl":corrupt_xai}
    findings=[{"category":name,"count":count,"severity":"high" if name in ("authentication","data_integrity") else "medium",
               "recommended_action":ADVICE[name]} for name,count in counts.most_common()]
    payload={"status":"attention" if failure_rows or any(corrupt.values()) else "ok","date":day,
             "generated_at":now.isoformat(),"lookback_hours":int(os.getenv("DAILY_LOG_ANALYSIS_LOOKBACK_HOURS","24")),
             "summary":{"runs":len(runs),"failed_runs":failed_runs,"errors":len(failure_rows),
                        "openai_calls":len(openai),"xai_calls":len(xai),"corrupt_jsonl_lines":sum(corrupt.values())},
             "runs_by_bot":dict(run_counts),"findings":findings,"corrupt_lines":corrupt,
             "error_samples":failure_rows[:20],"secrets_redacted":True}
    json_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    lines=[f"# 日次ログ分析 {day}","",f"- 状態: {payload['status']}",f"- 実行: {len(runs)}件（失敗 {failed_runs}件）",
           f"- エラーイベント: {len(failure_rows)}件",f"- JSONL破損行: {sum(corrupt.values())}件","","## 検出事項"]
    lines += [f"- [{r['severity']}] {r['category']}: {r['count']}件 — {r['recommended_action']}" for r in findings]
    if not findings: lines.append("- 重大な異常は検出されませんでした。")
    md_path.write_text("\n".join(lines)+"\n",encoding="utf-8")
    return payload
