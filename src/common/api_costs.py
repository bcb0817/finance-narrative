"""Local estimated API cost ledger used as a monthly safety brake."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime

try:
    from runtime import JST, state_dir
except ImportError:  # pragma: no cover
    from common.runtime import JST, state_dir


MODEL_PRICES = {
    "gpt-6-astra": (10.0, 50.0),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}


def _ledger_path():
    return state_dir() / "api_cost_ledger.jsonl"


def _lock_path():
    return _ledger_path().with_suffix(".lock")


def _append_record(record: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _acquire_ledger_lock(timeout_seconds: float = 5.0):
    """Serialize check-and-reserve across daemon processes without holding a network lock."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("OpenAI budget ledger is busy; request blocked safely")
            time.sleep(0.02)


def _release_ledger_lock(handle: int) -> None:
    try:
        os.close(handle)
    finally:
        try:
            _lock_path().unlink()
        except FileNotFoundError:
            pass


def monthly_openai_cost(now: datetime | None = None) -> float:
    now = now or datetime.now(JST)
    items: list[dict] = []
    try:
        for line in _ledger_path().read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                ts = datetime.fromisoformat(item["ts"])
                if (ts.year, ts.month) == (now.year, now.month):
                    items.append(item)
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    # This ledger is shared with other API-cost reporters.  Only records made
    # by this module (legacy records had no provider) belong to the OpenAI
    # safety brake; otherwise unrelated provider spend can incorrectly stop
    # OpenAI calls.
    openai_items = [
        item for item in items
        if item.get("provider") in {None, "", "openai"}
    ]
    reservations = {item.get("reservation_id"): float(item.get("estimated_usd", 0.0))
                    for item in openai_items if item.get("kind") == "reservation"}
    settled = {item.get("reservation_id") for item in items
               if item.get("kind") in {"usage", "release"} and item.get("reservation_id")}
    total = sum(float(item.get("estimated_usd", 0.0)) for item in openai_items
                if item.get("kind") not in {"reservation", "release"})
    total += sum(cost for reservation_id, cost in reservations.items() if reservation_id not in settled)
    return total


def projected_openai_cost(model: str, prompt: str, max_output_tokens: int) -> float:
    """Return a conservative preflight estimate for one Responses call.

    Responses usage is only available after the request completes.  Reserving
    the requested output capacity prevents a successful call from pushing the
    local monthly safety brake past its configured limit.
    """
    input_price, output_price = MODEL_PRICES.get(model, (1.0, 5.0))
    input_tokens = max(0, len(prompt)) / 4
    output_tokens = max(0, int(max_output_tokens))
    return input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price


def openai_budget_limit() -> float:
    """Return the effective OpenAI cap without allowing runtime drift to raise it.

    The daemon inherits its parent environment on Windows, so an older process
    can retain a larger value after ``.env`` has been corrected.  When the
    checked-in approval file is available, it is an upper bound rather than a
    source that a stale environment may override.
    """
    try:
        runtime_limit = float(os.getenv("OPENAI_MONTHLY_BUDGET_USD", "5.0") or 5.0)
    except ValueError:
        runtime_limit = 5.0
    try:
        from runtime import REPO_ROOT
    except ImportError:  # pragma: no cover
        from common.runtime import REPO_ROOT
    try:
        approved_limit = float(json.loads(
            (REPO_ROOT / "config" / "approved_budgets.json").read_text(encoding="utf-8")
        )["OPENAI_MONTHLY_BUDGET_USD"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        approved_limit = runtime_limit
    if approved_limit <= 0:
        approved_limit = runtime_limit
    return min(runtime_limit, approved_limit)


def ensure_openai_budget(*, model: str | None = None, prompt: str = "", max_output_tokens: int = 0) -> str | None:
    limit = openai_budget_limit()
    projected = projected_openai_cost(model, prompt, max_output_tokens) if model else 0.0
    if not model:
        return None
    lock = _acquire_ledger_lock()
    try:
        current = monthly_openai_cost()
        if current + projected > limit:
            raise RuntimeError(f"OpenAI monthly budget reached: ${current:.2f}/${limit:.2f}")
        reservation_id = uuid.uuid4().hex
        _append_record({"ts": datetime.now(JST).isoformat(), "kind": "reservation",
                        "reservation_id": reservation_id, "model": model,
                        "estimated_usd": round(projected, 8)})
        return reservation_id
    finally:
        _release_ledger_lock(lock)


def record_openai_usage(response, model: str, *, reservation_id: str | None = None) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0) or 0)
    input_price, output_price = MODEL_PRICES.get(model, (1.0, 5.0))
    if model == "gpt-6-astra" and input_tokens > 272000:
        input_price *= 2
        output_price *= 1.5
    estimated = input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price
    record = {
        "ts": datetime.now(JST).isoformat(),
        "kind": "usage",
        "reservation_id": reservation_id,
        "provider": "openai",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_usd": round(estimated, 8),
    }
    from common.editorial import TRACE
    if TRACE.get():
        record['editorial_decision_id'] = TRACE.get()
        record['cost_basis'] = 'local_token_price_estimate_not_invoice'
    _append_record(record)


def release_openai_reservation(reservation_id: str | None) -> None:
    if reservation_id:
        _append_record({"ts": datetime.now(JST).isoformat(), "kind": "release",
                        "reservation_id": reservation_id, "estimated_usd": 0.0})
