"""Deterministic builder for objective-count-neutral strategy drafts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from ...contracts.candidate import CandidateEvaluation, CandidateProposal
from ...contracts.common import canonical_fingerprint, finite, identifier, integer
from ...contracts.context import OperatingContext
from ...contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from ...contracts.reference import ContractRef
from ...selection import FinalizationArtifacts
from .models import (
    STRATEGY_SCHEMA_VERSION,
    StrategyAnchor,
    StrategyEntry,
    StrategyObjectiveSummary,
    canonical_refs,
)


def _applicability_values(
    context: OperatingContext,
    supplied: Mapping[str, float] | None,
) -> Mapping[str, float]:
    feed = context.facts.get("fresh_feed_load_kg_s")
    if isinstance(feed, bool) or not isinstance(feed, (int, float)):
        raise TypeError("strategy context requires a numeric fresh feed load")
    if supplied is not None:
        if "fresh_feed_load_kg_s" not in supplied:
            raise ValueError("applicability_values must include fresh_feed_load_kg_s")
        for key, value in supplied.items():
            fact = context.facts.get(key)
            if fact is not None and (
                isinstance(fact, bool)
                or not isinstance(fact, (int, float))
                or not math.isclose(
                    finite(value, context=f"applicability_values.{key}"),
                    finite(fact, context=f"context.facts.{key}"),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("applicability_values differ from operating context facts")
        return supplied
    return {"fresh_feed_load_kg_s": finite(feed, context="fresh_feed_load_kg_s")}


def _is_close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12)


def _constraint_result(
    *,
    operator: str,
    raw_value: float,
    limit: float,
    scale: float,
) -> tuple[bool, float]:
    if operator == "le":
        return raw_value <= limit, (limit - raw_value) / scale
    if operator == "ge":
        return raw_value >= limit, (raw_value - limit) / scale
    passed = _is_close(raw_value, limit)
    return passed, 0.0 if passed else -abs(raw_value - limit) / scale


def _validate_stage_constraints(
    problem: OptimizationProblem,
    evaluation: CandidateEvaluation,
) -> None:
    rules = tuple(
        item for item in problem.hard_constraints if item.evaluation_stage == evaluation.stage
    )
    outcomes = evaluation.constraints
    if tuple(item.constraint_id for item in outcomes) != tuple(
        item.constraint_id for item in rules
    ):
        raise ValueError("strategy evaluation does not cover every stage hard constraint")
    for rule, outcome in zip(rules, outcomes, strict=True):
        observed = evaluation.metrics.get(rule.metric_id)
        expected_passed, expected_margin = _constraint_result(
            operator=rule.operator,
            raw_value=outcome.raw_value,
            limit=rule.limit,
            scale=rule.normalization_scale,
        )
        if (
            outcome.metric_id != rule.metric_id
            or not _is_close(outcome.limit, rule.limit)
            or observed is None
            or not _is_close(observed, outcome.raw_value)
            or outcome.passed != expected_passed
            or not _is_close(outcome.normalized_margin, expected_margin)
        ):
            raise ValueError("strategy constraint outcome differs from the problem rule")


def _normalized_action_l1(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
) -> float:
    return sum(
        abs(proposal.decision_values[item.variable_id] - item.nominal_value)
        / (item.upper_bound - item.lower_bound)
        for item in problem.decision_domains
    )


def _validate_candidate_evidence(
    problem: OptimizationProblem,
    context: OperatingContext,
    proposal: CandidateProposal,
    static_evaluation: CandidateEvaluation,
    dynamic_evaluation: CandidateEvaluation,
) -> None:
    if not isinstance(problem, OptimizationProblem):
        raise TypeError("problem must be OptimizationProblem")
    if not isinstance(context, OperatingContext):
        raise TypeError("context must be OperatingContext")
    if not isinstance(proposal, CandidateProposal):
        raise TypeError("proposal must be CandidateProposal")
    if not isinstance(static_evaluation, CandidateEvaluation) or not isinstance(
        dynamic_evaluation,
        CandidateEvaluation,
    ):
        raise TypeError("strategy evaluations must be CandidateEvaluation values")
    if (
        problem.context_ref != context.ref
        or proposal.problem_ref != problem.ref
        or proposal.context_ref != context.ref
    ):
        raise ValueError("strategy candidate inputs reference another problem or context")
    if (
        static_evaluation.problem_ref != problem.ref
        or dynamic_evaluation.problem_ref != problem.ref
        or static_evaluation.context_ref != context.ref
        or dynamic_evaluation.context_ref != context.ref
        or static_evaluation.proposal_ref != proposal.ref
        or dynamic_evaluation.proposal_ref != proposal.ref
        or static_evaluation.stage != problem.evaluation_plan.static_stage
        or dynamic_evaluation.stage != problem.evaluation_plan.dynamic_stage
        or static_evaluation.status != "feasible"
        or dynamic_evaluation.status != "feasible"
    ):
        raise ValueError("strategy requires selected feasible M2 and M4 evaluations")
    objective_ids = tuple(item.metric_id for item in problem.objectives)
    if tuple(item.metric_id for item in static_evaluation.objective_outcomes) != objective_ids:
        raise ValueError("selected M2 objective vector differs from the problem")
    for spec, outcome in zip(
        problem.objectives,
        static_evaluation.objective_outcomes,
        strict=True,
    ):
        if (
            outcome.sense != spec.sense
            or outcome.unit != spec.unit
            or outcome.formula_id != spec.formula_id
            or not _is_close(
                outcome.normalized_directional_improvement,
                outcome.directional_absolute_improvement / spec.normalization_scale,
            )
            or (
                outcome.relative_directional_improvement is None
                and abs(outcome.baseline_value) > 1e-12
            )
            or (
                outcome.relative_directional_improvement is not None
                and (
                    abs(outcome.baseline_value) <= 1e-12
                    or not _is_close(
                        outcome.relative_directional_improvement,
                        outcome.directional_absolute_improvement / abs(outcome.baseline_value),
                    )
                )
            )
            or not _is_close(
                static_evaluation.metrics.get(outcome.metric_id, math.nan),
                outcome.candidate_value,
            )
        ):
            raise ValueError("selected M2 objective binding differs from the problem")
    domains = {item.variable_id: item for item in problem.decision_domains}
    if set(proposal.decision_values) != set(domains):
        raise ValueError("selected proposal differs from the problem decision vector")
    if any(
        not domains[key].lower_bound <= value <= domains[key].upper_bound
        for key, value in proposal.decision_values.items()
    ):
        raise ValueError("selected proposal lies outside the problem decision bounds")
    normalized_action = _normalized_action_l1(problem, proposal)
    if not _is_close(static_evaluation.normalized_action_l1, normalized_action) or not _is_close(
        dynamic_evaluation.normalized_action_l1,
        normalized_action,
    ):
        raise ValueError("strategy evaluation action distance differs from the proposal")
    _validate_stage_constraints(problem, static_evaluation)
    _validate_stage_constraints(problem, dynamic_evaluation)


def _validate_selected_evidence(
    problem: OptimizationProblem,
    context: OperatingContext,
    proposal: CandidateProposal,
    static_evaluation: CandidateEvaluation,
    dynamic_evaluation: CandidateEvaluation,
    finalization: FinalizationArtifacts,
) -> None:
    _validate_candidate_evidence(
        problem,
        context,
        proposal,
        static_evaluation,
        dynamic_evaluation,
    )
    if not isinstance(finalization, FinalizationArtifacts):
        raise TypeError("finalization must be FinalizationArtifacts")
    result = finalization.result
    assessment = finalization.publishability
    if result.status != "success" or not result.publishable:
        raise ValueError("strategy draft requires a publishable successful finalization")
    if assessment is None or not assessment.publishable:
        raise ValueError("strategy draft requires a passing publishability assessment")
    if (
        result.problem_ref != problem.ref
        or result.context_ref != context.ref
        or finalization.static_selection.problem_ref != problem.ref
        or finalization.static_selection.context_ref != context.ref
        or result.static_selection_ref != finalization.static_selection.ref
        or assessment.problem_ref != problem.ref
        or assessment.context_ref != context.ref
        or assessment.capability_catalog_ref != problem.capability_catalog_ref
        or assessment.system_policy_ref != problem.system_policy_ref
        or finalization.static_selection.status != "ready"
        or finalization.static_selection.solver_result_ref != result.solver_result_ref
        or finalization.static_selection.ranked_proposal_refs != result.ranked_proposal_refs
        or finalization.static_selection.shortlist_proposal_refs != result.shortlist_proposal_refs
        or result.result_mode != problem.result_request.mode
        or result.maximum_returned_candidates != problem.result_request.maximum_returned_candidates
    ):
        raise ValueError("strategy finalization references another problem or context")
    if (
        result.selected_proposal_ref != proposal.ref
        or result.selected_static_evaluation_ref != static_evaluation.ref
        or result.selected_dynamic_evaluation_ref != dynamic_evaluation.ref
        or assessment.selected_proposal_ref != proposal.ref
        or assessment.selected_static_evaluation_ref != static_evaluation.ref
    ):
        raise ValueError("strategy inputs differ from selected finalization refs")
    rules = problem.publishability_constraints
    if len(assessment.outcomes) != len(rules):
        raise ValueError("publishability assessment differs from problem rules")
    for rule, outcome in zip(rules, assessment.outcomes, strict=True):
        observed = static_evaluation.metrics.get(rule.metric_id)
        expected_passed = (
            observed is not None
            and _constraint_result(
                operator=rule.operator,
                raw_value=observed,
                limit=rule.limit,
                scale=rule.normalization_scale,
            )[0]
        )
        if (
            outcome.guardrail_id != rule.constraint_id
            or outcome.priority != rule.priority
            or outcome.metric_id != rule.metric_id
            or outcome.operator != rule.operator
            or not _is_close(outcome.limit, rule.limit)
            or outcome.observed_value is None
            or observed is None
            or not _is_close(outcome.observed_value, observed)
            or outcome.passed != expected_passed
        ):
            raise ValueError("publishability assessment differs from problem rules")


def anchor_from_verified_candidate(
    problem: OptimizationProblem,
    context: OperatingContext,
    proposal: CandidateProposal,
    static_evaluation: CandidateEvaluation,
    dynamic_evaluation: CandidateEvaluation,
    *,
    finalization_result_ref: ContractRef,
    applicability_values: Mapping[str, float] | None = None,
) -> StrategyAnchor:
    """Build a sampled anchor after fixed-action M2/M4 verification."""

    _validate_candidate_evidence(
        problem,
        context,
        proposal,
        static_evaluation,
        dynamic_evaluation,
    )
    if not isinstance(finalization_result_ref, ContractRef):
        raise TypeError("finalization_result_ref must be ContractRef")
    if (
        static_evaluation.minimum_normalized_margin is None
        or dynamic_evaluation.minimum_normalized_margin is None
    ):
        raise ValueError("verified strategy evaluations lack normalized margins")
    summaries = tuple(
        StrategyObjectiveSummary(
            metric_id=item.metric_id,
            sense=item.sense,
            unit=item.unit,
            formula_id=item.formula_id,
            baseline_value=item.baseline_value,
            candidate_value=item.candidate_value,
            directional_absolute_improvement=item.directional_absolute_improvement,
            relative_directional_improvement=item.relative_directional_improvement,
            normalized_directional_improvement=item.normalized_directional_improvement,
        )
        for item in static_evaluation.objective_outcomes
    )
    return StrategyAnchor(
        context_ref=context.ref,
        context_schema_ref=context.context_schema_ref,
        model_ref=context.model_ref,
        case_ref=context.case_ref,
        operating_mode=context.operating_mode,
        applicability_values=_applicability_values(context, applicability_values),
        action_values=proposal.decision_values,
        problem_ref=problem.ref,
        capability_catalog_ref=problem.capability_catalog_ref,
        system_policy_ref=problem.system_policy_ref,
        proposal_ref=proposal.ref,
        static_evaluation_ref=static_evaluation.ref,
        dynamic_evaluation_ref=dynamic_evaluation.ref,
        finalization_result_ref=finalization_result_ref,
        objective_summaries=summaries,
        minimum_normalized_margin=min(
            static_evaluation.minimum_normalized_margin,
            dynamic_evaluation.minimum_normalized_margin,
        ),
        evidence_refs=canonical_refs(
            tuple(item.ref for item in static_evaluation.evidence_refs)
            + tuple(item.ref for item in dynamic_evaluation.evidence_refs)
        ),
    )


def anchor_from_finalization(
    problem: OptimizationProblem,
    context: OperatingContext,
    proposal: CandidateProposal,
    static_evaluation: CandidateEvaluation,
    dynamic_evaluation: CandidateEvaluation,
    finalization: FinalizationArtifacts,
    *,
    applicability_values: Mapping[str, float] | None = None,
) -> StrategyAnchor:
    """Build one compact anchor from a verified unified finalization."""

    _validate_selected_evidence(
        problem,
        context,
        proposal,
        static_evaluation,
        dynamic_evaluation,
        finalization,
    )
    return anchor_from_verified_candidate(
        problem,
        context,
        proposal,
        static_evaluation,
        dynamic_evaluation,
        finalization_result_ref=finalization.result.ref,
        applicability_values=applicability_values,
    )


class StrategyBuilder:
    """Create an immutable strategy payload only from a publishable unified result."""

    def build(
        self,
        problem: OptimizationProblem,
        context: OperatingContext,
        proposal: CandidateProposal,
        static_evaluation: CandidateEvaluation,
        dynamic_evaluation: CandidateEvaluation,
        finalization: FinalizationArtifacts,
        *,
        additional_anchors: Sequence[StrategyAnchor] = (),
        applicability_values: Mapping[str, float] | None = None,
        revision: int = 1,
        supersedes: ContractRef | None = None,
        strategy_id: str | None = None,
    ) -> StrategyEntry:
        central = anchor_from_finalization(
            problem,
            context,
            proposal,
            static_evaluation,
            dynamic_evaluation,
            finalization,
            applicability_values=applicability_values,
        )
        anchors = (central, *tuple(additional_anchors))
        if any(not isinstance(item, StrategyAnchor) for item in anchors):
            raise TypeError("additional_anchors must contain StrategyAnchor values")
        revision_value = integer(revision, context="revision", minimum=1)
        resolved_id = self._strategy_id(
            problem,
            context,
            revision=revision_value,
            supersedes=supersedes,
            strategy_id=strategy_id,
        )
        domains = {item.variable_id: item for item in problem.decision_domains}
        baseline: dict[str, float] = {}
        for variable_id in domains:
            if variable_id not in context.current_setpoints:
                raise ValueError("context lacks a baseline for one selected decision")
            baseline[variable_id] = context.current_setpoints[variable_id]
        assessment = finalization.publishability
        if assessment is None:  # pragma: no cover - anchor validation guarantees this
            raise ValueError("publishability assessment is missing")
        dependencies = canonical_refs(
            (
                context.context_schema_ref,
                context.model_ref,
                context.case_ref,
                problem.capability_catalog_ref,
                problem.system_policy_ref,
                *(ref for anchor in anchors for ref in anchor.evidence_refs),
            )
        )
        return StrategyEntry(
            schema_version=STRATEGY_SCHEMA_VERSION,
            entry_version="strategy-entry",
            strategy_id=resolved_id,
            revision=revision_value,
            supersedes=supersedes,
            coverage_kind="point" if len(anchors) == 1 else "sampled_anchors",
            central_context_ref=context.ref,
            context_schema_ref=context.context_schema_ref,
            model_ref=context.model_ref,
            case_ref=context.case_ref,
            operating_mode=context.operating_mode,
            anchors=tuple(
                sorted(
                    anchors,
                    key=lambda item: (
                        item.context_ref.object_id,
                        item.context_ref.fingerprint,
                    ),
                )
            ),
            action_values=proposal.decision_values,
            action_units={key: domains[key].canonical_unit for key in domains},
            baseline_values=baseline,
            objective_order=tuple(item.metric_id for item in problem.objectives),
            application_method="step-hold",
            event_time_s=problem.evaluation_plan.m4_event_time_s,
            hold_policy="hold-until-offline-review",
            stop_conditions=(
                "m4-acceptance-fails",
                "required-data-quality-fails",
                "strategy-context-mismatch",
            ),
            problem_ref=problem.ref,
            capability_catalog_ref=problem.capability_catalog_ref,
            system_policy_ref=problem.system_policy_ref,
            solver_result_ref=finalization.result.solver_result_ref,
            static_selection_ref=finalization.static_selection.ref,
            finalization_result_ref=finalization.result.ref,
            publishability_assessment_ref=assessment.ref,
            selected_proposal_ref=proposal.ref,
            selected_static_evaluation_ref=static_evaluation.ref,
            selected_dynamic_evaluation_ref=dynamic_evaluation.ref,
            dependency_refs=dependencies,
            execution_scope="offline_simulation_only",
            control_authority="none",
            field_validated=False,
            dcs_write_capability=False,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    @staticmethod
    def _strategy_id(
        problem: OptimizationProblem,
        context: OperatingContext,
        *,
        revision: int,
        supersedes: ContractRef | None,
        strategy_id: str | None,
    ) -> str:
        if strategy_id is not None:
            resolved = identifier(strategy_id, context="strategy_id")
        elif supersedes is not None:
            suffix = f"-r{revision - 1}"
            if not supersedes.object_id.endswith(suffix):
                raise ValueError("supersedes object_id differs from the prior revision")
            resolved = identifier(
                supersedes.object_id[: -len(suffix)],
                context="strategy_id",
            )
        else:
            identity = canonical_fingerprint(
                {
                    "context_ref": context.ref.as_dict(),
                    "problem_ref": problem.ref.as_dict(),
                    "case_ref": context.case_ref.as_dict(),
                    "operating_mode": context.operating_mode,
                    "objective_order": [item.metric_id for item in problem.objectives],
                    "decision_ids": [item.variable_id for item in problem.decision_domains],
                    "claim_scope": ENGINEERING_CLAIM_SCOPE,
                }
            )
            resolved = f"strategy-{identity[:16]}"
        if revision == 1 and supersedes is not None:
            raise ValueError("revision one cannot supersede another strategy")
        if revision > 1 and supersedes is None:
            raise ValueError("later revisions require a supersedes ref")
        if supersedes is not None and supersedes.object_id != f"{resolved}-r{revision - 1}":
            raise ValueError("strategy_id differs from the superseded revision")
        return resolved


__all__ = [
    "StrategyBuilder",
    "anchor_from_finalization",
    "anchor_from_verified_candidate",
]
