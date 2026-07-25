from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .provider import TwelveDataMarketProvider
from .storage import atomic_json, market_data_dir


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}


def check_capabilities(*, refresh: bool = False) -> dict[str, Any]:
    path = market_data_dir() / "twelve_data_capabilities.json"
    if path.exists() and not refresh:
        import json
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, json.JSONDecodeError):
            pass
    provider = TwelveDataMarketProvider()
    usage = provider.api_usage(cache_seconds=0)
    minute_limit = int(usage.get("plan_limit", 0) or 0)
    daily_limit = usage.get("daily_limit")
    if daily_limit is None and minute_limit == 8:
        daily_limit = 800
    plan_name = (
        str(usage.get("plan") or "").strip()
        or ("Basic (inferred from 8 API credits/minute)" if minute_limit == 8 else "unknown")
    )
    external = _bool("TWELVEDATA_EXTERNAL_DISPLAY_APPROVED", False)
    capabilities = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "plan_name": plan_name,
        "websocket_available": "trial_only",
        "websocket_production_enabled": False,
        "real_time_equities": True if minute_limit == 8 else None,
        "delayed_equities": True,
        "forex_available": True,
        "crypto_available": True,
        "etf_available": True,
        "indices_available": None,
        "commodities_available": False if minute_limit == 8 else None,
        "fundamentals_available": False if minute_limit == 8 else None,
        "earnings_available": False if minute_limit == 8 else None,
        "press_releases_available": True if minute_limit == 8 else None,
        "technical_indicators_available": True,
        "extended_hours_available": False if minute_limit == 8 else None,
        "external_display_confirmed": external,
        "api_limits": {
            "credits_per_minute": minute_limit,
            "daily_limit": daily_limit,
            "current_usage": usage.get("current_usage"),
            "daily_usage": usage.get("daily_usage"),
        },
        "available_endpoints": ["api_usage", "quote", "price", "time_series", "technical_indicators"],
        "unavailable_features": [
            "production_websocket",
            "extended_hours",
            "fundamentals",
            "earnings_fundamentals",
            "spot_commodities",
            "external_display_without_separate_approval",
        ],
        "notes": [
            "Representative quote reads confirmed SPY, QQQ, SMH, TLT, GLD, BTC/USD and NVDA.",
            "Individual plans are treated as internal/non-display only.",
            "Press releases are documented for Basic+, but automatic production use remains disabled until a scoped probe is approved by credit policy.",
        ],
    }
    atomic_json(path, capabilities)
    return capabilities
