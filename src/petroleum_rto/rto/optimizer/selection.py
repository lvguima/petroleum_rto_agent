"""R4 Top-K dynamic verification and final deterministic selection."""

from __future__ import annotations

from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    ContractRef,
    OptimizationProblemV1,
    OptimizationResultStatus,
    OptimizationResultV1,
    StaticSearchResultV1,
)
from ..ports import CandidateEvaluatorPort


class DynamicFinalSelector:
    """Evaluate the complete static Top-K and produce the only final result contract."""

    def select(
        self,
        problem: OptimizationProblemV1,
        static: StaticSearchResultV1,
        evaluator: CandidateEvaluatorPort,
    ) -> OptimizationResultV1:
        if static.problem_ref != problem.ref or static.context_ref != problem.context_ref:
            raise ValueError("static search references another problem or context")
        ranking_refs = tuple(item.ref for item in static.ranked_feasible)
        if static.status != "success":
            status: OptimizationResultStatus = (
                "no_static_feasible"
                if static.status == "no_static_feasible"
                else "evaluation_error"
            )
            return self._result(
                problem,
                static,
                status=status,
                dynamic=(),
                selected_static=None,
                selected_dynamic=None,
                reason=(
                    "no-static-feasible"
                    if status == "no_static_feasible"
                    else "static-evaluation-error"
                ),
                ranking_refs=ranking_refs,
            )
        proposals = {item.ref: item for item in static.proposals}
        shortlist = static.ranked_feasible[: problem.evaluation_plan.top_k]
        dynamic = tuple(evaluator.evaluate(proposals[item.proposal_ref]) for item in shortlist)
        for static_item, dynamic_item in zip(shortlist, dynamic, strict=True):
            if dynamic_item.stage != "M4" or dynamic_item.proposal_ref != static_item.proposal_ref:
                raise ValueError("dynamic evaluation differs from the static shortlist")
        if any(item.status in {"invalid_request", "evaluation_error"} for item in dynamic):
            return self._result(
                problem,
                static,
                status="evaluation_error",
                dynamic=dynamic,
                selected_static=None,
                selected_dynamic=None,
                reason="dynamic-evaluation-error",
                ranking_refs=ranking_refs,
            )
        selected_static: CandidateEvaluationV1 | None = None
        selected_dynamic: CandidateEvaluationV1 | None = None
        for static_item, dynamic_item in zip(shortlist, dynamic, strict=True):
            if dynamic_item.status == "feasible":
                selected_static = static_item
                selected_dynamic = dynamic_item
                break
        if selected_static is None or selected_dynamic is None:
            return self._result(
                problem,
                static,
                status="shortlist_dynamic_failed",
                dynamic=dynamic,
                selected_static=None,
                selected_dynamic=None,
                reason="shortlist-dynamic-failed",
                ranking_refs=ranking_refs,
            )
        publish_rule = next(
            rule
            for rule in problem.constraints
            if rule.kind == "publishability"
            and rule.metric_id == "specific_furnace_fuel_improvement_fraction"
        )
        improvement = selected_static.relative_improvement
        if improvement is None:
            raise ValueError("selected static evaluation lacks relative improvement")
        publishable = improvement >= publish_rule.limit
        return self._result(
            problem,
            static,
            status="success" if publishable else "feasible_not_publishable",
            dynamic=dynamic,
            selected_static=selected_static,
            selected_dynamic=selected_dynamic,
            reason="selected-publishable" if publishable else "selected-below-publish-gate",
            ranking_refs=ranking_refs,
        )

    @staticmethod
    def _result(
        problem: OptimizationProblemV1,
        static: StaticSearchResultV1,
        *,
        status: OptimizationResultStatus,
        dynamic: tuple[CandidateEvaluationV1, ...],
        selected_static: CandidateEvaluationV1 | None,
        selected_dynamic: CandidateEvaluationV1 | None,
        reason: str,
        ranking_refs: tuple[ContractRef, ...],
    ) -> OptimizationResultV1:
        return OptimizationResultV1(
            schema_version=RTO_SCHEMA_VERSION,
            result_version="optimization-result-v1",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            static_search_ref=static.ref,
            static_ranking=ranking_refs,
            dynamic_evaluations=dynamic,
            selected_proposal_ref=(
                None if selected_static is None else selected_static.proposal_ref
            ),
            selected_static_evaluation_ref=(
                None if selected_static is None else selected_static.ref
            ),
            selected_dynamic_evaluation_ref=(
                None if selected_dynamic is None else selected_dynamic.ref
            ),
            publishable=status == "success",
            termination_reason=reason,
            claim_scope=CLAIM_SCOPE,
        )
