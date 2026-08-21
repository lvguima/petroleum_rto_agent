from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto.capabilities import (
    BundleCapabilityView,
    CapabilityCatalog,
    ContextSchema,
    SystemPolicy,
    build_public_capability_manifest,
    build_solver_routing_policy,
    load_capability_bundle,
)
from petroleum_rto.rto.unified_inputs import IntentResolver, OptimizationIntent
from tests.rto.unit.test_unified_intent import _raw as _intent_raw


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _all_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def test_load_unified_capabilities_exposes_atomic_composition(repo_root: Path) -> None:
    first = load_capability_bundle(repo_root)
    second = load_capability_bundle(repo_root)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert tuple(item.objective_id for item in first.catalog.objectives) == (
        "maximize-valuable-distillate-yield",
        "minimize-quality-proxy-change",
        "minimize-specific-furnace-energy",
    )
    assert tuple(
        item.decision_id for item in first.catalog.decisions if item.availability == "available"
    ) == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )
    assert first.catalog.decisions[1].decision_id == "reflux_ratio_target"
    assert first.catalog.decisions[1].availability == "deferred"
    assert "fresh_feed_load_kg_s" not in {item.decision_id for item in first.catalog.decisions}
    assert "fresh_feed_load_kg_s" in {item.field_id for item in first.context_schema.fields}
    assert {
        (item.minimum_objectives, item.maximum_objectives)
        for item in first.system_policy.execution_routes
    } == {(1, 1), (2, 3)}


def test_packaged_unified_bundle_matches_checkout_and_is_cwd_independent(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = load_capability_bundle(repo_root)
    monkeypatch.chdir(tmp_path)

    packaged = load_capability_bundle()
    raw = json.loads(
        resources.files("petroleum_rto.rto.data")
        .joinpath("unified_bundle.json")
        .read_text(encoding="utf-8")
    )

    assert packaged == checkout
    assert packaged.fingerprint == checkout.fingerprint
    assert raw == {
        "catalog": _object(repo_root / "configs" / "rto" / "capabilities" / "catalog.json"),
        "context_schema": _object(
            repo_root / "configs" / "rto" / "capabilities" / "context_schema.json"
        ),
        "system_policy": _object(
            repo_root / "configs" / "rto" / "capabilities" / "system_policy.json"
        ),
        "catalog_ref": packaged.catalog.ref.as_dict(),
        "context_schema_ref": packaged.context_schema.ref.as_dict(),
        "system_policy_ref": packaged.system_policy.ref.as_dict(),
        "bundle_fingerprint": packaged.fingerprint,
    }


def test_unified_bundle_loader_rejects_non_path_checkout_root() -> None:
    with pytest.raises(TypeError, match="pathlib.Path or None"):
        load_capability_bundle(cast(Path, "not-a-path"))


def test_context_schema_contains_structure_but_no_context_values(repo_root: Path) -> None:
    schema = load_capability_bundle(repo_root).context_schema.as_dict()

    assert set(schema) == {
        "schema_version",
        "context_schema_id",
        "context_schema_version",
        "claim_scope",
        "fields",
    }
    assert not (
        {"context_id", "provider_id", "model_ref", "case_ref", "data_timestamp"} & set(schema)
    )
    assert not ({"value", "default", "nominal_value"} & _all_keys(schema))


def test_public_manifest_omits_internal_bindings_and_formulas(repo_root: Path) -> None:
    manifest = build_public_capability_manifest(load_capability_bundle(repo_root))
    payload = manifest.as_dict()
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert (
        manifest.fingerprint
        == build_public_capability_manifest(load_capability_bundle(repo_root)).fingerprint
    )
    assert not (
        {
            "formula_ref",
            "source_paths",
            "m2_parameter_path",
            "m4_loop_id",
            "controller_owner",
            "compiler_rule_id",
            "m2_preset_id",
            "m4_preset_id",
            "cache_policy",
            "tie_breaks",
            "search_algorithm_id",
        }
        & _all_keys(payload)
    )
    assert "runtime_convergence_conservation_finite_nonnegative_v1" not in payload_text
    assert "operating.furnace_outlet_temperature_c" not in payload_text
    assert "recipe" not in payload
    assert "profile" not in payload


@pytest.mark.parametrize(
    ("filename", "parser"),
    [
        ("catalog.json", CapabilityCatalog.from_mapping),
        ("context_schema.json", ContextSchema.from_mapping),
        ("system_policy.json", SystemPolicy.from_mapping),
    ],
)
def test_unified_models_reject_unknown_root_fields(
    repo_root: Path,
    filename: str,
    parser: object,
) -> None:
    value = _object(repo_root / "configs" / "rto" / "capabilities" / filename)
    value["unexpected"] = True

    assert callable(parser)
    with pytest.raises(ValueError, match="unknown"):
        parser(value)


def test_catalog_rejects_unknown_nested_fields(repo_root: Path) -> None:
    value = _object(repo_root / "configs" / "rto" / "capabilities" / "catalog.json")
    metrics = cast(list[dict[str, object]], value["metrics"])
    metrics[0]["unexpected"] = True

    with pytest.raises(ValueError, match="unknown"):
        CapabilityCatalog.from_mapping(value)


def test_catalog_rejects_duplicate_atom_ids(repo_root: Path) -> None:
    value = _object(repo_root / "configs" / "rto" / "capabilities" / "catalog.json")
    metrics = cast(list[dict[str, object]], value["metrics"])
    metrics.append(copy.deepcopy(metrics[0]))

    with pytest.raises(ValueError, match="unique"):
        CapabilityCatalog.from_mapping(value)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_catalog_rejects_nonfinite_decision_bounds(
    repo_root: Path,
    bad_value: float,
) -> None:
    value = _object(repo_root / "configs" / "rto" / "capabilities" / "catalog.json")
    decisions = cast(list[dict[str, object]], value["decisions"])
    decisions[0]["lower_bound"] = bad_value

    with pytest.raises(ValueError, match="finite"):
        CapabilityCatalog.from_mapping(value)


def test_system_policy_rejects_nonfinite_guardrail_limit(repo_root: Path) -> None:
    value = _object(repo_root / "configs" / "rto" / "capabilities" / "system_policy.json")
    guardrails = cast(list[dict[str, object]], value["hard_guardrails"])
    guardrails[0]["limit"] = float("nan")

    with pytest.raises(ValueError, match="finite"):
        SystemPolicy.from_mapping(value)


@pytest.mark.parametrize("multi", [False, True])
def test_real_capability_bundle_resolves_same_unified_intent_contract(
    repo_root: Path,
    multi: bool,
) -> None:
    bundle = load_capability_bundle(repo_root)
    intent = OptimizationIntent.from_mapping(_intent_raw(multi=multi))

    resolution = IntentResolver().resolve(intent, BundleCapabilityView(bundle))

    assert resolution.status == "resolved"
    assert resolution.resolved_intent == intent


def test_system_policy_projects_to_internal_solver_order_without_manifest_leak(
    repo_root: Path,
) -> None:
    bundle = load_capability_bundle(repo_root)
    routing = build_solver_routing_policy(bundle)
    manifest = build_public_capability_manifest(bundle).as_dict()

    assert routing.solver_order == (
        "deterministic-full-grid",
        "coarse-grid-local-refine",
    )
    assert "search_algorithm_id" not in _all_keys(manifest)
