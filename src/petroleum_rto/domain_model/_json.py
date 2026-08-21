"""Small strict-JSON helpers shared by the provider-neutral domain-model core."""

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

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION: Final[re.Pattern[str]] = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_DEPTH: Final[int] = 64


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
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def version(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError(f"{context} must be a semantic version")
    return value


def text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def integer(value: object, *, context: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer and must not be boolean")
    if value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def freeze_json(value: object, *, context: str, _depth: int = 0) -> JsonValue:
    if _depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{context} exceeds the maximum JSON nesting depth")
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
            frozen[key] = freeze_json(item, context=f"{context}.{key}", _depth=_depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            freeze_json(
                item,
                context=f"{context}[{index}]",
                _depth=_depth + 1,
            )
            for index, item in enumerate(value)
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


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_constant(value: str) -> object:
    raise ValueError(f"JSON contains non-finite constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def decode_json_object(
    value: str | bytes | bytearray,
    *,
    context: str,
    maximum_bytes: int,
) -> Mapping[str, object]:
    if not isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be str, bytes or bytearray")
    try:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            source = value
        else:
            encoded = bytes(value)
            source = encoded.decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ValueError(f"{context} must be valid UTF-8 JSON") from exc
    if not 0 < len(encoded) <= maximum_bytes:
        raise ValueError(f"{context} exceeds its byte limit")
    try:
        decoded = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{context} must be valid UTF-8 JSON") from exc
    return as_mapping(decoded, context=context)
