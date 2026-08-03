from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from common.runtime import state_dir


UNKNOWN_CAUSE = "現時点で明確な材料は確認できていません"


@dataclass(frozen=True)
class FxContext:
    confidence: str = "unknown"
    summary: str = UNKNOWN_CAUSE
    sources: list[str] = field(default_factory=list)
    official_intervention_confirmation: bool = False


def load_integrated_context(*, hours: int = 6) -> list[dict[str, object]]:
    """Load conservative xAI synthesis as supporting context, never as intervention proof."""
    path = state_dir() / "fx" / "integrated_context.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    result = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
            when = datetime.fromisoformat(str(row.get("timestamp") or ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if when.astimezone(timezone.utc) < cutoff:
            continue
        evidence = row.get("evidence") or {}
        source_urls = row.get("source_urls") or []
        verified = bool(
            evidence.get("official_source_present")
            and evidence.get("quality") in {"high", "medium"}
            and source_urls
            and not row.get("facts_needing_confirmation")
        )
        result.append({
            "summary": row.get("summary"),
            "url": source_urls[0] if source_urls else "",
            "verified": verified,
            "analysis_id": row.get("analysis_id"),
            "xai_synthesis": True,
            "official_intervention_confirmation": False,
        })
    return result[-5:]


def classify_context(
    candidates: list[dict[str, object]] | None = None,
    *,
    official_mof_confirmation: bool = False,
) -> FxContext:
    """Conservatively classify supplied context; never infer intervention."""
    rows = candidates or []
    if official_mof_confirmation:
        sources = [str(row.get("url", "")) for row in rows if row.get("url")]
        return FxContext(
            confidence="confirmed",
            summary="財務省の公式発表で為替介入が確認されました",
            sources=sources,
            official_intervention_confirmation=True,
        )
    verified = [row for row in rows if row.get("verified") and row.get("summary")]
    if len(verified) >= 2:
        return FxContext(
            confidence="likely",
            summary=str(verified[0]["summary"]),
            sources=[str(row.get("url", "")) for row in verified if row.get("url")],
        )
    if verified:
        return FxContext(
            confidence="possible",
            summary=str(verified[0]["summary"]),
            sources=[str(verified[0].get("url", ""))] if verified[0].get("url") else [],
        )
    return FxContext()
