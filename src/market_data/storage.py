from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common.runtime import state_dir


def market_data_dir() -> Path:
    path = state_dir() / "market_data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
    return path


def append_jsonl(name: str, row: dict[str, Any]) -> Path:
    path = market_data_dir() / name
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def read_jsonl(name: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    path = market_data_dir() / name
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    corrupt: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                rows.append(value)
            else:
                corrupt.append(raw)
        except json.JSONDecodeError:
            corrupt.append(raw)
    if corrupt:
        quarantine = market_data_dir() / f"quarantine_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        with quarantine.open("a", encoding="utf-8") as handle:
            for raw in corrupt:
                handle.write(json.dumps({"source": name, "raw": raw}, ensure_ascii=False) + "\n")
    return rows[-limit:] if limit else rows


def load_state() -> dict[str, Any]:
    path = market_data_dir() / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(value: dict[str, Any]) -> Path:
    return atomic_json(market_data_dir() / "state.json", value)


def cache_get(key: str, *, max_age_seconds: int) -> dict[str, Any] | None:
    path = market_data_dir() / "cache" / f"{key}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        stored = datetime.fromisoformat(str(value["stored_at"]))
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - stored).total_seconds() <= max_age_seconds:
            return value.get("value")
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return None


def cache_put(key: str, value: dict[str, Any]) -> Path:
    return atomic_json(
        market_data_dir() / "cache" / f"{key}.json",
        {"stored_at": datetime.now(timezone.utc).isoformat(), "value": value},
    )


def cleanup() -> int:
    retention = {
        "quotes.jsonl": 1,
        "bars_1m.jsonl": 30,
        "bars_5m.jsonl": 90,
        "provider_usage.jsonl": 180,
    }
    now = datetime.now(timezone.utc)
    removed = 0
    for name, days in retention.items():
        rows = read_jsonl(name)
        if not rows:
            continue
        kept = []
        for row in rows:
            raw = row.get("timestamp") or row.get("source_timestamp")
            try:
                when = datetime.fromisoformat(str(raw))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < now - timedelta(days=days):
                    removed += 1
                    continue
            except (TypeError, ValueError):
                pass
            kept.append(row)
        path = market_data_dir() / name
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in kept),
            encoding="utf-8",
        )
        temporary.replace(path)
    return removed


def usage_summary() -> dict[str, Any]:
    rows = read_jsonl("provider_usage.jsonl")
    now = datetime.now(timezone.utc)
    today = now.date()
    current_minute = now.strftime("%Y-%m-%dT%H:%M")
    daily = minute = credits = errors = cache_hits = 0
    credits_after_provider_check = 0
    provider_daily_usage = provider_minute_usage = 0
    provider_checked_at: datetime | None = None
    capability_path = market_data_dir() / "twelve_data_capabilities.json"
    try:
        capability = json.loads(capability_path.read_text(encoding="utf-8"))
        raw_checked = capability.get("checked_at")
        provider_checked_at = datetime.fromisoformat(str(raw_checked))
        if provider_checked_at.tzinfo is None:
            provider_checked_at = provider_checked_at.replace(tzinfo=timezone.utc)
        limits = capability.get("api_limits", {})
        provider_daily_usage = int(limits.get("daily_usage", 0) or 0)
        provider_minute_usage = int(limits.get("current_usage", 0) or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        provider_checked_at = None
    for row in rows:
        try:
            when = datetime.fromisoformat(str(row.get("timestamp", "")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when.date() == today:
            daily += int(row.get("credits_used", 0) or 0)
            if provider_checked_at is not None and when > provider_checked_at:
                credits_after_provider_check += int(row.get("credits_used", 0) or 0)
        if when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M") == current_minute:
            minute += int(row.get("credits_used", 0) or 0)
        credits += int(row.get("credits_used", 0) or 0)
        errors += 0 if row.get("success", False) else 1
        cache_hits += 1 if row.get("cache_hit") else 0
    max_day = int(os.getenv("TWELVEDATA_MAX_CREDITS_PER_DAY", "760") or 760)
    max_minute = int(os.getenv("TWELVEDATA_MAX_CREDITS_PER_MINUTE", "8") or 8)
    global_daily = max(daily, provider_daily_usage + credits_after_provider_check)
    global_minute = minute
    if provider_checked_at and provider_checked_at.strftime("%Y-%m-%dT%H:%M") == current_minute:
        global_minute = max(minute, provider_minute_usage + credits_after_provider_check)
    return {
        "minute_credits": global_minute,
        "daily_credits": global_daily,
        "local_daily_credits": daily,
        "provider_daily_baseline": provider_daily_usage,
        "provider_usage_checked_at": provider_checked_at.isoformat() if provider_checked_at else None,
        "total_recorded_credits": credits,
        "minute_limit": max_minute,
        "daily_limit": max_day,
        "daily_ratio": global_daily / max_day if max_day else 0.0,
        "cache_hits": cache_hits,
        "cache_hit_rate": cache_hits / max(len(rows), 1),
        "errors": errors,
        "estimated_cost_usd": None,
        "estimated_cost_available": False,
    }
