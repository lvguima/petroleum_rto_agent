"""Load the unified capability inputs from fixed, source-controlled paths."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

from ..contracts.common import as_mapping, digest, strict_keys
from ..contracts.reference import ContractRef
from .models import CapabilityCatalog, ContextSchema, SystemPolicy, UnifiedCapabilityBundle

_MAX_CONFIG_BYTES = 1_000_000
_FILES = {
    "catalog": "catalog.json",
    "context_schema": "context_schema.json",
    "system_policy": "system_policy.json",
}
_RESOURCE_PACKAGE = "petroleum_rto.rto.data"
_RESOURCE_NAME = "unified_bundle.json"
_BUNDLE_FIELDS = {
    "catalog",
    "context_schema",
    "system_policy",
    "catalog_ref",
    "context_schema_ref",
    "system_policy_ref",
    "bundle_fingerprint",
}


def _reject_constant(value: str) -> object:
    raise ValueError(f"capability config contains non-finite JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"capability config contains duplicate key {key!r}")
        result[key] = value
    return result


def _parse_strict_object(data: bytes, *, context: str) -> object:
    if not data or len(data) > _MAX_CONFIG_BYTES:
        raise ValueError(f"{context} size must be between 1 byte and 1 MB")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} must be valid UTF-8 JSON") from exc


def _load_strict_object(path: Path, *, context: str) -> object:
    resolved = path.resolve()
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        raise ValueError(f"{context} must be an existing JSON file")
    return _parse_strict_object(resolved.read_bytes(), context=context)


def _bundle_from_components(
    catalog_raw: Mapping[str, object],
    context_raw: Mapping[str, object],
    policy_raw: Mapping[str, object],
) -> UnifiedCapabilityBundle:
    return UnifiedCapabilityBundle(
        catalog=CapabilityCatalog.from_mapping(catalog_raw),
        context_schema=ContextSchema.from_mapping(context_raw),
        system_policy=SystemPolicy.from_mapping(policy_raw),
    )


def _packaged_bundle() -> UnifiedCapabilityBundle:
    try:
        resource = resources.files(_RESOURCE_PACKAGE).joinpath(_RESOURCE_NAME)
        value = _parse_strict_object(
            resource.read_bytes(),
            context="packaged unified capability bundle",
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise ValueError("packaged unified capability bundle is missing") from exc
    raw = as_mapping(value, context="packaged unified capability bundle")
    strict_keys(
        raw,
        required=_BUNDLE_FIELDS,
        context="packaged unified capability bundle",
    )
    bundle = _bundle_from_components(
        as_mapping(raw["catalog"], context="packaged bundle.catalog"),
        as_mapping(raw["context_schema"], context="packaged bundle.context_schema"),
        as_mapping(raw["system_policy"], context="packaged bundle.system_policy"),
    )
    expected_refs = {
        "catalog_ref": bundle.catalog.ref,
        "context_schema_ref": bundle.context_schema.ref,
        "system_policy_ref": bundle.system_policy.ref,
    }
    for field, expected in expected_refs.items():
        supplied = ContractRef.from_mapping(
            as_mapping(raw[field], context=f"packaged bundle.{field}")
        )
        if supplied != expected:
            raise ValueError(f"packaged {field} differs from embedded capability content")
    if digest(raw["bundle_fingerprint"], context="bundle_fingerprint") != bundle.fingerprint:
        raise ValueError("packaged bundle_fingerprint differs from embedded capability content")
    return bundle


def load_capability_bundle(repo_root: Path | None = None) -> UnifiedCapabilityBundle:
    """Load fixed unified capabilities from a checkout or the installed package."""

    if repo_root is not None and not isinstance(repo_root, Path):
        raise TypeError("repo_root must be pathlib.Path or None")
    packaged = _packaged_bundle()
    if repo_root is None:
        return packaged
    root = repo_root.resolve()
    config_root = (root / "configs" / "rto" / "capabilities").resolve()
    resolved: dict[str, Path] = {}
    for name, relative in _FILES.items():
        path = (config_root / relative).resolve()
        if not path.is_relative_to(config_root) or not path.is_file():
            raise ValueError(f"required unified capability config is missing or unsafe: {relative}")
        resolved[name] = path

    catalog_raw = as_mapping(
        _load_strict_object(resolved["catalog"], context="capability catalog"),
        context="capability catalog",
    )
    context_raw = as_mapping(
        _load_strict_object(resolved["context_schema"], context="context schema"),
        context="context schema",
    )
    policy_raw = as_mapping(
        _load_strict_object(resolved["system_policy"], context="system policy"),
        context="system policy",
    )
    checkout = _bundle_from_components(catalog_raw, context_raw, policy_raw)
    if checkout != packaged or checkout.fingerprint != packaged.fingerprint:
        raise ValueError("checkout unified capability configs differ from packaged bundle")
    return checkout
