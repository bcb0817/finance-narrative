from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import MarketBar


def bars_fixture(
    symbol: str = "NVDA", *, asset_type: str = "equity",
    direction: str = "down", points: int = 180,
    volume_spike: bool = True, delayed: bool = False,
) -> list[MarketBar]:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if delayed:
        end -= timedelta(hours=2)
    sign = 1 if direction == "up" else -1
    start = 120.0 if asset_type != "crypto" else 60000.0
    bars = []
    for index in range(points):
        progress = index / max(points - 1, 1)
        shock = sign * max(0.0, progress - 0.90) * start * 0.60
        close = start * (1 + sign * progress * 0.002) + shock
        open_price = close - sign * start * 0.0005
        volume = 100_000.0 * (4.0 if volume_spike and index == points - 1 else 1.0)
        bars.append(MarketBar(
            symbol=symbol, interval="1min", open=open_price,
            high=max(open_price, close) + start * 0.0008,
            low=min(open_price, close) - start * 0.0008,
            close=close, volume=volume,
            timestamp=end - timedelta(minutes=points - 1 - index),
            source="fixture", session="regular",
            data_quality="delayed" if delayed else "good",
        ))
    return bars


def cross_asset_fixture(pattern: str = "risk_off") -> dict[str, float]:
    fixtures = {
        "risk_off": {"SPY": -1.4, "QQQ": -1.8, "SMH": -2.0, "TLT": 0.2, "GLD": 0.8, "BTC/USD": -2.2},
        "risk_on": {"SPY": 1.2, "QQQ": 1.6, "SMH": 2.0, "TLT": -0.2, "GLD": -0.3, "BTC/USD": 2.4},
        "yield_shock": {"SPY": -0.7, "QQQ": -1.4, "SMH": -1.8, "TLT": -1.2, "GLD": -0.2},
        "dollar_strength": {"USD/JPY": 0.9, "GLD": -0.8, "SPY": -0.2, "QQQ": -0.4},
        "semiconductor_specific": {"SPY": -0.2, "QQQ": -0.5, "SMH": -2.0, "TLT": 0.1},
        "unknown": {"SPY": 0.1, "QQQ": -0.1, "SMH": 0.0, "TLT": 0.1, "GLD": -0.1},
    }
    return fixtures[pattern]


def earnings_fixture(symbol: str = "GOOGL") -> dict[str, object]:
    return {
        "event_id": "fixture-earnings-001", "symbol": symbol,
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "revenue_current": 100.0, "revenue_prior_year": 90.0,
        "eps_current": 2.10, "eps_prior_year": 1.80,
        "guidance": "company guidance was updated",
        "price_before": 180.0, "price_after": 171.0,
        "market_consensus_available": False,
        "source": "fixture official filing",
    }


def analyze_earnings_reaction(event: dict[str, object]) -> dict[str, object]:
    before, after = float(event["price_before"]), float(event["price_after"])
    return {
        **event,
        "price_change_percent": (after / before - 1) * 100,
        "revenue_yoy_percent": (
            float(event["revenue_current"]) / float(event["revenue_prior_year"]) - 1
        ) * 100,
        "uses_beat_miss_language": False,
        "summary": "前年同期との変化と発表前後の価格反応のみを整理しました。市場予想との比較は行っていません。",
    }
