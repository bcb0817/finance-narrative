from __future__ import annotations

import json
import os
from pathlib import Path

from common.runtime import state_dir
from .client import WordPressRadarClient, WordPressRadarError
from .fixture import radar_fixture
from .queue import RadarPublishQueue
from .radar_models import build_snapshot

TRUE = {"1", "true", "yes", "on"}


def _load_source(*, fixture: bool = False) -> dict:
    if fixture:
        return radar_fixture()
    xai_dir = state_dir() / "xai"
    # Prefer the newest successful Social Intelligence run and its normalized
    # observations. Fall back to the legacy topic radar cache.
    def jsonl(name: str) -> list[dict]:
        try:
            return [json.loads(line) for line in (xai_dir / name).read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []
    runs = [row for row in jsonl("runs.jsonl") if row.get("status") == "success"]
    if runs:
        run = runs[-1]; run_id = str(run.get("run_id") or "")
        events = {str(row.get("candidate_id")): row for row in jsonl("events.jsonl")}
        topics = []
        overrides = _translation_overrides()
        for observation in [row for row in jsonl("observations.jsonl") if str(row.get("run_id")) == run_id][:5]:
            event = events.get(str(observation.get("event_id")), {})
            interpretation = observation.get("interpretation") or {}; delta = observation.get("delta") or {}
            override = overrides.get(str(observation.get("event_id")), {})
            topics.append({"event_id": observation.get("event_id"), "title": event.get("title") or interpretation.get("topic_summary"), "title_ja": override.get("title"),
                "canonical_topic": event.get("canonical_topic"), "tickers": event.get("tickers") or event.get("currencies") or [],
                "entities": event.get("entities") or [], "category": _category(event), "summary": interpretation.get("topic_summary"),
                "observed_velocity_score": delta.get("observed_velocity_score"), "observed_acceleration_score": delta.get("observed_acceleration_score"),
                "market_impact_score": event.get("market_impact_score"), "social_research_value": event.get("social_research_value"),
                "reliability": interpretation.get("confidence"), "official_source_present": bool((observation.get("metrics") or {}).get("official_account_participation") or event.get("official")),
                "source_confirmation_status": "observed_official_source" if event.get("official") else "unverified",
                "dominant_narrative": interpretation.get("dominant_narrative"), "dissent": interpretation.get("strongest_dissent"),
                "misconception": interpretation.get("common_misconception"), "posts": observation.get("posts") or [],
                "summary_ja": override.get("summary") or interpretation.get("topic_summary_ja"), "dominant_narrative_ja": override.get("dominant_narrative") or interpretation.get("dominant_narrative_ja"),
                "dissent_ja": override.get("dissent") or interpretation.get("strongest_dissent_ja"), "misconception_ja": override.get("misconception") or interpretation.get("common_misconception_ja"),
                "updated_at": observation.get("observed_at")})
        if topics:
            return {"run_id": run_id, "observed_at": topics[0].get("updated_at"), "generated_at": run.get("timestamp"), "topics": topics}
    path = xai_dir / "radar_cache.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"topics": []}
    except (OSError, json.JSONDecodeError):
        return {"topics": []}


def _category(event: dict) -> str:
    values = " ".join(str(x) for x in [event.get("event_type"), event.get("title"), *(event.get("tickers") or []), *(event.get("currencies") or [])]).lower()
    if any(x in values for x in ("usd", "jpy", "fx", "為替")): return "FX"
    if any(x in values for x in ("semiconductor", "chip", "半導体", "ai")): return "AI / SEMICONDUCTOR"
    if any(x in values for x in ("japan", "日本", "nikkei")): return "JAPAN STOCKS"
    if any(x in values for x in ("macro", "fed", "rate", "cpi", "pce", "金利")): return "MACRO"
    return "US STOCKS"


def _translation_overrides() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "x_radar_translations.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")); return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_public(*, fixture: bool = False, write: bool = True) -> dict:
    payload = build_snapshot(_load_source(fixture=fixture)).to_dict()
    path = state_dir() / "wordpress" / "latest_public_radar.json"
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "built", "path": str(path), "snapshot": payload}


def _notify(success: bool, payload: dict, detail: str) -> None:
    try:
        from common.operations_alerts import queue_discord_log
        if success:
            message = (f"X RadarをWordPressへ同期しました snapshot={payload['snapshot_id']} "
                       f"topics={len(payload['topics'])} observed_at={payload['observed_at']} {detail}")
        else:
            message = f"X Radar WordPress同期失敗 snapshot={payload['snapshot_id']} {detail[:160]}"
        queue_discord_log("wordpress.x_radar", message, level="INFO" if success else "ERROR")
    except Exception:
        pass


def publish(*, dry_run: bool = False, fixture: bool = False, client: WordPressRadarClient | None = None) -> dict:
    payload = build_public(fixture=fixture)["snapshot"]
    queue = RadarPublishQueue()
    if dry_run:
        return {"status": "dry_run", "snapshot_id": payload["snapshot_id"], "topic_count": len(payload["topics"]), "payload": payload}
    if os.getenv("WORDPRESS_RADAR_ENABLED", "false").lower() not in TRUE:
        return {"status": "disabled", "snapshot_id": payload["snapshot_id"]}
    if os.getenv("WORDPRESS_RADAR_PUBLIC_ENABLED", "false").lower() not in TRUE:
        queue.enqueue(payload)
        return {"status": "public_disabled", "snapshot_id": payload["snapshot_id"]}
    queue.enqueue(payload)
    try:
        result = (client or WordPressRadarClient.from_env()).publish(payload)
        queue.update(payload["snapshot_id"], "published", increment=True)
        _notify(True, payload, str(result.get("url") or ""))
        return {"status": "published", "snapshot_id": payload["snapshot_id"], "result": result}
    except WordPressRadarError as exc:
        status = "retrying" if exc.retryable else "failed"
        queue.update(payload["snapshot_id"], status, error=type(exc).__name__ + ": " + str(exc), increment=True)
        _notify(False, payload, f"error={type(exc).__name__} retryable={exc.retryable}")
        return {"status": status, "snapshot_id": payload["snapshot_id"], "error_type": type(exc).__name__}


def retry_queue(*, client: WordPressRadarClient | None = None, dry_run: bool = False) -> dict:
    queue = RadarPublishQueue(); processed = []
    for row in queue.rows():
        if row.get("status") not in {"pending", "retrying", "failed"}: continue
        if dry_run:
            processed.append({"snapshot_id": row.get("snapshot_id"), "status": "would_retry"}); continue
        try:
            result = (client or WordPressRadarClient.from_env()).publish(row["payload"])
            queue.update(row["snapshot_id"], "published", increment=True)
            processed.append({"snapshot_id": row["snapshot_id"], "status": "published", "result": result})
        except WordPressRadarError as exc:
            queue.update(row["snapshot_id"], "retrying" if exc.retryable else "failed", error=str(exc), increment=True)
            processed.append({"snapshot_id": row["snapshot_id"], "status": "failed", "error_type": type(exc).__name__})
    return {"status": "completed", "processed": processed}
