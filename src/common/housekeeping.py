"""Conservative cleanup for generated local artifacts.

Operational history under ``data`` is intentionally preserved.  Only known
re-creatable caches, dated outputs, reports, backups, and Python bytecode are
eligible for deletion.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from common.runtime import REPO_ROOT, state_dir


@dataclass(frozen=True)
class RetentionRule:
    relative_path: str
    days: int


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(int(os.getenv(name, str(default)) or default), minimum)
    except ValueError:
        return default


def retention_rules() -> tuple[RetentionRule, ...]:
    return (
        RetentionRule("outputs", _env_int("OUTPUT_RETENTION_DAYS", 30)),
        RetentionRule("backups", _env_int("BACKUP_RETENTION_DAYS", 30)),
        RetentionRule(".local-backups", _env_int("BACKUP_RETENTION_DAYS", 30)),
        RetentionRule("logs/reports", _env_int("REPORT_RETENTION_DAYS", 90)),
        RetentionRule("data/market_data/cache", _env_int("CACHE_RETENTION_DAYS", 7)),
        RetentionRule("knowledge/viral_patterns/reviews",
                      _env_int("REVIEW_RETENTION_DAYS", 90)),
    )


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
        return True
    except (OSError, ValueError):
        return False


def _remove_empty_parents(root: Path, *, dry_run: bool) -> int:
    removed = 0
    if not root.exists():
        return removed
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            if not any(directory.iterdir()):
                if not dry_run:
                    directory.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_generated(*, dry_run: bool = False, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    deleted_files = 0
    deleted_dirs = 0
    reclaimed_bytes = 0
    errors: list[str] = []

    for rule in retention_rules():
        root = REPO_ROOT / rule.relative_path
        if not root.exists() or not _inside_repo(root):
            continue
        cutoff = now - rule.days * 86400
        for path in root.rglob("*"):
            try:
                if (path.is_file() and not path.is_symlink()
                        and path.stat().st_mtime < cutoff):
                    size = path.stat().st_size
                    if not dry_run:
                        path.unlink()
                    deleted_files += 1
                    reclaimed_bytes += size
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}")
        deleted_dirs += _remove_empty_parents(root, dry_run=dry_run)

    # Bytecode and editor-style timestamped backups are always reproducible.
    for root, dirs, files in os.walk(REPO_ROOT, topdown=True):
        dirs[:] = [name for name in dirs if name not in {".git", ".venv"}]
        current = Path(root)
        for name in list(dirs):
            if name != "__pycache__":
                continue
            path = current / name
            try:
                size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                if not dry_run:
                    shutil.rmtree(path)
                deleted_dirs += 1
                reclaimed_bytes += size
                dirs.remove(name)
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}")
        cutoff = now - _env_int("TEMP_BACKUP_RETENTION_DAYS", 7) * 86400
        for name in files:
            if ".bak-" not in name:
                continue
            path = current / name
            try:
                if path.stat().st_mtime < cutoff and _inside_repo(path):
                    size = path.stat().st_size
                    if not dry_run:
                        path.unlink()
                    deleted_files += 1
                    reclaimed_bytes += size
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}")

    return {
        "status": "dry_run" if dry_run else ("partial" if errors else "success"),
        "deleted_files": deleted_files,
        "deleted_dirs": deleted_dirs,
        "reclaimed_bytes": reclaimed_bytes,
        "errors": errors[:20],
    }


def run_scheduled_housekeeping(*, force: bool = False, dry_run: bool = False) -> dict:
    enabled = os.getenv("HOUSEKEEPING_ENABLED", "true").strip().lower()
    if enabled not in {"1", "true", "yes"} and not force:
        return {"status": "disabled"}

    marker = state_dir() / "runtime" / "housekeeping.json"
    interval = _env_int("HOUSEKEEPING_INTERVAL_HOURS", 24) * 3600
    now = time.time()
    if not force and marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            if now - float(previous.get("completed_at_epoch", 0)) < interval:
                return {"status": "not_due"}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    result = cleanup_generated(dry_run=dry_run, now=now)
    if not dry_run and result["status"] == "success":
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"completed_at_epoch": now, **result}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker)
    return result
