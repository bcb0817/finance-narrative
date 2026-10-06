"""Dynamic S&P 500 top-100 earnings universe and deterministic event matching."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from common.runtime import JST, state_dir

RESULT_TERMS = (
    "announces financial results", "reports financial results",
    "announces quarterly results", "reports quarterly results",
    "announces earnings results", "reports earnings results",
    "earnings beat", "earnings miss", "quarterly earnings",
    "決算発表", "決算速報", "決算を発表",
)

# Defense in depth for transient quote-provider gaps. These companies are
# structurally within the US large-cap top 100; live values replace fallbacks
# whenever available. The list prevents a single 429 from disabling monitoring.
PROTECTED_LARGE_CAPS = {
    "NVDA": "Nvidia", "MSFT": "Microsoft", "AAPL": "Apple Inc.",
    "GOOGL": "Alphabet Inc.", "AMZN": "Amazon", "META": "Meta Platforms",
    "BRK-B": "Berkshire Hathaway", "AVGO": "Broadcom", "TSLA": "Tesla",
    "LLY": "Eli Lilly", "JPM": "JPMorgan Chase", "WMT": "Walmart",
    "V": "Visa", "MA": "Mastercard", "XOM": "ExxonMobil",
    "ORCL": "Oracle", "COST": "Costco", "JNJ": "Johnson & Johnson",
    "NFLX": "Netflix", "AMD": "Advanced Micro Devices",
}


def universe_path() -> Path:
    return state_dir() / "top100_market_cap.json"


def queue_path() -> Path:
    return state_dir() / "top100_earnings_queue.json"


def _company_key(name: str) -> str:
    value = re.sub(r"\([^)]*class\s+[a-z][^)]*\)", "", name.lower())
    value = re.sub(r"\bclass\s+[a-z]\b", "", value)
    value = re.sub(
        r"\b(incorporated|inc|corporation|corp|company|co|plc|ltd)\b\.?,?",
        "", value,
    )
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def save_universe(
    rows: list[dict], *, now: datetime | None = None, require_count: int = 0,
) -> dict:
    valid = []
    for row in rows:
        try:
            market_cap = float(row.get("market_cap") or 0)
        except (TypeError, ValueError):
            continue
        ticker = str(row.get("ticker") or "").upper().strip()
        name = str(row.get("company_name") or "").strip()
        if ticker and name and market_cap > 0:
            valid.append({"rank": 0, "ticker": ticker, "company_name": name,
                          "market_cap": market_cap})
    # Merge the last good snapshot by ticker. A transient quote failure for one
    # mega-cap must not silently remove that company from earnings monitoring.
    current_tickers = {row["ticker"] for row in valid}
    for previous in load_universe():
        ticker = str(previous.get("ticker") or "").upper().strip()
        if ticker and ticker not in current_tickers:
            try:
                market_cap = float(previous.get("market_cap") or 0)
            except (TypeError, ValueError):
                continue
            name = str(previous.get("company_name") or "").strip()
            if name and market_cap > 0:
                valid.append({"rank": 0, "ticker": ticker,
                              "company_name": name, "market_cap": market_cap})
                current_tickers.add(ticker)

    observed_max = max((row["market_cap"] for row in valid), default=1.0)
    for offset, (ticker, name) in enumerate(PROTECTED_LARGE_CAPS.items(), 1):
        if ticker not in current_tickers:
            valid.append({
                "rank": 0, "ticker": ticker, "company_name": name,
                "market_cap": observed_max * max(0.80, 1.0 - offset * 0.005),
                "market_cap_estimated": True,
            })
            current_tickers.add(ticker)

    valid.sort(key=lambda item: item["market_cap"], reverse=True)
    unique = []
    seen_companies = set()
    for row in valid:
        key = _company_key(row["company_name"])
        if not key or key in seen_companies:
            continue
        seen_companies.add(key)
        unique.append(row)
        if len(unique) >= 100:
            break
    valid = unique
    if len(valid) < max(0, int(require_count)):
        raise ValueError(
            f"top100 universe incomplete: {len(valid)}/{int(require_count)}"
        )
    for rank, row in enumerate(valid, 1):
        row["rank"] = rank
    payload = {
        "as_of": (now or datetime.now(JST)).astimezone(JST).isoformat(),
        "method": "S&P 500 constituents ranked by current market capitalization",
        "missing_quote_policy": "retain last good company value by ticker",
        "protected_large_cap_fallback": True,
        "count": len(valid), "companies": valid,
    }
    path = universe_path(); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return payload


def load_universe() -> list[dict]:
    try:
        payload = json.loads(universe_path().read_text(encoding="utf-8"))
        rows = payload.get("companies", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _name_variants(name: str) -> set[str]:
    normalized = _company_key(name)
    return {normalized} if len(normalized) >= 3 else set()


def match_top100_result(title: str) -> dict | None:
    lowered = str(title or "").lower()
    result_pattern = re.search(
        r"\b(reports?|announces?)\s+(?:its\s+)?(?:first|second|third|fourth|q[1-4])[- ]quarter\b",
        lowered,
    )
    if not any(term in lowered for term in RESULT_TERMS) and not result_pattern:
        return None
    for row in load_universe():
        ticker = str(row.get("ticker") or "").lower()
        ticker_hit = bool(ticker and re.search(rf"(?<![a-z0-9])\$?{re.escape(ticker)}(?![a-z0-9])", lowered))
        name_hit = any(name in lowered for name in _name_variants(str(row.get("company_name") or "")))
        if ticker_hit or name_hit:
            return row
    return None


def remember_events(events: list[dict]) -> None:
    try:
        existing = json.loads(queue_path().read_text(encoding="utf-8"))
        existing = existing if isinstance(existing, list) else []
    except (OSError, json.JSONDecodeError):
        existing = []
    def event_key(row: dict) -> str:
        title = str(row.get("title") or "").lower()
        period = re.search(
            r"\b(q[1-4]|first|second|third|fourth)[- ]?(?:quarter|q)?\s*(?:fiscal|fy)?\s*(20\d{2})?",
            title,
        )
        groups = list(period.groups()) if period else []
        if groups:
            groups[0] = {
                "first": "q1", "second": "q2", "third": "q3", "fourth": "q4",
            }.get(groups[0], groups[0])
        label = "-".join(value for value in groups if value)
        return f"{str(row.get('ticker') or '').upper()}:{label or title[:80]}"

    by_event = {event_key(row): row for row in existing if row.get("url")}
    now = datetime.now(JST).isoformat()
    for event in events:
        url = str(event.get("url") or "")
        if url:
            key = event_key(event)
            previous = by_event.get(key, {})
            if int(event.get("priority") or 0) >= int(previous.get("priority") or 0):
                by_event[key] = {
                    **previous, **event, "event_key": key,
                    "detected_at": previous.get("detected_at", now),
                }
    _write_queue(list(by_event.values())[-500:])


def _write_queue(rows: list[dict]) -> None:
    path = queue_path(); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[-500:], ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def pending_events(posted_urls: set[str]) -> list[dict]:
    try:
        rows = json.loads(queue_path().read_text(encoding="utf-8"))
        rows = rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError):
        return []
    pending = [row for row in rows if str(row.get("url") or "") not in posted_urls]
    if len(pending) != len(rows):
        _write_queue(pending)
    return pending


def status(now: datetime | None = None) -> dict:
    """Return an operator-friendly completeness, freshness and queue report."""
    now = (now or datetime.now(JST)).astimezone(JST)
    try:
        payload = json.loads(universe_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    companies = payload.get("companies", [])
    companies = companies if isinstance(companies, list) else []
    try:
        as_of = datetime.fromisoformat(str(payload.get("as_of") or "")).astimezone(JST)
    except (TypeError, ValueError):
        as_of = None
    age_hours = ((now - as_of).total_seconds() / 3600) if as_of else None

    try:
        from news_bot.posted_history import get_posted_urls
        posted_urls = get_posted_urls()
    except Exception:
        posted_urls = set()
    pending = pending_events(posted_urls)
    detected = []
    for row in pending:
        try:
            detected.append(datetime.fromisoformat(str(row.get("detected_at") or "")).astimezone(JST))
        except (TypeError, ValueError):
            continue
    oldest_pending_hours = (
        max(0.0, (now - min(detected)).total_seconds() / 3600) if detected else None
    )
    max_age = float(os.getenv("TOP100_EARNINGS_UNIVERSE_MAX_AGE_HOURS", "48") or 48)
    pending_limit = float(os.getenv("TOP100_EARNINGS_PENDING_ALERT_HOURS", "6") or 6)
    issues = []
    if len(companies) < 100:
        issues.append("universe_incomplete")
    if age_hours is None or age_hours > max_age:
        issues.append("universe_stale")
    if oldest_pending_hours is not None and oldest_pending_hours > pending_limit:
        issues.append("publication_pending_too_long")
    return {
        "health": "active" if not issues else "attention_required",
        "issues": issues,
        "universe": {
            "count": len(companies),
            "as_of": as_of.isoformat() if as_of else None,
            "age_hours": round(age_hours, 2) if age_hours is not None else None,
            "estimated_market_caps": sum(
                1 for row in companies if row.get("market_cap_estimated")
            ),
        },
        "publication_queue": {
            "pending_count": len(pending),
            "oldest_pending_hours": (
                round(oldest_pending_hours, 2)
                if oldest_pending_hours is not None else None
            ),
            "events": [
                {
                    key: row.get(key)
                    for key in ("ticker", "title", "source", "url", "detected_at")
                }
                for row in pending[:20]
            ],
        },
        "thresholds": {
            "universe_max_age_hours": max_age,
            "pending_alert_hours": pending_limit,
        },
        "checked_at": now.isoformat(),
    }
