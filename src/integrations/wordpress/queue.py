from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from common.runtime import JST, state_dir


class RadarPublishQueue:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "wordpress" / "radar_publish_queue.jsonl"

    def rows(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".radar-queue-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def enqueue(self, payload: dict, *, status: str = "pending", error: str = "") -> dict:
        rows, snapshot_id = self.rows(), str(payload.get("snapshot_id") or "")
        for row in rows:
            if row.get("snapshot_id") == snapshot_id:
                return row
        now = datetime.now(JST).isoformat()
        row = {"snapshot_id": snapshot_id, "status": status, "attempts": 0, "created_at": now,
               "updated_at": now, "last_error": str(error)[:160], "payload": payload}
        rows.append(row); self._write(rows)
        return row

    def update(self, snapshot_id: str, status: str, *, error: str = "", increment: bool = False) -> None:
        rows = self.rows()
        for row in rows:
            if row.get("snapshot_id") == snapshot_id:
                row["status"] = status; row["updated_at"] = datetime.now(JST).isoformat()
                row["last_error"] = str(error)[:160]
                if increment: row["attempts"] = int(row.get("attempts", 0)) + 1
        self._write(rows)

    def expire_old(self, days: int = 30) -> int:
        cutoff = datetime.now(JST) - timedelta(days=days); rows = self.rows(); changed = 0
        for row in rows:
            try: old = datetime.fromisoformat(row["created_at"]) < cutoff
            except (KeyError, ValueError): old = True
            if old and row.get("status") not in {"published", "expired"}:
                row["status"] = "expired"; changed += 1
        if changed: self._write(rows)
        return changed
