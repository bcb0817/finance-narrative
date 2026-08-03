"""Write a secret-free xAI utilization baseline for before/after comparisons."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.json_utils import make_json_safe
from common.ops_quality import xai_roi_report
from common.runtime import JST, load_env, output_dir
from common.xai_quality import cost_breakdown, funnel
from common.xai_radar import cache_status, status
from common.xai_social_intelligence import social_report


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _latency_and_failures() -> dict:
    path = ROOT / "data" / "xai" / "api_usage.jsonl"
    rows: list[dict] = []
    corrupt = 0
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    rows.append(value)
            except json.JSONDecodeError:
                corrupt += 1
    latency = [
        float(row["latency_ms"])
        for row in rows
        if row.get("latency_ms") is not None
    ]
    failures: dict[str, int] = {}
    for row in rows:
        if row.get("status") == "success" or row.get("success") is True:
            continue
        key = str(row.get("error_type") or row.get("failure_stage") or "unknown")
        failures[key] = failures.get(key, 0) + 1
    return {
        "rows": len(rows),
        "corrupt_rows": corrupt,
        "average_latency_ms": round(statistics.mean(latency), 1) if latency else None,
        "median_latency_ms": round(statistics.median(latency), 1) if latency else None,
        "failure_reasons": failures,
    }


def build(label: str, days: int) -> dict:
    load_env()
    radar = status()
    costs = cost_breakdown(days)
    result = {
        "schema_version": 1,
        "label": label,
        "generated_at": datetime.now(JST).isoformat(),
        "workspace": str(ROOT),
        "git_commit": _git_commit(),
        "architecture": (
            "event_led_x_social_intelligence"
            if label == "after" else "broad_topic_radar"
        ),
        "radar_status": {
            "enabled": radar.get("enabled"),
            "x_search_enabled": radar.get("x_search_enabled"),
            "model": radar.get("model"),
            "daily_limit": radar.get("usage", {}).get("daily_limit"),
            "monthly_budget_usd": radar.get("usage", {}).get("budget_usd"),
            "monthly_usage_usd": radar.get("usage", {}).get("spent_usd"),
            "remaining_usd": radar.get("usage", {}).get("remaining_usd"),
        },
        "cost_breakdown": costs,
        "funnel": funnel(days),
        "roi": xai_roi_report(days),
        "cache": cache_status(days),
        "latency_and_failures": _latency_and_failures(),
        "social_intelligence": social_report(days) if label == "after" else None,
        "limitations": [
            "Historical search-to-post attribution is not inferred.",
            "Observed X Search results are samples, not complete X totals.",
            "No secret values are included.",
        ],
    }
    return make_json_safe(result)


def _markdown(payload: dict) -> str:
    cost = payload["cost_breakdown"]
    radar = payload["radar_status"]
    latency = payload["latency_and_failures"]
    return "\n".join(
        [
            f"# xAI Maximum Utilization Baseline ({payload['label']})",
            "",
            f"- Generated: {payload['generated_at']}",
            f"- Commit: `{payload['git_commit']}`",
            f"- Architecture: `{payload['architecture']}`",
            f"- Model: `{radar.get('model')}`",
            f"- Budget: ${radar.get('monthly_budget_usd')} / month",
            f"- Runs: {cost.get('runs')}",
            f"- Successful runs: {cost.get('successful_runs')}",
            f"- Total cost: ${cost.get('cost_usd')}",
            f"- Cost/run: ${cost.get('cost_per_run_usd')}",
            f"- Cost/success: ${cost.get('cost_per_success_usd')}",
            f"- Attempted X Search calls: {cost.get('attempted_tool_calls')}",
            f"- Topics/useful: {cost.get('topics_returned')}/{cost.get('useful_topics')}",
            f"- News candidates/posts: {cost.get('news_candidates_created')}/{cost.get('posts_created')}",
            f"- Cache hit rate: {cost.get('cache', {}).get('hit_rate')}",
            f"- Average latency: {latency.get('average_latency_ms')} ms",
            f"- Failure reasons: {json.dumps(latency.get('failure_reasons'), ensure_ascii=False)}",
            "",
            "## Safety",
            "",
            "- No API key, Authorization header, or Discord webhook value is included.",
            "- Observed X Search results are not represented as totals for all of X.",
            "- Missing historical attribution remains null/zero.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=("before", "after"), required=True)
    parser.add_argument("--days", type=int, default=3650)
    parser.add_argument("--timestamp", default="")
    args = parser.parse_args()
    stamp = args.timestamp or datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    payload = build(args.label, max(1, args.days))
    folder = output_dir("baseline")
    stem = f"xai_maximum_utilization_{stamp}"
    json_path = folder / f"{stem}.json"
    md_path = folder / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
