"""Immutable result contracts for M4 closed-loop synthetic simulations."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

from ..dynamics.simulation import (
    DynamicConservationTolerances,
    DynamicCumulativeBalance,
    DynamicSample,
)
from ..dynamics.state import ACTUATOR_STATE_NAMES
from ..properties.components import ALL_COMPONENTS
from .config import CONTROL_PAIRING_WHITELIST, REQUIRED_CONTROL_LOOP_IDS

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset({"success", "failed"})
_REQUIRED_VERSION_NAMES = frozenset(
    {
        "software_version",
        "model_version",
        "parameter_set_version",
        "config_version",
        "case_version",
        "scenario_version",
        "control_version",
        "simulation_stage",
    }
)
_REQUIRED_METADATA_NAMES = frozenset(
    {"scenario_name", "scenario_version", "purpose", "synthetic", "data_origin"}
)
_REQUIRED_SUCCESS_CHECKS = frozenset(
    {
        "plant_execution",
        "plant_conservation",
        "automatic_initialization_no_bump",
        "baseline_hold",
        "loop_performance",
        "true_inventory_safety",
    }
)
_OWNED_ACTUATOR_NAMES = frozenset(
    pairing.manipulated_variable for pairing in CONTROL_PAIRING_WHITELIST.values()
)
_UNOWNED_ACTUATOR_NAMES = tuple(
    name for name in ACTUATOR_STATE_NAMES if name not in _OWNED_ACTUATOR_NAMES
)


def _finite(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def _signals_match(actual: float, expected: float) -> bool:
    return math.isclose(
        actual,
        expected,
        rel_tol=1e-12,
        abs_tol=1e-12 * max(abs(expected), 1.0),
    )


def _cumulative_relative_residual(
    residual: float,
    cumulative_in: float,
    *,
    flow_floor: float,
) -> float:
    """Return the M3 cumulative residual ratio for one conserved quantity."""

    return abs(residual) / max(cumulative_in, flow_floor)


def _sample_inventory_components(sample: DynamicSample) -> dict[str, float]:
    return {
        component: (
            sum(
                inventory.component_masses_kg[component]
                for inventory in sample.state.liquid_inventories.values()
            )
            + sample.state.top_gas_component_masses_kg[component]
        )
        for component in ALL_COMPONENTS
    }


def _sample_inventory_salt(sample: DynamicSample) -> float:
    return sum(
        inventory.salt_mass_kg
        for inventory in sample.state.liquid_inventories.values()
    )


def _require_balance_matches_samples(
    samples: tuple[ClosedLoopSample, ...],
    balance: DynamicCumulativeBalance,
) -> None:
    """Require the final ledger to be rebuilt from the carried endpoint evidence."""

    first = samples[0].plant
    final = samples[-1].plant
    expected = DynamicCumulativeBalance(
        initial_component_inventory_kg=_sample_inventory_components(first),
        final_component_inventory_kg=_sample_inventory_components(final),
        cumulative_component_in_kg=final.cumulative_component_in_kg,
        cumulative_component_out_kg=final.cumulative_component_out_kg,
        initial_inventory_salt_kg=_sample_inventory_salt(first),
        final_inventory_salt_kg=_sample_inventory_salt(final),
        cumulative_salt_in_kg=final.cumulative_salt_in_kg,
        cumulative_salt_out_kg=final.cumulative_salt_out_kg,
    )
    component_mapping_pairs = (
        (
            balance.initial_component_inventory_kg,
            expected.initial_component_inventory_kg,
        ),
        (
            balance.final_component_inventory_kg,
            expected.final_component_inventory_kg,
        ),
        (balance.cumulative_component_in_kg, expected.cumulative_component_in_kg),
        (balance.cumulative_component_out_kg, expected.cumulative_component_out_kg),
    )
    scalar_pairs = (
        (balance.initial_inventory_salt_kg, expected.initial_inventory_salt_kg),
        (balance.final_inventory_salt_kg, expected.final_inventory_salt_kg),
        (balance.cumulative_salt_in_kg, expected.cumulative_salt_in_kg),
        (balance.cumulative_salt_out_kg, expected.cumulative_salt_out_kg),
    )
    if any(
        not math.isclose(
            actual[component],
            expected_values[component],
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        for actual, expected_values in component_mapping_pairs
        for component in ALL_COMPONENTS
    ) or any(
        not math.isclose(actual, expected_value, rel_tol=1e-12, abs_tol=1e-12)
        for actual, expected_value in scalar_pairs
    ):
        raise ValueError(
            "closed-loop balance must agree with the initial and final plant samples"
        )


def _require_conservation(
    samples: tuple[ClosedLoopSample, ...],
    balance: DynamicCumulativeBalance,
    tolerances: DynamicConservationTolerances,
) -> None:
    """Recheck the M3 conservation evidence carried by an M4 result."""

    for sample in samples:
        plant = sample.plant
        if (
            abs(plant.instantaneous_mass_residual_kg_s)
            > tolerances.instantaneous_mass_atol_kg_s
            or plant.instantaneous_max_component_residual_kg_s
            > tolerances.instantaneous_component_atol_kg_s
            or abs(plant.instantaneous_salt_residual_kg_s)
            > tolerances.instantaneous_salt_atol_kg_s
        ):
            raise ValueError(
                "a successful closed-loop run must satisfy instantaneous "
                "conservation tolerances at every sample"
            )

        floor = tolerances.cumulative_flow_floor_kg
        maximum_component_relative = max(
            (
                _cumulative_relative_residual(
                    plant.component_balance_residuals_kg[component],
                    plant.cumulative_component_in_kg[component],
                    flow_floor=floor,
                )
                for component in ALL_COMPONENTS
            ),
            default=0.0,
        )
        mass_relative = _cumulative_relative_residual(
            plant.mass_balance_residual_kg,
            plant.cumulative_mass_in_kg,
            flow_floor=floor,
        )
        salt_relative = _cumulative_relative_residual(
            plant.salt_balance_residual_kg,
            plant.cumulative_salt_in_kg,
            flow_floor=floor,
        )
        if (
            maximum_component_relative > tolerances.cumulative_relative_atol
            or mass_relative > tolerances.cumulative_relative_atol
            or salt_relative > tolerances.cumulative_relative_atol
        ):
            raise ValueError(
                "a successful closed-loop run must satisfy cumulative "
                "conservation tolerances at every sample"
            )

    relative_atol = tolerances.cumulative_relative_atol
    floor = tolerances.cumulative_flow_floor_kg
    component_residuals = balance.component_residuals_kg
    if any(
        abs(component_residuals[component])
        > relative_atol
        * max(balance.cumulative_component_in_kg[component], floor)
        for component in ALL_COMPONENTS
    ):
        raise ValueError(
            "a successful closed-loop final balance must satisfy the absolute "
            "component tolerances implied by the cumulative conservation gate"
        )
    mass_atol_kg = relative_atol * max(balance.cumulative_mass_in_kg, floor)
    salt_atol_kg = relative_atol * max(balance.cumulative_salt_in_kg, floor)
    if (
        abs(balance.mass_residual_kg) > mass_atol_kg
        or abs(balance.salt_residual_kg) > salt_atol_kg
    ):
        raise ValueError(
            "a successful closed-loop final balance must satisfy the absolute "
            "mass and salt tolerances implied by the cumulative conservation gate"
        )


def _require_success_control_invariants(
    samples: tuple[ClosedLoopSample, ...],
) -> None:
    """Recompute control invariants that need only samples and the fixed pairing."""

    first = samples[0]
    process_value_scales: dict[str, float] = {}
    output_scales: dict[str, float] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        first_record = first.controls[loop_id]
        process_value_scale = first_record.process_value
        if process_value_scale <= 0.0:
            raise ValueError(
                "a successful closed-loop run requires positive first-sample "
                "process values as PV scales"
            )
        if not _signals_match(first_record.ramped_setpoint, process_value_scale):
            raise ValueError(
                "a successful closed-loop run requires first-sample ramped "
                "setpoints to equal the current process values"
            )
        process_value_scales[loop_id] = process_value_scale
        if not _signals_match(first_record.output_normalized, 1.0):
            raise ValueError(
                "a successful closed-loop run requires unit normalized outputs "
                "at the first sample"
            )
        manipulated_variable = CONTROL_PAIRING_WHITELIST[loop_id].manipulated_variable
        output_scale = first.plant.commands[manipulated_variable]
        if output_scale <= 0.0:
            raise ValueError(
                "a successful closed-loop run requires positive first-sample "
                "manipulated-variable commands as output scales"
            )
        output_scales[loop_id] = output_scale

    initial_commands = first.plant.commands
    for sample in samples:
        if any(
            not _signals_match(
                sample.plant.commands[actuator_name],
                initial_commands[actuator_name],
            )
            for actuator_name in _UNOWNED_ACTUATOR_NAMES
        ):
            raise ValueError(
                "a successful closed-loop run requires unowned actuator commands "
                "to remain at their first-sample values"
            )
        for loop_id in REQUIRED_CONTROL_LOOP_IDS:
            record = sample.controls[loop_id]
            process_value_scale = process_value_scales[loop_id]
            expected_error = (
                record.ramped_setpoint - record.process_value
            ) / process_value_scale
            if not _signals_match(record.error_normalized, expected_error):
                raise ValueError(
                    f"closed-loop current error for {loop_id!r} must agree with "
                    "its first-sample PV scale"
                )
            expected_decision_error = (
                record.ramped_setpoint - record.decision_process_value
            ) / process_value_scale
            if not _signals_match(
                record.decision_error_normalized,
                expected_decision_error,
            ):
                raise ValueError(
                    f"closed-loop decision error for {loop_id!r} must agree with "
                    "its first-sample PV scale"
                )
            expected_output_normalized = record.output / output_scales[loop_id]
            if not _signals_match(
                record.output_normalized,
                expected_output_normalized,
            ):
                raise ValueError(
                    f"closed-loop normalized output for {loop_id!r} must agree "
                    "with its first-sample command scale"
                )
            if loop_id != "furnace_temperature" and not _signals_match(
                record.feedforward_normalized,
                0.0,
            ):
                raise ValueError(
                    "only the furnace_temperature loop may carry normalized "
                    "feedforward in a successful closed-loop run"
                )


def _regular_times_through(
    completed_time_s: float,
    interval_s: float,
    *,
    time_tolerance_s: float,
) -> tuple[float, ...]:
    count = math.floor((completed_time_s + time_tolerance_s) / interval_s)
    return tuple(
        index * interval_s
        for index in range(count + 1)
        if index * interval_s <= completed_time_s + time_tolerance_s
    )


def _sample_indices_at_times(
    samples: tuple[ClosedLoopSample, ...],
    expected_times: tuple[float, ...],
    *,
    time_tolerance_s: float,
    context: str,
) -> tuple[int, ...]:
    """Linearly match required grid times while allowing extra event samples."""

    matched: list[int] = []
    sample_index = 0
    for expected_time in expected_times:
        while (
            sample_index < len(samples)
            and samples[sample_index].time_s < expected_time - time_tolerance_s
        ):
            sample_index += 1
        if (
            sample_index >= len(samples)
            or abs(samples[sample_index].time_s - expected_time) > time_tolerance_s
        ):
            raise ValueError(f"closed-loop samples are missing a required {context}")
        matched.append(sample_index)
        sample_index += 1
    return tuple(matched)


def _require_time_grid_and_control_ticks(
    samples: tuple[ClosedLoopSample, ...],
    *,
    duration_s: float,
    time_step_s: float,
    control_interval_s: float,
) -> None:
    """Require complete regular output/control grids without rejecting event points."""

    time_tolerance = 1e-12 * max(1.0, duration_s)
    duration_steps = round(duration_s / time_step_s)
    if duration_steps < 1 or not math.isclose(
        duration_steps * time_step_s,
        duration_s,
        rel_tol=0.0,
        abs_tol=time_tolerance,
    ):
        raise ValueError("closed-loop duration must be an integer multiple of time_step_s")
    if not samples:
        return
    completed_time = samples[-1].time_s
    output_times = _regular_times_through(
        completed_time,
        time_step_s,
        time_tolerance_s=time_tolerance,
    )
    _sample_indices_at_times(
        samples,
        output_times,
        time_tolerance_s=time_tolerance,
        context="regular output endpoint",
    )
    control_times = _regular_times_through(
        completed_time,
        control_interval_s,
        time_tolerance_s=time_tolerance,
    )
    control_indices = _sample_indices_at_times(
        samples,
        control_times,
        time_tolerance_s=time_tolerance,
        context="complete control tick",
    )
    for sample_index in control_indices:
        sample = samples[sample_index]
        for loop_id in REQUIRED_CONTROL_LOOP_IDS:
            record = sample.controls[loop_id]
            if not _signals_match(
                record.decision_process_value,
                record.process_value,
            ) or not _signals_match(
                record.decision_error_normalized,
                record.error_normalized,
            ):
                raise ValueError(
                    "closed-loop decision PV and error must equal the current values "
                    "at every complete control tick"
                )


def _require_success_performance_invariants(
    samples: tuple[ClosedLoopSample, ...],
    performance: Mapping[str, LoopPerformance],
) -> None:
    """Recompute performance fields that do not depend on acceptance configuration."""

    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        if samples[-1].controls[loop_id].saturated:
            raise ValueError(
                "a successful closed-loop run cannot end with a saturated loop"
            )
        normalized_iae = sum(
            0.5
            * (later.time_s - earlier.time_s)
            * (
                abs(earlier.controls[loop_id].error_normalized)
                + abs(later.controls[loop_id].error_normalized)
            )
            for earlier, later in pairwise(samples)
        )
        saturation_time = 0.0
        longest_saturation = 0.0
        current_saturation = 0.0
        for earlier, later in pairwise(samples):
            interval = later.time_s - earlier.time_s
            if earlier.controls[loop_id].saturated:
                saturation_time += interval
                current_saturation += interval
                longest_saturation = max(longest_saturation, current_saturation)
            else:
                current_saturation = 0.0
        metric = performance[loop_id]
        expected_values = (
            (metric.normalized_iae_s, normalized_iae, "normalized_iae_s"),
            (
                metric.final_error_fraction,
                abs(samples[-1].controls[loop_id].error_normalized),
                "final_error_fraction",
            ),
            (metric.saturation_time_s, saturation_time, "saturation_time_s"),
            (
                metric.longest_continuous_saturation_s,
                longest_saturation,
                "longest_continuous_saturation_s",
            ),
        )
        for actual, expected, field_name in expected_values:
            if not _signals_match(actual, expected):
                raise ValueError(
                    f"closed-loop performance {field_name} for {loop_id!r} "
                    "must agree with the sample series"
                )


@dataclass(frozen=True)
class ControlLoopRecord:
    """One loop decision attached to a plant endpoint sample."""

    target_setpoint: float
    ramped_setpoint: float
    process_value: float
    decision_process_value: float
    error_normalized: float
    decision_error_normalized: float
    proportional_term_normalized: float
    integral_term_normalized: float
    feedforward_normalized: float
    unconstrained_output_normalized: float
    magnitude_limited_output_normalized: float
    output_normalized: float
    output: float
    mode: str
    limited_by_magnitude: bool = False
    limited_by_rate: bool = False

    def __post_init__(self) -> None:
        for name in (
            "target_setpoint",
            "ramped_setpoint",
            "process_value",
            "decision_process_value",
            "error_normalized",
            "decision_error_normalized",
            "proportional_term_normalized",
            "integral_term_normalized",
            "feedforward_normalized",
            "unconstrained_output_normalized",
            "magnitude_limited_output_normalized",
            "output_normalized",
            "output",
        ):
            object.__setattr__(
                self,
                name,
                _finite(getattr(self, name), context=f"control record {name}"),
            )
        if self.mode not in {"automatic", "manual"}:
            raise ValueError("control record mode must be automatic or manual")
        if not isinstance(self.limited_by_magnitude, bool) or not isinstance(
            self.limited_by_rate, bool
        ):
            raise TypeError("control limiter flags must be booleans")
        expected_unconstrained = (
            1.0
            + self.feedforward_normalized
            + self.proportional_term_normalized
            + self.integral_term_normalized
        )
        if self.mode == "automatic" and not _signals_match(
            self.unconstrained_output_normalized,
            expected_unconstrained,
        ):
            raise ValueError(
                "control record unconstrained output must equal "
                "1 + feedforward + proportional + integral"
            )
        magnitude_changed = (
            self.unconstrained_output_normalized
            != self.magnitude_limited_output_normalized
        )
        if self.limited_by_magnitude != magnitude_changed:
            raise ValueError(
                "control record magnitude limiter flag must match its normalized "
                "output change"
            )
        rate_changed = (
            self.magnitude_limited_output_normalized != self.output_normalized
        )
        if self.limited_by_rate != rate_changed:
            raise ValueError(
                "control record rate limiter flag must match its normalized "
                "output change"
            )

    @property
    def saturated(self) -> bool:
        return self.limited_by_magnitude or self.limited_by_rate

    def as_dict(self) -> dict[str, object]:
        return {
            "target_setpoint": self.target_setpoint,
            "ramped_setpoint": self.ramped_setpoint,
            "process_value": self.process_value,
            "decision_process_value": self.decision_process_value,
            "error_normalized": self.error_normalized,
            "decision_error_normalized": self.decision_error_normalized,
            "proportional_term_normalized": self.proportional_term_normalized,
            "integral_term_normalized": self.integral_term_normalized,
            "feedforward_normalized": self.feedforward_normalized,
            "unconstrained_output_normalized": (
                self.unconstrained_output_normalized
            ),
            "magnitude_limited_output_normalized": (
                self.magnitude_limited_output_normalized
            ),
            "output_normalized": self.output_normalized,
            "output": self.output,
            "mode": self.mode,
            "limited_by_magnitude": self.limited_by_magnitude,
            "limited_by_rate": self.limited_by_rate,
            "saturated": self.saturated,
        }


@dataclass(frozen=True)
class ClosedLoopSample:
    """One M3 endpoint enriched by all seven M4 controller records."""

    plant: DynamicSample
    controls: Mapping[str, ControlLoopRecord]

    def __post_init__(self) -> None:
        if not isinstance(self.plant, DynamicSample):
            raise TypeError("closed-loop plant sample must be a DynamicSample")
        if set(self.plant.commands) != set(ACTUATOR_STATE_NAMES):
            raise ValueError(
                "closed-loop plant commands must contain exactly the 11 M3 "
                "actuator names"
            )
        if set(self.controls) != set(REQUIRED_CONTROL_LOOP_IDS):
            raise ValueError("closed-loop sample must contain exactly the seven M4 loops")
        copied: dict[str, ControlLoopRecord] = {}
        for loop_id, record in self.controls.items():
            if not isinstance(record, ControlLoopRecord):
                raise TypeError("closed-loop control records have the wrong type")
            pairing = CONTROL_PAIRING_WHITELIST[loop_id]
            expected_output = self.plant.commands[pairing.manipulated_variable]
            if not _signals_match(record.output, expected_output):
                raise ValueError(
                    f"closed-loop record output for {loop_id!r} must match its "
                    "plant command"
                )
            process_value_source = (
                self.plant.state.actuator_states
                if pairing.controlled_variable_source == "actuator"
                else self.plant.state.sensor_states
            )
            expected_process_value = process_value_source[
                pairing.controlled_variable_name
            ]
            if not _signals_match(record.process_value, expected_process_value):
                raise ValueError(
                    f"closed-loop record process_value for {loop_id!r} must match "
                    "the current plant PV"
                )
            copied[loop_id] = record
        object.__setattr__(self, "controls", MappingProxyType(copied))

    @property
    def time_s(self) -> float:
        return self.plant.time_s

    def as_dict(self) -> dict[str, object]:
        return {
            "time_s": self.time_s,
            "plant": self.plant.as_dict(),
            "controls": {
                loop_id: record.as_dict()
                for loop_id, record in self.controls.items()
            },
        }


@dataclass(frozen=True)
class LoopPerformance:
    """Traceable endpoint metrics and the acceptance outcome for one loop."""

    normalized_iae_s: float
    overshoot_fraction_of_pv_scale: float
    settling_time_s: float | None
    final_error_fraction: float
    tail_mean_absolute_error_fraction: float
    tail_slope_fraction_per_s: float
    tail_peak_to_peak_fraction: float
    saturation_time_s: float
    longest_continuous_saturation_s: float
    passed: bool
    failure_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        nonnegative_names = (
            "normalized_iae_s",
            "overshoot_fraction_of_pv_scale",
            "final_error_fraction",
            "tail_mean_absolute_error_fraction",
            "tail_slope_fraction_per_s",
            "tail_peak_to_peak_fraction",
            "saturation_time_s",
            "longest_continuous_saturation_s",
        )
        for name in nonnegative_names:
            value = _finite(getattr(self, name), context=name)
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        if self.settling_time_s is not None:
            settling = _finite(self.settling_time_s, context="settling_time_s")
            if settling < 0.0:
                raise ValueError("settling_time_s must be non-negative")
            object.__setattr__(self, "settling_time_s", settling)
        if not isinstance(self.passed, bool):
            raise TypeError("loop performance passed must be a boolean")
        reasons = tuple(self.failure_reasons)
        if any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ValueError("loop performance reasons must be non-empty strings")
        if self.passed and reasons:
            raise ValueError("passing loop performance cannot include failures")
        if self.passed and self.settling_time_s is None:
            raise ValueError("passing loop performance must include settling_time_s")
        if not self.passed and not reasons:
            raise ValueError("failed loop performance must include a failure reason")
        if self.longest_continuous_saturation_s > self.saturation_time_s:
            raise ValueError("longest saturation cannot exceed total saturation time")
        object.__setattr__(self, "failure_reasons", reasons)

    def as_dict(self) -> dict[str, object]:
        return {
            "normalized_iae_s": self.normalized_iae_s,
            "overshoot_fraction_of_pv_scale": (
                self.overshoot_fraction_of_pv_scale
            ),
            "settling_time_s": self.settling_time_s,
            "final_error_fraction": self.final_error_fraction,
            "tail_mean_absolute_error_fraction": (
                self.tail_mean_absolute_error_fraction
            ),
            "tail_slope_fraction_per_s": self.tail_slope_fraction_per_s,
            "tail_peak_to_peak_fraction": self.tail_peak_to_peak_fraction,
            "saturation_time_s": self.saturation_time_s,
            "longest_continuous_saturation_s": (
                self.longest_continuous_saturation_s
            ),
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons),
        }


@dataclass(frozen=True)
class ClosedLoopSimulationResult:
    """M4 feedback result with plant conservation and control acceptance gates."""

    status: str
    samples: tuple[ClosedLoopSample, ...]
    balance: DynamicCumulativeBalance
    conservation_tolerances: DynamicConservationTolerances
    loop_performance: Mapping[str, LoopPerformance]
    acceptance_checks: Mapping[str, bool]
    diagnostics: Mapping[str, float]
    versions: Mapping[str, str]
    metadata: Mapping[str, str]
    source_fingerprint: str
    control_fingerprint: str
    input_fingerprint: str
    requested_duration_s: float
    time_step_s: float
    control_interval_s: float
    failure_reason: str | None = None
    failure_stage: str | None = None
    failure_time_s: float | None = None

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported closed-loop status {self.status!r}")
        samples = tuple(self.samples)
        if any(not isinstance(sample, ClosedLoopSample) for sample in samples):
            raise TypeError("closed-loop samples have the wrong type")
        if samples and samples[0].time_s != 0.0:
            raise ValueError("the first closed-loop sample must be at t=0")
        if any(
            later.time_s <= earlier.time_s for earlier, later in pairwise(samples)
        ):
            raise ValueError("closed-loop sample times must increase strictly")
        duration = _finite(self.requested_duration_s, context="requested_duration_s")
        time_step = _finite(self.time_step_s, context="time_step_s")
        control_interval = _finite(
            self.control_interval_s, context="control_interval_s"
        )
        if duration <= 0.0 or time_step <= 0.0 or control_interval <= 0.0:
            raise ValueError("closed-loop time quantities must be positive")
        if samples and samples[-1].time_s > duration:
            raise ValueError("closed-loop sample exceeds requested duration")
        _require_time_grid_and_control_ticks(
            samples,
            duration_s=duration,
            time_step_s=time_step,
            control_interval_s=control_interval,
        )
        for name, digest in (
            ("source_fingerprint", self.source_fingerprint),
            ("control_fingerprint", self.control_fingerprint),
            ("input_fingerprint", self.input_fingerprint),
        ):
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.balance, DynamicCumulativeBalance):
            raise TypeError("closed-loop balance has the wrong type")
        if not isinstance(
            self.conservation_tolerances, DynamicConservationTolerances
        ):
            raise TypeError("closed-loop conservation tolerances have the wrong type")
        performance = dict(self.loop_performance)
        if any(not isinstance(key, str) for key in performance) or any(
            not isinstance(value, LoopPerformance) for value in performance.values()
        ):
            raise TypeError("loop performance values have the wrong type")
        if performance and set(performance) != set(REQUIRED_CONTROL_LOOP_IDS):
            raise ValueError("loop performance must cover exactly the seven M4 loops")
        checks = dict(self.acceptance_checks)
        if any(not isinstance(key, str) or not isinstance(value, bool) for key, value in checks.items()):
            raise TypeError("acceptance checks must map strings to booleans")
        if any(not isinstance(key, str) for key in self.diagnostics):
            raise TypeError("closed-loop diagnostic names must be strings")
        diagnostics = {
            key: _finite(value, context=f"diagnostic {key!r}")
            for key, value in self.diagnostics.items()
        }
        versions = dict(self.versions)
        metadata = dict(self.metadata)
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in versions.items()):
            raise TypeError("closed-loop versions must map strings")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()):
            raise TypeError("closed-loop metadata must map strings")
        if versions.get("simulation_stage") != "M4":
            raise ValueError("closed-loop simulation_stage must be M4")
        missing_versions = sorted(_REQUIRED_VERSION_NAMES - set(versions))
        if missing_versions:
            raise ValueError(
                "closed-loop result is missing required versions: "
                + ", ".join(missing_versions)
            )
        if any(not versions[name].strip() for name in _REQUIRED_VERSION_NAMES):
            raise ValueError("closed-loop required versions must be non-empty")
        missing_metadata = sorted(_REQUIRED_METADATA_NAMES - set(metadata))
        if missing_metadata:
            raise ValueError(
                "closed-loop result is missing required metadata: "
                + ", ".join(missing_metadata)
            )
        if any(not metadata[name].strip() for name in _REQUIRED_METADATA_NAMES):
            raise ValueError("closed-loop required metadata must be non-empty")
        if versions["scenario_version"] != metadata["scenario_version"]:
            raise ValueError("closed-loop scenario version must agree across traceability data")
        if metadata.get("synthetic") != "true":
            raise ValueError("closed-loop result must be marked synthetic")
        if metadata.get("data_origin") != "M4_closed_loop_simulation":
            raise ValueError("closed-loop result has an invalid data origin")
        if self.status == "success":
            if not samples or not math.isclose(
                samples[-1].time_s,
                duration,
                rel_tol=0.0,
                abs_tol=1e-12 * max(1.0, duration),
            ):
                raise ValueError("a successful closed-loop run must reach duration")
            if not checks or not all(checks.values()):
                raise ValueError("a successful closed-loop run must pass all gates")
            if set(checks) != set(_REQUIRED_SUCCESS_CHECKS):
                raise ValueError(
                    "a successful closed-loop run must contain the fixed M4 gates"
                )
            if set(performance) != set(REQUIRED_CONTROL_LOOP_IDS) or not all(
                item.passed for item in performance.values()
            ):
                raise ValueError(
                    "a successful closed-loop run requires seven passing loop metrics"
                )
            if any(
                record.mode != "automatic"
                for sample in samples
                for record in sample.controls.values()
            ):
                raise ValueError(
                    "a successful closed-loop run requires all seven loops to remain "
                    "automatic at every sample"
                )
            _require_success_control_invariants(samples)
            _require_success_performance_invariants(samples, performance)
            _require_balance_matches_samples(samples, self.balance)
            _require_conservation(
                samples,
                self.balance,
                self.conservation_tolerances,
            )
            if any(
                value is not None
                for value in (self.failure_reason, self.failure_stage, self.failure_time_s)
            ):
                raise ValueError("a successful closed-loop run cannot contain failure data")
        else:
            if (
                not isinstance(self.failure_reason, str)
                or not self.failure_reason.strip()
                or not isinstance(self.failure_stage, str)
                or not self.failure_stage.strip()
            ):
                raise ValueError("a failed closed-loop run must describe its failure")
            if self.failure_time_s is None:
                raise ValueError("a failed closed-loop run must record failure_time_s")
            failure_time = _finite(self.failure_time_s, context="failure_time_s")
            if failure_time < 0.0 or failure_time > duration:
                raise ValueError("failure_time_s must lie within the requested duration")
            completed_time = 0.0 if not samples else samples[-1].time_s
            time_tolerance = 1e-12 * max(1.0, duration, completed_time)
            if failure_time + time_tolerance < completed_time:
                raise ValueError(
                    "failure_time_s cannot precede the last valid closed-loop sample"
                )
            if not checks or "plant_execution" not in checks:
                raise ValueError("a failed closed-loop run must retain plant_execution")
            unknown_checks = sorted(set(checks) - set(_REQUIRED_SUCCESS_CHECKS))
            if unknown_checks:
                raise ValueError(
                    "a failed closed-loop run contains unknown M4 gates: "
                    + ", ".join(unknown_checks)
                )
            plant_completed = bool(samples) and math.isclose(
                samples[-1].time_s,
                duration,
                rel_tol=0.0,
                abs_tol=time_tolerance,
            )
            if checks["plant_execution"] != plant_completed:
                raise ValueError(
                    "failed closed-loop plant_execution must agree with whether "
                    "the plant reached duration"
                )
            if performance or "loop_performance" in checks:
                performance_passed = (
                    set(performance) == set(REQUIRED_CONTROL_LOOP_IDS)
                    and all(item.passed for item in performance.values())
                )
                if checks.get("loop_performance") != performance_passed:
                    raise ValueError(
                        "failed closed-loop loop_performance gate must agree with "
                        "exactly seven carried loop metrics"
                    )
            if all(checks.values()):
                raise ValueError("a failed closed-loop run must contain a failed gate")
            if samples:
                _require_balance_matches_samples(samples, self.balance)
            if checks.get("plant_conservation") is True:
                if not samples:
                    raise ValueError(
                        "a true plant_conservation gate requires retained samples"
                    )
                _require_conservation(
                    samples,
                    self.balance,
                    self.conservation_tolerances,
                )
            object.__setattr__(self, "failure_time_s", failure_time)
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "loop_performance", MappingProxyType(performance))
        object.__setattr__(self, "acceptance_checks", MappingProxyType(checks))
        object.__setattr__(self, "diagnostics", MappingProxyType(diagnostics))
        object.__setattr__(self, "versions", MappingProxyType(versions))
        object.__setattr__(self, "metadata", MappingProxyType(metadata))
        object.__setattr__(self, "requested_duration_s", duration)
        object.__setattr__(self, "time_step_s", time_step)
        object.__setattr__(self, "control_interval_s", control_interval)

    @property
    def completed_time_s(self) -> float:
        return 0.0 if not self.samples else self.samples[-1].time_s

    @property
    def acceptance_passed(self) -> bool:
        return self.status == "success" and set(self.acceptance_checks) == set(
            _REQUIRED_SUCCESS_CHECKS
        ) and all(self.acceptance_checks.values())

    def require_success(self) -> ClosedLoopSimulationResult:
        if self.status != "success":
            raise RuntimeError(self.failure_reason or "closed-loop simulation failed")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "samples": [sample.as_dict() for sample in self.samples],
            "balance": self.balance.as_dict(),
            "conservation_tolerances": self.conservation_tolerances.as_dict(),
            "loop_performance": {
                loop_id: value.as_dict()
                for loop_id, value in self.loop_performance.items()
            },
            "acceptance_checks": dict(self.acceptance_checks),
            "acceptance_passed": self.acceptance_passed,
            "diagnostics": dict(self.diagnostics),
            "versions": dict(self.versions),
            "metadata": dict(self.metadata),
            "source_fingerprint": self.source_fingerprint,
            "control_fingerprint": self.control_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "requested_duration_s": self.requested_duration_s,
            "time_step_s": self.time_step_s,
            "control_interval_s": self.control_interval_s,
            "completed_time_s": self.completed_time_s,
            "failure_reason": self.failure_reason,
            "failure_stage": self.failure_stage,
            "failure_time_s": self.failure_time_s,
        }
