from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def fx_dir() -> Path:
    root = Path(os.getenv("STATE_DIR", "data"))
    path = root / "fx"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def append_jsonl(name: str, data: dict[str, Any]) -> Path:
    path = fx_dir() / name
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    return path


def read_jsonl(name: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    path = fx_dir() / name
    if not path.exists():
        return []
    valid: list[dict[str, Any]] = []
    corrupt: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                valid.append(value)
            else:
                corrupt.append(raw)
        except json.JSONDecodeError:
            corrupt.append(raw)
    if corrupt:
        quarantine = fx_dir() / f"quarantine_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        with quarantine.open("a", encoding="utf-8") as handle:
            for raw in corrupt:
                handle.write(
                    json.dumps(
                        {"source": name, "raw": raw, "quarantined_at": datetime.now(timezone.utc).isoformat()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    return valid[-limit:] if limit else valid


def load_state() -> dict[str, Any]:
    path = fx_dir() / "state.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> Path:
    path = fx_dir() / "state.json"
    atomic_write_json(path, state)
    return path


def load_bar_cache(pair: str) -> list[dict[str, Any]]:
    path = fx_dir() / f"bars_cache_{pair}.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_bar_cache(pair: str, rows: list[dict[str, Any]]) -> Path:
    path = fx_dir() / f"bars_cache_{pair}.json"
    atomic_write_json(path, rows[-5000:])
    return path


def provider_usage_summary() -> dict[str, Any]:
    rows = read_jsonl("provider_usage.jsonl")
    now = datetime.now(timezone.utc)
    today = now.date()
    month = now.strftime("%Y-%m")
    daily = 0
    monthly = 0
    errors = 0
    reported_cost = 0.0
    has_reported_cost = False
    for row in rows:
        try:
            when = datetime.fromisoformat(str(row.get("timestamp", "")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when.date() == today:
            daily += 1
        if when.strftime("%Y-%m") == month:
            monthly += 1
        if row.get("status") == "failed":
            errors += 1
        if row.get("reported_cost_usd") is not None:
            reported_cost += float(row.get("reported_cost_usd") or 0)
            has_reported_cost = True
    manual_reported = os.getenv("FX_DATA_REPORTED_MONTHLY_COST_USD", "").strip()
    if manual_reported:
        reported_cost = float(manual_reported)
        has_reported_cost = True
    budget_raw = os.getenv("FX_DATA_MONTHLY_BUDGET_USD", "").strip()
    return {
        "daily_calls": daily,
        "monthly_calls": monthly,
        "errors": errors,
        "plan_name": os.getenv("FX_DATA_PLAN_NAME", "unknown"),
        "estimated_cost_usd": None,
        "estimated_cost_available": False,
        "reported_cost_usd": reported_cost if has_reported_cost else None,
        "monthly_budget_usd": float(budget_raw) if budget_raw else None,
    }


def cleanup(*, retention_days: int = 90) -> int:
    now = datetime.now(timezone.utc)
    retention = {
        "quotes.jsonl": 1,
        "bars.jsonl": 30,
        "provider_usage.jsonl": 180,
        "movements.jsonl": max(retention_days, 1),
        "alerts.jsonl": max(retention_days, 1),
    }
    removed = 0
    for name, days in retention.items():
        path = fx_dir() / name
        if not path.exists():
            continue
        cutoff = now - timedelta(days=days)
        kept: list[dict[str, Any]] = []
        rows = read_jsonl(name)
        for row in rows:
            raw = (
                row.get("timestamp")
                or row.get("detected_at")
                or row.get("received_at")
                or row.get("generated_at")
            )
            try:
                when = datetime.fromisoformat(str(raw))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                kept.append(row)
                continue
            if when >= cutoff:
                kept.append(row)
            else:
                removed += 1
        atomic_write_jsonl(path, kept)
    return removed


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)
