"""Small, safe RTO projections for user-facing model chat."""

from __future__ import annotations

import math

from ..contracts.context import OperatingContext
from ..orchestration.result import (
    OptimizationAdjustmentSummary,
    OptimizationAlternativeCandidateSummary,
    OptimizationBaselineSummary,
    OptimizationContextSummary,
    OptimizationPredictedEffectSummary,
    OptimizationRunSummary,
    OptimizationTargetSummary,
    build_optimization_run_summary,
)

_ATMOSPHERIC_PRESSURE_PA = 101_325.0
_FURNACE_TEMPERATURE_ID = "furnace_temperature_target_k"
_TOWER_TOP_PRESSURE_ID = "tower_top_pressure_target_pa_a"
_INITIAL_INVENTORY_IDS = ("flash_drum", "reflux_drum", "tower_bottom")


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
    "OptimizationAdjustmentSummary",
    "OptimizationAlternativeCandidateSummary",
    "OptimizationBaselineSummary",
    "OptimizationContextSummary",
    "OptimizationPredictedEffectSummary",
    "OptimizationRunSummary",
    "OptimizationTargetSummary",
    "build_chat_operating_status",
    "build_optimization_run_summary",
]
