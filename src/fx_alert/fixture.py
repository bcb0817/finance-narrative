from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import FxBar


def movement_fixture(
    pair: str = "USDJPY",
    *,
    now: datetime | None = None,
    direction: str = "up",
    points: int = 180,
) -> list[FxBar]:
    end = now or datetime.now(timezone.utc).replace(second=0, microsecond=0)
    sign = 1.0 if direction == "up" else -1.0
    bars: list[FxBar] = []
    for index in range(points):
        progress = index / max(points - 1, 1)
        drift = sign * (0.08 * progress)
        shock = sign * max(0.0, progress - 0.90) * 11.5
        close = 155.0 + drift + shock
        open_price = close - sign * 0.015
        bars.append(
            FxBar(
                pair=pair,
                timestamp=end - timedelta(minutes=points - 1 - index),
                interval="1min",
                open=open_price,
                high=max(open_price, close) + 0.025,
                low=min(open_price, close) - 0.025,
                close=close,
                provider="fixture",
            )
        )
    return bars
