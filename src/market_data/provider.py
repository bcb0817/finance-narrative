from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from common.runtime import load_env
from fx_alert.providers import TwelveDataProvider

from .models import MarketBar, MarketQuote, normalize_symbol
from .storage import append_jsonl, atomic_json, cache_get, cache_put, market_data_dir, usage_summary
from .symbols import symbol_config


class MarketDataUnavailable(RuntimeError):
    pass


class TwelveDataMarketProvider(TwelveDataProvider):
    """Generic market adapter reusing the existing Twelve Data provider client."""

    def __init__(self, *, session: Any = requests, api_key: str | None = None) -> None:
        load_env()
        super().__init__(session=session, api_key=api_key)

    @staticmethod
    def _cache_key(endpoint: str, params: dict[str, Any]) -> str:
        raw = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def request(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        operation: str,
        credits: int,
        cache_seconds: int = 0,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise MarketDataUnavailable("TWELVE_DATA_API_KEY is not configured")
        key = self._cache_key(endpoint, params)
        if cache_seconds:
            cached = cache_get(key, max_age_seconds=cache_seconds)
            if cached is not None:
                append_jsonl("provider_usage.jsonl", {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": "twelvedata", "endpoint": endpoint,
                    "symbol_count": len(str(params.get("symbol", "")).split(",")),
                    "credits_used": 0, "reported_usage": None, "estimated_usage": 0,
                    "cache_hit": True, "success": True, "latency_ms": 0,
                    "operation": operation, "error_type": None,
                })
                return cached
        usage = usage_summary()
        soft = float(os.getenv("TWELVEDATA_CREDIT_SOFT_LIMIT_PERCENT", "80") or 80) / 100
        hard = float(os.getenv("TWELVEDATA_CREDIT_HARD_LIMIT_PERCENT", "95") or 95) / 100
        if usage["daily_ratio"] >= hard:
            raise MarketDataUnavailable("Twelve Data daily hard credit limit reached")
        if usage["minute_credits"] + credits > usage["minute_limit"]:
            raise MarketDataUnavailable("Twelve Data minute credit limit reached")
        if usage["daily_ratio"] >= soft and operation not in {"capability", "high_priority"}:
            raise MarketDataUnavailable("Twelve Data soft credit limit: low priority operation skipped")
        started = time.perf_counter()
        timestamp = datetime.now(timezone.utc)
        try:
            response = self.session.get(
                f"{self.base_url}/{endpoint}",
                params={**params, "apikey": self.api_key},
                timeout=20,
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("status") == "error" or value.get("code"):
                message = value.get("message", "provider response error") if isinstance(value, dict) else "invalid response"
                raise MarketDataUnavailable(str(message))
            reported = response.headers.get("api-credits-used")
            append_jsonl("provider_usage.jsonl", {
                "timestamp": timestamp.isoformat(), "provider": "twelvedata",
                "endpoint": endpoint,
                "symbol_count": len([x for x in str(params.get("symbol", "")).split(",") if x]),
                "credits_used": int(reported) if str(reported or "").isdigit() else credits,
                "reported_usage": int(reported) if str(reported or "").isdigit() else None,
                "estimated_usage": credits, "cache_hit": False, "success": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "operation": operation, "error_type": None,
            })
            if cache_seconds:
                cache_put(key, value)
            return value
        except Exception as exc:
            try:
                append_jsonl("provider_usage.jsonl", {
                    "timestamp": timestamp.isoformat(), "provider": "twelvedata",
                    "endpoint": endpoint, "symbol_count": 0, "credits_used": 0,
                    "reported_usage": None, "estimated_usage": credits,
                    "cache_hit": False, "success": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "operation": operation, "error_type": type(exc).__name__,
                })
            except OSError:
                pass
            # requests exceptions may embed the complete URL, including apikey.
            # Never propagate their message or chained traceback to CLI/log output.
            if isinstance(exc, MarketDataUnavailable):
                raise MarketDataUnavailable(str(exc)) from None
            raise MarketDataUnavailable(
                f"Twelve Data {endpoint} request failed ({type(exc).__name__})"
            ) from None

    def api_usage(self, *, cache_seconds: int = 55) -> dict[str, Any]:
        return self.request("api_usage", {}, operation="capability", credits=1, cache_seconds=cache_seconds)

    def quote(self, symbol: str, *, cache_seconds: int = 60) -> MarketQuote:
        config = symbol_config(symbol)
        provider_symbol = config["provider_symbol"]
        value = self.request(
            "quote", {"symbol": provider_symbol},
            operation="quote", credits=1, cache_seconds=cache_seconds,
        )
        now = datetime.now(timezone.utc)
        raw_timestamp = value.get("timestamp")
        source_time = (
            datetime.fromtimestamp(float(raw_timestamp), timezone.utc)
            if raw_timestamp else now
        )
        delay = max(0.0, (now - source_time).total_seconds())
        quality = "good"
        if delay > int(os.getenv("MARKET_DATA_STALE_SECONDS", "300") or 300):
            quality = "delayed" if delay < 86400 else "rejected"
        last = value.get("close") or value.get("price")
        if last is None:
            raise MarketDataUnavailable(f"quote has no price: {provider_symbol}")
        quote = MarketQuote(
            symbol=config["symbol"], provider_symbol=provider_symbol,
            asset_type=config["asset_type"], last=float(last),
            bid=float(value["bid"]) if value.get("bid") is not None else None,
            ask=float(value["ask"]) if value.get("ask") is not None else None,
            volume=float(value["volume"]) if value.get("volume") not in (None, "") else None,
            source_timestamp=source_time, received_at=now, delay_seconds=delay,
            session=str(value.get("is_market_open") or "unknown"), data_quality=quality,
        )
        append_jsonl("quotes.jsonl", quote.to_dict())
        atomic_json(market_data_dir() / "last_market_data.json", {
            "kind": "quote", **quote.to_dict(),
        })
        return quote

    def bars(
        self, symbol: str, *, interval: str = "1min", outputsize: int = 100,
        cache_seconds: int = 300,
    ) -> list[MarketBar]:
        config = symbol_config(symbol)
        value = self.request(
            "time_series",
            {
                "symbol": config["provider_symbol"], "interval": interval,
                "outputsize": max(2, min(outputsize, 5000)),
                "timezone": "UTC", "order": "ASC",
            },
            operation="time_series", credits=1, cache_seconds=cache_seconds,
        )
        rows = value.get("values")
        if not isinstance(rows, list):
            raise MarketDataUnavailable(f"time_series has no values: {symbol}")
        result = []
        for row in rows:
            when = datetime.fromisoformat(str(row["datetime"]).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            result.append(MarketBar(
                symbol=config["symbol"], interval=interval,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]) if row.get("volume") not in (None, "") else None,
                timestamp=when, source="twelvedata",
                session="regular", data_quality="good",
            ))
        ordered = sorted(result, key=lambda item: item.timestamp)
        if ordered:
            atomic_json(market_data_dir() / "last_market_data.json", {
                "kind": "bar", **ordered[-1].to_dict(),
            })
        return ordered


def provider_status() -> dict[str, Any]:
    provider = TwelveDataMarketProvider()
    usage = usage_summary()
    from common.data_governance import classify_provider, license_status
    hard = float(os.getenv("TWELVEDATA_CREDIT_HARD_LIMIT_PERCENT", "95") or 95) / 100
    state = classify_provider(
        available=bool(provider.api_key),
        authenticated=bool(provider.api_key),
        budget_limited=float(usage.get("daily_ratio", 0) or 0) >= hard,
    )
    return {
        "provider": "twelvedata",
        "operational_state": state,
        "license": license_status(),
        "api_key_configured": bool(provider.api_key),
        "rest_available": bool(provider.api_key),
        "websocket_status": "trial_only_not_used_in_production",
        "usage": usage,
    }
