from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from petroleum_rto.rto.catalogs import load_rto_v2_bundle
from petroleum_rto.rto.contracts import (
    OptimizationProblemV2,
    ResolvedOptimizationIntentV2,
    canonical_json_bytes,
)
from petroleum_rto.rto.inputs import (
    DomainOptimizationIntentV2,
    ExternalOptimizationRequestV2,
    bind_external_optimization_request_v2,
    capability_manifest_v2,
    load_domain_optimization_intent_v2,
    load_external_optimization_request_v2,
    validate_domain_intent_v2,
)


def _intent_path(repo_root: Path) -> Path:
    return repo_root / "configs/rto/intents/quality_yield_energy_v2.json"


def _request_path(repo_root: Path) -> Path:
    return repo_root / "configs/rto/requests/multiobjective_example_v2.json"


def _raw(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def test_v2_bundle_matches_packaged_copy_and_v1_base(repo_root: Path) -> None:
    checkout = load_rto_v2_bundle(repo_root)
    packaged = load_rto_v2_bundle()

    assert checkout == packaged
    assert checkout.base == packaged.base
    assert checkout.objective_catalog.maximum_objectives == 3
    assert checkout.policy.search.maximum_m2_candidates == 81
    assert checkout.policy.evaluation.top_k == 5


def test_domain_intent_and_request_round_trip_without_solver(repo_root: Path) -> None:
    bundle = load_rto_v2_bundle(repo_root)
    intent = load_domain_optimization_intent_v2(_intent_path(repo_root))
    request = load_external_optimization_request_v2(_request_path(repo_root))
    validation = validate_domain_intent_v2(bundle, intent)
    bound = bind_external_optimization_request_v2(bundle, request)

    assert validation.valid
    assert validation.status == "valid"
    assert not validation.solver_called
    assert DomainOptimizationIntentV2.from_mapping(intent.as_dict()) == intent
    assert ExternalOptimizationRequestV2.from_mapping(request.as_dict()) == request
    assert (
        ResolvedOptimizationIntentV2.from_mapping(bound.resolved_intent.as_dict())
        == bound.resolved_intent
    )
    assert OptimizationProblemV2.from_mapping(bound.problem.as_dict()) == bound.problem
    assert tuple(item.metric_id for item in bound.problem.objectives) == (
        "quality_proxy_max_abs_relative_change",
        "valuable_distillate_yield",
        "specific_furnace_fuel_energy_mj_per_t",
    )
    assert canonical_json_bytes(bound.problem.as_dict()) == canonical_json_bytes(
        OptimizationProblemV2.from_mapping(bound.problem.as_dict()).as_dict()
    )


def test_audit_text_changes_do_not_change_semantic_problem(repo_root: Path) -> None:
    bundle = load_rto_v2_bundle(repo_root)
    request = load_external_optimization_request_v2(_request_path(repo_root))
    changed_intent = replace(
        request.optimization_intent,
        original_text="另一种自然语言表述，但执行语义不变。",
        rationale_summary="审计说明发生变化。",
        source=replace(request.optimization_intent.source, correlation_id="request-002"),
    )
    changed_request = replace(
        request,
        request_id="multiobjective-nominal-feed-v2-audit",
        optimization_intent=changed_intent,
    )

    first = bind_external_optimization_request_v2(bundle, request)
    second = bind_external_optimization_request_v2(bundle, changed_request)

    assert request.optimization_intent.audit_fingerprint != changed_intent.audit_fingerprint
    assert request.optimization_intent.semantic_fingerprint == changed_intent.semantic_fingerprint
    assert first.resolved_intent.audit_fingerprint != second.resolved_intent.audit_fingerprint
    assert first.resolved_intent.ref == second.resolved_intent.ref
    assert first.problem.ref == second.problem.ref
    assert request.fingerprint != changed_request.fingerprint


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update({"unknown": 1}), "fields differ"),
        (lambda raw: raw["objectives"].append(raw["objectives"][0]), "unique"),
        (lambda raw: raw["objectives"][1].update({"priority_tier": 4}), "contiguous"),
        (lambda raw: raw["selection"].update({"return_pareto_front": 1}), "boolean"),
        (lambda raw: raw.update({"feed_mass_flow_t_h": 400.0}), "fields differ"),
    ],
)
def test_domain_intent_contract_rejects_unsafe_shapes(
    repo_root: Path,
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    raw = _raw(_intent_path(repo_root))
    mutation(raw)
    path = tmp_path / "intent.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        load_domain_optimization_intent_v2(path)


def test_domain_intent_semantic_errors_are_structured(repo_root: Path) -> None:
    bundle = load_rto_v2_bundle(repo_root)
    intent = load_domain_optimization_intent_v2(_intent_path(repo_root))
    wrong_sense = replace(intent.objectives[1], sense="minimize")
    invalid = replace(
        intent,
        objectives=(intent.objectives[0], wrong_sense, intent.objectives[2]),
        ambiguities=("confirm-product-priority",),
    )

    result = validate_domain_intent_v2(bundle, invalid)

    assert not result.valid
    assert result.status == "needs_clarification"
    assert tuple(item.code for item in result.issues) == (
        "needs-clarification",
        "objective-sense-mismatch",
    )
    assert result.issues[1].json_pointer == "/objectives/1/sense"
    assert result.issues[1].supported_values == ("maximize",)
    assert not result.solver_called


def test_capability_manifest_is_derived_from_versioned_catalogs(repo_root: Path) -> None:
    bundle = load_rto_v2_bundle(repo_root)
    manifest = capability_manifest_v2(bundle).as_dict()

    assert manifest["supported_request_versions"] == ["external-optimization-request-v2"]
    assert manifest["maximum_objectives"] == 3
    assert manifest["maximum_returned_candidates"] == 5
    assert manifest["objective_profiles"] == ["quality-yield-energy-pareto-v1"]
    assert manifest["selection_profiles"] == ["lexicographic-quality-yield-energy-v1"]
    assert manifest["solver_called"] is False


def test_v2_loader_rejects_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"2.0.0","schema_version":"2.0.0"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_domain_optimization_intent_v2(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        load_external_optimization_request_v2(nonfinite)
