from __future__ import annotations

import math
import statistics

from .models import MarketBar


def percentage_change(start: float, end: float) -> float:
    return (end / start - 1) * 100 if start else 0.0


def returns(bars: list[MarketBar]) -> list[float]:
    return [percentage_change(a.close, b.close) for a, b in zip(bars, bars[1:]) if a.close]


def z_score(bars: list[MarketBar], value: float | None = None) -> float:
    series = returns(bars)
    if len(series) < 2:
        return 0.0
    sample = series[-1] if value is None else value
    deviation = statistics.pstdev(series)
    return abs(sample - statistics.mean(series)) / deviation if deviation else 0.0


def atr(bars: list[MarketBar], period: int = 14) -> float:
    if not bars:
        return 0.0
    true_ranges = []
    for index, bar in enumerate(bars[-period:]):
        previous = bars[max(0, len(bars) - period + index - 1)].close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    return statistics.mean(true_ranges) if true_ranges else 0.0


def atr_multiple(bars: list[MarketBar], absolute_change: float) -> float:
    value = atr(bars)
    return abs(absolute_change) / value if value else 0.0


def relative_volume(bars: list[MarketBar], period: int = 20) -> float | None:
    volumes = [float(bar.volume) for bar in bars if bar.volume is not None]
    if len(volumes) < 2:
        return None
    current = volumes[-1]
    baseline = volumes[-period - 1:-1]
    average = statistics.mean(baseline) if baseline else 0.0
    return current / average if average else None


def vwap(bars: list[MarketBar]) -> float | None:
    weighted = total = 0.0
    for bar in bars:
        if bar.volume is None:
            continue
        typical = (bar.high + bar.low + bar.close) / 3
        weighted += typical * bar.volume
        total += bar.volume
    return weighted / total if total else None


def rsi(bars: list[MarketBar], period: int = 14) -> float | None:
    changes = [b.close - a.close for a, b in zip(bars, bars[1:])][-period:]
    if len(changes) < period:
        return None
    gains = statistics.mean([max(change, 0) for change in changes])
    losses = statistics.mean([max(-change, 0) for change in changes])
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def bollinger_bands(bars: list[MarketBar], period: int = 20, width: float = 2.0) -> tuple[float, float, float] | None:
    closes = [bar.close for bar in bars][-period:]
    if len(closes) < period:
        return None
    mean = statistics.mean(closes)
    deviation = statistics.pstdev(closes)
    return mean - width * deviation, mean, mean + width * deviation


def moving_average(bars: list[MarketBar], period: int) -> float | None:
    closes = [bar.close for bar in bars][-period:]
    return statistics.mean(closes) if len(closes) == period else None


def rolling_correlation(left: list[float], right: list[float]) -> float | None:
    size = min(len(left), len(right))
    if size < 3:
        return None
    x, y = left[-size:], right[-size:]
    mx, my = statistics.mean(x), statistics.mean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / denominator if denominator else None
