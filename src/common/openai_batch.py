"""OpenAI Batch API support for delayed analysis jobs."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openai import OpenAI

try:
    from runtime import JST, state_dir
    from openai_config import OpenAIRole, env_bool, model_for, validate_models
except ImportError:  # pragma: no cover
    from common.runtime import JST, state_dir
    from common.openai_config import OpenAIRole, env_bool, model_for, validate_models

ENDPOINT = "/v1/responses"
TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}


class BatchConfigurationError(RuntimeError): pass
class BatchDisabledError(RuntimeError): pass


def batch_dir(create: bool = False) -> Path:
    path = state_dir() / "openai" / "batch"
    if create:
        (path / "requests").mkdir(parents=True, exist_ok=True)
        (path / "results").mkdir(parents=True, exist_ok=True)
    return path


def registry_path() -> Path: return batch_dir() / "registry.jsonl"


def _value(obj: Any, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _append(event: dict[str, Any]) -> None:
    batch_dir(True)
    row = {**event, "timestamp": datetime.now(JST).isoformat()}
    with registry_path().open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def events() -> list[dict[str, Any]]:
    if not registry_path().exists(): return []
    rows = []
    for line in registry_path().read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict): rows.append(row)
        except json.JSONDecodeError: pass
    return rows


def latest_batches() -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in events():
        batch_id = str(row.get("batch_id", ""))
        if batch_id: latest[batch_id] = {**latest.get(batch_id, {}), **row}
    return latest


def config_status() -> dict[str, Any]:
    latest = latest_batches()
    return {"enabled": env_bool("OPENAI_BATCH_ENABLED", False),
            "model": model_for(OpenAIRole.ANALYZE), "endpoint": ENDPOINT,
            "completion_window": os.getenv("OPENAI_BATCH_COMPLETION_WINDOW", "24h"),
            "max_requests": int(os.getenv("OPENAI_BATCH_MAX_REQUESTS", "100")),
            "max_submissions_per_day": int(os.getenv("OPENAI_BATCH_MAX_SUBMISSIONS_PER_DAY", "2")),
            "tracked": len(latest),
            "active": sum(row.get("status") not in TERMINAL_STATUSES for row in latest.values()),
            "registry": str(registry_path())}


def build_request_file(jobs: Iterable[dict[str, Any]], *, operation: str,
                       output_path: Path | None = None) -> Path:
    errors = validate_models()
    if errors: raise BatchConfigurationError("; ".join(errors))
    rows = list(jobs); maximum = int(os.getenv("OPENAI_BATCH_MAX_REQUESTS", "100"))
    if not 1 <= len(rows) <= maximum:
        raise ValueError(f"Batch request count must be between 1 and {maximum}")
    seen: set[str] = set(); payloads = []
    for job in rows:
        custom_id, prompt = str(job.get("custom_id", "")).strip(), str(job.get("input", "")).strip()
        if not custom_id or not prompt: raise ValueError("Each job requires custom_id and input")
        if custom_id in seen: raise ValueError(f"Duplicate custom_id: {custom_id}")
        seen.add(custom_id)
        body: dict[str, Any] = {"model": model_for(OpenAIRole.ANALYZE), "input": prompt,
                               "max_output_tokens": int(job.get("max_output_tokens", 1600)), "store": False}
        if job.get("schema"):
            body["text"] = {"format": {"type": "json_schema", "name": "result", "strict": True,
                                         "schema": job["schema"]}}
        payloads.append({"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body})
    if output_path is None:
        batch_dir(True)
        output_path = batch_dir() / "requests" / f"{operation}_{datetime.now(JST):%Y%m%d_%H%M%S}.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in payloads), encoding="utf-8")
    return output_path


def validate_request_file(path: Path) -> int:
    seen: set[str] = set(); count = 0; expected_model = model_for(OpenAIRole.ANALYZE)
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try: row = json.loads(line)
        except json.JSONDecodeError as exc: raise ValueError(f"Invalid JSON on line {number}") from exc
        custom_id = str(row.get("custom_id", ""))
        if row.get("method") != "POST" or row.get("url") != ENDPOINT:
            raise ValueError(f"Unsupported request on line {number}")
        if not custom_id or custom_id in seen: raise ValueError(f"Missing or duplicate custom_id on line {number}")
        if row.get("body", {}).get("model") != expected_model: raise ValueError(f"Unexpected model on line {number}")
        seen.add(custom_id); count += 1
    maximum = int(os.getenv("OPENAI_BATCH_MAX_REQUESTS", "100"))
    if not 1 <= count <= maximum: raise ValueError("Batch request count is outside configured bounds")
    return count


def _client(client=None):
    if client is not None: return client
    if not os.getenv("OPENAI_API_KEY"): raise BatchConfigurationError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def submit(path: Path, *, operation: str, client=None) -> dict[str, Any]:
    if not env_bool("OPENAI_BATCH_ENABLED", False): raise BatchDisabledError("OPENAI_BATCH_ENABLED=false")
    if os.getenv("OPENAI_BATCH_COMPLETION_WINDOW", "24h") != "24h":
        raise BatchConfigurationError("Only the 24h completion window is supported")
    count = validate_request_file(path); digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if any(r.get("event") == "submitted" and r.get("request_sha256") == digest for r in events()):
        raise ValueError("This request file was already submitted")
    today = datetime.now(JST).date().isoformat()
    used = sum(r.get("event") == "submitted" and str(r.get("timestamp", "")).startswith(today) for r in events())
    if used >= int(os.getenv("OPENAI_BATCH_MAX_SUBMISSIONS_PER_DAY", "2")):
        raise ValueError("OpenAI Batch daily submission limit reached")
    api = _client(client)
    with path.open("rb") as fh: uploaded = api.files.create(file=fh, purpose="batch")
    batch = api.batches.create(input_file_id=_value(uploaded, "id"), endpoint=ENDPOINT,
                               completion_window="24h", metadata={"operation": operation[:64]})
    result = {"event": "submitted", "batch_id": _value(batch, "id"),
              "status": _value(batch, "status", "validating"), "operation": operation,
              "request_count": count, "request_sha256": digest, "input_file_id": _value(uploaded, "id")}
    _append(result); return result


def refresh(batch_id: str, *, client=None) -> dict[str, Any]:
    batch = _client(client).batches.retrieve(batch_id)
    result = {"event": "status", "batch_id": batch_id, "status": _value(batch, "status"),
              "output_file_id": _value(batch, "output_file_id"), "error_file_id": _value(batch, "error_file_id")}
    _append(result); return result


def _content_bytes(content: Any) -> bytes:
    value = _value(content, "content", content)
    if isinstance(value, bytes): return value
    if isinstance(value, str): return value.encode("utf-8")
    if hasattr(content, "read"):
        value = content.read(); return value if isinstance(value, bytes) else str(value).encode("utf-8")
    raise TypeError("Unsupported file content response")


def collect(batch_id: str, *, client=None) -> dict[str, Any]:
    api = _client(client); batch = api.batches.retrieve(batch_id); status = _value(batch, "status")
    if status != "completed":
        result = {"event": "status", "batch_id": batch_id, "status": status}; _append(result); return result
    output_file_id = _value(batch, "output_file_id")
    if not output_file_id: raise RuntimeError("Completed batch has no output file")
    target = batch_dir(True) / "results" / f"{batch_id}.jsonl"
    target.write_bytes(_content_bytes(api.files.content(output_file_id)))
    result = {"event": "collected", "batch_id": batch_id, "status": status,
              "output_file_id": output_file_id, "result_path": str(target)}
    _append(result); return result


def cancel(batch_id: str, *, client=None) -> dict[str, Any]:
    batch = _client(client).batches.cancel(batch_id)
    result = {"event": "cancel_requested", "batch_id": batch_id,
              "status": _value(batch, "status", "cancelling")}
    _append(result); return result
