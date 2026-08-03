"""Bridge internal market triggers to independently sourced editorial posts.

Twelve Data is used only to decide *when and what topic* to research.  No
provider price, percentage, direction, chart, or timing value is copied into
this bridge or exposed to the news-generation prompt.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .storage import append_jsonl, read_jsonl


TRUE_VALUES = {"1", "true", "yes", "on"}
ALLOWED_SOURCE_GROUPS = {
    "official_macro",
    "official_regulatory",
    "official_policy",
    "company_filings",
    "market_news",
    "sector_news",
    "crypto_news",
}
SYMBOL_ALIASES = {
    "USD/JPY": ("usd/jpy", "usdjpy", "dollar", "yen", "currency", "foreign exchange", "boj", "fed"),
    "USDJPY": ("usd/jpy", "usdjpy", "dollar", "yen", "currency", "foreign exchange", "boj", "fed"),
    "SPY": ("s&p 500", "s&p500", "sp500", "us stocks", "wall street"),
    "QQQ": ("nasdaq", "nasdaq 100", "technology stocks", "tech stocks"),
    "SMH": ("semiconductor", "chip stocks", "nvidia", "amd", "tsmc", "asml"),
    "NVDA": ("nvda", "nvidia"),
    "AMD": ("amd", "advanced micro devices"),
    "MSFT": ("msft", "microsoft"),
    "GOOGL": ("googl", "google", "alphabet"),
    "TLT": ("treasury", "bond yields", "government bonds", "interest rates"),
    "GLD": ("gold", "bullion"),
    "BTC/USD": ("bitcoin", "btc", "crypto"),
}


def enabled() -> bool:
    return os.getenv("INDEPENDENT_CONFIRMATION_ENABLED", "true").strip().lower() in TRUE_VALUES


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def enqueue_internal_trigger(
    *,
    trigger_id: str,
    symbol: str,
    asset_type: str,
    provider: str = "twelvedata",
    detected_at: datetime | str | None = None,
    movement_window: str = "",
    internal_movement_class: str = "material_movement",
    data_quality: str = "unknown",
) -> dict[str, Any]:
    """Persist a non-numeric research trigger.

    Deliberately excluded: price, percentage, direction, window, volume, and
    chart paths.  Those values remain in the internal monitor's own records.
    """
    if not enabled():
        return {"status": "disabled"}
    detected = (
        detected_at.isoformat()
        if isinstance(detected_at, datetime)
        else str(detected_at or _now().isoformat())
    )
    stable_id = str(trigger_id or "").strip() or hashlib.sha256(
        f"{symbol}|{asset_type}|{detected}".encode("utf-8")
    ).hexdigest()[:20]
    row = {
        "trigger_id": stable_id,
        "created_at": _now().isoformat(),
        "detected_at": detected,
        "symbol": str(symbol or "").upper(),
        "asset_type": str(asset_type or "").lower(),
        "internal_trigger_provider": str(provider or "").lower(),
        "public_fact_source": None,
        "contains_provider_values": False,
        "contains_provider_chart": False,
        "status": "awaiting_independent_source",
    }
    append_jsonl("editorial_triggers.jsonl", row)
    try:
        from .evidence_flow import create_trigger_evidence
        create_trigger_evidence(
            provider=str(provider or "unknown"),
            symbol=str(symbol or ""),
            asset_type=str(asset_type or ""),
            detected_at=detected,
            movement_window=str(movement_window or ""),
            internal_movement_class=str(
                internal_movement_class or "material_movement"
            ),
            data_quality=str(data_quality or "unknown"),
            movement_id=stable_id,
        )
    except Exception:
        # The legacy editorial queue remains usable even if the evidence layer
        # is temporarily unavailable.
        pass
    return {**row, "research_status": row["status"], "status": "queued"}


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _now(parsed)


def recent_triggers(
    *, now: datetime | None = None, hours: int | None = None
) -> list[dict[str, Any]]:
    current = _now(now)
    lookback = max(1, hours or int(os.getenv("INDEPENDENT_CONFIRMATION_LOOKBACK_HOURS", "24")))
    cutoff = current - timedelta(hours=lookback)
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl("editorial_triggers.jsonl", limit=500):
        created = _parse_datetime(row.get("created_at") or row.get("detected_at"))
        if created is None or created < cutoff:
            continue
        key = str(row.get("trigger_id") or "")
        if key:
            latest[key] = row
    return sorted(
        latest.values(),
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )


def _terms(trigger: dict[str, Any]) -> tuple[str, ...]:
    symbol = str(trigger.get("symbol") or "").upper()
    aliases = list(SYMBOL_ALIASES.get(symbol, ()))
    if symbol and symbol not in {"USD/JPY", "USDJPY"}:
        aliases.append(symbol.lower())
    return tuple(dict.fromkeys(term.lower() for term in aliases if term))


def match_candidate(
    item: Any,
    *,
    triggers: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not enabled():
        return None
    source_group = str(getattr(item, "source_group", "") or "")
    if source_group not in ALLOWED_SOURCE_GROUPS:
        return None
    title = str(getattr(item, "title", "") or "")
    lowered = re.sub(r"\s+", " ", title.lower())
    best: tuple[int, dict[str, Any], list[str]] | None = None
    for trigger in triggers if triggers is not None else recent_triggers():
        matched = [term for term in _terms(trigger) if term in lowered]
        if not matched:
            continue
        score = max(len(term) for term in matched) + (
            20 if source_group.startswith("official_") or source_group == "company_filings" else 0
        )
        if best is None or score > best[0]:
            best = (score, trigger, matched)
    if best is None:
        return None
    score, trigger, matched = best
    return {
        "matched": True,
        "match_score": score,
        "trigger_id": trigger.get("trigger_id"),
        "symbol": trigger.get("symbol"),
        "asset_type": trigger.get("asset_type"),
        "internal_trigger_provider": trigger.get("internal_trigger_provider"),
        "matched_terms": matched[:5],
        "independent_source_title": title,
        "independent_source_url": str(getattr(item, "url", "") or ""),
        "independent_source_name": str(getattr(item, "source", "") or ""),
        "independent_source_group": source_group,
        "provider_values_exposed": False,
        "provider_chart_exposed": False,
    }


def prioritize_candidates(items: list[Any]) -> list[Any]:
    triggers = recent_triggers()
    if not triggers:
        return items
    indexed = list(enumerate(items))
    matches = {
        index: match_candidate(item, triggers=triggers)
        for index, item in indexed
    }
    return [
        item
        for index, item in sorted(
            indexed,
            key=lambda pair: (
                -int(bool(matches[pair[0]])),
                -int((matches[pair[0]] or {}).get("match_score") or 0),
                pair[0],
            ),
        )
    ]
