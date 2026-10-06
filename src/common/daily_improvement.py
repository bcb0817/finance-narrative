"""Once-per-JST-day improvement loop with bounded, auditable self-healing."""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from pathlib import Path

from common.runtime import JST, log_error, log_run, state_dir


def _state_path() -> Path:
    path = state_dir() / "daily_improvement_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        # State is derived data. Preserve the bad copy for diagnosis, then rebuild it.
        try:
            path.replace(path.with_suffix(f".corrupt-{datetime.now(JST):%Y%m%d%H%M%S}.json"))
        except OSError:
            pass
        return {}


def _write_state(value: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _metrics() -> list[dict]:
    from common.metrics_collector import load_snapshots

    snapshots = load_snapshots()
    try:
        history = json.loads((state_dir() / "posted_history.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        history = []
    posts = {
        str(row.get("tweet_id")): row for row in history
        if isinstance(row, dict) and row.get("tweet_id")
    }
    merged = []
    for snapshot in snapshots:
        post = posts.get(str(snapshot.get("tweet_id") or ""))
        if post:
            merged.append({
                **post, **snapshot,
                "retweets": snapshot.get("reposts", snapshot.get("retweets", 0)),
            })
    staged = [row for row in merged if row.get("stage") == "24h"]
    return staged or merged


def _category(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "json" in text or "decode" in text:
        return "data_integrity"
    if "401" in text or "auth" in text or "credential" in text:
        return "authentication"
    if (
        "429" in text or "rate limit" in text or "ratelimit" in text
        or "daily_limit" in text or "dailylimit" in text or "budget" in text
    ):
        return "budget_or_limit"
    if "permission" in text or "access is denied" in text:
        return "filesystem"
    if "timeout" in text or "connection" in text or "network" in text:
        return "network"
    return "application"


def run_daily_improvement(*, force: bool = False, now: datetime | None = None) -> dict:
    """Execute and verify the loop once daily; retry only safe recovery actions."""
    from common.editorial_policy import enabled
    if enabled():
        from common.editorial_metrics import daily_review
        from common.daily_log_analysis import analyze_daily_logs
        return {**daily_review(), 'log_analysis': analyze_daily_logs(now=now, force=force), 'x_post_called': False}
    from common.daily_log_analysis import analyze_daily_logs
    from common.performance_learning import (
        PRIMARY_OPTIMIZATION_OBJECTIVE, strategy_status, update_daily_learning,
    )

    current = (now or datetime.now(JST)).astimezone(JST)
    day = current.date().isoformat()
    state = _read_state()
    if not force and state.get("date") == day and state.get("status") in {"success", "degraded"}:
        return {**state, "status": "already_completed", "x_post_called": False}

    max_attempts = max(1, min(3, int(os.getenv("DAILY_IMPROVEMENT_MAX_ATTEMPTS", "3") or 3)))
    attempts = []
    result = None
    for attempt in range(1, max_attempts + 1):
        try:
            logs = analyze_daily_logs(now=current, force=force or attempt > 1)
            metrics = _metrics()
            before = strategy_status()
            # If the report job already made today's strategy, this job verifies it
            # instead of paying for a duplicate AI call.
            if before.get("status") == "active" and before.get("date") == day and not force:
                learning = {"status": "verified_existing", "date": day}
            else:
                learning = update_daily_learning(metrics, lookback_hours=24 if attempt == 1 else 48)
            strategy = strategy_status()
            acceptable = strategy.get("status") == "active"
            no_data = learning.get("status") == "skipped" and not metrics
            status = "success" if acceptable else ("degraded" if no_data else "failed")
            attempts.append({
                "attempt": attempt, "status": status,
                "action": "normal" if attempt == 1 else "regenerate_and_expand_lookback",
                "metrics": len(metrics), "learning_status": learning.get("status"),
                "strategy_status": strategy.get("status"),
            })
            if status in {"success", "degraded"}:
                result = {
                    "date": day, "status": status, "completed_at": datetime.now(JST).isoformat(),
                    "primary_objective": PRIMARY_OPTIMIZATION_OBJECTIVE,
                    "attempts": attempts, "log_analysis": {
                        "status": logs.get("status"), "summary": logs.get("summary", {}),
                        "findings": logs.get("findings", []),
                    },
                    "learning": learning, "strategy": strategy, "x_post_called": False,
                }
                break
            raise RuntimeError(f"strategy verification failed: {strategy.get('status', 'missing')}")
        except Exception as exc:  # noqa: BLE001 - boundary must record every failure
            category = _category(exc)
            attempts.append({
                "attempt": attempt, "status": "error", "category": category,
                "error_type": type(exc).__name__, "error": str(exc)[:500],
                "countermeasure": (
                    "generated data is regenerated and the metric lookback is expanded"
                    if category in {"data_integrity", "application", "network"}
                    else "unsafe automatic setting changes are blocked; retry and record"
                ),
            })
            log_error({
                "bot": "daily-improvement", "attempt": attempt,
                "category": category, "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "traceback": traceback.format_exc(limit=8),
                "ts": datetime.now(JST).isoformat(),
            })

    if result is None:
        result = {
            "date": day, "status": "failed", "completed_at": datetime.now(JST).isoformat(),
            "primary_objective": PRIMARY_OPTIMIZATION_OBJECTIVE,
            "attempts": attempts, "x_post_called": False,
        }
    _write_state(result)
    log_run({
        "bot": "daily-improvement", "status": result["status"],
        "returncode": 0 if result["status"] in {"success", "degraded"} else 1,
        "attempts": len(attempts), "started_at": current.isoformat(),
    })
    return result
