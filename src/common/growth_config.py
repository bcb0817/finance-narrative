"""Effective configuration reporting without exposing secrets."""
from __future__ import annotations
import os
from typing import Any

TRUE_VALUES = {"1", "true", "yes", "on"}


def flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in TRUE_VALUES


def effective_radar_status(schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    schedule = schedule or {}
    configured = flag("XAI_ENABLED") and flag("XAI_X_SEARCH_ENABLED", True)
    schedule_enabled = bool(schedule.get("enabled", False))
    key_configured = bool(os.getenv("XAI_API_KEY", "").strip())
    try:
        from common.xai_radar import usage_summary
    except ImportError:
        from xai_radar import usage_summary
    usage = usage_summary()
    within_budget = usage["remaining_usd"] > 0
    within_daily_limit = usage["daily_calls"] < int(usage.get("daily_limit") or 2)
    effective = all((configured, schedule_enabled, key_configured, within_budget, within_daily_limit))
    reasons = []
    if not configured: reasons.append("feature_flag_disabled")
    if not schedule_enabled: reasons.append("schedule_disabled")
    if not key_configured: reasons.append("api_key_missing")
    if not within_budget: reasons.append("monthly_budget_reached")
    if not within_daily_limit: reasons.append("daily_limit_reached")
    return {"configured_enabled": configured, "schedule_enabled": schedule_enabled,
            "api_key_configured": key_configured, "within_budget": within_budget,
            "within_daily_limit": within_daily_limit, "effective_enabled": effective,
            "disabled_reason": ",".join(reasons) if reasons else "ok"}
