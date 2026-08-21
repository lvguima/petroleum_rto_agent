"""Strict JSON decoding for untrusted domain-model responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

_MAX_RESPONSE_BYTES = 1_000_000


def _reject_constant(value: str) -> object:
    raise ValueError(f"domain model response contains non-finite JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"domain model response contains duplicate key {key!r}")
        result[key] = value
    return result


def decode_domain_model_response(value: str | bytes | bytearray) -> Mapping[str, object]:
    """Decode one bounded UTF-8 JSON object without losing duplicate-key evidence."""

    if not isinstance(value, (str, bytes, bytearray)):
        raise TypeError("domain model response JSON must be str, bytes or bytearray")
    try:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            source = value
        else:
            encoded = bytes(value)
            source = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("domain model response must be valid UTF-8 JSON") from exc
    if not 0 < len(encoded) <= _MAX_RESPONSE_BYTES:
        raise ValueError("domain model response size must be between 1 byte and 1 MB")
    try:
        decoded = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("domain model response must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, Mapping) or any(not isinstance(key, str) for key in decoded):
        raise TypeError("domain model response JSON must contain one object")
    return cast(Mapping[str, object], decoded)
