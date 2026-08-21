"""Private validation primitives for the intent boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Final, cast

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def strict_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    context: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
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


def text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def boolean(value: object, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be boolean")
    return value


def integer(value: object, *, context: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer and must not be boolean")
    if value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
