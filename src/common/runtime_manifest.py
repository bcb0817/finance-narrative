"""Reproducible runtime identity without secrets."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from common.runtime import JST, state_dir


ROOT = Path(__file__).resolve().parents[2]
SAFE_CONFIG_FILES = (
    ROOT / "config" / "schedule.json",
    ROOT / "config" / "market_watchlist.json",
    ROOT / ".env.example",
)


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, encoding="utf-8",
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def current_identity() -> dict:
    hashes = {path.name: _hash(path) for path in SAFE_CONFIG_FILES}
    combined = hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode("utf-8")
    ).hexdigest()
    source_entries = {
        str(path.relative_to(ROOT)): _hash(path)
        for path in sorted((ROOT / "src").rglob("*.py"))
    }
    source_entries["local_finance_bot.py"] = _hash(ROOT / "local_finance_bot.py")
    source_hash = hashlib.sha256(
        json.dumps(source_entries, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "git_commit": _git("rev-parse", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "dirty_working_tree": bool(_git("status", "--porcelain").strip()),
        "application_version": os.getenv("APP_VERSION", "local"),
        "config_hash": combined,
        "source_hash": source_hash,
        "schedule_hash": hashes.get("schedule.json"),
        "watchlist_hash": hashes.get("market_watchlist.json"),
        "python_version": platform.python_version(),
    }


def write_manifest() -> dict:
    payload = {"started_at": datetime.now(JST).isoformat(), **current_identity()}
    path = state_dir() / "runtime" / "runtime_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-manifest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {**payload, "path": str(path)}


def runtime_status() -> dict:
    current = current_identity()
    path = state_dir() / "runtime" / "runtime_manifest.json"
    try:
        running = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        running = {}
    config_changed = bool(running) and running.get("config_hash") != current["config_hash"]
    source_changed = bool(running) and running.get("source_hash") != current["source_hash"]
    return {
        "running_commit": running.get("git_commit"),
        "current_commit": current["git_commit"],
        "dirty_tree": current["dirty_working_tree"],
        "config_changed_since_startup": config_changed,
        "source_changed_since_startup": source_changed,
        "schedule_changed_since_startup": bool(running)
        and running.get("schedule_hash") != current["schedule_hash"],
        "watchlist_changed_since_startup": bool(running)
        and running.get("watchlist_hash") != current["watchlist_hash"],
        "restart_required": config_changed or source_changed
        or (bool(running) and running.get("git_commit") != current["git_commit"]),
        "manifest_present": bool(running),
    }
