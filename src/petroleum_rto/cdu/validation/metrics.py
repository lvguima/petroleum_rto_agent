"""Compact, deterministic M6 metrics extracted from accepted model results."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, cast

from ..control.config import REQUIRED_CONTROL_LOOP_IDS
from ..control.results import ClosedLoopSimulationResult
from ..dynamics.simulation import DynamicSimulationResult
from ..dynamics.state import LIQUID_INVENTORY_NAMES
from ..flowsheet.recycle import RecycleSolveResult

HYDROCARBON_PRODUCT_NAMES: Final[tuple[str, ...]] = (
    "gasoline",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
)

STEADY_OUTPUT_IDS: Final[tuple[str, ...]] = (
    *(f"product_flow_kg_s.{name}" for name in HYDROCARBON_PRODUCT_NAMES),
    *(f"product_yield_fraction.{name}" for name in HYDROCARBON_PRODUCT_NAMES),
    "energy.furnace_fuel_duty_w",
    "energy.actual_recovered_duty_w",
    "energy.potential_recovered_duty_w",
    "energy.pump_around_removed_duty_w",
    "quality.gasoline.t90_k_proxy",
    "quality.light_diesel.t90_k_proxy",
    "quality.heavy_diesel.t90_k_proxy",
    "quality.residue.density_kg_m3_proxy",
)


def _finite_mapping(values: Mapping[str, float], *, context: str) -> Mapping[str, float]:
    copied: dict[str, float] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name:
            raise TypeError(f"{context} names must be non-empty strings")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{context}.{name} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{context}.{name} must be finite")
        copied[name] = number
    return MappingProxyType(copied)


def _absolute_error_integral(
    times_s: tuple[float, ...],
    actual: tuple[float, ...],
    reference: tuple[float, ...],
) -> float:
    if not (len(times_s) == len(actual) == len(reference)) or not times_s:
        raise ValueError("tracking integral vectors must be non-empty and aligned")
    errors = tuple(abs(value - target) for value, target in zip(actual, reference))
    return math.fsum(
        0.5 * (errors[index - 1] + errors[index])
        * (times_s[index] - times_s[index - 1])
        for index in range(1, len(times_s))
    )


def _first_reference_change(
    reference: tuple[float, ...],
) -> int | None:
    """Return the first commanded step index, ignoring round-off noise."""

    if not reference:
        return None
    baseline = reference[0]
    tolerance = 1e-12 * max(abs(baseline), 1.0)
    return next(
        (
            index
            for index, value in enumerate(reference[1:], start=1)
            if abs(value - baseline) > tolerance
        ),
        None,
    )


def _response_t63_s(
    times_s: tuple[float, ...],
    response: tuple[float, ...],
    reference: tuple[float, ...],
) -> float:
    """Return first 63.2% response time relative to the first reference step.

    Dynamic samples at an event time retain the left-limit plant state while
    carrying the right-continuous command.  That sample therefore provides the
    unambiguous response origin.  A trace with no reference step or no material
    response returns zero; otherwise the threshold crossing is linearly
    interpolated between adjacent samples.
    """

    if not (len(times_s) == len(response) == len(reference)) or not times_s:
        raise ValueError("response-time vectors must be non-empty and aligned")
    event_index = _first_reference_change(reference)
    if event_index is None:
        return 0.0
    origin = response[event_index]
    final = response[-1]
    delta = final - origin
    tolerance = 1e-12 * max(abs(origin), abs(final), 1.0)
    if abs(delta) <= tolerance:
        return 0.0
    target = origin + (1.0 - math.exp(-1.0)) * delta
    increasing = delta > 0.0
    prior_time = times_s[event_index]
    prior_value = origin
    for index in range(event_index + 1, len(times_s)):
        current_time = times_s[index]
        current_value = response[index]
        crossed = current_value >= target if increasing else current_value <= target
        if crossed:
            segment = current_value - prior_value
            if abs(segment) <= tolerance:
                crossing_time = current_time
            else:
                fraction = (target - prior_value) / segment
                crossing_time = prior_time + fraction * (current_time - prior_time)
            return max(0.0, crossing_time - times_s[event_index])
        prior_time = current_time
        prior_value = current_value
    raise ValueError("response did not reach 63.2 percent of its final change")


def steady_output_metrics(result: RecycleSolveResult) -> Mapping[str, float]:
    """Return the fixed M6 steady output vector from a conserving M2 solve."""

    if not isinstance(result, RecycleSolveResult):
        raise TypeError("steady metrics require a RecycleSolveResult")
    flowsheet = result.require_converged()
    if not flowsheet.balance.passed(
        mass_atol_kg_s=1e-8,
        component_atol_kg_s=1e-8,
        salt_atol_kg_s=1e-10,
    ):
        raise ValueError("steady metrics require a conserving flowsheet")
    outputs: dict[str, float] = {}
    for product in HYDROCARBON_PRODUCT_NAMES:
        outputs[f"product_flow_kg_s.{product}"] = flowsheet.products[
            product
        ].mass_flow_kg_s
    for product in HYDROCARBON_PRODUCT_NAMES:
        outputs[f"product_yield_fraction.{product}"] = flowsheet.diagnostics[
            f"{product}_yield_mass_fraction"
        ]
    outputs.update(
        {
            "energy.furnace_fuel_duty_w": flowsheet.diagnostics[
                "furnace_fuel_duty_w"
            ],
            "energy.actual_recovered_duty_w": flowsheet.diagnostics[
                "actual_recovered_duty_w"
            ],
            "energy.potential_recovered_duty_w": flowsheet.diagnostics[
                "potential_recovered_duty_w"
            ],
            "energy.pump_around_removed_duty_w": flowsheet.diagnostics[
                "pump_around_removed_duty_w"
            ],
            "quality.gasoline.t90_k_proxy": flowsheet.qualities["gasoline"][
                "t90_k_proxy"
            ],
            "quality.light_diesel.t90_k_proxy": flowsheet.qualities[
                "light_diesel"
            ]["t90_k_proxy"],
            "quality.heavy_diesel.t90_k_proxy": flowsheet.qualities[
                "heavy_diesel"
            ]["t90_k_proxy"],
            "quality.residue.density_kg_m3_proxy": flowsheet.qualities["residue"][
                "density_kg_m3_proxy"
            ],
        }
    )
    if tuple(outputs) != STEADY_OUTPUT_IDS:
        raise AssertionError("steady metric order drifted")
    return _finite_mapping(outputs, context="steady metrics")


def dynamic_output_metrics(result: DynamicSimulationResult) -> Mapping[str, float]:
    """Summarize a successful M3 trace without serializing the full trajectory."""

    if not isinstance(result, DynamicSimulationResult):
        raise TypeError("dynamic metrics require a DynamicSimulationResult")
    result.require_success()
    if not result.samples:
        raise ValueError("dynamic metrics require samples")
    first = result.samples[0]
    last = result.samples[-1]
    first_state = first.state
    last_state = last.state
    times_s = tuple(sample.time_s for sample in result.samples)
    furnace_values = tuple(
        sample.state.thermal_states["furnace_outlet_temperature_k"]
        for sample in result.samples
    )
    pressure_values = tuple(
        cast(float, sample.evaluation["top_pressure_pa"])
        for sample in result.samples
    )
    tower_top_temperatures = tuple(
        sample.state.thermal_states["tower_top_temperature_k"]
        for sample in result.samples
    )
    kerosene_temperatures = tuple(
        sample.state.thermal_states["kerosene_temperature_k"]
        for sample in result.samples
    )
    outputs: dict[str, float] = {
        "final.furnace_outlet_temperature_k": last_state.thermal_states[
            "furnace_outlet_temperature_k"
        ],
        "minimum.furnace_outlet_temperature_k": min(furnace_values),
        "maximum.furnace_outlet_temperature_k": max(furnace_values),
        "final.tower_top_pressure_pa": pressure_values[-1],
        "minimum.tower_top_pressure_pa": min(pressure_values),
        "maximum.tower_top_pressure_pa": max(pressure_values),
        "final.tower_top_temperature_k": tower_top_temperatures[-1],
        "minimum.tower_top_temperature_k": min(tower_top_temperatures),
        "maximum.tower_top_temperature_k": max(tower_top_temperatures),
        "final.kerosene_temperature_k": kerosene_temperatures[-1],
        "minimum.kerosene_temperature_k": min(kerosene_temperatures),
        "maximum.kerosene_temperature_k": max(kerosene_temperatures),
    }
    feed_actuator_values = tuple(
        sample.state.actuator_states["fresh_feed_flow_kg_s"]
        for sample in result.samples
    )
    feed_command_values = tuple(
        sample.commands["fresh_feed_flow_kg_s"] for sample in result.samples
    )
    flash_inventory_values = tuple(
        sample.state.liquid_inventories["flash_drum"].total_mass_kg
        for sample in result.samples
    )
    flash_sensor_values = tuple(
        sample.state.sensor_states["flash_drum_inventory_kg"]
        for sample in result.samples
    )
    pressure_sensor_values = tuple(
        sample.state.sensor_states["tower_top_pressure_pa"]
        for sample in result.samples
    )
    outputs.update(
        {
            "tracking_iae.actuator.fresh_feed_flow_kg_s": (
                _absolute_error_integral(
                    times_s,
                    feed_actuator_values,
                    feed_command_values,
                )
            ),
            "tracking_iae.sensor.flash_drum_inventory_kg": (
                _absolute_error_integral(
                    times_s,
                    flash_sensor_values,
                    flash_inventory_values,
                )
            ),
            "tracking_iae.sensor.tower_top_pressure_pa": (
                _absolute_error_integral(
                    times_s,
                    pressure_sensor_values,
                    pressure_values,
                )
            ),
            "final_abs_tracking_error.actuator.fresh_feed_flow_kg_s": abs(
                feed_actuator_values[-1] - feed_command_values[-1]
            ),
            "final_abs_tracking_error.sensor.flash_drum_inventory_kg": abs(
                flash_sensor_values[-1] - flash_inventory_values[-1]
            ),
            "final_abs_tracking_error.sensor.tower_top_pressure_pa": abs(
                pressure_sensor_values[-1] - pressure_values[-1]
            ),
            "response_t63_s.actuator.fresh_feed_flow_kg_s": _response_t63_s(
                times_s,
                feed_actuator_values,
                feed_command_values,
            ),
            "response_t63_s.sensor.flash_drum_inventory_kg": _response_t63_s(
                times_s,
                flash_sensor_values,
                feed_command_values,
            ),
        }
    )
    for inventory_name in LIQUID_INVENTORY_NAMES:
        initial = first_state.liquid_inventories[inventory_name].total_mass_kg
        ratios = tuple(
            sample.state.liquid_inventories[inventory_name].total_mass_kg / initial
            for sample in result.samples
        )
        outputs[f"final_inventory_ratio.{inventory_name}"] = (
            last_state.liquid_inventories[inventory_name].total_mass_kg / initial
        )
        outputs[f"maximum_abs_inventory_deviation.{inventory_name}"] = max(
            abs(value - 1.0) for value in ratios
        )
    for product in HYDROCARBON_PRODUCT_NAMES:
        initial_evaluation = cast(
            Mapping[str, object], first.evaluation["stream_mass_flows_kg_s"]
        )
        final_evaluation = cast(
            Mapping[str, object], last.evaluation["stream_mass_flows_kg_s"]
        )
        stream_name = "residue_product" if product == "residue" else product
        initial_flow = cast(float, initial_evaluation[stream_name])
        final_flow = cast(float, final_evaluation[stream_name])
        outputs[f"final_product_flow_ratio.{product}"] = final_flow / initial_flow
    return _finite_mapping(outputs, context="dynamic metrics")


def closed_loop_output_metrics(
    result: ClosedLoopSimulationResult,
) -> Mapping[str, float]:
    """Summarize a successful M4 trace for M6 regression and uncertainty evidence."""

    if not isinstance(result, ClosedLoopSimulationResult):
        raise TypeError("closed-loop metrics require a ClosedLoopSimulationResult")
    if result.status != "success" or not result.samples:
        raise ValueError("closed-loop metrics require a successful result")
    outputs: dict[str, float] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        performance = result.loop_performance[loop_id]
        final_record = result.samples[-1].controls[loop_id]
        outputs[f"loop.{loop_id}.normalized_iae_s"] = performance.normalized_iae_s
        outputs[f"loop.{loop_id}.final_error_fraction"] = (
            performance.final_error_fraction
        )
        outputs[f"loop.{loop_id}.saturation_time_s"] = performance.saturation_time_s
        outputs[f"loop.{loop_id}.final_output_ratio"] = final_record.output_normalized
        outputs[f"loop.{loop_id}.settling_time_s"] = (
            0.0 if performance.settling_time_s is None else performance.settling_time_s
        )
    first_state = result.samples[0].plant.state
    for inventory_name in LIQUID_INVENTORY_NAMES:
        nominal = first_state.liquid_inventories[inventory_name].total_mass_kg
        ratios = tuple(
            sample.plant.state.liquid_inventories[inventory_name].total_mass_kg
            / nominal
            for sample in result.samples
        )
        outputs[f"inventory.{inventory_name}.minimum_ratio"] = min(ratios)
        outputs[f"inventory.{inventory_name}.maximum_ratio"] = max(ratios)
        outputs[f"inventory.{inventory_name}.final_ratio"] = ratios[-1]
    return _finite_mapping(outputs, context="closed-loop metrics")


def evaluate_metric_directions(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    expected_directions: Mapping[str, int],
    *,
    absolute_tolerance: float = 1e-12,
) -> Mapping[str, bool]:
    """Check configured -1/0/+1 metric directions against a baseline."""

    if not math.isfinite(absolute_tolerance) or absolute_tolerance < 0.0:
        raise ValueError("absolute_tolerance must be finite and non-negative")
    checks: dict[str, bool] = {}
    for metric_id in sorted(expected_directions):
        direction = expected_directions[metric_id]
        if direction not in {-1, 0, 1}:
            raise ValueError("expected metric directions must be -1, 0 or 1")
        if metric_id not in baseline or metric_id not in candidate:
            raise ValueError(f"direction metric {metric_id!r} is missing")
        delta = candidate[metric_id] - baseline[metric_id]
        if direction > 0:
            passed = delta > absolute_tolerance
        elif direction < 0:
            passed = delta < -absolute_tolerance
        else:
            passed = abs(delta) <= absolute_tolerance
        checks[metric_id] = passed
    return MappingProxyType(checks)
