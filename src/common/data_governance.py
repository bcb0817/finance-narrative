"""Twelve Data publication rights and provider-degradation policy."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


TRUE_VALUES = {"1", "true", "yes", "on"}


class DisplayStatus(str, Enum):
    UNKNOWN = "unknown"
    APPROVED = "approved"
    RESTRICTED = "restricted"
    DENIED = "denied"


def display_status() -> DisplayStatus:
    raw = os.getenv("TWELVEDATA_EXTERNAL_DISPLAY_STATUS", "").strip().lower()
    if not raw and _flag("TWELVEDATA_EXTERNAL_DISPLAY_APPROVED"):
        raw = "approved"
    raw = raw or "unknown"
    try:
        return DisplayStatus(raw)
    except ValueError:
        return DisplayStatus.UNKNOWN


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in TRUE_VALUES


def license_status() -> dict:
    status = display_status()
    legacy_approved = (
        "TWELVEDATA_EXTERNAL_DISPLAY_STATUS" not in os.environ
        and _flag("TWELVEDATA_EXTERNAL_DISPLAY_APPROVED")
    )
    return {
        "status": status.value,
        "attribution_configured": bool(os.getenv("TWELVEDATA_ATTRIBUTION_TEXT", "").strip()),
        "public_chart_allowed": _flag("TWELVEDATA_PUBLIC_CHART_ALLOWED", legacy_approved),
        "public_numeric_data_allowed": _flag("TWELVEDATA_PUBLIC_NUMERIC_DATA_ALLOWED", legacy_approved),
        "derived_data_allowed": _flag("TWELVEDATA_DERIVED_DATA_ALLOWED", legacy_approved),
        "discord_market_data_audience": os.getenv(
            "DISCORD_MARKET_DATA_AUDIENCE", "internal"
        ).strip().lower(),
        "human_contract_review_required": status is not DisplayStatus.APPROVED,
    }


@dataclass(frozen=True)
class PublicationDecision:
    allowed: bool
    reason: str
    status: str
    chart_allowed: bool
    numeric_data_allowed: bool
    internal_processing_allowed: bool = True

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def publication_decision(
    *,
    surface: str,
    includes_chart: bool = False,
    includes_numeric_data: bool = True,
    audience: str = "external",
) -> PublicationDecision:
    rights = license_status()
    status = DisplayStatus(rights["status"])
    internal = audience.strip().lower() == "internal"
    if internal:
        return PublicationDecision(
            True, "internal_analysis_allowed", status.value,
            bool(rights["public_chart_allowed"]), bool(rights["public_numeric_data_allowed"]),
        )
    allowed = status is DisplayStatus.APPROVED
    reasons: list[str] = []
    if not allowed:
        reasons.append(f"external_display_status={status.value}")
    if includes_chart and not rights["public_chart_allowed"]:
        allowed = False
        reasons.append("public_chart_not_allowed")
    if includes_numeric_data and not rights["public_numeric_data_allowed"]:
        allowed = False
        reasons.append("public_numeric_data_not_allowed")
    if surface == "discord" and rights["discord_market_data_audience"] != "external":
        allowed = False
        reasons.append("discord_audience_not_external")
    return PublicationDecision(
        allowed,
        "approved" if allowed else ",".join(reasons),
        status.value,
        bool(rights["public_chart_allowed"]),
        bool(rights["public_numeric_data_allowed"]),
    )


def license_checklist() -> dict:
    items = [
        "public_x_price_text", "public_chart_prices", "monetized_account",
        "real_time_data", "delayed_data", "derived_data", "attribution",
        "retention_period", "note", "youtube", "website", "discord",
    ]
    return {
        "status": display_status().value,
        "items": [{"item": item, "review_status": "unknown"} for item in items],
        "instruction": "Confirm these terms with Twelve Data or your contract administrator.",
    }


def market_publication_status() -> dict:
    audience = os.getenv("DISCORD_MARKET_DATA_AUDIENCE", "internal")
    return {
        "license": license_status(),
        "fx_x": publication_decision(surface="x", includes_chart=True).to_dict(),
        "market_x": publication_decision(surface="x", includes_chart=True).to_dict(),
        "discord": publication_decision(
            surface="discord", includes_chart=True, audience=audience
        ).to_dict(),
        "internal_analysis_continues": True,
    }


def masked_url_host(name: str) -> str:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw).hostname or ""
    except ValueError:
        return "configured"
    if not host:
        return "configured"
    parts = host.split(".")
    return "***." + ".".join(parts[-2:]) if len(parts) >= 2 else "***"


def classify_provider(
    *,
    available: bool,
    authenticated: bool = True,
    data_age_seconds: float | None = None,
    max_age_seconds: float = 180,
    budget_limited: bool = False,
) -> str:
    if display_status() is not DisplayStatus.APPROVED:
        return "license_blocked"
    if not authenticated:
        return "auth_failed"
    if budget_limited:
        return "budget_limited"
    if not available:
        return "unavailable"
    if data_age_seconds is not None and data_age_seconds > max_age_seconds:
        return "stale"
    if data_age_seconds is not None and data_age_seconds > max_age_seconds * 0.75:
        return "degraded"
    return "healthy"
