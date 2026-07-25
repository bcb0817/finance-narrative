from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.runtime import REPO_ROOT

from .models import normalize_symbol


WATCHLIST_PATH = REPO_ROOT / "config" / "market_watchlist.json"


def load_watchlist(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or WATCHLIST_PATH
    value = json.loads(source.read_text(encoding="utf-8"))
    rows = value.get("symbols", value) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("market watchlist must be a list")
    result = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["symbol"] = normalize_symbol(str(row["symbol"]))
        row["provider_symbol"] = normalize_symbol(str(row.get("provider_symbol") or row["symbol"]))
        if row["symbol"] in seen:
            raise ValueError(f"duplicate market symbol: {row['symbol']}")
        seen.add(row["symbol"])
        result.append(row)
    return result


def enabled_symbols(*, asset_types: set[str] | None = None) -> list[dict[str, Any]]:
    rows = [row for row in load_watchlist() if bool(row.get("enabled"))]
    if asset_types:
        rows = [row for row in rows if row.get("asset_type") in asset_types]
    return sorted(rows, key=lambda row: (-int(row.get("priority", 0)), row["symbol"]))


def symbol_config(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    for row in load_watchlist():
        if row["symbol"] == normalized:
            return row
    raise KeyError(f"symbol not registered: {normalized}")
