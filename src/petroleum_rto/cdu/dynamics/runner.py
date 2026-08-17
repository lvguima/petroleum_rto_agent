"""High-level preparation and execution of one versioned M3 scenario."""

from __future__ import annotations

import math
from collections.abc import Mapping

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import (
    CaseConfig,
    ModelConfig,
    ScenarioConfig,
    validate_config_compatibility,
)
from ..core.types import MaterialStream
from ..flowsheet.recycle import RecycleSettings, solve_recycle
from ..properties.components import ComponentCatalog
from .initialization import initialize_open_loop_dynamic_model
from .schedule import CommandEvent, CommandSchedule
from .simulation import DynamicSimulationResult, simulate_dynamic
from .state import ACTUATOR_STATE_NAMES


def _finite_event_number(
    event: Mapping[str, object],
    name: str,
    *,
    required: bool,
) -> float | None:
    if name not in event:
        if required:
            raise ValueError(f"scenario event is missing {name!r}")
        return None
    raw = event[name]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"scenario event {name!r} must be numeric")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"scenario event {name!r} must be finite")
    return value


def schedule_from_scenario(
    baseline_commands: Mapping[str, float],
    scenario: ScenarioConfig,
) -> CommandSchedule:
    """Convert a validated scenario into the dynamic command contract."""

    if not isinstance(scenario, ScenarioConfig):
        raise TypeError("scenario must be a ScenarioConfig")
    events: list[CommandEvent] = []
    for index, raw_event in enumerate(scenario.events):
        target = raw_event.get("target")
        if not isinstance(target, str) or not target.strip():
            raise TypeError(f"scenario event {index} target must be a non-empty string")
        time_s = _finite_event_number(raw_event, "time_s", required=True)
        value = _finite_event_number(raw_event, "value", required=True)
        duration_s = _finite_event_number(raw_event, "duration_s", required=False)
        if time_s is None or value is None:  # pragma: no cover - required above
            raise AssertionError("required event values were not returned")
        events.append(
            CommandEvent(
                time_s=time_s,
                target=target,
                value=value,
                duration_s=duration_s,
            )
        )
    return CommandSchedule(baseline_commands, tuple(events))


def _preflight_scenario(scenario: ScenarioConfig) -> None:
    """Reject bypassed scenario contracts before performing the M2 solve."""

    if not isinstance(scenario, ScenarioConfig):
        raise TypeError("scenario must be a ScenarioConfig")
    duration_s = _finite_event_number(
        {"duration_s": scenario.duration_s},
        "duration_s",
        required=True,
    )
    time_step_s = _finite_event_number(
        {"time_step_s": scenario.time_step_s},
        "time_step_s",
        required=True,
    )
    if duration_s is None or time_step_s is None:  # pragma: no cover - required above
        raise AssertionError("required scenario durations were not returned")
    if duration_s <= 0.0 or time_step_s <= 0.0:
        raise ValueError("scenario duration and time step must be positive")
    if time_step_s > duration_s:
        raise ValueError("scenario time step cannot exceed duration")
    if not isinstance(scenario.name, str) or not scenario.name.strip():
        raise ValueError("scenario name must be a non-empty string")
    if (
        not isinstance(scenario.scenario_version, str)
        or not scenario.scenario_version.strip()
    ):
        raise ValueError("scenario version must be a non-empty string")

    metadata = scenario.metadata
    if not isinstance(metadata, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise TypeError("scenario metadata must map string keys to string values")
    if metadata.get("synthetic") != "true":
        raise ValueError("scenario metadata.synthetic must be 'true'")
    purpose = metadata.get("purpose")
    if purpose is None or not purpose.strip():
        raise ValueError("scenario metadata.purpose must be a non-empty string")

    allowed_targets = set(ACTUATOR_STATE_NAMES)
    required_event_fields = {"time_s", "target", "value"}
    allowed_event_fields = {*required_event_fields, "duration_s"}
    for index, event in enumerate(scenario.events):
        if not isinstance(event, Mapping) or any(
            not isinstance(key, str) for key in event
        ):
            raise TypeError(f"scenario event {index} must be an object with string keys")
        event_fields = set(event)
        if event_fields != required_event_fields and not (
            event_fields == allowed_event_fields
        ):
            raise ValueError(
                f"scenario event {index} fields differ; "
                f"missing={sorted(required_event_fields - event_fields)}, "
                f"unknown={sorted(event_fields - allowed_event_fields)}"
            )
        target = event["target"]
        if not isinstance(target, str) or not target.strip():
            raise TypeError(f"scenario event {index} target must be a non-empty string")
        if target not in allowed_targets:
            raise ValueError(f"scenario event {index} has unknown target {target!r}")
        event_time_s = _finite_event_number(event, "time_s", required=True)
        event_value = _finite_event_number(event, "value", required=True)
        event_duration_s = _finite_event_number(
            event,
            "duration_s",
            required=False,
        )
        if event_time_s is None or event_value is None:  # pragma: no cover
            raise AssertionError("required event values were not returned")
        if event_time_s < 0.0:
            raise ValueError(f"scenario event {index} time cannot be negative")
        if event_value < 0.0:
            raise ValueError(f"scenario event {index} value must be non-negative")
        if event_duration_s is not None and event_duration_s <= 0.0:
            raise ValueError(f"scenario event {index} duration must be positive")


def run_dynamic_scenario(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    scenario: ScenarioConfig,
    *,
    recycle_settings: RecycleSettings | None = None,
    initial_reflux: MaterialStream | None = None,
    software_version: str = SOFTWARE_VERSION,
) -> DynamicSimulationResult:
    """Solve the M2 prerequisite, initialize M3, and execute one scenario."""

    _preflight_scenario(scenario)
    versions = validate_config_compatibility(
        model,
        case,
        software_version=software_version,
        catalog=catalog,
        scenario=scenario,
    )
    recycle = solve_recycle(
        model,
        case,
        catalog,
        settings=recycle_settings,
        initial_reflux=initial_reflux,
        software_version=software_version,
    )
    if not recycle.converged:
        stage = recycle.failure_stage or "unknown"
        reason = recycle.failure_reason or "M2 prerequisite did not converge"
        raise RuntimeError(f"M3 prerequisite failed at {stage}: {reason}")
    dynamic_model = initialize_open_loop_dynamic_model(
        model,
        case,
        catalog,
        recycle,
    )
    schedule = schedule_from_scenario(dynamic_model.baseline_commands, scenario)
    version_mapping = {
        name: value
        for name, value in versions.as_dict().items()
        if value is not None
    }
    version_mapping["simulation_stage"] = "M3"
    return simulate_dynamic(
        dynamic_model,
        schedule,
        scenario.duration_s,
        scenario.time_step_s,
        fingerprint=dynamic_model.input_fingerprint,
        versions=version_mapping,
        metadata={
            **scenario.metadata,
            "scenario_name": scenario.name,
            "scenario_version": scenario.scenario_version,
        },
    )


def run_dynamic(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    scenario: ScenarioConfig,
    *,
    recycle_settings: RecycleSettings | None = None,
    initial_reflux: MaterialStream | None = None,
    software_version: str = SOFTWARE_VERSION,
) -> DynamicSimulationResult:
    """Short alias for :func:`run_dynamic_scenario`."""

    return run_dynamic_scenario(
        model,
        case,
        catalog,
        scenario,
        recycle_settings=recycle_settings,
        initial_reflux=initial_reflux,
        software_version=software_version,
    )
