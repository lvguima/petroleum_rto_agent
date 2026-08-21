from __future__ import annotations

import json
from pathlib import Path

import pytest

from petroleum_rto.rto import load_rto_v1_bundle
from petroleum_rto.rto.contracts import (
    DecisionVariableCatalogV1,
    OperatingContextV1,
    canonical_fingerprint,
)


def test_load_v1_bundle_is_strict_and_deterministic(repo_root: Path) -> None:
    first = load_rto_v1_bundle(repo_root)
    second = load_rto_v1_bundle(repo_root)

    assert first == second
    assert first.intent.operating_context_ref == first.context.ref
    assert tuple(item.variable_id for item in first.decision_catalog.variables if item.enabled) == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )
    assert first.context.fingerprint == (
        "63469a6095e20c15e8ecaab3b181867f1635adb7ef560dc7831a2729dac5c245"
    )


def test_packaged_bundle_is_semantically_identical_to_checkout_configs(repo_root: Path) -> None:
    assert load_rto_v1_bundle() == load_rto_v1_bundle(repo_root)


def test_canonical_fingerprint_ignores_mapping_order() -> None:
    assert canonical_fingerprint({"a": 1.0, "b": 2.0}) == canonical_fingerprint(
        {"b": 2.0, "a": 1.0}
    )
    assert canonical_fingerprint({"a": 1.0}) != canonical_fingerprint({"a": 1.1})


@pytest.mark.parametrize("bad_value", [True, float("nan"), float("inf")])
def test_context_rejects_boolean_and_nonfinite_feed(repo_root: Path, bad_value: object) -> None:
    path = repo_root / "configs" / "rto" / "contexts" / "case_20260604_v1.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["feed_mass_flow_kg_s"] = bad_value
    with pytest.raises((TypeError, ValueError)):
        OperatingContextV1.from_mapping(value)


def test_context_rejects_unknown_field(repo_root: Path) -> None:
    path = repo_root / "configs" / "rto" / "contexts" / "case_20260604_v1.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["unexpected"] = 1
    with pytest.raises(ValueError, match="unknown"):
        OperatingContextV1.from_mapping(value)


def test_decision_catalog_rejects_unknown_unit(repo_root: Path) -> None:
    path = repo_root / "configs" / "rto" / "catalogs" / "decision_variables_v1.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["variables"][0]["canonical_unit"] = "barrels/day"
    with pytest.raises(ValueError, match="unit registry"):
        DecisionVariableCatalogV1.from_mapping(value)
