"""Safe downstream use and attribution for xAI X Search signals."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

from common.runtime import JST, state_dir


def _topic_match(title: str, topic: dict) -> bool:
    lower = title.lower()
    name = str(topic.get("topic") or "").strip().lower()
    if len(name) >= 3 and name in lower:
        return True
    for ticker in topic.get("tickers") or []:
        symbol = str(ticker).strip()
        if symbol and re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])",
                                title, flags=re.IGNORECASE):
            return True
    return False


def match_topic(title: str, topics: list[dict]) -> dict | None:
    matches = [topic for topic in topics if _topic_match(title, topic)]
    return max(
        matches,
        key=lambda item: (
            float(item.get("acceleration_score") or 0),
            float(item.get("velocity_score") or 0),
        ),
        default=None,
    )


def prioritize_candidates(candidates: list[Any], topics: list[dict]) -> list[Any]:
    """Apply xAI ranking after explicit enablement or machine-only qualification."""
    if not _score_bonus_effective():
        return list(candidates)
    ranked = []
    for original_index, candidate in enumerate(candidates):
        matched = match_topic(str(getattr(candidate, "title", "")), topics)
        ranked.append((
            0 if matched else 1,
            -float((matched or {}).get("acceleration_score") or 0),
            -float((matched or {}).get("velocity_score") or 0),
            original_index,
            candidate,
        ))
    ranked.sort(key=lambda item: item[:4])
    return [item[-1] for item in ranked]


def _score_bonus_effective() -> bool:
    true_values = {"1", "true", "yes", "on"}
    if os.getenv("XAI_SCORE_BONUS_ENABLED", "false").lower() in true_values:
        return True
    if os.getenv("XAI_SCORE_BONUS_AUTO_ENABLE", "true").lower() not in true_values:
        return False
    path = state_dir() / "xai" / "score_bonus_shadow.jsonl"
    if not path.exists():
        return False
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    required_days = int(os.getenv("XAI_SCORE_BONUS_SHADOW_DAYS", "14") or 14)
    minimum_rows = int(
        os.getenv("XAI_SCORE_BONUS_MIN_OBSERVATIONS", "20") or 20
    )
    observed_dates = {
        str(row.get("timestamp") or "")[:10]
        for row in rows if row.get("timestamp")
    }
    if len(observed_dates) < required_days or len(rows) < minimum_rows:
        return False
    tweet_ids = {
        str(row.get("tweet_id") or "")
        for row in rows if row.get("tweet_id")
    }
    metrics_path = state_dir() / "metrics_snapshots.jsonl"
    if not metrics_path.exists():
        return False
    for line in metrics_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            metric = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            str(metric.get("tweet_id") or "") in tweet_ids
            and metric.get("stage") == "24h"
            and metric.get("status") == "collected"
        ):
            return True
    return False


def _events_path():
    path = state_dir() / "xai" / "downstream_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record_downstream_event(
    run_id: str,
    event: str,
    *,
    candidate_id: str = "",
    tweet_id: str = "",
    now: datetime | None = None,
) -> bool:
    if not run_id or event not in {"news_candidate", "post_created"}:
        return False
    identity = tweet_id or candidate_id
    if not identity:
        return False
    digest = hashlib.sha256(
        f"{run_id}:{event}:{identity}".encode("utf-8")
    ).hexdigest()[:20]
    path = _events_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if f'"event_id": "{digest}"' in existing:
        return False
    row = {
        "timestamp": (now or datetime.now(JST)).isoformat(),
        "event_id": digest,
        "run_id": run_id,
        "event": event,
        "candidate_hash": hashlib.sha256(
            candidate_id.encode("utf-8")).hexdigest()[:16] if candidate_id else "",
        "tweet_id": str(tweet_id or ""),
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def downstream_events(days: int = 30) -> list[dict]:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    result = []
    path = _events_path()
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(str(row.get("timestamp") or ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=JST)
            if when >= cutoff:
                result.append(row)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return result
