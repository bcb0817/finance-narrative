"""Seven-day shadow candidates with deterministic automatic evaluation."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from common.runtime import JST
from .storage import append_jsonl, market_data_dir, read_jsonl, usage_summary


REVIEW_STATUSES = {
    "approved", "rejected", "false_positive", "duplicate", "low_value",
    "unverifiable", "license_blocked", "safety_blocked",
}


@dataclass
class ShadowCandidate:
    candidate_id: str
    detected_at: str
    symbol: str
    asset_type: str
    alert_type: str
    movement_percent: float
    relative_volume: float | None
    volatility_regime: str
    cross_asset_pattern: str
    cause_confidence: str
    supporting_sources: list[str]
    chart_path: str
    draft_text: str
    safety_passed: bool
    publication_rights_passed: bool
    would_post: bool
    blocked_reason: str
    review_status: str = "unverifiable"
    review_reason: str = ""
    threshold_type: str = ""
    discord_notification_status: str = "not_attempted"

    def to_dict(self) -> dict:
        return asdict(self)


def automatic_review(row: dict) -> dict:
    """Classify every shadow candidate without an approval queue."""
    blocked = str(row.get("blocked_reason") or "").lower()
    confidence = str(row.get("cause_confidence") or "unknown").lower()
    if not bool(row.get("safety_passed", False)):
        status, reason = "safety_blocked", "automatic_safety_gate"
    elif "duplicate" in blocked or "cooldown" in blocked:
        status, reason = "duplicate", "automatic_duplicate_or_cooldown_gate"
    elif "low_value" in blocked or "quality" in blocked:
        status, reason = "low_value", "automatic_quality_gate"
    elif not bool(row.get("publication_rights_passed", False)):
        status, reason = "license_blocked", "automatic_license_gate"
    elif confidence not in {"confirmed", "likely"}:
        status, reason = "unverifiable", "automatic_evidence_gate"
    else:
        status, reason = "approved", "all_automatic_gates_passed"
    return {
        **row,
        "would_post": status == "approved",
        "review_status": status,
        "review_reason": reason,
        "reviewer": "automatic_policy",
        "reviewed_at": (
            row.get("reviewed_at") or datetime.now(JST).isoformat()
        ),
        "human_review_required": False,
    }


def create_candidate(movement, *, chart_path: str, draft_text: str, rights_passed: bool,
                     blocked_reason: str, cross_asset_pattern: str = "unknown",
                     cause_confidence: str = "unknown") -> dict:
    raw = f"{movement.movement_id}:{movement.detected_at.isoformat()}:{movement.symbol}"
    candidate = ShadowCandidate(
        candidate_id=hashlib.sha256(raw.encode()).hexdigest()[:16],
        detected_at=movement.detected_at.isoformat(),
        symbol=movement.symbol,
        asset_type=movement.asset_type,
        alert_type=movement.alert_type,
        movement_percent=float(movement.percentage_change),
        relative_volume=movement.relative_volume,
        volatility_regime=(
            "extreme" if abs(movement.z_score) >= 4 else
            "high" if abs(movement.z_score) >= 2.5 else "normal"
        ),
        cross_asset_pattern=cross_asset_pattern,
        cause_confidence=cause_confidence,
        supporting_sources=[],
        chart_path=str(chart_path),
        draft_text=str(draft_text)[:500],
        safety_passed=True,
        publication_rights_passed=rights_passed,
        would_post=bool(rights_passed),
        blocked_reason=blocked_reason or "shadow_mode",
        threshold_type=movement.threshold_type,
    )
    evaluated = automatic_review(candidate.to_dict())
    existing = {row.get("candidate_id") for row in read_jsonl("shadow_candidates.jsonl")}
    if candidate.candidate_id not in existing:
        append_jsonl("shadow_candidates.jsonl", evaluated)
    return evaluated


def list_candidates(*, days: int = 30) -> list[dict]:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    rows = []
    for row in read_jsonl("shadow_candidates.jsonl"):
        try:
            when = datetime.fromisoformat(str(row.get("detected_at")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=JST)
            if when < cutoff:
                continue
        except ValueError:
            continue
        rows.append(automatic_review(row))
    return rows


def show(candidate_id: str) -> dict | None:
    return next((row for row in list_candidates(days=3650) if row.get("candidate_id") == candidate_id), None)


def review(candidate_id: str, status: str, reason: str = "") -> dict:
    candidate = show(candidate_id)
    if not candidate:
        raise KeyError(candidate_id)
    return {
        **automatic_review(candidate),
        "candidate_id": candidate_id,
        "requested_manual_status": str(status),
        "manual_review_accepted": False,
        "reason": "manual_review_disabled",
    }


def report(*, days: int = 7) -> dict:
    rows = list_candidates(days=days)
    statuses = Counter(row.get("review_status", "unverifiable") for row in rows)
    by_symbol = Counter(row.get("symbol", "unknown") for row in rows)
    by_type = Counter(row.get("alert_type", "unknown") for row in rows)
    by_threshold = Counter(row.get("threshold_type", "unknown") for row in rows)
    by_confidence = Counter(row.get("cause_confidence", "unknown") for row in rows)
    evaluated = len(rows)
    false_rate = statuses["false_positive"]/evaluated if evaluated else None
    unverifiable_rate = statuses["unverifiable"]/evaluated if evaluated else None
    credits = usage_summary()
    first = min((datetime.fromisoformat(row["detected_at"]) for row in rows), default=None)
    observed_days = (datetime.now(JST)-first.astimezone(JST)).total_seconds()/86400 if first else 0
    readiness = {
        "observed_7_days": observed_days >= 7,
        "automatic_evaluations_30": evaluated >= 30,
        "false_positive_below_10_percent": false_rate is not None and false_rate < 0.10,
        "unverifiable_below_20_percent": unverifiable_rate is not None and unverifiable_rate < 0.20,
        "license_approved": all(row.get("publication_rights_passed") for row in rows) if rows else False,
    }
    automatic_enable_eligible = all(readiness.values())
    return {
        "days": days, "observed_days": round(observed_days, 2), "detected": len(rows),
        "would_post": sum(bool(row.get("would_post")) for row in rows),
        "review_status": dict(statuses), "by_symbol": dict(by_symbol),
        "by_alert_type": dict(by_type), "by_threshold": dict(by_threshold),
        "by_confidence": dict(by_confidence),
        "chart_success_rate": round(sum(bool(row.get("chart_path")) for row in rows)/len(rows), 4) if rows else None,
        "discord_success_rate": round(sum(row.get("discord_notification_status") == "sent" for row in rows)/len(rows), 4) if rows else None,
        "twelve_data_credits": credits.get("daily_credits"),
        "credits_per_candidate": round(float(credits.get("daily_credits", 0))/len(rows), 3) if rows else None,
        "estimated_posts_per_day": round(len(rows)/max(observed_days, 1), 2),
        "readiness": readiness,
        "human_review_required": False,
        "ready_for_automatic_enable": automatic_enable_eligible,
        "automatic_enable_eligible": automatic_enable_eligible,
    }
