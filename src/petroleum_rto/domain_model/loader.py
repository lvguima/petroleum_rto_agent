"""Strict loader for the provider catalog's checkout and packaged copies."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Final

from ._json import decode_json_object
from .models import ProviderCatalog

_CATALOG_FILE: Final[str] = "provider_catalog.json"
_MAX_CATALOG_BYTES: Final[int] = 256 * 1024


def packaged_provider_catalog_bytes() -> bytes:
    """Return the exact package resource bytes, independent of the current directory."""

    resource = resources.files("petroleum_rto.domain_model.data").joinpath(_CATALOG_FILE)
    try:
        return resource.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("packaged domain-model provider catalog is missing") from exc


def load_provider_catalog(repo_root: Path | None = None) -> ProviderCatalog:
    """Load the package catalog and optionally prove checkout bytes are identical."""

    packaged = packaged_provider_catalog_bytes()
    if repo_root is not None:
        root = Path(repo_root).resolve()
        source_path = root / "configs" / "domain_model" / _CATALOG_FILE
        try:
            source = source_path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError("checkout domain-model provider catalog is missing") from exc
        if source != packaged:
            raise ValueError("checkout provider catalog differs byte-for-byte from package data")
    raw = decode_json_object(
        packaged,
        context="domain-model provider catalog",
        maximum_bytes=_MAX_CATALOG_BYTES,
    )
    return ProviderCatalog.from_mapping(raw)
