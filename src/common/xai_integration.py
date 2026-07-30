"""Safe downstream use and attribution for xAI X Search signals."""
from __future__ import annotations

import hashlib
import json
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
    """Use xAI only as a stable tie-breaker; it never bypasses posting gates."""
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
