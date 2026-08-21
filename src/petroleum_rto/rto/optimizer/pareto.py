"""Deterministic full-grid RTO V2 search and exact non-dominated sorting."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from typing import Protocol

from ..contracts.models import CLAIM_SCOPE, ContractRef, DecisionDomainV1, OperatingContextV1
from ..contracts.multiobjective import RTO_V2_SCHEMA_VERSION, OptimizationProblemV2
from ..contracts.results_v2 import (
    CandidateEvaluationV2,
    CandidateProposalV2,
    ObjectiveEquivalenceGroupV2,
    ParetoLayerV2,
    ParetoSearchResultV2,
    ParetoSearchStatusV2,
)


class MultiObjectiveEvaluatorPort(Protocol):
    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2: ...


class DeterministicParetoGridOptimizer:
    """Evaluate the frozen 9×9 grid and retain every exact Pareto layer."""

    def search(
        self,
        problem: OptimizationProblemV2,
        context: OperatingContextV1,
        evaluator: MultiObjectiveEvaluatorPort,
    ) -> ParetoSearchResultV2:
        if context.ref != problem.context_ref:
            raise ValueError("problem and search context differ")
        if len(problem.decision_domains) != 2:
            raise ValueError("RTO V2 initial search requires exactly two decision domains")
        ids = tuple(domain.variable_id for domain in problem.decision_domains)
        vectors = tuple(
            dict(zip(ids, values, strict=True))
            for values in itertools.product(
                *(self._grid_values(domain, problem) for domain in problem.decision_domains)
            )
        )
        if len(vectors) != problem.search_plan.maximum_m2_candidates:
            raise ValueError("generated grid differs from the frozen M2 candidate budget")
        proposals = tuple(
            self._proposal(problem, context, values, sequence=index)
            for index, values in enumerate(vectors)
        )
        evaluations = tuple(evaluator.evaluate(item) for item in proposals)
        self._validate_evaluations(problem, proposals, evaluations)
        error_count = sum(
            item.status in {"invalid_request", "evaluation_error"} for item in evaluations
        )
        feasible_count = sum(item.status == "feasible" for item in evaluations)
        infeasible_count = sum(item.status == "process_infeasible" for item in evaluations)
        if error_count:
            return self._result(
                problem,
                context,
                proposals,
                evaluations,
                (),
                (),
                (),
                status="evaluation_error",
                reason="grid-evaluation-error",
                feasible_count=feasible_count,
                infeasible_count=infeasible_count,
                error_count=error_count,
            )
        if not feasible_count:
            return self._result(
                problem,
                context,
                proposals,
                evaluations,
                (),
                (),
                (),
                status="no_static_feasible",
                reason="no-static-feasible",
                feasible_count=0,
                infeasible_count=infeasible_count,
                error_count=0,
            )
        layers, groups = self.rank_feasible(problem, evaluations)
        return self._result(
            problem,
            context,
            proposals,
            evaluations,
            layers,
            layers[0].evaluation_refs,
            groups,
            status="success",
            reason="pareto-grid-complete",
            feasible_count=feasible_count,
            infeasible_count=infeasible_count,
            error_count=0,
        )

    @staticmethod
    def _grid_values(
        domain: DecisionDomainV1,
        problem: OptimizationProblemV2,
    ) -> tuple[float, ...]:
        lower = domain.lower_bound
        upper = domain.upper_bound
        step = domain.refine_step
        count = round((upper - lower) / step) + 1
        values = tuple(lower + index * step for index in range(count))
        if count != problem.search_plan.points_per_dimension or abs(values[-1] - upper) > 1e-9:
            raise ValueError("each RTO V2 domain must define nine exact fine-grid points")
        return values

    @staticmethod
    def _proposal(
        problem: OptimizationProblemV2,
        context: OperatingContextV1,
        values: dict[str, float],
        *,
        sequence: int,
    ) -> CandidateProposalV2:
        return CandidateProposalV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            proposal_version="candidate-proposal-v2",
            candidate_id=f"candidate-v2-{sequence:03d}",
            sequence=sequence,
            origin="full-grid",
            problem_ref=problem.ref,
            context_ref=context.ref,
            decision_values=values,
            output_kind="steady-setpoint-vector",
            claim_scope=CLAIM_SCOPE,
        )

    @classmethod
    def rank_feasible(
        cls,
        problem: OptimizationProblemV2,
        evaluations: Iterable[CandidateEvaluationV2],
    ) -> tuple[tuple[ParetoLayerV2, ...], tuple[ObjectiveEquivalenceGroupV2, ...]]:
        feasible = tuple(item for item in evaluations if item.status == "feasible")
        cls._validate_objective_vectors(problem, feasible)
        grouped: dict[tuple[float, ...], list[CandidateEvaluationV2]] = {}
        for evaluation in feasible:
            grouped.setdefault(cls._raw_vector(problem, evaluation), []).append(evaluation)

        representatives: list[CandidateEvaluationV2] = []
        groups: list[ObjectiveEquivalenceGroupV2] = []
        for vector in sorted(grouped, key=lambda item: cls._directional_vector(problem, item)):
            members = sorted(grouped[vector], key=cls._equivalence_key)
            representative = members[0]
            representatives.append(representative)
            if len(members) > 1:
                groups.append(
                    ObjectiveEquivalenceGroupV2(
                        representative_ref=representative.ref,
                        member_refs=tuple(item.ref for item in members),
                    )
                )

        remaining = {item.ref: item for item in representatives}
        layers: list[ParetoLayerV2] = []
        rank = 1
        while remaining:
            front = [
                candidate
                for candidate in remaining.values()
                if not any(
                    cls.dominates(problem, other, candidate)
                    for other in remaining.values()
                    if other != candidate
                )
            ]
            if not front:  # pragma: no cover - strict partial order guarantees a front
                raise RuntimeError("non-dominated sorting failed to produce a front")
            ordered = tuple(sorted(front, key=lambda item: cls._front_key(problem, item)))
            layers.append(
                ParetoLayerV2(
                    rank=rank,
                    evaluation_refs=tuple(item.ref for item in ordered),
                )
            )
            for item in front:
                del remaining[item.ref]
            rank += 1
        groups.sort(key=lambda item: item.representative_ref.fingerprint)
        return tuple(layers), tuple(groups)

    @classmethod
    def dominates(
        cls,
        problem: OptimizationProblemV2,
        left: CandidateEvaluationV2,
        right: CandidateEvaluationV2,
    ) -> bool:
        left_vector = cls._directional_vector(problem, cls._raw_vector(problem, left))
        right_vector = cls._directional_vector(problem, cls._raw_vector(problem, right))
        return all(a <= b for a, b in zip(left_vector, right_vector, strict=True)) and any(
            a < b for a, b in zip(left_vector, right_vector, strict=True)
        )

    @staticmethod
    def _raw_vector(
        problem: OptimizationProblemV2,
        evaluation: CandidateEvaluationV2,
    ) -> tuple[float, ...]:
        return tuple(
            evaluation.outcome_by_id(spec.metric_id).candidate_value for spec in problem.objectives
        )

    @staticmethod
    def _directional_vector(
        problem: OptimizationProblemV2,
        vector: tuple[float, ...],
    ) -> tuple[float, ...]:
        return tuple(
            value if spec.sense == "minimize" else -value
            for spec, value in zip(problem.objectives, vector, strict=True)
        )

    @classmethod
    def _front_key(
        cls,
        problem: OptimizationProblemV2,
        evaluation: CandidateEvaluationV2,
    ) -> tuple[object, ...]:
        return (
            *cls._directional_vector(problem, cls._raw_vector(problem, evaluation)),
            *cls._equivalence_key(evaluation),
        )

    @staticmethod
    def _equivalence_key(
        evaluation: CandidateEvaluationV2,
    ) -> tuple[float, float, str]:
        if evaluation.minimum_normalized_margin is None:
            raise ValueError("feasible evaluation lacks a hard-constraint margin")
        return (
            -evaluation.minimum_normalized_margin,
            evaluation.normalized_action_l1,
            evaluation.proposal_ref.fingerprint,
        )

    @staticmethod
    def _validate_objective_vectors(
        problem: OptimizationProblemV2,
        evaluations: Iterable[CandidateEvaluationV2],
    ) -> None:
        expected = tuple(item.metric_id for item in problem.objectives)
        for evaluation in evaluations:
            actual = tuple(item.metric_id for item in evaluation.objective_outcomes)
            if actual != expected:
                raise ValueError("feasible evaluation objective vector differs from problem")
            for spec, outcome in zip(
                problem.objectives, evaluation.objective_outcomes, strict=True
            ):
                if (
                    outcome.sense != spec.sense
                    or outcome.unit != spec.unit
                    or outcome.kpi_formula_id != spec.kpi_formula_id
                ):
                    raise ValueError("objective outcome semantics differ from problem")

    @classmethod
    def _validate_evaluations(
        cls,
        problem: OptimizationProblemV2,
        proposals: tuple[CandidateProposalV2, ...],
        evaluations: tuple[CandidateEvaluationV2, ...],
    ) -> None:
        if len(evaluations) != len(proposals):
            raise ValueError("evaluator returned a different number of results")
        for proposal, evaluation in zip(proposals, evaluations, strict=True):
            if (
                evaluation.proposal_ref != proposal.ref
                or evaluation.problem_ref != problem.ref
                or evaluation.context_ref != problem.context_ref
                or evaluation.stage != "M2"
            ):
                raise ValueError("evaluation identity differs from generated proposal")
        cls._validate_objective_vectors(
            problem, (item for item in evaluations if item.status == "feasible")
        )

    @staticmethod
    def _result(
        problem: OptimizationProblemV2,
        context: OperatingContextV1,
        proposals: tuple[CandidateProposalV2, ...],
        evaluations: tuple[CandidateEvaluationV2, ...],
        layers: tuple[ParetoLayerV2, ...],
        pareto_refs: tuple[ContractRef, ...],
        groups: tuple[ObjectiveEquivalenceGroupV2, ...],
        *,
        status: ParetoSearchStatusV2,
        reason: str,
        feasible_count: int,
        infeasible_count: int,
        error_count: int,
    ) -> ParetoSearchResultV2:
        return ParetoSearchResultV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            search_version="deterministic-pareto-search-v2",
            status=status,
            problem_ref=problem.ref,
            context_ref=context.ref,
            proposals=proposals,
            evaluations=evaluations,
            pareto_layers=layers,
            pareto_refs=pareto_refs,
            equivalence_groups=groups,
            grid_count=len(proposals),
            feasible_count=feasible_count,
            process_infeasible_count=infeasible_count,
            error_count=error_count,
            termination_reason=reason,
            claim_scope=CLAIM_SCOPE,
        )
