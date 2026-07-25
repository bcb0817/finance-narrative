from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import MarketMovement
from .storage import load_state, save_state


@dataclass(frozen=True)
class MarketGate:
    allowed: bool
    reason: str


def check_gate(movement: MarketMovement, *, now: datetime | None = None) -> MarketGate:
    current = now or datetime.now(timezone.utc)
    state = load_state()
    alerts = [row for row in state.get("alerts", []) if isinstance(row, dict)]
    if any(row.get("movement_id") == movement.movement_id for row in alerts):
        return MarketGate(False, "duplicate_movement")
    parsed = []
    for row in alerts:
        try:
            when = datetime.fromisoformat(str(row["timestamp"]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            parsed.append((row, when))
        except (KeyError, ValueError):
            continue
    if sum(1 for _, when in parsed if when.astimezone().date() == current.astimezone().date()) >= int(
        os.getenv("MARKET_MAX_ALERTS_PER_DAY", "6") or 6
    ):
        return MarketGate(False, "daily_limit")
    if sum(1 for _, when in parsed if current - when < timedelta(hours=1)) >= int(
        os.getenv("MARKET_MAX_ALERTS_PER_HOUR", "1") or 1
    ):
        return MarketGate(False, "hourly_limit")
    cooldown = int(os.getenv("MARKET_ALERT_COOLDOWN_MINUTES", "90") or 90)
    for row, when in reversed(parsed):
        if row.get("symbol") == movement.symbol and current - when < timedelta(minutes=cooldown):
            return MarketGate(False, "symbol_cooldown")
    return MarketGate(True, "allowed")


def remember(movement: MarketMovement, *, status: str, tweet_id: str = "") -> None:
    state = load_state()
    alerts = [row for row in state.get("alerts", []) if isinstance(row, dict)]
    alerts.append({
        "movement_id": movement.movement_id, "symbol": movement.symbol,
        "timestamp": movement.detected_at.isoformat(), "status": status,
        "tweet_id": tweet_id,
    })
    state["alerts"] = alerts[-2000:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
