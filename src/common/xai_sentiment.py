"""One-shot X market sentiment estimate backed by Grok X Search."""
from __future__ import annotations

import json
import hashlib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from common.runtime import JST, load_env, state_dir


logger = logging.getLogger(__name__)
BASE_URL = "https://api.x.ai/v1"
TRUE_VALUES = {"1", "true", "yes", "on"}
MOMENTUM = {"rising", "stable", "falling", "unclear"}
STANCES = {"bullish", "neutral", "bearish"}


def _enabled() -> bool:
    return os.getenv("XAI_SENTIMENT_ENABLED", "false").strip().lower() in TRUE_VALUES


def sentiment_schema() -> dict[str, Any]:
    narrative = {
        "type": "object",
        "additionalProperties": False,
        "required": ["theme", "stance", "importance"],
        "properties": {
            "theme": {"type": "string"},
            "stance": {"type": "string", "enum": sorted(STANCES)},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "topic", "sentiment", "sentiment_score", "momentum", "confidence",
            "dominant_narratives", "contrarian_view", "summary", "post_angle",
        ],
        "properties": {
            "topic": {"type": "string"},
            "sentiment": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bullish", "neutral", "bearish"],
                "properties": {
                    name: {"type": "integer", "minimum": 0, "maximum": 100}
                    for name in ("bullish", "neutral", "bearish")
                },
            },
            "sentiment_score": {"type": "integer", "minimum": -100, "maximum": 100},
            "momentum": {"type": "string", "enum": sorted(MOMENTUM)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "dominant_narratives": {
                "type": "array", "maxItems": 5, "items": narrative,
            },
            "contrarian_view": {"type": "string"},
            "summary": {"type": "string"},
            "post_angle": {"type": "string"},
        },
    }
    return {
        "format": {
            "type": "json_schema", "name": "x_market_sentiment",
            "strict": True, "schema": schema,
        }
    }


def validate_sentiment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("sentiment response must be an object")
    required = {
        "topic", "sentiment", "sentiment_score", "momentum", "confidence",
        "dominant_narratives", "contrarian_view", "summary", "post_angle",
    }
    if set(value) != required:
        raise ValueError("sentiment response fields do not match schema")
    shares = value.get("sentiment")
    if not isinstance(shares, dict) or set(shares) != STANCES:
        raise ValueError("sentiment shares are invalid")
    if any(type(shares[name]) is not int or not 0 <= shares[name] <= 100 for name in STANCES):
        raise ValueError("sentiment shares must be integers from 0 to 100")
    if sum(shares.values()) != 100:
        raise ValueError("sentiment shares must total 100")
    score = value.get("sentiment_score")
    if type(score) is not int or not -100 <= score <= 100:
        raise ValueError("sentiment_score is out of range")
    if value.get("momentum") not in MOMENTUM:
        raise ValueError("momentum is invalid")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence is out of range")
    narratives = value.get("dominant_narratives")
    if not isinstance(narratives, list) or len(narratives) > 5:
        raise ValueError("dominant_narratives is invalid")
    for row in narratives:
        if not isinstance(row, dict) or set(row) != {"theme", "stance", "importance"}:
            raise ValueError("dominant narrative fields are invalid")
        if row["stance"] not in STANCES:
            raise ValueError("dominant narrative stance is invalid")
        importance = row["importance"]
        if isinstance(importance, bool) or not isinstance(importance, (int, float)) or not 0 <= importance <= 1:
            raise ValueError("dominant narrative importance is out of range")
    for name in ("topic", "contrarian_view", "summary", "post_angle"):
        if not isinstance(value.get(name), str):
            raise ValueError(f"{name} must be a string")
    return value


def _instructions() -> str:
    return """You are an X market sentiment intelligence agent.
Use X Search extensively and analyze recent, relevant discussion about the supplied
financial news, company, ticker, macro event, or market topic. Distinguish factual
information, market interpretation, speculation, and emotional retail reaction.
Reduce duplicate posts, reposted narratives, spam, engagement bait, irrelevant
mentions, and promotional content. Identify dominant and meaningful contrarian
narratives. Look for headline-versus-underlying reaction, retail-versus-professional
divergence, price interpretation, expectations, valuation, positioning, guidance,
macro implications, and second-order effects.

This is an estimate from relevant discussion examined through X Search, never a
statistically representative poll of all X users. Ensure bullish + neutral + bearish
equals exactly 100. post_angle must be a non-obvious publishing angle, not a news
summary. Do not give investment advice. Return only the strict JSON schema."""


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())[:300]
    secret = os.getenv("XAI_API_KEY", "")
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _cache_path() -> Path:
    return state_dir() / "xai" / "sentiment_cache.json"


def _cache_key(topic: str, context: str, symbols: list[str]) -> str:
    raw = json.dumps(
        {"topic": topic.lower(), "context": context, "symbols": symbols},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(key: str) -> dict[str, Any] | None:
    try:
        row = json.loads(_cache_path().read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(str(row.get("expires_at") or ""))
        if row.get("key") == key and expires > datetime.now(JST):
            return validate_sentiment(row.get("result"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _save_cache(key: str, result: dict[str, Any]) -> None:
    ttl = max(1, int(os.getenv("XAI_SENTIMENT_CACHE_TTL_MINUTES", "60") or 60))
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "key": key,
        "expires_at": (datetime.now(JST) + timedelta(minutes=ttl)).isoformat(),
        "result": result,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _record_usage(response: Any, topic: str) -> None:
    try:
        from common.xai_radar_v2 import _append, _path, _usage
        usage = _usage(response)
        _append(_path("api_usage.jsonl"), {
            "timestamp": datetime.now(JST).isoformat(),
            "operation": "x_sentiment",
            "model": os.getenv("XAI_MODEL", "grok-4.6"),
            "topic_hash": hashlib.sha256(topic.encode("utf-8")).hexdigest()[:16],
            "success": True,
            **usage,
        })
    except Exception as exc:
        logger.warning("[XAI] Sentiment usage recording failed: %s", type(exc).__name__)


def analyze_x_sentiment(
    topic: str,
    context: str = "",
    symbols: list[str] | None = None,
    *,
    client: Any = None,
) -> dict[str, Any] | None:
    """Return one X sentiment estimate, or ``None`` on any optional-layer failure."""
    from common.collection import enabled
    if enabled():
        # Paid searches must use event-led reservations, not this fail-open path.
        return None
    load_env()
    topic = " ".join(str(topic or "").split())
    if not topic:
        logger.warning("[XAI] Sentiment analysis failed: empty topic")
        return None
    if not _enabled():
        logger.info("[XAI] Sentiment analysis disabled")
        return None
    if not os.getenv("XAI_API_KEY") and client is None:
        logger.warning("[XAI] Sentiment analysis failed: API key missing")
        return None

    normalized_symbols = [str(symbol).upper() for symbol in (symbols or [])[:10]]
    cache_key = _cache_key(topic, str(context or "")[:6000], normalized_symbols)
    if client is None:
        cached = _cached(cache_key)
        if cached is not None:
            logger.info("[XAI] Sentiment cache hit: %s", topic[:300])
            return cached
        try:
            from common.xai_radar_v2 import _can_call
            allowed, reason = _can_call(datetime.now(JST))
        except Exception as exc:
            logger.warning("[XAI] Sentiment budget check failed open: %s", type(exc).__name__)
            allowed, reason = True, "budget_check_unavailable"
        if not allowed:
            logger.info("[XAI] Sentiment skipped: %s", reason)
            return None

    logger.info("[XAI] Sentiment analysis started")
    logger.info("[XAI] Topic: %s", topic[:300])
    try:
        if client is None:
            from openai import OpenAI
            api = OpenAI(
                api_key=os.environ["XAI_API_KEY"],
                base_url=os.getenv("XAI_BASE_URL", BASE_URL),
                max_retries=0,
                timeout=float(os.getenv("XAI_SENTIMENT_TIMEOUT_SECONDS", "120") or 120),
            )
        else:
            api = client
        payload = {
            "topic": topic,
            "context": str(context or "")[:6000],
            "symbols": normalized_symbols,
        }
        response = api.responses.create(
            model=os.getenv("XAI_MODEL", "grok-4.6"),
            instructions=_instructions(),
            input=json.dumps(payload, ensure_ascii=False),
            tools=[{"type": "x_search"}],
            text=sentiment_schema(),
            parallel_tool_calls=False,
            max_tool_calls=1,
            max_output_tokens=int(os.getenv("XAI_SENTIMENT_MAX_OUTPUT_TOKENS", "1200") or 1200),
            store=False,
        )
        result = validate_sentiment(json.loads(str(response.output_text)))
        if client is None:
            _save_cache(cache_key, result)
            _record_usage(response, topic)
        shares = result["sentiment"]
        logger.info("[XAI] Sentiment score: %s", result["sentiment_score"])
        logger.info(
            "[XAI] Bullish / Neutral / Bearish: %s / %s / %s",
            shares["bullish"], shares["neutral"], shares["bearish"],
        )
        logger.info("[XAI] Confidence: %s", result["confidence"])
        logger.info("[XAI] Analysis completed")
        return result
    except Exception as exc:
        logger.warning("[XAI] Sentiment analysis failed: %s", _safe_error(exc))
        return None
