"""Operational alerts with local persistence and optional Discord delivery."""
from __future__ import annotations
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

try: from runtime import JST, output_dir, state_dir, log_dir
except ImportError: from common.runtime import JST, output_dir, state_dir, log_dir

TRUE_VALUES = {"1", "true", "yes", "on"}
DISCORD_HOSTS = {"discord.com", "discordapp.com"}
SECRET_FIELD_RE = re.compile(
    r"(?i)(api[_ -]?key|token|secret|authorization|webhook[_ -]?url)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
SECRET_TOKEN_RE = re.compile(
    r"(?i)(https://(?:discord(?:app)?\.com)/api/webhooks/\d+/)[A-Za-z0-9._-]+"
    r"|\b(?:sk|xai|gho)-[A-Za-z0-9_-]{12,}\b"
)


def _read_json(path: Path, default):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return default


def _jsonl(path: Path) -> list[dict]:
    if not path.exists(): return []
    rows=[]
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row=json.loads(line)
            if isinstance(row,dict): rows.append(row)
        except json.JSONDecodeError: pass
    return rows


def evaluate(now: datetime | None=None) -> list[dict]:
    if os.getenv("ALERTS_ENABLED","true").lower() not in ("1","true","yes"): return []
    now=(now or datetime.now(JST)).astimezone(JST); alerts=[]
    heartbeat=_read_json(state_dir()/"daemon_heartbeat.json",{})
    try: updated=datetime.fromisoformat(heartbeat.get("updated_at","")).astimezone(JST)
    except (ValueError,TypeError): updated=None
    stale=int(os.getenv("ALERT_HEARTBEAT_STALE_MINUTES","10"))
    if not updated or now-updated>timedelta(minutes=stale):
        alerts.append({"code":"heartbeat_stale","severity":"high","detail":f"heartbeat older than {stale} minutes"})
    runs=_jsonl(log_dir()/"run_history.jsonl")
    threshold=int(os.getenv("ALERT_CONSECUTIVE_FAILURE_THRESHOLD","3"))
    by_bot={}
    for row in runs:
        by_bot.setdefault(str(row.get("bot","unknown")),[]).append(row)
    for bot, rows in by_bot.items():
        recent=rows[-threshold:]
        if len(recent)==threshold and all(int(r.get("returncode",0) or 0)!=0 for r in recent):
            alerts.append({"code":"consecutive_failures","severity":"high","bot":bot,"detail":f"{threshold} consecutive failures"})
    for path, code in ((state_dir()/"post_registry.jsonl","post_registry_jsonl_corrupt"),
                       (state_dir()/"metrics_snapshots.jsonl","metrics_jsonl_corrupt")):
        if path.exists():
            for number,line in enumerate(path.read_text(encoding="utf-8",errors="replace").splitlines(),1):
                try: json.loads(line)
                except json.JSONDecodeError:
                    alerts.append({"code":code,"severity":"medium","detail":f"line {number}"}); break
    if os.getenv("FX_ENABLED","true").strip().lower() in TRUE_VALUES:
        try:
            from fx_alert.providers import get_provider
            from fx_alert.storage import load_state as load_fx_state
            provider=get_provider().status(probe=False)
            if not provider.available:
                alerts.append({
                    "code":"fx_provider_unavailable",
                    "severity":"medium",
                    "bot":"fx-alert",
                    "detail":provider.detail,
                })
            quality_health=load_fx_state().get("quality_health",{})
            blocked=int(quality_health.get("consecutive_blocked_runs",0) or 0)
            quality_threshold=int(
                os.getenv("FX_QUALITY_ALERT_CONSECUTIVE_RUNS","3") or 3
            )
            if blocked >= quality_threshold:
                alerts.append({
                    "code":"fx_data_quality_degraded",
                    "severity":"high",
                    "bot":"fx-alert",
                    "detail":(
                        "FX market data failed freshness or quality checks for "
                        f"{quality_threshold} consecutive monitor runs; movement "
                        "detection is unavailable"
                    ),
                })
        except Exception as exc:
            alerts.append({
                "code":"fx_provider_unavailable",
                "severity":"medium",
                "bot":"fx-alert",
                "detail":f"provider status failed: {type(exc).__name__}",
            })
    if os.getenv("MARKET_DATA_ENABLED","true").strip().lower() in TRUE_VALUES:
        if os.getenv("TWELVEDATA_EXTERNAL_DISPLAY_APPROVED","false").strip().lower() not in TRUE_VALUES:
            alerts.append({
                "code":"market_data_external_display_not_approved",
                "severity":"medium",
                "bot":"market-data",
                "detail":"Twelve Data values remain local; external display and X posting are blocked",
            })
        try:
            from market_data.provider import provider_status
            market_provider=provider_status()
            if not market_provider.get("rest_available"):
                alerts.append({
                    "code":"market_data_provider_unavailable",
                    "severity":"medium",
                    "bot":"market-data",
                    "detail":"Twelve Data REST provider is not ready",
                })
            usage=market_provider.get("usage",{})
            ratio=float(usage.get("daily_ratio",0) or 0)
            soft=float(os.getenv("TWELVEDATA_CREDIT_SOFT_LIMIT_PERCENT","80") or 80)/100
            hard=float(os.getenv("TWELVEDATA_CREDIT_HARD_LIMIT_PERCENT","95") or 95)/100
            if ratio >= hard:
                alerts.append({
                    "code":"market_data_credit_hard_limit",
                    "severity":"high","bot":"market-data",
                    "detail":"Twelve Data hard credit limit reached; API reads are stopped",
                })
            elif ratio >= soft:
                alerts.append({
                    "code":"market_data_credit_soft_limit",
                    "severity":"medium","bot":"market-data",
                    "detail":"Twelve Data soft credit limit reached; low-priority reads are stopped",
                })
        except Exception as exc:
            alerts.append({
                "code":"market_data_provider_unavailable",
                "severity":"medium","bot":"market-data",
                "detail":f"provider status failed: {type(exc).__name__}",
            })
    if os.getenv("XAI_ENABLED", "true").strip().lower() in TRUE_VALUES:
        if os.getenv("XAI_SAFE_DISABLED", "false").strip().lower() in TRUE_VALUES:
            alerts.append({
                "code": "xai_safe_disabled",
                "severity": "high",
                "bot": "xai",
                "detail": "xAI is safely disabled by credential or configuration policy",
            })
        if os.getenv("XAI_KEY_ROTATION_VERIFIED", "false").strip().lower() not in TRUE_VALUES:
            alerts.append({
                "code": "xai_key_rotation_verification_required",
                "severity": "high",
                "bot": "xai",
                "detail": "xAI key rotation configuration is not marked verified",
            })
        try:
            from common.xai_social_intelligence import (
                adaptive_cost_policy, budget_status, read_jsonl,
            )
            xai_budget = budget_status()
            if float(xai_budget.get("remaining_usd") or 0) <= float(
                os.getenv("XAI_TARGET_COST_PER_CALL_USD", "0.10") or 0.10
            ):
                alerts.append({
                    "code": "xai_monthly_budget_stop",
                    "severity": "high",
                    "bot": "xai",
                    "detail": "xAI monthly budget reserve reached; xAI calls are stopped",
                })
            policy = adaptive_cost_policy()
            if policy.get("temporary_pause"):
                alerts.append({
                    "code": "xai_adaptive_cost_pause",
                    "severity": "high",
                    "bot": "xai",
                    "detail": "xAI paused after three unusually expensive social research runs",
                })
            recent_runs = read_jsonl("runs.jsonl", limit=1)
            if recent_runs and recent_runs[-1].get("status") == "failed":
                alerts.append({
                    "code": "xai_latest_run_failed",
                    "severity": "medium",
                    "bot": "xai",
                    "detail": (
                        "latest xAI research run failed safely: "
                        f"{recent_runs[-1].get('error_type') or 'unknown'}"
                    ),
                })
            if (
                recent_runs
                and recent_runs[-1].get("status") == "success"
                and recent_runs[-1].get("integration_status") == "failed"
            ):
                alerts.append({
                    "code": "xai_integration_failed",
                    "severity": "medium",
                    "bot": "xai",
                    "detail": (
                        "xAI research succeeded but local integration failed safely: "
                        f"{recent_runs[-1].get('integration_error_type') or 'unknown'}"
                    ),
                })
        except Exception as exc:
            alerts.append({
                "code": "xai_status_unavailable",
                "severity": "medium",
                "bot": "xai",
                "detail": f"xAI status inspection failed: {type(exc).__name__}",
            })
    return [{**row,"detected_at":now.isoformat()} for row in alerts]


def write_alerts(now: datetime | None=None) -> tuple[Path,list[dict]]:
    now=(now or datetime.now(JST)).astimezone(JST); rows=evaluate(now)
    folder=output_dir("alerts"); current=folder/"current_alerts.json"
    folder.mkdir(parents=True,exist_ok=True)
    try:
        _atomic_write(current,json.dumps(rows,ensure_ascii=False,indent=2)+"\n")
    except OSError as exc:
        fallback=log_dir()/"critical_fallback.log"; fallback.parent.mkdir(parents=True,exist_ok=True)
        with fallback.open("a",encoding="utf-8") as handle:
            handle.write(f"{now.isoformat()} alerts_write_failed {type(exc).__name__}\n")
        return fallback,rows
    md=folder/f"{now:%Y-%m-%d}.md"
    lines=[f"# Operations alerts {now:%Y-%m-%d}",""]+[f"- [{r['severity']}] {r['code']}: {r['detail']}" for r in rows]
    if not rows: lines.append("- No active alerts")
    _atomic_write(md,"\n".join(lines)+"\n")
    delivery=send_discord_alerts(rows,now=now)
    _atomic_write(folder/"discord_delivery.json",
                  json.dumps(delivery,ensure_ascii=False,indent=2)+"\n")
    return current,rows


def _atomic_write(path: Path, text: str) -> None:
    descriptor,name=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _discord_state_path() -> Path:
    return state_dir()/"discord_alert_state.json"


def _alert_key(row: dict) -> str:
    stable={
        "code":str(row.get("code") or ""),
        "bot":str(row.get("bot") or ""),
        "detail":str(row.get("detail") or ""),
    }
    raw=json.dumps(stable,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _discord_webhook_url() -> str:
    from urllib.parse import urlparse

    url=os.getenv("DISCORD_WEBHOOK_URL","").strip()
    if not url:
        return ""
    parsed=urlparse(url)
    if parsed.scheme!="https" or parsed.hostname not in DISCORD_HOSTS:
        return ""
    if not parsed.path.startswith("/api/webhooks/"):
        return ""
    return url


def _discord_message(new_rows: list[dict], resolved_count: int, now: datetime) -> str:
    lines=[]
    if new_rows:
        lines.append(f"🚨 finance-narrative: {len(new_rows)}件の新しい運用アラート")
        for row in new_rows[:10]:
            severity=str(row.get("severity") or "unknown").upper()
            bot=f" bot={row['bot']}" if row.get("bot") else ""
            detail=str(row.get("detail") or "")[:300]
            lines.append(f"• [{severity}] {row.get('code','unknown')}{bot}: {detail}")
        if len(new_rows)>10:
            lines.append(f"• ほか {len(new_rows)-10}件")
    if resolved_count:
        lines.append(f"✅ {resolved_count}件のアラートが解消しました")
    lines.append(f"検知時刻: {now:%Y-%m-%d %H:%M:%S} JST")
    return "\n".join(lines)[:1900]


def _allowlist_alert(row: dict) -> dict:
    """Only explicitly approved operational fields may reach Discord."""
    return {
        "code": redact_discord_text(row.get("code") or "unknown")[:120],
        "severity": redact_discord_text(row.get("severity") or "unknown")[:20],
        "bot": redact_discord_text(row.get("bot") or row.get("component") or "")[:80],
        "detail": redact_discord_text(
            row.get("safe_message") or row.get("detail") or ""
        )[:500],
        "error_type": redact_discord_text(row.get("error_type") or "")[:80],
        "first_seen": redact_discord_text(row.get("first_seen") or "")[:80],
        "last_seen": redact_discord_text(row.get("last_seen") or "")[:80],
        "resolved": bool(row.get("resolved", False)),
    }


def redact_discord_text(value: object) -> str:
    """Remove common credentials before any content leaves the machine."""
    text=str(value)
    text=SECRET_FIELD_RE.sub(lambda match:f"{match.group(1)}{match.group(2)}<redacted>",text)
    text=SECRET_TOKEN_RE.sub(
        lambda match:(match.group(1)+"<redacted>") if match.group(1) else "<redacted>",
        text,
    )
    for name, secret in os.environ.items():
        if secret and len(secret)>=12 and any(
            marker in name.upper() for marker in ("KEY","TOKEN","SECRET","WEBHOOK")
        ):
            text=text.replace(secret,"<redacted>")
    return text


def _discord_log_queue_path() -> Path:
    return state_dir()/"discord_log_queue.jsonl"


def queue_discord_log(source: str, value: object, *,
                      level: str="INFO", now: datetime | None=None) -> bool:
    """Durably queue a redacted log entry for batched Discord delivery."""
    if os.getenv("DISCORD_LOGS_ENABLED","false").strip().lower() not in TRUE_VALUES:
        return False
    timestamp=(now or datetime.now(JST)).astimezone(JST).isoformat()
    safe_message=redact_discord_text(value)
    parts=[safe_message[index:index+1400]
           for index in range(0,max(1,len(safe_message)),1400)]
    path=_discord_log_queue_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open("a",encoding="utf-8",newline="\n") as handle:
            for part_number,part in enumerate(parts,1):
                row={
                    "ts":timestamp,
                    "source":redact_discord_text(source)[:80],
                    "level":str(level or "INFO").upper()[:20],
                    "message":part,
                }
                if len(parts)>1:
                    row["part"]=f"{part_number}/{len(parts)}"
                handle.write(json.dumps(row,ensure_ascii=False)+"\n")
        return True
    except OSError:
        return False


def _log_chunks(rows: list[dict], limit: int=1850) -> list[tuple[str,int]]:
    chunks=[]; current=[]; current_count=0
    for row in rows:
        timestamp=str(row.get("ts") or "")[11:19]
        prefix=f"{timestamp} [{row.get('level','INFO')}] {row.get('source','log')}: "
        if row.get("part"):
            prefix+=f"({row['part']}) "
        message=redact_discord_text(row.get("message") or "")
        line=(prefix+message)[:limit]
        candidate="\n".join(current+[line])
        if current and len(candidate)>limit:
            chunks.append(("\n".join(current),current_count))
            current=[]; current_count=0
        current.append(line); current_count+=1
    if current:
        chunks.append(("\n".join(current),current_count))
    return chunks


def flush_discord_logs(*, session=requests, max_batches: int | None=None) -> dict:
    """Send queued logs in bounded batches; unsent rows stay queued."""
    if os.getenv("DISCORD_LOGS_ENABLED","false").strip().lower() not in TRUE_VALUES:
        return {"status":"disabled","sent_batches":0,"sent_rows":0}
    url=_discord_webhook_url()
    if not url:
        return {"status":"configuration_error","sent_batches":0,"sent_rows":0}
    path=_discord_log_queue_path()
    rows=_jsonl(path)
    if not rows:
        return {"status":"empty","sent_batches":0,"sent_rows":0}
    batch_limit=max_batches if max_batches is not None else int(
        os.getenv("DISCORD_LOG_MAX_BATCHES_PER_FLUSH","5") or 5
    )
    chunks=_log_chunks(rows)
    sent_batches=0; sent_rows=0
    for content,row_count in chunks[:max(1,batch_limit)]:
        payload={
            "username":"finance-narrative logs",
            "allowed_mentions":{"parse":[]},
            "content":"```text\n"+content[:1850]+"\n```",
        }
        try:
            response=session.post(url,json=payload,timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            remaining=rows[sent_rows:]
            _atomic_write(path,"".join(json.dumps(row,ensure_ascii=False)+"\n" for row in remaining))
            return {"status":"delivery_failed","error_type":type(exc).__name__,
                    "sent_batches":sent_batches,"sent_rows":sent_rows,
                    "remaining_rows":len(remaining)}
        sent_batches+=1; sent_rows+=row_count
    remaining=rows[sent_rows:]
    _atomic_write(path,"".join(json.dumps(row,ensure_ascii=False)+"\n" for row in remaining))
    return {"status":"sent" if sent_rows else "deferred",
            "sent_batches":sent_batches,"sent_rows":sent_rows,
            "remaining_rows":len(remaining)}


def notify_x_post(record: dict, *, session=requests) -> dict:
    """Notify Discord once for each successfully created X post."""
    if os.getenv("DISCORD_POST_NOTIFICATIONS_ENABLED","false").strip().lower() not in TRUE_VALUES:
        return {"status":"disabled","sent":False}
    url=_discord_webhook_url()
    if not url:
        return {"status":"configuration_error","sent":False}
    tweet_id=str(record.get("tweet_id") or "").strip()
    if not tweet_id:
        return {"status":"invalid_post","sent":False}
    state_path=state_dir()/"discord_post_notifications.json"
    state=_read_json(state_path,{"tweet_ids":[]})
    sent_ids={str(item) for item in state.get("tweet_ids",[])}
    if tweet_id in sent_ids:
        return {"status":"duplicate","sent":False,"tweet_id":tweet_id}
    bot=redact_discord_text(record.get("bot") or "unknown")
    mode=redact_discord_text(record.get("mode") or "")
    text=redact_discord_text(record.get("text") or record.get("title") or "（本文なし）")
    x_url=f"https://x.com/i/web/status/{tweet_id}"
    content=(
        f"📣 Xへ投稿しました\n"
        f"Bot: {bot} / mode: {mode or '-'}\n"
        f"{text[:1450]}\n"
        f"{x_url}"
    )[:1900]
    payload={"username":"finance-narrative posts",
             "allowed_mentions":{"parse":[]},"content":content}
    try:
        response=session.post(url,json=payload,timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        queue_discord_log("discord.post_notification",
                          f"tweet_id={tweet_id} delivery failed: {type(exc).__name__}",
                          level="ERROR")
        return {"status":"delivery_failed","error_type":type(exc).__name__,
                "sent":False,"tweet_id":tweet_id}
    sent_ids.add(tweet_id)
    kept=sorted(sent_ids)[-2000:]
    _atomic_write(state_path,json.dumps(
        {"tweet_ids":kept,"updated_at":datetime.now(JST).isoformat()},
        ensure_ascii=False,indent=2,
    )+"\n")
    return {"status":"sent","sent":True,"tweet_id":tweet_id}


def notify_impression_strategy(payload: dict, *, session=requests) -> dict:
    """Send one concise daily strategy result; never forward raw logs."""
    if os.getenv("DISCORD_ALERTS_ENABLED","false").strip().lower() not in TRUE_VALUES:
        return {"status":"disabled","sent":False}
    url=_discord_webhook_url()
    if not url:
        return {"status":"configuration_error","sent":False}
    strategy_id=redact_discord_text(payload.get("strategy_id") or "")[:40]
    strategy=payload.get("strategy") if isinstance(payload.get("strategy"),dict) else {}
    if not strategy_id:
        return {"status":"invalid_strategy","sent":False}
    state_path=state_dir()/"discord_impression_strategy.json"
    state=_read_json(state_path,{})
    if state.get("strategy_id")==strategy_id:
        return {"status":"duplicate","sent":False,"strategy_id":strategy_id}
    objective=redact_discord_text(strategy.get("objective") or "方針なし")[:500]
    confidence=redact_discord_text(strategy.get("confidence") or "low")[:20]
    focus=[
        redact_discord_text(item)[:240]
        for item in (strategy.get("tomorrow_focus") or [])[:3]
    ]
    lines=[
        "📈 日次インプレッション改善方針を更新しました",
        f"目標: {objective}",
        f"確度: {confidence}",
    ]
    goal=payload.get("daily_goal") if isinstance(payload.get("daily_goal"),dict) else {}
    if goal:
        lines.append(
            f"投稿目標: {int(goal.get('completed_count') or 0)}/"
            f"{int(goal.get('target') or 20)} "
            f"(不足 {int(goal.get('shortfall') or 0)})"
        )
        adjustment=goal.get("program_adjustment") or {}
        lines.append(
            f"自動調整: {redact_discord_text(adjustment.get('status') or 'not_needed')}"
        )
    if focus:
        lines.append("重点:")
        lines.extend(f"• {item}" for item in focus)
    lines.append(
        "適応制御: 閾値・間隔・投稿上限・X予算・安全再審査をハード上限内で変更。"
    )
    lines.append(
        "固定制御: 事実確認・投資助言禁止・重複防止・ライセンス・キー保護。"
    )
    request={
        "username":"finance-narrative review",
        "allowed_mentions":{"parse":[]},
        "content":"\n".join(lines)[:1900],
    }
    try:
        response=session.post(url,json=request,timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        return {"status":"delivery_failed","error_type":type(exc).__name__,
                "sent":False,"strategy_id":strategy_id}
    _atomic_write(state_path,json.dumps({
        "strategy_id":strategy_id,
        "sent_at":datetime.now(JST).isoformat(),
    },ensure_ascii=False,indent=2)+"\n")
    return {"status":"sent","sent":True,"strategy_id":strategy_id}


def notify_xai_research_result(
    run_row: dict,
    observations: list[dict],
    opportunities: list[dict],
    *,
    integrated_analyses: list[dict] | None = None,
    session=requests,
) -> dict:
    """Send one compact research result; raw logs and prompts never leave the host."""
    if os.getenv("DISCORD_XAI_NOTIFICATIONS_ENABLED", "true").strip().lower() not in TRUE_VALUES:
        return {"status": "disabled", "sent": False}
    if os.getenv("DISCORD_ALERTS_ENABLED", "false").strip().lower() not in TRUE_VALUES:
        return {"status": "disabled", "sent": False}
    url = _discord_webhook_url()
    if not url:
        return {"status": "configuration_error", "sent": False}
    run_id = redact_discord_text(run_row.get("run_id") or "")[:64]
    if not run_id:
        return {"status": "invalid_run", "sent": False}
    state_path = state_dir() / "discord_xai_research.json"
    state = _read_json(state_path, {"run_ids": []})
    sent_ids = {str(item) for item in state.get("run_ids", [])}
    if run_id in sent_ids:
        return {"status": "duplicate", "sent": False, "run_id": run_id}

    lines = [
        "🔎 xAI調査結果" if run_row.get("status") == "success" else "⚠️ xAI調査失敗",
        (
            f"mode={redact_discord_text(run_row.get('radar_mode') or '-')}, "
            f"events={int(run_row.get('events_researched') or 0)}, "
            f"cost=${float(run_row.get('cost_usd') or 0):.4f}, "
            f"cache={'hit' if run_row.get('cache_hit') else 'miss'}"
        ),
    ]
    if run_row.get("status") != "success":
        lines.append(
            "停止理由: "
            + redact_discord_text(
                run_row.get("error_type") or run_row.get("failure_stage") or "unknown"
            )[:120]
        )
    if integrated_analyses:
        lines.append("統合分析:")
        for item in integrated_analyses[:3]:
            evidence = item.get("evidence") or {}
            lines.append(
                f"• {redact_discord_text(item.get('integrated_summary') or '')[:220]} "
                f"[根拠={redact_discord_text(evidence.get('quality') or 'low')}, "
                f"events={int(evidence.get('event_count') or 0)}, "
                f"accounts={int(evidence.get('unique_account_count') or 0)}]"
            )
            if item.get("facts_needing_confirmation"):
                lines.append("  判定: 追加確認が必要")
        lines.append(
            f"編集候補={len(opportunities)}件 / 自動投稿なし"
        )
    else:
        for observation in observations[:3]:
            interpretation = observation.get("interpretation") or {}
            summary = (
                interpretation.get("topic_summary")
                or interpretation.get("dominant_narrative")
                or observation.get("event_id")
                or "event"
            )
            lines.append(f"• {redact_discord_text(summary)[:240]}")
        lines.append(f"編集候補={len(opportunities)}件 / 自動投稿なし")
    payload = {
        "username": "finance-narrative xAI",
        "allowed_mentions": {"parse": []},
        "content": "\n".join(lines)[:1900],
    }
    try:
        response = session.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        return {
            "status": "delivery_failed",
            "error_type": type(exc).__name__,
            "sent": False,
            "run_id": run_id,
        }
    sent_ids.add(run_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        state_path,
        json.dumps(
            {
                "run_ids": sorted(sent_ids)[-2000:],
                "updated_at": datetime.now(JST).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return {"status": "sent", "sent": True, "run_id": run_id}


def send_discord_alerts(rows: list[dict], *, now: datetime | None=None,
                        session=requests) -> dict:
    """Send only state changes. A failed delivery is retried on the next run."""
    now=(now or datetime.now(JST)).astimezone(JST)
    if os.getenv("DISCORD_ALERTS_ENABLED","false").strip().lower() not in TRUE_VALUES:
        return {"status":"disabled","sent":False,"checked_at":now.isoformat()}
    url=_discord_webhook_url()
    if not url:
        return {"status":"configuration_error","reason":"webhook_url_missing_or_invalid",
                "sent":False,"checked_at":now.isoformat()}

    state_path=_discord_state_path()
    previous=_read_json(state_path,{})
    previous_keys=set(previous.get("active_keys",[]))
    safe_rows=[_allowlist_alert(row) for row in rows]
    current_by_key={_alert_key(row):row for row in safe_rows}
    current_keys=set(current_by_key)
    new_keys=current_keys-previous_keys
    resolved_keys=previous_keys-current_keys
    if not new_keys and not resolved_keys:
        return {"status":"unchanged","sent":False,"active":len(current_keys),
                "checked_at":now.isoformat()}

    new_rows=[current_by_key[key] for key in sorted(new_keys)]
    payload={
        "username":"finance-narrative alerts",
        "allowed_mentions":{"parse":[]},
        "content":_discord_message(new_rows,len(resolved_keys),now),
    }
    try:
        response=session.post(url,json=payload,timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        return {"status":"delivery_failed","error_type":type(exc).__name__,
                "sent":False,"active":len(current_keys),"checked_at":now.isoformat()}

    state={"active_keys":sorted(current_keys),"updated_at":now.isoformat()}
    state_path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_write(state_path,json.dumps(state,ensure_ascii=False,indent=2)+"\n")
    return {"status":"sent","sent":True,"new":len(new_keys),
            "resolved":len(resolved_keys),"active":len(current_keys),
            "checked_at":now.isoformat()}


def send_discord_preview(code: str, detail: str, *, file_path: str = "",
                         session=requests) -> dict:
    """Send one result preview once, optionally attaching a safe local chart."""
    if os.getenv("DISCORD_ALERTS_ENABLED","false").strip().lower() not in TRUE_VALUES:
        return {"status":"disabled","sent":False}
    url=_discord_webhook_url()
    if not url:
        return {"status":"configuration_error","sent":False}
    stable_code=str(code or "").strip()[:160]
    if not stable_code:
        return {"status":"invalid_preview","sent":False}
    state_path=state_dir()/"discord_preview_notifications.json"
    state=_read_json(state_path,{"codes":[]})
    sent_codes={str(item) for item in state.get("codes",[])}
    if stable_code in sent_codes:
        return {"status":"duplicate","sent":False,"code":stable_code}
    payload={
        "username":"finance-narrative preview",
        "allowed_mentions":{"parse":[]},
        "content":redact_discord_text(detail)[:1900],
    }
    attachment: Path | None=None
    if file_path:
        try:
            candidate=Path(file_path).resolve()
            allowed=output_dir("market_charts").resolve()
            if candidate.is_file() and candidate.suffix.lower()==".png" and (
                candidate==allowed or allowed in candidate.parents
            ):
                attachment=candidate
        except OSError:
            attachment=None
    try:
        if attachment:
            with attachment.open("rb") as handle:
                response=session.post(
                    url,
                    data={"payload_json":json.dumps(payload,ensure_ascii=False)},
                    files={"files[0]":(attachment.name,handle,"image/png")},
                    timeout=20,
                )
        else:
            response=session.post(url,json=payload,timeout=10)
        response.raise_for_status()
    except (OSError, requests.RequestException) as exc:
        return {"status":"delivery_failed","error_type":type(exc).__name__,
                "sent":False,"code":stable_code}
    sent_codes.add(stable_code)
    state_path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_write(state_path,json.dumps({
        "codes":sorted(sent_codes)[-2000:],
        "updated_at":datetime.now(JST).isoformat(),
    },ensure_ascii=False,indent=2)+"\n")
    return {"status":"sent","sent":True,"code":stable_code,
            "attachment_sent":bool(attachment)}


def self_test() -> dict:
    folder=output_dir("alerts"); folder.mkdir(parents=True,exist_ok=True)
    path=folder/f"self-test-{os.getpid()}.tmp"
    try:
        path.write_text("ok",encoding="utf-8")
        readable=path.read_text(encoding="utf-8")=="ok"
        path.unlink()
        discord_enabled=os.getenv("DISCORD_ALERTS_ENABLED","false").strip().lower() in TRUE_VALUES
        return {"status":"ok" if readable else "failed","path":str(folder),
                "discord_enabled":discord_enabled,
                "discord_webhook_configured":bool(_discord_webhook_url()),
                "production_file_untouched":True}
    except OSError as exc:
        try: path.unlink(missing_ok=True)
        except OSError: pass
        return {"status":"failed","error_type":type(exc).__name__,"error":str(exc)[:240],
                "path":str(folder),"production_file_untouched":True}
