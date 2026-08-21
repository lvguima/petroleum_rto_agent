from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.catalogs import RtoCatalogBundleV2, load_rto_v2_bundle
from petroleum_rto.rto.compilation import MultiObjectiveCandidatePlanCompiler
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_V2_SCHEMA_VERSION,
    CandidateEvaluationV2,
    CandidateProposalV2,
    ConstraintOutcomeV1,
    DynamicVerificationV2,
    ObjectiveOutcomeV2,
    OptimizationProblemV2,
    OptimizationResultV2,
    PreferenceSelectionV2,
    RunEvidenceRefV1,
    SimulationRunBundleV1,
)
from petroleum_rto.rto.evaluation import MultiObjectiveDynamicPairedEvaluator
from petroleum_rto.rto.inputs import (
    bind_external_optimization_request_v2,
    load_external_optimization_request_v2,
)
from petroleum_rto.rto.optimizer import (
    DeterministicParetoGridOptimizer,
    MultiObjectiveDynamicFinalSelector,
    ParetoPreferenceSelector,
)


def _basis(repo_root: Path) -> tuple[RtoCatalogBundleV2, OptimizationProblemV2]:
    bundle = load_rto_v2_bundle(repo_root)
    request = load_external_optimization_request_v2(
        repo_root / "configs/rto/requests/multiobjective_example_v2.json"
    )
    bound = bind_external_optimization_request_v2(bundle, request)
    return bound.bundle, bound.problem


def _evidence(seed: str) -> RunEvidenceRefV1:
    return RunEvidenceRefV1(
        provider_id="fake-provider",
        run_ref=f"/tmp/{seed}",
        provider_request_fingerprint=seed * 64,
        request_fingerprint=seed * 64,
        effective_input_fingerprint=seed * 64,
        result_fingerprint=seed * 64,
        manifest_fingerprint=seed * 64,
        versions={"model": "v1"},
        source_fingerprints={"source": seed * 64},
    )


def _static_evaluation(
    problem: OptimizationProblemV2,
    proposal: CandidateProposalV2,
    *,
    energy: float,
) -> CandidateEvaluationV2:
    baselines = (0.0, 0.49, 188.0)
    values = (
        proposal.sequence / 100_000.0,
        0.49 + proposal.sequence / 1_000_000.0,
        energy,
    )
    outcomes = tuple(
        ObjectiveOutcomeV2(
            metric_id=spec.metric_id,
            sense=spec.sense,
            unit=spec.unit,
            kpi_formula_id=spec.kpi_formula_id,
            baseline_value=baseline,
            candidate_value=value,
            directional_absolute_improvement=(
                baseline - value if spec.sense == "minimize" else value - baseline
            ),
            relative_directional_improvement=(
                None
                if spec.relative_improvement_policy == "zero-baseline-null"
                else (
                    (baseline - value if spec.sense == "minimize" else value - baseline)
                    / abs(baseline)
                )
            ),
            relative_unavailable_reason=(
                "zero-baseline"
                if spec.relative_improvement_policy == "zero-baseline-null"
                else None
            ),
            normalized_directional_improvement=(
                (baseline - value if spec.sense == "minimize" else value - baseline)
                / spec.normalization_scale
            ),
        )
        for spec, baseline, value in zip(problem.objectives, baselines, values, strict=True)
    )
    constraint = ConstraintOutcomeV1(
        constraint_id="fixture-m2-gate",
        stage="M2",
        metric_id="m2_evaluable",
        operator="ge",
        limit=1.0,
        candidate_value=1.0,
        baseline_value=1.0,
        normalized_margin=1.0,
        passed=True,
    )
    return CandidateEvaluationV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        evaluation_version="candidate-evaluation-v2",
        stage="M2",
        status="feasible",
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-v2-m2-{proposal.fingerprint[:16]}",
        objective_outcomes=outcomes,
        metrics={"m2_evaluable": 1.0},
        constraints=(constraint,),
        minimum_normalized_margin=1.0,
        normalized_action_l1=proposal.sequence / 100.0,
        reason_codes=(),
        baseline_evidence=_evidence("a"),
        candidate_evidence=_evidence("b"),
        claim_scope=CLAIM_SCOPE,
    )


class _StaticEvaluator:
    def __init__(self, problem: OptimizationProblemV2, *, energy: float) -> None:
        self._problem = problem
        self._energy = energy

    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        return _static_evaluation(self._problem, proposal, energy=self._energy)


def _dynamic_evaluation(
    problem: OptimizationProblemV2,
    proposal: CandidateProposalV2,
    status: str,
) -> CandidateEvaluationV2:
    if status not in {"feasible", "process_infeasible", "evaluation_error"}:
        raise ValueError("bad fixture status")
    feasible = status == "feasible"
    error = status == "evaluation_error"
    constraint = ConstraintOutcomeV1(
        constraint_id="fixture-m4-gate",
        stage="M4",
        metric_id="m4_acceptance_passed",
        operator="ge",
        limit=1.0,
        candidate_value=1.0 if feasible else 0.0,
        baseline_value=1.0,
        normalized_margin=0.0 if feasible else -1.0,
        passed=feasible,
    )
    return CandidateEvaluationV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        evaluation_version="candidate-evaluation-v2",
        stage="M4",
        status=cast(Any, status),
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-v2-m4-{proposal.fingerprint[:16]}",
        objective_outcomes=(),
        metrics={} if error else {"m4_acceptance_passed": 1.0 if feasible else 0.0},
        constraints=() if error else (constraint,),
        minimum_normalized_margin=None if error else constraint.normalized_margin,
        normalized_action_l1=proposal.sequence / 100.0,
        reason_codes=() if feasible else ("fixture-dynamic-failure",),
        baseline_evidence=None if error else _evidence("c"),
        candidate_evidence=None if error else _evidence("d"),
        claim_scope=CLAIM_SCOPE,
    )


class _DynamicEvaluator:
    def __init__(
        self,
        problem: OptimizationProblemV2,
        status_for_index: Callable[[int], str],
    ) -> None:
        self._problem = problem
        self._status_for_index = status_for_index
        self.calls: list[CandidateProposalV2] = []

    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        self.calls.append(proposal)
        return _dynamic_evaluation(
            self._problem,
            proposal,
            self._status_for_index(len(self.calls) - 1),
        )


def _search_and_preference(
    repo_root: Path,
    *,
    energy: float = 180.0,
) -> tuple[
    RtoCatalogBundleV2,
    OptimizationProblemV2,
    object,
    PreferenceSelectionV2,
]:
    bundle, problem = _basis(repo_root)
    search = DeterministicParetoGridOptimizer().search(
        problem,
        bundle.base.context,
        _StaticEvaluator(problem, energy=energy),
    )
    profile = bundle.preference_catalog.profile_by_id(problem.preference_profile_id)
    preference = ParetoPreferenceSelector().select(problem, search, profile)
    return bundle, problem, search, preference


def test_lexicographic_preference_is_explicit_and_deterministic(repo_root: Path) -> None:
    _, _, search, preference = _search_and_preference(repo_root)

    from petroleum_rto.rto.contracts import ParetoSearchResultV2

    assert isinstance(search, ParetoSearchResultV2)
    assert preference.status == "success"
    assert len(preference.shortlist_refs) == 5
    proposals = {item.ref: item for item in search.proposals}
    assert [proposals[ref].sequence for ref in preference.shortlist_refs] == [0, 1, 2, 3, 4]
    assert PreferenceSelectionV2.from_mapping(preference.as_dict()) == preference


def test_v2_dynamic_evaluator_reuses_existing_m4_acceptance_without_objectives(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle, problem = _basis(repo_root)
    proposal = DeterministicParetoGridOptimizer._proposal(
        problem,
        bundle.base.context,
        {
            "furnace_temperature_target_k": 627.35,
            "tower_top_pressure_target_pa_a": 151325.0,
        },
        sequence=0,
    )
    pair = MultiObjectiveCandidatePlanCompiler().compile_pair(
        problem,
        bundle.base.context,
        proposal,
        stage="M4",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M4")
    candidate = make_bundle(pair.candidate.provider_request_fingerprint, stage="M4")
    baseline = replace(
        baseline,
        versions={**baseline.versions, "scenario_version": "baseline-empty-events"},
    )
    candidate = replace(
        candidate,
        versions={**candidate.versions, "scenario_version": "candidate-setpoint-events"},
    )

    result = MultiObjectiveDynamicPairedEvaluator(bundle.base.kpi_catalog).evaluate(
        problem, proposal, pair, baseline, candidate
    )

    assert result.status == "feasible"
    assert result.objective_outcomes == ()
    assert result.metrics["m4_acceptance_passed"] == 1.0
    assert CandidateEvaluationV2.from_mapping(result.as_dict()) == result


def test_dynamic_failure_falls_back_but_all_top_five_are_evaluated(
    repo_root: Path,
) -> None:
    bundle, problem, search, preference = _search_and_preference(repo_root)
    from petroleum_rto.rto.contracts import ParetoSearchResultV2

    assert isinstance(search, ParetoSearchResultV2)
    evaluator = _DynamicEvaluator(
        problem,
        lambda index: "process_infeasible" if index == 0 else "feasible",
    )
    publishability = bundle.publishability_catalog.profile_by_id(problem.publishability_profile_id)

    verification, result = MultiObjectiveDynamicFinalSelector().select(
        problem, search, preference, publishability, evaluator
    )

    assert verification is not None
    assert verification.status == "success"
    assert len(evaluator.calls) == 5
    assert result.status == "success"
    assert result.selected_proposal_ref == evaluator.calls[1].ref
    assert result.publishable
    assert DynamicVerificationV2.from_mapping(verification.as_dict()) == verification
    assert OptimizationResultV2.from_mapping(result.as_dict()) == result


def test_all_dynamic_failures_do_not_claim_global_infeasibility(repo_root: Path) -> None:
    bundle, problem, search, preference = _search_and_preference(repo_root)
    from petroleum_rto.rto.contracts import ParetoSearchResultV2

    assert isinstance(search, ParetoSearchResultV2)
    evaluator = _DynamicEvaluator(problem, lambda _: "process_infeasible")
    verification, result = MultiObjectiveDynamicFinalSelector().select(
        problem,
        search,
        preference,
        bundle.publishability_catalog.profile_by_id(problem.publishability_profile_id),
        evaluator,
    )

    assert verification is not None
    assert verification.status == "pareto_shortlist_dynamic_failed"
    assert result.status == "pareto_shortlist_dynamic_failed"
    assert result.selected_proposal_ref is None
    assert len(evaluator.calls) == 5


def test_system_error_is_not_downgraded_to_dynamic_infeasibility(repo_root: Path) -> None:
    bundle, problem, search, preference = _search_and_preference(repo_root)
    from petroleum_rto.rto.contracts import ParetoSearchResultV2

    assert isinstance(search, ParetoSearchResultV2)
    evaluator = _DynamicEvaluator(
        problem,
        lambda index: "evaluation_error" if index == 2 else "feasible",
    )
    verification, result = MultiObjectiveDynamicFinalSelector().select(
        problem,
        search,
        preference,
        bundle.publishability_catalog.profile_by_id(problem.publishability_profile_id),
        evaluator,
    )

    assert verification is not None
    assert verification.status == "evaluation_error"
    assert result.status == "evaluation_error"
    assert result.selected_proposal_ref is None


def test_publishability_is_post_selection_and_preserves_selected_result(
    repo_root: Path,
) -> None:
    bundle, problem, search, preference = _search_and_preference(repo_root, energy=187.5)
    from petroleum_rto.rto.contracts import ParetoSearchResultV2

    assert isinstance(search, ParetoSearchResultV2)
    evaluator = _DynamicEvaluator(problem, lambda _: "feasible")
    verification, result = MultiObjectiveDynamicFinalSelector().select(
        problem,
        search,
        preference,
        bundle.publishability_catalog.profile_by_id(problem.publishability_profile_id),
        evaluator,
    )

    assert verification is not None and verification.status == "success"
    assert result.status == "feasible_not_publishable"
    assert result.selected_proposal_ref is not None
    assert result.selected_objectives
    assert not result.publishable
