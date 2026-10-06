"""Shared posting limits and cost guardrails for every finance bot."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from runtime import JST, state_dir
except ImportError:  # pragma: no cover
    from common.runtime import JST, state_dir


URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
TICKER_RE = re.compile(r"(?<![A-Z0-9])\$?([A-Z]{2,5})(?![A-Z0-9])")
THEMES = {
    "ai": ("AI", "人工知能", "生成AI", "データセンター"),
    "semis": ("半導体", "GPU", "チップ", "TSMC", "NVIDIA"),
    "rates": ("金利", "FRB", "FOMC", "インフレ", "国債", "利回り"),
    "macro": ("雇用", "GDP", "CPI", "景気", "経済指標"),
    "energy": ("原油", "OPEC", "天然ガス", "エネルギー"),
    "crypto": ("Bitcoin", "ビットコイン", "暗号資産", "仮想通貨"),
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _history() -> list[dict]:
    path = state_dir() / "posted_history.json"
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        # Preserve posting limits when one historical record is malformed.
        # Entries are written one-per-indented object, so valid neighbors can
        # be recovered without treating the entire history as empty.
        recovered: list[dict] = []
        try:
            body = raw.strip()[1:-1]
            for chunk in re.split(r"\},\s*\n\s{2}\{", body):
                candidate = "{" + chunk.strip().strip(",").strip("{}").strip() + "}"
                try:
                    entry = json.loads(candidate)
                    if isinstance(entry, dict):
                        recovered.append(entry)
                except json.JSONDecodeError:
                    continue
        except (NameError, OSError):
            pass
        return recovered


def _uncertain_attempts() -> list[dict]:
    """Return live-write attempts whose X response did not include an ID.

    A missing ID is ambiguous: X may have accepted the write even though the
    client failed before reading the response.  These attempts must therefore
    consume the same rate capacity as a confirmed post until they age out.
    """
    path = state_dir() / "uncertain_post_attempts.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in rows if isinstance(row, dict)]


def record_uncertain_attempt(text: str, *, now: datetime | None = None) -> None:
    """Durably reserve rate-limit capacity after an ambiguous live X write."""
    current = now or datetime.now(JST)
    path = state_dir() / "uncertain_post_attempts.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"attempted_at": current.isoformat(), "text": text or ""}, ensure_ascii=False) + "\n")
    except OSError:
        # Logging the failed post remains the fallback; never turn a completed
        # X write into a process error merely because this guard could not log.
        return


def _attempted_at(entry: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(entry.get("attempted_at", "")))
        return value if value.tzinfo else value.replace(tzinfo=JST)
    except ValueError:
        return None


def _posted_at(entry: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(entry.get("posted_at", "")))
        return value if value.tzinfo else value.replace(tzinfo=JST)
    except ValueError:
        return None


def _themes(text: str) -> set[str]:
    lowered = text.casefold()
    return {
        theme for theme, words in THEMES.items()
        if any(word.casefold() in lowered for word in words)
    }


def _tickers(text: str) -> set[str]:
    stop = {"AI", "GDP", "CPI", "FRB", "FOMC", "ETF", "CEO", "USD", "JST"}
    return {m.group(1) for m in TICKER_RE.finditer(text) if m.group(1) not in stop}


def check_post(text: str, *, now: datetime | None = None) -> PolicyDecision:
    """Check a prospective X write against daily, hourly and content limits."""
    from common.daily_post_goal import effective_float, effective_int
    now = now or datetime.now(JST)
    if URL_RE.search(text or ""):
        return PolicyDecision(False, "url_not_allowed")

    entries = [(e, _posted_at(e)) for e in _history()]
    entries = [(e, dt.astimezone(JST)) for e, dt in entries if dt is not None]
    today = [(e, dt) for e, dt in entries if dt.date() == now.date()]
    uncertain = [(e, _attempted_at(e)) for e in _uncertain_attempts()]
    uncertain = [(e, dt.astimezone(JST)) for e, dt in uncertain if dt is not None and dt <= now]
    uncertain_today = [(e, dt) for e, dt in uncertain if dt.date() == now.date()]
    daily_limit = effective_int("DAILY_POST_LIMIT", 30)
    if len(today) + len(uncertain_today) >= daily_limit:
        return PolicyDecision(False, f"daily_limit:{len(today) + len(uncertain_today)}/{daily_limit}")

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_count = sum(1 for _, dt in today if dt >= hour_start) + sum(
        1 for _, dt in uncertain_today if dt >= hour_start
    )
    hourly_limit = effective_int("HOURLY_POST_LIMIT", 2)
    if hour_count >= hourly_limit:
        return PolicyDecision(False, f"hourly_limit:{hour_count}/{hourly_limit}")

    ticker_cooldown = timedelta(minutes=_env_int("TICKER_COOLDOWN_MINUTES", 180))
    theme_cooldown = timedelta(minutes=_env_int("THEME_COOLDOWN_MINUTES", 90))
    new_tickers = _tickers(text)
    new_themes = _themes(text)
    for entry, dt in reversed(entries):
        age = now - dt
        old_text = f"{entry.get('text', '')} {entry.get('title', '')}"
        if age <= ticker_cooldown and new_tickers & _tickers(old_text):
            return PolicyDecision(False, "ticker_cooldown")
        if age <= theme_cooldown and new_themes & _themes(old_text):
            return PolicyDecision(False, "theme_cooldown")

    month_count = sum(1 for _, dt in entries if (dt.year, dt.month) == (now.year, now.month))
    write_cost = _env_float("X_CONTENT_CREATE_USD", 0.015)
    write_budget = effective_float("X_WRITE_MONTHLY_BUDGET_USD", 15.0)
    if (month_count + 1) * write_cost > write_budget:
        return PolicyDecision(False, "monthly_x_write_budget")
    return PolicyDecision(True)


def policy_status(*, now: datetime | None = None) -> dict:
    from common.daily_post_goal import effective_float, effective_int
    now = now or datetime.now(JST)
    from common.editorial_policy import enabled
    if enabled():
        from common.editorial import history, budget_ok
        from common.editorial_delivery import outstanding, capacity_reason
        rows = history()
        pending = outstanding(rows)
        dated = [(r, _posted_at(r).astimezone(JST)) for r in rows + pending]
        today = [r for r, t in dated if t.date() == now.date()]
        month_count = sum(t.year == now.year and t.month == now.month for _, t in dated)
        return {'policy': 'editorial_v2', 'target': None, 'today_count': len(today),
                'month_count': month_count,
                'estimated_x_write_usd': round(month_count * _env_float('X_CONTENT_CREATE_USD', .015), 4),
                'hour_count': sum(now - timedelta(hours=1) < t <= now for _, t in dated),
                'normal_count': sum(not r.get('editorial', {}).get('important_update', False) for r in today),
                'daily_limit': effective_int('DAILY_POST_LIMIT', 10),
                'normal_daily_limit': max(0, min(6, _env_int('EDITORIAL_NORMAL_DAILY_LIMIT', 6))),
                'hourly_limit': effective_int('HOURLY_POST_LIMIT', 2), 'hour_window': 'rolling_60_minutes',
                'normal_min_gap_minutes': max(30, _env_int('EDITORIAL_NORMAL_GAP_MINUTES', 30)),
                'normal_block_reason': capacity_reason(rows, pending, now),
                'breaking_block_reason': capacity_reason(rows, pending, now, breaking=True),
                'openai_budget_stopped': not budget_ok(),
                'monthly_write_budget_usd': effective_float('X_WRITE_MONTHLY_BUDGET_USD', 15.0)}
    dated = [(entry, _posted_at(entry)) for entry in _history()]
    dates = [dt.astimezone(JST) for _entry, dt in dated if dt is not None]
    today_count = sum(1 for dt in dates if dt.date() == now.date())
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_count = sum(1 for dt in dates if dt >= hour_start and dt.date() == now.date())
    month_count = sum(1 for dt in dates if (dt.year, dt.month) == (now.year, now.month))
    unit_cost = _env_float("X_CONTENT_CREATE_USD", 0.015)
    return {
        "today_count": today_count,
        "daily_limit": effective_int("DAILY_POST_LIMIT", 30),
        "hour_count": hour_count,
        "hourly_limit": effective_int("HOURLY_POST_LIMIT", 2),
        "month_count": month_count,
        "estimated_x_write_usd": round(month_count * unit_cost, 4),
        "monthly_write_budget_usd": effective_float("X_WRITE_MONTHLY_BUDGET_USD", 15.0),
    }
