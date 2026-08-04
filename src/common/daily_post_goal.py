"""Daily X-post goal tracking and bounded, reversible volume tuning."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from common.runtime import JST, state_dir


TRUE_VALUES = {"1", "true", "yes", "on"}
TUNABLE_DEFAULTS = {
    "NEWS_IDLE_FALLBACK_HOURS": 3,
    "QUIET_MIN_GAP_MINUTES": 60,
    "QUIET_MAX_GAP_MINUTES": 120,
    "NEWS_POST_VALUE_THRESHOLD": 7,
    "NEWS_RELEVANCE_THRESHOLD": 8,
    "NEWS_BUZZ_THRESHOLD": 8,
    "NEWS_NARRATIVE_THRESHOLD": 8,
    "NEWS_THEME_THRESHOLD": 8,
    "DAILY_POST_LIMIT": 30,
    "HOURLY_POST_LIMIT": 2,
    "X_WRITE_MONTHLY_BUDGET_USD": 15.0,
    "SAFETY_REVIEW_RETRY_LIMIT": 0,
}
TUNABLE_BOUNDS = {
    "NEWS_IDLE_FALLBACK_HOURS": (1, 3),
    "QUIET_MIN_GAP_MINUTES": (30, 60),
    "QUIET_MAX_GAP_MINUTES": (60, 120),
    "NEWS_POST_VALUE_THRESHOLD": (6, 8),
    "NEWS_RELEVANCE_THRESHOLD": (6, 9),
    "NEWS_BUZZ_THRESHOLD": (6, 9),
    "NEWS_NARRATIVE_THRESHOLD": (6, 9),
    "NEWS_THEME_THRESHOLD": (6, 9),
    "DAILY_POST_LIMIT": (30, 40),
    "HOURLY_POST_LIMIT": (2, 4),
    "X_WRITE_MONTHLY_BUDGET_USD": (15.0, 20.0),
    "SAFETY_REVIEW_RETRY_LIMIT": (0, 1),
}
MISSED_STEPS = {
    "NEWS_IDLE_FALLBACK_HOURS": -1,
    "QUIET_MIN_GAP_MINUTES": -10,
    "QUIET_MAX_GAP_MINUTES": -15,
    "NEWS_POST_VALUE_THRESHOLD": -1,
    "NEWS_RELEVANCE_THRESHOLD": -1,
    "NEWS_BUZZ_THRESHOLD": -1,
    "NEWS_NARRATIVE_THRESHOLD": -1,
    "NEWS_THEME_THRESHOLD": -1,
    "DAILY_POST_LIMIT": 2,
    "HOURLY_POST_LIMIT": 1,
    "X_WRITE_MONTHLY_BUDGET_USD": 1.0,
    "SAFETY_REVIEW_RETRY_LIMIT": 1,
}
PROTECTED_CONTROLS = (
    "OPENAI_MONTHLY_BUDGET_USD",
    "XAI_MONTHLY_BUDGET_USD",
    "TWELVEDATA_EXTERNAL_DISPLAY_STATUS",
    "deterministic_safety_check",
    "fact_confirmation",
    "duplicate_prevention",
    "investment_advice_prohibition",
    "license_compliance",
    "api_key_protection",
)


def daily_target() -> int:
    try:
        return max(1, int(os.getenv("DAILY_POST_TARGET", "20") or 20))
    except ValueError:
        return 20


def _learning_dir() -> Path:
    path = state_dir() / "learning"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _policy_path() -> Path:
    return _learning_dir() / "daily_post_goal_policy.json"


def _reviews_path() -> Path:
    return _learning_dir() / "daily_post_goal_reviews.jsonl"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _posted_rows() -> list[dict[str, Any]]:
    value = _read_json(state_dir() / "posted_history.json", [])
    return value if isinstance(value, list) else []


def _parse_posted_at(row: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(row.get("posted_at") or ""))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(JST)


def post_count(day: date) -> int:
    ids: set[str] = set()
    anonymous = 0
    for row in _posted_rows():
        posted = _parse_posted_at(row)
        if posted is None or posted.date() != day:
            continue
        tweet_id = str(row.get("tweet_id") or "").strip()
        if tweet_id:
            ids.add(tweet_id)
        else:
            anonymous += 1
    return len(ids) + anonymous


def _current_values() -> dict[str, float]:
    stored = _read_json(_policy_path(), {})
    values = stored.get("effective_values", {}) if isinstance(stored, dict) else {}
    result: dict[str, float] = {}
    for name, default in TUNABLE_DEFAULTS.items():
        try:
            base = float(os.getenv(name, "") or default)
            result[name] = float(values.get(name, base))
        except (TypeError, ValueError):
            result[name] = float(default)
    return result


def effective_int(name: str, default: int) -> int:
    """Read a runtime value, allowing only the explicit volume-tuning list."""
    if name not in TUNABLE_DEFAULTS:
        try:
            return int(os.getenv(name, "") or default)
        except ValueError:
            return default
    return int(round(_current_values()[name]))


def effective_float(name: str, default: float) -> float:
    if name not in TUNABLE_DEFAULTS:
        try:
            return float(os.getenv(name, "") or default)
        except ValueError:
            return default
    return float(_current_values()[name])


def _reviewed_days() -> set[str]:
    path = _reviews_path()
    if not path.exists():
        return set()
    days: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("completed_day"):
                days.add(str(row["completed_day"]))
    except OSError:
        pass
    return days


def _apply_missed_day_tuning(completed_day: date, shortfall: int) -> dict[str, Any]:
    enabled = os.getenv("DAILY_GOAL_AUTO_TUNE_ENABLED", "true").lower() in TRUE_VALUES
    before = _current_values()
    after = dict(before)
    if enabled:
        for name, step in MISSED_STEPS.items():
            low, high = TUNABLE_BOUNDS[name]
            if name == "DAILY_POST_LIMIT":
                high = float(os.getenv("DAILY_GOAL_DAILY_LIMIT_HARD_MAX", "40") or 40)
            elif name == "HOURLY_POST_LIMIT":
                high = float(os.getenv("DAILY_GOAL_HOURLY_LIMIT_HARD_MAX", "4") or 4)
            elif name == "X_WRITE_MONTHLY_BUDGET_USD":
                high = float(
                    os.getenv("DAILY_GOAL_X_WRITE_BUDGET_HARD_MAX_USD", "20") or 20
                )
            after[name] = min(high, max(low, before[name] + step))
    changed = {
        name: {"before": before[name], "after": after[name]}
        for name in after if before[name] != after[name]
    }
    payload = {
        "updated_at": datetime.now(JST).isoformat(),
        "reason": "daily_post_target_missed",
        "completed_day": completed_day.isoformat(),
        "shortfall": shortfall,
        "effective_values": after,
        "changed": changed,
        "adaptation_tier": min(
            3, int(_read_json(_policy_path(), {}).get("adaptation_tier", 0) or 0) + 1
        ),
        "protected_controls_unchanged": list(PROTECTED_CONTROLS),
        "arbitrary_source_editing": False,
    }
    if enabled:
        _policy_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "status": "applied" if changed else ("at_bounds" if enabled else "disabled"),
        **payload,
    }


def review_daily_goal(
    *, now: datetime | None = None, apply_adjustment: bool = True
) -> dict[str, Any]:
    current = (now or datetime.now(JST)).astimezone(JST)
    target = daily_target()
    completed_day = current.date() - timedelta(days=1)
    completed_count = post_count(completed_day)
    today_count = post_count(current.date())
    elapsed_ratio = (current.hour * 60 + current.minute) / (24 * 60)
    expected_now = min(target, int(target * elapsed_ratio))
    shortfall = max(0, target - completed_count)
    already_reviewed = completed_day.isoformat() in _reviewed_days()
    adjustment: dict[str, Any] = {"status": "not_needed"}
    if shortfall and apply_adjustment and not already_reviewed:
        adjustment = _apply_missed_day_tuning(completed_day, shortfall)
    elif shortfall and already_reviewed:
        adjustment = {"status": "already_applied"}
    result = {
        "status": "achieved" if shortfall == 0 else "missed",
        "target": target,
        "completed_day": completed_day.isoformat(),
        "completed_count": completed_count,
        "achievement_rate": round(completed_count / target, 4),
        "shortfall": shortfall,
        "today": current.date().isoformat(),
        "today_count": today_count,
        "expected_today_by_now": expected_now,
        "today_on_pace": today_count >= expected_now,
        "program_adjustment": adjustment,
        "quality_gates_relaxed": any(
            name.startswith("NEWS_") and name.endswith("_THRESHOLD")
            for name in adjustment.get("changed", {})
        ),
        "post_limits_changed": any(
            name in {"DAILY_POST_LIMIT", "HOURLY_POST_LIMIT"}
            for name in adjustment.get("changed", {})
        ),
        "budgets_changed": "X_WRITE_MONTHLY_BUDGET_USD" in adjustment.get("changed", {}),
        "safety_review_mode": (
            "retry_review"
            if effective_int("SAFETY_REVIEW_RETRY_LIMIT", 0) > 0
            else "standard"
        ),
    }
    if not already_reviewed and apply_adjustment:
        path = _reviews_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def goal_status(now: datetime | None = None) -> dict[str, Any]:
    result = review_daily_goal(now=now, apply_adjustment=False)
    active_policy = _read_json(_policy_path(), {})
    effective = _current_values()
    result["effective_tuning"] = effective
    result["active_policy"] = active_policy if isinstance(active_policy, dict) else {}
    result["quality_gates_relaxed"] = any(
        effective[name] < float(os.getenv(name, "") or TUNABLE_DEFAULTS[name])
        for name in effective
        if name.startswith("NEWS_") and name.endswith("_THRESHOLD")
    )
    result["post_limits_changed"] = any(
        effective[name] != float(os.getenv(name, "") or TUNABLE_DEFAULTS[name])
        for name in ("DAILY_POST_LIMIT", "HOURLY_POST_LIMIT")
    )
    result["budgets_changed"] = (
        effective["X_WRITE_MONTHLY_BUDGET_USD"]
        != float(
            os.getenv("X_WRITE_MONTHLY_BUDGET_USD", "")
            or TUNABLE_DEFAULTS["X_WRITE_MONTHLY_BUDGET_USD"]
        )
    )
    result["auto_tune_enabled"] = (
        os.getenv("DAILY_GOAL_AUTO_TUNE_ENABLED", "true").lower() in TRUE_VALUES
    )
    return result
