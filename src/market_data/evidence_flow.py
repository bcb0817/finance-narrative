"""Evidence-first workflow for market-triggered editorial publication.

The provider trigger is internal.  Public bundles and OpenAI requests contain
only independently published information and never contain provider prices,
returns, volume, or chart paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from common.json_utils import make_json_safe
from common.runtime import JST, output_dir

from .storage import append_jsonl, market_data_dir, read_jsonl


TRUE_VALUES = {"1", "true", "yes", "on"}
FORBIDDEN_PUBLIC_KEYS = {
    "provider_price", "current_price", "start_price", "end_price",
    "percentage_change", "change_pct", "change_yen", "absolute_change",
    "volume", "relative_volume", "high", "low", "chart", "chart_path",
    "api_key", "webhook", "raw_headers",
}
PRICE_CLAIM_RE = re.compile(
    r"(?:急騰|急落|急変|何％|何円|[+-]?\d+(?:\.\d+)?\s*%|"
    r"\d+(?:\.\d+)?\s*(?:円|ドル)\s*(?:上昇|下落|高|安))",
    re.I,
)
INTERVENTION_RE = re.compile(r"(?:為替介入|currency intervention|yen intervention)", re.I)
REFERENCE_ONLY_SOURCES = {"fed_h10", "ecb_reference_rate"}


class CausalConfidence(str, Enum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"


class PublicationMode(str, Enum):
    VERIFIED_EVENT = "verified_event"
    VERIFIED_MARKET_REACTION = "verified_market_reaction"
    CAUSAL_EXPLAINER = "causal_explainer"
    BACKGROUND_EXPLAINER = "background_explainer"
    UNKNOWN_CAUSE = "unknown_cause"
    LOW_VALUE = "low_value"


@dataclass(frozen=True)
class TriggerEvidence:
    provider: str
    symbol: str
    asset_type: str
    detected_at: str
    movement_window: str
    internal_movement_class: str
    data_quality: str
    movement_id: str
    evidence_type: str = "trigger_evidence"


@dataclass(frozen=True)
class EventEvidence:
    evidence_id: str
    source_id: str
    source_type: str
    reliability_tier: int
    official: bool
    title: str
    canonical_url: str
    published_at: str | None
    event_timestamp: str | None
    entity: str
    ticker: str
    event_type: str
    confirmed_facts: list[str]
    source_excerpt: str
    retrieved_at: str
    independence_key: str
    source_purpose: str = "event_confirmation"
    evidence_type: str = "event_evidence"


@dataclass(frozen=True)
class CausalEvidence:
    event_id: str
    movement_id: str
    movement_start_at: str
    event_timestamp: str | None
    published_at: str | None
    retrieved_at: str
    time_distance_minutes: float | None
    event_before_movement: bool
    event_after_movement: bool
    stale_event: bool
    timing_confidence: str
    entity_match: bool
    ticker_match: bool
    event_type_match: bool
    market_relevance: str
    direction_consistency: str
    cross_source_confirmation: int
    supporting_event_ids: list[str]
    alternative_explanations: list[str]
    contradictory_evidence: list[str]
    causal_confidence: str
    causal_claim_allowed: bool
    evidence_type: str = "causal_evidence"


@dataclass(frozen=True)
class PublicationEvidence:
    claim_id: str
    claim_text: str
    evidence_ids: list[str]
    claim_type: str
    factual: bool
    causal: bool
    interpretation: bool
    supported: bool
    publication_allowed: bool
    attribution_required: bool
    evidence_type: str = "publication_evidence"


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in TRUE_VALUES


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    return _utc(parsed)


def _canonical_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
    return urlunsplit(("https" if parts.scheme else "", host, path, "", ""))


def _host(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def _stable_id(*values: Any, length: int = 20) -> str:
    raw = "|".join(str(value or "") for value in values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9一-龯ぁ-んァ-ン]+", " ", value.lower()).strip()


def _similarity(left: str, right: str) -> float:
    a, b = set(_normalize_text(left).split()), set(_normalize_text(right).split())
    return len(a & b) / max(1, len(a | b))


def _latest_rows(name: str, key: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(name, limit=5000):
        identifier = str(row.get(key) or "")
        if identifier:
            latest[identifier] = row
    return latest


def _append_metric(
    event: str, movement_id: str, **values: Any
) -> dict[str, Any]:
    row = {
        "timestamp": _utc().isoformat(),
        "event": event,
        "movement_id": movement_id,
        **make_json_safe(values),
    }
    append_jsonl("trigger_metrics.jsonl", row)
    return row


def create_trigger_evidence(
    *,
    provider: str,
    symbol: str,
    asset_type: str,
    detected_at: datetime | str,
    movement_window: str,
    internal_movement_class: str,
    data_quality: str,
    movement_id: str,
) -> dict[str, Any]:
    detected = (
        detected_at.isoformat()
        if isinstance(detected_at, datetime)
        else str(detected_at)
    )
    evidence = TriggerEvidence(
        provider=str(provider or "unknown").lower(),
        symbol=str(symbol or "").upper(),
        asset_type=str(asset_type or "").lower(),
        detected_at=detected,
        movement_window=str(movement_window or ""),
        internal_movement_class=str(internal_movement_class or "material_movement"),
        data_quality=str(data_quality or "unknown"),
        movement_id=str(movement_id or _stable_id(symbol, detected)),
    )
    row = asdict(evidence)
    existing = _latest_rows("trigger_evidence.jsonl", "movement_id")
    if evidence.movement_id not in existing:
        append_jsonl("trigger_evidence.jsonl", row)
        _append_metric("movement_detected", evidence.movement_id, asset_type=evidence.asset_type)
    enqueue_pending(row)
    return row


def source_route(asset_type: str, symbol: str = "") -> dict[str, Any]:
    asset = str(asset_type or "").lower()
    normalized = str(symbol or "").upper().replace(" ", "")
    if asset in {"forex", "fx"} or normalized in {"USDJPY", "USD/JPY"}:
        return {
            "asset_class": "forex",
            "priority": [
                "boj.or.jp", "mof.go.jp", "federalreserve.gov", "bls.gov",
                "bea.gov", "treasury.gov", "major_financial_media",
            ],
            "supplemental": ["ecb.europa.eu", "x_market_reaction", "fed_h10"],
            "reference_only": ["ecb.europa.eu", "fed_h10"],
        }
    if asset in {"equity", "stock"}:
        return {
            "asset_class": "us_equity",
            "priority": [
                "company_ir", "sec.gov", "exchange_official",
                "regulator", "major_financial_media",
            ],
            "supplemental": [],
            "reference_only": [],
        }
    if asset in {"jp_equity", "japan_equity"}:
        return {
            "asset_class": "japan_equity",
            "priority": [
                "tdnet", "edinet-fsa.go.jp", "company_ir", "jpx.co.jp",
                "government", "major_financial_media",
            ],
            "supplemental": [],
            "reference_only": [],
        }
    if asset in {"etf", "index"}:
        return {
            "asset_class": "index_etf",
            "priority": [
                "central_bank", "government", "economic_release",
                "market_structure", "major_financial_media",
            ],
            "supplemental": [],
            "reference_only": [],
        }
    if asset in {"energy", "oil", "commodity"}:
        return {
            "asset_class": "energy",
            "priority": ["eia.gov", "government", "company_ir", "major_financial_media"],
            "supplemental": [],
            "reference_only": [],
        }
    if asset in {"crypto", "cryptocurrency"}:
        return {
            "asset_class": "crypto",
            "priority": ["regulator", "exchange_official", "company_ir", "major_financial_media"],
            "supplemental": ["specialist_media"],
            "reference_only": [],
        }
    return {
        "asset_class": asset or "unknown",
        "priority": ["official_source", "major_financial_media"],
        "supplemental": [],
        "reference_only": [],
    }


def source_route_rank(item: Any, trigger: dict[str, Any]) -> dict[str, Any]:
    """Rank an available candidate against the configured asset source route."""
    route = source_route(
        str(trigger.get("asset_type") or ""),
        str(trigger.get("symbol") or ""),
    )
    url = str(getattr(item, "url", "") or "")
    text = " ".join([
        _host(url),
        str(getattr(item, "source", "") or ""),
        str(getattr(item, "source_group", "") or ""),
    ]).lower()
    aliases = {
        "official_source": ("official_",),
        "major_financial_media": ("market_news",),
        "company_ir": ("company", "investor relations", "company_filings"),
        "exchange_official": ("exchange", "nasdaq", "nyse", "jpx"),
        "regulator": ("sec.gov", "regulator", "official_regulatory"),
        "government": (".gov", ".go.jp", "government"),
        "economic_release": ("official_macro", "bls.gov", "bea.gov"),
        "central_bank": ("federalreserve.gov", "boj.or.jp", "ecb.europa.eu"),
        "market_structure": ("exchange", "official_regulatory"),
        "specialist_media": ("sector_news", "crypto_news"),
        "tdnet": ("tdnet", "release.tdnet.info"),
    }

    def matches(token: str) -> bool:
        options = aliases.get(token, (token,))
        return any(option.lower() in text for option in options)

    for index, token in enumerate(route["priority"]):
        if matches(token):
            return {
                "route": route["asset_class"],
                "tier": "priority",
                "rank": index,
                "score": 100 - index,
                "matched_route_source": token,
            }
    for index, token in enumerate(route["supplemental"]):
        if matches(token):
            return {
                "route": route["asset_class"],
                "tier": "supplemental",
                "rank": index,
                "score": 40 - index,
                "matched_route_source": token,
            }
    return {
        "route": route["asset_class"],
        "tier": "unrouted",
        "rank": 999,
        "score": 0,
        "matched_route_source": "",
    }


def _source_profile(item: Any) -> dict[str, Any]:
    url = str(getattr(item, "url", "") or "")
    host = _host(url)
    group = str(getattr(item, "source_group", "") or "")
    source = str(getattr(item, "source", "") or "")
    official = group.startswith("official_") or group == "company_filings"
    if official:
        tier = 1
    elif group == "market_news":
        tier = 2
    elif group in {"sector_news", "crypto_news"}:
        tier = 3
    else:
        tier = 4
    lower = f"{source} {host}".lower()
    if "h10" in lower or "h.10" in lower:
        source_id, purpose = "fed_h10", "reference_only"
    elif "ecb" in lower and ("reference" in lower or group == "official_fx"):
        source_id, purpose = "ecb_reference_rate", "reference_only"
    elif "sec.gov" in host:
        source_id, purpose = "sec", "event_confirmation"
    elif "boj.or.jp" in host:
        source_id, purpose = "boj", "event_confirmation"
    elif "mof.go.jp" in host:
        source_id, purpose = "japan_mof", "event_confirmation"
    else:
        source_id, purpose = host or _normalize_text(source), "event_confirmation"
    return {
        "source_id": source_id,
        "source_type": group or "unknown",
        "official": official,
        "reliability_tier": tier,
        "source_purpose": purpose,
        "host": host,
    }


def _infer_event_type(title: str) -> str:
    text = title.lower()
    patterns = (
        ("fx_intervention", ("intervention", "為替介入")),
        ("monetary_policy", ("fomc", "interest rate", "policy rate", "利上げ", "利下げ")),
        ("inflation_release", ("cpi", "ppi", "pce", "inflation")),
        ("employment_release", ("payroll", "jobs", "unemployment", "雇用")),
        ("earnings", ("earnings", "results", "revenue", "profit", "決算")),
        ("guidance", ("guidance", "forecast", "見通し")),
        ("regulatory_filing", ("8-k", "10-q", "10-k", "sec filing")),
        ("energy_release", ("inventory", "eia", "oil", "crude")),
        ("market_structure", ("halt", "suspension", "delist", "取引停止")),
    )
    return next((name for name, words in patterns if any(word in text for word in words)), "generic_news")


def _entity_match(title: str, trigger: dict[str, Any]) -> tuple[bool, bool]:
    symbol = str(trigger.get("symbol") or "").upper()
    text = title.lower()
    aliases = {
        "USD/JPY": ("usd/jpy", "usdjpy", "yen", "dollar", "boj", "fed", "currency"),
        "USDJPY": ("usd/jpy", "usdjpy", "yen", "dollar", "boj", "fed", "currency"),
        "NVDA": ("nvda", "nvidia"),
        "AMD": ("amd", "advanced micro devices"),
        "MSFT": ("msft", "microsoft"),
        "GOOGL": ("googl", "google", "alphabet"),
        "SPY": ("s&p 500", "sp500", "us stocks"),
        "QQQ": ("nasdaq", "nasdaq 100", "tech stocks"),
        "SMH": ("semiconductor", "chip", "nvidia", "amd"),
        "TLT": ("treasury", "bond", "yield"),
        "GLD": ("gold", "bullion"),
        "BTC/USD": ("bitcoin", "btc", "crypto"),
    }
    terms = aliases.get(symbol, (symbol.lower(),))
    exact = bool(symbol and symbol.lower() in text)
    entity = any(term and term in text for term in terms)
    return entity, exact


def build_event_evidence(item: Any, trigger: dict[str, Any]) -> dict[str, Any]:
    title = str(getattr(item, "title", "") or "").strip()
    url = _canonical_url(str(getattr(item, "url", "") or ""))
    published_raw = str(getattr(item, "published", "") or "")
    published = _parse_dt(published_raw)
    profile = _source_profile(item)
    entity_match, ticker_match = _entity_match(title, trigger)
    ticker = str(trigger.get("symbol") or "") if entity_match else ""
    event_type = _infer_event_type(title)
    normalized_headline = _normalize_text(title)
    wire = next(
        (name for name in ("reuters", "associated press", "ap", "bloomberg")
         if name in f"{title} {getattr(item, 'source', '')}".lower()),
        "",
    )
    independence_key = _stable_id(
        wire or profile["host"], normalized_headline, event_type, ticker
    )
    evidence_id = _stable_id(url, title, published_raw, profile["source_id"])
    evidence = EventEvidence(
        evidence_id=evidence_id,
        source_id=profile["source_id"],
        source_type=profile["source_type"],
        reliability_tier=profile["reliability_tier"],
        official=profile["official"],
        title=title,
        canonical_url=url,
        published_at=published.isoformat() if published else None,
        event_timestamp=published.isoformat() if published else None,
        entity=str(trigger.get("symbol") or "") if entity_match else "",
        ticker=ticker,
        event_type=event_type,
        confirmed_facts=[title] if title else [],
        source_excerpt="",
        retrieved_at=_utc().isoformat(),
        independence_key=independence_key,
        source_purpose=profile["source_purpose"],
    )
    row = asdict(evidence)
    existing = _latest_rows("event_evidence.jsonl", "evidence_id")
    if evidence_id not in existing:
        append_jsonl("event_evidence.jsonl", row)
    return row


def source_independence(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(events)
    clusters: list[list[dict[str, Any]]] = []
    for row in rows:
        placed = False
        for cluster in clusters:
            reference = cluster[0]
            same_key = (
                row.get("independence_key")
                and row.get("independence_key") == reference.get("independence_key")
            )
            same_url = (
                row.get("canonical_url")
                and row.get("canonical_url") == reference.get("canonical_url")
            )
            same_content = _similarity(
                str(row.get("title") or ""),
                str(reference.get("title") or ""),
            ) >= 0.85
            if same_key or same_url or same_content:
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    return {
        "raw_source_count": len(rows),
        "independent_source_count": len(clusters),
        "duplicate_republication_count": max(0, len(rows) - len(clusters)),
        "clusters": [
            [str(item.get("evidence_id") or "") for item in cluster]
            for cluster in clusters
        ],
    }


def timestamp_proximity(
    trigger: dict[str, Any],
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    movement = _parse_dt(trigger.get("detected_at"))
    event_time = _parse_dt(event.get("event_timestamp") or event.get("published_at"))
    retrieved = _parse_dt(event.get("retrieved_at")) or _utc(now)
    distance = (
        (event_time - movement).total_seconds() / 60
        if movement and event_time else None
    )
    before_limit = int(os.getenv("CAUSE_EVENT_MAX_BEFORE_MINUTES", "120"))
    after_limit = int(
        os.getenv(
            "CAUSE_NEWS_MAX_AFTER_MINUTES"
            if not event.get("official")
            else "CAUSE_EVENT_MAX_AFTER_MINUTES",
            "60" if not event.get("official") else "30",
        )
    )
    stale_hours = int(os.getenv("CAUSE_STALE_EVENT_HOURS", "24"))
    stale = bool(
        event_time and movement
        and abs((event_time - movement).total_seconds()) > stale_hours * 3600
    )
    if distance is None:
        confidence = "unknown"
    elif stale or distance < -before_limit or distance > after_limit:
        confidence = "low"
    elif -30 <= distance <= 15:
        confidence = "high"
    else:
        confidence = "medium"
    return {
        "movement_start_at": movement.isoformat() if movement else str(trigger.get("detected_at") or ""),
        "event_timestamp": event_time.isoformat() if event_time else None,
        "published_at": event.get("published_at"),
        "retrieved_at": retrieved.isoformat(),
        "time_distance_minutes": round(distance, 2) if distance is not None else None,
        "event_before_movement": bool(distance is not None and distance <= 0),
        "event_after_movement": bool(distance is not None and distance > 0),
        "stale_event": stale,
        "timing_confidence": confidence,
    }


def evaluate_causal_evidence(
    trigger: dict[str, Any],
    event: dict[str, Any],
    *,
    related_events: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    timing = timestamp_proximity(trigger, event)
    entity_match, ticker_match = _entity_match(str(event.get("title") or ""), trigger)
    event_type = str(event.get("event_type") or "generic_news")
    event_type_match = event_type != "generic_news"
    related = [
        row for row in related_events
        if str(row.get("event_type") or "") == event_type
        and _entity_match(str(row.get("title") or ""), trigger)[0]
        and int(row.get("reliability_tier") or 4) <= 2
        and str(row.get("source_purpose") or "") != "reference_only"
        and timestamp_proximity(trigger, row)["timing_confidence"]
        in {"high", "medium"}
        and not timestamp_proximity(trigger, row)["stale_event"]
    ]
    independence = source_independence([event, *related])
    independent_count = independence["independent_source_count"]
    supporting_event_ids = [
        cluster[0] for cluster in independence["clusters"] if cluster
    ]
    alternatives = []
    contradictions = []
    if event_type == "generic_news":
        alternatives.append("specific_event_not_identified")
    if not entity_match:
        alternatives.append("entity_match_weak")
    if timing["timing_confidence"] in {"low", "unknown"}:
        alternatives.append("timing_not_aligned")
    reference_only = str(event.get("source_purpose") or "") == "reference_only"
    official = bool(event.get("official"))
    tier = int(event.get("reliability_tier") or 4)
    intervention = bool(INTERVENTION_RE.search(str(event.get("title") or "")))
    intervention_official = (
        intervention
        and official
        and str(event.get("source_id") or "") in {"boj", "japan_mof"}
    )

    if reference_only or timing["stale_event"] or not entity_match:
        confidence = CausalConfidence.UNKNOWN
    elif intervention and not intervention_official:
        confidence = CausalConfidence.LIKELY if tier <= 2 and event_type_match else CausalConfidence.POSSIBLE
    elif (
        official and event_type_match
        and timing["timing_confidence"] in {"high", "medium"}
        and not contradictions
    ):
        confidence = CausalConfidence.CONFIRMED
    elif (
        independent_count >= 2 and tier <= 2 and event_type_match
        and timing["timing_confidence"] in {"high", "medium"}
    ):
        confidence = CausalConfidence.CONFIRMED
    elif tier <= 2 and event_type_match and timing["timing_confidence"] in {"high", "medium"}:
        confidence = CausalConfidence.LIKELY
    elif entity_match and timing["timing_confidence"] != "low":
        confidence = CausalConfidence.POSSIBLE
    else:
        confidence = CausalConfidence.UNKNOWN

    causal_allowed = confidence in {
        CausalConfidence.CONFIRMED, CausalConfidence.LIKELY
    } and not reference_only and not (intervention and not intervention_official)
    evidence = CausalEvidence(
        event_id=str(event.get("evidence_id") or ""),
        movement_id=str(trigger.get("movement_id") or ""),
        movement_start_at=timing["movement_start_at"],
        event_timestamp=timing["event_timestamp"],
        published_at=timing["published_at"],
        retrieved_at=timing["retrieved_at"],
        time_distance_minutes=timing["time_distance_minutes"],
        event_before_movement=timing["event_before_movement"],
        event_after_movement=timing["event_after_movement"],
        stale_event=timing["stale_event"],
        timing_confidence=timing["timing_confidence"],
        entity_match=entity_match,
        ticker_match=ticker_match,
        event_type_match=event_type_match,
        market_relevance="direct" if entity_match and event_type_match else "related" if entity_match else "weak",
        direction_consistency="not_evaluated_without_public_market_data",
        cross_source_confirmation=independent_count,
        supporting_event_ids=supporting_event_ids,
        alternative_explanations=alternatives,
        contradictory_evidence=contradictions,
        causal_confidence=confidence.value,
        causal_claim_allowed=causal_allowed,
    )
    row = asdict(evidence)
    append_jsonl("causal_evidence.jsonl", row)
    return row


def choose_publication_mode(
    event: dict[str, Any],
    causal: dict[str, Any],
    *,
    public_market_data_rights: bool = False,
) -> str:
    confidence = str(causal.get("causal_confidence") or "unknown")
    if (
        public_market_data_rights
        and confidence in {"confirmed", "likely"}
        and causal.get("causal_claim_allowed")
    ):
        return PublicationMode.VERIFIED_MARKET_REACTION.value
    if confidence == "confirmed" and event.get("official"):
        return PublicationMode.VERIFIED_EVENT.value
    if confidence in {"confirmed", "likely"} and causal.get("causal_claim_allowed"):
        return PublicationMode.CAUSAL_EXPLAINER.value
    if confidence == "possible":
        return PublicationMode.BACKGROUND_EXPLAINER.value
    return PublicationMode.UNKNOWN_CAUSE.value


def _claims_for_events(
    events: list[dict[str, Any]], causal: dict[str, Any]
) -> list[dict[str, Any]]:
    claims: list[PublicationEvidence] = []
    for event in events:
        evidence_id = str(event.get("evidence_id") or "")
        for fact in event.get("confirmed_facts") or []:
            claims.append(PublicationEvidence(
                claim_id=_stable_id(evidence_id, fact),
                claim_text=str(fact),
                evidence_ids=[evidence_id] if evidence_id else [],
                claim_type="event_fact",
                factual=True,
                causal=False,
                interpretation=False,
                supported=bool(evidence_id and fact),
                publication_allowed=bool(evidence_id and fact),
                attribution_required=True,
            ))
    if causal.get("causal_claim_allowed"):
        causal_evidence_ids = [
            str(value) for value in causal.get("supporting_event_ids") or []
            if value
        ]
        claims.append(PublicationEvidence(
            claim_id=_stable_id(*causal_evidence_ids, "causal"),
            claim_text=(
                "背景にはこのイベントがあります"
                if causal.get("causal_confidence") == "confirmed"
                else "このイベントが意識された可能性があります"
            ),
            evidence_ids=causal_evidence_ids,
            claim_type="causal_interpretation",
            factual=False,
            causal=True,
            interpretation=True,
            supported=bool(causal_evidence_ids),
            publication_allowed=bool(causal_evidence_ids),
            attribution_required=True,
        ))
    rows = [asdict(item) for item in claims]
    for row in rows:
        append_jsonl("publication_evidence.jsonl", row)
    return rows


def build_public_evidence_bundle(
    trigger: dict[str, Any],
    event: dict[str, Any],
    causal: dict[str, Any],
) -> dict[str, Any]:
    mode = choose_publication_mode(event, causal, public_market_data_rights=False)
    supporting_ids = [
        str(value) for value in causal.get("supporting_event_ids") or []
        if value
    ]
    event_rows = {
        str(row.get("evidence_id") or ""): row
        for row in read_jsonl("event_evidence.jsonl", limit=500)
    }
    public_events = [
        event_rows[evidence_id]
        for evidence_id in supporting_ids
        if evidence_id in event_rows
    ]
    if str(event.get("evidence_id") or "") not in {
        str(row.get("evidence_id") or "") for row in public_events
    }:
        public_events.append(event)
    claims = _claims_for_events(public_events, causal)
    allowed_claims = [
        row["claim_text"] for row in claims
        if row["supported"] and row["publication_allowed"]
    ]
    bundle = {
        "bundle_id": _stable_id(trigger.get("movement_id"), event.get("evidence_id"), mode),
        "candidate_id": _stable_id(event.get("evidence_id"), mode),
        "event_facts": [
            fact for row in public_events for fact in row.get("confirmed_facts") or []
        ],
        "official_source_titles": [
            row.get("title") for row in public_events
            if row.get("official") and row.get("title")
        ],
        "source_urls": [
            row.get("canonical_url") for row in public_events
            if row.get("canonical_url")
        ],
        "published_times": [
            row.get("published_at") for row in public_events
            if row.get("published_at")
        ],
        "evidence_ids": [
            row.get("evidence_id") for row in public_events
            if row.get("evidence_id")
        ],
        "causal_confidence": causal.get("causal_confidence"),
        "causal_claim_allowed": bool(causal.get("causal_claim_allowed")),
        "allowed_claims": allowed_claims,
        "prohibited_claims": [
            "内部センサー由来の価格・変化率・出来高・チャート",
            "公開可能な価格根拠のない急騰・急落・急変表現",
            "未確認の為替介入",
            "機関投資家の売買を推測する表現",
        ],
        "alternative_interpretations": causal.get("alternative_explanations") or [],
        "freshness": {
            "timing_confidence": causal.get("timing_confidence"),
            "time_distance_minutes": causal.get("time_distance_minutes"),
            "stale_event": bool(causal.get("stale_event")),
        },
        "content_mode": mode,
        "claims": claims,
        "attribution": [{
            "evidence_id": row.get("evidence_id"),
            "source": row.get("source_id"),
            "url": row.get("canonical_url"),
        } for row in public_events],
    }
    validation = validate_public_bundle(bundle)
    bundle["validation"] = validation
    if validation["allowed"]:
        append_jsonl("public_evidence_bundles.jsonl", bundle)
    return bundle


def validate_public_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    found: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower in FORBIDDEN_PUBLIC_KEYS:
                    found.append(f"{path}.{key_lower}".strip("."))
                walk(item, f"{path}.{key_lower}".strip("."))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(bundle)
    claims = list(bundle.get("claims") or [])
    unsupported = [
        str(row.get("claim_id") or "")
        for row in claims
        if (
            (row.get("factual") or row.get("causal"))
            and not row.get("evidence_ids")
        )
    ]
    mode = str(bundle.get("content_mode") or "")
    allowed = not found and not unsupported and mode not in {
        PublicationMode.UNKNOWN_CAUSE.value,
        PublicationMode.LOW_VALUE.value,
    }
    return {
        "allowed": allowed,
        "forbidden_fields": sorted(set(found)),
        "unsupported_claim_ids": unsupported,
        "reason": (
            "public_evidence_only"
            if allowed else
            "forbidden_internal_data" if found else
            "claim_without_evidence" if unsupported else
            "non_publication_mode"
        ),
    }


def validate_structured_output(
    result: dict[str, Any],
    bundle: dict[str, Any],
) -> dict[str, Any]:
    evidence_ids = {str(value) for value in bundle.get("evidence_ids") or []}
    claims = list(result.get("claims") or [])
    unsupported = []
    for claim in claims:
        mapped = {str(value) for value in claim.get("evidence_ids") or []}
        if (claim.get("factual") or claim.get("causal")) and (
            not mapped or not mapped.issubset(evidence_ids)
        ):
            unsupported.append(str(claim.get("claim_text") or ""))
    text = str(result.get("draft_text") or "")
    price_claim = bool(PRICE_CLAIM_RE.search(text))
    intervention_claim = bool(INTERVENTION_RE.search(text))
    intervention_supported = any(
        "intervention" in str(fact).lower() or "為替介入" in str(fact)
        for fact in bundle.get("event_facts") or []
    ) and bundle.get("causal_confidence") == "confirmed"
    allowed = bool(
        bundle.get("validation", {}).get("allowed")
        and not unsupported
        and not price_claim
        and not (intervention_claim and not intervention_supported)
        and not result.get("safety_flags")
        and str(result.get("recommended_mode") or "") == str(bundle.get("content_mode") or "")
    )
    return {
        "allowed": allowed,
        "unsupported_claims": unsupported,
        "unlicensed_market_movement_claim": price_claim,
        "unconfirmed_intervention_claim": intervention_claim and not intervention_supported,
        "reason": "validated" if allowed else "structured_output_rejected",
    }


def generate_structured_publication(
    bundle: dict[str, Any],
    *,
    service: Any | None = None,
) -> dict[str, Any]:
    if not bundle.get("validation", {}).get("allowed"):
        return {
            "status": "rejected",
            "rejection_reason": bundle.get("validation", {}).get("reason"),
        }
    from common.openai_config import OpenAIRole
    from common.openai_service import OpenAIService
    schema = {
        "type": "object",
        "properties": {
            "post_value": {"type": "integer", "minimum": 0, "maximum": 10},
            "recommended_mode": {
                "type": "string",
                "enum": [
                    "verified_event",
                    "verified_market_reaction",
                    "causal_explainer",
                    "background_explainer",
                    "unknown_cause",
                    "low_value",
                ],
            },
            "draft_text": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_text": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "factual": {"type": "boolean"},
                        "causal": {"type": "boolean"},
                    },
                    "required": ["claim_text", "evidence_ids", "factual", "causal"],
                    "additionalProperties": False,
                },
            },
            "evidence_mapping": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_index": {"type": "integer"},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["claim_index", "evidence_ids"],
                    "additionalProperties": False,
                },
            },
            "causal_language": {"type": "string"},
            "uncertainty_statement": {"type": "string"},
            "attribution": {"type": "string"},
            "safety_flags": {"type": "array", "items": {"type": "string"}},
            "rejection_reason": {"type": "string"},
        },
        "required": [
            "post_value", "recommended_mode", "draft_text", "claims",
            "evidence_mapping", "causal_language", "uncertainty_statement",
            "attribution", "safety_flags", "rejection_reason",
        ],
        "additionalProperties": False,
    }
    prompt = (
        "金融ニュース編集者として、次の公開可能な証拠だけからX投稿候補を作成してください。"
        "価格、騰落率、出来高、チャート、入力にない数値を追加しないでください。"
        "全ての事実claimに最低1件のevidence_idを付け、原因確度に対応した日本語を使ってください。"
        "一般論だけならpost_valueを4以下にしてrejection_reasonを記載してください。\n"
        + json.dumps(make_json_safe(bundle), ensure_ascii=False)
    )
    api = service or OpenAIService()
    result = api.structured(
        prompt, schema, role=OpenAIRole.GENERATE,
        operation="market_trigger_publication",
    )
    validation = validate_structured_output(result, bundle)
    return {
        "status": "ready" if validation["allowed"] else "rejected",
        **result,
        "validation": validation,
    }


def evaluate_candidate(item: Any, trigger: dict[str, Any]) -> dict[str, Any]:
    event = build_event_evidence(item, trigger)
    movement_id = str(trigger.get("movement_id") or "")
    linked_event_ids = {
        str(row.get("event_id") or "")
        for row in read_jsonl("causal_evidence.jsonl", limit=500)
        if row.get("movement_id") == movement_id
    }
    prior = [
        row for row in read_jsonl("event_evidence.jsonl", limit=500)
        if (
            row.get("evidence_id") != event.get("evidence_id")
            and str(row.get("evidence_id") or "") in linked_event_ids
        )
    ]
    causal = evaluate_causal_evidence(trigger, event, related_events=prior)
    bundle = build_public_evidence_bundle(trigger, event, causal)
    confidence = str(causal.get("causal_confidence") or "unknown")
    _append_metric(
        "independent_confirmation",
        str(trigger.get("movement_id") or ""),
        causal_confidence=confidence,
        source_type=event.get("source_type"),
        source_reliability=event.get("reliability_tier"),
        evidence_count=causal.get("cross_source_confirmation"),
        posting_mode=bundle.get("content_mode"),
    )
    return {"trigger": trigger, "event": event, "causal": causal, "bundle": bundle}


def _parse_recheck_minutes() -> list[int]:
    raw = os.getenv("MARKET_TRIGGER_RECHECK_MINUTES", "15,30,60")
    values = []
    for part in raw.split(","):
        try:
            value = int(part.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return sorted(set(values)) or [15, 30, 60]


def enqueue_pending(trigger: dict[str, Any]) -> dict[str, Any]:
    if not _flag("MARKET_TRIGGER_RECHECK_ENABLED", True):
        return {"status": "disabled"}
    movement_id = str(trigger.get("movement_id") or "")
    existing = _latest_rows("pending_confirmations.jsonl", "movement_id").get(movement_id)
    if existing and existing.get("status") in {"pending", "confirmed", "likely"}:
        return existing
    detected = _parse_dt(trigger.get("detected_at")) or _utc()
    minutes = _parse_recheck_minutes()
    row = {
        "movement_id": movement_id,
        "asset": trigger.get("symbol"),
        "asset_type": trigger.get("asset_type"),
        "detected_at": detected.isoformat(),
        "next_check_at": detected.isoformat(),
        "attempts": 0,
        "last_result": "not_checked",
        "candidate_sources": [],
        "status": "pending",
        "expires_at": (
            detected + timedelta(
                minutes=int(os.getenv("MARKET_TRIGGER_MAX_AGE_MINUTES", "120"))
            )
        ).isoformat(),
        "recheck_minutes": minutes,
        "updated_at": _utc().isoformat(),
    }
    append_jsonl("pending_confirmations.jsonl", row)
    return row


def pending_confirmations(*, include_terminal: bool = False) -> list[dict[str, Any]]:
    rows = list(_latest_rows("pending_confirmations.jsonl", "movement_id").values())
    if not include_terminal:
        rows = [row for row in rows if row.get("status") == "pending"]
    return sorted(rows, key=lambda row: str(row.get("next_check_at") or ""))


def record_suppression(
    movement_id: str,
    *,
    reason: str,
    confidence: str = "unknown",
    mode: str = "unknown_cause",
    detail: str = "",
) -> dict[str, Any]:
    row = {
        "timestamp": _utc().isoformat(),
        "movement_id": movement_id,
        "reason": reason,
        "causal_confidence": confidence,
        "publication_mode": mode,
        "detail": str(detail)[:300],
    }
    append_jsonl("suppression_log.jsonl", row)
    _append_metric(
        "suppressed",
        movement_id,
        reason=reason,
        causal_confidence=confidence,
        posting_mode=mode,
        detail=str(detail)[:300],
    )
    return row


def record_publication_result(
    movement_id: str,
    *,
    posted: bool,
    mode: str,
    reason: str,
    detection_to_post_seconds: float | None = None,
    post_value: int | None = None,
) -> dict[str, Any]:
    """Record the final publication decision without provider-derived values."""
    event = "publication_posted" if posted else "publication_stopped"
    return _append_metric(
        event,
        movement_id,
        posting_mode=mode,
        reason=reason,
        detection_to_post_seconds=detection_to_post_seconds,
        post_value=post_value,
    )


def _update_pending(row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    attempts = int(row.get("attempts") or 0)
    schedule = list(row.get("recheck_minutes") or _parse_recheck_minutes())
    detected = _parse_dt(row.get("detected_at")) or now
    expires = _parse_dt(row.get("expires_at")) or (
        detected + timedelta(minutes=int(os.getenv("MARKET_TRIGGER_MAX_AGE_MINUTES", "120")))
    )
    if now >= expires:
        row.update({
            "status": "expired",
            "last_result": "no_independent_source_before_expiry",
            "next_check_at": None,
            "updated_at": now.isoformat(),
        })
        record_suppression(
            str(row.get("movement_id") or ""),
            reason="confirmation_expired",
        )
        return row
    if attempts < len(schedule):
        row["next_check_at"] = (detected + timedelta(minutes=schedule[attempts])).isoformat()
    else:
        row["next_check_at"] = expires.isoformat()
    row.update({
        "attempts": attempts + 1,
        "last_result": "no_eligible_independent_source",
        "updated_at": now.isoformat(),
    })
    return row


def process_recheck(
    movement_id: str,
    *,
    dry_run: bool = True,
    candidate_items: Iterable[Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    trigger = _latest_rows("trigger_evidence.jsonl", "movement_id").get(movement_id)
    pending = _latest_rows("pending_confirmations.jsonl", "movement_id").get(movement_id)
    if not trigger or not pending:
        return {"status": "not_found", "movement_id": movement_id}
    if candidate_items is None:
        try:
            from news_bot.news import fetch_news_candidates
            candidate_items = fetch_news_candidates(posted_urls=set(), limit=50)
        except Exception as exc:
            updated = dict(pending)
            updated["last_result"] = f"source_fetch_failed:{type(exc).__name__}"
            updated["updated_at"] = current.isoformat()
            if not dry_run:
                append_jsonl("pending_confirmations.jsonl", updated)
            return {
                "status": "source_fetch_failed",
                "error_type": type(exc).__name__,
                "daemon_safe": True,
            }
    try:
        from .editorial_bridge import match_candidate
        matches = [
            item for item in candidate_items
            if match_candidate(item, triggers=[trigger])
        ]
    except Exception:
        matches = []
    matches.sort(
        key=lambda item: source_route_rank(item, trigger)["score"],
        reverse=True,
    )
    evaluated = [evaluate_candidate(item, trigger) for item in matches[:10]]
    rank = {"confirmed": 4, "likely": 3, "possible": 2, "unknown": 1}
    best = max(
        evaluated,
        key=lambda item: rank.get(str(item["causal"].get("causal_confidence")), 0),
        default=None,
    )
    updated = dict(pending)
    if best:
        confidence = str(best["causal"].get("causal_confidence") or "unknown")
        mode = str(best["bundle"].get("content_mode") or "unknown_cause")
        updated["candidate_sources"] = sorted({
            str(item["event"].get("source_id") or "") for item in evaluated
        })
        updated["last_result"] = confidence
        if confidence in {"confirmed", "likely"}:
            updated["status"] = confidence
            updated["next_check_at"] = None
            _append_metric(
                "confirmation_completed",
                movement_id,
                causal_confidence=confidence,
                posting_mode=mode,
                detection_to_confirmation_seconds=max(
                    0, (current - (_parse_dt(trigger.get("detected_at")) or current)).total_seconds()
                ),
            )
        else:
            updated = _update_pending(updated, now=current)
            updated["last_result"] = confidence
            record_suppression(
                movement_id,
                reason="causal_confidence_insufficient",
                confidence=confidence,
                mode=mode,
            )
    else:
        updated = _update_pending(updated, now=current)
    updated["updated_at"] = current.isoformat()
    if not dry_run:
        append_jsonl("pending_confirmations.jsonl", updated)
        notify_confirmation_result(updated)
    return {
        "status": updated.get("status"),
        "dry_run": dry_run,
        "pending": updated,
        "evaluated_count": len(evaluated),
        "best": best,
        "x_post_attempted": False,
    }


def process_due_rechecks(
    *, dry_run: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    current = _utc(now)
    results = []
    for row in pending_confirmations():
        due = _parse_dt(row.get("next_check_at"))
        if due is not None and due <= current:
            results.append(
                process_recheck(
                    str(row.get("movement_id") or ""),
                    dry_run=dry_run,
                    now=current,
                )
            )
    return {
        "status": "completed",
        "due_count": len(results),
        "results": results,
        "x_post_attempted": False,
    }


def notify_confirmation_result(row: dict[str, Any]) -> dict[str, Any]:
    try:
        from common.operations_alerts import send_discord_alerts
        detail = (
            f"asset={row.get('asset')} "
            f"confirmation={row.get('last_result')} "
            f"recheck={row.get('status')} "
            f"next={row.get('next_check_at') or '-'} "
            f"sources={','.join(row.get('candidate_sources') or []) or '-'}"
        )
        return send_discord_alerts([{
            "code": f"market_confirmation_{row.get('movement_id')}",
            "severity": "info",
            "bot": "market-evidence",
            "safe_message": detail,
        }])
    except Exception as exc:
        return {"status": "failed_safe", "error_type": type(exc).__name__}


def record_later_outcome(
    movement_id: str,
    *,
    later_confirmed_cause: str,
    later_sources: list[str],
    decision_correct: bool | None,
    missed_opportunity: bool,
    false_causal_candidate: bool,
    recommended_rule_change: str = "",
) -> dict[str, Any]:
    original = _latest_rows("causal_evidence.jsonl", "movement_id").get(movement_id, {})
    row = {
        "movement_id": movement_id,
        "original_decision": (
            "suppressed"
            if any(
                item.get("movement_id") == movement_id
                for item in read_jsonl("suppression_log.jsonl", limit=1000)
            )
            else "candidate"
        ),
        "original_confidence": original.get("causal_confidence", "unknown"),
        "later_confirmed_cause": later_confirmed_cause,
        "later_sources": list(later_sources),
        "review_timestamp": _utc().isoformat(),
        "decision_correct": decision_correct,
        "missed_opportunity": bool(missed_opportunity),
        "false_causal_candidate": bool(false_causal_candidate),
        "recommended_rule_change": recommended_rule_change,
    }
    append_jsonl("later_outcome_reviews.jsonl", row)
    _append_metric(
        "later_outcome_review",
        movement_id,
        original_decision=row["original_decision"],
        original_confidence=row["original_confidence"],
        decision_correct=decision_correct,
        missed_opportunity=bool(missed_opportunity),
        false_causal_candidate=bool(false_causal_candidate),
    )
    return row


def evidence_for(movement_id: str) -> dict[str, Any]:
    causal_rows = [
        row for row in read_jsonl("causal_evidence.jsonl")
        if row.get("movement_id") == movement_id
    ]
    linked_event_ids = {
        str(row.get("event_id") or "") for row in causal_rows
        if row.get("event_id")
    }
    event_rows = [
        row for row in read_jsonl("event_evidence.jsonl")
        if str(row.get("evidence_id") or "") in linked_event_ids
    ]
    return {
        "trigger_evidence": [
            row for row in read_jsonl("trigger_evidence.jsonl")
            if row.get("movement_id") == movement_id
        ],
        "event_evidence": event_rows,
        "causal_evidence": causal_rows,
        "publication_evidence": [
            row for row in read_jsonl("publication_evidence.jsonl")
            if any(str(value) in linked_event_ids for value in row.get("evidence_ids") or [])
        ],
        "pending": _latest_rows("pending_confirmations.jsonl", "movement_id").get(movement_id),
        "suppressions": [
            row for row in read_jsonl("suppression_log.jsonl")
            if row.get("movement_id") == movement_id
        ],
    }


def get_trigger(movement_id: str) -> dict[str, Any] | None:
    return _latest_rows("trigger_evidence.jsonl", "movement_id").get(
        str(movement_id or "")
    )


def trigger_status() -> dict[str, Any]:
    triggers = read_jsonl("trigger_evidence.jsonl")
    pending = pending_confirmations(include_terminal=True)
    status_counts = Counter(str(row.get("status") or "unknown") for row in pending)
    confidence_counts = Counter(
        str(row.get("causal_confidence") or "unknown")
        for row in read_jsonl("causal_evidence.jsonl")
    )
    return {
        "enabled": _flag("MARKET_TRIGGER_RECHECK_ENABLED", True),
        "trigger_count": len(triggers),
        "pending_count": status_counts.get("pending", 0),
        "queue_status": dict(status_counts),
        "causal_confidence": {
            value: confidence_counts.get(value, 0)
            for value in ("confirmed", "likely", "possible", "unknown")
        },
        "recheck_minutes": _parse_recheck_minutes(),
        "max_age_minutes": int(os.getenv("MARKET_TRIGGER_MAX_AGE_MINUTES", "120")),
        "public_provider_values_allowed": False,
        "direct_twelve_data_publication_allowed": False,
    }


def confirmation_report(*, days: int = 7) -> dict[str, Any]:
    cutoff = _utc() - timedelta(days=max(1, days))
    metrics = [
        row for row in read_jsonl("trigger_metrics.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    triggers = {str(row.get("movement_id")) for row in metrics if row.get("event") == "movement_detected"}
    confirmations = [row for row in metrics if row.get("event") == "independent_confirmation"]
    completed = [row for row in metrics if row.get("event") == "confirmation_completed"]
    suppressed = [row for row in metrics if row.get("event") == "suppressed"]
    confidence = Counter(str(row.get("causal_confidence") or "unknown") for row in confirmations)
    source = Counter(str(row.get("source_type") or "unknown") for row in confirmations)
    asset = Counter(str(row.get("asset_type") or "unknown") for row in metrics)
    mode = Counter(str(row.get("posting_mode") or "unknown") for row in confirmations)
    confirmation_seconds = [
        float(row["detection_to_confirmation_seconds"])
        for row in completed if row.get("detection_to_confirmation_seconds") is not None
    ]
    later = [
        row for row in read_jsonl("later_outcome_reviews.jsonl")
        if (_parse_dt(row.get("review_timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    return {
        "days": days,
        "movement_detected_count": len(triggers),
        "independent_confirmation_count": len(confirmations),
        "confirmation_rate": len(completed) / max(1, len(triggers)),
        "confirmed_count": confidence["confirmed"],
        "likely_count": confidence["likely"],
        "possible_count": confidence["possible"],
        "unknown_count": confidence["unknown"],
        "no_source_count": sum(
            row.get("reason") in {"confirmation_expired", "no_independent_source"}
            for row in read_jsonl("suppression_log.jsonl")
        ),
        "source_found_later_count": sum(bool(row.get("later_confirmed_cause")) for row in later),
        "expired_count": sum(
            row.get("status") == "expired" for row in pending_confirmations(include_terminal=True)
        ),
        "posted_count": sum(
            row.get("event") == "publication_posted" for row in metrics
        ),
        "suppressed_count": len(suppressed),
        "suppression_reason": dict(Counter(str(row.get("reason") or "unknown") for row in suppressed)),
        "average_confirmation_seconds": (
            sum(confirmation_seconds) / len(confirmation_seconds)
            if confirmation_seconds else None
        ),
        "source_type": dict(source),
        "asset_type": dict(asset),
        "posting_mode": dict(mode),
        "later_outcome_reviews": len(later),
        "false_causal_candidates": sum(bool(row.get("false_causal_candidate")) for row in later),
    }


def suppressed_report(*, hours: int = 24) -> dict[str, Any]:
    cutoff = _utc() - timedelta(hours=max(1, hours))
    rows = [
        row for row in read_jsonl("suppression_log.jsonl")
        if (
            _parse_dt(row.get("timestamp"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ) >= cutoff
    ]
    return {
        "hours": hours,
        "count": len(rows),
        "by_reason": dict(
            Counter(str(row.get("reason") or "unknown") for row in rows)
        ),
        "rows": rows,
    }


def source_report(*, days: int = 7) -> dict[str, Any]:
    report = confirmation_report(days=days)
    return {
        "days": days,
        "source_counts": report["source_type"],
        "asset_counts": report["asset_type"],
        "reference_only_sources": sorted(REFERENCE_ONLY_SOURCES),
        "routes": {
            name: source_route(name)
            for name in ("forex", "equity", "jp_equity", "etf", "energy", "crypto")
        },
    }


def later_review_report(*, days: int = 7) -> dict[str, Any]:
    cutoff = _utc() - timedelta(days=max(1, days))
    rows = [
        row for row in read_jsonl("later_outcome_reviews.jsonl")
        if (_parse_dt(row.get("review_timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    return {
        "days": days,
        "count": len(rows),
        "decision_correct": sum(row.get("decision_correct") is True for row in rows),
        "missed_opportunity": sum(bool(row.get("missed_opportunity")) for row in rows),
        "false_causal_candidate": sum(bool(row.get("false_causal_candidate")) for row in rows),
        "rows": rows,
    }


def publication_evidence_check(candidate_id: str) -> dict[str, Any]:
    bundle = next(
        (
            row for row in reversed(read_jsonl("public_evidence_bundles.jsonl"))
            if str(row.get("candidate_id") or "") == str(candidate_id)
        ),
        None,
    )
    if not bundle:
        return {"status": "not_found", "candidate_id": candidate_id}
    return {
        "status": "allowed" if validate_public_bundle(bundle)["allowed"] else "blocked",
        "candidate_id": candidate_id,
        "validation": validate_public_bundle(bundle),
        "content_mode": bundle.get("content_mode"),
        "evidence_ids": bundle.get("evidence_ids"),
    }


def baseline_snapshot(*, stage: str = "before") -> dict[str, Any]:
    now = datetime.now(JST)
    fx_movements = _read_external_jsonl(market_data_dir().parent / "fx" / "movements.jsonl")
    market_movements = read_jsonl("movements.jsonl")
    alerts = read_jsonl("alerts.jsonl")
    shadow = read_jsonl("shadow_candidates.jsonl")
    history = _read_json(market_data_dir().parent / "posted_history.json", [])
    report = confirmation_report(days=3650)
    snapshot = {
        "stage": stage,
        "created_at": now.isoformat(),
        "workspace": str(Path.cwd()),
        "git_commit": _git_commit(),
        "market_movement_detected_count": len(market_movements),
        "fx_movement_detected_count": len(fx_movements),
        "movement_detected_count": len(market_movements) + len(fx_movements),
        "independent_confirmation_count": report["independent_confirmation_count"],
        "publication_candidate_count": len(shadow),
        "posted_count": len(history) if isinstance(history, list) else 0,
        "independent_confirmation_missing_stop_count": sum(
            row.get("reason") in {"confirmation_expired", "no_independent_source"}
            for row in read_jsonl("suppression_log.jsonl")
        ),
        "post_value_stop_count": sum(
            "quality" in str(row.get("status") or "") for row in alerts
        ),
        "license_stop_count": sum(
            "license" in str(row.get("status") or "") or "license" in str(row.get("reason") or "")
            for row in alerts
        ),
        "causal_confidence": {
            key: report[f"{key}_count"]
            for key in ("confirmed", "likely", "possible", "unknown")
        },
        "average_confirmation_seconds": report["average_confirmation_seconds"],
        "source_type": report["source_type"],
        "asset_type": report["asset_type"],
        "posting_mode": report["posting_mode"],
        "later_confirmed_count": report["source_found_later_count"],
        "stored_data_limitations": [
            "pre-existing movement records do not contain the new four-part evidence model",
            "historical independent confirmation and causal confidence cannot be reconstructed reliably",
        ],
    }
    folder = output_dir("baseline")
    stamp = now.strftime("%Y%m%d_%H%M%S")
    json_path = folder / f"market_trigger_publication_baseline_{stamp}.json"
    md_path = folder / f"market_trigger_publication_baseline_{stamp}.md"
    json_path.write_text(
        json.dumps(make_json_safe(snapshot), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_baseline_markdown(snapshot), encoding="utf-8")
    return {
        **snapshot,
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }


def _read_external_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _git_commit() -> str:
    try:
        import subprocess
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path.cwd(), capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _baseline_markdown(snapshot: dict[str, Any]) -> str:
    confidence = snapshot["causal_confidence"]
    return "\n".join([
        f"# Market trigger publication baseline ({snapshot['stage']})",
        "",
        f"- Created: {snapshot['created_at']}",
        f"- Workspace: `{snapshot['workspace']}`",
        f"- Git commit: `{snapshot['git_commit']}`",
        f"- Market movements: {snapshot['market_movement_detected_count']}",
        f"- FX movements: {snapshot['fx_movement_detected_count']}",
        f"- Independent confirmations: {snapshot['independent_confirmation_count']}",
        f"- Publication candidates: {snapshot['publication_candidate_count']}",
        f"- Posted history rows: {snapshot['posted_count']}",
        f"- License stops: {snapshot['license_stop_count']}",
        f"- Confidence: confirmed={confidence['confirmed']}, likely={confidence['likely']}, possible={confidence['possible']}, unknown={confidence['unknown']}",
        "",
        "## Data limitations",
        *[f"- {item}" for item in snapshot["stored_data_limitations"]],
        "",
    ])


def report_lines(*, days: int = 7) -> list[str]:
    report = confirmation_report(days=days)
    return [
        "--- Market Trigger Confirmation ---",
        f"  detected={report['movement_detected_count']} confirmed={report['confirmed_count']} likely={report['likely_count']}",
        f"  possible={report['possible_count']} unknown={report['unknown_count']} confirmation_rate={report['confirmation_rate']:.1%}",
        f"  posted={report['posted_count']} suppressed={report['suppressed_count']} expired={report['expired_count']}",
        f"  later_confirmed={report['source_found_later_count']} false_causal={report['false_causal_candidates']}",
        f"  avg_confirmation_seconds={report['average_confirmation_seconds']}",
        f"  sources={report['source_type']}",
        f"  assets={report['asset_type']}",
        f"  modes={report['posting_mode']}",
    ]
