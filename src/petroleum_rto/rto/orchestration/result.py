"""Small in-memory optimization result projection shared by orchestration and runtime."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from ..contracts.candidate import (
    CandidateEvaluation,
    CandidateProposal,
    EvaluationStatus,
    ObjectiveOutcome,
)
from ..contracts.common import JsonValue, freeze_json_mapping, thaw_json
from ..contracts.context import OperatingContext
from ..contracts.finalization import FinalizationStatus
from ..contracts.problem import ObjectiveSense, OptimizationProblem
from ..contracts.reference import ContractRef
from ..intent import OptimizationIntent
from .models import (
    CapabilityBundleSnapshot,
    DynamicVerificationArtifact,
    FinalizationArtifact,
    SolverExecutionArtifact,
)

_WORKFLOW_ID_PATTERN = re.compile(r"offline-rto-[0-9a-f]{16}\Z")
_RESULT_SUMMARY_FIELDS = frozenset(
    {
        "status",
        "targets",
        "operating_context",
        "baseline_values",
        "recommended_adjustments",
        "predicted_effects",
        "alternative_candidates",
    }
)


class _OptimizationRunRecord(Protocol):
    """Fields needed to summarize either a completed run record or its stage state."""

    @property
    def context(self) -> OperatingContext: ...

    @property
    def intent(self) -> OptimizationIntent: ...

    @property
    def capability_snapshot(self) -> CapabilityBundleSnapshot: ...

    @property
    def problem(self) -> OptimizationProblem: ...

    @property
    def solver_execution(self) -> SolverExecutionArtifact: ...

    @property
    def dynamic_verification(self) -> DynamicVerificationArtifact: ...

    @property
    def finalization(self) -> FinalizationArtifact: ...


@dataclass(frozen=True, slots=True)
class OptimizationContextSummary:
    """Small trusted-context projection attached to one optimization result."""

    operating_mode: str
    fresh_feed_load_kg_s: float
    fresh_feed_load_t_per_h: float
    data_timestamp: str
    data_quality: str

    def as_dict(self) -> dict[str, object]:
        return {
            "operating_mode": self.operating_mode,
            "fresh_feed_load_kg_s": self.fresh_feed_load_kg_s,
            "fresh_feed_load_t_per_h": self.fresh_feed_load_t_per_h,
            "data_timestamp": self.data_timestamp,
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True, slots=True)
class OptimizationTargetSummary:
    """One business target confirmed for the optimization run."""

    metric_id: str
    business_name: str
    sense: ObjectiveSense
    priority: int
    unit: str

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "business_name": self.business_name,
            "sense": self.sense,
            "priority": self.priority,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class OptimizationBaselineSummary:
    """Baseline value paired with one requested objective."""

    metric_id: str
    value: float
    unit: str

    def as_dict(self) -> dict[str, object]:
        return {"metric_id": self.metric_id, "value": self.value, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class OptimizationAdjustmentSummary:
    """One current setpoint and one candidate replacement."""

    variable_id: str
    business_name: str
    unit: str
    baseline_value: float
    recommended_value: float
    adjustment: float

    def as_dict(self) -> dict[str, object]:
        return {
            "variable_id": self.variable_id,
            "business_name": self.business_name,
            "unit": self.unit,
            "baseline_value": self.baseline_value,
            "recommended_value": self.recommended_value,
            "adjustment": self.adjustment,
        }


@dataclass(frozen=True, slots=True)
class OptimizationPredictedEffectSummary:
    """Predicted objective effect for one candidate."""

    metric_id: str
    predicted_value: float
    unit: str
    directional_improvement: float
    relative_improvement: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "predicted_value": self.predicted_value,
            "unit": self.unit,
            "directional_improvement": self.directional_improvement,
            "relative_improvement": self.relative_improvement,
        }


@dataclass(frozen=True, slots=True)
class OptimizationAlternativeCandidateSummary:
    """One ranked non-selected candidate with its actual verification state."""

    rank: int
    adjustments: tuple[OptimizationAdjustmentSummary, ...]
    predicted_effects: tuple[OptimizationPredictedEffectSummary, ...]
    verification_stage: Literal["M2", "M4"]
    verification_status: EvaluationStatus

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("alternative candidate rank must be a positive integer")
        adjustments = tuple(self.adjustments)
        effects = tuple(self.predicted_effects)
        if not adjustments or any(
            not isinstance(item, OptimizationAdjustmentSummary) for item in adjustments
        ):
            raise TypeError("alternative candidate adjustments must be non-empty summaries")
        if not effects or any(
            not isinstance(item, OptimizationPredictedEffectSummary) for item in effects
        ):
            raise TypeError("alternative candidate effects must be non-empty summaries")
        variable_ids = tuple(item.variable_id for item in adjustments)
        metric_ids = tuple(item.metric_id for item in effects)
        if len(variable_ids) != len(set(variable_ids)):
            raise ValueError("alternative candidate adjustments must be unique")
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("alternative candidate effects must be unique")
        if self.verification_stage not in {"M2", "M4"}:
            raise ValueError("alternative candidate verification_stage must be M2 or M4")
        if self.verification_status not in {
            "feasible",
            "process_infeasible",
            "invalid_request",
            "evaluation_error",
            "not_evaluated",
        }:
            raise ValueError("unsupported alternative candidate verification status")
        if self.verification_stage == "M2" and self.verification_status != "feasible":
            raise ValueError("a ranked M2-only alternative must be statically feasible")
        object.__setattr__(self, "adjustments", adjustments)
        object.__setattr__(self, "predicted_effects", effects)

    def as_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "adjustments": [item.as_dict() for item in self.adjustments],
            "predicted_effects": [item.as_dict() for item in self.predicted_effects],
            "verification_stage": self.verification_stage,
            "verification_status": self.verification_status,
        }


@dataclass(frozen=True, slots=True)
class OptimizationRunSummary:
    """Typed, model-safe projection of one completed optimization run."""

    status: FinalizationStatus
    targets: tuple[OptimizationTargetSummary, ...]
    operating_context: OptimizationContextSummary
    baseline_values: tuple[OptimizationBaselineSummary, ...]
    recommended_adjustments: tuple[OptimizationAdjustmentSummary, ...]
    predicted_effects: tuple[OptimizationPredictedEffectSummary, ...]
    alternative_candidates: tuple[OptimizationAlternativeCandidateSummary, ...]

    def __post_init__(self) -> None:
        alternatives = tuple(self.alternative_candidates)
        if any(
            not isinstance(item, OptimizationAlternativeCandidateSummary) for item in alternatives
        ):
            raise TypeError("alternative_candidates must contain candidate summaries")
        ranks = tuple(item.rank for item in alternatives)
        if ranks != tuple(sorted(ranks)) or len(ranks) != len(set(ranks)):
            raise ValueError("alternative candidates must have unique ascending ranks")
        object.__setattr__(self, "alternative_candidates", alternatives)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "targets": [item.as_dict() for item in self.targets],
            "operating_context": self.operating_context.as_dict(),
            "baseline_values": [item.as_dict() for item in self.baseline_values],
            "recommended_adjustments": [item.as_dict() for item in self.recommended_adjustments],
            "predicted_effects": [item.as_dict() for item in self.predicted_effects],
            "alternative_candidates": [item.as_dict() for item in self.alternative_candidates],
        }


@dataclass(frozen=True, slots=True)
class OptimizationRunReceipt:
    """Stable locator and compact result returned after one confirmed run."""

    workflow_id: str
    result_source: str
    result_summary: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.workflow_id, str)
            or _WORKFLOW_ID_PATTERN.fullmatch(self.workflow_id) is None
        ):
            raise ValueError("workflow_id must match the offline RTO workflow format")
        expected_source = f"{self.workflow_id}/result.json"
        if self.result_source != expected_source:
            raise ValueError("result_source must be the controlled workflow-relative result path")
        if not isinstance(self.result_summary, Mapping):
            raise TypeError("result_summary must be a mapping")
        summary = freeze_json_mapping(self.result_summary, context="result_summary")
        if set(summary) != _RESULT_SUMMARY_FIELDS:
            raise ValueError("result_summary fields differ from the compact result contract")
        object.__setattr__(self, "result_summary", summary)

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "result_source": self.result_source,
            "result_summary": thaw_json(cast(JsonValue, self.result_summary)),
        }


def _find_by_ref(values: tuple[object, ...], reference: object, *, context: str) -> object:
    matches = tuple(item for item in values if getattr(item, "ref", None) == reference)
    if len(matches) != 1:
        raise ValueError(f"{context} must resolve to exactly one in-memory object")
    return matches[0]


def _selected_artifacts(
    record: _OptimizationRunRecord,
) -> tuple[CandidateProposal, CandidateEvaluation, CandidateEvaluation] | None:
    result = record.finalization.result
    if result.selected_proposal_ref is None:
        return None
    if (
        result.selected_static_evaluation_ref is None
        or result.selected_dynamic_evaluation_ref is None
    ):
        raise ValueError("selected result lacks complete evaluation references")
    proposal = _find_by_ref(
        record.solver_execution.result.proposals,
        result.selected_proposal_ref,
        context="selected proposal",
    )
    static = _find_by_ref(
        record.solver_execution.result.evaluations,
        result.selected_static_evaluation_ref,
        context="selected static evaluation",
    )
    dynamic = _find_by_ref(
        record.dynamic_verification.evaluations,
        result.selected_dynamic_evaluation_ref,
        context="selected dynamic evaluation",
    )
    if not isinstance(proposal, CandidateProposal):
        raise TypeError("selected proposal has an unexpected type")
    if not isinstance(static, CandidateEvaluation) or not isinstance(dynamic, CandidateEvaluation):
        raise TypeError("selected evaluation has an unexpected type")
    if (
        static.stage != "M2"
        or static.status != "feasible"
        or static.proposal_ref != proposal.ref
        or dynamic.stage != "M4"
        or dynamic.status != "feasible"
        or dynamic.proposal_ref != proposal.ref
    ):
        raise ValueError("selected evaluations differ from the selected proposal")
    return proposal, static, dynamic


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def _context_summary(context: OperatingContext) -> OptimizationContextSummary:
    feed_kg_s = _positive_number(
        context.facts.get("fresh_feed_load_kg_s"), name="fresh_feed_load_kg_s"
    )
    return OptimizationContextSummary(
        operating_mode=context.operating_mode,
        fresh_feed_load_kg_s=feed_kg_s,
        fresh_feed_load_t_per_h=round(feed_kg_s * 3.6, 12),
        data_timestamp=context.data_timestamp,
        data_quality=context.data_quality,
    )


def _outcomes_by_metric(
    evaluation: CandidateEvaluation,
    expected_metrics: set[str],
    *,
    context: str,
) -> dict[str, ObjectiveOutcome]:
    if evaluation.stage != "M2" or evaluation.status != "feasible":
        raise ValueError(f"{context} must use one feasible M2 evaluation")
    outcomes = {item.metric_id: item for item in evaluation.objective_outcomes}
    if len(outcomes) != len(evaluation.objective_outcomes):
        raise ValueError(f"{context} contains duplicate objective metrics")
    if set(outcomes) != expected_metrics:
        raise ValueError(f"{context} objective outcomes differ from the optimization targets")
    return outcomes


def _adjustments(
    record: _OptimizationRunRecord,
    proposal: CandidateProposal,
) -> tuple[OptimizationAdjustmentSummary, ...]:
    decision_capabilities = {
        item.decision_id: item for item in record.capability_snapshot.bundle.catalog.decisions
    }
    expected_decisions = {item.variable_id for item in record.problem.decision_domains}
    if set(proposal.decision_values) != expected_decisions:
        raise ValueError("candidate proposal differs from the problem decision domains")
    result: list[OptimizationAdjustmentSummary] = []
    for domain in record.problem.decision_domains:
        capability = decision_capabilities.get(domain.variable_id)
        if capability is None:
            raise ValueError("problem decision is absent from the capability snapshot")
        baseline = record.context.current_setpoints.get(domain.variable_id)
        if baseline is None:
            raise ValueError("operating context lacks a candidate decision baseline")
        recommended = proposal.decision_values[domain.variable_id]
        result.append(
            OptimizationAdjustmentSummary(
                variable_id=domain.variable_id,
                business_name=capability.business_name,
                unit=domain.canonical_unit,
                baseline_value=baseline,
                recommended_value=recommended,
                adjustment=recommended - baseline,
            )
        )
    return tuple(result)


def _predicted_effects(
    problem: OptimizationProblem,
    outcomes: Mapping[str, ObjectiveOutcome],
) -> tuple[OptimizationPredictedEffectSummary, ...]:
    result: list[OptimizationPredictedEffectSummary] = []
    for spec in problem.objectives:
        outcome = outcomes[spec.metric_id]
        if outcome.sense != spec.sense or outcome.unit != spec.unit:
            raise ValueError("candidate objective outcome differs from the problem contract")
        result.append(
            OptimizationPredictedEffectSummary(
                metric_id=spec.metric_id,
                predicted_value=outcome.candidate_value,
                unit=spec.unit,
                directional_improvement=outcome.directional_absolute_improvement,
                relative_improvement=outcome.relative_directional_improvement,
            )
        )
    return tuple(result)


def _proposal_for_ref(
    record: _OptimizationRunRecord,
    proposal_ref: ContractRef,
) -> CandidateProposal:
    proposal = _find_by_ref(
        record.solver_execution.result.proposals,
        proposal_ref,
        context="candidate proposal",
    )
    if not isinstance(proposal, CandidateProposal):
        raise TypeError("candidate proposal has an unexpected type")
    return proposal


def _static_evaluation_for_proposal(
    record: _OptimizationRunRecord,
    proposal_ref: ContractRef,
) -> CandidateEvaluation:
    matches = tuple(
        item
        for item in record.solver_execution.result.evaluations
        if getattr(item, "proposal_ref", None) == proposal_ref
    )
    if len(matches) != 1:
        raise ValueError("candidate must resolve to exactly one in-memory M2 evaluation")
    evaluation = matches[0]
    if not isinstance(evaluation, CandidateEvaluation):
        raise TypeError("candidate M2 evaluation has an unexpected type")
    return evaluation


def _dynamic_evaluations_by_proposal(
    record: _OptimizationRunRecord,
) -> dict[ContractRef, CandidateEvaluation]:
    final = record.finalization.result
    actual = record.dynamic_verification.evaluations
    if len(actual) != len(final.dynamic_evaluation_refs):
        raise ValueError("dynamic verification differs from finalization references")
    result: dict[ContractRef, CandidateEvaluation] = {}
    for proposal_ref, evaluation_ref in zip(
        final.dynamic_proposal_refs,
        final.dynamic_evaluation_refs,
        strict=True,
    ):
        evaluation = _find_by_ref(
            actual,
            evaluation_ref,
            context="candidate dynamic evaluation",
        )
        if not isinstance(evaluation, CandidateEvaluation):
            raise TypeError("candidate M4 evaluation has an unexpected type")
        if evaluation.stage != "M4" or evaluation.proposal_ref != proposal_ref:
            raise ValueError("candidate M4 evaluation differs from finalization")
        if proposal_ref in result:
            raise ValueError("dynamic candidate proposals must be unique")
        result[proposal_ref] = evaluation
    return result


def build_optimization_run_summary(record: _OptimizationRunRecord) -> OptimizationRunSummary:
    """Project an in-memory orchestrator result without reading persisted evidence."""

    selected = _selected_artifacts(record)
    priorities = {item.metric_id: item.priority for item in record.intent.objectives}
    expected_metrics = {item.metric_id for item in record.problem.objectives}
    if set(priorities) != expected_metrics:
        raise ValueError("intent objectives differ from the optimization problem")
    objective_capabilities = {
        item.metric_id: item for item in record.capability_snapshot.bundle.catalog.objectives
    }
    targets: list[OptimizationTargetSummary] = []
    for spec in record.problem.objectives:
        capability = objective_capabilities.get(spec.metric_id)
        if capability is None:
            raise ValueError("problem objective is absent from the capability snapshot")
        targets.append(
            OptimizationTargetSummary(
                metric_id=spec.metric_id,
                business_name=capability.business_name,
                sense=spec.sense,
                priority=priorities[spec.metric_id],
                unit=spec.unit,
            )
        )
    outcomes: dict[str, ObjectiveOutcome] = {}
    if selected is not None:
        outcomes = _outcomes_by_metric(
            selected[1],
            expected_metrics,
            context="selected candidate",
        )

    baseline_values = tuple(
        OptimizationBaselineSummary(
            metric_id=spec.metric_id,
            value=outcomes[spec.metric_id].baseline_value,
            unit=spec.unit,
        )
        for spec in record.problem.objectives
        if spec.metric_id in outcomes
    )
    predicted_effects = () if selected is None else _predicted_effects(record.problem, outcomes)
    adjustments = () if selected is None else _adjustments(record, selected[0])

    final = record.finalization.result
    maximum_candidates = final.maximum_returned_candidates
    if (
        maximum_candidates != record.problem.result_request.maximum_returned_candidates
        or maximum_candidates != record.intent.result_request.max_candidates
    ):
        raise ValueError("candidate output limit differs across intent, problem, and result")
    ranking = tuple(final.ranked_proposal_refs)
    returned = tuple(final.returned_proposal_refs)
    if len(ranking) != len(set(ranking)) or len(returned) != len(set(returned)):
        raise ValueError("candidate result references must be unique")
    rank_by_ref = {proposal_ref: rank for rank, proposal_ref in enumerate(ranking, start=1)}
    if any(proposal_ref not in rank_by_ref for proposal_ref in returned):
        raise ValueError("returned candidate is absent from the final ranking")
    if returned != tuple(sorted(returned, key=rank_by_ref.__getitem__)):
        raise ValueError("returned candidates must preserve final ranking order")

    alternative_candidates: list[OptimizationAlternativeCandidateSummary] = []
    if selected is not None:
        selected_ref = selected[0].ref
        limit = max(0, maximum_candidates - 1)
        alternative_refs = tuple(
            proposal_ref for proposal_ref in returned if proposal_ref != selected_ref
        )[:limit]
        dynamic_by_proposal = _dynamic_evaluations_by_proposal(record)
        for proposal_ref in alternative_refs:
            proposal = _proposal_for_ref(record, proposal_ref)
            static = _static_evaluation_for_proposal(record, proposal_ref)
            alternative_outcomes = _outcomes_by_metric(
                static,
                expected_metrics,
                context="alternative candidate",
            )
            dynamic = dynamic_by_proposal.get(proposal_ref)
            alternative_candidates.append(
                OptimizationAlternativeCandidateSummary(
                    rank=rank_by_ref[proposal_ref],
                    adjustments=_adjustments(record, proposal),
                    predicted_effects=_predicted_effects(
                        record.problem,
                        alternative_outcomes,
                    ),
                    verification_stage="M2" if dynamic is None else "M4",
                    verification_status=static.status if dynamic is None else dynamic.status,
                )
            )

    return OptimizationRunSummary(
        status=record.finalization.result.status,
        targets=tuple(targets),
        operating_context=_context_summary(record.context),
        baseline_values=baseline_values,
        recommended_adjustments=adjustments,
        predicted_effects=predicted_effects,
        alternative_candidates=tuple(alternative_candidates),
    )


__all__ = [
    "OptimizationAdjustmentSummary",
    "OptimizationAlternativeCandidateSummary",
    "OptimizationBaselineSummary",
    "OptimizationContextSummary",
    "OptimizationPredictedEffectSummary",
    "OptimizationRunReceipt",
    "OptimizationRunSummary",
    "OptimizationTargetSummary",
    "build_optimization_run_summary",
]
