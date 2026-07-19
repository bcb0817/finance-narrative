"""Local estimated API cost ledger used as a monthly safety brake."""
from __future__ import annotations

import json
import os
from datetime import datetime

try:
    from runtime import JST, state_dir
except ImportError:  # pragma: no cover
    from common.runtime import JST, state_dir


MODEL_PRICES = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}


def _ledger_path():
    return state_dir() / "api_cost_ledger.jsonl"


def monthly_openai_cost(now: datetime | None = None) -> float:
    now = now or datetime.now(JST)
    total = 0.0
    try:
        for line in _ledger_path().read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                ts = datetime.fromisoformat(item["ts"])
                if (ts.year, ts.month) == (now.year, now.month):
                    total += float(item.get("estimated_usd", 0.0))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    return total


def ensure_openai_budget() -> None:
    limit = float(os.getenv("OPENAI_MONTHLY_BUDGET_USD", "5.0") or 5.0)
    current = monthly_openai_cost()
    if current >= limit:
        raise RuntimeError(f"OpenAI monthly budget reached: ${current:.2f}/${limit:.2f}")


def record_openai_usage(response, model: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    input_price, output_price = MODEL_PRICES.get(model, (1.0, 5.0))
    estimated = input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price
    record = {
        "ts": datetime.now(JST).isoformat(),
        "provider": "openai",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_usd": round(estimated, 8),
    }
    path = _ledger_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
