from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from petroleum_rto.rto.capabilities import UnifiedCapabilityBundle, load_capability_bundle
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
    ConstraintOutcome,
    ObjectiveOutcome,
)
from petroleum_rto.rto.contracts.evidence import (
    RUN_EVIDENCE_SCHEMA_VERSION,
    PairRole,
    RunEvidenceRef,
)
from petroleum_rto.rto.contracts.finalization import (
    FinalizationResult,
    PublishabilityAssessment,
    StaticPreferenceSelection,
)
from petroleum_rto.rto.contracts.problem import (
    ENGINEERING_CLAIM_SCOPE,
    OptimizationProblem,
    SelectionPreference,
)
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.contracts.solver_result import (
    SOLVER_RESULT_SCHEMA_VERSION,
    SolutionGroup,
    SolverResult,
)
from petroleum_rto.rto.problem import UnifiedProblemBuilder
from petroleum_rto.rto.selection import UnifiedFinalSelector
from petroleum_rto.rto.unified_inputs import load_optimization_intent

DynamicStatus = Literal[
    "feasible",
    "process_infeasible",
    "invalid_request",
    "evaluation_error",
    "not_evaluated",
]


def _basis(
    repo_root: Path,
    *,
    multi: bool,
) -> tuple[UnifiedCapabilityBundle, OptimizationProblem]:
    bundle = load_capability_bundle(repo_root)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    intent = load_optimization_intent(
        repo_root
        / "configs/rto/intents"
        / ("quality_yield_energy.json" if multi else "minimize_specific_furnace_energy.json")
    )
    return bundle, UnifiedProblemBuilder().build(bundle, intent, context)


def _evidence(role: PairRole) -> RunEvidenceRef:
    digit = "1" if role == "baseline" else "2"
    return RunEvidenceRef(
        schema_version=RUN_EVIDENCE_SCHEMA_VERSION,
        evidence_version="synthetic-evidence",
        pair_role=role,
        provider_id="synthetic-provider",
        run_ref=f"/synthetic/{role}",
        provider_request_fingerprint=digit * 64,
        request_fingerprint=digit * 64,
        effective_input_fingerprint=digit * 64,
        result_fingerprint=digit * 64,
        manifest_fingerprint=digit * 64,
        versions={"model": "synthetic"},
        source_fingerprints={"source": digit * 64},
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


_PAIRED_EVIDENCE = (_evidence("baseline"), _evidence("candidate"))


def _proposal(problem: OptimizationProblem, index: int) -> CandidateProposal:
    values = {
        item.variable_id: (
            item.lower_bound + 0.25 * index
            if item.variable_id == "furnace_temperature_target_k"
            else item.nominal_value
        )
        for item in problem.decision_domains
    }
    return CandidateProposal(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        proposal_version="synthetic-proposal",
        candidate_id=f"candidate-{index}",
        sequence=index,
        origin="unit-test",
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        decision_values=values,
        output_kind="steady-setpoint-vector",
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _static_evaluation(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
    values: tuple[float, ...],
    *,
    margin: float = 1.0,
    action_l1: float = 0.0,
    publish_improvement: float | None = 0.006,
) -> CandidateEvaluation:
    outcomes: list[ObjectiveOutcome] = []
    for objective, candidate in zip(problem.objectives, values, strict=True):
        baseline = candidate + 1.0 if objective.sense == "minimize" else candidate - 1.0
        outcomes.append(
            ObjectiveOutcome(
                metric_id=objective.metric_id,
                sense=objective.sense,
                unit=objective.unit,
                formula_id=objective.formula_id,
                baseline_value=baseline,
                candidate_value=candidate,
                directional_absolute_improvement=1.0,
                relative_directional_improvement=None,
                normalized_directional_improvement=1.0,
            )
        )
    metrics = {item.metric_id: item.candidate_value for item in outcomes}
    if publish_improvement is not None:
        metrics["specific_furnace_fuel_improvement_fraction"] = publish_improvement
    return CandidateEvaluation(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        evaluation_version="synthetic-m2-evaluation",
        stage="M2",
        status="feasible",
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-{proposal.fingerprint[:16]}",
        objective_outcomes=tuple(outcomes),
        metrics=metrics,
        constraints=(
            ConstraintOutcome(
                constraint_id="m2-structural-numeric",
                metric_id="m2_evaluable",
                raw_value=1.0,
                limit=1.0,
                normalized_margin=margin,
                passed=True,
            ),
        ),
        minimum_normalized_margin=margin,
        normalized_action_l1=action_l1,
        reason_codes=(),
        evidence_refs=_PAIRED_EVIDENCE,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _static_failure(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        evaluation_version="synthetic-m2-evaluation",
        stage="M2",
        status="process_infeasible",
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-{proposal.fingerprint[:16]}",
        objective_outcomes=(),
        metrics={},
        constraints=(),
        minimum_normalized_margin=None,
        normalized_action_l1=0.0,
        reason_codes=("m2-structural-numeric",),
        evidence_refs=(),
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _dynamic_evaluation(
    problem: OptimizationProblem,
    proposal_ref: ContractRef,
    status: DynamicStatus,
) -> CandidateEvaluation:
    reason = {
        "feasible": (),
        "process_infeasible": ("m4-stability-acceptance",),
        "invalid_request": ("m4-request-rejected",),
        "evaluation_error": ("m4-execution-error",),
        "not_evaluated": ("m4-not-evaluated",),
    }[status]
    evaluated = status in {"feasible", "process_infeasible"}
    passed = status == "feasible"
    return CandidateEvaluation(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        evaluation_version="synthetic-m4-evaluation",
        stage="M4",
        status=status,
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal_ref,
        pair_id=f"pair-{proposal_ref.fingerprint[:16]}",
        objective_outcomes=(),
        metrics={"m4_acceptance_passed": 1.0 if passed else 0.0} if evaluated else {},
        constraints=(
            ConstraintOutcome(
                constraint_id="m4-stability-acceptance",
                metric_id="m4_acceptance_passed",
                raw_value=1.0 if passed else 0.0,
                limit=1.0,
                normalized_margin=1.0 if passed else -1.0,
                passed=passed,
            ),
        )
        if evaluated
        else (),
        minimum_normalized_margin=(1.0 if passed else -1.0) if evaluated else None,
        normalized_action_l1=0.0,
        reason_codes=reason,
        evidence_refs=_PAIRED_EVIDENCE if evaluated else (),
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _solver_result(
    problem: OptimizationProblem,
    proposals: tuple[CandidateProposal, ...],
    evaluations: tuple[CandidateEvaluation, ...],
    *,
    representation: Literal["ordered", "layered"] = "ordered",
    groups: tuple[tuple[ContractRef, ...], ...] | None = None,
    status: Literal[
        "success",
        "no_static_feasible",
        "evaluation_error",
        "unsupported_problem",
    ] = "success",
) -> SolverResult:
    raw_groups = tuple((item.ref,) for item in evaluations) if groups is None else groups
    return SolverResult(
        schema_version=SOLVER_RESULT_SCHEMA_VERSION,
        result_version="synthetic-solver-result",
        status=status,
        problem_ref=problem.ref,
        solver_ref=ContractRef("synthetic-solver", "a" * 64),
        proposals=proposals,
        evaluations=evaluations,
        solution_representation=representation,
        solution_groups=(
            tuple(
                SolutionGroup(rank=index, evaluation_refs=refs)
                for index, refs in enumerate(raw_groups, start=1)
            )
            if status == "success"
            else ()
        ),
        termination_reason=("solver-success" if status == "success" else "solver-terminal"),
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _mapping(
    evaluations: tuple[CandidateEvaluation, ...],
) -> dict[ContractRef, CandidateEvaluation]:
    return {item.proposal_ref: item for item in evaluations}


def test_single_objective_ranks_verifies_and_returns_only_selected(
    repo_root: Path,
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, (value,))
        for proposal, value in zip(proposals, (189.0, 187.0, 188.0), strict=True)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, "feasible")
        for ref in selection.shortlist_proposal_refs
    }

    artifacts = selector.select(problem, solver, _mapping(static), dynamic, bundle)

    assert artifacts.result.status == "success"
    assert artifacts.result.selected_proposal_ref == proposals[1].ref
    assert artifacts.result.returned_proposal_refs == (proposals[1].ref,)
    assert artifacts.result.ranked_proposal_refs == (
        proposals[1].ref,
        proposals[2].ref,
        proposals[0].ref,
    )
    assert StaticPreferenceSelection.from_mapping(selection.as_dict()) == selection
    assert artifacts.publishability is not None
    assert (
        PublishabilityAssessment.from_mapping(artifacts.publishability.as_dict())
        == artifacts.publishability
    )
    assert FinalizationResult.from_mapping(artifacts.result.as_dict()) == artifacts.result


def test_three_objective_order_honors_sense_and_output_cap(repo_root: Path) -> None:
    bundle, base = _basis(repo_root, multi=True)
    problem = replace(
        base,
        result_request=replace(base.result_request, maximum_returned_candidates=2),
    )
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = (
        _static_evaluation(problem, proposals[0], (0.001, 0.20, 190.0)),
        _static_evaluation(problem, proposals[1], (0.001, 0.30, 195.0)),
        _static_evaluation(problem, proposals[2], (0.002, 0.90, 100.0)),
    )
    solver = _solver_result(
        problem,
        proposals,
        static,
        representation="layered",
        groups=((static[2].ref, static[0].ref, static[1].ref),),
    )
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, "feasible")
        for ref in selection.shortlist_proposal_refs
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert selection.ranked_proposal_refs == (
        proposals[1].ref,
        proposals[0].ref,
        proposals[2].ref,
    )
    assert result.returned_proposal_refs == (proposals[1].ref, proposals[0].ref)
    assert result.selected_proposal_ref == proposals[1].ref
    tampered = result.as_dict()
    tampered["returned_proposal_refs"] = [item.as_dict() for item in result.ranked_proposal_refs]
    with pytest.raises(ValueError, match="output limit"):
        FinalizationResult.from_mapping(tampered)


def test_single_objective_can_return_ranked_alternatives_and_selected(
    repo_root: Path,
) -> None:
    bundle, base = _basis(repo_root, multi=False)
    problem = replace(
        base,
        result_request=replace(
            base.result_request,
            mode="ranked-and-selected",
            maximum_returned_candidates=2,
        ),
    )
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, (value,))
        for proposal, value in zip(proposals, (189.0, 187.0, 188.0), strict=True)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, "feasible")
        for ref in selection.shortlist_proposal_refs
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert result.selected_proposal_ref == proposals[1].ref
    assert result.returned_proposal_refs == (proposals[1].ref, proposals[2].ref)
    assert FinalizationResult.from_mapping(result.as_dict()) == result


def test_multiobjective_selected_mode_returns_only_the_dynamic_selection(
    repo_root: Path,
) -> None:
    bundle, base = _basis(repo_root, multi=True)
    problem = replace(
        base,
        result_request=replace(
            base.result_request,
            mode="selected",
            maximum_returned_candidates=1,
        ),
    )
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = (
        _static_evaluation(problem, proposals[0], (0.001, 0.20, 190.0)),
        _static_evaluation(problem, proposals[1], (0.001, 0.30, 195.0)),
        _static_evaluation(problem, proposals[2], (0.002, 0.90, 100.0)),
    )
    solver = _solver_result(
        problem,
        proposals,
        static,
        representation="layered",
        groups=((static[2].ref, static[0].ref, static[1].ref),),
    )
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, "feasible")
        for ref in selection.shortlist_proposal_refs
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert result.selected_proposal_ref == proposals[1].ref
    assert result.returned_proposal_refs == (proposals[1].ref,)


def test_alternative_refs_are_static_and_keep_failed_dynamic_candidates_auditable(
    repo_root: Path,
) -> None:
    bundle, base = _basis(repo_root, multi=True)
    problem = replace(
        base,
        result_request=replace(base.result_request, maximum_returned_candidates=2),
    )
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, values)
        for proposal, values in zip(
            proposals,
            ((0.001, 0.30, 190.0), (0.002, 0.40, 180.0), (0.003, 0.50, 170.0)),
            strict=True,
        )
    )
    solver = _solver_result(
        problem,
        proposals,
        static,
        representation="layered",
        groups=((static[0].ref, static[1].ref, static[2].ref),),
    )
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    statuses: tuple[DynamicStatus, ...] = (
        "process_infeasible",
        "feasible",
        "feasible",
    )
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, status)
        for ref, status in zip(selection.shortlist_proposal_refs, statuses, strict=True)
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert result.returned_proposal_refs == selection.ranked_proposal_refs[:2]
    assert dynamic[result.returned_proposal_refs[0]].status == "process_infeasible"
    assert result.selected_proposal_ref == selection.ranked_proposal_refs[1]


def test_atomic_tie_breaks_are_applied_in_declared_order(repo_root: Path) -> None:
    _, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(4))
    static = (
        _static_evaluation(problem, proposals[0], (100.0,), margin=0.5, action_l1=0.1),
        _static_evaluation(problem, proposals[1], (100.0,), margin=0.8, action_l1=0.9),
        _static_evaluation(problem, proposals[2], (100.0,), margin=0.8, action_l1=0.2),
        _static_evaluation(problem, proposals[3], (100.0,), margin=0.8, action_l1=0.2),
    )
    solver = _solver_result(problem, proposals, static)

    ranking = (
        UnifiedFinalSelector()
        .rank_static(
            problem,
            solver,
            _mapping(static),
        )
        .ranked_proposal_refs
    )

    fingerprint_order = tuple(
        sorted((proposals[2].ref, proposals[3].ref), key=lambda item: item.fingerprint)
    )
    assert ranking == (*fingerprint_order, proposals[1].ref, proposals[0].ref)


def test_layered_solver_only_exposes_first_pareto_front(repo_root: Path) -> None:
    _, base = _basis(repo_root, multi=True)
    problem = replace(
        base,
        evaluation_plan=replace(base.evaluation_plan, dynamic_shortlist_size=2),
    )
    proposals = tuple(_proposal(problem, index) for index in range(3))
    first_a = _static_evaluation(problem, proposals[0], (0.0010, 0.40, 190.0))
    first_b = _static_evaluation(problem, proposals[1], (0.0020, 0.80, 180.0))
    dominated = _static_evaluation(problem, proposals[2], (0.0015, 0.30, 195.0))
    static = (first_a, first_b, dominated)
    solver = _solver_result(
        problem,
        proposals,
        static,
        representation="layered",
        groups=((first_a.ref, first_b.ref), (dominated.ref,)),
    )

    selection = UnifiedFinalSelector().rank_static(problem, solver, _mapping(static))

    assert selection.ranked_proposal_refs == (proposals[0].ref, proposals[1].ref)
    assert proposals[2].ref not in selection.shortlist_proposal_refs


def test_top_three_failure_does_not_claim_fourth_or_global_infeasibility(
    repo_root: Path,
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(4))
    static = tuple(
        _static_evaluation(problem, proposal, (float(index),))
        for index, proposal in enumerate(proposals)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, "process_infeasible")
        for ref in selection.shortlist_proposal_refs
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert len(selection.shortlist_proposal_refs) == 3
    assert proposals[3].ref not in result.dynamic_proposal_refs
    assert result.status == "no_verified_candidate"
    assert result.termination_reason == "verification-budget-exhausted"
    assert result.returned_proposal_refs == ()


@pytest.mark.parametrize("failure_status", ["invalid_request", "evaluation_error"])
def test_dynamic_system_status_wins_over_other_feasible_candidates(
    repo_root: Path,
    failure_status: Literal["invalid_request", "evaluation_error"],
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, (float(index),))
        for index, proposal in enumerate(proposals)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    statuses: tuple[DynamicStatus, ...] = ("feasible", failure_status, "feasible")
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, status)
        for ref, status in zip(selection.shortlist_proposal_refs, statuses, strict=True)
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert result.status == failure_status
    assert result.selected_proposal_ref is None
    assert result.returned_proposal_refs == ()


def test_first_dynamic_failure_falls_back_after_full_shortlist(repo_root: Path) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, (float(index),))
        for index, proposal in enumerate(proposals)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    statuses: tuple[DynamicStatus, ...] = (
        "process_infeasible",
        "feasible",
        "feasible",
    )
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, status)
        for ref, status in zip(selection.shortlist_proposal_refs, statuses, strict=True)
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert len(result.dynamic_evaluation_refs) == 3
    assert result.status == "success"
    assert result.selected_proposal_ref == selection.shortlist_proposal_refs[1]
    assert result.returned_proposal_refs == (selection.shortlist_proposal_refs[1],)


def test_full_shortlist_is_required_before_any_candidate_can_be_selected(
    repo_root: Path,
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, (float(index),))
        for index, proposal in enumerate(proposals)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    first = selection.shortlist_proposal_refs[0]

    result = selector.select(
        problem,
        solver,
        _mapping(static),
        {first: _dynamic_evaluation(problem, first, "feasible")},
        bundle,
    ).result

    assert result.status == "evaluation_error"
    assert result.termination_reason == "dynamic-evaluation-incomplete"
    assert result.selected_proposal_ref is None


def test_not_evaluated_prevents_success_even_when_first_candidate_is_feasible(
    repo_root: Path,
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(3))
    static = tuple(
        _static_evaluation(problem, proposal, (float(index),))
        for index, proposal in enumerate(proposals)
    )
    solver = _solver_result(problem, proposals, static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    statuses: tuple[DynamicStatus, ...] = (
        "feasible",
        "not_evaluated",
        "feasible",
    )
    dynamic = {
        ref: _dynamic_evaluation(problem, ref, status)
        for ref, status in zip(selection.shortlist_proposal_refs, statuses, strict=True)
    }

    result = selector.select(problem, solver, _mapping(static), dynamic, bundle).result

    assert result.status == "evaluation_error"
    assert result.termination_reason == "dynamic-evaluation-incomplete"
    assert result.selected_proposal_ref is None


@pytest.mark.parametrize("publish_improvement", [0.001, None])
def test_publishability_is_independent_and_requires_explicit_m2_metric(
    repo_root: Path,
    publish_improvement: float | None,
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposal = _proposal(problem, 0)
    static = (
        _static_evaluation(
            problem,
            proposal,
            (100.0,),
            publish_improvement=publish_improvement,
        ),
    )
    solver = _solver_result(problem, (proposal,), static)
    dynamic = {proposal.ref: _dynamic_evaluation(problem, proposal.ref, "feasible")}

    artifacts = UnifiedFinalSelector().select(
        problem,
        solver,
        _mapping(static),
        dynamic,
        bundle,
    )

    assert artifacts.result.status == "feasible_not_publishable"
    assert artifacts.result.selected_proposal_ref == proposal.ref
    assert artifacts.result.returned_proposal_refs == (proposal.ref,)
    assert artifacts.publishability is not None
    assert not artifacts.publishability.publishable
    assert artifacts.publishability.outcomes[0].observed_value == publish_improvement


def test_publishability_rule_must_close_against_policy_and_catalog(repo_root: Path) -> None:
    bundle, base = _basis(repo_root, multi=False)
    rule = base.publishability_constraints[0]
    problem = replace(
        base,
        publishability_constraints=(replace(rule, limit=rule.limit + 0.001),),
    )
    proposal = _proposal(problem, 0)
    static = (_static_evaluation(problem, proposal, (100.0,)),)
    solver = _solver_result(problem, (proposal,), static)
    dynamic = {proposal.ref: _dynamic_evaluation(problem, proposal.ref, "feasible")}

    with pytest.raises(ValueError, match="differ from SystemPolicy and catalog"):
        UnifiedFinalSelector().select(
            problem,
            solver,
            _mapping(static),
            dynamic,
            bundle,
        )


@pytest.mark.parametrize(
    ("method", "tie_breaks", "match"),
    [
        ("unknown-method", None, "require preference method"),
        (
            "single-objective",
            ("exact-margin-action-fingerprint",),
            "trusted atomic total-order sequence",
        ),
    ],
)
def test_unknown_preference_method_and_tie_break_alias_are_rejected(
    repo_root: Path,
    method: str,
    tie_breaks: tuple[str, ...] | None,
    match: str,
) -> None:
    _, base = _basis(repo_root, multi=False)
    problem = replace(
        base,
        preference=SelectionPreference(
            method=method,
            objective_order=base.preference.objective_order,
            tie_breaks=(base.preference.tie_breaks if tie_breaks is None else tie_breaks),
        ),
    )
    proposal = _proposal(problem, 0)
    static = (_static_evaluation(problem, proposal, (100.0,)),)
    solver = _solver_result(problem, (proposal,), static)

    with pytest.raises(ValueError, match=match):
        UnifiedFinalSelector().rank_static(problem, solver, _mapping(static))


def test_only_static_infeasibility_returns_no_feasible(repo_root: Path) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposal = _proposal(problem, 0)
    static = (_static_failure(problem, proposal),)
    solver = _solver_result(
        problem,
        (proposal,),
        static,
        status="no_static_feasible",
    )

    artifacts = UnifiedFinalSelector().select(
        problem,
        solver,
        _mapping(static),
        {},
        bundle,
    )

    assert artifacts.result.status == "no_feasible"
    assert artifacts.result.ranked_proposal_refs == ()
    assert artifacts.result.returned_proposal_refs == ()
    assert artifacts.publishability is None
