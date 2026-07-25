from __future__ import annotations

from datetime import datetime, timezone

from .models import MarketBar


def aggregate_bars(bars: list[MarketBar], *, minutes: int) -> list[MarketBar]:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    buckets: dict[datetime, list[MarketBar]] = {}
    for bar in sorted(bars, key=lambda item: item.timestamp):
        when = bar.timestamp
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        bucket = when.replace(
            minute=(when.minute // minutes) * minutes,
            second=0, microsecond=0,
        )
        buckets.setdefault(bucket, []).append(bar)
    result = []
    for timestamp, rows in sorted(buckets.items()):
        volumes = [row.volume for row in rows if row.volume is not None]
        result.append(MarketBar(
            symbol=rows[0].symbol, interval=f"{minutes}min",
            open=rows[0].open, high=max(row.high for row in rows),
            low=min(row.low for row in rows), close=rows[-1].close,
            volume=sum(volumes) if volumes else None, timestamp=timestamp,
            source=rows[0].source, session=rows[0].session,
            complete=len(rows) == minutes,
            data_quality=(
                "good" if all(row.data_quality == "good" for row in rows)
                else "incomplete"
            ),
        ))
    return result
