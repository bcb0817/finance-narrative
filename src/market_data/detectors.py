from __future__ import annotations

import hashlib
import os
from datetime import timedelta

from .models import MarketBar, MarketMovement
from .technical import atr_multiple, percentage_change, relative_volume, z_score


MEGACAP_THRESHOLDS = {5: 1.5, 15: 2.5, 60: 4.0, 1440: 6.0}
ETF_THRESHOLDS = {5: 0.8, 15: 1.2, 60: 2.0, 1440: 3.5}


def _threshold(asset_type: str, minutes: int) -> float:
    source = MEGACAP_THRESHOLDS if asset_type == "equity" else ETF_THRESHOLDS
    default = source[minutes]
    prefix = "MEGACAP" if asset_type == "equity" else "ETF"
    suffix = "1D" if minutes == 1440 else ("1H" if minutes == 60 else f"{minutes}M")
    return float(os.getenv(f"{prefix}_MOVE_{suffix}_PERCENT", str(default)) or default)


def detect_movement(bars: list[MarketBar], *, asset_type: str, session: str = "regular") -> MarketMovement | None:
    if len(bars) < 20:
        return None
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    latest = ordered[-1]
    candidates = []
    for minutes in (5, 15, 60, 1440):
        cutoff = latest.timestamp - timedelta(minutes=minutes)
        prior = [bar for bar in ordered if bar.timestamp <= cutoff]
        if not prior:
            continue
        start = prior[-1]
        pct = percentage_change(start.close, latest.close)
        threshold = _threshold(asset_type, minutes)
        if abs(pct) < threshold:
            continue
        z = z_score(ordered[-60:], pct)
        atrm = atr_multiple(ordered[-20:], latest.close - start.close)
        rv = relative_volume(ordered[-21:])
        breakout = latest.close >= max(bar.high for bar in ordered[:-1]) or latest.close <= min(bar.low for bar in ordered[:-1])
        z_min = float(os.getenv("MEGACAP_Z_SCORE_MIN", "2.5") or 2.5)
        atr_min = float(os.getenv("MEGACAP_ATR_MULTIPLE_MIN", "1.5") or 1.5)
        rv_min = float(os.getenv("MEGACAP_RELATIVE_VOLUME_MIN", "2.5") or 2.5)
        secondary = z >= z_min or atrm >= atr_min or breakout or (rv or 0) >= rv_min
        if not secondary:
            continue
        raw = f"{latest.symbol}:{minutes}:{latest.timestamp.isoformat()}:{latest.close:.8f}"
        candidates.append(MarketMovement(
            movement_id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            symbol=latest.symbol, asset_type=asset_type,
            direction="up" if pct > 0 else "down",
            start_price=start.close, current_price=latest.close,
            absolute_change=latest.close - start.close, percentage_change=pct,
            window_minutes=minutes,
            high=max(bar.high for bar in ordered if bar.timestamp >= cutoff),
            low=min(bar.low for bar in ordered if bar.timestamp >= cutoff),
            detected_at=latest.timestamp, volume=latest.volume,
            relative_volume=rv, volatility_score=max(z, atrm),
            z_score=z, atr_multiple=atrm, session=session,
            alert_type="market_breaking" if asset_type == "equity" else "sector_divergence",
        ))
    return max(candidates, key=lambda movement: abs(movement.percentage_change), default=None)


def detect_volume_anomaly(bars: list[MarketBar], *, asset_type: str) -> MarketMovement | None:
    if len(bars) < 21:
        return None
    rv = relative_volume(bars)
    pct = percentage_change(bars[-2].close, bars[-1].close)
    minimum = float(os.getenv("VOLUME_ALERT_RELATIVE_VOLUME_MIN", "3.0") or 3.0)
    if rv is None or rv < minimum or abs(pct) < 1.0:
        return None
    raw = f"volume:{bars[-1].symbol}:{bars[-1].timestamp.isoformat()}"
    return MarketMovement(
        movement_id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        symbol=bars[-1].symbol, asset_type=asset_type,
        direction="up" if pct >= 0 else "down",
        start_price=bars[-2].close, current_price=bars[-1].close,
        absolute_change=bars[-1].close - bars[-2].close,
        percentage_change=pct, window_minutes=1,
        high=bars[-1].high, low=bars[-1].low,
        detected_at=bars[-1].timestamp, volume=bars[-1].volume,
        relative_volume=rv, alert_level="medium", alert_type="volume_alert",
    )
