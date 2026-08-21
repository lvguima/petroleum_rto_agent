"""Installed-code and environment provenance for M7 run manifests."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections.abc import Iterator, Mapping
from functools import cache
from importlib import metadata, resources
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType

_DISTRIBUTION_NAME = "petroleum-rto-agent"


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


def _source_tree_sha256(root: Traversable) -> str:
    """Hash the explicit installed-code closure that can affect CDU execution."""

    root_init = root.joinpath("__init__.py")
    if not root_init.is_file():
        raise RuntimeError("installed petroleum_rto root __init__.py is missing")
    cdu_root = root.joinpath("cdu")
    if not cdu_root.is_dir():
        raise RuntimeError("installed petroleum_rto.cdu package is missing")
    digest = hashlib.sha256()
    digest.update(b"petroleum_rto/__init__.py\0")
    digest.update(root_init.read_bytes())
    digest.update(b"\0")
    found_cdu_source = False
    for relative, item in _runtime_files(cdu_root, "petroleum_rto/cdu"):
        found_cdu_source = True
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    if not found_cdu_source:
        raise RuntimeError("installed petroleum_rto.cdu source tree is empty")
    return digest.hexdigest()


def _installed_source_tree_root() -> Traversable:
    return resources.files("petroleum_rto")


def _installed_source_is_immutable(root: Traversable) -> bool:
    """Return true only for the matching non-editable installed distribution."""

    try:
        distribution = metadata.distribution(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return False
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is not None:
        try:
            direct_url_payload = json.loads(direct_url)
        except json.JSONDecodeError:
            return False
        if not isinstance(direct_url_payload, Mapping):
            return False
        directory_info = direct_url_payload.get("dir_info")
        if directory_info is not None:
            if not isinstance(directory_info, Mapping):
                return False
            editable = directory_info.get("editable")
            if editable not in (None, False):
                return False
    if isinstance(root, Path):
        installed_root = Path(str(distribution.locate_file("petroleum_rto")))
        try:
            return root.resolve() == installed_root.resolve()
        except OSError:
            return False
    return False


@cache
def _cached_installed_source_tree_sha256() -> str:
    """Hash once for an immutable installed distribution within this process."""

    return _source_tree_sha256(_installed_source_tree_root())


def installed_source_tree_sha256() -> str:
    """Hash CDU installed sources without coupling manifests to sibling packages."""

    root = _installed_source_tree_root()
    if _installed_source_is_immutable(root):
        return _cached_installed_source_tree_sha256()
    return _source_tree_sha256(root)


def runtime_environment() -> Mapping[str, str]:
    """Return the deterministic environment fields retained by every manifest."""

    try:
        distribution_version = metadata.version(_DISTRIBUTION_NAME)
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
