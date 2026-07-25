"""xAI cost attribution and outcome funnel."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

from common.runtime import JST, state_dir
from common.xai_radar_v2 import cache_status


def _jsonl(path):
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    except (OSError, json.JSONDecodeError):
        pass
    return rows


def _cost(row: dict) -> float:
    return float(
        row.get("cost_usd")
        if row.get("cost_usd") is not None
        else row.get("reported_cost_usd")
        if row.get("reported_cost_usd") is not None
        else row.get("estimated_cost_usd") or 0
    )


def _attributed_posts(cutoff: datetime) -> list[dict]:
    try:
        value = json.loads((state_dir() / "posted_history.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    result = []
    for row in value if isinstance(value, list) else []:
        if not row.get("radar_run_id"):
            continue
        try:
            when = datetime.fromisoformat(str(row.get("posted_at")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=JST)
            if when >= cutoff:
                result.append(row)
        except (TypeError, ValueError):
            continue
    return result


def cost_breakdown(days: int = 30) -> dict:
    cutoff = datetime.now(JST) - timedelta(days=max(1, days))
    rows = []
    for row in _jsonl(state_dir() / "xai" / "api_usage.jsonl"):
        try:
            when = datetime.fromisoformat(str(row.get("timestamp")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=JST)
            if when >= cutoff:
                rows.append(row)
        except ValueError:
            continue
    successes = [r for r in rows if r.get("status") == "success" or r.get("success") is True]
    total_cost = sum(_cost(r) for r in rows)
    topics = sum(int(r.get("topics_returned", r.get("topic_count", 0)) or 0) for r in rows)
    useful = sum(int(r.get("useful_topics", 0) or 0) for r in rows)
    candidates = sum(int(r.get("news_candidates_created", 0) or 0) for r in rows)
    attributed_posts = _attributed_posts(cutoff)
    run_ids = {str(row.get("run_id")) for row in rows if row.get("run_id")}
    matched_posts = [
        row for row in attributed_posts
        if str(row.get("radar_run_id")) in run_ids
    ]
    posts = len({str(row.get("tweet_id")) for row in matched_posts if row.get("tweet_id")})
    attempts = sum(int(r.get("attempted_tool_calls", r.get("tool_calls", 0)) or 0) for r in rows)
    successful_tools = sum(int(r.get("successful_tool_calls", r.get("tool_calls", 0)) or 0) for r in successes)
    attributable = [
        r for r in successes
        if "useful_topics" in r or "news_candidates_created" in r
    ]
    unused = sum(
        1 for r in successes
        if r in attributable
        if int(r.get("useful_topics", 0) or 0) == 0
        and int(r.get("news_candidates_created", 0) or 0) == 0
    )
    return {
        "days": days, "runs": len(rows), "successful_runs": len(successes),
        "attempted_tool_calls": attempts, "successful_tool_calls": successful_tools,
        "x_search_calls": sum(int(r.get("x_search_calls", r.get("tool_calls", 0)) or 0) for r in rows),
        "cost_usd": round(total_cost, 6),
        "cost_per_run_usd": round(total_cost/len(rows), 6) if rows else None,
        "cost_per_success_usd": round(total_cost/len(successes), 6) if successes else None,
        "topics_returned": topics, "useful_topics": useful,
        "news_candidates_created": candidates, "posts_created": posts,
        "post_ids": sorted({
            str(row.get("tweet_id")) for row in matched_posts if row.get("tweet_id")
        }),
        "attribution_source": "posted_history.radar_run_id",
        "cost_per_useful_topic_usd": round(total_cost/useful, 6) if useful else None,
        "cost_per_news_candidate_usd": round(total_cost/candidates, 6) if candidates else None,
        "cost_per_post_usd": round(total_cost/posts, 6) if posts else None,
        "unused_result_rate": round(unused/len(attributable), 4) if attributable else None,
        "cache": cache_status(days),
        "failure_stages": {
            str(key): sum(1 for row in rows if row.get("failure_stage") == key)
            for key in sorted({row.get("failure_stage") for row in rows if row.get("failure_stage")})
        },
        "error_types": {
            str(key): sum(1 for row in rows if row.get("error_type") == key)
            for key in sorted({row.get("error_type") for row in rows if row.get("error_type")})
        },
    }


def funnel(days: int = 30) -> dict:
    value = cost_breakdown(days)
    return {
        "xai_runs": value["runs"],
        "topic_runs": sum(value[key] > 0 for key in ("topics_returned",)),
        "topics_returned": value["topics_returned"],
        "useful_topics": value["useful_topics"],
        "news_candidates": value["news_candidates_created"],
        "post_candidates": value["posts_created"],
        "actual_posts": value["posts_created"],
        "metrics_1h": 0,
        "metrics_24h": 0,
        "note": "Missing historical attribution remains null/zero; it is not inferred.",
    }
