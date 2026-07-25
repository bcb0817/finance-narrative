from __future__ import annotations

from dataclasses import dataclass, field


UNKNOWN_CAUSE = "現時点で明確な材料は確認できていません"


@dataclass(frozen=True)
class FxContext:
    confidence: str = "unknown"
    summary: str = UNKNOWN_CAUSE
    sources: list[str] = field(default_factory=list)
    official_intervention_confirmation: bool = False


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
