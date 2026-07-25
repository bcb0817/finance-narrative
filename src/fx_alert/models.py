from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def normalize_pair(pair: str) -> str:
    value = pair.upper().replace("/", "").replace("-", "").replace("_", "")
    if len(value) != 6 or not value.isalpha():
        raise ValueError(f"invalid FX pair: {pair}")
    return value


@dataclass(frozen=True)
class FxQuote:
    pair: str
    timestamp: datetime
    price: float
    bid: float | None = None
    ask: float | None = None
    provider: str = ""
    received_at: datetime | None = None
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", normalize_pair(self.pair))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        if self.received_at is not None:
            data["received_at"] = self.received_at.isoformat()
        data["mid"] = self.mid
        data["source"] = self.provider
        data["source_timestamp"] = self.timestamp.isoformat()
        return data

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.price

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FxQuote":
        return cls(
            pair=str(data["pair"]),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            price=float(data["price"]),
            bid=float(data["bid"]) if data.get("bid") is not None else None,
            ask=float(data["ask"]) if data.get("ask") is not None else None,
            provider=str(data.get("provider", "")),
            received_at=(
                datetime.fromisoformat(str(data["received_at"]))
                if data.get("received_at") else None
            ),
            latency_ms=float(data["latency_ms"]) if data.get("latency_ms") is not None else None,
        )


@dataclass(frozen=True)
class FxBar:
    pair: str
    timestamp: datetime
    interval: str
    open: float
    high: float
    low: float
    close: float
    provider: str = ""
    complete: bool = True
    data_quality: str = "good"

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", normalize_pair(self.pair))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FxBar":
        return cls(
            pair=str(data["pair"]),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            interval=str(data["interval"]),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            provider=str(data.get("provider", "")),
        )


@dataclass
class FxMovement:
    movement_id: str
    pair: str
    detected_at: datetime
    window: str
    start_price: float
    end_price: float
    change_yen: float
    change_pct: float
    direction: str
    z_score: float = 0.0
    atr_multiple: float = 0.0
    triggers: list[str] = field(default_factory=list)
    quality: str = "good"
    cause_confidence: str = "unknown"
    cause_summary: str = "現時点で明確な材料は確認できていません"
    context_sources: list[str] = field(default_factory=list)
    chart_path: str = ""
    high: float | None = None
    low: float | None = None
    data_source: str = ""
    confirmed: bool = False
    alert_level: str = "high"
    fixed_threshold_passed: bool = True
    dynamic_confirmation_passed: bool = True
    volatility_regime: str = "normal"
    hard_triggered: bool = False
    event_window: bool = False
    threshold_version: str = "v2_dynamic_2026_07"
    rejection_reason: str = ""

    def __post_init__(self) -> None:
        self.pair = normalize_pair(self.pair)

    @property
    def direction_ja(self) -> str:
        return "円安" if self.direction == "up" else "円高"

    @property
    def movement_direction(self) -> str:
        return "yen_weakening" if self.direction == "up" else "yen_strengthening"

    @property
    def current_price(self) -> float:
        return self.end_price

    @property
    def absolute_change(self) -> float:
        return self.change_yen

    @property
    def percentage_change(self) -> float:
        return self.change_pct

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["detected_at"] = self.detected_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FxMovement":
        values = dict(data)
        values["detected_at"] = datetime.fromisoformat(str(values["detected_at"]))
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in values.items() if key in allowed})
