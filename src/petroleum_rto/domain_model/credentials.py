"""Local-only DMXAPI credential loading with a fixed, non-packaged file boundary."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Final

LOCAL_DMX_API_CREDENTIAL_FILE: Final[Path] = Path(__file__).with_name("dmx_api.json")

_MAXIMUM_CREDENTIAL_FILE_BYTES: Final[int] = 4096
_MINIMUM_CREDENTIAL_LENGTH: Final[int] = 8
_MAXIMUM_CREDENTIAL_LENGTH: Final[int] = 2048


class LocalCredentialError(ValueError):
    """A safe local-configuration error that never includes credential material."""


def load_local_dmx_api_key(path: Path = LOCAL_DMX_API_CREDENTIAL_FILE) -> str | None:
    """Load a local API key from a protected file without following symbolic links.

    The checkout-only file may contain either the bare key or a strict JSON object
    shaped as ``{"api_key": "..."}``. Missing files return ``None``.
    """

    if not isinstance(path, Path):
        raise TypeError("local credential path must be pathlib.Path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LocalCredentialError("local credential file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LocalCredentialError("local credential source must be a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise LocalCredentialError("local credential file permissions are too broad")
        if not 0 < metadata.st_size <= _MAXIMUM_CREDENTIAL_FILE_BYTES:
            raise LocalCredentialError("local credential file size is invalid")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(_MAXIMUM_CREDENTIAL_FILE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != metadata.st_size or len(payload) > _MAXIMUM_CREDENTIAL_FILE_BYTES:
        raise LocalCredentialError("local credential file changed while being read")
    try:
        source = payload.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise LocalCredentialError("local credential file must contain printable ASCII") from exc
    if source.startswith("{"):
        try:
            raw = json.loads(
                source,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(raw, dict) or set(raw) != {"api_key"}:
                raise ValueError("invalid credential object")
            value = raw["api_key"]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LocalCredentialError("local credential JSON contract is invalid") from exc
        if not isinstance(value, str):
            raise LocalCredentialError("local credential JSON contract is invalid")
        credential = value
    else:
        credential = source
    if (
        not _MINIMUM_CREDENTIAL_LENGTH <= len(credential) <= _MAXIMUM_CREDENTIAL_LENGTH
        or credential != credential.strip()
        or any(not 33 <= ord(character) <= 126 for character in credential)
    ):
        raise LocalCredentialError("local credential value is invalid")
    return credential


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported credential JSON constant: {value}")


__all__ = [
    "LOCAL_DMX_API_CREDENTIAL_FILE",
    "LocalCredentialError",
    "load_local_dmx_api_key",
]
