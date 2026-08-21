"""Resolve stable CDU repository resource identifiers to physical module paths.

M5--M7 evidence stores repository-relative paths as part of its immutable audit
contract.  The project now keeps CDU assets in module namespaces, while those
historic identifiers remain unchanged so artifact bytes and fingerprints do not
need to be rewritten merely because the checkout was reorganized.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Final

_PHYSICAL_PREFIXES: Final[tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = (
    (("reports", "modeling"), ("reports", "cdu")),
    (("base_files",), ("data", "cdu", "raw")),
    (("configs",), ("configs", "cdu")),
    (("data",), ("data", "cdu")),
)
_LEGACY_CRLF_RESOURCE_IDS: Final[frozenset[str]] = frozenset(
    {
        "configs/models/cdu_mini_v0.1.0.json",
        "configs/models/components_v0.1.0.json",
        "configs/cases/case_20260604.json",
        "configs/scenarios/open_loop_baseline_v0.1.0.json",
        "configs/scenarios/open_loop_feed_step_v0.1.0.json",
    }
)


def canonicalize_cdu_resource_bytes(payload: bytes, resource_id: str) -> bytes:
    """Return bytes using the frozen newline convention for a CDU resource id.

    Five early resources were accepted from a Windows CRLF checkout and their
    byte digests are embedded in M5--M7 evidence. Git stores the same text with
    LF line endings, so those specific resources are normalized to the accepted
    CRLF convention before hashing. Later resources retain raw-byte hashing.
    """

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if resource_id in _LEGACY_CRLF_RESOURCE_IDS:
        normalized_lf = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        payload = normalized_lf.replace(b"\n", b"\r\n")
    return payload


def cdu_resource_bytes_sha256(payload: bytes, resource_id: str) -> str:
    """Hash bytes using the frozen newline convention for a CDU resource id."""

    return hashlib.sha256(canonicalize_cdu_resource_bytes(payload, resource_id)).hexdigest()


def cdu_resource_file_sha256(path: Path, resource_id: str) -> str:
    """Hash a physical file according to its stable logical resource id."""

    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    return cdu_resource_bytes_sha256(path.read_bytes(), resource_id)


def resolve_cdu_repository_path(repo_root: Path, resource_id: str) -> Path:
    """Map a stable CDU resource id to its namespaced physical checkout path.

    Unknown top-level identifiers remain repository-relative.  This permits the
    helper to validate shared files without teaching CDU code about other
    modules.  Existing legacy layouts are accepted as a read-compatibility
    fallback, but new paths are always preferred and are used for new outputs.
    """

    if not isinstance(repo_root, Path):
        raise TypeError("repo_root must be a pathlib.Path")
    if not isinstance(resource_id, str) or not resource_id or "\\" in resource_id:
        raise ValueError("resource_id must be a non-empty repository-relative POSIX path")
    parsed = PurePosixPath(resource_id)
    if parsed.is_absolute() or not parsed.parts or "." in parsed.parts or ".." in parsed.parts:
        raise ValueError("resource_id must stay inside the repository")

    root = repo_root.resolve()
    physical_parts = parsed.parts
    for logical_prefix, physical_prefix in _PHYSICAL_PREFIXES:
        if parsed.parts[: len(logical_prefix)] == logical_prefix:
            physical_parts = physical_prefix + parsed.parts[len(logical_prefix) :]
            break

    physical = root.joinpath(*physical_parts)
    legacy = root.joinpath(*parsed.parts)
    selected = physical if physical.exists() or not legacy.exists() else legacy
    resolved = selected.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"resource path escapes repository: {resource_id}") from exc
    return selected
