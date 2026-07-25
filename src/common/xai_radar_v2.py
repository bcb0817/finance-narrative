from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI
from common.runtime import state_dir

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://api.x.ai/v1"


def _path(name: str) -> Path:
    data = state_dir() / "xai"
    data.mkdir(parents=True, exist_ok=True)
    return data / name


def _now() -> datetime:
    return datetime.now(JST)


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return rows


def _append(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _atomic(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(item, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _cache_key() -> str:
    value = {
        "version": 2,
        "watch": os.getenv("XAI_RADAR_WATCH_HANDLES", ""),
        "topics": _env_int("XAI_RADAR_MAX_TOPICS", 5),
        "posts": _env_int("XAI_RADAR_MAX_POSTS_PER_TOPIC", 2),
        "accounts": _env_int("XAI_RADAR_MAX_ACCOUNTS_PER_TOPIC", 2),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def _event_mode(now: datetime | None = None) -> bool:
    current = now or _now()
    if _env_bool("XAI_EVENT_MODE"):
        return True
    try:
        return datetime.fromisoformat(_path("event_mode_until.txt").read_text(encoding="utf-8").strip()) > current
    except (OSError, ValueError):
        return False


def daily_limit(now: datetime | None = None) -> int:
    key = "XAI_EVENT_MAX_SEARCH_CALLS_PER_DAY" if _event_mode(now) else "XAI_MAX_SEARCH_CALLS_PER_DAY"
    return max(1, _env_int(key, 4 if _event_mode(now) else 2))


def _cache() -> dict:
    value = _json(_path("radar_cache.json"), {})
    return value if isinstance(value, dict) else {}


def _valid(value: dict, now: datetime | None = None) -> bool:
    if value.get("cache_key") not in {None, _cache_key()}:
        return False
    try:
        return bool(value.get("topics")) and datetime.fromisoformat(str(value["expires_at"])) > (now or _now())
    except (KeyError, TypeError, ValueError):
        return False


def load_cache(now: datetime | None = None, record_hit: bool = False) -> list[dict]:
    value = _cache()
    hit = _valid(value, now)
    if record_hit:
        _append(
            _path("cache_events.jsonl"),
            {
                "timestamp": (now or _now()).isoformat(),
                "event": "hit" if hit else "miss",
                "cache_key": _cache_key(),
                "source_run_id": value.get("run_id"),
            },
        )
    return list(value.get("topics") or []) if hit else []


def cache_status(days: int = 7) -> dict:
    value = _cache()
    cutoff = _now() - timedelta(days=max(1, days))
    events = []
    for row in _jsonl(_path("cache_events.jsonl")):
        try:
            if datetime.fromisoformat(str(row.get("timestamp"))) >= cutoff:
                events.append(row)
        except (TypeError, ValueError):
            pass
    hits = sum(row.get("event") == "hit" for row in events)
    misses = sum(row.get("event") == "miss" for row in events)
    return {
        "valid": _valid(value),
        "topic_count": len(value.get("topics") or []),
        "generated_at": value.get("generated_at"),
        "expires_at": value.get("expires_at"),
        "cache_key": value.get("cache_key"),
        "expected_cache_key": _cache_key(),
        "source_run_id": value.get("run_id"),
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
    }


def _cost(row: dict) -> float:
    if row.get("reported_cost_usd") is not None:
        return float(row["reported_cost_usd"])
    return float(row.get("estimated_cost_usd") or 0)


def usage_summary(now: datetime | None = None, days: int = 31) -> dict:
    current = now or _now()
    cutoff = current - timedelta(days=max(1, days))
    rows = []
    for row in _jsonl(_path("api_usage.jsonl")):
        try:
            if datetime.fromisoformat(str(row.get("timestamp"))) >= cutoff:
                rows.append(row)
        except (TypeError, ValueError):
            pass
    today = [row for row in rows if str(row.get("timestamp", "")).startswith(current.date().isoformat())]
    successes = [row for row in rows if row.get("status") == "success" or row.get("success") is True]
    total = sum(_cost(row) for row in rows)
    effective=round(total,6)
    return {
        "calls": len(rows),
        "monthly_calls":len(rows),
        "daily_calls": len(today),
        "successful_calls": len(successes),
        "daily_limit": daily_limit(current),
        "reported_cost_usd": round(sum(float(row["reported_cost_usd"]) for row in rows if row.get("reported_cost_usd") is not None), 6),
        "estimated_only_cost_usd": round(sum(float(row.get("estimated_cost_usd") or 0) for row in rows if row.get("reported_cost_usd") is None), 6),
        "total_effective_cost_usd": effective,
        "spent_usd":effective,
        "average_cost_per_success_usd": round(total / len(successes), 6) if successes else None,
        "budget_usd":_env_float("XAI_MONTHLY_BUDGET_USD",5.0),
        "remaining_usd":round(max(0.0,_env_float("XAI_MONTHLY_BUDGET_USD",5.0)-total),6),
    }


def _can_call(now: datetime | None = None) -> tuple[bool, str]:
    if not _env_bool("XAI_ENABLED", True):
        return False, "disabled"
    if not os.getenv("XAI_API_KEY"):
        return False, "missing_api_key"
    usage = usage_summary(now)
    if usage["daily_calls"] >= usage["daily_limit"]:
        return False, "daily_limit"
    if usage["total_effective_cost_usd"] >= _env_float("XAI_MONTHLY_BUDGET_USD", 5.0):
        return False, "monthly_budget"
    return True, ""


def _parse_topics(raw: str, now: datetime | None = None) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    items = value.get("topics", []) if isinstance(value, dict) else []
    result = []
    current = now or _now()
    for item in items[: max(1, _env_int("XAI_RADAR_MAX_TOPICS", 5))]:
        if not isinstance(item, dict) or not item.get("topic"):
            continue
        count_60=float(item.get("mention_count_60m") or item.get("observed_mention_count") or item.get("mention_count") or 0)
        count_6h=float(item.get("mention_count_6h") or 0)
        acceleration=count_60/(count_6h/6.0) if count_6h>0 else count_60
        result.append(
            {
                "topic": str(item["topic"])[:120],
                "tickers": [str(x).upper()[:16] for x in item.get("tickers", [])[:5]],
                "category": str(item.get("category") or "other")[:40],
                "summary": str(item.get("summary") or "")[:360],
                "observed_mention_count": int(item.get("observed_mention_count") or 0),
                "mention_count": int(item.get("observed_mention_count") or 0),
                "velocity_score": max(0.0, min(10.0, float(item.get("velocity_score") or min(10,count_60)))),
                "acceleration_score": max(0.0, min(10.0, float(item.get("acceleration_score") if item.get("acceleration_score") is not None else acceleration))),
                "representative_posts": list(item.get("representative_posts") or [])[:2],
                "representative_accounts": [str(x)[:80] for x in (item.get("representative_accounts") or [])[:2]],
                "source_reliability": str(item.get("source_reliability") or "unknown"),
                "primary_source_available": bool(item.get("primary_source_available")),
                "source_confirmation": str(item.get("source_confirmation") or "x_discussion"),
                "news_confirmation_status": "unverified",
                "detected_at": current.isoformat(),
            }
        )
    return result


def _schema() -> dict:
    topic = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "topic", "tickers", "category", "summary", "observed_mention_count",
            "velocity_score", "acceleration_score", "representative_posts",
            "representative_accounts", "source_reliability",
            "primary_source_available", "source_confirmation",
        ],
        "properties": {
            "topic": {"type": "string"},
            "tickers": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
            "category": {"type": "string"},
            "summary": {"type": "string"},
            "observed_mention_count": {"type": "integer", "minimum": 0},
            "velocity_score": {"type": "number", "minimum": 0, "maximum": 10},
            "acceleration_score": {"type": "number", "minimum": 0, "maximum": 10},
            "representative_posts": {"type": "array", "maxItems": 2, "items": {
                "type": "object","additionalProperties":False,
                "required":["post_id","url","username","excerpt"],
                "properties":{"post_id":{"type":"string"},"url":{"type":"string"},
                              "username":{"type":"string"},"excerpt":{"type":"string"}},
            }},
            "representative_accounts": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
            "source_reliability": {"type": "string"},
            "primary_source_available": {"type": "boolean"},
            "source_confirmation": {"type": "string"},
        },
    }
    return {"format": {"type": "json_schema", "name": "x_topic_radar", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["topics"],
        "properties": {"topics": {"type": "array", "maxItems": 5, "items": topic}},
    }}}


def _usage(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    ticks = int(getattr(usage, "cost_in_usd_ticks", 0) or 0)
    tools = int(getattr(usage, "num_server_side_tools_used", 0) or getattr(usage, "tool_calls", 0) or 0)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached_input_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    output_details = getattr(usage, "output_tokens_details", None)
    reasoning_tokens = int(getattr(output_details, "reasoning_tokens", 0) or 0)
    estimate = tools * _env_float("XAI_SEARCH_TOOL_COST", 0.005)
    return {
        "tool_calls": tools,
        "attempted_tool_calls": tools,
        "successful_tool_calls": tools,
        "x_search_calls": tools,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(estimate, 6) if not ticks else 0.0,
        "reported_cost_usd": ticks / 1e10 if ticks else None,
    }


def refresh(*, client: Any = None, now: datetime | None = None, force: bool = False) -> dict:
    current = now or _now()
    allowed, reason = _can_call(current)
    if not allowed:
        return {"status": "skipped", "reason": reason, "topics": load_cache(current)}
    if not force and client is None:
        topics = load_cache(current, record_hit=True)
        if topics:
            return {"status": "cached", "topics": topics, "cache": cache_status()}
    run_id = uuid.uuid4().hex
    model = os.getenv("XAI_MODEL", "grok-4-1-fast")
    response = None
    started = time.perf_counter()
    try:
        api = client or OpenAI(api_key=os.environ["XAI_API_KEY"], base_url=os.getenv("XAI_BASE_URL", BASE_URL), max_retries=0)
        handles = [x.strip().lstrip("@") for x in os.getenv("XAI_RADAR_WATCH_HANDLES", "").split(",") if x.strip()][:5]
        tool: dict[str, Any] = {"type": "x_search", "enable_image_understanding": False, "enable_video_understanding": False}
        if handles:
            tool["allowed_x_handles"] = handles
        prompt = (
            "直近60分と過去6時間を比較し、米国株・AI・半導体・大型テックで議論が急加速したテーマだけを調査。"
            "検索は必要最小限、同じ検索を反復しない。最大5テーマ、各テーマ代表投稿2件、主要アカウント2件。"
            "画像・動画・全文スレッドは取得しない。事実と観測を分離し、短い日本語でJSONスキーマに厳密に従う。"
        )
        response = api.responses.create(
            model=model,
            input=prompt,
            tools=[tool],
            text=_schema(),
            max_output_tokens=_env_int("XAI_MAX_OUTPUT_TOKENS", 1400),
        )
        topics = _parse_topics(str(response.output_text), current)
        usage = _usage(response)
        effective = usage["reported_cost_usd"] if usage.get("reported_cost_usd") is not None else usage["estimated_cost_usd"]
        allocation = round(float(effective) / len(topics), 6) if topics else 0.0
        for topic in topics:
            topic["radar_run_id"] = run_id
            topic["xai_cost_attribution_usd"] = allocation
        ttl = max(1, _env_int("XAI_CACHE_TTL_MINUTES", 60))
        _atomic(_path("radar_cache.json"), {
            "cache_key": _cache_key(), "run_id": run_id, "generated_at": current.isoformat(),
            "expires_at": (current + timedelta(minutes=ttl)).isoformat(), "topics": topics,
        })
        for topic in topics:
            _append(_path("topic_radar.jsonl"), {"timestamp": current.isoformat(), **topic})
        row = {"timestamp": current.isoformat(), "run_id": run_id, "model": model, "operation": "x_topic_radar",
               "status": "success", "success": True, "topic_count": len(topics),
               "request_id": str(getattr(response, "id", "") or ""),
               "topics_returned": len(topics),
               "useful_topics": sum(bool(topic.get("primary_source_available")) for topic in topics),
               "news_candidates_created": 0, "posts_created": 0, "post_ids": [],
               "cache_hit": False, "failure_stage": None, "error_type": None,
               "latency_ms": round((time.perf_counter() - started) * 1000), **usage}
        _append(_path("api_usage.jsonl"), row)
        return {"status": "ok", "run_id": run_id, "topics": topics, "usage": row}
    except Exception as exc:
        row = {"timestamp": current.isoformat(), "run_id": run_id, "model": model, "operation": "x_topic_radar",
               "status": "failed", "success": False, "error_type": type(exc).__name__,
               "request_id": str(getattr(response, "id", "") or ""),
               "topics_returned": 0, "useful_topics": 0,
               "news_candidates_created": 0, "posts_created": 0, "post_ids": [],
               "cache_hit": False, "failure_stage": "request_or_parse",
               "latency_ms": round((time.perf_counter() - started) * 1000), **_usage(response)}
        _append(_path("api_usage.jsonl"), row)
        return {"status": "fallback", "reason": type(exc).__name__, "topics": list(_cache().get("topics") or [])}


def topic_velocity(topic: str | int | float = "", ticker: str | int | float = "") -> dict | None:
    if isinstance(topic,(int,float)) and isinstance(ticker,(int,float)):
        base=max(float(ticker)/6.0,1.0)
        acceleration=float(topic)/base
        return {"velocity_60m":float(topic),"observed_velocity_60m":float(topic),
                "velocity_6h":float(ticker),"observed_velocity_6h":float(ticker),
                "acceleration_score":acceleration,"observed_acceleration_score":acceleration}
    needle = topic.lower().strip()
    symbol = ticker.upper().strip()
    candidates = load_cache(record_hit=True)
    matches = [
        item for item in candidates
        if (symbol and symbol in [str(x).upper() for x in item.get("tickers", [])])
        or (needle and (needle in str(item.get("topic", "")).lower() or str(item.get("topic", "")).lower() in needle))
    ]
    return max(matches, key=lambda item: float(item.get("acceleration_score") or 0)) if matches else None


def radar_plan(now: datetime | None = None) -> dict:
    current = now or _now()
    usage = usage_summary(current)
    return {
        "mode": "event" if _event_mode(current) else "normal",
        "windows_jst": ["21:00", "22:30"],
        "calls_used": usage["daily_calls"],
        "calls_remaining": max(0, daily_limit(current) - usage["daily_calls"]),
        "daily_limit": daily_limit(current),
        "cache_ttl_minutes": _env_int("XAI_CACHE_TTL_MINUTES", 60),
        "max_topics": _env_int("XAI_RADAR_MAX_TOPICS", 5),
        "max_posts_per_topic": _env_int("XAI_RADAR_MAX_POSTS_PER_TOPIC", 2),
        "max_accounts_per_topic": _env_int("XAI_RADAR_MAX_ACCOUNTS_PER_TOPIC", 2),
    }


def cost_report(days: int = 30) -> dict:
    result = usage_summary(days=days)
    result["cache"] = cache_status(days)
    result["target_cost_per_success_usd"] = _env_float("XAI_TARGET_COST_PER_CALL_USD", 0.10)
    average = result["average_cost_per_success_usd"]
    result["target_met"] = average is not None and average <= result["target_cost_per_success_usd"]
    return result


def status() -> dict:
    allowed, reason = _can_call()
    return {
        "enabled": _env_bool("XAI_ENABLED", True),
        "api_key_configured": bool(os.getenv("XAI_API_KEY")),
        "model": os.getenv("XAI_MODEL", "grok-4-1-fast"),
        "ready": allowed,
        "reason": reason,
        "plan": radar_plan(),
        "cache": cache_status(),
        "usage": usage_summary(),
    }
