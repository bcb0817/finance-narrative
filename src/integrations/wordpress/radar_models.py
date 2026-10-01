from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from common.runtime import JST
from .sanitization import clean_text, contains_secret_shape, safe_x_url

DISCLAIMER = (
    "このページはX Searchで取得した限定的な検索結果を分析したもので、"
    "X全体の投稿量や市場全体の意見を示すものではありません。"
)
ALLOWED_TRENDS = {"up", "flat", "down"}
ALLOWED_RELIABILITY = {"high", "medium", "low", "unknown"}


def _score(value: Any) -> float:
    try:
        return round(max(0.0, min(100.0, float(value or 0))), 2)
    except (TypeError, ValueError):
        return 0.0


def attention_level(velocity: float, acceleration: float) -> str:
    score = max(_score(velocity), _score(acceleration))
    very_high = float(os.getenv("X_RADAR_ATTENTION_VERY_HIGH", "85"))
    high = float(os.getenv("X_RADAR_ATTENTION_HIGH", "70"))
    normal = float(os.getenv("X_RADAR_ATTENTION_NORMAL", "35"))
    return "VERY_HIGH" if score >= very_high else "HIGH" if score >= high else "NORMAL" if score >= normal else "LOW"


def trend_direction(velocity: float, acceleration: float, raw: object = "") -> str:
    explicit = str(raw or "").lower()
    if explicit in ALLOWED_TRENDS:
        return explicit
    acceleration = _score(acceleration)
    if acceleration >= 65:
        return "up"
    if acceleration <= 25 and _score(velocity) <= 35:
        return "down"
    return "flat"


def observed_radar_score(velocity: float, acceleration: float, market_impact: float,
                         reliability: str, official: bool) -> float:
    """Bounded observation score, never a measure of all X activity."""
    reliability_points = {"high": 10.0, "medium": 6.0, "low": 2.0}.get(reliability, 0.0)
    impact = _score(market_impact)
    return round(min(100.0, _score(velocity) * .4 + _score(acceleration) * .2
                     + impact * .2 + reliability_points + (10.0 if official else 0.0)), 2)


@dataclass(frozen=True)
class RepresentativePost:
    post_id: str = ""
    handle: str = ""
    account_type: str = "unknown"
    url: str = ""
    short_summary: str = "この論点について言及"
    why_selected: str = "今回の検索結果で観測された代表例"

    @classmethod
    def from_internal(cls, row: dict) -> "RepresentativePost | None":
        url = safe_x_url(row.get("url"))
        if not url:
            return None
        return cls(
            post_id=clean_text(row.get("post_id"), 32),
            handle=clean_text(row.get("handle") or row.get("username") or row.get("account"), 32).lstrip("@"),
            account_type=clean_text(row.get("account_type"), 40) or "unknown",
            url=url,
            short_summary=clean_text(row.get("short_summary") or "この論点について言及", 100),
            why_selected=clean_text(row.get("why_selected") or "今回の検索結果で観測された代表例", 160),
        )


@dataclass(frozen=True)
class PublicRadarTopic:
    topic_id: str
    rank: int
    title: str
    canonical_topic: str
    tickers: list[str]
    entities: list[str]
    category: str
    summary: str
    observed_velocity_score: float
    observed_acceleration_score: float
    observed_radar_score: float
    trend_direction: str
    attention_level: str
    reliability: str
    official_source_present: bool
    source_confirmation_status: str
    dominant_narrative: str
    dissent: str
    misconception: str
    representative_posts: list[RepresentativePost] = field(default_factory=list)
    translations: dict[str, dict[str, str]] = field(default_factory=dict)
    updated_at: str = ""


@dataclass(frozen=True)
class PublicRadarSnapshot:
    schema_version: int
    snapshot_id: str
    run_id: str
    observed_at: str
    generated_at: str
    radar_type: str
    disclaimer: str
    topics: list[PublicRadarTopic]

    def to_dict(self) -> dict:
        payload = asdict(self)
        if contains_secret_shape(payload):
            raise ValueError("public payload contains a forbidden secret-shaped field")
        return payload


def _normalized_score(row: dict, *keys: str) -> float:
    for key in keys:
        if row.get(key) is not None:
            value = _score(row[key])
            # Legacy radar used a 0..10 scale.
            return value * 10 if 0 < value <= 10 else value
    return 0.0


def build_snapshot(internal: dict, *, generated_at: datetime | None = None) -> PublicRadarSnapshot:
    now = (generated_at or datetime.now(JST)).astimezone(JST)
    observed = clean_text(internal.get("observed_at") or internal.get("generated_at") or now.isoformat(), 40)
    run_id = clean_text(internal.get("run_id") or internal.get("radar_run_id"), 64) or "local"
    topics = []
    candidates = []
    for row in (internal.get("topics") or [])[:5]:
        if not isinstance(row, dict):
            continue
        title = clean_text(row.get("title") or row.get("topic"), 120)
        if not title:
            continue
        velocity = _normalized_score(row, "observed_velocity_score", "velocity_score", "observed_velocity_60m")
        acceleration = _normalized_score(row, "observed_acceleration_score", "acceleration_score")
        interpretation = row.get("interpretation") if isinstance(row.get("interpretation"), dict) else {}
        posts = []
        for item in (row.get("representative_posts") or row.get("posts") or [])[:2]:
            if isinstance(item, dict) and (post := RepresentativePost.from_internal(item)):
                posts.append(post)
        canonical = clean_text(row.get("canonical_topic") or title.lower(), 160)
        topic_id = clean_text(row.get("topic_id") or row.get("event_id"), 64) or hashlib.sha256(canonical.encode()).hexdigest()[:20]
        reliability = clean_text(row.get("reliability") or row.get("source_reliability") or interpretation.get("confidence"), 16).lower()
        official = bool(row.get("official_source_present", row.get("primary_source_available", False)))
        market_impact = _normalized_score(row, "market_impact_score", "social_research_value")
        score = observed_radar_score(velocity, acceleration, market_impact, reliability, official)
        candidates.append(PublicRadarTopic(
            topic_id=topic_id, rank=0, title=title, canonical_topic=canonical,
            tickers=[clean_text(x, 16) for x in (row.get("tickers") or [])[:10] if clean_text(x, 16)],
            entities=[clean_text(x, 80) for x in (row.get("entities") or [])[:10] if clean_text(x, 80)],
            category=clean_text(row.get("category") or "OTHER", 40).upper(),
            summary=clean_text(row.get("summary") or interpretation.get("topic_summary") or "今回の検索結果で観測された議論です。", 280),
            observed_velocity_score=velocity, observed_acceleration_score=acceleration,
            observed_radar_score=score,
            trend_direction=trend_direction(velocity, acceleration, row.get("trend_direction")),
            attention_level=attention_level(velocity, acceleration),
            reliability=reliability if reliability in ALLOWED_RELIABILITY else "unknown",
            official_source_present=official,
            source_confirmation_status=clean_text(row.get("source_confirmation_status") or row.get("news_confirmation_status") or "unverified", 80),
            dominant_narrative=clean_text(row.get("dominant_narrative") or row.get("consensus_view") or interpretation.get("dominant_narrative"), 280),
            dissent=clean_text(row.get("dissent") or row.get("dissenting_view") or interpretation.get("strongest_dissent"), 280),
            misconception=clean_text(row.get("misconception") or row.get("possible_misconception") or interpretation.get("common_misconception"), 280),
            representative_posts=posts,
            translations={
                "en": {"title": title, "summary": clean_text(row.get("summary") or interpretation.get("topic_summary"), 280),
                    "dominant_narrative": clean_text(row.get("dominant_narrative") or interpretation.get("dominant_narrative"), 280),
                    "dissent": clean_text(row.get("dissent") or interpretation.get("strongest_dissent"), 280),
                    "misconception": clean_text(row.get("misconception") or interpretation.get("common_misconception"), 280)},
                "ja": {"title": clean_text(row.get("title_ja") or "金融市場の関連テーマ", 120),
                    "summary": clean_text(row.get("summary_ja") or interpretation.get("topic_summary_ja") or "今回のX Searchで観測された金融テーマです。", 280),
                    "dominant_narrative": clean_text(row.get("dominant_narrative_ja") or interpretation.get("dominant_narrative_ja"), 280),
                    "dissent": clean_text(row.get("dissent_ja") or interpretation.get("strongest_dissent_ja"), 280),
                    "misconception": clean_text(row.get("misconception_ja") or interpretation.get("common_misconception_ja"), 280)},
            }, updated_at=clean_text(row.get("updated_at") or row.get("detected_at") or observed, 40),
        ))
    for rank, topic in enumerate(sorted(candidates, key=lambda item: item.observed_radar_score, reverse=True), 1):
        topics.append(PublicRadarTopic(**{**asdict(topic), "rank": rank,
            "representative_posts": topic.representative_posts}))
    generated = now.isoformat()
    snapshot_seed = f"{run_id}:{observed}:{','.join(t.topic_id for t in topics)}"
    snapshot_id = clean_text(internal.get("snapshot_id"), 64) or hashlib.sha256(snapshot_seed.encode()).hexdigest()[:32]
    return PublicRadarSnapshot(1, snapshot_id, run_id, observed, generated, "x_search_observed", DISCLAIMER, topics)
