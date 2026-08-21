from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.capabilities import CapabilityBundle, load_capability_bundle
from petroleum_rto.rto.compilation import (
    CandidateCompilationError,
    CandidatePlanCompiler,
    CompiledPair,
    SystemCompilationError,
)
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
)
from petroleum_rto.rto.contracts.common import JsonValue
from petroleum_rto.rto.contracts.context import OperatingContext
from petroleum_rto.rto.contracts.evidence import RunEvidenceRef
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from petroleum_rto.rto.contracts.simulation import (
    SIMULATION_SCHEMA_VERSION,
    SimulationEvaluationRequest,
    SimulationPreview,
    SimulationRunBundle,
)
from petroleum_rto.rto.evaluation import (
    M2EvaluationService,
    M2PairedEvaluator,
    TrustedM2FormulaRegistry,
)
from petroleum_rto.rto.intent import OptimizationIntent, load_optimization_intent
from petroleum_rto.rto.problem import ProblemBuilder
from tests.rto.unit.test_unified_intent import _raw as _intent_raw


def _basis(
    repo_root: Path,
    *,
    multi: bool,
    one_decision: bool = False,
    objective_metric_id: str | None = None,
    objective_sense: str = "minimize",
) -> tuple[
    CapabilityBundle,
    OperatingContext,
    OptimizationProblem,
    CandidateProposal,
    CompiledPair,
]:
    bundle = load_capability_bundle(repo_root)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    intent_path = (
        repo_root / "configs/rto/intents/quality_yield_energy.json"
        if multi
        else repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    )
    intent = load_optimization_intent(intent_path)
    if objective_metric_id is not None:
        if multi:
            raise ValueError("objective_metric_id override is only for a single-objective basis")
        raw = _intent_raw(multi=False)
        raw["intent_id"] = f"single-{objective_metric_id}"
        raw["objectives"] = [
            {
                "metric_id": objective_metric_id,
                "sense": objective_sense,
                "priority": 1,
            }
        ]
        raw["preference"]["objective_order"] = [objective_metric_id]
        intent = OptimizationIntent.from_mapping(raw)
    if one_decision:
        raw = intent.as_dict()
        raw["decision_variables"] = ["furnace_temperature_target_k"]
        intent = type(intent).from_mapping(raw)
    problem = ProblemBuilder().build(bundle, intent, context)
    values = {
        domain.variable_id: (
            627.35 if domain.variable_id == "furnace_temperature_target_k" else 151325.0
        )
        for domain in reversed(problem.decision_domains)
    }
    proposal = CandidateProposal(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        proposal_version="candidate-proposal",
        candidate_id="unified-m2-fixture",
        sequence=0,
        origin="fixture",
        problem_ref=problem.ref,
        context_ref=context.ref,
        decision_values=values,
        output_kind="steady-setpoint-vector",
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    pair = CandidatePlanCompiler(bundle.catalog).compile_pair(
        problem,
        context,
        proposal,
        stage="M2",
        request_factory=CduM7RequestFactory(),
    )
    return bundle, context, problem, proposal, pair


def test_one_and_many_objectives_share_candidate_evaluation(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    for multi, expected_ids in (
        (False, ("specific_furnace_fuel_energy_mj_per_t",)),
        (
            True,
            (
                "quality_proxy_max_abs_relative_change",
                "valuable_distillate_yield",
                "specific_furnace_fuel_energy_mj_per_t",
            ),
        ),
    ):
        bundle, _, problem, proposal, pair = _basis(repo_root, multi=multi)
        baseline = make_bundle(
            pair.baseline.provider_request_fingerprint,
            stage="M2",
            objective=188.0,
        )
        candidate = make_bundle(
            pair.candidate.provider_request_fingerprint,
            stage="M2",
            objective=187.0,
            quality_scale=1.001,
            yield_delta=0.001,
        )

        result = M2PairedEvaluator(problem, bundle.catalog).evaluate(
            proposal,
            pair,
            baseline,
            candidate,
        )

        assert isinstance(result, CandidateEvaluation)
        assert result.status == "feasible"
        assert tuple(item.metric_id for item in result.objective_outcomes) == expected_ids
        assert tuple(item.pair_role for item in result.evidence_refs) == (
            "baseline",
            "candidate",
        )
        assert result.outcome_by_id(
            "specific_furnace_fuel_energy_mj_per_t"
        ).candidate_value == pytest.approx(187.0)
        relocated = replace(
            result,
            evidence_refs=tuple(
                replace(
                    item,
                    run_ref=f"/relocated/{item.pair_role}",
                    manifest_fingerprint=("0" if item.pair_role == "baseline" else "1") * 64,
                )
                for item in result.evidence_refs
            ),
        )
        assert relocated.fingerprint == result.fingerprint
        assert relocated.as_dict() != result.as_dict()


def test_formula_registry_applies_paired_constraints_and_rejects_untrusted_binding(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, _, problem, proposal, pair = _basis(repo_root, multi=True)
    baseline = make_bundle(
        pair.baseline.provider_request_fingerprint,
        stage="M2",
        objective=188.0,
    )
    candidate = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M2",
        objective=187.0,
        quality_scale=1.006,
        yield_delta=0.001,
    )

    result = M2PairedEvaluator(problem, bundle.catalog).evaluate(
        proposal,
        pair,
        baseline,
        candidate,
    )

    assert result.status == "process_infeasible"
    assert result.reason_codes == ("quality-proxy-preservation",)
    assert result.objective_outcomes
    assert result.evidence_refs
    tampered = replace(
        problem,
        objectives=(
            replace(problem.objectives[0], formula_id="unregistered-formula"),
            *problem.objectives[1:],
        ),
    )
    with pytest.raises(ValueError, match="trusted binding"):
        M2PairedEvaluator(tampered, bundle.catalog, TrustedM2FormulaRegistry())


@pytest.mark.parametrize(
    ("metric_id", "sense"),
    [
        ("quality_proxy_max_abs_relative_change", "minimize"),
        ("valuable_distillate_yield", "maximize"),
    ],
)
def test_publishability_metric_is_computed_for_non_energy_single_objectives_without_m2_gating(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    metric_id: str,
    sense: str,
) -> None:
    bundle, _, problem, proposal, pair = _basis(
        repo_root,
        multi=False,
        objective_metric_id=metric_id,
        objective_sense=sense,
    )
    baseline = make_bundle(
        pair.baseline.provider_request_fingerprint,
        stage="M2",
        objective=188.0,
    )
    candidate = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M2",
        objective=188.0,
        quality_scale=1.001,
        yield_delta=0.001,
    )

    result = M2PairedEvaluator(problem, bundle.catalog).evaluate(
        proposal,
        pair,
        baseline,
        candidate,
    )

    assert tuple(item.metric_id for item in result.objective_outcomes) == (metric_id,)
    assert result.metrics["specific_furnace_fuel_energy_mj_per_t"] == pytest.approx(188.0)
    assert result.metrics["specific_furnace_fuel_improvement_fraction"] == pytest.approx(0.0)
    assert result.status == "feasible"
    assert "minimum-publishable-energy-improvement" not in {
        item.constraint_id for item in result.constraints
    }


def test_evidence_semantic_fingerprint_ignores_audit_location_and_manifest(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    _, _, _, _, pair = _basis(repo_root, multi=False)
    bundle = make_bundle(pair.baseline.provider_request_fingerprint, stage="M2")
    evidence = RunEvidenceRef.from_bundle(bundle, pair_role="baseline")

    assert replace(evidence, run_ref="/relocated/evidence").fingerprint == evidence.fingerprint
    changed_manifest = replace(evidence, manifest_fingerprint="e" * 64)
    assert changed_manifest.fingerprint == evidence.fingerprint
    assert changed_manifest.as_dict() != evidence.as_dict()
    semantic_changes = (
        replace(evidence, provider_request_fingerprint="a" * 64),
        replace(evidence, request_fingerprint="b" * 64),
        replace(evidence, effective_input_fingerprint="c" * 64),
        replace(evidence, result_fingerprint="d" * 64),
        replace(evidence, versions={**evidence.versions, "runtime": "changed"}),
        replace(evidence, source_fingerprints={**evidence.source_fingerprints, "model": "f" * 64}),
    )
    assert all(item.fingerprint != evidence.fingerprint for item in semantic_changes)
    assert RunEvidenceRef.from_mapping(evidence.as_dict()) == evidence


def test_atomic_decision_compilation_preserves_unselected_context_setpoint(
    repo_root: Path,
) -> None:
    _, _, _, _, pair = _basis(repo_root, multi=False, one_decision=True)
    baseline = pair.baseline.provider_request["parameters"]
    candidate = pair.candidate.provider_request["parameters"]

    assert isinstance(baseline, Mapping)
    assert isinstance(candidate, Mapping)
    assert baseline["operating.tower_top_pressure_mpa_g"] == 0.051
    assert candidate["operating.tower_top_pressure_mpa_g"] == 0.051
    assert baseline["operating.furnace_outlet_temperature_c"] == 355.2
    assert candidate["operating.furnace_outlet_temperature_c"] == 354.2


class _Simulator:
    def __init__(
        self,
        context: OperatingContext,
        make_bundle: Callable[..., SimulationRunBundle],
        *,
        wrong_model: bool = False,
    ) -> None:
        self._context = context
        self._make_bundle = make_bundle
        self._wrong_model = wrong_model
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
                "model": "0" * 64 if self._wrong_model else self._context.model_ref.fingerprint,
                "case": self._context.case_ref.fingerprint,
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
            stage="M2",
            objective=188.0 if request.pair_role == "baseline" else 187.0,
            quality_scale=1.0 if request.pair_role == "baseline" else 1.001,
            yield_delta=0.0 if request.pair_role == "baseline" else 0.001,
        )

    def read_evidence(self, run_ref: Path) -> SimulationRunBundle:
        raise NotImplementedError(run_ref)


class _WrongPresetFactory:
    def __init__(self, *, stage: str) -> None:
        self._delegate = CduM7RequestFactory()
        self._stage = stage

    @property
    def provider_id(self) -> str:
        return self._delegate.provider_id

    @property
    def compiler_version(self) -> str:
        return self._delegate.compiler_version

    def build_m2_request(
        self,
        context: OperatingContext,
        decision_values: Mapping[str, float],
    ) -> Mapping[str, JsonValue]:
        payload = dict(self._delegate.build_m2_request(context, decision_values))
        if self._stage == "M2":
            payload["preset_id"] = "wrong-steady-preset"
        return payload

    def build_m4_request(
        self,
        context: OperatingContext,
        decision_values: Mapping[str, float],
        *,
        candidate: bool,
        event_time_s: float,
        duration_s: float,
        time_step_s: float,
    ) -> Mapping[str, JsonValue]:
        payload = dict(
            self._delegate.build_m4_request(
                context,
                decision_values,
                candidate=candidate,
                event_time_s=event_time_s,
                duration_s=duration_s,
                time_step_s=time_step_s,
            )
        )
        if self._stage == "M4":
            payload["preset_id"] = "wrong-dynamic-preset"
        return payload


def test_service_caches_pair_and_rejects_preview_context_drift_before_execution(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, context, problem, proposal, _ = _basis(repo_root, multi=False)
    simulator = _Simulator(context, make_bundle)
    service = M2EvaluationService(
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

    drifting = _Simulator(context, make_bundle, wrong_model=True)
    guarded = M2EvaluationService(
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


def test_m2_service_separates_candidate_errors_from_factory_configuration_errors(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    bundle, context, problem, proposal, _ = _basis(repo_root, multi=False)
    compiler = CandidatePlanCompiler(bundle.catalog)
    invalid_proposal = replace(
        proposal,
        decision_values={
            **proposal.decision_values,
            "furnace_temperature_target_k": 700.0,
        },
    )
    with pytest.raises(CandidateCompilationError, match="outside the local domain"):
        compiler.compile_pair(
            problem,
            context,
            invalid_proposal,
            stage="M2",
            request_factory=CduM7RequestFactory(),
        )
    with pytest.raises(SystemCompilationError, match="factory failed"):
        compiler.compile_pair(
            problem,
            context,
            proposal,
            stage="M2",
            request_factory=_WrongPresetFactory(stage="M2"),
        )

    candidate_simulator = _Simulator(context, make_bundle)
    invalid = M2EvaluationService(
        problem,
        context,
        bundle.catalog,
        compiler,
        CduM7RequestFactory(),
        candidate_simulator,
    ).evaluate(invalid_proposal)
    assert invalid.status == "invalid_request"
    assert invalid.reason_codes == ("candidate-compilation-failed",)
    assert candidate_simulator.evaluate_calls == 0

    system_simulator = _Simulator(context, make_bundle)
    system_error = M2EvaluationService(
        problem,
        context,
        bundle.catalog,
        compiler,
        _WrongPresetFactory(stage="M2"),
        system_simulator,
    ).evaluate(proposal)
    assert system_error.status == "evaluation_error"
    assert system_error.reason_codes == ("system-compilation-failed",)
    assert system_simulator.evaluate_calls == 0


def test_builder_rejects_unbound_business_constraint(repo_root: Path) -> None:
    bundle = load_capability_bundle(repo_root)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    intent = load_optimization_intent(
        repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    )
    raw = intent.as_dict()
    raw["constraints"] = ["quality-proxy-preservation"]

    with pytest.raises(ValueError, match="business constraint bindings"):
        ProblemBuilder().build(bundle, type(intent).from_mapping(raw), context)
