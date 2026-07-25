from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper().replace("-", "/")
    if not value or len(value) > 20:
        raise ValueError(f"invalid market symbol: {symbol}")
    if "/" in value:
        left, right, *extra = value.split("/")
        if extra or not left.isalnum() or not right.isalnum():
            raise ValueError(f"invalid market symbol: {symbol}")
        return f"{left}/{right}"
    if not value.replace(".", "").isalnum():
        raise ValueError(f"invalid market symbol: {symbol}")
    return value


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    provider_symbol: str
    asset_type: str
    last: float
    source_timestamp: datetime
    received_at: datetime
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    source: str = "twelvedata"
    delay_seconds: float = 0.0
    session: str = "unknown"
    data_quality: str = "good"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "provider_symbol", normalize_symbol(self.provider_symbol))

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["source_timestamp"] = self.source_timestamp.isoformat()
        row["received_at"] = self.received_at.isoformat()
        row["mid"] = self.mid
        return row


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    timestamp: datetime
    volume: float | None = None
    source: str = "twelvedata"
    session: str = "unknown"
    complete: bool = True
    data_quality: str = "good"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["timestamp"] = self.timestamp.isoformat()
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "MarketBar":
        values = dict(row)
        values["timestamp"] = datetime.fromisoformat(str(values["timestamp"]))
        return cls(**values)


@dataclass
class MarketMovement:
    movement_id: str
    symbol: str
    asset_type: str
    direction: str
    start_price: float
    current_price: float
    absolute_change: float
    percentage_change: float
    window_minutes: int
    high: float
    low: float
    detected_at: datetime
    volume: float | None = None
    relative_volume: float | None = None
    threshold_type: str = "fixed_and_volatility"
    volatility_score: float = 0.0
    z_score: float = 0.0
    atr_multiple: float = 0.0
    data_source: str = "twelvedata"
    data_quality: str = "good"
    alert_level: str = "high"
    session: str = "unknown"
    alert_type: str = "market_breaking"

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["detected_at"] = self.detected_at.isoformat()
        return row


@dataclass
class CrossAssetSignal:
    signal_id: str
    detected_at: datetime
    primary_symbol: str
    related_symbols: list[str]
    pattern_type: str
    movements: dict[str, float]
    likely_interpretation: str
    alternative_interpretations: list[str] = field(default_factory=list)
    confidence: str = "unknown"
    source_confirmation_status: str = "unknown"
    radar_influenced: bool = False
    recommended_post_type: str = "cross_asset_explanation"
    observed_facts: list[str] = field(default_factory=list)
    inferred_interpretations: list[str] = field(default_factory=list)
    disconfirming_evidence: list[str] = field(default_factory=list)
    confirmation_sources: list[str] = field(default_factory=list)
    causality_claim_allowed: bool = False
    publication_language: str = "現時点で明確な材料は確認できていません"

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["detected_at"] = self.detected_at.isoformat()
        return row
