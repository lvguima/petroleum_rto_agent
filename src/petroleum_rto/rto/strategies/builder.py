"""Deterministic construction of immutable R5 strategy drafts."""

from __future__ import annotations

from collections.abc import Sequence

from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    ContractRef,
    OperatingContextV1,
    OptimizationProblemV1,
    OptimizationResultV1,
    StaticSearchResultV1,
)
from .models import StrategyAnchorV1, StrategyEntryV1, strategy_dependencies


def optimization_result_ref(result: OptimizationResultV1) -> ContractRef:
    return ContractRef(result.result_id, result.fingerprint)


def anchor_from_evaluations(
    context: OperatingContextV1,
    proposal: CandidateProposalV1,
    static: CandidateEvaluationV1,
    dynamic: CandidateEvaluationV1,
) -> StrategyAnchorV1:
    """Build one evidence-complete anchor from already evaluated contracts."""

    if proposal.context_ref != context.ref:
        raise ValueError("anchor proposal references another context")
    if static.proposal_ref != proposal.ref or dynamic.proposal_ref != proposal.ref:
        raise ValueError("anchor evaluations reference another proposal")
    if static.baseline_objective is None or static.candidate_objective is None:
        raise ValueError("anchor static evaluation lacks objective values")
    if static.relative_improvement is None or static.minimum_normalized_margin is None:
        raise ValueError("anchor static evaluation lacks improvement or margin")
    if dynamic.minimum_normalized_margin is None:
        raise ValueError("anchor dynamic evaluation lacks a normalized margin")
    source_refs = tuple(
        ContractRef(f"source-{key}", fingerprint)
        for evaluation in (static, dynamic)
        for evidence in (evaluation.baseline_evidence, evaluation.candidate_evidence)
        if evidence is not None
        for key, fingerprint in evidence.source_fingerprints.items()
    )
    return StrategyAnchorV1(
        context_ref=context.ref,
        feed_mass_flow_kg_s=context.feed_mass_flow_kg_s,
        action_setpoints=proposal.decision_values,
        static_evaluation_ref=static.ref,
        dynamic_evaluation_ref=dynamic.ref,
        baseline_objective=static.baseline_objective,
        candidate_objective=static.candidate_objective,
        relative_improvement=static.relative_improvement,
        minimum_normalized_margin=min(
            static.minimum_normalized_margin,
            dynamic.minimum_normalized_margin,
        ),
        evidence_source_refs=source_refs,
    )


class StrategyBuilder:
    """Create a draft only from a publishable result and complete M2/M4 evidence."""

    def build(
        self,
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        static_search: StaticSearchResultV1,
        result: OptimizationResultV1,
        anchors: Sequence[StrategyAnchorV1],
        *,
        revision: int = 1,
        supersedes: ContractRef | None = None,
    ) -> StrategyEntryV1:
        if result.status != "success" or not result.publishable:
            raise ValueError("strategy draft requires a publishable optimization result")
        if result.problem_ref != problem.ref or result.context_ref != context.ref:
            raise ValueError("optimization result references another problem or context")
        if result.static_search_ref != static_search.ref:
            raise ValueError("optimization result references another static search")
        selected_ref = result.selected_proposal_ref
        selected_static_ref = result.selected_static_evaluation_ref
        selected_dynamic_ref = result.selected_dynamic_evaluation_ref
        if selected_ref is None or selected_static_ref is None or selected_dynamic_ref is None:
            raise ValueError("optimization result lacks a complete selected candidate")
        selected = next(
            (item for item in static_search.proposals if item.ref == selected_ref), None
        )
        if selected is None:
            raise ValueError("selected proposal is absent from static search")
        selected_static = next(
            (item for item in static_search.evaluations if item.ref == selected_static_ref),
            None,
        )
        selected_dynamic = next(
            (item for item in result.dynamic_evaluations if item.ref == selected_dynamic_ref),
            None,
        )
        if selected_static is None or selected_dynamic is None:
            raise ValueError("selected evaluations are absent from optimization evidence")
        anchor_values = tuple(anchors)
        if not anchor_values:
            raise ValueError("strategy draft requires at least one anchor")
        central = [item for item in anchor_values if item.context_ref == context.ref]
        if len(central) != 1:
            raise ValueError("strategy anchors require exactly one central operating context")
        if (
            central[0].static_evaluation_ref != selected_static.ref
            or central[0].dynamic_evaluation_ref != selected_dynamic.ref
        ):
            raise ValueError("central strategy anchor differs from the selected result")
        action = dict(selected.decision_values)
        if any(dict(item.action_setpoints) != action for item in anchor_values):
            raise ValueError("all strategy anchors must use the selected action vector")
        dependencies = strategy_dependencies(
            (
                context.model_ref,
                context.case_ref,
                problem.decision_catalog_ref,
                problem.kpi_catalog_ref,
                problem.constraint_profile_ref,
                problem.policy_ref,
                *(ref for anchor in anchor_values for ref in anchor.evidence_source_refs),
            )
        )
        strategy_id = f"strategy-{result.fingerprint[:16]}"
        return StrategyEntryV1(
            schema_version=RTO_SCHEMA_VERSION,
            entry_version="strategy-entry-v1",
            strategy_id=strategy_id,
            revision=revision,
            supersedes=supersedes,
            coverage_kind="point" if len(anchor_values) == 1 else "sampled_anchors",
            case_ref=context.case_ref,
            operating_mode=context.operating_mode,
            anchors=tuple(sorted(anchor_values, key=lambda item: item.feed_mass_flow_kg_s)),
            action_setpoints=action,
            baseline_setpoints=context.current_setpoints,
            application_profile_id="step-hold-v1",
            event_time_s=problem.evaluation_plan.m4_event_time_s,
            hold_policy="hold-until-offline-review",
            stop_conditions=(
                "m4-acceptance-fails",
                "required-data-quality-fails",
                "strategy-context-mismatch",
            ),
            problem_ref=problem.ref,
            optimization_result_ref=optimization_result_ref(result),
            selected_proposal_ref=selected.ref,
            objective_metric_id=problem.objective_metric_id,
            dependency_refs=dependencies,
            execution_scope="offline_simulation_only",
            control_authority="none",
            field_validated=False,
            dcs_write_capability=False,
            claim_scope=CLAIM_SCOPE,
        )
