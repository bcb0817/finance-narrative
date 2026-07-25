from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .models import FxBar, FxQuote


def quotes_to_minute_bars(quotes: list[FxQuote]) -> list[FxBar]:
    groups: dict[datetime, list[FxQuote]] = defaultdict(list)
    for quote in sorted(quotes, key=lambda item: item.timestamp):
        minute = quote.timestamp.replace(second=0, microsecond=0)
        groups[minute].append(quote)
    bars: list[FxBar] = []
    for timestamp, items in sorted(groups.items()):
        prices = [item.price for item in items]
        bars.append(
            FxBar(
                pair=items[0].pair,
                timestamp=timestamp,
                interval="1min",
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                provider=items[-1].provider,
            )
        )
    return bars


def aggregate_bars(bars: list[FxBar], *, minutes: int) -> list[FxBar]:
    if minutes < 1:
        raise ValueError("minutes must be positive")
    groups: dict[datetime, list[FxBar]] = defaultdict(list)
    for bar in sorted(bars, key=lambda item: item.timestamp):
        floored = bar.timestamp.replace(
            minute=(bar.timestamp.minute // minutes) * minutes,
            second=0,
            microsecond=0,
        )
        groups[floored].append(bar)
    result: list[FxBar] = []
    for timestamp, items in sorted(groups.items()):
        result.append(
            FxBar(
                pair=items[0].pair,
                timestamp=timestamp,
                interval=f"{minutes}min",
                open=items[0].open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=items[-1].close,
                provider=items[-1].provider,
            )
        )
    return result


def resample_recent(bars: list[FxBar], *, hours: int) -> list[FxBar]:
    if not bars:
        return []
    cutoff = max(item.timestamp for item in bars) - timedelta(hours=hours)
    return [item for item in bars if item.timestamp >= cutoff]
