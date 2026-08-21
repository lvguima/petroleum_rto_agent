"""Strict JSON validation and versioned canonical fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, cast

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]

IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


def strict_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    context: str,
) -> None:
    allowed = required | (set() if optional is None else optional)
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise ValueError(f"{context} fields differ; missing={missing}, unknown={unknown}")


def as_mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def as_sequence(value: object, *, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be a sequence")
    return cast(Sequence[object], value)


def identifier(value: object, *, context: str) -> str:
    if isinstance(value, str) and (".." in value or "/" in value or "\\" in value):
        raise ValueError(f"{context} must not contain path traversal")
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def boolean(value: object, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be boolean")
    return value


def finite(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric and must not be boolean")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def integer(value: object, *, context: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer and must not be boolean")
    if value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def string_mapping(value: object, *, context: str) -> Mapping[str, str]:
    raw = as_mapping(value, context=context)
    return MappingProxyType(
        {
            identifier(key, context=f"{context} key"): text(item, context=f"{context}.{key}")
            for key, item in raw.items()
        }
    )


def numeric_mapping(value: object, *, context: str) -> Mapping[str, float]:
    raw = as_mapping(value, context=context)
    return MappingProxyType(
        {
            identifier(key, context=f"{context} key"): finite(item, context=f"{context}.{key}")
            for key, item in raw.items()
        }
    )


def freeze_json(value: object, *, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{context} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{context} keys must be strings")
            frozen[key] = freeze_json(item, context=f"{context}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            freeze_json(item, context=f"{context}[{index}]") for index, item in enumerate(value)
        )
    raise TypeError(f"{context} must contain only JSON values")


def freeze_json_mapping(value: object, *, context: str) -> Mapping[str, JsonValue]:
    frozen = freeze_json(as_mapping(value, context=context), context=context)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError(f"{context} did not remain a mapping")
    return frozen


def thaw_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    frozen = freeze_json(value, context="canonical JSON")
    return json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
