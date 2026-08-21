"""Load external RTO JSON without accepting duplicate keys or non-finite values."""

from __future__ import annotations

import json
from pathlib import Path

from .models import ExternalOptimizationRequestV1
from .v2_models import DomainOptimizationIntentV2, ExternalOptimizationRequestV2

_MAX_REQUEST_BYTES = 1_000_000


def _reject_constant(value: str) -> object:
    raise ValueError(f"external request contains non-finite JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"external request contains duplicate key {key!r}")
        result[key] = value
    return result


def _load_strict_json(path: Path, *, context: str) -> object:
    if not isinstance(path, Path):
        raise TypeError("path must be pathlib.Path")
    resolved = path.resolve()
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        raise ValueError(f"{context} must be an existing .json file")
    size = resolved.stat().st_size
    if size <= 0 or size > _MAX_REQUEST_BYTES:
        raise ValueError(f"{context} size must be between 1 byte and 1 MB")
    try:
        return json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} must be valid UTF-8 JSON") from exc


def load_external_optimization_request(path: Path) -> ExternalOptimizationRequestV1:
    """Strictly load one user/domain-model request from a JSON file."""

    raw = _load_strict_json(path, context="external request")
    return ExternalOptimizationRequestV1.from_mapping(raw)


def load_domain_optimization_intent_v2(path: Path) -> DomainOptimizationIntentV2:
    """Load one strict intent-only V2 JSON file without binding context."""

    raw = _load_strict_json(path, context="external domain intent")
    return DomainOptimizationIntentV2.from_mapping(raw)


def load_external_optimization_request_v2(path: Path) -> ExternalOptimizationRequestV2:
    """Load one strict V2 request; no fallback or field-based version guessing."""

    raw = _load_strict_json(path, context="external optimization request V2")
    return ExternalOptimizationRequestV2.from_mapping(raw)
