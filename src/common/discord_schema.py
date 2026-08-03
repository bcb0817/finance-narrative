"""Allowlist-first Discord payload construction."""
from __future__ import annotations

import json
from typing import Any

from common.operations_alerts import redact_discord_text


SCHEMAS = {
    "post_success": ("bot_type", "text", "display_url", "created_at", "media_type", "safe_summary"),
    "fx_alert": ("pair", "movement_id", "movement_window", "change", "change_percent",
                 "cause_confidence", "chart_path", "blocked_reason"),
    "operations_alert": ("severity", "component", "safe_message", "error_type",
                         "first_seen", "last_seen", "resolved"),
    "xai_research": ("run_id", "radar_mode", "event_count", "cost_usd",
                     "event_summaries", "representative_posts",
                     "content_opportunities", "cache_hit", "failure_reason"),
}


def sanitize_payload(notification_type: str, value: dict[str, Any]) -> dict[str, Any]:
    allowed = SCHEMAS.get(notification_type)
    if allowed is None:
        raise ValueError("unknown Discord notification type")
    safe: dict[str, Any] = {}
    for key in allowed:
        if key not in value or value[key] is None:
            continue
        raw = value[key]
        if isinstance(raw, (str, int, float, bool)):
            cleaned = redact_discord_text(raw)
        else:
            cleaned = redact_discord_text(json.dumps(raw, ensure_ascii=False))
        safe[key] = cleaned[:1500]
    second_pass = redact_discord_text(json.dumps(safe, ensure_ascii=False))
    if "<redacted>" in second_pass:
        # Redacted placeholders are safe and prove the second scanner ran.
        safe["_secrets_redacted"] = "true"
    return safe
