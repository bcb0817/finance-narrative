from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from common.runtime import load_env, log_error, log_run

from .chart import create_chart
from .context import classify_context
from .detector import detect_movements, strongest_movement
from .fixture import movement_fixture
from .models import FxBar, normalize_pair
from .notifications import notify_preview, notify_result
from .post import build_post, publish
from .providers import get_provider
from .quality import validate_bars
from .state import check_alert_gate, remember_alert
from .storage import append_jsonl, cleanup, load_bar_cache, save_bar_cache


TRUE_VALUES = {"1", "true", "yes"}


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
        stale_seconds=int(os.getenv("FX_DATA_MAX_AGE_SECONDS", "90") or 90),
    )
    if not quality.good:
        append_jsonl("alerts.jsonl", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "quality_blocked",
            "reasons": quality.reasons,
        })
        return {"status": "quality_blocked", "quality": quality.quality, "reasons": quality.reasons}
    movement = strongest_movement(detect_movements(bars))
    if movement is None:
        return {"status": "no_material_movement", "quality": quality.quality}
    context = classify_context()
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
    text = build_post(movement)
    if send_preview:
        notify_preview(movement, text)
    if dry_run:
        return {
            "status": "dry_run",
            "movement": movement.to_dict(),
            "text": text,
            "chart": str(image_path),
            "metadata": str(metadata_path),
            "would_post": os.getenv("FX_POST_ENABLED", "false").strip().lower() in TRUE_VALUES,
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
        result["pair"] = selected
        result["fixture"] = fixture
        cleanup(retention_days=int(os.getenv("FX_RETENTION_DAYS", "90") or 90))
        log_run({"bot": "fx-alert", "result": result.get("status"), "pair": selected})
        return result
    except Exception as exc:
        log_error({"bot": "fx-alert", "error_type": type(exc).__name__, "pair": selected})
        return {
            "status": "provider_unavailable",
            "pair": selected,
            "error_type": type(exc).__name__,
            "safe_failure": True,
        }
