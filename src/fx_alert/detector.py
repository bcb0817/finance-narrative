from __future__ import annotations

import hashlib
import math
import os
import statistics
from dataclasses import dataclass
from datetime import timedelta

from .models import FxBar, FxMovement


@dataclass(frozen=True)
class Threshold:
    minutes: int
    pct: float
    yen: float


THRESHOLDS: dict[str, Threshold] = {
    "5m": Threshold(5, 0.30, 0.50),
    "15m": Threshold(15, 0.50, 0.80),
    "1h": Threshold(60, 0.80, 1.20),
    "4h": Threshold(240, 1.20, 1.80),
    "24h": Threshold(1440, 1.50, 2.00),
}


def configured_thresholds() -> dict[str, Threshold]:
    mapping = {
        "5m": ("5M", 5), "15m": ("15M", 15), "1h": ("1H", 60),
        "4h": ("4H", 240), "24h": ("24H", 1440),
    }
    result = {}
    for key, (suffix, minutes) in mapping.items():
        default = THRESHOLDS[key]
        result[key] = Threshold(
            minutes,
            float(os.getenv(f"FX_MOVE_{suffix}_PERCENT", str(default.pct)) or default.pct),
            float(os.getenv(f"FX_MOVE_{suffix}_JPY", str(default.yen)) or default.yen),
        )
    return result


def _returns(bars: list[FxBar]) -> list[float]:
    return [
        (current.close / previous.close - 1) * 100
        for previous, current in zip(bars, bars[1:])
        if previous.close
    ]


def _z_score(bars: list[FxBar], change_pct: float) -> float:
    values = _returns(bars)
    if len(values) < 2:
        return 0.0
    deviation = statistics.pstdev(values)
    return abs(change_pct) / deviation if deviation > 0 else 0.0


def _atr_multiple(bars: list[FxBar], change_yen: float, period: int = 14) -> float:
    recent = bars[-period:]
    ranges = [item.high - item.low for item in recent]
    atr = statistics.mean(ranges) if ranges else 0.0
    return abs(change_yen) / atr if atr > 0 else 0.0


def _major_level_crossed(start: float, end: float) -> bool:
    low, high = sorted((start, end))
    return math.floor(low) < math.floor(high) or math.floor(low * 2) < math.floor(high * 2)


def detect_movements(
    bars: list[FxBar],
    *,
    important_event: bool = False,
    pair: str | None = None,
) -> list[FxMovement]:
    if len(bars) < 12:
        return []
    ordered = sorted(bars, key=lambda item: item.timestamp)
    latest = ordered[-1]
    pair_name = pair or latest.pair
    results: list[FxMovement] = []
    thresholds = configured_thresholds()
    z_min = float(os.getenv("FX_Z_SCORE_MIN", "2.5") or 2.5)
    atr_min = float(os.getenv("FX_ATR_MULTIPLE_MIN", "1.5") or 1.5)
    for window, threshold in thresholds.items():
        cutoff = latest.timestamp - timedelta(minutes=threshold.minutes)
        candidates = [item for item in ordered if item.timestamp <= cutoff]
        if not candidates:
            continue
        start_bar = candidates[-1]
        change_yen = latest.close - start_bar.close
        change_pct = change_yen / start_bar.close * 100
        if abs(change_pct) < threshold.pct and abs(change_yen) < threshold.yen:
            continue
        z_score = _z_score(ordered[-60:], change_pct)
        atr_multiple = _atr_multiple(ordered, change_yen)
        period = [item for item in ordered if item.timestamp >= cutoff]
        previous = [item for item in ordered if item.timestamp < cutoff]
        breakout = bool(previous) and (
            latest.close > max(item.high for item in previous)
            or latest.close < min(item.low for item in previous)
        )
        major_level = _major_level_crossed(start_bar.close, latest.close)
        secondary = (
            z_score >= z_min
            or atr_multiple >= atr_min
            or important_event
            or breakout
            or major_level
        )
        if not secondary:
            continue
        triggers = [f"{window}_threshold"]
        if z_score >= z_min:
            triggers.append("z_score")
        if atr_multiple >= atr_min:
            triggers.append("atr")
        if important_event:
            triggers.append("important_event")
        if breakout:
            triggers.append("breakout")
        if major_level:
            triggers.append("major_level")
        raw_id = f"{pair_name}:{window}:{latest.timestamp.isoformat()}:{latest.close:.5f}"
        movement_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
        results.append(
            FxMovement(
                movement_id=movement_id,
                pair=pair_name,
                detected_at=latest.timestamp,
                window=window,
                start_price=start_bar.close,
                end_price=latest.close,
                change_yen=change_yen,
                change_pct=change_pct,
                direction="up" if change_yen > 0 else "down",
                z_score=z_score,
                atr_multiple=atr_multiple,
                triggers=triggers,
                high=max(item.high for item in period),
                low=min(item.low for item in period),
                data_source=latest.provider,
                confirmed=True,
            )
        )
    return results


def strongest_movement(movements: list[FxMovement]) -> FxMovement | None:
    if not movements:
        return None
    return max(
        movements,
        key=lambda item: (
            abs(item.change_pct),
            abs(item.change_yen),
            configured_thresholds()[item.window].minutes,
        ),
    )
