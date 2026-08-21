"""Fixed 25-point coarse grid plus at most eight new local refinement points."""

from __future__ import annotations

import itertools

from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    DecisionDomainV1,
    OperatingContextV1,
    OptimizationProblemV1,
    StaticSearchResultV1,
    StaticSearchStatus,
)
from ..ports import CandidateEvaluatorPort


class DeterministicGridOptimizer:
    """Generate a stable V1 candidate set and rank only M2-feasible evaluations."""

    def search(
        self,
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        evaluator: CandidateEvaluatorPort,
    ) -> StaticSearchResultV1:
        if context.ref != problem.context_ref:
            raise ValueError("problem and search context differ")
        if len(problem.decision_domains) != 2:
            raise ValueError("RTO V1 search requires exactly two decision domains")
        coarse_vectors = tuple(
            dict(zip(self._ids(problem), values, strict=True))
            for values in itertools.product(
                *(self._coarse_values(domain) for domain in problem.decision_domains)
            )
        )
        if len(coarse_vectors) != 25:
            raise ValueError("RTO V1 coarse grid must contain exactly 25 points")
        proposals = [
            self._proposal(problem, context, values, index=index, origin="coarse")
            for index, values in enumerate(coarse_vectors)
        ]
        evaluations = [evaluator.evaluate(item) for item in proposals]
        if self._contains_system_error(evaluations):
            return self._result(
                problem,
                context,
                proposals,
                evaluations,
                (),
                status="evaluation_error",
                refinement_count=0,
                reason="coarse-evaluation-error",
            )
        coarse_feasible = self._rank(evaluations)
        if not coarse_feasible:
            return self._result(
                problem,
                context,
                proposals,
                evaluations,
                (),
                status="no_static_feasible",
                refinement_count=0,
                reason="no-static-feasible",
            )
        center_ref = coarse_feasible[0].proposal_ref
        center = next(item for item in proposals if item.ref == center_ref)
        seen = {item.fingerprint for item in proposals}
        refinement: list[CandidateProposalV1] = []
        for values in itertools.product(
            *(
                self._refinement_values(domain, center.decision_values[domain.variable_id])
                for domain in problem.decision_domains
            )
        ):
            proposal = self._proposal(
                problem,
                context,
                dict(zip(self._ids(problem), values, strict=True)),
                index=len(proposals) + len(refinement),
                origin="refinement",
            )
            if proposal.fingerprint in seen:
                continue
            seen.add(proposal.fingerprint)
            refinement.append(proposal)
        if len(refinement) > 8:
            raise ValueError("RTO V1 refinement generated more than eight new points")
        refinement_evaluations = [evaluator.evaluate(item) for item in refinement]
        proposals.extend(refinement)
        evaluations.extend(refinement_evaluations)
        if len(evaluations) > problem.search_plan.maximum_m2_executions:
            raise ValueError("search exceeded the frozen M2 execution budget")
        if self._contains_system_error(refinement_evaluations):
            return self._result(
                problem,
                context,
                proposals,
                evaluations,
                (),
                status="evaluation_error",
                refinement_count=len(refinement),
                reason="refinement-evaluation-error",
            )
        ranked = self._rank(evaluations)
        return self._result(
            problem,
            context,
            proposals,
            evaluations,
            ranked,
            status="success",
            refinement_count=len(refinement),
            reason="static-search-complete",
        )

    @staticmethod
    def _ids(problem: OptimizationProblemV1) -> tuple[str, ...]:
        return tuple(domain.variable_id for domain in problem.decision_domains)

    @staticmethod
    def _coarse_values(domain: DecisionDomainV1) -> tuple[float, ...]:
        lower = domain.lower_bound
        upper = domain.upper_bound
        step = domain.coarse_step
        count = round((upper - lower) / step) + 1
        values = tuple(lower + index * step for index in range(count))
        if count != 5 or abs(values[-1] - upper) > 1e-9:
            raise ValueError("each RTO V1 coarse domain must define five exact points")
        return values

    @staticmethod
    def _refinement_values(domain: DecisionDomainV1, center: float) -> tuple[float, ...]:
        candidates = (center - domain.refine_step, center, center + domain.refine_step)
        return tuple(
            value
            for value in candidates
            if domain.lower_bound - 1e-12 <= value <= domain.upper_bound + 1e-12
        )

    @staticmethod
    def _proposal(
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        values: dict[str, float],
        *,
        index: int,
        origin: str,
    ) -> CandidateProposalV1:
        return CandidateProposalV1(
            schema_version=RTO_SCHEMA_VERSION,
            proposal_version="candidate-proposal-v1",
            candidate_id=f"candidate-{index:03d}",
            sequence=index,
            origin=origin,
            problem_ref=problem.ref,
            context_ref=context.ref,
            decision_values=values,
            output_kind="steady-setpoint-vector",
            claim_scope=CLAIM_SCOPE,
        )

    @staticmethod
    def _contains_system_error(evaluations: list[CandidateEvaluationV1]) -> bool:
        return any(item.status in {"invalid_request", "evaluation_error"} for item in evaluations)

    @staticmethod
    def _rank(
        evaluations: list[CandidateEvaluationV1],
    ) -> tuple[CandidateEvaluationV1, ...]:
        feasible = [item for item in evaluations if item.status == "feasible"]
        if any(
            item.candidate_objective is None or item.minimum_normalized_margin is None
            for item in feasible
        ):
            raise ValueError("feasible M2 evaluation lacks ranking values")
        return tuple(sorted(feasible, key=DeterministicGridOptimizer._sort_key))

    @staticmethod
    def _sort_key(item: CandidateEvaluationV1) -> tuple[float, float, float, str]:
        if item.candidate_objective is None or item.minimum_normalized_margin is None:
            raise ValueError("feasible M2 evaluation lacks ranking values")
        return (
            item.candidate_objective,
            -item.minimum_normalized_margin,
            item.normalized_action_l1,
            item.proposal_ref.fingerprint,
        )

    @staticmethod
    def _result(
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        proposals: list[CandidateProposalV1],
        evaluations: list[CandidateEvaluationV1],
        ranked: tuple[CandidateEvaluationV1, ...],
        *,
        status: StaticSearchStatus,
        refinement_count: int,
        reason: str,
    ) -> StaticSearchResultV1:
        return StaticSearchResultV1(
            schema_version=RTO_SCHEMA_VERSION,
            search_version="deterministic-static-search-v1",
            status=status,
            problem_ref=problem.ref,
            context_ref=context.ref,
            proposals=tuple(proposals),
            evaluations=tuple(evaluations),
            ranked_feasible=ranked,
            coarse_count=25,
            refinement_count=refinement_count,
            termination_reason=reason,
            claim_scope=CLAIM_SCOPE,
        )
