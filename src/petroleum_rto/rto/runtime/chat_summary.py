"""Small, safe RTO result projection for user-facing model chat."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Protocol

from ..contracts.candidate import CandidateEvaluation, CandidateProposal, ObjectiveOutcome
from ..contracts.context import OperatingContext
from ..contracts.evaluation import CandidateEvaluationV1
from ..contracts.models import CandidateProposalV1
from ..contracts.results_v2 import (
    CandidateEvaluationV2,
    CandidateProposalV2,
    ObjectiveOutcomeV2,
)
from ..orchestration import (
    LegacyOfflineRtoRunRecordV1,
    OfflineRtoRunRecordV2,
    UnifiedOfflineRtoRunRecord,
)

type ChatCompatibleRunRecord = (
    UnifiedOfflineRtoRunRecord | LegacyOfflineRtoRunRecordV1 | OfflineRtoRunRecordV2
)

_CLAIM_SCOPE = "engineering_simulation_only"
_ATMOSPHERIC_PRESSURE_PA = 101_325.0
_FURNACE_TEMPERATURE_ID = "furnace_temperature_target_k"
_TOWER_TOP_PRESSURE_ID = "tower_top_pressure_target_pa_a"
_INITIAL_INVENTORY_IDS = ("flash_drum", "reflux_drum", "tower_bottom")
_LEGACY_V1_OBJECTIVE_UNITS = {
    "specific_furnace_fuel_energy_mj_per_t": "MJ/t",
}


class _DecisionDomainLike(Protocol):
    @property
    def variable_id(self) -> str: ...

    @property
    def canonical_unit(self) -> str: ...


def _setpoints(
    values: Mapping[str, float],
    domains: Iterable[_DecisionDomainLike],
) -> list[dict[str, object]]:
    units = {domain.variable_id: domain.canonical_unit for domain in domains}
    if set(values) != set(units):
        raise ValueError("selected proposal differs from the problem decision domains")
    return [
        {
            "variable_id": variable_id,
            "value": values[variable_id],
            "unit": units[variable_id],
        }
        for variable_id in sorted(values)
    ]


def _constraints(*evaluations: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for evaluation in evaluations:
        if not isinstance(
            evaluation,
            (CandidateEvaluation, CandidateEvaluationV1, CandidateEvaluationV2),
        ):
            raise TypeError("constraint summary requires a typed candidate evaluation")
        result.extend(
            {
                "stage": evaluation.stage,
                "constraint_id": item.constraint_id,
                "passed": item.passed,
            }
            for item in evaluation.constraints
        )
    return result


def _objective(outcome: ObjectiveOutcome | ObjectiveOutcomeV2) -> dict[str, object]:
    return {
        "metric_id": outcome.metric_id,
        "sense": outcome.sense,
        "unit": outcome.unit,
        "baseline_value": outcome.baseline_value,
        "candidate_value": outcome.candidate_value,
        "directional_improvement": outcome.directional_absolute_improvement,
    }


def _empty(status: str) -> dict[str, object]:
    return _summary(
        status=status,
        selected_setpoints=[],
        objectives=[],
        constraints=[],
        publishable=False,
    )


def _summary(
    *,
    status: str,
    selected_setpoints: list[dict[str, object]],
    objectives: list[dict[str, object]],
    constraints: list[dict[str, object]],
    publishable: bool,
) -> dict[str, object]:
    return {
        "status": status,
        "selected_setpoints": selected_setpoints,
        "objectives": objectives,
        "constraints": constraints,
        "publishable": publishable,
        "claim_scope": _CLAIM_SCOPE,
        "field_validated": False,
        "control_authority": "none",
    }


def _find_by_ref(values: Iterable[object], reference: object, *, context: str) -> object:
    matches = tuple(item for item in values if getattr(item, "ref", None) == reference)
    if len(matches) != 1:
        raise ValueError(f"{context} must resolve to exactly one stored object")
    return matches[0]


def _unified(record: UnifiedOfflineRtoRunRecord) -> dict[str, object]:
    result = record.finalization.result
    if result.selected_proposal_ref is None:
        return _empty(result.status)
    if (
        result.selected_static_evaluation_ref is None
        or result.selected_dynamic_evaluation_ref is None
    ):
        raise ValueError("selected unified result lacks complete evaluation references")
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
    if not isinstance(proposal, CandidateProposal) or not isinstance(static, CandidateEvaluation):
        raise TypeError("unified selected artifacts have unexpected types")
    if not isinstance(dynamic, CandidateEvaluation):
        raise TypeError("unified dynamic evaluation has an unexpected type")
    constraint_summary = _constraints(static, dynamic)
    if record.finalization.publishability is not None:
        constraint_summary.extend(
            {
                "stage": "post_selection",
                "constraint_id": item.guardrail_id,
                "passed": item.passed,
            }
            for item in record.finalization.publishability.outcomes
        )
    return _summary(
        status=result.status,
        selected_setpoints=_setpoints(proposal.decision_values, record.problem.decision_domains),
        objectives=[_objective(item) for item in static.objective_outcomes],
        constraints=constraint_summary,
        publishable=result.publishable,
    )


def _legacy_v1(record: LegacyOfflineRtoRunRecordV1) -> dict[str, object]:
    result = record.optimization_result
    if result.selected_proposal_ref is None:
        return _empty(result.status)
    if (
        result.selected_static_evaluation_ref is None
        or result.selected_dynamic_evaluation_ref is None
    ):
        raise ValueError("selected legacy V1 result lacks complete evaluation references")
    proposal = _find_by_ref(
        record.static_search.proposals,
        result.selected_proposal_ref,
        context="selected legacy V1 proposal",
    )
    static = _find_by_ref(
        record.static_search.evaluations,
        result.selected_static_evaluation_ref,
        context="selected legacy V1 static evaluation",
    )
    dynamic = _find_by_ref(
        result.dynamic_evaluations,
        result.selected_dynamic_evaluation_ref,
        context="selected legacy V1 dynamic evaluation",
    )
    if not isinstance(proposal, CandidateProposalV1) or not isinstance(
        static, CandidateEvaluationV1
    ):
        raise TypeError("legacy V1 selected artifacts have unexpected types")
    if not isinstance(dynamic, CandidateEvaluationV1):
        raise TypeError("legacy V1 dynamic evaluation has an unexpected type")
    if (
        static.baseline_objective is None
        or static.candidate_objective is None
        or static.objective_metric_id is None
    ):
        raise ValueError("selected legacy V1 evaluation lacks objective values")
    try:
        objective_unit = _LEGACY_V1_OBJECTIVE_UNITS[static.objective_metric_id]
    except KeyError as exc:
        raise ValueError("selected legacy V1 objective has no safe display unit") from exc
    improvement = (
        static.baseline_objective - static.candidate_objective
        if record.problem.objective_sense == "minimize"
        else static.candidate_objective - static.baseline_objective
    )
    return _summary(
        status=result.status,
        selected_setpoints=_setpoints(proposal.decision_values, record.problem.decision_domains),
        objectives=[
            {
                "metric_id": static.objective_metric_id,
                "sense": record.problem.objective_sense,
                "unit": objective_unit,
                "baseline_value": static.baseline_objective,
                "candidate_value": static.candidate_objective,
                "directional_improvement": improvement,
            }
        ],
        constraints=_constraints(static, dynamic),
        publishable=result.publishable,
    )


def _legacy_v2(record: OfflineRtoRunRecordV2) -> dict[str, object]:
    result = record.optimization_result
    if result.selected_proposal_ref is None:
        return _empty(result.status)
    if (
        result.selected_static_evaluation_ref is None
        or result.selected_dynamic_evaluation_ref is None
    ):
        raise ValueError("selected legacy V2 result lacks complete evaluation references")
    proposal = _find_by_ref(
        record.pareto_search.proposals,
        result.selected_proposal_ref,
        context="selected legacy V2 proposal",
    )
    static = _find_by_ref(
        record.pareto_search.evaluations,
        result.selected_static_evaluation_ref,
        context="selected legacy V2 static evaluation",
    )
    if record.dynamic_verification is None:
        raise ValueError("selected legacy V2 result lacks dynamic verification")
    dynamic = _find_by_ref(
        record.dynamic_verification.evaluations,
        result.selected_dynamic_evaluation_ref,
        context="selected legacy V2 dynamic evaluation",
    )
    if not isinstance(proposal, CandidateProposalV2) or not isinstance(
        static, CandidateEvaluationV2
    ):
        raise TypeError("legacy V2 selected artifacts have unexpected types")
    if not isinstance(dynamic, CandidateEvaluationV2):
        raise TypeError("legacy V2 dynamic evaluation has an unexpected type")
    return _summary(
        status=result.status,
        selected_setpoints=_setpoints(proposal.decision_values, record.problem.decision_domains),
        objectives=[_objective(item) for item in result.selected_objectives],
        constraints=_constraints(static, dynamic),
        publishable=result.publishable,
    )


def build_chat_result_summary(record: ChatCompatibleRunRecord) -> dict[str, object]:
    """Project one strictly loaded RTO run into a small model-safe result summary."""

    if isinstance(record, UnifiedOfflineRtoRunRecord):
        return _unified(record)
    if isinstance(record, LegacyOfflineRtoRunRecordV1):
        return _legacy_v1(record)
    if isinstance(record, OfflineRtoRunRecordV2):
        return _legacy_v2(record)
    raise TypeError("record must be a strictly loaded unified, legacy V1, or legacy V2 run")


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def build_chat_operating_status(context: OperatingContext) -> dict[str, object]:
    """Project one trusted simulation context into a small user-facing status summary."""

    if not isinstance(context, OperatingContext):
        raise TypeError("context must be a strictly loaded OperatingContext")
    expected_setpoints = {_FURNACE_TEMPERATURE_ID, _TOWER_TOP_PRESSURE_ID}
    if set(context.current_setpoints) != expected_setpoints:
        raise ValueError("operating context must define the two supported CDU setpoints")
    if set(context.initial_state) != set(_INITIAL_INVENTORY_IDS):
        raise ValueError("operating context must define the supported inventory ratios")

    feed_kg_s = _positive_number(
        context.facts.get("fresh_feed_load_kg_s"),
        name="fresh_feed_load_kg_s",
    )
    furnace_k = _positive_number(
        context.current_setpoints[_FURNACE_TEMPERATURE_ID],
        name=_FURNACE_TEMPERATURE_ID,
    )
    pressure_pa_a = _positive_number(
        context.current_setpoints[_TOWER_TOP_PRESSURE_ID],
        name=_TOWER_TOP_PRESSURE_ID,
    )
    if pressure_pa_a <= _ATMOSPHERIC_PRESSURE_PA:
        raise ValueError("tower top absolute pressure must exceed atmospheric pressure")

    return {
        "state_kind": "configured_simulation_context",
        "simulator_mode": "on_demand_offline",
        "simulator_state": "idle",
        "simulation_executed_for_this_query": False,
        "operating_mode": context.operating_mode,
        "fresh_feed_load": {
            "kg_per_s": feed_kg_s,
            "t_per_h": round(feed_kg_s * 3.6, 12),
        },
        "current_setpoints": [
            {
                "variable_id": _FURNACE_TEMPERATURE_ID,
                "value_k": furnace_k,
                "value_deg_c": round(furnace_k - 273.15, 12),
            },
            {
                "variable_id": _TOWER_TOP_PRESSURE_ID,
                "value_pa_a": pressure_pa_a,
                "value_mpa_a": round(pressure_pa_a / 1_000_000.0, 12),
                "value_mpa_g": round(
                    (pressure_pa_a - _ATMOSPHERIC_PRESSURE_PA) / 1_000_000.0,
                    12,
                ),
            },
        ],
        "initial_inventory_ratios": {
            name: _positive_number(context.initial_state[name], name=name)
            for name in _INITIAL_INVENTORY_IDS
        },
        "data_timestamp": context.data_timestamp,
        "data_quality": context.data_quality,
        "claim_scope": context.claim_scope,
        "live_plant_data": False,
        "field_validated": False,
        "control_authority": "none",
    }


__all__ = [
    "ChatCompatibleRunRecord",
    "build_chat_operating_status",
    "build_chat_result_summary",
]
