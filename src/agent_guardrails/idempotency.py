"""Canonical form of tool arguments, so LLM noise cannot create a second 'different' action."""

import hashlib
import json
import re
from typing import Any

from pydantic_core import to_jsonable_python

_INT = re.compile(r"-?\d+")


def normalize_args(value: Any) -> Any:
    """Drop None, treat 123 and "123" alike, sort keys, recursively."""
    if isinstance(value, dict):
        return {k: normalize_args(v) for k, v in sorted(value.items()) if v is not None}
    if isinstance(value, list):
        return [normalize_args(v) for v in value]
    # Simplification: "007" becomes 7, so zero-padded string ids collide; per-field schemas would fix it.
    if isinstance(value, str) and _INT.fullmatch(value):
        return int(value)
    return value


def jsonable(args: dict[str, Any]) -> dict[str, Any]:
    """Plain-JSON form (nested pydantic models, datetimes, ...) that can be stored and re-validated."""
    return to_jsonable_python(args, fallback=str)


def canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(normalize_args(jsonable(args)), sort_keys=True, separators=(",", ":"))


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_args(args).encode()).hexdigest()


def idempotency_key(run_id: str, tool_name: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(f"{run_id}\x00{tool_name}\x00{canonical_args(args)}".encode()).hexdigest()
