"""Strict JSON loader for the unified, context-free optimization intent."""

from __future__ import annotations

import json
from pathlib import Path

from .models import OptimizationIntent

_MAX_INTENT_BYTES = 1_000_000


def _reject_constant(value: str) -> object:
    raise ValueError(f"optimization intent contains non-finite JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"optimization intent contains duplicate key {key!r}")
        result[key] = value
    return result


def load_optimization_intent(path: Path) -> OptimizationIntent:
    """Load one strict UTF-8 JSON intent without binding context or selecting a solver."""

    if not isinstance(path, Path):
        raise TypeError("path must be pathlib.Path")
    resolved = path.resolve()
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        raise ValueError("optimization intent must be an existing .json file")
    size = resolved.stat().st_size
    if size <= 0 or size > _MAX_INTENT_BYTES:
        raise ValueError("optimization intent size must be between 1 byte and 1 MB")
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("optimization intent must be valid UTF-8 JSON") from exc
    return OptimizationIntent.from_mapping(value)
