"""Small, safe RTO projections for user-facing model chat."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Protocol

from ..contracts.candidate import CandidateEvaluation, CandidateProposal, ObjectiveOutcome
from ..contracts.context import OperatingContext
from ..orchestration import OfflineRtoRunRecord

type ChatCompatibleRunRecord = OfflineRtoRunRecord

_ATMOSPHERIC_PRESSURE_PA = 101_325.0
_FURNACE_TEMPERATURE_ID = "furnace_temperature_target_k"
_TOWER_TOP_PRESSURE_ID = "tower_top_pressure_target_pa_a"
_INITIAL_INVENTORY_IDS = ("flash_drum", "reflux_drum", "tower_bottom")


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


def _constraints(*evaluations: CandidateEvaluation) -> list[dict[str, object]]:
    return [
        {
            "stage": evaluation.stage,
            "constraint_id": item.constraint_id,
            "passed": item.passed,
        }
        for evaluation in evaluations
        for item in evaluation.constraints
    ]


def _objective(outcome: ObjectiveOutcome) -> dict[str, object]:
    return {
        "metric_id": outcome.metric_id,
        "sense": outcome.sense,
        "unit": outcome.unit,
        "baseline_value": outcome.baseline_value,
        "candidate_value": outcome.candidate_value,
        "directional_improvement": outcome.directional_absolute_improvement,
    }


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
    }


def _find_by_ref(values: Iterable[object], reference: object, *, context: str) -> object:
    matches = tuple(item for item in values if getattr(item, "ref", None) == reference)
    if len(matches) != 1:
        raise ValueError(f"{context} must resolve to exactly one stored object")
    return matches[0]


def build_chat_result_summary(record: OfflineRtoRunRecord) -> dict[str, object]:
    """Project one strictly loaded run into a model-safe summary."""

    if not isinstance(record, OfflineRtoRunRecord):
        raise TypeError("record must be a strictly loaded offline RTO run")
    result = record.finalization.result
    if result.selected_proposal_ref is None:
        return _summary(
            status=result.status,
            selected_setpoints=[],
            objectives=[],
            constraints=[],
            publishable=False,
        )
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


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def build_chat_operating_status(context: OperatingContext) -> dict[str, object]:
    """Project one trusted simulation context into a user-facing status summary."""

    if not isinstance(context, OperatingContext):
        raise TypeError("context must be a strictly loaded OperatingContext")
    expected_setpoints = {_FURNACE_TEMPERATURE_ID, _TOWER_TOP_PRESSURE_ID}
    if set(context.current_setpoints) != expected_setpoints:
        raise ValueError("operating context must define the two supported CDU setpoints")
    if set(context.initial_state) != set(_INITIAL_INVENTORY_IDS):
        raise ValueError("operating context must define the supported inventory ratios")
    feed_kg_s = _positive_number(
        context.facts.get("fresh_feed_load_kg_s"), name="fresh_feed_load_kg_s"
    )
    furnace_k = _positive_number(
        context.current_setpoints[_FURNACE_TEMPERATURE_ID], name=_FURNACE_TEMPERATURE_ID
    )
    pressure_pa_a = _positive_number(
        context.current_setpoints[_TOWER_TOP_PRESSURE_ID], name=_TOWER_TOP_PRESSURE_ID
    )
    if pressure_pa_a <= _ATMOSPHERIC_PRESSURE_PA:
        raise ValueError("tower top absolute pressure must exceed atmospheric pressure")
    return {
        "state_kind": "configured_simulation_context",
        "simulator_mode": "on_demand_offline",
        "simulator_state": "idle",
        "operating_mode": context.operating_mode,
        "fresh_feed_load": {"kg_per_s": feed_kg_s, "t_per_h": round(feed_kg_s * 3.6, 12)},
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
                "value_mpa_g": round((pressure_pa_a - _ATMOSPHERIC_PRESSURE_PA) / 1_000_000.0, 12),
            },
        ],
        "initial_inventory_ratios": {
            name: _positive_number(context.initial_state[name], name=name)
            for name in _INITIAL_INVENTORY_IDS
        },
        "data_timestamp": context.data_timestamp,
        "data_quality": context.data_quality,
    }


__all__ = [
    "ChatCompatibleRunRecord",
    "build_chat_operating_status",
    "build_chat_result_summary",
]
