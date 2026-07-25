from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import FxMovement
from .storage import load_state, save_state


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def check_alert_gate(movement: FxMovement, *, now: datetime | None = None) -> GateDecision:
    current = now or datetime.now(timezone.utc)
    state = load_state()
    alerts = [row for row in state.get("alerts", []) if isinstance(row, dict)]
    if any(row.get("movement_id") == movement.movement_id for row in alerts):
        return GateDecision(False, "duplicate_movement")
    recent = []
    for row in alerts:
        when = _parse(str(row.get("timestamp", "")))
        if when:
            recent.append((row, when))
    daily_limit = int(os.getenv("FX_MAX_ALERTS_PER_DAY", os.getenv("FX_DAILY_POST_LIMIT", "6")) or 6)
    # Rolling 24 hours avoids a midnight boundary loophole and timezone-dependent tests.
    if sum(1 for _, when in recent if timedelta(0) <= current - when < timedelta(hours=24)) >= daily_limit:
        return GateDecision(False, "daily_limit")
    if any(current - when < timedelta(hours=1) for _, when in recent):
        return GateDecision(False, "hourly_limit")
    pair_rows = [(row, when) for row, when in recent if row.get("pair") == movement.pair]
    if pair_rows:
        last, when = max(pair_rows, key=lambda item: item[1])
        cooldown = int(os.getenv("FX_ALERT_COOLDOWN_MINUTES", os.getenv("FX_COOLDOWN_MINUTES", "90")) or 90)
        if current - when < timedelta(minutes=cooldown):
            previous_direction = str(last.get("direction", ""))
            previous_change = abs(float(last.get("change_pct", 0.0) or 0.0))
            incremental = abs(movement.change_pct) - previous_change
            re_alert = float(os.getenv("FX_RE_ALERT_ADDITIONAL_PERCENT", "0.50") or 0.50)
            reversal = float(os.getenv("FX_REVERSAL_ALERT_PERCENT", "0.70") or 0.70)
            if previous_direction == movement.direction and incremental < re_alert:
                return GateDecision(False, "same_direction_cooldown")
            if previous_direction != movement.direction and abs(movement.change_pct) < reversal:
                return GateDecision(False, "reversal_cooldown")
    return GateDecision(True, "allowed")


def remember_alert(movement: FxMovement, *, status: str, tweet_id: str = "") -> None:
    state = load_state()
    alerts = [row for row in state.get("alerts", []) if isinstance(row, dict)]
    alerts.append(
        {
            "movement_id": movement.movement_id,
            "pair": movement.pair,
            "timestamp": movement.detected_at.isoformat(),
            "direction": movement.direction,
            "change_yen": movement.change_yen,
            "change_pct": movement.change_pct,
            "status": status,
            "tweet_id": tweet_id,
        }
    )
    state["alerts"] = alerts[-1000:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
