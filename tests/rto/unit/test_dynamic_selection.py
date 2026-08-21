from __future__ import annotations

from collections.abc import Callable

from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.contracts import (
    CandidateEvaluationV1,
    CandidateProposalV1,
    EvaluationStatus,
    OptimizationProblemV1,
    OptimizationResultV1,
    StaticSearchResultV1,
)
from petroleum_rto.rto.optimizer import DeterministicGridOptimizer, DynamicFinalSelector


class _StaticEvaluator:
    def __init__(self, make_evaluation: Callable[..., CandidateEvaluationV1]) -> None:
        self._make = make_evaluation

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        temperature = proposal.decision_values["furnace_temperature_target_k"]
        pressure = proposal.decision_values["tower_top_pressure_target_pa_a"]
        objective = 180.0 + (temperature - 628.35) ** 2 + ((pressure - 152325.0) / 1000.0) ** 2
        return self._make(proposal, objective=objective, improvement=0.01)


class _DynamicEvaluator:
    def __init__(
        self,
        make_evaluation: Callable[..., CandidateEvaluationV1],
        statuses: tuple[EvaluationStatus, ...],
    ) -> None:
        self._make = make_evaluation
        self._statuses = statuses
        self.calls: list[CandidateProposalV1] = []

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        index = len(self.calls)
        self.calls.append(proposal)
        return self._make(proposal, stage="M4", status=self._statuses[index])


def _static_search(
    bundle: RtoCatalogBundle,
    problem: OptimizationProblemV1,
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> StaticSearchResultV1:
    return DeterministicGridOptimizer().search(
        problem,
        bundle.context,
        _StaticEvaluator(make_evaluation),
    )


def test_selector_checks_all_top3_and_selects_first_dynamic_feasible(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    bundle, problem = rto_basis
    static = _static_search(bundle, problem, make_evaluation)
    dynamic = _DynamicEvaluator(
        make_evaluation,
        ("process_infeasible", "feasible", "feasible"),
    )

    result = DynamicFinalSelector().select(problem, static, dynamic)

    assert result.status == "success"
    assert result.selected_proposal_ref == static.ranked_feasible[1].proposal_ref
    assert len(dynamic.calls) == 3
    assert OptimizationResultV1.from_mapping(result.as_dict()) == result


def test_top3_failure_does_not_claim_global_no_feasible(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    bundle, problem = rto_basis
    static = _static_search(bundle, problem, make_evaluation)
    dynamic = _DynamicEvaluator(
        make_evaluation,
        ("process_infeasible", "process_infeasible", "process_infeasible"),
    )

    result = DynamicFinalSelector().select(problem, static, dynamic)

    assert result.status == "shortlist_dynamic_failed"
    assert result.selected_proposal_ref is None
    assert len(static.ranked_feasible) > 3


def test_publishability_is_checked_after_dynamic_selection(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    bundle, problem = rto_basis

    class LowImprovement(_StaticEvaluator):
        def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
            base = super().evaluate(proposal)
            return make_evaluation(
                proposal,
                objective=base.candidate_objective or 180.0,
                improvement=0.001,
            )

    static = DeterministicGridOptimizer().search(
        problem,
        bundle.context,
        LowImprovement(make_evaluation),
    )
    dynamic = _DynamicEvaluator(make_evaluation, ("feasible", "feasible", "feasible"))

    result = DynamicFinalSelector().select(problem, static, dynamic)

    assert result.status == "feasible_not_publishable"
    assert not result.publishable
    assert result.selected_proposal_ref is not None
