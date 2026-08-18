"""Closed-loop performance metrics and quantitative M4 acceptance gates."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from types import MappingProxyType
from typing import Protocol

from .config import REQUIRED_CONTROL_LOOP_IDS, ControlConfig
from .results import ClosedLoopSample, LoopPerformance

_INVENTORY_STATE_NAMES = {
    "flash_inventory": "flash_drum",
    "reflux_inventory": "reflux_drum",
    "bottom_inventory": "tower_bottom",
}


class _TimedSample(Protocol):
    @property
    def time_s(self) -> float: ...


def _linear_slope(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    mean_t = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_t) ** 2 for point in points)
    if denominator == 0.0:
        return 0.0
    return sum(
        (time_s - mean_t) * (value - mean_y) for time_s, value in points
    ) / denominator


def _window_points[TimedSampleT: _TimedSample](
    samples: Sequence[TimedSampleT],
    *,
    start_time_s: float,
    value: Callable[[TimedSampleT], float],
) -> tuple[tuple[float, float], ...]:
    """Clip a sampled series to a window, interpolating its exact left boundary."""

    selected = [
        (sample.time_s, value(sample))
        for sample in samples
        if sample.time_s >= start_time_s
    ]
    if not selected:
        raise ValueError("metric window does not contain any samples")
    if selected[0][0] == start_time_s:
        return tuple(selected)
    earlier = max(
        (sample for sample in samples if sample.time_s < start_time_s),
        key=lambda sample: sample.time_s,
        default=None,
    )
    later = next(sample for sample in samples if sample.time_s > start_time_s)
    if earlier is None:
        raise ValueError("metric window starts before the first sample")
    fraction = (start_time_s - earlier.time_s) / (later.time_s - earlier.time_s)
    interpolated = value(earlier) + fraction * (value(later) - value(earlier))
    return ((start_time_s, interpolated), *selected)


def _time_weighted_mean(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 2:
        raise ValueError("time-weighted mean requires a positive-duration window")
    duration = points[-1][0] - points[0][0]
    if duration <= 0.0:
        raise ValueError("time-weighted mean window must have positive duration")
    integral = sum(
        0.5 * (later[0] - earlier[0]) * (earlier[1] + later[1])
        for earlier, later in pairwise(points)
    )
    return integral / duration


def _trapezoid_absolute_error(
    samples: Sequence[ClosedLoopSample],
    loop_id: str,
) -> float:
    return sum(
        0.5
        * (later.time_s - earlier.time_s)
        * (
            abs(earlier.controls[loop_id].error_normalized)
            + abs(later.controls[loop_id].error_normalized)
        )
        for earlier, later in pairwise(samples)
    )


def _left_held_saturation_metrics(
    states: Sequence[tuple[float, bool]],
    *,
    start_time_s: float,
    duration_s: float,
) -> tuple[float, float, bool]:
    """Measure true-state intervals clipped to one explicit time window."""

    if not states:
        raise ValueError("saturation metrics require at least one state")
    if duration_s < start_time_s:
        raise ValueError("saturation window duration precedes its start")
    if any(later[0] <= earlier[0] for earlier, later in pairwise(states)):
        raise ValueError("saturation state times must increase strictly")
    total = 0.0
    longest = 0.0
    current = 0.0
    for index, (state_time_s, saturated) in enumerate(states):
        next_time_s = (
            states[index + 1][0] if index + 1 < len(states) else duration_s
        )
        overlap_start = max(state_time_s, start_time_s)
        overlap_end = min(next_time_s, duration_s)
        if overlap_end <= overlap_start:
            continue
        interval = overlap_end - overlap_start
        if saturated:
            total += interval
            current += interval
            longest = max(longest, current)
        else:
            current = 0.0
    terminal_saturated = (
        states[-1][1]
        and start_time_s <= states[-1][0] <= duration_s
        and states[-1][0] == duration_s
    )
    return total, longest, total > 0.0 or terminal_saturated


def _saturation_metrics(
    samples: Sequence[ClosedLoopSample],
    loop_id: str,
    *,
    start_time_s: float,
    duration_s: float,
) -> tuple[float, float, bool]:
    states = tuple(
        (sample.time_s, sample.controls[loop_id].saturated) for sample in samples
    )
    return _left_held_saturation_metrics(
        states,
        start_time_s=start_time_s,
        duration_s=duration_s,
    )


def _ramp_completion_time(
    samples: Sequence[ClosedLoopSample],
    loop_id: str,
    disturbance_time_s: float,
) -> float | None:
    final_target = samples[-1].controls[loop_id].target_setpoint
    scale = samples[0].controls[loop_id].target_setpoint
    tolerance = 1e-12 * max(abs(scale), 1.0)
    for sample in samples:
        if sample.time_s < disturbance_time_s:
            continue
        record = sample.controls[loop_id]
        if (
            abs(record.target_setpoint - final_target) <= tolerance
            and abs(record.ramped_setpoint - final_target) <= tolerance
        ):
            return sample.time_s
    return None


def _settling_response_time(
    samples: Sequence[ClosedLoopSample],
    loop_id: str,
    *,
    start_time_s: float,
    band_fraction: float,
    dwell_s: float,
) -> float | None:
    candidates = [sample for sample in samples if sample.time_s >= start_time_s]
    if not candidates:
        return None
    last_outside_index = -1
    for index, sample in enumerate(candidates):
        if abs(sample.controls[loop_id].error_normalized) > band_fraction:
            last_outside_index = index
    settled_index = last_outside_index + 1
    if settled_index >= len(candidates):
        return None
    settled_at = candidates[settled_index].time_s
    if candidates[-1].time_s - settled_at < dwell_s:
        return None
    return settled_at - start_time_s


def _overshoot_fraction(
    samples: Sequence[ClosedLoopSample],
    loop_id: str,
    *,
    start_time_s: float,
) -> float:
    nominal = samples[0].controls[loop_id].target_setpoint
    target = samples[-1].controls[loop_id].target_setpoint
    selected = [sample for sample in samples if sample.time_s >= start_time_s]
    if not selected:
        return 0.0
    process_values = [sample.controls[loop_id].process_value for sample in selected]
    change = target - nominal
    if change > 0.0:
        return max(0.0, max(process_values) - target) / nominal
    if change < 0.0:
        return max(0.0, target - min(process_values)) / nominal
    return max(abs(value - target) for value in process_values) / nominal


def evaluate_closed_loop_acceptance(
    samples: Sequence[ClosedLoopSample],
    control_config: ControlConfig,
    *,
    disturbance_time_s: float | None,
) -> tuple[
    Mapping[str, LoopPerformance],
    Mapping[str, bool],
    Mapping[str, float],
]:
    """Compute deterministic metrics and all non-plant M4 success gates."""

    frozen_samples = tuple(samples)
    if not frozen_samples:
        raise ValueError("closed-loop acceptance requires at least one sample")
    expected_loops = set(REQUIRED_CONTROL_LOOP_IDS)
    if any(set(sample.controls) != expected_loops for sample in frozen_samples):
        raise ValueError("every closed-loop sample must contain exactly seven loops")
    acceptance = control_config.acceptance
    duration = frozen_samples[-1].time_s
    baseline_end = duration if disturbance_time_s is None else disturbance_time_s
    tolerance = 1e-12 * max(duration, 1.0)
    if duration + tolerance < acceptance.tail_window_s:
        raise ValueError(
            "closed-loop samples do not cover the complete acceptance tail window"
        )
    if baseline_end + tolerance < acceptance.baseline_tail_window_s:
        raise ValueError(
            "closed-loop samples do not cover the complete baseline tail window"
        )
    baseline_start = max(0.0, baseline_end - acceptance.baseline_tail_window_s)
    baseline_samples = tuple(
        sample
        for sample in frozen_samples
        if baseline_start <= sample.time_s <= baseline_end
        and (disturbance_time_s is None or sample.time_s < disturbance_time_s)
    )
    if not baseline_samples:
        raise ValueError("closed-loop run has no baseline acceptance samples")

    initial = frozen_samples[0]
    initial_deltas: dict[str, float] = {}
    baseline_errors: dict[str, float] = {}
    baseline_slopes: dict[str, float] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        manipulated = control_config.loops[loop_id].manipulated_variable
        baseline_command = initial.plant.commands[manipulated]
        initial_deltas[loop_id] = abs(
            initial.controls[loop_id].output - baseline_command
        )
        scale = initial.controls[loop_id].target_setpoint
        baseline_errors[loop_id] = max(
            abs(sample.controls[loop_id].error_normalized)
            for sample in baseline_samples
        )
        baseline_slopes[loop_id] = abs(
            _linear_slope(
                tuple(
                    (
                        sample.time_s,
                        sample.controls[loop_id].process_value / scale,
                    )
                    for sample in baseline_samples
                )
            )
        )

    tail_start = max(0.0, duration - acceptance.tail_window_s)
    performance: dict[str, LoopPerformance] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        scale = initial.controls[loop_id].target_setpoint
        target_changed = not math.isclose(
            frozen_samples[-1].controls[loop_id].target_setpoint,
            initial.controls[loop_id].target_setpoint,
            rel_tol=0.0,
            abs_tol=1e-12 * max(abs(scale), 1.0),
        )
        response_origin = 0.0 if disturbance_time_s is None else disturbance_time_s
        ramp_complete = (
            _ramp_completion_time(
                frozen_samples,
                loop_id,
                response_origin,
            )
            if target_changed
            else response_origin
        )
        settling_time = (
            None
            if ramp_complete is None
            else _settling_response_time(
                frozen_samples,
                loop_id,
                start_time_s=ramp_complete,
                band_fraction=acceptance.band_fraction(loop_id),
                dwell_s=acceptance.settling_dwell_s,
            )
        )
        def tail_error_value(
            sample: ClosedLoopSample,
            selected_loop: str = loop_id,
        ) -> float:
            return abs(sample.controls[selected_loop].error_normalized)

        def tail_process_value(
            sample: ClosedLoopSample,
            selected_loop: str = loop_id,
            pv_scale: float = scale,
        ) -> float:
            return sample.controls[selected_loop].process_value / pv_scale

        tail_error_points = _window_points(
            frozen_samples,
            start_time_s=tail_start,
            value=tail_error_value,
        )
        tail_value_points = _window_points(
            frozen_samples,
            start_time_s=tail_start,
            value=tail_process_value,
        )
        tail_mean = _time_weighted_mean(tail_error_points)
        tail_slope = abs(
            _linear_slope(tail_value_points)
        )
        tail_values = [point[1] for point in tail_value_points]
        tail_peak_to_peak = max(tail_values) - min(tail_values)
        saturation_time, longest_saturation, _ = _saturation_metrics(
            frozen_samples,
            loop_id,
            start_time_s=0.0,
            duration_s=duration,
        )
        _, _, tail_saturated = _saturation_metrics(
            frozen_samples,
            loop_id,
            start_time_s=tail_start,
            duration_s=duration,
        )
        failures: list[str] = []
        if settling_time is None:
            failures.append("settling dwell was not achieved")
        elif settling_time > acceptance.recovery_time_s[loop_id]:
            failures.append("recovery time exceeded")
        if tail_mean > acceptance.tail_mean_abs_error_fraction:
            failures.append("tail mean absolute error exceeded")
        if tail_slope > acceptance.tail_slope_fraction_per_s:
            failures.append("tail slope exceeded")
        if tail_peak_to_peak > acceptance.tail_peak_to_peak_fraction:
            failures.append("tail peak-to-peak variation exceeded")
        if longest_saturation > acceptance.max_continuous_saturation_s:
            failures.append("continuous saturation exceeded")
        if tail_saturated:
            failures.append("tail window contains saturation")
        performance[loop_id] = LoopPerformance(
            normalized_iae_s=_trapezoid_absolute_error(
                frozen_samples,
                loop_id,
            ),
            overshoot_fraction_of_pv_scale=_overshoot_fraction(
                frozen_samples,
                loop_id,
                start_time_s=response_origin,
            ),
            settling_time_s=settling_time,
            final_error_fraction=abs(
                frozen_samples[-1].controls[loop_id].error_normalized
            ),
            tail_mean_absolute_error_fraction=tail_mean,
            tail_slope_fraction_per_s=tail_slope,
            tail_peak_to_peak_fraction=tail_peak_to_peak,
            saturation_time_s=saturation_time,
            longest_continuous_saturation_s=longest_saturation,
            passed=not failures,
            failure_reasons=tuple(failures),
        )

    nominal_inventories = {
        loop_id: initial.plant.state.liquid_inventories[state_name].total_mass_kg
        for loop_id, state_name in _INVENTORY_STATE_NAMES.items()
    }
    minimum_inventory_ratio = math.inf
    maximum_inventory_ratio = -math.inf
    maximum_inventory_violation = 0.0
    for sample in frozen_samples:
        for loop_id, state_name in _INVENTORY_STATE_NAMES.items():
            ratio = (
                sample.plant.state.liquid_inventories[state_name].total_mass_kg
                / nominal_inventories[loop_id]
            )
            minimum_inventory_ratio = min(minimum_inventory_ratio, ratio)
            maximum_inventory_ratio = max(maximum_inventory_ratio, ratio)
            maximum_inventory_violation = max(
                maximum_inventory_violation,
                acceptance.inventory_true_min_ratio - ratio,
                ratio - acceptance.inventory_true_max_ratio,
            )
    all_automatic = all(
        sample.controls[loop_id].mode == "automatic"
        for sample in frozen_samples
        for loop_id in REQUIRED_CONTROL_LOOP_IDS
    )
    no_bump = all_automatic and all(
        initial_deltas[loop_id]
        <= 1e-12
        * max(
            abs(
                initial.plant.commands[
                    control_config.loops[loop_id].manipulated_variable
                ]
            ),
            1.0,
        )
        for loop_id in REQUIRED_CONTROL_LOOP_IDS
    )
    baseline_passed = all(
        baseline_errors[loop_id] <= acceptance.baseline_error_fraction
        and baseline_slopes[loop_id] <= acceptance.baseline_slope_fraction_per_s
        for loop_id in REQUIRED_CONTROL_LOOP_IDS
    )
    inventory_safe = maximum_inventory_violation <= 0.0
    loop_performance_passed = all(item.passed for item in performance.values())
    checks = MappingProxyType(
        {
            "automatic_initialization_no_bump": no_bump,
            "baseline_hold": baseline_passed,
            "loop_performance": loop_performance_passed,
            "true_inventory_safety": inventory_safe,
        }
    )
    diagnostics = MappingProxyType(
        {
            "maximum_initial_command_delta": max(initial_deltas.values()),
            "maximum_baseline_error_fraction": max(baseline_errors.values()),
            "maximum_baseline_slope_fraction_per_s": max(
                baseline_slopes.values()
            ),
            "minimum_true_inventory_ratio": minimum_inventory_ratio,
            "maximum_true_inventory_ratio": maximum_inventory_ratio,
            "maximum_true_inventory_band_violation": max(
                0.0, maximum_inventory_violation
            ),
        }
    )
    return MappingProxyType(performance), checks, diagnostics
