from __future__ import annotations

from dataclasses import replace

from petroleum_rto.rto.contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
    ConstraintOutcome,
    ObjectiveOutcome,
)
from petroleum_rto.rto.contracts.evidence import (
    RUN_EVIDENCE_SCHEMA_VERSION,
    RunEvidenceRef,
)
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE
from petroleum_rto.rto.solvers import CoarseRefineGridSolver, FullGridParetoSolver
from tests.rto.unit.test_unified_problem_contract import _objective, _problem


def _evidence(pair_role: str, digit: str) -> RunEvidenceRef:
    if pair_role not in {"baseline", "candidate"}:
        raise ValueError("unsupported fixture pair role")
    return RunEvidenceRef(
        schema_version=RUN_EVIDENCE_SCHEMA_VERSION,
        evidence_version="synthetic-evidence",
        pair_role=pair_role,
        provider_id="synthetic-provider",
        run_ref=f"/tmp/{pair_role}",
        provider_request_fingerprint=digit * 64,
        request_fingerprint=digit * 64,
        effective_input_fingerprint=digit * 64,
        result_fingerprint=digit * 64,
        manifest_fingerprint=digit * 64,
        versions={"model": "synthetic"},
        source_fingerprints={"source": digit * 64},
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


class _Evaluator:
    def __init__(self, problem) -> None:
        self.problem = problem
        self.calls = 0

    def evaluate(self, proposal: CandidateProposal) -> CandidateEvaluation:
        self.calls += 1
        x, y = tuple(proposal.decision_values.values())
        outcomes: list[ObjectiveOutcome] = []
        for objective in self.problem.objectives:
            candidate = x if objective.metric_id != "yield" else y
            baseline = 0.0
            directional = -candidate if objective.sense == "minimize" else candidate
            outcomes.append(
                ObjectiveOutcome(
                    metric_id=objective.metric_id,
                    sense=objective.sense,
                    unit=objective.unit,
                    formula_id=objective.formula_id,
                    baseline_value=baseline,
                    candidate_value=candidate,
                    directional_absolute_improvement=directional,
                    relative_directional_improvement=None,
                    normalized_directional_improvement=directional,
                )
            )
        return CandidateEvaluation(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            evaluation_version="synthetic-evaluation",
            stage="M2",
            status="feasible",
            problem_ref=self.problem.ref,
            context_ref=self.problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=f"pair-{proposal.fingerprint[:16]}",
            objective_outcomes=tuple(outcomes),
            metrics={item.metric_id: item.candidate_value for item in outcomes},
            constraints=(
                ConstraintOutcome(
                    constraint_id="m2-structural-numeric",
                    metric_id="m2_evaluable",
                    raw_value=1.0,
                    limit=1.0,
                    normalized_margin=1.0,
                    passed=True,
                ),
            ),
            minimum_normalized_margin=1.0,
            normalized_action_l1=0.0,
            reason_codes=(),
            evidence_refs=(
                _evidence("baseline", "1"),
                _evidence("candidate", "2"),
            ),
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )


def test_scalar_plugin_uses_common_vector_evaluation_for_minimize_and_maximize() -> None:
    minimize_problem = replace(
        _problem(_objective("energy")),
        solve_requirements=replace(
            _problem(_objective("energy")).solve_requirements,
            maximum_evaluations=33,
        ),
    )
    minimize_result = CoarseRefineGridSolver().solve(minimize_problem, _Evaluator(minimize_problem))
    first_min = next(
        item
        for item in minimize_result.evaluations
        if item.ref == minimize_result.solution_groups[0].evaluation_refs[0]
    )
    assert minimize_result.status == "success"
    assert len(minimize_result.proposals) <= 33
    assert first_min.outcome_by_id("energy").candidate_value == 626.35

    maximize_problem = replace(
        minimize_problem,
        objectives=(_objective("energy", "maximize"),),
        preference=replace(
            minimize_problem.preference,
            objective_order=("energy",),
        ),
    )
    maximize_result = CoarseRefineGridSolver().solve(maximize_problem, _Evaluator(maximize_problem))
    first_max = next(
        item
        for item in maximize_result.evaluations
        if item.ref == maximize_result.solution_groups[0].evaluation_refs[0]
    )
    assert first_max.outcome_by_id("energy").candidate_value == 630.35


def test_pareto_plugin_uses_same_problem_and_evaluation_contract() -> None:
    problem = _problem(_objective("energy"), _objective("yield", "maximize"))
    evaluator = _Evaluator(problem)

    result = FullGridParetoSolver().solve(problem, evaluator)

    assert result.status == "success"
    assert result.solution_representation == "layered"
    assert len(result.proposals) == evaluator.calls == 81
    assert len(result.solution_groups) >= 1
    first = next(
        item
        for item in result.evaluations
        if item.ref == result.solution_groups[0].evaluation_refs[0]
    )
    assert first.outcome_by_id("energy").candidate_value == 626.35
    assert first.outcome_by_id("yield").candidate_value == 154325.0
