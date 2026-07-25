from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests

from .models import FxBar, FxQuote, normalize_pair
from .provider_base import FxDataProvider, ProviderStatus
from .storage import append_jsonl, provider_usage_summary, read_jsonl


def provider_symbol(pair: str) -> str:
    value = normalize_pair(pair)
    return f"{value[:3]}/{value[3:]}"


class TwelveDataProvider(FxDataProvider):
    name = "twelvedata"
    base_url = "https://api.twelvedata.com"

    def __init__(self, *, session: Any = requests, api_key: str | None = None) -> None:
        self.session = session
        self.api_key = api_key if api_key is not None else os.getenv("TWELVE_DATA_API_KEY", "").strip()

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("TWELVE_DATA_API_KEY is not configured")
        usage = provider_usage_summary()
        maximum = int(os.getenv("FX_DATA_MAX_REST_CALLS_PER_DAY", "800") or 800)
        if usage["daily_calls"] >= maximum:
            raise RuntimeError("FX provider daily call limit reached")
        budget = usage.get("monthly_budget_usd")
        reported = usage.get("reported_cost_usd")
        if budget is not None and reported is not None and reported >= budget:
            raise RuntimeError("FX provider monthly budget reached")
        safe_params = dict(params)
        safe_params["apikey"] = self.api_key
        started = datetime.now(timezone.utc)
        try:
            response = self.session.get(f"{self.base_url}/{endpoint}", params=safe_params, timeout=15)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("status") == "error":
                raise RuntimeError(str(data.get("message", "provider response error")))
            append_jsonl(
                "provider_usage.jsonl",
                {
                    "provider": self.name,
                    "endpoint": endpoint,
                    "timestamp": started.isoformat(),
                    "status": "success",
                },
            )
            return data
        except Exception as exc:
            append_jsonl(
                "provider_usage.jsonl",
                {
                    "provider": self.name,
                    "endpoint": endpoint,
                    "timestamp": started.isoformat(),
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
            )
            raise

    def status(self, *, probe: bool = False) -> ProviderStatus:
        usage = provider_usage_summary()
        quote_rows = read_jsonl("quotes.jsonl", limit=1)
        latest = quote_rows[-1] if quote_rows else {}
        last_quote_time = str(latest.get("timestamp")) if latest else None
        age = None
        if last_quote_time:
            try:
                when = datetime.fromisoformat(last_quote_time)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - when).total_seconds())
            except ValueError:
                pass
        common = {
            "websocket_available": False,
            "rest_available": bool(self.api_key),
            "connection_status": "rest_ready" if self.api_key else "disconnected",
            "last_quote_time": last_quote_time,
            "data_age_seconds": round(age, 1) if age is not None else None,
            "current_pair": latest.get("pair"),
            "current_price": latest.get("price"),
            "daily_api_calls": usage["daily_calls"],
            "estimated_cost_usd": usage["estimated_cost_usd"],
            "reported_cost_usd": usage["reported_cost_usd"],
            "monthly_budget_usd": usage["monthly_budget_usd"],
            "errors": usage["errors"],
        }
        if not self.api_key:
            return ProviderStatus(
                self.name, False, False, "rest", "API key is not configured", ["quote", "bars"],
                **common,
            )
        if not probe:
            return ProviderStatus(
                self.name, True, True, "rest", "configured (probe skipped)", ["quote", "bars"],
                **common,
            )
        try:
            self.get_quote("USDJPY")
            return ProviderStatus(
                self.name, True, True, "rest", "probe succeeded", ["quote", "bars"],
                **{**common, "connection_status": "probe_succeeded"},
            )
        except Exception as exc:
            return ProviderStatus(
                self.name, True, False, "rest", f"probe failed: {type(exc).__name__}", ["quote", "bars"],
                **{**common, "connection_status": "probe_failed"},
            )

    def get_quote(self, pair: str) -> FxQuote:
        data = self._request("quote", {"symbol": provider_symbol(pair)})
        timestamp = data.get("timestamp")
        when = (
            datetime.fromtimestamp(float(timestamp), timezone.utc)
            if timestamp
            else datetime.now(timezone.utc)
        )
        close = data.get("close") or data.get("price")
        return FxQuote(
            pair=pair,
            timestamp=when,
            price=float(close),
            bid=float(data["bid"]) if data.get("bid") is not None else None,
            ask=float(data["ask"]) if data.get("ask") is not None else None,
            provider=self.name,
            received_at=datetime.now(timezone.utc),
            latency_ms=max(0.0, (datetime.now(timezone.utc) - when).total_seconds() * 1000),
        )

    def get_bars(self, pair: str, *, interval: str = "1min", outputsize: int = 300) -> list[FxBar]:
        data = self._request(
            "time_series",
            {
                "symbol": provider_symbol(pair),
                "interval": interval,
                "outputsize": max(12, min(int(outputsize), 5000)),
                "timezone": "UTC",
                "order": "ASC",
            },
        )
        values = data.get("values")
        if not isinstance(values, list):
            raise RuntimeError("provider returned no bars")
        bars: list[FxBar] = []
        for row in values:
            timestamp = datetime.fromisoformat(str(row["datetime"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            bars.append(
                FxBar(
                    pair=pair,
                    timestamp=timestamp,
                    interval=interval,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    provider=self.name,
                )
            )
        return sorted(bars, key=lambda item: item.timestamp)


class PolygonProvider(FxDataProvider):
    """Interface placeholder. It cannot be selected until the adapter is completed."""

    name = "polygon"

    def status(self, *, probe: bool = False) -> ProviderStatus:
        configured = bool(os.getenv("POLYGON_API_KEY", "").strip())
        return ProviderStatus(
            self.name,
            configured,
            False,
            "rest",
            "adapter interface only; production access is disabled",
            [],
            websocket_available=False,
            rest_available=False,
        )

    def get_quote(self, pair: str) -> FxQuote:
        raise RuntimeError("Polygon FX adapter is not implemented")

    def get_bars(self, pair: str, *, interval: str, outputsize: int) -> list[FxBar]:
        raise RuntimeError("Polygon FX adapter is not implemented")


def get_provider(name: str | None = None) -> FxDataProvider:
    selected = (name or os.getenv("FX_DATA_PROVIDER", "twelvedata")).strip().lower()
    if selected == "twelvedata":
        return TwelveDataProvider()
    if selected == "polygon":
        return PolygonProvider()
    raise ValueError(f"unsupported FX provider: {selected}")
