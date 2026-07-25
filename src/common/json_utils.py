"""Bounded JSON normalization for local state, reports, and JSONL logs."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


def make_json_safe(value: Any, *, _depth: int = 0,
                   max_depth: int = 20, max_items: int = 10_000) -> Any:
    """Recursively convert known local value types without stringifying arbitrary objects."""
    if _depth > max_depth:
        raise ValueError(f"JSON value exceeds maximum depth {max_depth}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return make_json_safe(value.value, _depth=_depth + 1,
                              max_depth=max_depth, max_items=max_items)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, BaseException):
        return {
            "error_type": type(value).__name__,
            "message": str(value)[:1000],
        }
    if is_dataclass(value) and not isinstance(value, type):
        return make_json_safe(asdict(value), _depth=_depth + 1,
                              max_depth=max_depth, max_items=max_items)
    if isinstance(value, dict):
        if len(value) > max_items:
            raise ValueError(f"JSON mapping exceeds maximum items {max_items}")
        return {
            str(key): make_json_safe(item, _depth=_depth + 1,
                                     max_depth=max_depth, max_items=max_items)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > max_items:
            raise ValueError(f"JSON collection exceeds maximum items {max_items}")
        return [
            make_json_safe(item, _depth=_depth + 1,
                           max_depth=max_depth, max_items=max_items)
            for item in value
        ]
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def json_dumps(value: Any, **kwargs) -> str:
    return json.dumps(make_json_safe(value), **kwargs)
