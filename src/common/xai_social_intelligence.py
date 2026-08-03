"""Event-led X Social Intelligence for the finance bot.

xAI is used as a bounded research layer. It does not establish facts, create
prices, make trading recommendations, or post to X.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from openai import OpenAI

from common.json_utils import make_json_safe
from common.runtime import JST, REPO_ROOT, load_env, state_dir


TRUE_VALUES = {"1", "true", "yes", "on"}
BASE_URL = "https://api.x.ai/v1"
MODES = {"event_reaction", "movement_explanation", "expert_watch", "exploration"}
ACCOUNT_TYPES = {
    "official_government", "official_company", "official_product", "regulator",
    "central_bank", "journalist", "analyst", "economist", "industry_expert",
    "institutional", "media", "independent_commentator", "aggregator",
    "promotional", "bot_like", "unknown",
}
LOW_QUALITY_PATTERNS = re.compile(
    r"(?i)\b(giveaway|referral|affiliate|signal\s*group|pump|guaranteed\s*profit|"
    r"join\s+my\s+(?:discord|telegram)|dm\s+me|100x|free\s+signals?)\b"
)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
HANDLE_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{1,20})")
TICKER_RE = re.compile(r"(?<![A-Z0-9])\$?([A-Z]{1,5})(?![A-Z0-9])")
ALIASES = {
    "alphabet": "google", "googl": "google", "goog": "google",
    "google": "google", "meta platforms": "meta", "facebook": "meta",
    "nvidia": "nvidia", "nvda": "nvidia", "microsoft": "microsoft",
    "msft": "microsoft", "federal reserve": "fed", "fomc": "fed",
    "bank of japan": "boj", "日本銀行": "boj", "日銀": "boj",
    "usd/jpy": "usdjpy", "usd jpy": "usdjpy", "ドル円": "usdjpy",
}


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in TRUE_VALUES


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def adaptive_cost_policy() -> dict[str, Any]:
    """Return the next-run shrink policy from recent actual social run costs."""
    warning = _env_float("XAI_COST_PER_RUN_WARNING_USD", 0.30)
    successful = [
        row for row in read_jsonl("runs.jsonl", limit=10)
        if row.get("status") == "success" and row.get("cost_usd") is not None
    ]
    recent_costs = [float(row.get("cost_usd") or 0) for row in successful[-3:]]
    average = statistics.mean(recent_costs) if recent_costs else 0.0
    level = 0
    if average > warning:
        level = 1
    if average > warning * 1.5:
        level = 2
    if average > warning * 2:
        level = 3
    latest_at = _parse_dt(successful[-1].get("timestamp")) if successful else None
    pause_minutes = _env_int("XAI_ADAPTIVE_PAUSE_MINUTES", 120, 1)
    pause_active = bool(
        level >= 3
        and len(recent_costs) >= 3
        and latest_at
        and datetime.now(JST) - latest_at.astimezone(JST)
        < timedelta(minutes=pause_minutes)
    )
    return {
        "level": level,
        "recent_average_cost_usd": round(average, 6) if recent_costs else None,
        "warning_usd": warning,
        "exploration_allowed": level == 0,
        "max_events": {0: 5, 1: 3, 2: 2, 3: 1}[level],
        "max_observed_posts_per_event": {0: 30, 1: 16, 2: 8, 3: 4}[level],
        "max_output_tokens": {0: 2200, 1: 1700, 2: 1200, 3: 800}[level],
        "minimum_priority": {0: 0.0, 1: 0.0, 2: 7.0, 3: 9.0}[level],
        "temporary_pause": pause_active,
        "pause_minutes": pause_minutes,
        "shrink_order": [
            "stop_exploration", "reduce_events", "reduce_representative_posts",
            "exclude_low_priority", "shorten_output", "prefer_cache",
            "temporary_xai_pause",
        ],
    }


def xai_dir() -> Path:
    path = state_dir() / "xai"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(make_json_safe(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _event_trigger_path() -> Path:
    return xai_dir() / "event_trigger.json"


def pending_event_trigger(now: datetime | None = None) -> dict[str, Any] | None:
    path = _event_trigger_path()
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expires = _parse_dt(row.get("expires_at"))
    if expires and expires < _now(now):
        return None
    return row if isinstance(row, dict) else None


def consume_event_trigger(now: datetime | None = None) -> dict[str, Any] | None:
    row = pending_event_trigger(now)
    path = _event_trigger_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    return row


def append_jsonl(name: str, value: dict[str, Any]) -> Path:
    path = xai_dir() / name
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(make_json_safe(value), ensure_ascii=False) + "\n")
    return path


def _record_run(row: dict[str, Any]) -> None:
    """Write the new ledger and the legacy budget ledger exactly once."""
    append_jsonl("runs.jsonl", row)
    append_jsonl("usage.jsonl", row)
    compatibility = {
        **row,
        "operation": "x_social_intelligence",
        "topic_count": int(row.get("events_researched") or 0),
        "unique_posts_returned": int(row.get("unique_posts_returned") or 0),
    }
    append_jsonl("api_usage.jsonl", compatibility)


def read_jsonl(name: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    path = xai_dir() / name
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    corrupt: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                rows.append(value)
            else:
                corrupt.append(raw)
        except json.JSONDecodeError:
            corrupt.append(raw)
    if corrupt:
        quarantine = xai_dir() / f"quarantine_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        with quarantine.open("a", encoding="utf-8", newline="\n") as handle:
            for raw in corrupt:
                handle.write(json.dumps({
                    "source": name,
                    "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                    "quarantined_at": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
    return rows[-limit:] if limit else rows


def _parse_dt(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(JST)
    return current.astimezone(JST) if current.tzinfo else current.replace(tzinfo=JST)


def canonicalize(value: str) -> str:
    text = re.sub(r"[^0-9a-zA-Z一-龥ぁ-んァ-ン]+", " ", str(value or "").lower()).strip()
    for alias, canonical in ALIASES.items():
        text = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", canonical, text)
    return " ".join(text.split())


def normalize_tickers(values: Iterable[str]) -> list[str]:
    aliases = {"GOOG": "GOOGL", "BRK.B": "BRK-B", "USDJPY": "USD/JPY"}
    result = []
    for raw in values:
        ticker = str(raw or "").upper().strip().lstrip("$")
        ticker = aliases.get(ticker, ticker)
        if re.fullmatch(r"[A-Z0-9./-]{1,12}", ticker):
            result.append(ticker)
    return list(dict.fromkeys(result))[:10]


@dataclass
class EventCandidate:
    candidate_id: str
    source_type: str
    source_id: str
    title: str
    canonical_topic: str
    entities: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    event_type: str = "other"
    official: bool = False
    reliability_tier: int = 3
    published_at: str = ""
    detected_at: str = ""
    source_urls: list[str] = field(default_factory=list)
    confirmed_facts: list[str] = field(default_factory=list)
    unconfirmed_claims: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    market_movement: dict[str, Any] = field(default_factory=dict)
    urgency_score: float = 0.0
    market_impact_score: float = 0.0
    novelty_score: float = 0.0
    social_research_value: float = 0.0
    xai_search_query: str = ""
    xai_search_priority: float = 0.0
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return make_json_safe(asdict(self))


def _candidate_id(source_type: str, source_id: str, canonical_topic: str) -> str:
    raw = f"{source_type}|{source_id}|{canonical_topic}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _event_type(title: str, source_type: str = "") -> str:
    value = canonicalize(title)
    mappings = (
        ("fx_movement", ("usdjpy", "為替", "円安", "円高")),
        ("earnings", ("earnings", "決算", "guidance")),
        ("monetary_policy", ("fed", "boj", "ecb", "政策金利")),
        ("economic_indicator", ("cpi", "pce", "gdp", "雇用", "小売売上")),
        ("regulation", ("sec", "regulation", "規制", "関税")),
        ("price_movement", ("急騰", "急落", "出来高", "movement")),
        ("company_announcement", ("発表", "launch", "acquisition", "買収")),
    )
    if source_type in {
        "fx_movement", "market_movement", "volume_anomaly",
        "market_map", "cross_asset_signal",
    }:
        return source_type
    return next((name for name, keys in mappings if any(key in value for key in keys)), "news")


def _query_for(candidate: EventCandidate) -> str:
    names = list(dict.fromkeys(
        [candidate.title, *candidate.entities, *candidate.tickers, *candidate.currencies]
    ))
    core = " OR ".join(f'"{item}"' for item in names[:6] if item)
    exclusions = "-giveaway -referral -affiliate -\"signal group\" -pump -\"guaranteed profit\""
    return f"({core}) {candidate.event_type} {exclusions}".strip()


def make_candidate(
    *,
    source_type: str,
    source_id: str,
    title: str,
    entities: Iterable[str] = (),
    tickers: Iterable[str] = (),
    currencies: Iterable[str] = (),
    countries: Iterable[str] = (),
    official: bool = False,
    reliability_tier: int = 3,
    published_at: str = "",
    source_urls: Iterable[str] = (),
    confirmed_facts: Iterable[str] = (),
    unconfirmed_claims: Iterable[str] = (),
    open_questions: Iterable[str] = (),
    market_movement: dict[str, Any] | None = None,
    urgency_score: float = 0.0,
    market_impact_score: float = 0.0,
    novelty_score: float = 0.0,
    social_research_value: float = 0.0,
    now: datetime | None = None,
) -> EventCandidate:
    current = _now(now)
    topic = canonicalize(" ".join([title, *entities, *tickers]))[:240]
    candidate = EventCandidate(
        candidate_id=_candidate_id(source_type, source_id, topic),
        source_type=source_type,
        source_id=str(source_id)[:160],
        title=str(title)[:360],
        canonical_topic=topic,
        entities=list(dict.fromkeys(str(item)[:120] for item in entities if item))[:10],
        tickers=normalize_tickers(tickers),
        currencies=list(dict.fromkeys(str(item).upper()[:12] for item in currencies if item))[:5],
        countries=list(dict.fromkeys(str(item).upper()[:8] for item in countries if item))[:5],
        event_type=_event_type(title, source_type),
        official=bool(official),
        reliability_tier=min(5, max(1, int(reliability_tier))),
        published_at=published_at,
        detected_at=current.isoformat(),
        source_urls=list(dict.fromkeys(str(item)[:500] for item in source_urls if item))[:5],
        confirmed_facts=[str(item)[:360] for item in confirmed_facts if item][:8],
        unconfirmed_claims=[str(item)[:240] for item in unconfirmed_claims if item][:8],
        open_questions=[str(item)[:240] for item in open_questions if item][:8],
        market_movement=make_json_safe(market_movement or {}),
        urgency_score=min(10.0, max(0.0, float(urgency_score))),
        market_impact_score=min(10.0, max(0.0, float(market_impact_score))),
        novelty_score=min(10.0, max(0.0, float(novelty_score))),
        social_research_value=min(10.0, max(0.0, float(social_research_value))),
        expires_at=(current + timedelta(hours=24)).isoformat(),
    )
    official_bonus = 2.5 if candidate.official else 0.0
    reliability_bonus = (6 - candidate.reliability_tier) * 0.5
    movement_bonus = 1.0 if candidate.market_movement else 0.0
    candidate.xai_search_priority = round(
        candidate.urgency_score * 0.25
        + candidate.market_impact_score * 0.25
        + candidate.novelty_score * 0.15
        + candidate.social_research_value * 0.25
        + official_bonus + reliability_bonus + movement_bonus,
        3,
    )
    candidate.xai_search_query = _query_for(candidate)
    return candidate


def _recent_rows(path: Path, time_keys: tuple[str, ...], hours: int = 24) -> list[dict]:
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        when = next((_parse_dt(row.get(key)) for key in time_keys if row.get(key)), None)
        if when and when.astimezone(timezone.utc) >= cutoff:
            result.append(row)
    return result


def gather_event_candidates(
    *, rss_items: Iterable[Any] | None = None, now: datetime | None = None
) -> list[EventCandidate]:
    """Generate candidates locally; no xAI request is made here."""
    current = _now(now)
    candidates: list[EventCandidate] = []
    if rss_items is None:
        try:
            from news_bot.news import fetch_news_candidates
            from news_bot.posted_history import get_posted_urls
            rss_items = fetch_news_candidates(get_posted_urls(), limit=12)
        except Exception:
            rss_items = []
    for item in rss_items or []:
        title = str(getattr(item, "title", "") or (item.get("title") if isinstance(item, dict) else ""))
        if not title:
            continue
        source = str(getattr(item, "source", "") or (item.get("source") if isinstance(item, dict) else ""))
        group = str(getattr(item, "source_group", "") or (item.get("source_group") if isinstance(item, dict) else ""))
        url = str(getattr(item, "url", "") or (item.get("url") if isinstance(item, dict) else ""))
        published = getattr(item, "published_at", "") or (item.get("published_at") if isinstance(item, dict) else "")
        official = group.startswith("official")
        tickers = TICKER_RE.findall(title)
        candidates.append(make_candidate(
            source_type="official_rss" if official else "rss_news",
            source_id=url or f"{source}:{title}",
            title=title,
            tickers=tickers,
            official=official,
            reliability_tier=1 if official else 3,
            published_at=str(published or ""),
            source_urls=[url] if url else [],
            confirmed_facts=[title] if official else [],
            open_questions=["X上の主要な反応、反対意見、誤解は何か"],
            urgency_score=8 if official else 6,
            market_impact_score=8 if official else 6,
            novelty_score=7,
            social_research_value=8,
            now=current,
        ))
    for row in _recent_rows(state_dir() / "fx" / "movements.jsonl", ("detected_at",), 24):
        title = (
            f"{row.get('pair', 'USDJPY')} {row.get('window', '')} "
            f"{float(row.get('change_pct', 0) or 0):+.2f}% movement"
        )
        candidates.append(make_candidate(
            source_type="fx_movement", source_id=str(row.get("movement_id") or title),
            title=title, entities=["USD/JPY"], currencies=["USD", "JPY"],
            countries=["US", "JP"], reliability_tier=2,
            published_at=str(row.get("detected_at") or ""),
            confirmed_facts=[title], market_movement=row,
            open_questions=["同時刻に言及された原因候補と公式情報候補は何か"],
            urgency_score=9, market_impact_score=8, novelty_score=8,
            social_research_value=9, now=current,
        ))
    for row in _recent_rows(state_dir() / "market_data" / "movements.jsonl", ("detected_at",), 24):
        symbol = str(row.get("symbol") or "")
        source_type = "volume_anomaly" if row.get("alert_type") == "volume_alert" else "market_movement"
        title = (
            f"{symbol} {row.get('window_minutes', '')}m "
            f"{float(row.get('percentage_change', 0) or 0):+.2f}% movement"
        )
        candidates.append(make_candidate(
            source_type=source_type, source_id=str(row.get("movement_id") or title),
            title=title, entities=[symbol], tickers=[symbol],
            reliability_tier=2, published_at=str(row.get("detected_at") or ""),
            confirmed_facts=[title], market_movement=row,
            open_questions=["価格または出来高変化と整合する原因候補は何か"],
            urgency_score=8, market_impact_score=7, novelty_score=8,
            social_research_value=8, now=current,
        ))
    try:
        from weekly_bot.weekly_events import _macro_events
        market_date = current.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        for row in _macro_events():
            if str(row.get("date")) != market_date:
                continue
            candidates.append(make_candidate(
                source_type="official_calendar",
                source_id=f"{row.get('date')}:{row.get('title')}",
                title=str(row.get("title") or ""),
                countries=[str(row.get("country") or "US")],
                official=True, reliability_tier=1,
                published_at=str(row.get("date") or ""),
                source_urls=[str(row.get("source_url") or "")],
                confirmed_facts=[f"公式日程: {row.get('date')} {row.get('time_et', '')} ET"],
                open_questions=["発表後の共通認識、反対意見、誤解は何か"],
                urgency_score=9, market_impact_score=9, novelty_score=6,
                social_research_value=9, now=current,
            ))
    except Exception:
        pass
    for row in read_jsonl("manual_events.jsonl"):
        expires = _parse_dt(row.get("expires_at"))
        if expires and expires < current:
            continue
        candidates.append(make_candidate(
            source_type="manual", source_id=str(row.get("source_id") or uuid.uuid4().hex),
            title=str(row.get("title") or ""), entities=row.get("entities") or [],
            tickers=row.get("tickers") or [], official=bool(row.get("official")),
            reliability_tier=int(row.get("reliability_tier") or 3),
            source_urls=row.get("source_urls") or [],
            confirmed_facts=row.get("confirmed_facts") or [],
            unconfirmed_claims=row.get("unconfirmed_claims") or [],
            open_questions=row.get("open_questions") or [],
            urgency_score=float(row.get("urgency_score") or 5),
            market_impact_score=float(row.get("market_impact_score") or 5),
            novelty_score=float(row.get("novelty_score") or 5),
            social_research_value=float(row.get("social_research_value") or 5),
            now=current,
        ))
    candidate_fields = set(EventCandidate.__dataclass_fields__)
    latest_events: dict[str, dict[str, Any]] = {}
    for row in read_jsonl("events.jsonl", limit=500):
        event_id = str(row.get("candidate_id") or "")
        if event_id:
            latest_events[event_id] = row
    for row in latest_events.values():
        if row.get("status") != "queued":
            continue
        expires = _parse_dt(row.get("expires_at"))
        if expires and expires < current:
            continue
        try:
            candidates.append(EventCandidate(**{
                key: row[key] for key in candidate_fields if key in row
            }))
        except (TypeError, ValueError):
            continue
    return deduplicate_candidates(candidates)


def deduplicate_candidates(candidates: Iterable[EventCandidate]) -> list[EventCandidate]:
    ranked = sorted(candidates, key=lambda item: item.xai_search_priority, reverse=True)
    result: list[EventCandidate] = []
    for candidate in ranked:
        duplicate = False
        for existing in result:
            ticker_overlap = bool(set(candidate.tickers) & set(existing.tickers))
            similarity = SequenceMatcher(
                None, candidate.canonical_topic, existing.canonical_topic
            ).ratio()
            if candidate.candidate_id == existing.candidate_id or similarity >= 0.82 or (
                ticker_overlap and candidate.event_type == existing.event_type
            ):
                duplicate = True
                break
        if not duplicate:
            result.append(candidate)
    return result


def select_candidates(
    candidates: Iterable[EventCandidate], *, maximum: int | None = None
) -> list[EventCandidate]:
    maximum = min(5, max(1, maximum or _env_int("XAI_MAX_EVENTS_PER_RUN", 5, 1)))
    result: list[EventCandidate] = []
    concentration: dict[str, int] = {}
    for item in sorted(candidates, key=lambda row: row.xai_search_priority, reverse=True):
        concentration_key = (
            item.tickers[0] if item.tickers else
            item.entities[0].lower() if item.entities else
            item.canonical_topic.split(" ", 1)[0]
        )
        if concentration.get(concentration_key, 0) >= 2:
            continue
        result.append(item)
        concentration[concentration_key] = concentration.get(concentration_key, 0) + 1
        if len(result) >= maximum:
            break
    return result


def account_watchlist() -> dict[str, Any]:
    path = REPO_ROOT / "config" / "xai_account_watchlist.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _watchlist_handles(buckets: Iterable[str]) -> list[str]:
    result: list[str] = []
    watchlist = account_watchlist()
    for bucket in buckets:
        for row in watchlist.get(bucket, []):
            handle = str(
                row.get("handle") if isinstance(row, dict) else row
            ).strip().lstrip("@")
            if handle and re.fullmatch(r"[A-Za-z0-9_]{1,20}", handle):
                result.append(handle)
    return list(dict.fromkeys(result))[:20]


def classify_account(handle: str, *, hint: str = "", text: str = "") -> dict[str, Any]:
    normalized = str(handle or "").strip().lstrip("@").lower()
    watchlist = account_watchlist()
    for bucket in ("excluded", "low_quality", "official", "expert", "media", "high_priority"):
        for row in watchlist.get(bucket, []):
            username = str(row.get("handle") if isinstance(row, dict) else row).lstrip("@").lower()
            if username != normalized:
                continue
            category = str(row.get("category") if isinstance(row, dict) else "") or (
                "promotional" if bucket in {"excluded", "low_quality"} else
                "media" if bucket == "media" else
                "industry_expert" if bucket == "expert" else
                "official_company"
            )
            factors = {
                key: int(row.get(key, 50))
                for key in (
                    "officiality", "historical_accuracy", "source_proximity",
                    "domain_expertise", "original_post_ratio", "citation_rate",
                    "correction_history", "low_hype", "low_duplication",
                    "low_affiliate_tendency",
                )
            } if isinstance(row, dict) else {}
            derived_score = (
                round(statistics.mean(factors.values()))
                if factors else 80
            )
            return {
                "handle": normalized, "category": category,
                "quality_score": int(
                    row.get("quality_score", derived_score)
                    if isinstance(row, dict) else derived_score
                ),
                "quality_factors": factors,
                "watchlist_bucket": bucket,
                "classification_source": "configured_policy",
                "human_review_required": False,
            }
    combined = f"{hint} {text}".lower()
    if LOW_QUALITY_PATTERNS.search(combined):
        category, score = "promotional", 10
    elif "bot" in combined and "robot" not in combined:
        category, score = "bot_like", 15
    elif hint in ACCOUNT_TYPES:
        category, score = hint, 50
    else:
        category, score = "unknown", 40
    return {
        "handle": normalized, "category": category, "quality_score": score,
        "quality_factors": {},
        "watchlist_bucket": "",
        "classification_source": "automatic_heuristic",
        "human_review_required": False,
    }


def _post_key(post: dict[str, Any]) -> tuple[str, str, str]:
    post_id = str(post.get("post_id") or "")
    url = str(post.get("url") or "")
    text = canonicalize(str(post.get("excerpt") or post.get("text") or ""))
    return post_id, url, text


def normalize_posts(posts: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observed = [row for row in posts if isinstance(row, dict)]
    accepted: list[dict[str, Any]] = []
    accounts: dict[str, int] = {}
    domains: set[str] = set()
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_texts: list[str] = []
    seen_threads: set[str] = set()
    seen_quote_sources: set[str] = set()
    duplicates = 0
    for row in observed:
        post_id, url, normalized_text = _post_key(row)
        handle = str(row.get("account") or row.get("username") or "").lstrip("@")
        thread_id = str(row.get("thread_id") or row.get("conversation_id") or "")
        quote_source = str(
            row.get("quoted_post_id") or row.get("original_post_id") or ""
        )
        excerpt = str(row.get("excerpt") or row.get("text") or "")[:360]
        account = classify_account(handle, hint=str(row.get("account_type") or ""), text=excerpt)
        is_duplicate = (
            bool(post_id and post_id in seen_ids)
            or bool(url and url in seen_urls)
            or bool(thread_id and thread_id in seen_threads)
            or bool(quote_source and quote_source in seen_quote_sources)
            or accounts.get(handle.lower(), 0) >= 2
            or any(SequenceMatcher(None, normalized_text, old).ratio() >= 0.90
                   for old in seen_texts if normalized_text and old)
        )
        low_quality = (
            account["category"] in {"promotional", "bot_like"}
            or bool(LOW_QUALITY_PATTERNS.search(excerpt))
            or bool(row.get("is_repost"))
        )
        if is_duplicate or low_quality:
            duplicates += 1
            continue
        if post_id:
            seen_ids.add(post_id)
        if url:
            seen_urls.add(url)
            try:
                from urllib.parse import urlparse
                host = urlparse(url).hostname
                if host:
                    domains.add(host.lower())
            except ValueError:
                pass
        if normalized_text:
            seen_texts.append(normalized_text)
        if thread_id:
            seen_threads.add(thread_id)
        if quote_source:
            seen_quote_sources.add(quote_source)
        accounts[handle.lower()] = accounts.get(handle.lower(), 0) + 1
        accepted.append({
            "post_id": post_id[:40],
            "url": url[:500],
            "account": handle[:40],
            "account_type": account["category"],
            "account_quality_score": account["quality_score"],
            "excerpt": excerpt,
            "thread_id": thread_id[:60],
            "is_reply": bool(row.get("is_reply")),
            "is_quote": bool(row.get("is_quote")),
            "source_url": str(row.get("source_url") or "")[:500],
        })
        source_url = str(row.get("source_url") or "")
        if source_url:
            try:
                from urllib.parse import urlparse
                source_host = urlparse(source_url).hostname
                if source_host:
                    domains.add(source_host.lower())
            except ValueError:
                pass
    total = len(observed)
    unique_accounts = len(accounts)
    top_share = max(accounts.values(), default=0) / len(accepted) if accepted else 0.0
    verified_types = {
        "official_government", "official_company", "official_product", "regulator",
        "central_bank",
    }
    expert_types = {"journalist", "analyst", "economist", "industry_expert", "institutional"}
    metrics = {
        "observed_result_count": total,
        "unique_original_posts": len(accepted),
        "unique_accounts": unique_accounts,
        "unique_verified_or_official_accounts": len({
            row["account"] for row in accepted if row["account_type"] in verified_types
        }),
        "unique_expert_accounts": len({
            row["account"] for row in accepted if row["account_type"] in expert_types
        }),
        "unique_domains": len(domains),
        "reply_count": sum(bool(row["is_reply"]) for row in accepted),
        "quote_count": sum(bool(row["is_quote"]) for row in accepted),
        "repost_like_count_if_available": None,
        "independent_commentary_count": sum(
            not row["is_reply"] and not row["is_quote"] for row in accepted
        ),
        "duplicate_ratio": round(duplicates / total, 4) if total else 0.0,
        "account_concentration": round(
            sum(count * count for count in accounts.values()) / (len(accepted) ** 2), 4
        ) if accepted else 0.0,
        "top_account_share": round(top_share, 4),
        "official_account_participation": any(
            row["account_type"] in verified_types for row in accepted
        ),
        "expert_account_participation": any(
            row["account_type"] in expert_types for row in accepted
        ),
        "measurement_scope": "observed_independent_posts_within_search_results",
    }
    return accepted, metrics


def compute_delta(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {
            "comparable": False, "reason": "no_previous_observation",
            "observed_velocity_score": None, "observed_acceleration_score": None,
            "new_unique_posts": None, "new_unique_accounts": None,
            "new_independent_commentary": None, "new_official_participation": None,
            "new_expert_participation": None, "narrative_change_score": None,
            "new_quote_posts": None, "new_replies": None,
            "new_topic_points": None, "new_dissent": None,
            "new_misconceptions": None, "new_evidence_urls": None,
            "consensus_change": None, "dissent_change": None,
        }
    current_at = _parse_dt(current.get("observed_at"))
    previous_at = _parse_dt(previous.get("observed_at"))
    if not current_at or not previous_at or current_at <= previous_at:
        return {"comparable": False, "reason": "invalid_observation_interval"}
    hours = (current_at - previous_at).total_seconds() / 3600
    cur_metrics = current.get("metrics") or {}
    old_metrics = previous.get("metrics") or {}
    current_posts = {
        str(row.get("post_id") or row.get("url") or "")
        for row in current.get("posts") or []
        if row.get("post_id") or row.get("url")
    }
    previous_posts = {
        str(row.get("post_id") or row.get("url") or "")
        for row in previous.get("posts") or []
        if row.get("post_id") or row.get("url")
    }
    current_accounts = {
        str(row.get("account") or "").lower()
        for row in current.get("posts") or [] if row.get("account")
    }
    previous_accounts = {
        str(row.get("account") or "").lower()
        for row in previous.get("posts") or [] if row.get("account")
    }
    new_posts = (
        len(current_posts - previous_posts)
        if current_posts or previous_posts
        else max(0, int(cur_metrics.get("unique_original_posts") or 0)
                 - int(old_metrics.get("unique_original_posts") or 0))
    )
    new_accounts = (
        len(current_accounts - previous_accounts)
        if current_accounts or previous_accounts
        else max(0, int(cur_metrics.get("unique_accounts") or 0)
                 - int(old_metrics.get("unique_accounts") or 0))
    )
    new_commentary = max(0, int(cur_metrics.get("independent_commentary_count") or 0)
                         - int(old_metrics.get("independent_commentary_count") or 0))
    velocity = new_posts / hours if hours > 0 else None
    previous_velocity = previous.get("delta", {}).get("observed_velocity_score")
    acceleration = (
        velocity - float(previous_velocity)
        if velocity is not None and previous_velocity is not None else None
    )
    current_interpretation = current.get("interpretation") or {}
    old_interpretation = previous.get("interpretation") or {}
    dominant = str(current_interpretation.get("dominant_narrative") or "")
    old_dominant = str(old_interpretation.get("dominant_narrative") or "")
    dissent = " ".join(current_interpretation.get("alternative_narratives") or [])
    old_dissent = " ".join(old_interpretation.get("alternative_narratives") or [])
    current_topics = {
        str(row.get("angle") or "")
        for row in current_interpretation.get("content_angles") or []
        if isinstance(row, dict) and row.get("angle")
    }
    previous_topics = {
        str(row.get("angle") or "")
        for row in old_interpretation.get("content_angles") or []
        if isinstance(row, dict) and row.get("angle")
    }
    current_misconception = str(
        current_interpretation.get("common_misconception") or ""
    )
    previous_misconception = str(
        old_interpretation.get("common_misconception") or ""
    )
    current_urls = {
        str(row.get("source_url") or "")
        for row in current.get("posts") or [] if row.get("source_url")
    }
    previous_urls = {
        str(row.get("source_url") or "")
        for row in previous.get("posts") or [] if row.get("source_url")
    }
    return {
        "comparable": True,
        "interval_hours": round(hours, 3),
        "observed_velocity_score": round(velocity, 3) if velocity is not None else None,
        "observed_acceleration_score": round(acceleration, 3) if acceleration is not None else None,
        "new_unique_posts": new_posts,
        "new_unique_accounts": new_accounts,
        "new_independent_commentary": new_commentary,
        "new_quote_posts": max(
            0, int(cur_metrics.get("quote_count") or 0)
            - int(old_metrics.get("quote_count") or 0),
        ),
        "new_replies": max(
            0, int(cur_metrics.get("reply_count") or 0)
            - int(old_metrics.get("reply_count") or 0),
        ),
        "new_official_participation": (
            bool(cur_metrics.get("official_account_participation"))
            and not bool(old_metrics.get("official_account_participation"))
        ),
        "new_expert_participation": (
            bool(cur_metrics.get("expert_account_participation"))
            and not bool(old_metrics.get("expert_account_participation"))
        ),
        "narrative_change_score": round(
            1.0 - SequenceMatcher(None, dominant, old_dominant).ratio(), 3
        ) if dominant and old_dominant else None,
        "new_topic_points": sorted(current_topics - previous_topics)[:10],
        "new_dissent": dissent if dissent and dissent != old_dissent else "",
        "new_misconceptions": (
            current_misconception
            if current_misconception
            and current_misconception != previous_misconception else ""
        ),
        "new_evidence_urls": sorted(current_urls - previous_urls)[:10],
        "consensus_change": dominant if dominant != old_dominant else "",
        "dissent_change": dissent if dissent != old_dissent else "",
    }


def _latest_observation(event_id: str) -> dict[str, Any] | None:
    rows = [
        row for row in read_jsonl("observations.jsonl")
        if str(row.get("event_id")) == str(event_id)
    ]
    return max(rows, key=lambda row: str(row.get("observed_at") or ""), default=None)


def _partition_event_cache(
    candidates: list[EventCandidate], now: datetime
) -> tuple[list[EventCandidate], list[dict[str, Any]]]:
    ttl = timedelta(minutes=_env_int("XAI_CACHE_TTL_MINUTES", 60, 1))
    uncached: list[EventCandidate] = []
    cached: list[dict[str, Any]] = []
    for candidate in candidates:
        observation = _latest_observation(candidate.candidate_id)
        observed_at = _parse_dt((observation or {}).get("observed_at"))
        if observation and observed_at and now - observed_at.astimezone(JST) < ttl:
            cached.append(observation)
        else:
            uncached.append(candidate)
    append_jsonl("social_cache_events.jsonl", {
        "timestamp": now.isoformat(),
        "event": "hit" if cached else "miss",
        "hits": len(cached),
        "misses": len(uncached),
        "event_ids": [row.get("event_id") for row in cached],
        "ttl_minutes": int(ttl.total_seconds() // 60),
    })
    return uncached, cached


def social_cache_status(days: int = 7) -> dict[str, Any]:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    rows = [
        row for row in read_jsonl("social_cache_events.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    hits = sum(int(row.get("hits") or 0) for row in rows)
    misses = sum(int(row.get("misses") or 0) for row in rows)
    return {
        "ttl_minutes": _env_int("XAI_CACHE_TTL_MINUTES", 60, 1),
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
        "prompt_cache_enabled": _env_bool("XAI_PROMPT_CACHE_ENABLED", True),
    }


def _schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}, "maxItems": 8}
    observed_post = {
        "type": "object", "additionalProperties": False,
        "required": [
            "post_id", "url", "account", "account_type", "excerpt",
            "thread_id", "is_reply", "is_quote", "is_repost", "source_url",
        ],
        "properties": {
            "post_id": {"type": "string"}, "url": {"type": "string"},
            "account": {"type": "string"},
            "account_type": {
                "type": "string",
                "enum": [
                    "official_government", "official_company", "official_product",
                    "regulator", "central_bank", "journalist", "analyst",
                    "economist", "industry_expert", "institutional", "media",
                    "independent_commentator", "aggregator", "promotional",
                    "bot_like", "unknown",
                ],
            },
            "excerpt": {"type": "string"}, "thread_id": {"type": "string"},
            "is_reply": {"type": "boolean"}, "is_quote": {"type": "boolean"},
            "is_repost": {"type": "boolean"}, "source_url": {"type": "string"},
        },
    }
    angle = {
        "type": "object", "additionalProperties": False,
        "required": ["angle", "why_useful", "recommended_format", "confidence"],
        "properties": {
            "angle": {"type": "string"}, "why_useful": {"type": "string"},
            "recommended_format": {
                "type": "string",
                "enum": ["x", "note_free", "note_paid", "threads", "youtube_short",
                         "youtube_long", "newsletter", "weekly_report"],
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
    }
    event = {
        "type": "object", "additionalProperties": False,
        "required": [
            "event_id", "topic_summary", "why_people_are_discussing_it",
            "dominant_narrative", "alternative_narratives", "strongest_dissent",
            "common_misconception", "unanswered_questions", "useful_expert_points",
            "market_implication_candidates", "facts_needing_confirmation",
            "potentially_false_claims", "content_angles", "channel_fit",
            "novelty_assessment", "confidence", "observed_posts",
        ],
        "properties": {
            "event_id": {"type": "string"}, "topic_summary": {"type": "string"},
            "why_people_are_discussing_it": {"type": "string"},
            "dominant_narrative": {"type": "string"},
            "alternative_narratives": string_array,
            "strongest_dissent": {"type": "string"},
            "common_misconception": {"type": "string"},
            "unanswered_questions": string_array,
            "useful_expert_points": string_array,
            "market_implication_candidates": string_array,
            "facts_needing_confirmation": string_array,
            "potentially_false_claims": string_array,
            "content_angles": {"type": "array", "items": angle, "maxItems": 8},
            "channel_fit": string_array,
            "novelty_assessment": {"type": "string"},
            "confidence": {
                "type": "string", "enum": ["confirmed", "likely", "possible", "unknown"],
            },
            "observed_posts": {"type": "array", "items": observed_post, "maxItems": 30},
        },
    }
    return {
        "format": {
            "type": "json_schema", "name": "x_social_intelligence", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["events"],
                "properties": {"events": {"type": "array", "items": event, "maxItems": 5}},
            },
        }
    }


def _fixed_instructions() -> str:
    return (
        "You are the X research layer for a financial media system. "
        "Research only the supplied events with X Search. Search each event separately. "
        "Treat counts as observed search samples, never totals for all of X. "
        "Do not establish facts, invent market data, recommend trades, predict prices, "
        "identify private attributes, claim institutional activity without evidence, "
        "or confirm FX intervention without an official authority. "
        "Return short excerpts only. Exclude reposts, copy posts, affiliate promotion, "
        "signal groups, giveaways, and bot-like repeated text. "
        "Separate confirmed local facts from X claims needing verification. "
        "Use the strict JSON schema."
    )


def _validate_structured_result(
    value: Any, selected: list[EventCandidate]
) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise ValueError("invalid_structured_output")
    required = {
        "event_id", "topic_summary", "why_people_are_discussing_it",
        "dominant_narrative", "alternative_narratives", "strongest_dissent",
        "common_misconception", "unanswered_questions", "useful_expert_points",
        "market_implication_candidates", "facts_needing_confirmation",
        "potentially_false_claims", "content_angles", "channel_fit",
        "novelty_assessment", "confidence", "observed_posts",
    }
    returned_ids: set[str] = set()
    for row in value["events"]:
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError("invalid_structured_event")
        if row.get("confidence") not in {"confirmed", "likely", "possible", "unknown"}:
            raise ValueError("invalid_confidence")
        returned_ids.add(str(row.get("event_id") or ""))
    expected_ids = {item.candidate_id for item in selected}
    if returned_ids != expected_ids:
        raise ValueError("structured_event_id_mismatch")
    return value


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    ticks = int(getattr(usage, "cost_in_usd_ticks", 0) or 0)
    tools = int(getattr(usage, "num_server_side_tools_used", 0) or 0)
    return {
        "attempted_tool_calls": tools, "successful_tool_calls": tools,
        "x_search_calls": tools,
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "cached_input_tokens": int(getattr(input_details, "cached_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(output_details, "reasoning_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cost_usd": round(ticks / 1e10, 8) if ticks else None,
        "reported_cost_usd": round(ticks / 1e10, 8) if ticks else None,
        "estimated_cost_usd": round(tools * 0.005, 6) if not ticks else 0.0,
    }


def _mode_for(candidates: list[EventCandidate], now: datetime) -> str:
    if any(item.source_type in {
        "fx_movement", "market_movement", "volume_anomaly",
        "market_map", "cross_asset_signal",
    } for item in candidates):
        return "movement_explanation" if now.strftime("%H:%M") >= "22:00" else "event_reaction"
    return "event_reaction"


def _exploration_allowed() -> bool:
    if not _env_bool("XAI_EXPLORATION_ENABLED", True):
        return False
    runs = read_jsonl("runs.jsonl")
    total = sum(float(row.get("cost_usd") or 0) for row in runs)
    exploration = sum(
        float(row.get("cost_usd") or 0) for row in runs
        if row.get("radar_mode") == "exploration"
    )
    limit = _env_float("XAI_EXPLORATION_BUDGET_PERCENT", 20.0) / 100.0
    return total <= 0 or exploration / total < limit


def _cooldown_filter(
    candidates: list[EventCandidate], now: datetime
) -> list[EventCandidate]:
    cooldown = timedelta(minutes=_env_int("XAI_EVENT_RESEARCH_COOLDOWN_MINUTES", 60))
    latest: dict[str, datetime] = {}
    for row in read_jsonl("observations.jsonl"):
        event_id = str(row.get("event_id") or "")
        observed = _parse_dt(row.get("observed_at"))
        if event_id and observed and (event_id not in latest or observed > latest[event_id]):
            latest[event_id] = observed
    return [
        item for item in candidates
        if item.candidate_id not in latest or now - latest[item.candidate_id].astimezone(JST) >= cooldown
    ]


def _daily_soft_budget_reached(now: datetime) -> bool:
    day = now.date().isoformat()
    cost = sum(
        float(row.get("cost_usd") or row.get("reported_cost_usd") or 0)
        for row in read_jsonl("runs.jsonl")
        if str(row.get("timestamp") or "").startswith(day)
    )
    return cost >= _env_float("XAI_DAILY_SOFT_BUDGET_USD", 0.75)


def _adaptive_maximum(now: datetime | None = None) -> int:
    current = _now(now)
    scheduled_maximum = (
        3 if current.strftime("%H:%M") >= "20:30"
        and current.strftime("%H:%M") < "22:00" else 5
    )
    policy = adaptive_cost_policy()
    return min(
        scheduled_maximum,
        policy["max_events"],
        _env_int("XAI_MAX_EVENTS_PER_RUN", 5, 1),
    )


def _event_payload(candidates: list[EventCandidate]) -> list[dict[str, Any]]:
    return [{
        "event_id": item.candidate_id,
        "event_type": item.event_type,
        "title": item.title,
        "query": item.xai_search_query,
        "entities": item.entities,
        "tickers": item.tickers,
        "confirmed_facts": item.confirmed_facts,
        "unconfirmed_claims": item.unconfirmed_claims,
        "open_questions": item.open_questions,
        "source_urls": item.source_urls,
        "market_movement": item.market_movement,
    } for item in candidates]


def run(
    *,
    dry_run: bool = False,
    fixture_response: dict[str, Any] | None = None,
    candidates: Iterable[EventCandidate] | None = None,
    radar_mode: str = "",
    now: datetime | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Research a bounded event batch. Never posts to X."""
    load_env()
    current = _now(now)
    if _env_bool("XAI_SAFE_DISABLED", False):
        return {
            "status": "skipped",
            "reason": "xai_safe_disabled",
            "events": [],
            "x_post_called": False,
        }
    from common.xai_radar_v2 import _can_call
    allowed, reason = _can_call(current)
    if not allowed and fixture_response is None:
        return {"status": "skipped", "reason": reason, "events": []}
    policy = adaptive_cost_policy()
    if policy["temporary_pause"] and fixture_response is None:
        return {
            "status": "skipped", "reason": "adaptive_cost_pause",
            "events": [], "cost_policy": policy, "x_post_called": False,
        }
    gathered = (
        list(candidates)
        if candidates is not None
        else gather_event_candidates(rss_items=[] if dry_run else None, now=current)
    )
    prioritized = [
        row for row in gathered
        if row.xai_search_priority >= float(policy["minimum_priority"])
    ]
    preselected = select_candidates(prioritized, maximum=_adaptive_maximum(current))
    selected, cached_observations = _partition_event_cache(preselected, current)
    selected = _cooldown_filter(selected, current)
    mode = (
        radar_mode if radar_mode in MODES
        else _mode_for(selected or preselected, current)
    )
    mode_flag = {
        "event_reaction": "XAI_EVENT_REACTION_ENABLED",
        "movement_explanation": "XAI_MOVEMENT_EXPLANATION_ENABLED",
        "expert_watch": "XAI_EXPERT_WATCH_ENABLED",
        "exploration": "XAI_EXPLORATION_ENABLED",
    }[mode]
    if not _env_bool(mode_flag, True):
        return {"status": "skipped", "reason": f"{mode}_disabled", "events": []}
    if mode == "exploration":
        selected = selected[:_env_int("XAI_MAX_EXPLORATION_QUERIES_PER_RUN", 1, 1)]
    if mode == "exploration" and (
        not policy["exploration_allowed"] or not _exploration_allowed()
    ):
        return {"status": "skipped", "reason": "exploration_budget_limit", "events": []}
    if _daily_soft_budget_reached(current) and mode == "exploration":
        return {"status": "skipped", "reason": "daily_soft_budget", "events": []}
    if not selected and cached_observations:
        cached_by_id = {
            str(row.get("event_id") or ""): row for row in cached_observations
        }
        for event in preselected:
            if event.candidate_id in cached_by_id:
                append_jsonl("events.jsonl", {
                    **event.to_dict(),
                    "status": "cache_satisfied",
                    "cached_observation_id": cached_by_id[
                        event.candidate_id
                    ].get("observation_id"),
                    "researched_at": current.isoformat(),
                })
        return {
            "status": "cached", "radar_mode": mode,
            "events": cached_observations, "api_called": False,
            "x_post_called": False, "cost_policy": policy,
        }
    if not selected and mode != "exploration":
        return {"status": "skipped", "reason": "no_event_candidates", "events": []}
    if mode == "exploration" and not selected:
        rotation = ["米国株", "日本株", "AI半導体", "為替金利", "エネルギー政策"]
        theme = rotation[current.date().toordinal() % len(rotation)]
        selected = [make_candidate(
            source_type="exploration", source_id=f"{current.date()}:{theme}",
            title=f"{theme}の早期兆候探索", novelty_score=8,
            social_research_value=8, urgency_score=4, market_impact_score=5,
            now=current,
        )]
    for item in selected:
        append_jsonl("events.jsonl", {**item.to_dict(), "status": "queued"})
    if dry_run and fixture_response is None:
        return {
            "status": "dry_run", "radar_mode": mode,
            "events": [item.to_dict() for item in selected],
            "api_called": False, "x_post_called": False,
        }
    run_id = uuid.uuid4().hex
    started = time.perf_counter()
    response = None
    failure_stage = None
    try:
        if fixture_response is not None:
            parsed = fixture_response
        else:
            api = client or OpenAI(
                api_key=os.environ["XAI_API_KEY"],
                base_url=os.getenv("XAI_BASE_URL", BASE_URL),
                max_retries=0,
                timeout=float(os.getenv("XAI_TIMEOUT_SECONDS", "60") or 60),
            )
            user_input = (
                f"Radar mode: {mode}\n"
                "Research these locally generated events. Use no more than one X Search "
                "tool call per event and do not broaden beyond them. "
                f"Return at most {policy['max_observed_posts_per_event']} observed posts "
                "per event, prioritizing independent and primary-source commentary.\n"
                + json.dumps(_event_payload(selected), ensure_ascii=False)
            )
            search_days = _env_int("XAI_SEARCH_LOOKBACK_DAYS", 3, 1)
            search_tool = {
                "type": "x_search",
                "from_date": (current.date() - timedelta(days=min(search_days, 14))).isoformat(),
                "to_date": current.date().isoformat(),
                "enable_image_understanding": False,
                "enable_video_understanding": False,
            }
            excluded_handles = _watchlist_handles(("excluded", "low_quality"))
            if excluded_handles:
                search_tool["excluded_x_handles"] = excluded_handles[:20]
            request_args = {
                "model": os.getenv("XAI_MODEL", "grok-4.5"),
                "instructions": _fixed_instructions(),
                "input": user_input,
                "tools": [search_tool],
                "text": _schema(),
                "reasoning": {"effort": os.getenv("XAI_REASONING_EFFORT", "low")},
                "parallel_tool_calls": False,
                "max_tool_calls": len(selected),
                "max_output_tokens": min(
                    _env_int("XAI_MAX_OUTPUT_TOKENS", 2200, 400),
                    int(policy["max_output_tokens"]),
                ),
                "store": False,
            }
            if _env_bool("XAI_PROMPT_CACHE_ENABLED", True):
                request_args["prompt_cache_key"] = (
                    "finance-narrative:x-social-intelligence:v1"
                )
            response = api.responses.create(**request_args)
            parsed = json.loads(str(response.output_text))
        parsed = _validate_structured_result(parsed, selected)
        returned = {
            str(row.get("event_id")): row
            for row in parsed.get("events", [])
            if isinstance(row, dict)
        }
        observations = []
        useful_insights = 0
        opportunities = []
        exploration_reviews = []
        for event in selected:
            raw = returned.get(event.candidate_id, {})
            normalized_posts, metrics = normalize_posts(
                (raw.get("observed_posts") or [])[
                    :int(policy["max_observed_posts_per_event"])
                ]
            )
            confidence = str(raw.get("confidence") or "unknown")
            if event.source_type == "fx_movement" and confidence == "confirmed":
                confidence = "likely"
            interpretation = {
                key: raw.get(key)
                for key in (
                    "topic_summary", "why_people_are_discussing_it", "dominant_narrative",
                    "alternative_narratives", "strongest_dissent", "common_misconception",
                    "unanswered_questions", "useful_expert_points",
                    "market_implication_candidates", "facts_needing_confirmation",
                    "potentially_false_claims", "content_angles", "channel_fit",
                    "novelty_assessment",
                )
            }
            interpretation["confidence"] = confidence
            observation = {
                "observation_id": uuid.uuid4().hex,
                "run_id": run_id, "event_id": event.candidate_id,
                "radar_mode": mode, "observed_at": current.isoformat(),
                "query": event.xai_search_query,
                "metrics": metrics, "posts": normalized_posts,
                "interpretation": make_json_safe(interpretation),
            }
            previous = _latest_observation(event.candidate_id)
            observation["delta"] = compute_delta(observation, previous)
            append_jsonl("observations.jsonl", observation)
            append_jsonl("events.jsonl", {
                **event.to_dict(),
                "status": "researched",
                "run_id": run_id,
                "researched_at": current.isoformat(),
            })
            for post in normalized_posts:
                append_jsonl("posts.jsonl", {
                    **post, "event_id": event.candidate_id, "run_id": run_id,
                    "observed_at": current.isoformat(),
                })
                append_jsonl("accounts.jsonl", {
                    "run_id": run_id,
                    "event_id": event.candidate_id,
                    "observed_at": current.isoformat(),
                    "handle": post.get("account") or "",
                    "account_type": post.get("account_type") or "unknown",
                    "quality_score": post.get("account_quality_score") or 0,
                })
            append_jsonl("raw_results.jsonl", {
                "run_id": run_id,
                "event_id": event.candidate_id,
                "observed_at": current.isoformat(),
                "observed_post_ids": [
                    str(post.get("post_id") or "") for post in normalized_posts
                ],
                "metrics": metrics,
            })
            observations.append(observation)
            insight_count = sum(bool(interpretation.get(key)) for key in (
                "dominant_narrative", "strongest_dissent", "common_misconception",
            ))
            useful_insights += insight_count
            opportunities.extend(create_content_opportunities(event, observation))
            if event.source_type == "fx_movement":
                _save_fx_research_context(event, observation)
            if mode == "exploration":
                exploration_row = _review_exploration_candidate(event, observation)
                exploration_reviews.append(exploration_row)
                if exploration_row.get("eligible_for_news"):
                    append_jsonl("news_candidates.jsonl", exploration_row)
        try:
            integrated = integrate_research_results(
                event_ids=[item.candidate_id for item in selected],
                days=_env_int("XAI_INTEGRATION_LOOKBACK_DAYS", 3, 1),
                persist=True,
                now=current,
            )
        except Exception as integration_exc:
            integrated = {
                "status": "failed",
                "error_type": type(integration_exc).__name__,
                "analysis_count": 0,
                "created_count": 0,
                "analyses": [],
                "additional_api_calls": 0,
            }
        usage = _usage(response)
        cached_ids = {
            str(row.get("event_id") or "") for row in cached_observations
        }
        for event in preselected:
            if event.candidate_id in cached_ids:
                append_jsonl("events.jsonl", {
                    **event.to_dict(),
                    "status": "cache_satisfied",
                    "researched_at": current.isoformat(),
                })
        cost = usage.get("cost_usd")
        if cost is None:
            cost = usage.get("estimated_cost_usd") or 0.0
        run_row = {
            "timestamp": current.isoformat(), "run_id": run_id,
            "request_id": str(getattr(response, "id", "") or ""),
            "radar_mode": mode, "event_ids": [item.candidate_id for item in selected],
            "model": os.getenv("XAI_MODEL", "grok-4.5"),
            "status": "success", "success": True,
            **usage, "cost_usd": cost,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "cache_hit": bool(usage.get("cached_input_tokens")),
            "local_event_cache_hits": len(cached_observations),
            "events_researched": len(selected),
            "useful_events": sum(
                bool(row["interpretation"].get("dominant_narrative")) for row in observations
            ),
            "useful_insights": useful_insights,
            "unique_posts_returned": sum(
                int(row.get("metrics", {}).get("unique_original_posts") or 0)
                for row in observations
            ),
            "news_candidates_created": sum(
                bool(row.get("eligible_for_news")) for row in exploration_reviews
            ),
            "content_opportunities_created": len(opportunities),
            "integrated_analyses_created": int(integrated.get("created_count") or 0),
            "integrated_analysis_ids": [
                str(row.get("analysis_id") or "")
                for row in integrated.get("analyses") or []
            ],
            "integration_status": integrated.get("status"),
            "integration_error_type": integrated.get("error_type"),
            "posts_created": 0, "post_ids": [],
            "failure_stage": None, "error_type": None,
            "cost_policy": policy,
        }
        _record_run(run_row)
        _atomic_json(xai_dir() / "state.json", {
            "last_successful_run": run_id, "last_successful_at": current.isoformat(),
            "last_mode": mode, "last_event_ids": run_row["event_ids"],
        })
        try:
            from common.operations_alerts import notify_xai_research_result
            notify_xai_research_result(
                run_row, observations, opportunities,
                integrated_analyses=integrated.get("analyses") or [],
            )
        except Exception:
            pass
        return {
            "status": "success", "run_id": run_id, "radar_mode": mode,
            "events": observations, "opportunities": opportunities,
            "integrated_analysis": integrated,
            "cached_events": cached_observations,
            "usage": run_row, "x_post_called": False,
        }
    except Exception as exc:
        failure_stage = failure_stage or "request_or_parse"
        usage = _usage(response)
        row = {
            "timestamp": current.isoformat(), "run_id": run_id,
            "request_id": str(getattr(response, "id", "") or ""),
            "radar_mode": mode, "event_ids": [item.candidate_id for item in selected],
            "model": os.getenv("XAI_MODEL", "grok-4.5"),
            "status": "failed", "success": False, **usage,
            "cost_usd": usage.get("cost_usd") or usage.get("estimated_cost_usd") or 0.0,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "cache_hit": False, "events_researched": 0, "useful_events": 0,
            "useful_insights": 0, "unique_posts_returned": 0,
            "news_candidates_created": 0,
            "content_opportunities_created": 0, "posts_created": 0, "post_ids": [],
            "failure_stage": failure_stage, "error_type": type(exc).__name__,
        }
        _record_run(row)
        state = {}
        try:
            state = json.loads((xai_dir() / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        _atomic_json(xai_dir() / "state.json", {
            **state, "last_failure": type(exc).__name__,
            "last_failure_at": current.isoformat(),
        })
        try:
            from common.operations_alerts import notify_xai_research_result
            notify_xai_research_result(row, [], [])
        except Exception:
            pass
        return {
            "status": "failed", "reason": type(exc).__name__,
            "safe_failure": True, "events": [], "x_post_called": False,
        }


@dataclass
class ChannelOpportunity:
    opportunity_id: str
    event_id: str
    channel: str
    content_angle: str
    audience_question: str
    misconception: str
    dissent: str
    supporting_sources: list[str]
    freshness: str
    expected_lifetime: str
    effort_level: str
    monetization_relevance: str
    confidence: str
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return make_json_safe(asdict(self))


def create_content_opportunities(
    event: EventCandidate, observation: dict[str, Any]
) -> list[dict[str, Any]]:
    interpretation = observation.get("interpretation") or {}
    result = []
    for item in (interpretation.get("content_angles") or [])[:8]:
        if not isinstance(item, dict) or not item.get("angle"):
            continue
        channel = str(item.get("recommended_format") or "x")
        if channel not in {
            "x", "note_free", "note_paid", "threads", "youtube_short",
            "youtube_long", "newsletter", "weekly_report",
        }:
            channel = "x"
        raw = f"{event.candidate_id}|{channel}|{item.get('angle')}"
        opportunity = ChannelOpportunity(
            opportunity_id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20],
            event_id=event.candidate_id, channel=channel,
            content_angle=str(item.get("angle"))[:360],
            audience_question=str(interpretation.get("unanswered_questions", [""])[0]
                                  if interpretation.get("unanswered_questions") else "")[:240],
            misconception=str(interpretation.get("common_misconception") or "")[:240],
            dissent=str(interpretation.get("strongest_dissent") or "")[:240],
            supporting_sources=event.source_urls[:5],
            freshness="breaking" if event.urgency_score >= 8 else "current",
            expected_lifetime="24h" if event.urgency_score >= 8 else "7d",
            effort_level="low" if channel in {"x", "threads", "youtube_short"} else "medium",
            monetization_relevance="high" if channel in {"note_paid", "youtube_long"} else "medium",
            confidence=str(item.get("confidence") or "low"),
        )
        row = opportunity.to_dict()
        append_jsonl("content_opportunities.jsonl", {
            **row, "created_at": observation.get("observed_at"),
            "run_id": observation.get("run_id"),
        })
        result.append(row)
    return result


def _latest_event_rows() -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl("events.jsonl"):
        event_id = str(row.get("candidate_id") or "")
        if event_id:
            latest[event_id] = row
    return latest


def _analysis_anchors(event: dict[str, Any]) -> set[str]:
    anchors = {
        canonicalize(str(item))
        for item in (
            list(event.get("tickers") or [])
            + list(event.get("currencies") or [])
            + list(event.get("entities") or [])
            + list(event.get("countries") or [])
        )
        if canonicalize(str(item))
    }
    generic = {
        "ai", "news", "market", "markets", "stock", "stocks", "shares",
        "breaking", "update", "analysis", "決算", "市場", "株価", "急増",
        "急落", "上昇", "下落", "発表", "観測", "企業", "関連",
    }
    topic_tokens = {
        token for token in canonicalize(event.get("canonical_topic") or "").split()
        if len(token) >= 3 and token not in generic
    }
    return anchors | topic_tokens


def _related_events(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_tickers = normalize_tickers(left.get("tickers") or [])
    right_tickers = normalize_tickers(right.get("tickers") or [])
    if left_tickers and right_tickers and left_tickers[0] == right_tickers[0]:
        return True
    left_topic = canonicalize(left.get("canonical_topic") or left.get("title") or "")
    right_topic = canonicalize(right.get("canonical_topic") or right.get("title") or "")
    similarity = (
        SequenceMatcher(None, left_topic, right_topic).ratio()
        if left_topic and right_topic else 0.0
    )
    shared = _analysis_anchors(left) & _analysis_anchors(right)
    if left_tickers and right_tickers and set(left_tickers) & set(right_tickers):
        return similarity >= 0.40
    if len(shared) >= 2 and similarity >= 0.32:
        return True
    return bool(
        left_topic and right_topic and similarity >= 0.64
    )


def _group_related_event_ids(
    event_ids: list[str], events: dict[str, dict[str, Any]]
) -> list[list[str]]:
    remaining = list(dict.fromkeys(event_ids))
    groups: list[list[str]] = []
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for event_id in list(remaining):
                if any(
                    _related_events(events.get(event_id, {}), events.get(member, {}))
                    for member in group
                ):
                    group.append(event_id)
                    remaining.remove(event_id)
                    changed = True
        maximum = _env_int("XAI_INTEGRATION_MAX_EVENTS_PER_GROUP", 12, 2)
        groups.extend(
            group[index:index + maximum]
            for index in range(0, len(group), maximum)
        )
    return groups


def _text_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _cluster_statements(
    rows: list[tuple[str, str]], *, threshold: float = 0.72
) -> list[dict[str, Any]]:
    """Cluster paraphrased statements while preserving event provenance."""
    clusters: list[dict[str, Any]] = []
    for statement, event_id in rows:
        canonical = canonicalize(statement)
        if not canonical:
            continue
        match = next(
            (
                cluster for cluster in clusters
                if SequenceMatcher(None, canonical, cluster["canonical"]).ratio()
                >= threshold
            ),
            None,
        )
        if match is None:
            clusters.append({
                "statement": statement[:360],
                "canonical": canonical,
                "event_ids": [event_id],
                "mentions": 1,
            })
        else:
            match["mentions"] += 1
            if event_id not in match["event_ids"]:
                match["event_ids"].append(event_id)
    return sorted(
        clusters,
        key=lambda row: (len(row["event_ids"]), row["mentions"]),
        reverse=True,
    )


def _integrated_group(
    group: list[str],
    latest_observations: dict[str, dict[str, Any]],
    events: dict[str, dict[str, Any]],
    *,
    current: datetime,
) -> dict[str, Any]:
    observations_rows = [latest_observations[event_id] for event_id in group]
    narrative_rows: list[tuple[str, str]] = []
    implication_rows: list[tuple[str, str]] = []
    dissent: list[dict[str, str]] = []
    misconceptions: list[dict[str, str]] = []
    confirmation: list[dict[str, str]] = []
    open_questions: list[dict[str, str]] = []
    false_claims: list[dict[str, str]] = []
    observation_ids: list[str] = []
    run_ids: list[str] = []
    unique_accounts: set[str] = set()
    official_participation = False
    independent_commentary = 0
    for observation in observations_rows:
        event_id = str(observation.get("event_id") or "")
        interpretation = observation.get("interpretation") or {}
        observation_ids.append(str(observation.get("observation_id") or ""))
        run_ids.append(str(observation.get("run_id") or ""))
        for key in ("dominant_narrative", "alternative_narratives", "useful_expert_points"):
            narrative_rows.extend(
                (item, event_id) for item in _text_items(interpretation.get(key))
            )
        implication_rows.extend(
            (item, event_id)
            for item in _text_items(interpretation.get("market_implication_candidates"))
        )
        for target, key in (
            (dissent, "strongest_dissent"),
            (misconceptions, "common_misconception"),
            (confirmation, "facts_needing_confirmation"),
            (open_questions, "unanswered_questions"),
            (false_claims, "potentially_false_claims"),
        ):
            target.extend({
                "statement": item[:360], "event_id": event_id,
            } for item in _text_items(interpretation.get(key)))
        metrics = observation.get("metrics") or {}
        official_participation = official_participation or bool(
            metrics.get("official_account_participation")
        )
        independent_commentary += int(
            metrics.get("independent_commentary_count") or 0
        )
        for post in observation.get("posts") or []:
            account = str(post.get("account") or post.get("author_handle") or "")
            if account:
                unique_accounts.add(account.lower())
    source_evidence = [
        {
            "event_id": event_id,
            "source_type": events.get(event_id, {}).get("source_type"),
            "official": bool(events.get(event_id, {}).get("official")),
            "reliability_tier": events.get(event_id, {}).get("reliability_tier"),
            "confirmed_facts": list(
                events.get(event_id, {}).get("confirmed_facts") or []
            )[:8],
            "source_urls": list(events.get(event_id, {}).get("source_urls") or [])[:5],
            "market_movement": events.get(event_id, {}).get("market_movement") or {},
        }
        for event_id in group
    ]
    official_source_present = any(row["official"] for row in source_evidence)

    narrative_clusters = _cluster_statements(narrative_rows)
    implication_clusters = _cluster_statements(implication_rows)
    corroborated = [
        {
            "statement": row["statement"],
            "supporting_event_ids": row["event_ids"],
            "independent_event_count": len(row["event_ids"]),
        }
        for row in narrative_clusters if len(row["event_ids"]) >= 2
    ][:8]
    evidence_score = (
        (2 if len(group) >= 2 else 0)
        + (2 if official_participation or official_source_present else 0)
        + (2 if len(unique_accounts) >= 3 else 0)
        + (1 if independent_commentary >= 2 else 0)
    )
    evidence_quality = "high" if evidence_score >= 6 else (
        "medium" if evidence_score >= 3 else "low"
    )
    requires_confirmation = bool(confirmation or false_claims)
    posting_readiness = (
        "ready_for_draft"
        if evidence_quality in {"high", "medium"}
        and not requires_confirmation
        and len(unique_accounts) >= 2
        else "requires_confirmation"
    )
    titles = [
        str(events.get(event_id, {}).get("title") or event_id)
        for event_id in group
    ]
    anchor_sets = [
        _analysis_anchors(events.get(event_id, {})) for event_id in group
    ]
    shared_anchors = sorted(
        set.intersection(*anchor_sets) if anchor_sets and all(anchor_sets) else set()
    )
    leading = corroborated[0]["statement"] if corroborated else (
        narrative_clusters[0]["statement"] if narrative_clusters else "統合可能な見解なし"
    )
    return {
        "theme": " / ".join(titles[:3])[:360],
        "event_ids": group,
        "observation_ids": list(dict.fromkeys(observation_ids)),
        "run_ids": list(dict.fromkeys(run_ids)),
        "shared_anchors": shared_anchors[:20],
        "integrated_summary": leading[:500],
        "corroborated_findings": corroborated,
        "single_event_findings": [
            {"statement": row["statement"], "event_ids": row["event_ids"]}
            for row in narrative_clusters if len(row["event_ids"]) == 1
        ][:8],
        "market_implication_candidates": [
            {
                "statement": row["statement"],
                "supporting_event_ids": row["event_ids"],
            }
            for row in implication_clusters[:8]
        ],
        "dissent": dissent[:8],
        "common_misconceptions": misconceptions[:8],
        "potentially_false_claims": false_claims[:8],
        "facts_needing_confirmation": confirmation[:10],
        "unanswered_questions": open_questions[:10],
        "source_evidence": source_evidence,
        "evidence": {
            "event_count": len(group),
            "unique_account_count": len(unique_accounts),
            "independent_commentary_count": independent_commentary,
            "official_account_participation": official_participation,
            "official_source_present": official_source_present,
            "quality": evidence_quality,
        },
        "posting_readiness": posting_readiness,
        "automatic_posting_allowed": posting_readiness == "ready_for_draft",
        "human_review_required": False,
        "analysis_scope": "latest_observation_per_event_not_all_of_x",
        "analyzed_at": current.isoformat(),
    }


def _analysis_change(
    current: dict[str, Any], previous: dict[str, Any] | None
) -> tuple[bool, list[str]]:
    if not previous:
        return True, ["initial_analysis"]
    changes = []
    similarity = SequenceMatcher(
        None,
        canonicalize(current.get("integrated_summary") or ""),
        canonicalize(previous.get("integrated_summary") or ""),
    ).ratio()
    if similarity < 0.85:
        changes.append("summary_changed")
    if current.get("posting_readiness") != previous.get("posting_readiness"):
        changes.append("posting_readiness_changed")
    if (current.get("evidence") or {}).get("quality") != (
        previous.get("evidence") or {}
    ).get("quality"):
        changes.append("evidence_quality_changed")
    current_findings = {
        canonicalize(row.get("statement") or "")
        for row in current.get("corroborated_findings") or []
    }
    previous_findings = {
        canonicalize(row.get("statement") or "")
        for row in previous.get("corroborated_findings") or []
    }
    if current_findings != previous_findings:
        changes.append("corroborated_findings_changed")
    if len(current.get("facts_needing_confirmation") or []) != len(
        previous.get("facts_needing_confirmation") or []
    ):
        changes.append("confirmation_requirements_changed")
    return bool(changes), changes


def _integrated_draft_brief(analysis: dict[str, Any]) -> dict[str, Any]:
    ready = analysis.get("posting_readiness") == "ready_for_draft"
    return {
        "draft_id": hashlib.sha256(
            f"integrated|{analysis.get('analysis_id')}".encode("utf-8")
        ).hexdigest()[:20],
        "analysis_id": analysis.get("analysis_id"),
        "created_at": analysis.get("analyzed_at"),
        "status": "ready" if ready else "blocked_pending_confirmation",
        "recommended_lead": analysis.get("integrated_summary"),
        "confirmed_points": [
            row.get("statement")
            for row in analysis.get("corroborated_findings") or []
        ][:4],
        "market_implications": [
            row.get("statement")
            for row in analysis.get("market_implication_candidates") or []
        ][:3],
        "include_dissent": [
            row.get("statement") for row in analysis.get("dissent") or []
        ][:2],
        "must_verify": [
            row.get("statement")
            for row in analysis.get("facts_needing_confirmation") or []
        ][:5],
        "avoid_claiming": [
            row.get("statement")
            for row in analysis.get("potentially_false_claims") or []
        ][:5],
        "automatic_posting_allowed": ready,
        "human_review_required": False,
        "generation_scope": "editorial_brief_not_final_post",
    }


def _append_external_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(make_json_safe(row), ensure_ascii=False) + "\n")


def _save_integrated_downstream_context(analysis: dict[str, Any]) -> None:
    source_types = {
        str(row.get("source_type") or "")
        for row in analysis.get("source_evidence") or []
    }
    row = {
        "timestamp": analysis.get("analyzed_at"),
        "analysis_id": analysis.get("analysis_id"),
        "event_ids": analysis.get("event_ids") or [],
        "summary": analysis.get("integrated_summary"),
        "evidence": analysis.get("evidence") or {},
        "posting_readiness": analysis.get("posting_readiness"),
        "facts_needing_confirmation": analysis.get("facts_needing_confirmation") or [],
        "source_urls": list(dict.fromkeys(
            str(url)
            for source in analysis.get("source_evidence") or []
            for url in source.get("source_urls") or []
            if url
        ))[:10],
        "automatic_posting_allowed": bool(
            analysis.get("automatic_posting_allowed")
        ),
        "human_review_required": False,
    }
    if "fx_movement" in source_types:
        _append_external_jsonl(state_dir() / "fx" / "integrated_context.jsonl", row)
    if source_types & {
        "market_movement", "volume_anomaly", "market_map", "cross_asset_signal",
    }:
        _append_external_jsonl(
            state_dir() / "market_data" / "integrated_context.jsonl", row
        )


def import_legacy_research() -> dict[str, Any]:
    """Idempotently bridge legacy radar topics into the normalized observation DB."""
    legacy_rows = read_jsonl("topic_radar.jsonl")
    existing_observations = {
        str(row.get("observation_id") or "")
        for row in read_jsonl("observations.jsonl")
    }
    imported = 0
    event_ids = []
    for row in legacy_rows:
        topic = str(row.get("topic") or "").strip()
        observed_at = str(
            row.get("detected_at") or row.get("timestamp") or ""
        )
        if not topic or not observed_at:
            continue
        run_id = str(row.get("radar_run_id") or "legacy-radar")
        observation_id = hashlib.sha256(
            f"legacy|{run_id}|{topic}|{observed_at}".encode("utf-8")
        ).hexdigest()[:32]
        source_id = f"{run_id}:{topic}"
        candidate = make_candidate(
            source_type="legacy_radar",
            source_id=source_id,
            title=topic,
            tickers=row.get("tickers") or [],
            reliability_tier={
                "high": 2, "medium": 3, "low": 4,
            }.get(str(row.get("source_reliability") or "").lower(), 4),
            published_at=observed_at,
            source_urls=[
                str(post.get("url") or "")
                for post in row.get("representative_posts") or []
                if post.get("url")
            ],
            unconfirmed_claims=[str(row.get("summary") or "")],
            open_questions=[
                str(row.get("source_confirmation") or "一次情報での確認が必要")
            ],
            urgency_score=min(10, float(row.get("velocity_score") or 0)),
            market_impact_score=6,
            novelty_score=min(10, float(row.get("acceleration_score") or 0)),
            social_research_value=7,
            now=_parse_dt(observed_at),
        )
        event_ids.append(candidate.candidate_id)
        if observation_id in existing_observations:
            continue
        accounts = [
            str(item) for item in row.get("representative_accounts") or [] if item
        ]
        posts = [
            {
                "post_id": str(post.get("post_id") or ""),
                "url": str(post.get("url") or ""),
                "account": str(post.get("username") or ""),
                "excerpt": str(post.get("excerpt") or "")[:280],
                "account_type": "unknown",
                "account_quality_score": 0,
            }
            for post in row.get("representative_posts") or []
            if isinstance(post, dict)
        ]
        append_jsonl("events.jsonl", {
            **candidate.to_dict(),
            "status": "imported_legacy",
            "run_id": run_id,
            "researched_at": observed_at,
        })
        append_jsonl("observations.jsonl", {
            "observation_id": observation_id,
            "run_id": run_id,
            "event_id": candidate.candidate_id,
            "radar_mode": "legacy_import",
            "observed_at": observed_at,
            "query": "",
            "metrics": {
                "observed_mention_count": int(
                    row.get("observed_mention_count")
                    or row.get("mention_count") or 0
                ),
                "unique_original_posts": len(posts),
                "unique_accounts": len(set(accounts)),
                "independent_commentary_count": len(set(accounts)),
                "official_account_participation": False,
                "expert_account_participation": False,
                "legacy_import": True,
            },
            "posts": posts,
            "interpretation": {
                "topic_summary": str(row.get("summary") or ""),
                "why_people_are_discussing_it": topic,
                "dominant_narrative": str(row.get("summary") or ""),
                "alternative_narratives": [],
                "strongest_dissent": "",
                "common_misconception": "",
                "unanswered_questions": [
                    str(row.get("source_confirmation") or "一次情報での確認が必要")
                ],
                "useful_expert_points": [],
                "market_implication_candidates": [],
                "facts_needing_confirmation": [
                    "旧Radar結果は未確認の観測として移行。正式ソースでの再確認が必要"
                ],
                "potentially_false_claims": [],
                "content_angles": [],
                "channel_fit": [],
                "novelty_assessment": "legacy_radar_import",
                "confidence": "possible",
            },
            "delta": {
                "observed_velocity_score": float(row.get("velocity_score") or 0),
                "observed_acceleration_score": float(
                    row.get("acceleration_score") or 0
                ),
            },
        })
        imported += 1
    return {
        "status": "success",
        "legacy_rows": len(legacy_rows),
        "imported": imported,
        "already_present": max(0, len(legacy_rows) - imported),
        "event_ids": list(dict.fromkeys(event_ids)),
        "additional_api_calls": 0,
    }


def integrate_research_results(
    *,
    event_ids: Iterable[str] = (),
    days: int = 3,
    persist: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Integrate stored research locally without another xAI/OpenAI call."""
    current = _now(now)
    cutoff = current - timedelta(days=max(1, days))
    legacy_import = (
        import_legacy_research()
        if persist
        else {
            "status": "skipped_dry_run",
            "imported": 0,
            "additional_api_calls": 0,
        }
    )
    events = _latest_event_rows()
    requested = {str(item) for item in event_ids if str(item)}
    all_latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl("observations.jsonl"):
        observed = _parse_dt(row.get("observed_at"))
        event_id = str(row.get("event_id") or "")
        if not event_id or not observed or observed.astimezone(JST) < cutoff:
            continue
        previous = all_latest.get(event_id)
        if previous is None or str(row.get("observed_at") or "") > str(
            previous.get("observed_at") or ""
        ):
            all_latest[event_id] = row
    latest_observations = {
        event_id: row for event_id, row in all_latest.items()
        if not requested
        or event_id in requested
        or any(
            _related_events(events.get(event_id, {}), events.get(seed, {}))
            for seed in requested
        )
    }
    groups = _group_related_event_ids(list(latest_observations), events)
    analyses = [
        _integrated_group(group, latest_observations, events, current=current)
        for group in groups
    ]
    existing_rows = read_jsonl("integrated_analyses.jsonl")
    for analysis in analyses:
        stable = "|".join(sorted(analysis["observation_ids"]))
        analysis["analysis_id"] = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
        overlapping = [
            row for row in existing_rows
            if set(row.get("event_ids") or []) & set(analysis.get("event_ids") or [])
        ]
        previous = overlapping[-1] if overlapping else None
        material_change, change_reasons = _analysis_change(analysis, previous)
        analysis["version"] = int((previous or {}).get("version") or 0) + 1
        analysis["supersedes_analysis_id"] = (
            previous.get("analysis_id") if previous else None
        )
        analysis["material_change"] = material_change
        analysis["change_reasons"] = change_reasons
        analysis["draft_brief"] = _integrated_draft_brief(analysis)
    created = 0
    drafts_created = 0
    if persist:
        existing = {
            str(row.get("analysis_id") or "")
            for row in existing_rows
        }
        existing_drafts = {
            str(row.get("draft_id") or "")
            for row in read_jsonl("integrated_drafts.jsonl")
        }
        for analysis in analyses:
            if analysis["analysis_id"] not in existing:
                append_jsonl("integrated_analyses.jsonl", analysis)
                created += 1
                _save_integrated_downstream_context(analysis)
            draft = analysis["draft_brief"]
            if draft["draft_id"] not in existing_drafts:
                append_jsonl("integrated_drafts.jsonl", draft)
                drafts_created += 1
    return {
        "status": "success" if analyses else "no_research_results",
        "analyzed_at": current.isoformat(),
        "days": max(1, days),
        "event_count": len(latest_observations),
        "seed_event_count": len(requested),
        "related_prior_events_included": max(
            0, len(latest_observations) - len(requested & set(latest_observations))
        ),
        "legacy_import": legacy_import,
        "analysis_count": len(analyses),
        "created_count": created,
        "material_change_count": sum(
            bool(row.get("material_change")) for row in analyses
        ),
        "drafts_created": drafts_created,
        "persisted": persist,
        "additional_api_calls": 0,
        "analyses": analyses,
    }


def match_integrated_analysis(
    *, title: str = "", tickers: Iterable[str] = (), event_id: str = ""
) -> dict[str, Any] | None:
    """Find the latest matching synthesis without changing candidate ranking."""
    query = canonicalize(" ".join([title, *normalize_tickers(tickers)]))
    query_terms = set(query.split())
    best = None
    best_score = 0.0
    for analysis in read_jsonl("integrated_analyses.jsonl"):
        if event_id and event_id in set(analysis.get("event_ids") or []):
            score = 1.0
        else:
            theme = canonicalize(
                " ".join([
                    str(analysis.get("theme") or ""),
                    str(analysis.get("integrated_summary") or ""),
                    " ".join(analysis.get("shared_anchors") or []),
                ])
            )
            terms = set(theme.split())
            overlap = (
                len(query_terms & terms) / len(query_terms | terms)
                if query_terms | terms else 0.0
            )
            semantic = SequenceMatcher(None, query, theme).ratio() if query and theme else 0
            score = overlap * 0.6 + semantic * 0.4
        if score >= 0.32 and score >= best_score:
            best = {**analysis, "match_score": round(score, 3)}
            best_score = score
    return best


def record_integrated_analysis_use(
    *, analysis_id: str, use_type: str, reference_id: str
) -> dict[str, Any]:
    usage_id = hashlib.sha256(
        f"{analysis_id}|{use_type}|{reference_id}".encode("utf-8")
    ).hexdigest()[:20]
    existing = {
        str(row.get("usage_id") or "")
        for row in read_jsonl("integrated_analysis_usage.jsonl")
    }
    row = {
        "timestamp": datetime.now(JST).isoformat(),
        "usage_id": usage_id,
        "analysis_id": str(analysis_id or ""),
        "use_type": str(use_type or "")[:80],
        "reference_id_hash": hashlib.sha256(
            str(reference_id or "").encode("utf-8")
        ).hexdigest()[:20],
    }
    if analysis_id and usage_id not in existing:
        append_jsonl("integrated_analysis_usage.jsonl", row)
    return row


def _review_exploration_candidate(
    event: EventCandidate, observation: dict[str, Any]
) -> dict[str, Any]:
    metrics = observation.get("metrics") or {}
    interpretation = observation.get("interpretation") or {}
    prior_candidates = read_jsonl("news_candidates.jsonl", limit=200)
    not_prior_duplicate = not any(
        str(row.get("event_id") or "") == event.candidate_id
        or SequenceMatcher(
            None,
            canonicalize(str(row.get("title") or "")),
            event.canonical_topic,
        ).ratio() >= 0.85
        for row in prior_candidates
    )
    checks = {
        "multiple_independent_accounts": (
            int(metrics.get("unique_accounts") or 0) >= 2
            and int(metrics.get("independent_commentary_count") or 0) >= 2
        ),
        "not_single_url_copy": (
            int(metrics.get("unique_original_posts") or 0) >= 2
            and float(metrics.get("duplicate_ratio") or 0) < 0.75
        ),
        "trusted_source_signal": bool(
            metrics.get("official_account_participation")
            or metrics.get("expert_account_participation")
        ),
        "market_alignment": bool(event.market_movement),
        "not_prior_candidate_duplicate": not_prior_duplicate,
        "not_promotional": not any(
            row.get("account_type") in {"promotional", "bot_like"}
            for row in observation.get("posts") or []
        ),
        "facts_still_require_confirmation": bool(
            interpretation.get("facts_needing_confirmation")
        ),
    }
    eligible = all(checks.values())
    row = {
        "timestamp": observation.get("observed_at"),
        "run_id": observation.get("run_id"),
        "event_id": event.candidate_id,
        "title": event.title,
        "source_type": event.source_type,
        "checks": checks,
        "eligible_for_news": eligible,
        "posting_allowed": False,
        "requires_rss_or_official_confirmation": True,
        "reason": "all_safety_checks_passed" if eligible else "confirmation_gate_not_met",
    }
    append_jsonl("exploration_reviews.jsonl", row)
    return row


def _append_external_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(make_json_safe(row), ensure_ascii=False) + "\n")


def _save_fx_research_context(
    event: EventCandidate, observation: dict[str, Any]
) -> None:
    interpretation = observation.get("interpretation") or {}
    row = {
        "timestamp": observation.get("observed_at"),
        "movement_id": event.source_id,
        "event_id": event.candidate_id,
        "run_id": observation.get("run_id"),
        "confidence": interpretation.get("confidence") or "unknown",
        "cause_candidates": (
            interpretation.get("market_implication_candidates") or []
        )[:5],
        "dominant_narrative": str(
            interpretation.get("dominant_narrative") or ""
        )[:360],
        "strongest_dissent": str(
            interpretation.get("strongest_dissent") or ""
        )[:240],
        "potentially_false_claims": (
            interpretation.get("potentially_false_claims") or []
        )[:5],
        "facts_needing_confirmation": (
            interpretation.get("facts_needing_confirmation") or []
        )[:5],
        "official_sources": event.source_urls[:5],
        "xai_only_not_confirmed": True,
    }
    _append_external_jsonl(state_dir() / "fx" / "xai_context.jsonl", row)


def enqueue_fx_movement(movement: Any) -> dict[str, Any]:
    row = movement.to_dict() if hasattr(movement, "to_dict") else dict(movement)
    candidate = make_candidate(
        source_type="fx_movement",
        source_id=str(row.get("movement_id") or uuid.uuid4().hex),
        title=(
            f"{row.get('pair', 'USDJPY')} {row.get('window', '')} "
            f"{float(row.get('change_pct', 0) or 0):+.2f}% movement"
        ),
        entities=["USD/JPY"], currencies=["USD", "JPY"], countries=["US", "JP"],
        reliability_tier=2, published_at=str(row.get("detected_at") or ""),
        confirmed_facts=["価格変動は市場データで確認済み"],
        open_questions=["原因候補、公式発表候補、主要報道候補、反対意見、噂を区別する"],
        market_movement=row, urgency_score=9, market_impact_score=8,
        novelty_score=8, social_research_value=9,
    )
    append_jsonl("events.jsonl", {**candidate.to_dict(), "status": "queued"})
    (xai_dir() / "event_mode_until.txt").write_text(
        (datetime.now(JST) + timedelta(minutes=30)).isoformat(),
        encoding="utf-8",
    )
    _atomic_json(_event_trigger_path(), {
        "created_at": datetime.now(JST).isoformat(),
        "expires_at": (datetime.now(JST) + timedelta(minutes=30)).isoformat(),
        "event_id": candidate.candidate_id,
        "source_type": "fx_movement",
        "source_id": candidate.source_id,
        "reason": "material_fx_movement",
    })
    return candidate.to_dict()


def enqueue_market_movement(movement: Any) -> dict[str, Any]:
    row = movement.to_dict() if hasattr(movement, "to_dict") else dict(movement)
    symbol = str(row.get("symbol") or "market")
    alert_type = str(row.get("alert_type") or "market_movement")
    candidate = make_candidate(
        source_type=(
            "volume_anomaly" if alert_type == "volume_alert" else "market_movement"
        ),
        source_id=str(row.get("movement_id") or uuid.uuid4().hex),
        title=(
            f"{symbol} {row.get('window_minutes', '')}m "
            f"{float(row.get('percentage_change', 0) or 0):+.2f}% movement"
        ),
        entities=[symbol], tickers=[symbol],
        reliability_tier=2,
        published_at=str(row.get("detected_at") or ""),
        confirmed_facts=["価格・出来高変化は市場データで確認済み"],
        open_questions=[
            "同時刻の公式発表、主要報道、独立した原因候補、反対意見は何か"
        ],
        market_movement=row,
        urgency_score=9 if str(row.get("alert_level") or "") == "high" else 7,
        market_impact_score=8,
        novelty_score=8,
        social_research_value=9,
    )
    append_jsonl("events.jsonl", {**candidate.to_dict(), "status": "queued"})
    if str(row.get("alert_level") or "") in {"high", "critical"}:
        (xai_dir() / "event_mode_until.txt").write_text(
            (datetime.now(JST) + timedelta(minutes=30)).isoformat(),
            encoding="utf-8",
        )
        _atomic_json(_event_trigger_path(), {
            "created_at": datetime.now(JST).isoformat(),
            "expires_at": (datetime.now(JST) + timedelta(minutes=30)).isoformat(),
            "event_id": candidate.candidate_id,
            "source_type": candidate.source_type,
            "source_id": candidate.source_id,
            "reason": "material_market_movement",
        })
    return candidate.to_dict()


def enqueue_market_map_event(value: dict[str, Any]) -> dict[str, Any]:
    detected = datetime.now(JST)
    headline = str(value.get("headline") or "Large market map movement")
    source_id = hashlib.sha256(
        f"{detected:%Y-%m-%dT%H:%M}|{headline}".encode("utf-8")
    ).hexdigest()[:20]
    candidate = make_candidate(
        source_type="market_map",
        source_id=source_id,
        title=headline,
        entities=["S&P 500", str(value.get("top_sector") or "")],
        official=False,
        reliability_tier=2,
        confirmed_facts=[
            f"時価総額変化: {float(value.get('market_move') or 0):+.0f} USD",
            f"推定指数変化: {float(value.get('total_pct') or 0):+.2f}%",
        ],
        open_questions=[
            "広範な市場変動と整合する公式発表・主要報道・反対意見は何か"
        ],
        market_movement=make_json_safe(value),
        urgency_score=9,
        market_impact_score=9,
        novelty_score=8,
        social_research_value=9,
        now=detected,
    )
    append_jsonl("events.jsonl", {**candidate.to_dict(), "status": "queued"})
    (xai_dir() / "event_mode_until.txt").write_text(
        (detected + timedelta(minutes=30)).isoformat(), encoding="utf-8"
    )
    _atomic_json(_event_trigger_path(), {
        "created_at": detected.isoformat(),
        "expires_at": (detected + timedelta(minutes=30)).isoformat(),
        "event_id": candidate.candidate_id,
        "source_type": "market_map",
        "source_id": source_id,
        "reason": "large_market_map_change",
    })
    return candidate.to_dict()


def enqueue_cross_asset_signal(value: dict[str, Any]) -> dict[str, Any]:
    detected = _parse_dt(value.get("detected_at")) or datetime.now(JST)
    movements = value.get("movements") if isinstance(value.get("movements"), dict) else {}
    primary = str(value.get("primary_symbol") or "cross-asset")
    candidate = make_candidate(
        source_type="cross_asset_signal",
        source_id=str(value.get("signal_id") or uuid.uuid4().hex),
        title=f"{primary} cross-asset {value.get('pattern_type') or 'movement'}",
        entities=[primary, *(value.get("related_symbols") or [])],
        tickers=[primary, *(value.get("related_symbols") or [])],
        reliability_tier=2,
        published_at=detected.isoformat(),
        confirmed_facts=[
            "複数資産の同時変動は市場データで観測済み",
            "同時変動だけでは因果関係を確定しない",
        ],
        open_questions=[
            "同時刻の公式発表、主要報道、共通認識、反対解釈は何か"
        ],
        market_movement=make_json_safe(value),
        urgency_score=8,
        market_impact_score=8,
        novelty_score=8,
        social_research_value=9,
        now=detected,
    )
    append_jsonl("events.jsonl", {**candidate.to_dict(), "status": "queued"})
    if movements and max(abs(float(item or 0)) for item in movements.values()) >= 1.0:
        _atomic_json(_event_trigger_path(), {
            "created_at": datetime.now(JST).isoformat(),
            "expires_at": (datetime.now(JST) + timedelta(minutes=30)).isoformat(),
            "event_id": candidate.candidate_id,
            "source_type": "cross_asset_signal",
            "source_id": candidate.source_id,
            "reason": "material_cross_asset_signal",
        })
    return candidate.to_dict()


def match_news_event(
    *, title: str, url: str = "", tickers: Iterable[str] = (),
    event_type: str = "", published_at: str = "",
) -> dict[str, Any] | None:
    title_canonical = canonicalize(title)
    ticker_set = set(normalize_tickers(tickers or TICKER_RE.findall(title)))
    candidates = read_jsonl("events.jsonl")
    observations = read_jsonl("observations.jsonl")
    latest_by_event: dict[str, dict[str, Any]] = {}
    for row in observations:
        event_id = str(row.get("event_id") or "")
        if event_id and str(row.get("observed_at") or "") > str(
            latest_by_event.get(event_id, {}).get("observed_at") or ""
        ):
            latest_by_event[event_id] = row
    best = None
    best_score = 0.0
    for event in candidates:
        event_id = str(event.get("candidate_id") or "")
        observation = latest_by_event.get(event_id)
        if not observation:
            continue
        event_tickers = set(normalize_tickers(event.get("tickers") or []))
        entity_score = 1.0 if ticker_set and ticker_set & event_tickers else 0.0
        semantic = SequenceMatcher(
            None, title_canonical, str(event.get("canonical_topic") or "")
        ).ratio()
        title_terms = set(title_canonical.split())
        event_terms = set(str(event.get("canonical_topic") or "").split())
        key_term_score = (
            len(title_terms & event_terms) / len(title_terms | event_terms)
            if title_terms | event_terms else 0.0
        )
        type_score = 1.0 if event_type and event_type == event.get("event_type") else 0.0
        official_score = 0.5 if set(event.get("source_urls") or []) & {url} else 0.0
        event_time = _parse_dt(event.get("published_at") or event.get("detected_at"))
        news_time = _parse_dt(published_at)
        time_score = (
            1.0 if event_time and news_time
            and abs((event_time - news_time).total_seconds()) <= 86400 else 0.0
        )
        score = (
            entity_score * 0.30
            + semantic * 0.30
            + key_term_score * 0.15
            + type_score * 0.10
            + official_score * 0.10
            + time_score * 0.05
        )
        if score > best_score and score >= 0.45:
            best_score = score
            best = {**event, "observation": observation, "match_score": round(score, 3)}
    return best


def cost_attribution(run_id: str) -> float:
    """Attribute one run equally across its researched events."""
    matches = [
        row for row in read_jsonl("runs.jsonl")
        if str(row.get("run_id") or "") == str(run_id or "")
    ]
    if not matches:
        return 0.0
    row = matches[-1]
    denominator = max(1, int(row.get("events_researched") or 0))
    return round(float(row.get("cost_usd") or 0) / denominator, 8)


def shadow_record_news(
    *, title: str, matched: dict[str, Any] | None, original_rank: int,
    hypothetical_rank: int, posted: bool = False, tweet_id: str = "",
) -> dict[str, Any]:
    row = {
        "timestamp": datetime.now(JST).isoformat(),
        "shadow_days_required": _env_int("XAI_SCORE_BONUS_SHADOW_DAYS", 14),
        "score_bonus_enabled": _env_bool("XAI_SCORE_BONUS_ENABLED", False),
        "title_hash": hashlib.sha256(title.encode("utf-8")).hexdigest()[:16],
        "xai_event_id": (matched or {}).get("candidate_id"),
        "xai_run_id": (matched or {}).get("observation", {}).get("run_id"),
        "original_rank": original_rank, "hypothetical_rank": hypothetical_rank,
        "rank_changed": original_rank != hypothetical_rank,
        "posted": posted, "tweet_id": tweet_id,
    }
    append_jsonl("score_bonus_shadow.jsonl", row)
    return row


def record_post_outcome(
    *, run_id: str, event_id: str, tweet_id: str, title: str = ""
) -> dict[str, Any]:
    row = {
        "timestamp": datetime.now(JST).isoformat(),
        "run_id": str(run_id or ""),
        "event_id": str(event_id or ""),
        "tweet_id": str(tweet_id or ""),
        "title": str(title or "")[:240],
        "posted": bool(tweet_id),
        "outcome": "post_created" if tweet_id else "not_posted",
    }
    append_jsonl("outcomes.jsonl", row)
    append_jsonl("score_bonus_shadow.jsonl", {
        **row,
        "original_rank": None,
        "hypothetical_rank": None,
        "rank_changed": None,
        "matched": True,
        "score_bonus_applied": False,
    })
    return row


def record_content_opportunity_use(
    *, run_id: str, event_id: str, use_type: str, reference_id: str
) -> dict[str, Any]:
    stable = hashlib.sha256(
        f"{run_id}|{event_id}|{use_type}|{reference_id}".encode("utf-8")
    ).hexdigest()[:20]
    existing = {
        str(row.get("usage_id") or "")
        for row in read_jsonl("content_opportunity_usage.jsonl")
    }
    row = {
        "timestamp": datetime.now(JST).isoformat(),
        "usage_id": stable,
        "run_id": str(run_id or ""),
        "event_id": str(event_id or ""),
        "use_type": str(use_type or "")[:80],
        "reference_id_hash": hashlib.sha256(
            str(reference_id or "").encode("utf-8")
        ).hexdigest()[:20],
    }
    if stable not in existing:
        append_jsonl("content_opportunity_usage.jsonl", row)
    return row


def list_events(limit: int = 50) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl("events.jsonl"):
        key = str(row.get("candidate_id") or "")
        if key:
            latest[key] = row
    return list(latest.values())[-max(1, limit):]


def event_show(event_id: str) -> dict[str, Any] | None:
    return next((row for row in reversed(list_events(500)) if row.get("candidate_id") == event_id), None)


def observations(event_id: str) -> list[dict[str, Any]]:
    return [row for row in read_jsonl("observations.jsonl") if row.get("event_id") == event_id]


def delta(event_id: str) -> dict[str, Any]:
    rows = observations(event_id)
    if not rows:
        return {"status": "not_found", "event_id": event_id}
    return rows[-1].get("delta") or {"comparable": False, "reason": "missing_delta"}


def exploration_status(days: int = 30) -> dict[str, Any]:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    runs = [
        row for row in read_jsonl("runs.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    total = sum(float(row.get("cost_usd") or 0) for row in runs)
    explore_rows = [row for row in runs if row.get("radar_mode") == "exploration"]
    explore_run_ids = {str(row.get("run_id") or "") for row in explore_rows}
    usage_rows = [
        row for row in read_jsonl("content_opportunity_usage.jsonl")
        if str(row.get("run_id") or "") in explore_run_ids
    ]
    used_run_ids = {str(row.get("run_id") or "") for row in usage_rows}
    exploration_cost = sum(float(row.get("cost_usd") or 0) for row in explore_rows)
    used = len(usage_rows)
    return {
        "enabled": _env_bool("XAI_EXPLORATION_ENABLED", True),
        "runs": len(explore_rows), "cost_usd": round(exploration_cost, 6),
        "budget_share": round(exploration_cost / total, 4) if total else 0.0,
        "budget_limit": _env_float("XAI_EXPLORATION_BUDGET_PERCENT", 20.0) / 100,
        "unused_rate": round(
            sum(
                1 for row in explore_rows
                if str(row.get("run_id") or "") not in used_run_ids
                and not int(row.get("news_candidates_created") or 0)
            ) / len(explore_rows), 4
        ) if explore_rows else None,
        "outputs_used": used,
    }


def budget_status() -> dict[str, Any]:
    from common.xai_radar_v2 import usage_summary
    usage = usage_summary()
    current = datetime.now(JST)
    daily_cost = sum(
        float(row.get("cost_usd") or 0) for row in read_jsonl("runs.jsonl")
        if str(row.get("timestamp") or "").startswith(current.date().isoformat())
    )
    return {
        **usage, "daily_social_intelligence_cost_usd": round(daily_cost, 6),
        "daily_soft_budget_usd": _env_float("XAI_DAILY_SOFT_BUDGET_USD", 0.75),
        "cost_per_run_warning_usd": _env_float("XAI_COST_PER_RUN_WARNING_USD", 0.30),
        "exploration": exploration_status(),
        "adaptive_cost_policy": adaptive_cost_policy(),
        "social_cache": social_cache_status(),
    }


def key_safety_status() -> dict[str, Any]:
    """Check for the current key without returning or logging its value."""
    load_env()
    key = os.getenv("XAI_API_KEY", "")
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    current_matches: list[str] = []
    webhook_matches: list[str] = []
    roots = [REPO_ROOT / "src", REPO_ROOT / "config", REPO_ROOT / "logs", state_dir()]
    if key or webhook:
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {
                    ".py", ".json", ".jsonl", ".log", ".md", ".txt", ".yml", ".yaml",
                }:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    if key and key in text:
                        current_matches.append(str(path.relative_to(REPO_ROOT)))
                    if webhook and webhook in text:
                        webhook_matches.append(str(path.relative_to(REPO_ROOT)))
                except OSError:
                    continue
    git_match = False
    git_webhook_match = False
    if key or webhook:
        try:
            history = subprocess.run(
                ["git", "log", "-p", "--all", "--", ".", ":(exclude).env"],
                cwd=REPO_ROOT, capture_output=True, check=False, timeout=30,
            ).stdout
            git_match = bool(key and key.encode("utf-8") in history)
            git_webhook_match = bool(webhook and webhook.encode("utf-8") in history)
        except (OSError, subprocess.TimeoutExpired):
            git_match = False
            git_webhook_match = False
    verified = _env_bool("XAI_KEY_ROTATION_VERIFIED", False)
    return {
        "api_key_configured": bool(key),
        "rotation_verification": "verified" if verified else "rotation verification required",
        "current_key_found_in_scanned_files": bool(current_matches),
        "matching_file_count": len(current_matches),
        "matching_paths": sorted(current_matches)[:20],
        "current_key_found_in_git_history": git_match,
        "current_webhook_found_in_scanned_files": bool(webhook_matches),
        "current_webhook_matching_file_count": len(webhook_matches),
        "current_webhook_found_in_git_history": git_webhook_match,
        "safe_disable_flags": ["XAI_SAFE_DISABLED=true", "XAI_ENABLED=false"],
        "secret_value_returned": False,
        "operator_action_required": (
            not verified or bool(current_matches) or git_match
            or bool(webhook_matches) or git_webhook_match
        ),
        "human_review_required": False,
    }


def shadow_report(days: int = 14) -> dict[str, Any]:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    rows = [
        row for row in read_jsonl("score_bonus_shadow.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    observed_dates = {
        str(row.get("timestamp") or "")[:10] for row in rows if row.get("timestamp")
    }
    tweet_ids = {str(row.get("tweet_id")) for row in rows if row.get("tweet_id")}
    metrics = [
        row for row in _read_external_jsonl(state_dir() / "metrics_snapshots.jsonl")
        if str(row.get("tweet_id")) in tweet_ids
    ]
    return {
        "days": days, "observed_days": len(observed_dates), "rows": len(rows),
        "rank_changes": sum(bool(row.get("rank_changed")) for row in rows),
        "actual_posts": sum(bool(row.get("posted")) for row in rows),
        "metrics_1h": sum(row.get("stage") == "1h" and row.get("status") == "collected" for row in metrics),
        "metrics_6h": sum(row.get("stage") == "6h" and row.get("status") == "collected" for row in metrics),
        "metrics_24h": sum(row.get("stage") == "24h" and row.get("status") == "collected" for row in metrics),
        "score_bonus_enabled": _env_bool("XAI_SCORE_BONUS_ENABLED", False),
        "human_review_required": False,
        "automatic_activation_eligible": (
            len(observed_dates)
            >= _env_int("XAI_SCORE_BONUS_SHADOW_DAYS", 14)
            and len(rows) >= _env_int("XAI_SCORE_BONUS_MIN_OBSERVATIONS", 20)
            and any(row.get("stage") == "24h" for row in metrics)
        ),
    }


def _read_external_jsonl(path: Path) -> list[dict[str, Any]]:
    result = []
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                result.append(value)
        except json.JSONDecodeError:
            continue
    return result


def social_report(days: int = 30) -> dict[str, Any]:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    runs = [
        row for row in read_jsonl("runs.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    observations_rows = [
        row for row in read_jsonl("observations.jsonl")
        if (_parse_dt(row.get("observed_at")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    integrated_rows = [
        row for row in read_jsonl("integrated_analyses.jsonl")
        if (_parse_dt(row.get("analyzed_at")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    integrated_drafts = [
        row for row in read_jsonl("integrated_drafts.jsonl")
        if (_parse_dt(row.get("created_at")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    integrated_usage = [
        row for row in read_jsonl("integrated_analysis_usage.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=JST)) >= cutoff
    ]
    successful = [row for row in runs if row.get("status") == "success"]
    used_run_ids = {
        str(row.get("run_id") or "")
        for row in (
            read_jsonl("content_opportunity_usage.jsonl")
            + read_jsonl("outcomes.jsonl")
        )
        if row.get("run_id")
    }
    post_history: list[dict[str, Any]] = []
    try:
        value = json.loads((state_dir() / "posted_history.json").read_text(encoding="utf-8"))
        if isinstance(value, list):
            post_history = [row for row in value if isinstance(row, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    run_ids = {str(row.get("run_id") or "") for row in runs}
    xai_posts = [
        row for row in post_history
        if str(row.get("xai_run_id") or row.get("radar_run_id") or "") in run_ids
    ]
    xai_tweet_ids = {str(row.get("tweet_id") or "") for row in xai_posts}
    normal_tweet_ids = {
        str(row.get("tweet_id") or "") for row in post_history
        if row.get("tweet_id") and not row.get("xai_researched")
    }
    snapshots = _read_external_jsonl(state_dir() / "metrics_snapshots.jsonl")

    def stage_median(tweet_ids: set[str], stage: str) -> float | None:
        values = [
            float(row.get("impressions") or row.get("impression_count") or 0)
            for row in snapshots
            if str(row.get("tweet_id") or "") in tweet_ids
            and str(row.get("stage") or "") == stage
            and (row.get("impressions") is not None or row.get("impression_count") is not None)
        ]
        return round(statistics.median(values), 2) if values else None
    total_cost = sum(float(row.get("cost_usd") or 0) for row in runs)
    useful_insights = sum(int(row.get("useful_insights") or 0) for row in runs)
    modes: dict[str, int] = {}
    failures: dict[str, int] = {}
    for row in runs:
        modes[str(row.get("radar_mode") or "unknown")] = modes.get(
            str(row.get("radar_mode") or "unknown"), 0
        ) + 1
        if row.get("status") != "success":
            key = str(row.get("error_type") or "unknown")
            failures[key] = failures.get(key, 0) + 1
    all_metrics = [row.get("metrics") or {} for row in observations_rows]
    deltas = [row.get("delta") or {} for row in observations_rows]
    velocity_rows = [
        row for row in observations_rows
        if (row.get("delta") or {}).get("observed_velocity_score") is not None
    ]
    acceleration_rows = [
        row for row in observations_rows
        if (row.get("delta") or {}).get("observed_acceleration_score") is not None
    ]
    fastest = max(
        velocity_rows,
        key=lambda row: float((row.get("delta") or {}).get("observed_velocity_score") or -1),
        default=None,
    )
    fastest_acceleration = max(
        acceleration_rows,
        key=lambda row: float(
            (row.get("delta") or {}).get("observed_acceleration_score") or -1
        ),
        default=None,
    )
    dominant_narratives = [
        str((row.get("interpretation") or {}).get("dominant_narrative") or "")
        for row in observations_rows
        if (row.get("interpretation") or {}).get("dominant_narrative")
    ]
    total_unique_posts = sum(
        int(row.get("unique_original_posts") or 0) for row in all_metrics
    )
    total_unique_accounts = sum(
        int(row.get("unique_accounts") or 0) for row in all_metrics
    )
    total_impressions = sum(
        float(row.get("impressions") or row.get("impression_count") or 0)
        for row in snapshots
        if str(row.get("tweet_id") or "") in xai_tweet_ids
        and str(row.get("stage") or "") == "24h"
    )
    posted_count = len(xai_posts)
    usage_rows = [
        row for row in read_jsonl("content_opportunity_usage.jsonl")
        if (_parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=JST)) >= cutoff
        and str(row.get("run_id") or "") in run_ids
    ]
    matched_news_candidates = {
        str(row.get("usage_id") or "")
        for row in usage_rows
        if row.get("use_type") == "news_candidate_match"
    }
    exploration_news_candidates = sum(
        int(row.get("news_candidates_created") or 0) for row in runs
    )
    news_candidate_count = exploration_news_candidates + len(matched_news_candidates)
    return {
        "days": days, "runs": len(runs), "successful_runs": len(successful),
        "success_rate": round(len(successful) / len(runs), 4) if runs else None,
        "runs_by_mode": modes,
        "events_researched": sum(int(row.get("events_researched") or 0) for row in runs),
        "useful_events": sum(int(row.get("useful_events") or 0) for row in runs),
        "useful_insights": useful_insights,
        "unique_original_posts": total_unique_posts,
        "unique_accounts": total_unique_accounts,
        "independent_commentary_count": sum(
            int(row.get("independent_commentary_count") or 0)
            for row in all_metrics
        ),
        "official_participations": sum(bool(row.get("official_account_participation")) for row in all_metrics),
        "expert_participations": sum(bool(row.get("expert_account_participation")) for row in all_metrics),
        "max_velocity_event_id": fastest.get("event_id") if fastest else None,
        "max_observed_velocity_score": (
            (fastest.get("delta") or {}).get("observed_velocity_score") if fastest else None
        ),
        "max_acceleration_event_id": (
            fastest_acceleration.get("event_id") if fastest_acceleration else None
        ),
        "max_observed_acceleration_score": (
            (fastest_acceleration.get("delta") or {}).get(
                "observed_acceleration_score"
            ) if fastest_acceleration else None
        ),
        "dominant_narratives": list(dict.fromkeys(dominant_narratives))[:5],
        "dissent_count": sum(bool((row.get("interpretation") or {}).get("strongest_dissent")) for row in observations_rows),
        "misconception_count": sum(bool((row.get("interpretation") or {}).get("common_misconception")) for row in observations_rows),
        "news_candidates_created": news_candidate_count,
        "content_opportunities_created": sum(int(row.get("content_opportunities_created") or 0) for row in runs),
        "integrated_analyses": len(integrated_rows),
        "integrated_ready_for_draft": sum(
            row.get("posting_readiness") == "ready_for_draft"
            for row in integrated_rows
        ),
        "integrated_requires_confirmation": sum(
            row.get("posting_readiness") == "requires_confirmation"
            for row in integrated_rows
        ),
        "integrated_high_evidence": sum(
            (row.get("evidence") or {}).get("quality") == "high"
            for row in integrated_rows
        ),
        "integrated_material_changes": sum(
            bool(row.get("material_change")) for row in integrated_rows
        ),
        "integrated_drafts": len(integrated_drafts),
        "integrated_ready_drafts": sum(
            row.get("status") == "ready" for row in integrated_drafts
        ),
        "integrated_usage": len(integrated_usage),
        "integrated_post_conversions": sum(
            row.get("use_type") == "post_created" for row in integrated_usage
        ),
        "integrated_unused_rate": round(
            sum(
                str(row.get("analysis_id") or "") not in {
                    str(item.get("analysis_id") or "") for item in integrated_usage
                }
                for row in integrated_rows
            ) / len(integrated_rows),
            4,
        ) if integrated_rows else None,
        "posts_created": posted_count,
        "unused_result_rate": round(
            sum(
                str(row.get("run_id") or "") not in used_run_ids
                and not int(row.get("news_candidates_created") or 0)
                for row in successful
            )
            / len(successful), 4
        ) if successful else None,
        "exploration": exploration_status(days),
        "total_cost_usd": round(total_cost, 6),
        "cost_per_run_usd": round(total_cost / len(runs), 6) if runs else None,
        "cost_per_success_usd": round(
            total_cost / len(successful), 6
        ) if successful else None,
        "cost_per_useful_insight_usd": round(total_cost / useful_insights, 6) if useful_insights else None,
        "cost_per_event_usd": round(
            total_cost / sum(int(row.get("events_researched") or 0) for row in runs), 6
        ) if sum(int(row.get("events_researched") or 0) for row in runs) else None,
        "cost_per_content_opportunity_usd": round(
            total_cost / sum(int(row.get("content_opportunities_created") or 0) for row in runs),
            6,
        ) if sum(int(row.get("content_opportunities_created") or 0) for row in runs) else None,
        "cost_per_news_candidate_usd": round(
            total_cost / news_candidate_count,
            6,
        ) if news_candidate_count else None,
        "cost_per_post_usd": round(total_cost / posted_count, 6) if posted_count else None,
        "cost_per_1000_impressions_usd": round(
            total_cost / total_impressions * 1000, 6
        ) if total_impressions else None,
        "xai_post_median_impressions": {
            stage: stage_median(xai_tweet_ids, stage) for stage in ("1h", "6h", "24h")
        },
        "normal_post_median_impressions": {
            stage: stage_median(normal_tweet_ids, stage) for stage in ("1h", "6h", "24h")
        },
        "cache_hit_rate": round(
            sum(bool(row.get("cache_hit")) for row in runs) / len(runs), 4
        ) if runs else None,
        "social_event_cache": social_cache_status(days),
        "useful_insight_rate": round(
            useful_insights
            / sum(int(row.get("events_researched") or 0) for row in runs),
            4,
        ) if sum(int(row.get("events_researched") or 0) for row in runs) else None,
        "failure_reasons": failures,
        "remaining_budget_usd": budget_status().get("remaining_usd"),
        "key_rotation_verified": _env_bool("XAI_KEY_ROTATION_VERIFIED", False),
        "recommended_next_allocation": (
            "event_reaction優先、22:30は再観測とmovement_explanation。"
            "explorationは総費用の20%未満。"
        ),
        "measurement_scope": "observed_search_results_not_all_of_x",
    }


def cleanup() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    policies = {
        "raw_results.jsonl": 7, "posts.jsonl": 30,
        "observations.jsonl": 180, "events.jsonl": 180,
        "accounts.jsonl": 180, "integrated_analyses.jsonl": 180,
        "integrated_drafts.jsonl": 180,
        "integrated_analysis_usage.jsonl": 180,
    }
    removed = 0
    for name, days in policies.items():
        rows = read_jsonl(name)
        kept = []
        for row in rows:
            when = _parse_dt(
                row.get("timestamp") or row.get("observed_at")
                or row.get("detected_at") or row.get("created_at")
            )
            if when and when.astimezone(timezone.utc) < now - timedelta(days=days):
                removed += 1
            else:
                kept.append(row)
        if rows:
            path = xai_dir() / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                "".join(json.dumps(make_json_safe(row), ensure_ascii=False) + "\n" for row in kept),
                encoding="utf-8",
            )
            os.replace(temporary, path)
    return {"status": "success", "removed_rows": removed, "policies_days": policies}
