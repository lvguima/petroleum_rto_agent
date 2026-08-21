from __future__ import annotations

from collections.abc import Callable

from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.contracts import (
    CandidateEvaluationV1,
    CandidateProposalV1,
    OptimizationProblemV1,
    StaticSearchResultV1,
)
from petroleum_rto.rto.optimizer import DeterministicGridOptimizer


class _QuadraticEvaluator:
    def __init__(
        self,
        make_evaluation: Callable[..., CandidateEvaluationV1],
        *,
        all_infeasible: bool = False,
        inject_error: bool = False,
    ) -> None:
        self._make = make_evaluation
        self._all_infeasible = all_infeasible
        self._inject_error = inject_error
        self.calls: list[CandidateProposalV1] = []

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        self.calls.append(proposal)
        if self._inject_error and proposal.sequence == 3:
            return self._make(proposal, status="evaluation_error")
        if self._all_infeasible:
            return self._make(proposal, status="process_infeasible")
        temperature = proposal.decision_values["furnace_temperature_target_k"]
        pressure = proposal.decision_values["tower_top_pressure_target_pa_a"]
        objective = 180.0 + (temperature - 628.35) ** 2 + ((pressure - 152325.0) / 1000.0) ** 2
        return self._make(proposal, objective=objective, margin=1.0, improvement=0.01)


def test_fixed_search_generates_25_plus_8_and_is_repeatable(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    bundle, problem = rto_basis
    first_evaluator = _QuadraticEvaluator(make_evaluation)
    second_evaluator = _QuadraticEvaluator(make_evaluation)

    first = DeterministicGridOptimizer().search(problem, bundle.context, first_evaluator)
    second = DeterministicGridOptimizer().search(problem, bundle.context, second_evaluator)

    assert first.status == "success"
    assert first.coarse_count == 25
    assert first.refinement_count == 8
    assert len(first.evaluations) == 33
    assert len({item.proposal_ref for item in first.evaluations}) == 33
    assert first.ranked_feasible[0].candidate_objective == 180.0
    assert first.fingerprint == second.fingerprint
    assert StaticSearchResultV1.from_mapping(first.as_dict()) == first


def test_no_static_feasible_stops_without_refinement(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    bundle, problem = rto_basis
    evaluator = _QuadraticEvaluator(make_evaluation, all_infeasible=True)

    result = DeterministicGridOptimizer().search(problem, bundle.context, evaluator)

    assert result.status == "no_static_feasible"
    assert result.refinement_count == 0
    assert len(evaluator.calls) == 25


def test_system_error_aborts_trusted_ranking_but_is_not_process_infeasible(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    bundle, problem = rto_basis
    evaluator = _QuadraticEvaluator(make_evaluation, inject_error=True)

    result = DeterministicGridOptimizer().search(problem, bundle.context, evaluator)

    assert result.status == "evaluation_error"
    assert result.ranked_feasible == ()
    assert len(evaluator.calls) == 25
