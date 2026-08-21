from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.capabilities import CapabilityBundle
from petroleum_rto.rto.compilation import (
    CandidatePlanCompiler,
    CompiledPair,
    assert_compiled_pair,
)
from petroleum_rto.rto.contracts.candidate import CandidateEvaluation, CandidateProposal
from petroleum_rto.rto.contracts.common import JsonValue, thaw_json
from petroleum_rto.rto.contracts.context import OperatingContext
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from petroleum_rto.rto.contracts.simulation import (
    SIMULATION_SCHEMA_VERSION,
    SimulationEvaluationRequest,
    SimulationPreview,
    SimulationRunBundle,
)
from petroleum_rto.rto.evaluation import (
    M4EvaluationService,
    M4PairedEvaluator,
)
from tests.rto.unit.test_unified_m2_evaluation import _basis, _WrongPresetFactory


def _m4_basis(
    repo_root: Path,
    *,
    multi: bool,
    one_decision: bool = False,
) -> tuple[
    CapabilityBundle,
    OperatingContext,
    OptimizationProblem,
    CandidateProposal,
    CompiledPair,
]:
    bundle, context, problem, proposal, _ = _basis(
        repo_root,
        multi=multi,
        one_decision=one_decision,
    )
    pair = CandidatePlanCompiler(bundle.catalog).compile_pair(
        problem,
        context,
        proposal,
        stage="M4",
        request_factory=CduM7RequestFactory(),
    )
    return bundle, context, problem, proposal, pair


def _plain(value: Mapping[str, JsonValue]) -> dict[str, object]:
    payload = thaw_json(cast(JsonValue, value))
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def test_m4_compiler_keeps_initialization_equal_and_emits_absolute_ratio_events(
    repo_root: Path,
) -> None:
    _, context, problem, proposal, pair = _m4_basis(repo_root, multi=True)
    baseline = _plain(pair.baseline.provider_request)
    candidate = _plain(pair.candidate.provider_request)

    assert baseline["parameters"] == candidate["parameters"]
    assert baseline["overrides"] == candidate["overrides"]
    assert baseline["initial_state"] == candidate["initial_state"]
    baseline_scenario = cast(dict[str, object], baseline["scenario"])
    candidate_scenario = cast(dict[str, object], candidate["scenario"])
    assert baseline_scenario["events"] == []
    events = cast(list[dict[str, object]], candidate_scenario["events"])
    assert [event["time_s"] for event in events] == [600.0, 600.0]
    assert [event["target"] for event in events] == [
        "furnace_temperature.setpoint_ratio",
        "top_pressure.setpoint_ratio",
    ]
    assert cast(float, events[0]["value"]) == pytest.approx(
        proposal.decision_values["furnace_temperature_target_k"]
        / context.current_setpoints["furnace_temperature_target_k"]
    )
    assert cast(float, events[1]["value"]) == pytest.approx(
        proposal.decision_values["tower_top_pressure_target_pa_a"]
        / context.current_setpoints["tower_top_pressure_target_pa_a"]
    )
    assert all(event["value_basis"] == "setpoint_ratio" for event in events)
    assert all(event["duration_s"] is None for event in events)
    assert candidate_scenario["duration_s"] == problem.evaluation_plan.m4_duration_s
    assert candidate_scenario["time_step_s"] == problem.evaluation_plan.m4_time_step_s


def test_m4_compiler_supports_atomic_decision_and_rejects_pair_drift(
    repo_root: Path,
) -> None:
    bundle, _, problem, _, pair = _m4_basis(
        repo_root,
        multi=False,
        one_decision=True,
    )
    payload = _plain(pair.candidate.provider_request)
    scenario = cast(dict[str, object], payload["scenario"])
    events = cast(list[dict[str, object]], scenario["events"])
    assert [event["target"] for event in events] == ["furnace_temperature.setpoint_ratio"]

    parameters = cast(dict[str, object], payload["parameters"])
    parameters["operating.tower_top_pressure_mpa_g"] = 0.052
    corrupted = CompiledPair(
        baseline=pair.baseline,
        candidate=replace(pair.candidate, provider_request=payload),
    )
    with pytest.raises(ValueError, match="outside the stage whitelist"):
        assert_compiled_pair(corrupted, problem, bundle.catalog)


@pytest.mark.parametrize("multi", [False, True])
def test_one_and_many_objectives_share_m4_candidate_evaluation(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    multi: bool,
) -> None:
    bundle, _, problem, proposal, pair = _m4_basis(repo_root, multi=multi)
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M4")
    candidate = make_bundle(pair.candidate.provider_request_fingerprint, stage="M4")

    result = M4PairedEvaluator(problem, bundle.catalog).evaluate(
        proposal,
        pair,
        baseline,
        candidate,
    )

    assert isinstance(result, CandidateEvaluation)
    assert result.stage == "M4"
    assert result.status == "feasible"
    assert result.objective_outcomes == ()
    assert result.metrics["m4_acceptance_passed"] == 1.0
    assert tuple(item.constraint_id for item in result.constraints) == ("m4-stability-acceptance",)
    assert result.proposal_ref == proposal.ref
    assert tuple(item.pair_role for item in result.evidence_refs) == (
        "baseline",
        "candidate",
    )


def test_m4_failure_classification_uses_complete_acceptance_evidence(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, _, problem, proposal, pair = _m4_basis(repo_root, multi=False)
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M4")
    accepted = make_bundle(pair.candidate.provider_request_fingerprint, stage="M4")
    process_failure = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M4",
        accepted=False,
    )
    successful_acceptance_failure = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M4",
        accepted=False,
        runtime_status="success",
        engine_status="success",
    )
    system_failure = replace(
        process_failure,
        failure_stage="integration",
        failure_reason="synthetic integration failure",
    )
    rejected = replace(accepted, runtime_status="rejected", summary={})
    bad_baseline = make_bundle(
        pair.baseline.provider_request_fingerprint,
        stage="M4",
        accepted=False,
    )
    evaluator = M4PairedEvaluator(problem, bundle.catalog)

    process = evaluator.evaluate(proposal, pair, baseline, process_failure)
    process_after_success = evaluator.evaluate(
        proposal,
        pair,
        baseline,
        successful_acceptance_failure,
    )
    system = evaluator.evaluate(proposal, pair, baseline, system_failure)
    invalid = evaluator.evaluate(proposal, pair, baseline, rejected)
    context_error = evaluator.evaluate(proposal, pair, bad_baseline, accepted)

    assert process.status == process_after_success.status == "process_infeasible"
    assert process.reason_codes == ("m4-stability-acceptance",)
    assert system.status == "evaluation_error"
    assert system.reason_codes == ("m4-execution-error",)
    assert invalid.status == "invalid_request"
    assert invalid.reason_codes == ("m4-request-rejected",)
    assert context_error.status == "evaluation_error"
    assert context_error.reason_codes == ("m4-baseline-invalid",)


class _M4Simulator:
    def __init__(
        self,
        context: OperatingContext,
        make_bundle: Callable[..., SimulationRunBundle],
        *,
        wrong_case: bool = False,
        baseline_accepted: bool = True,
    ) -> None:
        self._context = context
        self._make_bundle = make_bundle
        self._wrong_case = wrong_case
        self._baseline_accepted = baseline_accepted
        self.evaluate_calls = 0

    def preview(self, request: SimulationEvaluationRequest) -> SimulationPreview:
        return SimulationPreview(
            schema_version=SIMULATION_SCHEMA_VERSION,
            preview_version="fake-preview",
            simulation_request_ref=request.ref,
            provider_id=request.provider_id,
            provider_preview_fingerprint=request.fingerprint,
            effective_input_fingerprint=request.provider_request_fingerprint,
            base_object_fingerprints={
                "model": self._context.model_ref.fingerprint,
                "case": "0" * 64 if self._wrong_case else self._context.case_ref.fingerprint,
            },
            effective_object_fingerprints={},
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    def evaluate(
        self,
        request: SimulationEvaluationRequest,
        expected_preview_fingerprint: str,
    ) -> SimulationRunBundle:
        if expected_preview_fingerprint != request.fingerprint:
            raise ValueError("preview fingerprint differs")
        self.evaluate_calls += 1
        return self._make_bundle(
            request.provider_request_fingerprint,
            stage="M4",
            accepted=self._baseline_accepted if request.pair_role == "baseline" else True,
        )

    def read_evidence(self, run_ref: Path) -> SimulationRunBundle:
        raise NotImplementedError(run_ref)


def test_m4_service_caches_baseline_and_candidate_and_guards_preview_context(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, context, problem, proposal, _ = _m4_basis(repo_root, multi=True)
    simulator = _M4Simulator(context, make_bundle)
    service = M4EvaluationService(
        problem,
        context,
        bundle.catalog,
        CandidatePlanCompiler(bundle.catalog),
        CduM7RequestFactory(),
        simulator,
    )

    first = service.evaluate(proposal)
    second = service.evaluate(proposal)

    assert first is second
    assert first.status == "feasible"
    assert service.physical_execution_count == simulator.evaluate_calls == 2
    assert service.cache_hit_count == 1

    drifting = _M4Simulator(context, make_bundle, wrong_case=True)
    guarded = M4EvaluationService(
        problem,
        context,
        bundle.catalog,
        CandidatePlanCompiler(bundle.catalog),
        CduM7RequestFactory(),
        drifting,
    ).evaluate(proposal)
    assert guarded.status == "evaluation_error"
    assert guarded.reason_codes == ("simulator-or-evaluator-error",)
    assert drifting.evaluate_calls == 0


def test_m4_service_stops_after_and_caches_an_ineligible_baseline(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, context, problem, proposal, _ = _m4_basis(repo_root, multi=False)
    simulator = _M4Simulator(
        context,
        make_bundle,
        baseline_accepted=False,
    )
    service = M4EvaluationService(
        problem,
        context,
        bundle.catalog,
        CandidatePlanCompiler(bundle.catalog),
        CduM7RequestFactory(),
        simulator,
    )

    first = service.evaluate(proposal)
    second = service.evaluate(proposal)

    assert first is second
    assert first.status == "evaluation_error"
    assert first.reason_codes == ("m4-baseline-invalid",)
    assert service.physical_execution_count == simulator.evaluate_calls == 1
    assert service.cache_hit_count == 1


def test_m4_service_classifies_wrong_preset_factory_as_system_error_before_execution(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, context, problem, proposal, _ = _m4_basis(repo_root, multi=False)
    simulator = _M4Simulator(context, make_bundle)

    result = M4EvaluationService(
        problem,
        context,
        bundle.catalog,
        CandidatePlanCompiler(bundle.catalog),
        _WrongPresetFactory(stage="M4"),
        simulator,
    ).evaluate(proposal)

    assert result.status == "evaluation_error"
    assert result.reason_codes == ("system-compilation-failed",)
    assert simulator.evaluate_calls == 0


def test_builder_projects_versioned_dynamic_plan_for_both_objective_counts(
    repo_root: Path,
) -> None:
    for multi in (False, True):
        bundle, _, problem, _, _ = _m4_basis(repo_root, multi=multi)
        route = next(
            item
            for item in bundle.system_policy.execution_routes
            if item.minimum_objectives <= len(problem.objectives) <= item.maximum_objectives
        )
        plan = problem.evaluation_plan
        assert (
            plan.m2_preset_id,
            plan.m4_preset_id,
            plan.m4_event_time_s,
            plan.m4_duration_s,
            plan.m4_time_step_s,
            plan.dynamic_shortlist_size,
        ) == (
            route.m2_preset_id,
            route.m4_preset_id,
            route.m4_event_time_s,
            route.m4_duration_s,
            route.m4_time_step_s,
            route.top_k,
        )
