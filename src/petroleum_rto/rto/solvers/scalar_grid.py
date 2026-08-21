"""Deterministic coarse-grid plus local-refinement scalar solver plugin."""

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


class CoarseRefineGridSolver:
    """Preserve the validated 25-point plus at most eight-point scalar policy."""

    @property
    def descriptor(self) -> SolverDescriptor:
        return SolverDescriptor(
            solver_id="coarse-grid-local-refine",
            solver_version="1.0.0",
            deterministic=True,
            supported_result_modes=("ranked-and-selected", "selected-solution"),
        )

    def supports(self, features: ProblemFeatures) -> SolverSupport:
        reasons: list[str] = []
        if features.objective_count != 1:
            reasons.append("objective-count-unsupported")
        if not 1 <= features.decision_count <= 2:
            reasons.append("decision-count-unsupported")
        if not features.bounded:
            reasons.append("unbounded-domain-unsupported")
        if features.result_mode not in self.descriptor.supported_result_modes:
            reasons.append("result-mode-unsupported")
        if features.deterministic and not self.descriptor.deterministic:
            reasons.append("determinism-unsupported")
        required = 5**features.decision_count + 3**features.decision_count - 1
        if features.maximum_evaluations < required:
            reasons.append("evaluation-budget-insufficient")
        return SolverSupport.no(*reasons) if reasons else SolverSupport.yes()

    def solve(
        self,
        problem: OptimizationProblem,
        evaluator: CandidateEvaluatorPort,
    ) -> SolverResult:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be OptimizationProblem")
        if len(problem.objectives) != 1 or not 1 <= len(problem.decision_domains) <= 2:
            raise ValueError("scalar grid solver supports one objective and one or two decisions")
        coarse_vectors = tuple(
            dict(zip(self._ids(problem), values, strict=True))
            for values in itertools.product(
                *(self._coarse_values(domain) for domain in problem.decision_domains)
            )
        )
        if len(coarse_vectors) != 5 ** len(problem.decision_domains):
            raise ValueError("scalar coarse grid cardinality differs from its decision domains")
        proposals = [
            self._proposal(problem, values, sequence=index, origin="coarse")
            for index, values in enumerate(coarse_vectors)
        ]
        evaluations = [self._evaluate(problem, proposal, evaluator) for proposal in proposals]
        if self._contains_system_error(evaluations):
            return self._result(
                problem,
                proposals,
                evaluations,
                (),
                status="evaluation_error",
                reason="coarse-evaluation-error",
            )
        ranked = self._rank(problem, evaluations)
        if not ranked:
            return self._result(
                problem,
                proposals,
                evaluations,
                (),
                status="no_static_feasible",
                reason="no-static-feasible",
            )
        center_ref = ranked[0].proposal_ref
        center = next(item for item in proposals if item.ref == center_ref)
        seen = {item.fingerprint for item in proposals}
        refinement: list[CandidateProposal] = []
        for values in itertools.product(
            *(
                self._refinement_values(domain, center.decision_values[domain.variable_id])
                for domain in problem.decision_domains
            )
        ):
            proposal = self._proposal(
                problem,
                dict(zip(self._ids(problem), values, strict=True)),
                sequence=len(proposals) + len(refinement),
                origin="refinement",
            )
            if proposal.fingerprint in seen:
                continue
            seen.add(proposal.fingerprint)
            refinement.append(proposal)
        maximum_new = 3 ** len(problem.decision_domains) - 1
        if len(refinement) > maximum_new:
            raise ValueError("scalar refinement exceeded its local-grid cardinality")
        refinement_evaluations = [
            self._evaluate(problem, proposal, evaluator) for proposal in refinement
        ]
        proposals.extend(refinement)
        evaluations.extend(refinement_evaluations)
        if len(evaluations) > problem.solve_requirements.maximum_evaluations:
            raise ValueError("scalar solver exceeded maximum_evaluations")
        if self._contains_system_error(refinement_evaluations):
            return self._result(
                problem,
                proposals,
                evaluations,
                (),
                status="evaluation_error",
                reason="refinement-evaluation-error",
            )
        return self._result(
            problem,
            proposals,
            evaluations,
            self._rank(problem, evaluations),
            status="success",
            reason="scalar-search-complete",
        )

    @staticmethod
    def _ids(problem: OptimizationProblem) -> tuple[str, ...]:
        return tuple(item.variable_id for item in problem.decision_domains)

    @staticmethod
    def _coarse_values(domain: DecisionDomain) -> tuple[float, ...]:
        count = round((domain.upper_bound - domain.lower_bound) / domain.coarse_step) + 1
        values = tuple(domain.lower_bound + index * domain.coarse_step for index in range(count))
        if count != 5 or abs(values[-1] - domain.upper_bound) > 1e-9:
            raise ValueError("each scalar coarse domain must define five exact points")
        return values

    @staticmethod
    def _refinement_values(domain: DecisionDomain, center: float) -> tuple[float, ...]:
        return tuple(
            value
            for value in (center - domain.refine_step, center, center + domain.refine_step)
            if domain.lower_bound - 1e-12 <= value <= domain.upper_bound + 1e-12
        )

    @staticmethod
    def _proposal(
        problem: OptimizationProblem,
        values: dict[str, float],
        *,
        sequence: int,
        origin: str,
    ) -> CandidateProposal:
        return CandidateProposal(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            proposal_version="candidate-proposal",
            candidate_id=f"candidate-{sequence:03d}",
            sequence=sequence,
            origin=origin,
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
            raise ValueError("evaluation identity differs from the scalar proposal")
        expected = tuple(item.metric_id for item in problem.objectives)
        actual = tuple(item.metric_id for item in evaluation.objective_outcomes)
        if evaluation.status == "feasible" and actual != expected:
            raise ValueError("evaluation objective vector differs from the scalar problem")
        return evaluation

    @staticmethod
    def _contains_system_error(evaluations: list[CandidateEvaluation]) -> bool:
        return any(item.status in {"invalid_request", "evaluation_error"} for item in evaluations)

    @staticmethod
    def _rank(
        problem: OptimizationProblem,
        evaluations: list[CandidateEvaluation],
    ) -> tuple[CandidateEvaluation, ...]:
        objective = problem.objectives[0]
        feasible = [item for item in evaluations if item.status == "feasible"]
        if any(item.minimum_normalized_margin is None for item in feasible):
            raise ValueError("feasible evaluation lacks a hard-constraint margin")

        def key(item: CandidateEvaluation) -> tuple[float, float, float, str]:
            outcome = item.outcome_by_id(objective.metric_id)
            directional = (
                outcome.candidate_value
                if objective.sense == "minimize"
                else -outcome.candidate_value
            )
            assert item.minimum_normalized_margin is not None
            return (
                directional,
                -item.minimum_normalized_margin,
                item.normalized_action_l1,
                item.proposal_ref.fingerprint,
            )

        return tuple(sorted(feasible, key=key))

    def _result(
        self,
        problem: OptimizationProblem,
        proposals: list[CandidateProposal],
        evaluations: list[CandidateEvaluation],
        ranked: tuple[CandidateEvaluation, ...],
        *,
        status: SolverResultStatus,
        reason: str,
    ) -> SolverResult:
        if status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported scalar result status")
        descriptor = self.descriptor
        return SolverResult(
            schema_version=SOLVER_RESULT_SCHEMA_VERSION,
            result_version="scalar-grid-result",
            status=status,
            problem_ref=problem.ref,
            solver_ref=ContractRef(descriptor.solver_id, descriptor.fingerprint),
            proposals=tuple(proposals),
            evaluations=tuple(evaluations),
            solution_representation="ordered",
            solution_groups=tuple(
                SolutionGroup(rank=index, evaluation_refs=(item.ref,))
                for index, item in enumerate(ranked, start=1)
            ),
            termination_reason=reason,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
