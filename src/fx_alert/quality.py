from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import FxBar, FxQuote


@dataclass(frozen=True)
class QualityResult:
    quality: str
    reasons: list[str] = field(default_factory=list)

    @property
    def good(self) -> bool:
        return self.quality == "good"


def validate_quotes(
    quotes: list[FxQuote],
    *,
    now: datetime | None = None,
    stale_seconds: int = 90,
    minimum_points: int = 12,
    outlier_pct: float = 5.0,
) -> QualityResult:
    reasons: list[str] = []
    if len(quotes) < minimum_points:
        reasons.append("insufficient_points")
    if not quotes:
        return QualityResult("bad", reasons or ["empty"])
    ordered = sorted(quotes, key=lambda item: item.timestamp)
    current = now or datetime.now(timezone.utc)
    latest = ordered[-1].timestamp
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if (current - latest).total_seconds() > stale_seconds:
        reasons.append("stale")
    timestamps = [item.timestamp for item in ordered]
    if len(timestamps) != len(set(timestamps)):
        reasons.append("duplicate_timestamp")
    for quote in ordered:
        if quote.price <= 0:
            reasons.append("invalid_price")
            break
        if quote.bid is not None and quote.ask is not None:
            if quote.bid > quote.ask:
                reasons.append("inverted_spread")
                break
            if quote.price and (quote.ask - quote.bid) / quote.price * 100 > 1.0:
                reasons.append("wide_spread")
                break
    for previous, current_quote in zip(ordered, ordered[1:]):
        if previous.price and abs(current_quote.price / previous.price - 1) * 100 > outlier_pct:
            reasons.append("outlier")
            break
    return QualityResult("good" if not reasons else "bad", sorted(set(reasons)))


def validate_bars(
    bars: list[FxBar],
    *,
    minimum_points: int | None = None,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> QualityResult:
    reasons: list[str] = []
    minimum_points = minimum_points if minimum_points is not None else 12
    if len(bars) < minimum_points:
        reasons.append("insufficient_points")
    timestamps = [item.timestamp for item in bars]
    if len(timestamps) != len(set(timestamps)):
        reasons.append("duplicate_timestamp")
    for bar in bars:
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            reasons.append("invalid_price")
            break
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
            reasons.append("invalid_ohlc")
            break
    if bars:
        current = now or datetime.now(timezone.utc)
        latest = max(item.timestamp for item in bars)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        maximum_age = stale_seconds if stale_seconds is not None else 90
        if (current - latest).total_seconds() > maximum_age:
            reasons.append("stale")
    return QualityResult("good" if not reasons else "bad", sorted(set(reasons)))
