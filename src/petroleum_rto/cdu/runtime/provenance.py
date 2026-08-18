"""Installed-code and environment provenance for M7 run manifests."""

from __future__ import annotations

import hashlib
import platform
import sys
from collections.abc import Iterator, Mapping
from importlib import metadata, resources
from importlib.resources.abc import Traversable
from types import MappingProxyType


def _runtime_files(
    node: Traversable,
    prefix: str = "",
) -> Iterator[tuple[str, Traversable]]:
    for child in sorted(node.iterdir(), key=lambda item: item.name):
        relative = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            if child.name != "__pycache__":
                yield from _runtime_files(child, relative)
        elif child.name.endswith((".py", ".json")):
            yield relative.replace("\\", "/"), child


def installed_source_tree_sha256() -> str:
    """Hash installed Python sources and packaged JSON resources by relative path."""

    root = resources.files("petroleum_rto")
    digest = hashlib.sha256()
    found = False
    for relative, item in _runtime_files(root):
        found = True
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    if not found:
        raise RuntimeError("installed petroleum_rto source tree is empty")
    return digest.hexdigest()


def runtime_environment() -> Mapping[str, str]:
    """Return the deterministic environment fields retained by every manifest."""

    try:
        distribution_version = metadata.version("petroleum-rto-cdu-model")
    except metadata.PackageNotFoundError:
        distribution_version = "not-installed"

    def present(value: str) -> str:
        return value if value.strip() else "unknown"

    return MappingProxyType(
        {
            "distribution_version": distribution_version,
            "git_commit": "unavailable",
            "git_dirty": "unavailable",
            "python_implementation": present(platform.python_implementation()),
            "python_version": present(platform.python_version()),
            "python_full_version": present(sys.version.splitlines()[0]),
            "operating_system": present(platform.system()),
            "os_release": present(platform.release()),
            "machine": present(platform.machine()),
        }
    )


__all__ = ["installed_source_tree_sha256", "runtime_environment"]
