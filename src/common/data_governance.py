"""Twelve Data publication rights and provider-degradation policy."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


TRUE_VALUES = {"1", "true", "yes", "on"}
OFFICIAL_EDITORIAL_GROUPS = {
    "official_macro", "official_regulatory", "official_policy",
    "official_fx", "company_filings",
}
OFFICIAL_EDITORIAL_DOMAINS = {
    "federalreserve.gov", "bea.gov", "bls.gov", "eia.gov", "sec.gov",
    "whitehouse.gov", "treasury.gov", "boj.or.jp", "ecb.europa.eu",
}


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
        "external_rights_configuration_required": (
            status is not DisplayStatus.APPROVED
        ),
        "automated_fail_closed": True,
        "human_review_required": False,
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
        "items": [
            {"item": item, "configuration_status": "unknown"}
            for item in items
        ],
        "instruction": (
            "Publication remains automatically blocked until explicit rights "
            "configuration is supplied."
        ),
        "human_review_required": False,
        "automated_fail_closed": True,
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
        "provider_isolated_editorial_x": {
            **provider_isolated_editorial_decision(
                source_url="https://www.federalreserve.gov/",
                source_group="official_macro",
                provider_lineage=[],
            ),
            "scope": (
                "official-source editorial only; no Twelve Data trigger, "
                "price, movement, chart, or derived value"
            ),
            "not_a_legal_determination": True,
        },
        "internal_analysis_continues": True,
    }


def _domain_allowed(host: str) -> bool:
    host = host.lower().strip(".")
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_EDITORIAL_DOMAINS)


def provider_isolated_editorial_decision(
    *,
    source_url: str,
    source_group: str,
    provider_lineage: list[str] | tuple[str, ...] = (),
) -> dict:
    """Allow an independent editorial lane, never a transformed provider-data lane."""
    try:
        host = (urlparse(source_url).hostname or "").lower()
    except ValueError:
        host = ""
    lineage = {str(item).strip().lower() for item in provider_lineage if item}
    enabled = _flag("OFFICIAL_EDITORIAL_POST_ENABLED", True)
    allowed = bool(
        enabled
        and source_group in OFFICIAL_EDITORIAL_GROUPS
        and _domain_allowed(host)
        and not lineage
    )
    reasons = []
    if not enabled:
        reasons.append("official_editorial_disabled")
    if source_group not in OFFICIAL_EDITORIAL_GROUPS:
        reasons.append("source_group_not_official")
    if not _domain_allowed(host):
        reasons.append("source_domain_not_allowlisted")
    if lineage:
        reasons.append("market_data_provider_lineage_present")
    return {
        "allowed": allowed,
        "reason": "official_public_source_only" if allowed else ",".join(reasons),
        "source_host": host,
        "source_group": source_group,
        "provider_lineage": sorted(lineage),
        "includes_twelve_data": "twelvedata" in lineage,
        "external_display_rights_not_inferred": True,
    }


def independent_confirmation_decision(
    *,
    source_url: str,
    source_group: str,
    publication_provider_lineage: list[str] | tuple[str, ...] = (),
    internal_trigger_providers: list[str] | tuple[str, ...] = (),
    includes_trigger_values: bool = False,
    includes_trigger_chart: bool = False,
) -> dict:
    """Decide whether an internal trigger may lead to a source-only post.

    A market provider may initiate research, but it must not be a publication
    fact source.  The resulting post must be based on a separate RSS/article or
    official data release and may not reuse the trigger's values or chart.
    """
    try:
        host = (urlparse(source_url).hostname or "").lower()
    except ValueError:
        host = ""
    publication_lineage = {
        str(item).strip().lower() for item in publication_provider_lineage if item
    }
    trigger_lineage = {
        str(item).strip().lower() for item in internal_trigger_providers if item
    }
    supported_group = source_group in (
        OFFICIAL_EDITORIAL_GROUPS
        | {"market_news", "sector_news", "crypto_news"}
    )
    separate_source = bool(
        host
        and "twelvedata" not in host
        and "twelvedata" not in publication_lineage
    )
    allowed = bool(
        _flag("INDEPENDENT_CONFIRMATION_ENABLED", True)
        and supported_group
        and separate_source
        and not includes_trigger_values
        and not includes_trigger_chart
    )
    reasons = []
    if not _flag("INDEPENDENT_CONFIRMATION_ENABLED", True):
        reasons.append("independent_confirmation_disabled")
    if not supported_group:
        reasons.append("unsupported_independent_source_group")
    if not separate_source:
        reasons.append("publication_source_not_independent")
    if includes_trigger_values:
        reasons.append("internal_trigger_values_present")
    if includes_trigger_chart:
        reasons.append("internal_trigger_chart_present")
    return {
        "allowed": allowed,
        "reason": "independent_source_only" if allowed else ",".join(reasons),
        "source_host": host,
        "source_group": source_group,
        "publication_provider_lineage": sorted(publication_lineage),
        "internal_trigger_providers": sorted(trigger_lineage),
        "includes_trigger_values": bool(includes_trigger_values),
        "includes_trigger_chart": bool(includes_trigger_chart),
        "twelve_data_is_internal_trigger_only": (
            "twelvedata" in trigger_lineage
            and "twelvedata" not in publication_lineage
        ),
        "not_a_legal_determination": True,
    }


def validate_provider_isolated_editorial_text(
    text: str, *, source_title: str
) -> dict:
    """Block text that appears to reintroduce live provider-derived observations."""
    normalized = str(text or "")
    prohibited = (
        r"\b(?:1m|5m|15m|30m|1h|4h|24h)\b",
        r"(?:現在値|リアルタイム|チャートでは|データ提供|Twelve\s*Data)",
        r"(?:から|→)\s*\d+(?:\.\d+)?(?:円|ドル)?\s*(?:へ|まで)",
    )
    hit = next((pattern for pattern in prohibited if re.search(pattern, normalized, re.I)), "")
    source_numbers = set(re.findall(r"\d+(?:\.\d+)?", source_title or ""))
    output_numbers = set(re.findall(r"\d+(?:\.\d+)?", normalized))
    invented_numbers = sorted(output_numbers - source_numbers)
    allowed = bool(normalized.strip()) and not hit and not invented_numbers
    return {
        "allowed": allowed,
        "reason": (
            "official_editorial_text"
            if allowed else
            "provider_like_market_observation" if hit else
            "number_not_present_in_official_source_title" if invented_numbers else
            "empty_text"
        ),
        "matched_pattern": hit,
        "invented_numbers": invented_numbers[:10],
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
