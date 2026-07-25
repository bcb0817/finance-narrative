"""Stage-level metrics observability and rolling success rates."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from common.metrics_collector import STAGES, WINDOW_ENV, load_snapshots
from common.runtime import JST, state_dir
import json
import os


PIPELINE_STAGES = (
    "eligible", "queued", "requested", "fetched", "parsed",
    "validated", "stored", "completed", "missed",
)
MISSED_REASONS = {
    "deadline_passed", "tweet_not_found", "API_error", "rate_limit",
    "auth_error", "parse_error", "validation_error", "storage_error",
    "registry_missing", "duplicate", "unknown",
}


def _dt(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None


def _normalized_status(row: dict) -> str:
    status = str(row.get("pipeline_stage") or row.get("status") or "").lower()
    if status == "collected":
        return "completed"
    if status == "unavailable":
        return "missed"
    return status if status in PIPELINE_STAGES else "unknown"


def _normalized_reason(row: dict) -> str | None:
    raw = str(row.get("reason") or "")
    mapping = {
        "collection_window_expired": "deadline_passed",
        "daemon_unavailable": "deadline_passed",
        "post_missing_or_deleted": "tweet_not_found",
    }
    value = mapping.get(raw, raw)
    return value if value in MISSED_REASONS else ("unknown" if value else None)


def _posts() -> list[dict]:
    try:
        value = json.loads((state_dir() / "posted_history.json").read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def stage_status(*, days: int = 30, now: datetime | None = None) -> dict:
    current = (now or datetime.now(JST)).astimezone(JST)
    cutoff = current - timedelta(days=max(1, days))
    rows = [r for r in load_snapshots() if (_dt(r.get("metrics_collected_at")) or current) >= cutoff]
    result: dict[str, dict] = {}
    all_stage_counts = Counter()
    all_reasons = Counter()
    for stage, _hours in STAGES:
        stage_rows = [r for r in rows if str(r.get("stage")) == stage]
        statuses = Counter(_normalized_status(r) for r in stage_rows)
        reasons = Counter(
            reason for reason in (_normalized_reason(r) for r in stage_rows) if reason
        )
        completed = statuses["completed"]
        missed = statuses["missed"]
        pending = sum(statuses[name] for name in PIPELINE_STAGES[:-2])
        denominator = completed + missed
        result[stage] = {
            "eligible": denominator + pending,
            "completed": completed,
            "missed": missed,
            "pending": pending,
            "success_rate": round(completed / denominator, 4) if denominator else None,
            "stage_counts": dict(statuses),
            "missed_reasons": dict(reasons),
            "api_calls": sum(int(r.get("api_calls", 1 if _normalized_status(r) in {"completed", "missed"} else 0) or 0) for r in stage_rows),
            "retry_count": sum(int(r.get("retry_count", 0) or 0) for r in stage_rows),
        }
        all_stage_counts.update(statuses)
        all_reasons.update(reasons)
    pending_targets = []
    existing = {(str(r.get("tweet_id")), str(r.get("stage"))) for r in rows}
    for post in _posts():
        posted = _dt(post.get("posted_at"))
        if not posted:
            continue
        age_minutes = (current - posted.astimezone(JST)).total_seconds() / 60
        for stage, hours in STAGES:
            key = (str(post.get("tweet_id")), stage)
            if key in existing:
                continue
            end = float(os.getenv(WINDOW_ENV[stage][2], WINDOW_ENV[stage][3]))
            due = posted + timedelta(minutes=end)
            if age_minutes <= end:
                pending_targets.append({
                    "tweet_id": str(post.get("tweet_id")),
                    "stage": stage,
                    "deadline": due.isoformat(),
                    "minutes_until_deadline": round((due-current).total_seconds()/60, 1),
                })
    pending_targets.sort(key=lambda row: row["deadline"])
    total_completed = all_stage_counts["completed"]
    total_missed = all_stage_counts["missed"]
    return {
        "days": days,
        "observation_started_at": min(
            (_dt(r.get("metrics_collected_at")) for r in rows if _dt(r.get("metrics_collected_at"))),
            default=None,
        ),
        "observation_days": round(
            (current - min(
                (_dt(r.get("metrics_collected_at")) for r in rows if _dt(r.get("metrics_collected_at"))),
                default=current,
            )).total_seconds()/86400, 2
        ),
        "by_window": result,
        "overall_success_rate": round(total_completed/(total_completed+total_missed), 4)
        if total_completed+total_missed else None,
        "stage_counts": dict(all_stage_counts),
        "missed_reasons": dict(all_reasons),
        "next_due": pending_targets[:10],
        "oldest_pending": pending_targets[0] if pending_targets else None,
        "target_90_percent_observed": days >= 7 and all(
            row["success_rate"] is not None and row["success_rate"] >= 0.9
            for row in result.values()
        ),
    }


def missed_items(*, limit: int = 100) -> list[dict]:
    rows = []
    for row in reversed(load_snapshots()):
        if _normalized_status(row) == "missed":
            rows.append({**row, "normalized_reason": _normalized_reason(row)})
            if len(rows) >= limit:
                break
    return rows
