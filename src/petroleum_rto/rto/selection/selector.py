"""Objective-count-neutral preference, verification fallback, and publishability."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..capabilities.models import CapabilityBundle
from ..contracts.candidate import CandidateEvaluation
from ..contracts.finalization import (
    FINALIZATION_SCHEMA_VERSION,
    FinalizationResult,
    FinalizationStatus,
    PublishabilityAssessment,
    PublishabilityOutcome,
    StaticPreferenceSelection,
    StaticSelectionStatus,
)
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, ObjectiveSpec, OptimizationProblem
from ..contracts.reference import ContractRef
from ..contracts.solver_result import SolverResult

_ATOMIC_TIE_BREAKS = (
    "minimum-hard-constraint-margin-desc",
    "normalized-action-l1-asc",
    "proposal-fingerprint-asc",
)
_SYSTEM_FAILURE_STATUS = Literal["invalid_request", "evaluation_error"]


@dataclass(frozen=True)
class FinalizationArtifacts:
    """In-memory grouping of the three independently fingerprinted artifacts."""

    static_selection: StaticPreferenceSelection
    publishability: PublishabilityAssessment | None
    result: FinalizationResult

    def __post_init__(self) -> None:
        if not isinstance(self.static_selection, StaticPreferenceSelection):
            raise TypeError("static_selection must be StaticPreferenceSelection")
        if self.publishability is not None and not isinstance(
            self.publishability, PublishabilityAssessment
        ):
            raise TypeError("publishability must be PublishabilityAssessment or None")
        if not isinstance(self.result, FinalizationResult):
            raise TypeError("result must be FinalizationResult")
        if self.result.static_selection_ref != self.static_selection.ref:
            raise ValueError("final result references another static selection")
        expected = None if self.publishability is None else self.publishability.ref
        if self.result.publishability_assessment_ref != expected:
            raise ValueError("final result references another publishability assessment")


class PublishabilityAssessor:
    """Apply only explicit post-selection bindings from the system policy."""

    def assess(
        self,
        problem: OptimizationProblem,
        selected_static: CandidateEvaluation,
        bundle: CapabilityBundle,
    ) -> PublishabilityAssessment:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be OptimizationProblem")
        if not isinstance(selected_static, CandidateEvaluation):
            raise TypeError("selected_static must be CandidateEvaluation")
        if not isinstance(bundle, CapabilityBundle):
            raise TypeError("bundle must be CapabilityBundle")
        if (
            problem.capability_catalog_ref != bundle.catalog.ref
            or problem.system_policy_ref != bundle.system_policy.ref
        ):
            raise ValueError("problem references another capability catalog or system policy")
        if (
            selected_static.problem_ref != problem.ref
            or selected_static.context_ref != problem.context_ref
            or selected_static.stage != problem.evaluation_plan.static_stage
            or selected_static.status != "feasible"
        ):
            raise ValueError("publishability requires the selected feasible static evaluation")

        guardrails = {item.guardrail_id: item for item in bundle.catalog.guardrails}
        metrics = {item.metric_id: item for item in bundle.catalog.metrics}
        expected_rules = tuple(
            (
                binding.guardrail_id,
                binding.priority,
                guardrails[binding.guardrail_id].metric_id,
                guardrails[binding.guardrail_id].stage,
                binding.operator,
                binding.limit,
                guardrails[binding.guardrail_id].unit,
                binding.normalization_scale,
                "system",
            )
            for binding in sorted(
                bundle.system_policy.publishability_guardrails,
                key=lambda item: item.priority,
            )
        )
        actual_rules = tuple(
            (
                rule.constraint_id,
                rule.priority,
                rule.metric_id,
                rule.evaluation_stage,
                rule.operator,
                rule.limit,
                rule.unit,
                rule.normalization_scale,
                rule.source,
            )
            for rule in problem.publishability_constraints
        )
        if actual_rules != expected_rules:
            raise ValueError(
                "problem publishability constraints differ from SystemPolicy and catalog"
            )
        outcomes: list[PublishabilityOutcome] = []
        for binding in sorted(
            bundle.system_policy.publishability_guardrails,
            key=lambda item: item.priority,
        ):
            guardrail = guardrails[binding.guardrail_id]
            metric = metrics[guardrail.metric_id]
            if guardrail.stage != "post_selection" or metric.stage != "post_selection":
                raise ValueError("publishability policy must bind post-selection capabilities")
            observed = selected_static.metrics.get(metric.metric_id)
            passed = observed is not None and self._passes(
                observed,
                operator=binding.operator,
                limit=binding.limit,
            )
            outcomes.append(
                PublishabilityOutcome(
                    guardrail_id=binding.guardrail_id,
                    priority=binding.priority,
                    metric_id=guardrail.metric_id,
                    operator=binding.operator,
                    limit=binding.limit,
                    observed_value=observed,
                    passed=passed,
                    reason_code=(
                        "publishability-passed"
                        if passed
                        else "publishability-metric-unavailable"
                        if observed is None
                        else "publishability-threshold-not-met"
                    ),
                )
            )
        publishable = all(item.passed for item in outcomes)
        evidence_missing = any(item.observed_value is None for item in outcomes)
        return PublishabilityAssessment(
            schema_version=FINALIZATION_SCHEMA_VERSION,
            assessment_version="publishability-assessment",
            status="publishable" if publishable else "not_publishable",
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            capability_catalog_ref=bundle.catalog.ref,
            system_policy_ref=bundle.system_policy.ref,
            selected_proposal_ref=selected_static.proposal_ref,
            selected_static_evaluation_ref=selected_static.ref,
            outcomes=tuple(outcomes),
            publishable=publishable,
            termination_reason=(
                "publishability-passed"
                if publishable
                else "publishability-evidence-unavailable"
                if evidence_missing
                else "publishability-threshold-not-met"
            ),
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    @staticmethod
    def _passes(value: float, *, operator: str, limit: float) -> bool:
        if operator == "le":
            return value <= limit
        if operator == "ge":
            return value >= limit
        if operator == "eq":
            return math.isclose(value, limit, rel_tol=0.0, abs_tol=1e-12)
        raise ValueError("unsupported publishability operator")


class FinalSelector:
    """Rank 1..N objectives, consume a Top-K M4 prefix, then publish-gate."""

    def __init__(self, publishability: PublishabilityAssessor | None = None) -> None:
        self._publishability = publishability or PublishabilityAssessor()
        if not isinstance(self._publishability, PublishabilityAssessor):
            raise TypeError("publishability must be PublishabilityAssessor")

    def rank_static(
        self,
        problem: OptimizationProblem,
        solver_result: SolverResult,
        m2_evaluations: Mapping[ContractRef, CandidateEvaluation],
    ) -> StaticPreferenceSelection:
        self._validate_preference(problem)
        static = self._validate_static_inputs(problem, solver_result, m2_evaluations)
        status, reason = self._static_terminal_status(solver_result, tuple(static.values()))
        if status is not None:
            return self._selection(problem, solver_result, status=status, reason=reason)

        by_evaluation_ref = {item.ref: item for item in static.values()}
        candidates: list[CandidateEvaluation] = []
        groups = (
            solver_result.solution_groups[:1]
            if solver_result.solution_representation == "layered"
            else solver_result.solution_groups
        )
        for group in groups:
            for evaluation_ref in group.evaluation_refs:
                evaluation = by_evaluation_ref.get(evaluation_ref)
                if evaluation is None:
                    raise ValueError("solver solution references an unknown M2 evaluation")
                if evaluation.status != "feasible":
                    raise ValueError("solver solution groups may contain only feasible evaluations")
                candidates.append(evaluation)
        ranking = self._rank(problem, tuple(candidates))
        return self._selection(
            problem,
            solver_result,
            status="ready",
            reason="static-preference-ranked",
            ranking=tuple(item.proposal_ref for item in ranking),
        )

    def select(
        self,
        problem: OptimizationProblem,
        solver_result: SolverResult,
        m2_evaluations: Mapping[ContractRef, CandidateEvaluation],
        m4_evaluations: Mapping[ContractRef, CandidateEvaluation],
        bundle: CapabilityBundle,
    ) -> FinalizationArtifacts:
        selection = self.rank_static(problem, solver_result, m2_evaluations)
        static = self._validate_static_inputs(problem, solver_result, m2_evaluations)
        if selection.status != "ready":
            if m4_evaluations:
                raise ValueError("non-ready static selection cannot consume M4 evaluations")
            final_status: FinalizationStatus = selection.status
            result = self._result(
                problem,
                solver_result,
                selection,
                status=final_status,
                reason=selection.termination_reason,
            )
            return FinalizationArtifacts(selection, None, result)

        dynamic = self._validate_dynamic_inputs(problem, selection, m4_evaluations)
        if len(dynamic) != len(selection.shortlist_proposal_refs):
            result = self._result(
                problem,
                solver_result,
                selection,
                status="evaluation_error",
                reason="dynamic-evaluation-incomplete",
                dynamic=dynamic,
            )
            return FinalizationArtifacts(selection, None, result)
        system_status = self._system_failure(tuple(dynamic.values()))
        if system_status is not None:
            status, reason = system_status
            result = self._result(
                problem,
                solver_result,
                selection,
                status=status,
                reason=reason,
                dynamic=dynamic,
            )
            return FinalizationArtifacts(selection, None, result)

        selected_dynamic = next(
            (item for item in dynamic.values() if item.status == "feasible"),
            None,
        )
        if selected_dynamic is None:
            exhausted = len(selection.ranked_proposal_refs) > len(selection.shortlist_proposal_refs)
            reason = (
                "verification-budget-exhausted"
                if exhausted
                else "no-dynamically-feasible-candidate"
            )
            result = self._result(
                problem,
                solver_result,
                selection,
                status="no_verified_candidate",
                reason=reason,
                dynamic=dynamic,
            )
            return FinalizationArtifacts(selection, None, result)

        selected_static = static[selected_dynamic.proposal_ref]
        assessment = self._publishability.assess(problem, selected_static, bundle)
        selected_status: FinalizationStatus = (
            "success" if assessment.publishable else "feasible_not_publishable"
        )
        result = self._result(
            problem,
            solver_result,
            selection,
            status=selected_status,
            reason=(
                "selected-publishable"
                if assessment.publishable
                else "selected-feasible-not-publishable"
            ),
            dynamic=dynamic,
            selected_static=selected_static,
            selected_dynamic=selected_dynamic,
            assessment=assessment,
        )
        return FinalizationArtifacts(selection, assessment, result)

    @staticmethod
    def _validate_static_inputs(
        problem: OptimizationProblem,
        solver_result: SolverResult,
        evaluations: Mapping[ContractRef, CandidateEvaluation],
    ) -> dict[ContractRef, CandidateEvaluation]:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be OptimizationProblem")
        if not isinstance(solver_result, SolverResult):
            raise TypeError("solver_result must be SolverResult")
        if not isinstance(evaluations, Mapping):
            raise TypeError("m2_evaluations must be a mapping")
        if solver_result.problem_ref != problem.ref:
            raise ValueError("solver result references another optimization problem")
        proposals = {item.ref: item for item in solver_result.proposals}
        expected = {item.proposal_ref: item for item in solver_result.evaluations}
        if set(evaluations) != set(proposals) or set(evaluations) != set(expected):
            raise ValueError("M2 evaluation mapping must cover every solver proposal exactly")
        result: dict[ContractRef, CandidateEvaluation] = {}
        for proposal_ref, evaluation in evaluations.items():
            if not isinstance(proposal_ref, ContractRef):
                raise TypeError("M2 evaluation keys must be ContractRef")
            if not isinstance(evaluation, CandidateEvaluation):
                raise TypeError("M2 evaluation values must be CandidateEvaluation")
            proposal = proposals[proposal_ref]
            solver_evaluation = expected[proposal_ref]
            if (
                evaluation.proposal_ref != proposal_ref
                or evaluation.problem_ref != problem.ref
                or evaluation.context_ref != problem.context_ref
                or evaluation.stage != problem.evaluation_plan.static_stage
                or proposal.problem_ref != problem.ref
                or proposal.context_ref != problem.context_ref
                or evaluation.ref != solver_evaluation.ref
            ):
                raise ValueError("M2 evaluation identity differs from the solver result")
            result[proposal_ref] = evaluation
        return result

    @staticmethod
    def _static_terminal_status(
        solver_result: SolverResult,
        evaluations: tuple[CandidateEvaluation, ...],
    ) -> tuple[StaticSelectionStatus | None, str]:
        if solver_result.status == "unsupported_problem":
            return "unsupported_problem", "solver-unsupported-problem"
        failure = FinalSelector._system_failure(evaluations, prefix="static")
        if failure is not None:
            return failure
        if solver_result.status == "evaluation_error":
            return "evaluation_error", "static-evaluation-error"
        if solver_result.status == "no_static_feasible":
            if any(item.status == "feasible" for item in evaluations):
                raise ValueError("no_static_feasible solver result contains a feasible M2 result")
            if any(item.status == "not_evaluated" for item in evaluations):
                return "evaluation_error", "static-evaluation-incomplete"
            return "no_feasible", "no-static-feasible"
        if solver_result.status != "success":  # pragma: no cover - contract guards this
            raise ValueError("unsupported solver result status")
        return None, "static-selection-ready"

    @staticmethod
    def _system_failure(
        evaluations: tuple[CandidateEvaluation, ...],
        *,
        prefix: str = "dynamic",
    ) -> tuple[_SYSTEM_FAILURE_STATUS, str] | None:
        statuses = {item.status for item in evaluations}
        if "invalid_request" in statuses:
            return "invalid_request", f"{prefix}-invalid-request"
        if "evaluation_error" in statuses:
            return "evaluation_error", f"{prefix}-evaluation-error"
        if "not_evaluated" in statuses:
            return "evaluation_error", f"{prefix}-evaluation-incomplete"
        return None

    @classmethod
    def _rank(
        cls,
        problem: OptimizationProblem,
        evaluations: tuple[CandidateEvaluation, ...],
    ) -> tuple[CandidateEvaluation, ...]:
        objective_by_id = {item.metric_id: item for item in problem.objectives}
        ordered_specs = tuple(objective_by_id[item] for item in problem.preference.objective_order)
        keyed = tuple(
            (cls._preference_key(problem, ordered_specs, item), item) for item in evaluations
        )
        keys = tuple(key for key, _ in keyed)
        if len(keys) != len(set(keys)):
            raise ValueError("explicit preference and tie-breaks do not define a total order")
        return tuple(item for _, item in sorted(keyed, key=lambda pair: pair[0]))

    @staticmethod
    def _validate_preference(problem: OptimizationProblem) -> None:
        expected_method = "single-objective" if len(problem.objectives) == 1 else "lexicographic"
        if problem.preference.method != expected_method:
            raise ValueError(
                f"{len(problem.objectives)} objective(s) require preference method "
                f"{expected_method!r}"
            )
        if problem.preference.tie_breaks != _ATOMIC_TIE_BREAKS:
            raise ValueError(
                "preference tie_breaks must use the trusted atomic total-order sequence"
            )

    @staticmethod
    def _preference_key(
        problem: OptimizationProblem,
        objectives: tuple[ObjectiveSpec, ...],
        evaluation: CandidateEvaluation,
    ) -> tuple[float | str, ...]:
        key: list[float | str] = []
        for objective in objectives:
            outcome = evaluation.outcome_by_id(objective.metric_id)
            if outcome.sense != objective.sense:
                raise ValueError("M2 objective outcome sense differs from the problem")
            key.append(
                outcome.candidate_value
                if objective.sense == "minimize"
                else -outcome.candidate_value
            )
        for tie_break in problem.preference.tie_breaks:
            if tie_break == "minimum-hard-constraint-margin-desc":
                if evaluation.minimum_normalized_margin is None:
                    raise ValueError("feasible M2 evaluation lacks a minimum margin")
                key.append(-evaluation.minimum_normalized_margin)
            elif tie_break == "normalized-action-l1-asc":
                key.append(evaluation.normalized_action_l1)
            elif tie_break == "proposal-fingerprint-asc":
                key.append(evaluation.proposal_ref.fingerprint)
            else:
                raise ValueError(f"unsupported explicit tie-break {tie_break!r}")
        return tuple(key)

    @staticmethod
    def _selection(
        problem: OptimizationProblem,
        solver_result: SolverResult,
        *,
        status: StaticSelectionStatus,
        reason: str,
        ranking: tuple[ContractRef, ...] = (),
    ) -> StaticPreferenceSelection:
        limit = problem.evaluation_plan.dynamic_shortlist_size
        return StaticPreferenceSelection(
            schema_version=FINALIZATION_SCHEMA_VERSION,
            selection_version="static-preference-selection",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            solver_result_ref=solver_result.ref,
            shortlist_limit=limit,
            ranked_proposal_refs=ranking,
            shortlist_proposal_refs=ranking[:limit],
            termination_reason=reason,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    @staticmethod
    def _validate_dynamic_inputs(
        problem: OptimizationProblem,
        selection: StaticPreferenceSelection,
        evaluations: Mapping[ContractRef, CandidateEvaluation],
    ) -> dict[ContractRef, CandidateEvaluation]:
        if not isinstance(evaluations, Mapping):
            raise TypeError("m4_evaluations must be a mapping")
        allowed = selection.shortlist_proposal_refs
        if len(evaluations) > len(allowed):
            raise ValueError("M4 evaluations exceed the dynamic shortlist budget")
        expected_prefix = allowed[: len(evaluations)]
        if set(evaluations) != set(expected_prefix):
            raise ValueError("M4 evaluations must cover a contiguous shortlist prefix")
        result: dict[ContractRef, CandidateEvaluation] = {}
        for proposal_ref in expected_prefix:
            evaluation = evaluations[proposal_ref]
            if not isinstance(proposal_ref, ContractRef):
                raise TypeError("M4 evaluation keys must be ContractRef")
            if not isinstance(evaluation, CandidateEvaluation):
                raise TypeError("M4 evaluation values must be CandidateEvaluation")
            if (
                evaluation.proposal_ref != proposal_ref
                or evaluation.problem_ref != problem.ref
                or evaluation.context_ref != problem.context_ref
                or evaluation.stage != problem.evaluation_plan.dynamic_stage
            ):
                raise ValueError("M4 evaluation identity differs from the shortlist")
            if evaluation.objective_outcomes:
                raise ValueError("M4 evaluation must not repeat static objective outcomes")
            if evaluation.status == "feasible" and (
                not evaluation.constraints
                or any(not item.passed for item in evaluation.constraints)
                or evaluation.minimum_normalized_margin is None
            ):
                raise ValueError("feasible M4 evaluation requires passed dynamic constraints")
            if evaluation.status == "process_infeasible" and not evaluation.evidence_refs:
                raise ValueError("process-infeasible M4 evaluation requires physical evidence")
            result[proposal_ref] = evaluation
        return result

    @staticmethod
    def _result(
        problem: OptimizationProblem,
        solver_result: SolverResult,
        selection: StaticPreferenceSelection,
        *,
        status: FinalizationStatus,
        reason: str,
        dynamic: Mapping[ContractRef, CandidateEvaluation] | None = None,
        selected_static: CandidateEvaluation | None = None,
        selected_dynamic: CandidateEvaluation | None = None,
        assessment: PublishabilityAssessment | None = None,
    ) -> FinalizationResult:
        dynamic = {} if dynamic is None else dynamic
        return FinalizationResult(
            schema_version=FINALIZATION_SCHEMA_VERSION,
            result_version="optimization-finalization",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            solver_result_ref=solver_result.ref,
            static_selection_ref=selection.ref,
            result_mode=problem.result_request.mode,
            maximum_returned_candidates=(problem.result_request.maximum_returned_candidates),
            ranked_proposal_refs=selection.ranked_proposal_refs,
            returned_proposal_refs=(
                ()
                if problem.result_request.mode == "selected" and selected_static is None
                else (selected_static.proposal_ref,)
                if problem.result_request.mode == "selected" and selected_static is not None
                else selection.ranked_proposal_refs[
                    : problem.result_request.maximum_returned_candidates
                ]
            ),
            shortlist_proposal_refs=selection.shortlist_proposal_refs,
            dynamic_proposal_refs=tuple(dynamic),
            dynamic_evaluation_refs=tuple(item.ref for item in dynamic.values()),
            selected_proposal_ref=(
                None if selected_static is None else selected_static.proposal_ref
            ),
            selected_static_evaluation_ref=(
                None if selected_static is None else selected_static.ref
            ),
            selected_dynamic_evaluation_ref=(
                None if selected_dynamic is None else selected_dynamic.ref
            ),
            publishability_assessment_ref=None if assessment is None else assessment.ref,
            publishable=status == "success",
            termination_reason=reason,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )


__all__ = ["FinalSelector", "FinalizationArtifacts", "PublishabilityAssessor"]
