from __future__ import annotations

import os
import hashlib
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from common.runtime import load_env, log_error, log_run

from .aggregation import aggregate_bars
from .chart import create_market_chart
from .cross_asset import classify_cross_asset
from .detectors import detect_movement, detect_volume_anomaly
from .fixtures import (
    analyze_earnings_reaction,
    bars_fixture,
    cross_asset_fixture,
    earnings_fixture,
)
from .models import MarketBar
from .models import MarketMovement
from .notifications import notify_market_preview
from .posts import build_market_post, external_display_approved, market_post_enabled, publish_market
from .shadow import create_candidate
from .provider import MarketDataUnavailable, TwelveDataMarketProvider, provider_status
from .state import check_gate, remember
from .storage import append_jsonl, cleanup, load_state, save_state, usage_summary
from .storage import market_data_dir
from .symbols import enabled_symbols, symbol_config


TRUE_VALUES = {"1", "true", "yes"}
ET = ZoneInfo("America/New_York")


def enabled() -> bool:
    load_env()
    return os.getenv("MARKET_DATA_ENABLED", "true").lower() in TRUE_VALUES


def _regular_market_open(now: datetime) -> bool:
    current = now.astimezone(ET)
    return current.weekday() < 5 and dtime(9, 30) <= current.time() <= dtime(16, 0)


def symbols_for_run(now: datetime | None = None, *, maximum: int = 4) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    candidates = []
    if _regular_market_open(current):
        candidates.extend(enabled_symbols(asset_types={"equity", "etf"}))
    if current.minute < 15:
        candidates.extend(enabled_symbols(asset_types={"crypto"}))
    candidates = [row for row in candidates if row["symbol"] != "USD/JPY"]
    if not candidates:
        return []
    state = load_state()
    index = int(state.get("rotation_index", 0) or 0) % len(candidates)
    ordered = candidates[index:] + candidates[:index]
    selected = ordered[:maximum]
    state["rotation_index"] = (index + len(selected)) % len(candidates)
    state["rotation_updated_at"] = current.isoformat()
    save_state(state)
    return selected


def _quality(bars: list[MarketBar], *, now: datetime | None = None) -> tuple[str, float]:
    if len(bars) < 20:
        return "incomplete", float("inf")
    current = now or datetime.now(timezone.utc)
    latest = bars[-1].timestamp
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = max(0.0, (current - latest).total_seconds())
    if any(
        min(bar.open, bar.high, bar.low, bar.close) <= 0
        or bar.low > min(bar.open, bar.close)
        or bar.high < max(bar.open, bar.close)
        for bar in bars
    ):
        return "rejected", age
    one_minute_changes = [
        abs((current_bar.close / previous.close - 1) * 100)
        for previous, current_bar in zip(bars, bars[1:])
        if previous.close
    ]
    suspicious_limit = 50.0 if bars[-1].symbol in {"BTC/USD", "ETH/USD"} else 25.0
    if one_minute_changes and max(one_minute_changes) > suspicious_limit:
        return "suspicious", age
    symbol = bars[-1].symbol
    if symbol in {"BTC/USD", "ETH/USD"}:
        maximum = int(os.getenv("CRYPTO_DATA_MAX_AGE_SECONDS", "120") or 120)
    else:
        maximum = int(os.getenv("EQUITY_DATA_MAX_AGE_SECONDS", "180") or 180)
    if age > maximum:
        return ("delayed" if age < 86400 else "rejected"), age
    return "good", age


def evaluate_bars(
    bars: list[MarketBar], *, asset_type: str, display_name: str = "",
    dry_run: bool, fixture: bool = False, send_preview: bool = False,
) -> dict[str, Any]:
    quality, delay = _quality(bars)
    if quality != "good" and not fixture:
        append_jsonl("alerts.jsonl", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": bars[-1].symbol if bars else "", "status": "quality_blocked",
            "data_quality": quality, "delay_seconds": delay,
        })
        return {"status": "quality_blocked", "data_quality": quality, "delay_seconds": delay}
    if asset_type == "equity":
        alert_enabled = os.getenv("MEGACAP_ALERT_ENABLED", "true").lower() in TRUE_VALUES
    elif asset_type == "etf":
        alert_enabled = os.getenv("ETF_ALERT_ENABLED", "true").lower() in TRUE_VALUES
    elif asset_type == "crypto":
        alert_enabled = os.getenv("CRYPTO_SIGNAL_ENABLED", "true").lower() in TRUE_VALUES
    else:
        alert_enabled = False
    movement = detect_movement(bars, asset_type=asset_type) if alert_enabled else None
    volume_movement = (
        detect_volume_anomaly(bars, asset_type=asset_type)
        if os.getenv("VOLUME_ALERT_ENABLED", "true").lower() in TRUE_VALUES else None
    )
    movement = movement or volume_movement
    if movement is None:
        return {"status": "no_material_movement", "data_quality": quality}
    if fixture:
        movement.data_source = "fixture"
    movement.data_quality = "delayed" if quality == "delayed" else "good"
    gate = check_gate(movement)
    if not gate.allowed and not fixture:
        append_jsonl("alerts.jsonl", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "movement_id": movement.movement_id, "symbol": movement.symbol,
            "status": "gate_blocked", "reason": gate.reason,
        })
        return {"status": "gate_blocked", "reason": gate.reason, "movement": movement.to_dict()}
    chart, metadata = create_market_chart(
        bars, movement, display_name=display_name,
        delayed=movement.data_quality == "delayed",
    )
    text = build_market_post(movement)
    append_jsonl(
        "fixture_results.jsonl" if fixture else "movements.jsonl",
        movement.to_dict(),
    )
    if not fixture and not dry_run:
        try:
            from .editorial_bridge import enqueue_internal_trigger
            enqueue_internal_trigger(
                trigger_id=movement.movement_id,
                symbol=movement.symbol,
                asset_type=movement.asset_type,
                provider=movement.data_source,
                detected_at=movement.detected_at,
                movement_window=str(movement.window_minutes),
                internal_movement_class=movement.alert_type,
                data_quality=movement.data_quality,
            )
        except Exception as exc:
            log_error({
                "bot": "market-data",
                "stage": "independent_confirmation_queue",
                "error_type": type(exc).__name__,
            })
        try:
            from common.xai_social_intelligence import enqueue_market_movement
            enqueue_market_movement(movement)
        except Exception as exc:
            log_error({
                "bot": "market-data",
                "stage": "xai_event_queue",
                "error_type": type(exc).__name__,
            })
    shadow = None
    if not fixture:
        shadow = create_candidate(
            movement,
            chart_path=str(chart),
            draft_text=text,
            rights_passed=external_display_approved(),
            blocked_reason=(
                "market_post_disabled" if not market_post_enabled()
                else "license_blocked" if not external_display_approved()
                else "shadow_observation"
            ),
        )
    if send_preview:
        notify_market_preview(
            movement, text, fixture=fixture,
            blocked_reason="external_display_not_approved" if not external_display_approved() else "",
            chart_path=str(chart),
        )
    if dry_run or fixture:
        return {
            "status": "dry_run", "movement": movement.to_dict(),
            "text": text, "chart": str(chart), "metadata": str(metadata),
            "would_post": (
                external_display_approved()
                and market_post_enabled()
                and os.getenv("POST_ENABLED", "false").lower() in TRUE_VALUES
            ),
            "external_display_approved": external_display_approved(),
            "shadow_candidate": shadow,
        }
    result = publish_market(movement, str(chart))
    remember(movement, status=result.status, tweet_id=result.tweet_id)
    append_jsonl("alerts.jsonl", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "movement_id": movement.movement_id, "symbol": movement.symbol,
        "status": result.status, "reason": result.reason, "tweet_id": result.tweet_id,
    })
    return {
        "status": result.status, "movement": movement.to_dict(),
        "text": result.text, "chart": str(chart),
        "reason": result.reason, "tweet_id": result.tweet_id,
        "shadow_candidate": shadow,
    }


def run_market_monitor(*, dry_run: bool = False) -> dict[str, Any]:
    load_env()
    if not enabled():
        return {"status": "disabled"}
    selected = symbols_for_run()
    if not selected:
        result = {"status": "waiting_for_market_session", "symbols": []}
        log_run({"bot": "market-data", "result": result["status"]})
        return result
    provider = TwelveDataMarketProvider()
    results = []
    cross_changes: dict[str, float] = {}
    for config in selected:
        try:
            bars = provider.bars(config["symbol"], outputsize=180, cache_seconds=240)
            for bar in bars[-65:]:
                append_jsonl("bars_1m.jsonl", bar.to_dict())
            for bar in aggregate_bars(bars[-90:], minutes=5):
                if bar.complete:
                    append_jsonl("bars_5m.jsonl", bar.to_dict())
            if len(bars) >= 2:
                latest = bars[-1]
                cutoff = latest.timestamp.timestamp() - 3600
                prior = [bar for bar in bars if bar.timestamp.timestamp() <= cutoff]
                start = prior[-1] if prior else bars[0]
                if start.close:
                    cross_changes[config["symbol"]] = (latest.close / start.close - 1) * 100
            results.append({
                "symbol": config["symbol"],
                **evaluate_bars(
                    bars, asset_type=config["asset_type"],
                    display_name=config.get("display_name", ""),
                    dry_run=dry_run, fixture=False, send_preview=not dry_run,
                ),
            })
        except Exception as exc:
            results.append({
                "symbol": config["symbol"], "status": "provider_unavailable",
                "error_type": type(exc).__name__,
            })
    cleanup()
    recheck_result = None
    if not dry_run:
        try:
            from .evidence_flow import process_due_rechecks
            recheck_result = process_due_rechecks(dry_run=False)
        except Exception as exc:
            recheck_result = {
                "status": "failed_safe",
                "error_type": type(exc).__name__,
                "daemon_safe": True,
            }
    cross_result = None
    if (
        os.getenv("CROSS_ASSET_ENABLED", "true").lower() in TRUE_VALUES
        and len(cross_changes) >= 3
        and any(abs(value) >= 0.5 for value in cross_changes.values())
    ):
        signal = classify_cross_asset(cross_changes)
        append_jsonl("cross_asset_signals.jsonl", signal.to_dict())
        cross_result = signal.to_dict()
        if not dry_run:
            try:
                from common.xai_social_intelligence import enqueue_cross_asset_signal
                enqueue_cross_asset_signal(cross_result)
            except Exception as exc:
                log_error({
                    "bot": "market-data",
                    "stage": "xai_cross_asset_queue",
                    "error_type": type(exc).__name__,
                })
    status = "completed" if any(row["status"] not in {"provider_unavailable"} for row in results) else "provider_unavailable"
    log_run({"bot": "market-data", "result": status, "symbols": [row["symbol"] for row in results]})
    return {
        "status": status, "results": results, "cross_asset_signal": cross_result,
        "trigger_rechecks": recheck_result,
        "usage": usage_summary(),
    }


def run_fixture(kind: str, *, send_preview: bool = False) -> dict[str, Any]:
    if kind == "cross_asset":
        signal = classify_cross_asset(cross_asset_fixture("risk_off"))
        append_jsonl("fixture_results.jsonl", {
            "fixture_type": "cross_asset", **signal.to_dict(),
        })
        return {"status": "dry_run", "signal": signal.to_dict(), "would_post": False}
    if kind == "earnings":
        reaction = analyze_earnings_reaction(earnings_fixture())
        append_jsonl("fixture_results.jsonl", {
            "fixture_type": "earnings_reaction", **reaction,
        })
        bars = bars_fixture("GOOGL", direction="down")
        result = evaluate_bars(
            bars, asset_type="equity", display_name="Alphabet",
            dry_run=True, fixture=True, send_preview=send_preview,
        )
        return {"status": "dry_run", "reaction": reaction, "chart_result": result, "would_post": False}
    if kind == "etf":
        bars = bars_fixture("QQQ", asset_type="etf", direction="down")
        return evaluate_bars(
            bars, asset_type="etf", display_name="Nasdaq 100 ETF",
            dry_run=True, fixture=True, send_preview=send_preview,
        )
    bars = bars_fixture("NVDA", asset_type="equity", direction="down")
    return evaluate_bars(
        bars, asset_type="equity", display_name="NVIDIA",
        dry_run=True, fixture=True, send_preview=send_preview,
    )


def check_symbol(symbol: str) -> dict[str, Any]:
    config = symbol_config(symbol)
    bars = TwelveDataMarketProvider().bars(config["symbol"], outputsize=180, cache_seconds=240)
    return {
        "symbol": config["symbol"],
        **evaluate_bars(
            bars, asset_type=config["asset_type"],
            display_name=config.get("display_name", ""),
            dry_run=True, fixture=False, send_preview=False,
        ),
    }


def chart_symbol(symbol: str, *, period: str = "24h") -> dict[str, Any]:
    config = symbol_config(symbol)
    outputsize = {"1h": 60, "4h": 240, "24h": 390}.get(period, 390)
    bars = TwelveDataMarketProvider().bars(
        config["symbol"], outputsize=outputsize, cache_seconds=240
    )
    if len(bars) < 2:
        return {"status": "insufficient_data", "symbol": config["symbol"]}
    start, end = bars[0], bars[-1]
    pct = (end.close / start.close - 1) * 100 if start.close else 0.0
    raw = f"chart:{config['symbol']}:{period}:{end.timestamp.isoformat()}"
    quality, _delay = _quality(bars)
    movement = MarketMovement(
        movement_id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        symbol=config["symbol"], asset_type=config["asset_type"],
        direction="up" if pct >= 0 else "down",
        start_price=start.close, current_price=end.close,
        absolute_change=end.close - start.close, percentage_change=pct,
        window_minutes=max(1, int((end.timestamp-start.timestamp).total_seconds()/60)),
        high=max(bar.high for bar in bars), low=min(bar.low for bar in bars),
        detected_at=end.timestamp, volume=end.volume,
        data_quality=quality, alert_type="what_to_watch",
    )
    chart, metadata = create_market_chart(
        bars, movement, display_name=config.get("display_name", ""),
        delayed=quality != "good",
    )
    return {
        "status": "created", "symbol": config["symbol"],
        "chart": str(chart), "metadata": str(metadata),
        "data_quality": quality, "would_post": False,
    }


def market_status() -> dict[str, Any]:
    capabilities_path = market_data_dir() / "twelve_data_capabilities.json"
    capabilities = {}
    try:
        import json
        capabilities = json.loads(capabilities_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    state = load_state()
    last_bar = None
    try:
        import json
        direct_path = market_data_dir() / "last_market_data.json"
        if direct_path.exists():
            last_bar = json.loads(direct_path.read_text(encoding="utf-8"))
        from .storage import read_jsonl
        rows = read_jsonl("bars_1m.jsonl", limit=500)
        stored_bar = max(rows, key=lambda row: str(row.get("timestamp", "")), default=None)
        if stored_bar and (
            not last_bar
            or str(stored_bar.get("timestamp", "")) > str(
                last_bar.get("timestamp") or last_bar.get("source_timestamp") or ""
            )
        ):
            last_bar = stored_bar
    except Exception:
        last_bar = None
    last_timestamp = str(
        (last_bar or {}).get("timestamp")
        or (last_bar or {}).get("source_timestamp")
        or ""
    )
    data_age_seconds = None
    try:
        parsed = datetime.fromisoformat(last_timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        data_age_seconds = max(0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        pass
    status_quality = (last_bar or {}).get("data_quality")
    if data_age_seconds is not None:
        stale_seconds = int(os.getenv("MARKET_DATA_STALE_SECONDS", "300") or 300)
        if data_age_seconds > stale_seconds:
            status_quality = "delayed" if data_age_seconds < 86400 else "rejected"
    return {
        "enabled": enabled(), "post_enabled": market_post_enabled(),
        "external_display_approved": external_display_approved(),
        "provider": provider_status(),
        "plan": capabilities.get("plan_name", "unknown"),
        "available_asset_types": ["equity", "etf", "forex", "crypto"],
        "enabled_symbols": [row["symbol"] for row in enabled_symbols()],
        "rotation_index": state.get("rotation_index", 0),
        "alert_count": len(state.get("alerts", [])),
        "last_quote": {
            "kind": (last_bar or {}).get("kind", "bar" if last_bar else None),
            "symbol": (last_bar or {}).get("symbol"),
            "source_timestamp": last_timestamp or None,
            "data_age_seconds": data_age_seconds,
            "data_quality": status_quality,
        },
        "usage": usage_summary(),
    }
