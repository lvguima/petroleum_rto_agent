"""Deterministic full-grid Pareto solver plugin."""

from __future__ import annotations

import itertools

from ..contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
)
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, DecisionDomain, OptimizationProblem
from ..contracts.reference import ContractRef
from ..contracts.solver_result import (
    SOLVER_RESULT_SCHEMA_VERSION,
    SolutionGroup,
    SolverResult,
    SolverResultStatus,
)
from .models import ProblemFeatures, SolverDescriptor, SolverSupport
from .port import CandidateEvaluatorPort


class FullGridParetoSolver:
    """Evaluate the exact fine grid and return every non-dominated layer."""

    @property
    def descriptor(self) -> SolverDescriptor:
        return SolverDescriptor(
            solver_id="deterministic-full-grid",
            solver_version="2.0.0",
            deterministic=True,
            supported_result_modes=(
                "pareto-and-selected",
                "selected-solution",
            ),
        )

    def supports(self, features: ProblemFeatures) -> SolverSupport:
        reasons: list[str] = []
        if features.objective_count < 2:
            reasons.append("objective-count-unsupported")
        if not 1 <= features.decision_count <= 2:
            reasons.append("decision-count-unsupported")
        if not features.bounded or features.grid_cardinality is None:
            reasons.append("finite-grid-required")
        if features.result_mode not in self.descriptor.supported_result_modes:
            reasons.append("result-mode-unsupported")
        if (
            features.grid_cardinality is not None
            and features.grid_cardinality > features.maximum_evaluations
        ):
            reasons.append("evaluation-budget-insufficient")
        return SolverSupport.no(*reasons) if reasons else SolverSupport.yes()

    def solve(
        self,
        problem: OptimizationProblem,
        evaluator: CandidateEvaluatorPort,
    ) -> SolverResult:
        if len(problem.objectives) < 2 or not 1 <= len(problem.decision_domains) <= 2:
            raise ValueError(
                "Pareto grid solver requires at least two objectives and one or two decisions"
            )
        ids = tuple(item.variable_id for item in problem.decision_domains)
        vectors = tuple(
            dict(zip(ids, values, strict=True))
            for values in itertools.product(
                *(self._grid_values(domain) for domain in problem.decision_domains)
            )
        )
        if len(vectors) > problem.solve_requirements.maximum_evaluations:
            raise ValueError("Pareto grid exceeds maximum_evaluations")
        proposals = tuple(
            self._proposal(problem, values, sequence=index) for index, values in enumerate(vectors)
        )
        evaluations = tuple(self._evaluate(problem, item, evaluator) for item in proposals)
        if any(item.status in {"invalid_request", "evaluation_error"} for item in evaluations):
            return self._result(
                problem,
                proposals,
                evaluations,
                (),
                status="evaluation_error",
                reason="grid-evaluation-error",
            )
        feasible = tuple(item for item in evaluations if item.status == "feasible")
        if not feasible:
            return self._result(
                problem,
                proposals,
                evaluations,
                (),
                status="no_static_feasible",
                reason="no-static-feasible",
            )
        return self._result(
            problem,
            proposals,
            evaluations,
            self._layers(problem, feasible),
            status="success",
            reason="pareto-grid-complete",
        )

    @staticmethod
    def _grid_values(domain: DecisionDomain) -> tuple[float, ...]:
        count = round((domain.upper_bound - domain.lower_bound) / domain.refine_step) + 1
        values = tuple(domain.lower_bound + index * domain.refine_step for index in range(count))
        if abs(values[-1] - domain.upper_bound) > 1e-9:
            raise ValueError("decision domain does not define an exact fine grid")
        return values

    @staticmethod
    def _proposal(
        problem: OptimizationProblem,
        values: dict[str, float],
        *,
        sequence: int,
    ) -> CandidateProposal:
        return CandidateProposal(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            proposal_version="candidate-proposal",
            candidate_id=f"candidate-{sequence:03d}",
            sequence=sequence,
            origin="full-grid",
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            decision_values=values,
            output_kind="steady-setpoint-vector",
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    @staticmethod
    def _evaluate(
        problem: OptimizationProblem,
        proposal: CandidateProposal,
        evaluator: CandidateEvaluatorPort,
    ) -> CandidateEvaluation:
        evaluation = evaluator.evaluate(proposal)
        if (
            evaluation.problem_ref != problem.ref
            or evaluation.context_ref != problem.context_ref
            or evaluation.proposal_ref != proposal.ref
            or evaluation.stage != problem.evaluation_plan.static_stage
        ):
            raise ValueError("evaluation identity differs from the Pareto proposal")
        if evaluation.status == "feasible":
            expected = tuple(item.metric_id for item in problem.objectives)
            actual = tuple(item.metric_id for item in evaluation.objective_outcomes)
            if actual != expected:
                raise ValueError("evaluation objective vector differs from the Pareto problem")
        return evaluation

    @classmethod
    def _layers(
        cls,
        problem: OptimizationProblem,
        evaluations: tuple[CandidateEvaluation, ...],
    ) -> tuple[tuple[CandidateEvaluation, ...], ...]:
        grouped: dict[tuple[float, ...], list[CandidateEvaluation]] = {}
        for item in evaluations:
            grouped.setdefault(cls._raw_vector(problem, item), []).append(item)
        representatives = tuple(
            min(members, key=cls._equivalence_key)
            for _, members in sorted(
                grouped.items(), key=lambda entry: cls._directional_vector(problem, entry[0])
            )
        )
        remaining = {item.ref: item for item in representatives}
        layers: list[tuple[CandidateEvaluation, ...]] = []
        while remaining:
            front = tuple(
                item
                for item in remaining.values()
                if not any(
                    cls._dominates(problem, other, item)
                    for other in remaining.values()
                    if other != item
                )
            )
            ordered = tuple(
                sorted(
                    front,
                    key=lambda item: (
                        *cls._directional_vector(problem, cls._raw_vector(problem, item)),
                        *cls._equivalence_key(item),
                    ),
                )
            )
            layers.append(ordered)
            for item in front:
                del remaining[item.ref]
        return tuple(layers)

    @classmethod
    def _dominates(
        cls,
        problem: OptimizationProblem,
        left: CandidateEvaluation,
        right: CandidateEvaluation,
    ) -> bool:
        left_vector = cls._directional_vector(problem, cls._raw_vector(problem, left))
        right_vector = cls._directional_vector(problem, cls._raw_vector(problem, right))
        return all(a <= b for a, b in zip(left_vector, right_vector, strict=True)) and any(
            a < b for a, b in zip(left_vector, right_vector, strict=True)
        )

    @staticmethod
    def _raw_vector(
        problem: OptimizationProblem,
        evaluation: CandidateEvaluation,
    ) -> tuple[float, ...]:
        return tuple(
            evaluation.outcome_by_id(item.metric_id).candidate_value for item in problem.objectives
        )

    @staticmethod
    def _directional_vector(
        problem: OptimizationProblem,
        vector: tuple[float, ...],
    ) -> tuple[float, ...]:
        return tuple(
            value if objective.sense == "minimize" else -value
            for objective, value in zip(problem.objectives, vector, strict=True)
        )

    @staticmethod
    def _equivalence_key(item: CandidateEvaluation) -> tuple[float, float, str]:
        if item.minimum_normalized_margin is None:
            raise ValueError("feasible evaluation lacks a hard-constraint margin")
        return (
            -item.minimum_normalized_margin,
            item.normalized_action_l1,
            item.proposal_ref.fingerprint,
        )

    def _result(
        self,
        problem: OptimizationProblem,
        proposals: tuple[CandidateProposal, ...],
        evaluations: tuple[CandidateEvaluation, ...],
        layers: tuple[tuple[CandidateEvaluation, ...], ...],
        *,
        status: SolverResultStatus,
        reason: str,
    ) -> SolverResult:
        if status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported Pareto result status")
        descriptor = self.descriptor
        return SolverResult(
            schema_version=SOLVER_RESULT_SCHEMA_VERSION,
            result_version="pareto-grid-result",
            status=status,
            problem_ref=problem.ref,
            solver_ref=ContractRef(descriptor.solver_id, descriptor.fingerprint),
            proposals=proposals,
            evaluations=evaluations,
            solution_representation="layered",
            solution_groups=tuple(
                SolutionGroup(rank=index, evaluation_refs=tuple(item.ref for item in layer))
                for index, layer in enumerate(layers, start=1)
            ),
            termination_reason=reason,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
