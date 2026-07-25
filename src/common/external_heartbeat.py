"""Provider-neutral external dead-man heartbeat publisher."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from common.data_governance import masked_url_host
from common.runtime import JST, state_dir


TRUE_VALUES = {"1", "true", "yes", "on"}


def _path() -> Path:
    return state_dir() / "external_heartbeat.json"


def _read() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(value: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".external-heartbeat-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enabled() -> bool:
    return os.getenv("EXTERNAL_HEARTBEAT_ENABLED", "false").lower() in TRUE_VALUES


def status() -> dict:
    state = _read()
    return {
        "enabled": enabled(),
        "url_configured": bool(os.getenv("EXTERNAL_HEARTBEAT_URL", "").strip()),
        "url_host": masked_url_host("EXTERNAL_HEARTBEAT_URL"),
        "interval_minutes": int(os.getenv("EXTERNAL_HEARTBEAT_INTERVAL_MINUTES", "5")),
        "last_successful_ping": state.get("last_successful_ping"),
        "last_attempt": state.get("last_attempt"),
        "consecutive_failures": int(state.get("consecutive_failures", 0) or 0),
        "last_error_type": state.get("last_error_type"),
    }


def publish(*, session=requests, dry_run: bool = False, now: datetime | None = None) -> dict:
    current = (now or datetime.now(JST)).astimezone(JST)
    url = os.getenv("EXTERNAL_HEARTBEAT_URL", "").strip()
    payload = {
        "service": "finance-narrative",
        "status": "ok",
        "timestamp": current.isoformat(),
        "daemon_instance_id": os.getenv("DAEMON_INSTANCE_ID", "") or "dry-run",
        "version": os.getenv("APP_VERSION", "local"),
    }
    if dry_run:
        parsed = urlparse(url) if url else None
        return {
            "status": "dry_run",
            "would_send": enabled() and bool(parsed and parsed.scheme == "https" and parsed.hostname),
            "payload": payload,
            **status(),
        }
    if not enabled():
        return {"status": "disabled", **status()}
    state = _read()
    if not dry_run and state.get("last_attempt"):
        try:
            previous = datetime.fromisoformat(str(state["last_attempt"]))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=JST)
            interval = int(os.getenv("EXTERNAL_HEARTBEAT_INTERVAL_MINUTES", "5"))
            if (current - previous.astimezone(JST)).total_seconds() < interval * 60:
                return {"status": "not_due", **status()}
        except ValueError:
            pass
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {"status": "configuration_error", "reason": "valid_https_url_required", **status()}
    payload["daemon_instance_id"] = os.getenv("DAEMON_INSTANCE_ID", "") or uuid.uuid4().hex[:12]
    try:
        response = session.post(
            url, json=payload,
            timeout=int(os.getenv("EXTERNAL_HEARTBEAT_TIMEOUT_SECONDS", "10")),
        )
        response.raise_for_status()
        state.update({
            "last_attempt": current.isoformat(),
            "last_successful_ping": current.isoformat(),
            "consecutive_failures": 0,
            "last_error_type": None,
        })
        result = "sent"
    except requests.RequestException as exc:
        state.update({
            "last_attempt": current.isoformat(),
            "consecutive_failures": int(state.get("consecutive_failures", 0) or 0) + 1,
            "last_error_type": type(exc).__name__,
        })
        result = "delivery_failed"
    _write(state)
    return {"status": result, **status()}
