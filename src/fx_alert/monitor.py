from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from common.runtime import load_env, log_error, log_run

from .chart import create_chart
from .context import classify_context, load_integrated_context
from .detector import detect_movements, strongest_movement
from .fixture import movement_fixture
from .models import FxBar, normalize_pair
from .notifications import notify_preview, notify_result
from .post import build_post, publish
from .providers import get_provider
from .quality import validate_bars
from .state import check_alert_gate, remember_alert
from .storage import (
    append_jsonl,
    cleanup,
    load_bar_cache,
    load_state,
    save_bar_cache,
    save_state,
)


TRUE_VALUES = {"1", "true", "yes"}


def effective_max_age_seconds() -> int:
    """Return a freshness limit compatible with the polling cadence.

    A completed one-minute bar can legitimately trail wall-clock time.  The
    previous 90-second fixed limit rejected every poll when the five-minute
    monitor received bars about two minutes late.
    """
    configured = int(os.getenv("FX_DATA_MAX_AGE_SECONDS", "600") or 600)
    poll_seconds = int(os.getenv("FX_POLL_INTERVAL_MINUTES", "5") or 5) * 60
    finalization_allowance = int(
        os.getenv("FX_BAR_FINALIZATION_ALLOWANCE_SECONDS", "120") or 120
    )
    return max(configured, poll_seconds + finalization_allowance)


def _bar_age_seconds(bars: list[FxBar], *, now: datetime | None = None) -> float | None:
    if not bars:
        return None
    current = now or datetime.now(timezone.utc)
    latest = max(item.timestamp for item in bars)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return max(0.0, (current - latest).total_seconds())


def _record_quality_health(
    bars: list[FxBar], *, good: bool, reasons: list[str]
) -> dict[str, Any]:
    state = load_state()
    previous = state.get("quality_health", {})
    consecutive = 0 if good else int(previous.get("consecutive_blocked_runs", 0) or 0) + 1
    latest = max((item.timestamp for item in bars), default=None)
    health = {
        "status": "healthy" if good else "degraded",
        "consecutive_blocked_runs": consecutive,
        "reasons": [] if good else list(reasons),
        "latest_bar_at": latest.isoformat() if latest else None,
        "data_age_seconds": round(_bar_age_seconds(bars) or 0.0, 1) if bars else None,
        "max_age_seconds": effective_max_age_seconds(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state["quality_health"] = health
    state["updated_at"] = health["updated_at"]
    save_state(state)
    return health


def enabled() -> bool:
    load_env()
    return os.getenv("FX_ENABLED", "true").strip().lower() in TRUE_VALUES


def configured_pairs() -> list[str]:
    load_env()
    raw = os.getenv("FX_PAIRS", "USD/JPY")
    pairs: list[str] = []
    for item in raw.split(","):
        try:
            pairs.append(normalize_pair(item.strip()))
        except ValueError:
            continue
    return pairs or ["USDJPY"]


def fetch_bars(pair: str) -> list[FxBar]:
    provider = get_provider()
    cached = [FxBar.from_dict(row) for row in load_bar_cache(pair)]
    outputsize = 1500 if len(cached) < 1440 else 12
    fetched = provider.get_bars(pair, interval="1min", outputsize=outputsize)
    merged = {item.timestamp.isoformat(): item for item in cached}
    merged.update({item.timestamp.isoformat(): item for item in fetched})
    bars = sorted(merged.values(), key=lambda item: item.timestamp)[-5000:]
    for bar in fetched:
        append_jsonl("bars.jsonl", bar.to_dict())
    save_bar_cache(pair, [item.to_dict() for item in bars])
    try:
        quote = provider.get_quote(pair)
        append_jsonl("quotes.jsonl", quote.to_dict())
    except Exception:
        pass
    return bars


def evaluate(
    bars: list[FxBar],
    *,
    dry_run: bool,
    send_preview: bool = False,
) -> dict[str, Any]:
    quality = validate_bars(
        bars,
        minimum_points=int(os.getenv("FX_MIN_POINTS_FOR_ALERT", "12") or 12),
        stale_seconds=effective_max_age_seconds(),
    )
    if not quality.good:
        health = _record_quality_health(bars, good=False, reasons=quality.reasons)
        append_jsonl("alerts.jsonl", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "quality_blocked",
            "reasons": quality.reasons,
            "consecutive_blocked_runs": health["consecutive_blocked_runs"],
            "data_age_seconds": health["data_age_seconds"],
            "max_age_seconds": health["max_age_seconds"],
        })
        threshold = int(os.getenv("FX_QUALITY_ALERT_CONSECUTIVE_RUNS", "3") or 3)
        return {
            "status": (
                "quality_degraded"
                if health["consecutive_blocked_runs"] >= threshold
                else "quality_blocked"
            ),
            "quality": quality.quality,
            "reasons": quality.reasons,
            "health": health,
        }
    health = _record_quality_health(bars, good=True, reasons=[])
    movement = strongest_movement(detect_movements(bars))
    if movement is None:
        return {
            "status": "no_material_movement",
            "quality": quality.quality,
            "health": health,
        }
    context = classify_context(load_integrated_context())
    movement.cause_confidence = context.confidence
    movement.cause_summary = context.summary
    movement.context_sources = context.sources
    gate = check_alert_gate(movement)
    if not gate.allowed:
        append_jsonl("alerts.jsonl", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "movement_id": movement.movement_id,
            "status": "gate_blocked",
            "reason": gate.reason,
        })
        return {"status": "gate_blocked", "reason": gate.reason, "movement": movement.to_dict()}
    image_path, metadata_path = create_chart(bars, movement)
    append_jsonl("movements.jsonl", movement.to_dict())
    if not dry_run:
        try:
            from market_data.editorial_bridge import enqueue_internal_trigger
            enqueue_internal_trigger(
                trigger_id=movement.movement_id,
                symbol=f"{movement.pair[:3]}/{movement.pair[3:]}",
                asset_type="forex",
                provider=movement.data_source,
                detected_at=movement.detected_at,
                movement_window=movement.window,
                internal_movement_class=movement.alert_level,
                data_quality=quality.quality,
            )
        except Exception as exc:
            log_error({
                "bot": "fx-alert",
                "stage": "independent_confirmation_queue",
                "error_type": type(exc).__name__,
            })
        try:
            from common.xai_social_intelligence import enqueue_fx_movement
            enqueue_fx_movement(movement)
        except Exception as exc:
            # xAI research queuing must never stop the FX monitor.
            log_error({
                "bot": "fx-alert", "stage": "xai_event_queue",
                "error_type": type(exc).__name__,
            })
    text = build_post(movement)
    if send_preview:
        notify_preview(movement, text)
    if dry_run:
        from common.data_governance import publication_decision
        rights = publication_decision(
            surface="x", includes_chart=True, includes_numeric_data=True
        )
        return {
            "status": "dry_run",
            "movement": movement.to_dict(),
            "text": text,
            "chart": str(image_path),
            "metadata": str(metadata_path),
            "would_post": (
                rights.allowed
                and os.getenv("FX_POST_ENABLED", "false").strip().lower() in TRUE_VALUES
                and os.getenv("POST_ENABLED", "false").strip().lower() in TRUE_VALUES
            ),
            "publication_rights": rights.to_dict(),
        }
    result = publish(movement, str(image_path))
    remember_alert(movement, status=result.status, tweet_id=result.tweet_id)
    append_jsonl(
        "alerts.jsonl",
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "movement_id": movement.movement_id,
            "status": result.status,
            "tweet_id": result.tweet_id,
            "reason": result.reason,
        },
    )
    notify_result(movement, result.status, tweet_id=result.tweet_id)
    return {
        "status": result.status,
        "movement": movement.to_dict(),
        "text": result.text,
        "chart": str(image_path),
        "tweet_id": result.tweet_id,
        "reason": result.reason,
    }


def run_monitor(
    *,
    dry_run: bool = False,
    fixture: bool = False,
    pair: str | None = None,
    send_preview: bool = True,
) -> dict[str, Any]:
    load_env()
    selected = normalize_pair(pair or configured_pairs()[0])
    if not enabled():
        return {"status": "disabled", "pair": selected}
    try:
        bars = movement_fixture(selected) if fixture else fetch_bars(selected)
        result = evaluate(bars, dry_run=dry_run, send_preview=send_preview)
        if not dry_run and not fixture:
            try:
                from market_data.evidence_flow import process_due_rechecks
                result["trigger_rechecks"] = process_due_rechecks(dry_run=False)
            except Exception as exc:
                result["trigger_rechecks"] = {
                    "status": "failed_safe",
                    "error_type": type(exc).__name__,
                    "daemon_safe": True,
                }
        result["pair"] = selected
        result["fixture"] = fixture
        cleanup(retention_days=int(os.getenv("FX_RETENTION_DAYS", "90") or 90))
        log_run({"bot": "fx-alert", "result": result.get("status"), "pair": selected})
        return result
    except Exception as exc:
        # Keep enough context to diagnose provider failures without exposing
        # credentials that may be present in an upstream exception message.
        from common.daily_log_analysis import redact

        error_detail = redact(str(exc))[:240]
        log_error({
            "bot": "fx-alert",
            "error_type": type(exc).__name__,
            "error_detail": error_detail,
            "pair": selected,
        })
        return {
            "status": "provider_unavailable",
            "pair": selected,
            "error_type": type(exc).__name__,
            "safe_failure": True,
        }
