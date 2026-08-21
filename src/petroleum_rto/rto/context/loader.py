"""Strict JSON loader for trusted operating context fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from ..contracts.common import as_mapping
from ..contracts.context import OperatingContext

_MAX_CONTEXT_BYTES = 1_000_000


def _reject_constant(value: str) -> object:
    raise ValueError(f"operating context contains non-finite JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"operating context contains duplicate key {key!r}")
        result[key] = value
    return result


def load_operating_context(path: Path) -> OperatingContext:
    """Load one explicit trusted context; this function never reads an intent."""

    if not isinstance(path, Path):
        raise TypeError("path must be pathlib.Path")
    resolved = path.resolve()
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        raise ValueError("operating context must be an existing JSON file")
    size = resolved.stat().st_size
    if size <= 0 or size > _MAX_CONTEXT_BYTES:
        raise ValueError("operating context size must be between 1 byte and 1 MB")
    try:
        raw = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("operating context must be valid UTF-8 JSON") from exc
    return OperatingContext.from_mapping(as_mapping(raw, context="operating context"))
