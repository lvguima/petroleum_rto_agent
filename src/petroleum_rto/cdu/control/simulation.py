"""Feedback-aware M4 simulation with digital PI decisions and M3 RK4 physics."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

from ..core.config import canonical_fingerprint
from ..core.math_utils import rk4_step
from ..dynamics.equations import OpenLoopDynamicModel
from ..dynamics.simulation import (
    DynamicConservationError,
    DynamicConservationTolerances,
    _build_balance,
    _evaluation_and_rates,
    _inventory_components,
    _inventory_salt,
    _make_sample,
    _require_cumulative_conservation,
    _result_diagnostics,
)
from ..dynamics.state import DynamicState
from ..properties.components import ALL_COMPONENTS
from .config import REQUIRED_CONTROL_LOOP_IDS, ControlConfig
from .controllers import PIControllerState, PIControllerUpdate
from .loops import ControlLoopAssembly, assemble_control_loops
from .metrics import evaluate_closed_loop_acceptance
from .results import (
    ClosedLoopSample,
    ClosedLoopSimulationResult,
    ControlLoopRecord,
    LoopPerformance,
)
from .scenario import ClosedLoopScenarioConfig

_REQUIRED_SOURCE_VERSION_NAMES = frozenset(
    {
        "software_version",
        "model_version",
        "parameter_set_version",
        "config_version",
        "case_version",
    }
)
_TIME_RELATIVE_TOLERANCE = 1e-12


def _finite(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def _model_number(
    dynamic_model: OpenLoopDynamicModel,
    section: str,
    name: str,
) -> float:
    raw_section = dynamic_model.model.equipment[section]
    value = _finite(raw_section[name], context=f"model.{section}.{name}")
    return value


def _time_tolerance(duration_s: float) -> float:
    return _TIME_RELATIVE_TOLERANCE * max(duration_s, 1.0)


def _times_close(left: float, right: float, *, duration_s: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=0.0,
        abs_tol=_time_tolerance(duration_s),
    )


def _output_endpoints(duration_s: float, interval_s: float) -> tuple[float, ...]:
    """Return every requested output time, including the exact scenario end."""

    count = round(duration_s / interval_s)
    return tuple(
        duration_s if index == count else index * interval_s for index in range(1, count + 1)
    )


def _control_endpoints(duration_s: float, interval_s: float) -> tuple[float, ...]:
    """Return only complete digital-control ticks, never a partial final interval."""

    tolerance = _time_tolerance(duration_s)
    count = math.floor(duration_s / interval_s + _TIME_RELATIVE_TOLERANCE)
    endpoints: list[float] = []
    for index in range(1, count + 1):
        endpoint = index * interval_s
        if endpoint > duration_s + tolerance:
            continue
        endpoints.append(
            duration_s if _times_close(endpoint, duration_s, duration_s=duration_s) else endpoint
        )
    return tuple(endpoints)


def _merged_endpoints(
    duration_s: float,
    output_endpoints: Sequence[float],
    control_endpoints: Sequence[float],
    event_endpoints: Sequence[float],
) -> tuple[float, ...]:
    """Merge mathematically equal times while preferring exact output timestamps."""

    tagged = sorted(
        (
            *((time_s, 0) for time_s in output_endpoints),
            *((time_s, 1) for time_s in control_endpoints),
            *((time_s, 2) for time_s in event_endpoints),
        ),
        key=lambda item: item[0],
    )
    if not tagged:
        return ()
    groups: list[list[tuple[float, int]]] = [[tagged[0]]]
    for item in tagged[1:]:
        # Anchor each tolerance group to its first time.  Comparing with the
        # previous item would make grouping transitive and could collapse an
        # event that is not actually close to the selected endpoint.
        if _times_close(item[0], groups[-1][0][0], duration_s=duration_s):
            groups[-1].append(item)
        else:
            groups.append([item])
    return tuple(min(group, key=lambda item: (item[1], item[0]))[0] for group in groups)


def _is_control_tick(time_s: float, interval_s: float, *, duration_s: float) -> bool:
    if time_s <= 0.0:
        return False
    nearest_index = round(time_s / interval_s)
    return nearest_index >= 1 and _times_close(
        time_s,
        nearest_index * interval_s,
        duration_s=duration_s,
    )


def _integration_endpoints(
    scenario: ClosedLoopScenarioConfig,
    control_interval_s: float,
) -> tuple[float, ...]:
    return _merged_endpoints(
        scenario.duration_s,
        _output_endpoints(scenario.duration_s, scenario.time_step_s),
        _control_endpoints(scenario.duration_s, control_interval_s),
        tuple(
            event.time_s
            for event in scenario.events
            if not _times_close(
                event.time_s,
                0.0,
                duration_s=scenario.duration_s,
            )
            and event.time_s <= scenario.duration_s
        ),
    )


def _event_mapping(
    scenario: ClosedLoopScenarioConfig,
) -> Mapping[float, Mapping[str, float]]:
    grouped: dict[float, dict[str, float]] = {}
    previous_time: float | None = None
    for event in scenario.events:
        event_time = (
            0.0
            if _times_close(
                event.time_s,
                0.0,
                duration_s=scenario.duration_s,
            )
            else event.time_s
        )
        if previous_time is not None and _times_close(
            event_time,
            previous_time,
            duration_s=scenario.duration_s,
        ):
            event_time = previous_time
        else:
            previous_time = event_time
        by_loop = grouped.setdefault(event_time, {})
        if event.loop_id in by_loop:
            raise ValueError(f"scenario repeats loop {event.loop_id!r} at t={event_time:g} s")
        by_loop[event.loop_id] = event.setpoint_ratio
    return MappingProxyType(
        {time_s: MappingProxyType(dict(events)) for time_s, events in grouped.items()}
    )


def _event_updates_at(
    event_mapping: Mapping[float, Mapping[str, float]],
    time_s: float,
    *,
    duration_s: float,
) -> Mapping[str, float] | None:
    for event_time, updates in event_mapping.items():
        if _times_close(event_time, time_s, duration_s=duration_s):
            return updates
    return None


def _initial_records(
    assembly: ControlLoopAssembly,
    state: DynamicState,
    target_ratios: Mapping[str, float],
) -> Mapping[str, ControlLoopRecord]:
    process_values = assembly.process_values(state)
    setpoints = assembly.target_setpoints(target_ratios)
    feedforwards = assembly.feedforward_outputs(state)
    records: dict[str, ControlLoopRecord] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        assembled = assembly.loops[loop_id]
        controller_state = assembled.initial_state
        process_value = process_values[loop_id]
        target_setpoint = setpoints[loop_id]
        error = (controller_state.ramped_setpoint - process_value) / assembled.nominal_process_value
        proportional = assembled.controller.spec.signed_proportional_gain * error
        feedforward = feedforwards[loop_id] / assembled.nominal_output
        raw = (
            assembled.controller.bias_normalized
            + feedforward
            + proportional
            + controller_state.integral_term_normalized
        )
        records[loop_id] = ControlLoopRecord(
            target_setpoint=target_setpoint,
            ramped_setpoint=controller_state.ramped_setpoint,
            process_value=process_value,
            decision_process_value=process_value,
            error_normalized=error,
            decision_error_normalized=error,
            proportional_term_normalized=proportional,
            integral_term_normalized=controller_state.integral_term_normalized,
            feedforward_normalized=feedforward,
            unconstrained_output_normalized=raw,
            magnitude_limited_output_normalized=controller_state.output_normalized,
            output_normalized=controller_state.output_normalized,
            output=controller_state.output_normalized * assembled.nominal_output,
            mode=controller_state.mode,
        )
    return MappingProxyType(records)


def _records_from_updates(
    updates: Mapping[str, PIControllerUpdate],
) -> Mapping[str, ControlLoopRecord]:
    return MappingProxyType(
        {
            loop_id: ControlLoopRecord(
                target_setpoint=updates[loop_id].target_setpoint,
                ramped_setpoint=updates[loop_id].state.ramped_setpoint,
                process_value=updates[loop_id].process_value,
                decision_process_value=updates[loop_id].process_value,
                error_normalized=updates[loop_id].error_normalized,
                decision_error_normalized=updates[loop_id].error_normalized,
                proportional_term_normalized=(updates[loop_id].proportional_term_normalized),
                integral_term_normalized=(updates[loop_id].state.integral_term_normalized),
                feedforward_normalized=updates[loop_id].feedforward_normalized,
                unconstrained_output_normalized=(updates[loop_id].unconstrained_output_normalized),
                magnitude_limited_output_normalized=(
                    updates[loop_id].magnitude_limited_output_normalized
                ),
                output_normalized=updates[loop_id].output_normalized,
                output=updates[loop_id].output,
                mode=updates[loop_id].state.mode,
                limited_by_magnitude=updates[loop_id].limited_by_magnitude,
                limited_by_rate=updates[loop_id].limited_by_rate,
            )
            for loop_id in REQUIRED_CONTROL_LOOP_IDS
        }
    )


def _observe_held_records(
    assembly: ControlLoopAssembly,
    state: DynamicState,
    records: Mapping[str, ControlLoopRecord],
) -> Mapping[str, ControlLoopRecord]:
    """Refresh sampled PV/error while retaining the last actual PI decision."""

    process_values = assembly.process_values(state)
    return MappingProxyType(
        {
            loop_id: replace(
                records[loop_id],
                process_value=process_values[loop_id],
                error_normalized=(records[loop_id].ramped_setpoint - process_values[loop_id])
                / assembly.loops[loop_id].nominal_process_value,
            )
            for loop_id in REQUIRED_CONTROL_LOOP_IDS
        }
    )


def _update_controllers(
    assembly: ControlLoopAssembly,
    controller_states: Mapping[str, PIControllerState],
    state: DynamicState,
    target_ratios: Mapping[str, float],
    *,
    elapsed_s: float,
) -> tuple[
    Mapping[str, PIControllerState],
    Mapping[str, PIControllerUpdate],
    Mapping[str, float],
]:
    process_values = assembly.process_values(state)
    setpoints = assembly.target_setpoints(target_ratios)
    feedforwards = assembly.feedforward_outputs(state)
    updates: dict[str, PIControllerUpdate] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        loop = assembly.loops[loop_id]
        updates[loop_id] = loop.controller.update(
            controller_states[loop_id],
            process_value=process_values[loop_id],
            target_setpoint=setpoints[loop_id],
            dt_s=elapsed_s,
            feedforward_output=feedforwards[loop_id],
        )
    frozen_updates = MappingProxyType(updates)
    states = MappingProxyType(
        {loop_id: updates[loop_id].state for loop_id in REQUIRED_CONTROL_LOOP_IDS}
    )
    commands = assembly.commands_from_updates(frozen_updates)
    return states, frozen_updates, commands


def validate_closed_loop_scenario_compatibility(
    control_config: ControlConfig,
    scenario: ClosedLoopScenarioConfig,
) -> None:
    checks = {
        "schema_version": (scenario.schema_version, control_config.schema_version),
        "control_version": (
            scenario.control_version,
            control_config.control_version,
        ),
        "config_version": (scenario.config_version, control_config.config_version),
        "model_version": (scenario.model_version, control_config.model_version),
        "parameter_set_version": (
            scenario.parameter_set_version,
            control_config.parameter_set_version,
        ),
        "case_version": (
            scenario.case_version,
            control_config.tuning_basis_case_version,
        ),
    }
    mismatches = sorted(name for name, pair in checks.items() if pair[0] != pair[1])
    if mismatches:
        raise ValueError("closed-loop scenario version mismatch: " + ", ".join(mismatches))
    unknown_loops = sorted(
        {event.loop_id for event in scenario.events} - set(REQUIRED_CONTROL_LOOP_IDS)
    )
    if unknown_loops:
        raise ValueError("closed-loop scenario has unknown loops: " + ", ".join(unknown_loops))
    _event_mapping(scenario)


def simulate_closed_loop(
    dynamic_model: OpenLoopDynamicModel,
    control_config: ControlConfig,
    scenario: ClosedLoopScenarioConfig,
    *,
    versions: Mapping[str, str],
    conservation_tolerances: DynamicConservationTolerances | None = None,
    plant_initial_state: DynamicState | None = None,
) -> ClosedLoopSimulationResult:
    """Run one M4 digital-feedback scenario without mutating the M3 model."""

    if not isinstance(dynamic_model, OpenLoopDynamicModel):
        raise TypeError("dynamic_model must be an OpenLoopDynamicModel")
    if not isinstance(control_config, ControlConfig):
        raise TypeError("control_config must be a ControlConfig")
    if not isinstance(scenario, ClosedLoopScenarioConfig):
        raise TypeError("scenario must be a ClosedLoopScenarioConfig")
    validate_closed_loop_scenario_compatibility(control_config, scenario)
    copied_versions = dict(versions)
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in copied_versions.items()
    ):
        raise TypeError("closed-loop versions must map strings")
    missing_versions = sorted(_REQUIRED_SOURCE_VERSION_NAMES - set(copied_versions))
    if missing_versions:
        raise ValueError(
            "closed-loop simulation is missing required source versions: "
            + ", ".join(missing_versions)
        )
    if any(not copied_versions[name].strip() for name in _REQUIRED_SOURCE_VERSION_NAMES):
        raise ValueError("closed-loop required source versions must be non-empty")
    model_versions = dynamic_model.versions
    mismatched_source_versions = sorted(
        name
        for name in _REQUIRED_SOURCE_VERSION_NAMES
        if model_versions.get(name) != copied_versions[name]
    )
    if mismatched_source_versions:
        raise ValueError(
            "closed-loop source versions disagree with M3: " + ", ".join(mismatched_source_versions)
        )
    declared_control_versions = {
        "model_version": control_config.model_version,
        "parameter_set_version": control_config.parameter_set_version,
        "config_version": control_config.config_version,
        "case_version": control_config.tuning_basis_case_version,
    }
    mismatched_control_versions = sorted(
        name
        for name, declared in declared_control_versions.items()
        if model_versions.get(name) != declared
    )
    if mismatched_control_versions:
        raise ValueError(
            "closed-loop control versions disagree with M3: "
            + ", ".join(mismatched_control_versions)
        )
    reserved_versions = {
        "scenario_version": scenario.scenario_version,
        "control_version": control_config.control_version,
    }
    conflicting_reserved = sorted(
        name
        for name, expected in reserved_versions.items()
        if name in copied_versions and copied_versions[name] != expected
    )
    if conflicting_reserved:
        raise ValueError(
            "closed-loop reserved version mismatch: " + ", ".join(conflicting_reserved)
        )
    copied_versions.update({**reserved_versions, "simulation_stage": "M4"})
    metadata = {
        **scenario.metadata,
        "scenario_name": scenario.name,
        "scenario_version": scenario.scenario_version,
        "synthetic": "true",
        "data_origin": "M4_closed_loop_simulation",
    }
    selected_tolerances = (
        DynamicConservationTolerances.from_dynamic_model(dynamic_model)
        if conservation_tolerances is None
        else conservation_tolerances
    )
    if not isinstance(selected_tolerances, DynamicConservationTolerances):
        raise TypeError("conservation_tolerances has the wrong type")
    assembly = assemble_control_loops(
        control_config,
        dynamic_model.initial_state,
        dynamic_model.baseline_commands,
        furnace_efficiency=_model_number(dynamic_model, "furnace", "efficiency"),
        furnace_heat_loss_w=_model_number(dynamic_model, "furnace", "heat_loss_w"),
    )
    initial_state = (
        dynamic_model.initial_state if plant_initial_state is None else plant_initial_state
    )
    if not isinstance(initial_state, DynamicState):
        raise TypeError("plant_initial_state must be a DynamicState or None")
    if len(initial_state.to_vector()) != len(dynamic_model.initial_state.to_vector()):
        raise ValueError("plant_initial_state layout differs from the M3 model")
    event_mapping = _event_mapping(scenario)
    nominal_endpoints = _output_endpoints(
        scenario.duration_s,
        scenario.time_step_s,
    )
    endpoints = _integration_endpoints(scenario, control_config.control_interval_s)
    input_fingerprint = canonical_fingerprint(
        {
            "simulation_stage": "M4",
            "source_fingerprint": dynamic_model.input_fingerprint,
            "control_fingerprint": control_config.input_fingerprint,
            "versions": copied_versions,
            "initial_state": initial_state.as_dict(),
            "scenario": scenario.as_dict(),
            "control": control_config.as_dict(),
            "conservation_tolerances": selected_tolerances.as_dict(),
        }
    )
    initial_components = _inventory_components(initial_state)
    initial_salt = _inventory_salt(initial_state)
    state_length = len(initial_state.to_vector())
    cumulative_dimension = 2 * len(ALL_COMPONENTS) + 2
    augmented_state = initial_state.to_vector() + (0.0,) * cumulative_dimension
    target_ratios: dict[str, float] = {loop_id: 1.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS}
    controller_states: Mapping[str, PIControllerState] = assembly.initial_controller_states
    current_commands: Mapping[str, float] = assembly.baseline_commands
    initial_state_is_custom = initial_state != dynamic_model.initial_state
    current_records = (
        MappingProxyType({})
        if initial_state_is_custom
        else _initial_records(assembly, initial_state, target_ratios)
    )
    last_decision_time = 0.0
    decision_count = 0
    samples: list[ClosedLoopSample] = []
    active_time = 0.0

    def finish(
        status: str,
        *,
        loop_performance: Mapping[str, LoopPerformance] | None = None,
        acceptance_checks: Mapping[str, bool] | None = None,
        extra_diagnostics: Mapping[str, float] | None = None,
        failure_reason: str | None = None,
        failure_stage: str | None = None,
        failure_time_s: float | None = None,
    ) -> ClosedLoopSimulationResult:
        plant_samples = tuple(sample.plant for sample in samples)
        diagnostics = _result_diagnostics(
            plant_samples,
            nominal_endpoints=nominal_endpoints,
            integration_endpoints=endpoints,
            tolerances=selected_tolerances,
        )
        diagnostics["controller_decisions"] = float(decision_count)
        if extra_diagnostics is not None:
            diagnostics.update(extra_diagnostics)
        return ClosedLoopSimulationResult(
            status=status,
            samples=tuple(samples),
            balance=_build_balance(initial_state, plant_samples),
            conservation_tolerances=selected_tolerances,
            loop_performance={} if loop_performance is None else loop_performance,
            acceptance_checks=(
                {"plant_execution": False} if acceptance_checks is None else acceptance_checks
            ),
            diagnostics=diagnostics,
            versions=copied_versions,
            metadata=metadata,
            source_fingerprint=dynamic_model.input_fingerprint,
            control_fingerprint=control_config.input_fingerprint,
            input_fingerprint=input_fingerprint,
            requested_duration_s=scenario.duration_s,
            time_step_s=scenario.time_step_s,
            control_interval_s=control_config.control_interval_s,
            failure_reason=failure_reason,
            failure_stage=failure_stage,
            failure_time_s=failure_time_s,
        )

    initial_event_updates = _event_updates_at(
        event_mapping,
        0.0,
        duration_s=scenario.duration_s,
    )
    if initial_event_updates is not None or initial_state_is_custom:
        if initial_event_updates is not None:
            target_ratios.update(initial_event_updates)
        controller_states, initial_updates, current_commands = _update_controllers(
            assembly,
            controller_states,
            initial_state,
            target_ratios,
            elapsed_s=0.0,
        )
        current_records = _records_from_updates(initial_updates)
        decision_count += 1
    try:
        _, initial_evaluation, _, initial_residuals = _evaluation_and_rates(
            dynamic_model,
            initial_state,
            current_commands,
            capture_payload=True,
            tolerances=selected_tolerances,
        )
        initial_plant_sample = _make_sample(
            time_s=0.0,
            state=initial_state,
            commands=current_commands,
            evaluation=initial_evaluation,
            cumulative_rates=(0.0,) * cumulative_dimension,
            initial_component_inventory_kg=initial_components,
            initial_inventory_salt_kg=initial_salt,
            instantaneous_residuals=initial_residuals,
        )
        _require_cumulative_conservation(initial_plant_sample, selected_tolerances)
        samples.append(ClosedLoopSample(initial_plant_sample, current_records))
    except DynamicConservationError as exc:
        return finish(
            "failed",
            failure_reason=f"{type(exc).__name__}: {exc}",
            failure_stage="conservation",
            failure_time_s=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - result retains runtime failure
        return finish(
            "failed",
            failure_reason=f"{type(exc).__name__}: {exc}",
            failure_stage="initial_evaluation",
            failure_time_s=0.0,
        )

    start_time = 0.0
    for end_time in endpoints:
        interval_commands = current_commands
        interval = end_time - start_time

        def augmented_rhs(
            stage_time_s: float,
            values: Sequence[float],
            *,
            held_commands: Mapping[str, float] = interval_commands,
        ) -> tuple[float, ...]:
            nonlocal active_time
            active_time = stage_time_s
            stage_state = DynamicState.from_vector(values[:state_length])
            evaluation, _, boundary_rates, _ = _evaluation_and_rates(
                dynamic_model,
                stage_state,
                held_commands,
                capture_payload=False,
                tolerances=selected_tolerances,
            )
            if len(evaluation.derivative_vector) != state_length:
                raise ValueError("M3 derivative dimension changed during M4 simulation")
            return tuple(evaluation.derivative_vector) + boundary_rates

        try:
            augmented_state = rk4_step(
                augmented_rhs,
                start_time,
                augmented_state,
                interval,
            )
            active_time = end_time
            endpoint_state = DynamicState.from_vector(augmented_state[:state_length])
            event_updates = _event_updates_at(
                event_mapping,
                end_time,
                duration_s=scenario.duration_s,
            )
            decision_due = (
                _is_control_tick(
                    end_time,
                    control_config.control_interval_s,
                    duration_s=scenario.duration_s,
                )
                or event_updates is not None
            )
            if decision_due:
                if event_updates is not None:
                    target_ratios.update(event_updates)
                elapsed = end_time - last_decision_time
                controller_states, updates, current_commands = _update_controllers(
                    assembly,
                    controller_states,
                    endpoint_state,
                    target_ratios,
                    elapsed_s=elapsed,
                )
                current_records = _records_from_updates(updates)
                last_decision_time = end_time
                decision_count += 1
            else:
                current_records = _observe_held_records(
                    assembly,
                    endpoint_state,
                    current_records,
                )
            _, endpoint_evaluation, _, endpoint_residuals = _evaluation_and_rates(
                dynamic_model,
                endpoint_state,
                current_commands,
                capture_payload=True,
                tolerances=selected_tolerances,
            )
            plant_sample = _make_sample(
                time_s=end_time,
                state=endpoint_state,
                commands=current_commands,
                evaluation=endpoint_evaluation,
                cumulative_rates=augmented_state[state_length:],
                initial_component_inventory_kg=initial_components,
                initial_inventory_salt_kg=initial_salt,
                instantaneous_residuals=endpoint_residuals,
            )
            _require_cumulative_conservation(plant_sample, selected_tolerances)
            samples.append(ClosedLoopSample(plant_sample, current_records))
            start_time = end_time
        except DynamicConservationError as exc:
            return finish(
                "failed",
                failure_reason=f"{type(exc).__name__}: {exc}",
                failure_stage="conservation",
                failure_time_s=active_time,
            )
        except Exception as exc:  # noqa: BLE001 - retain last valid endpoint
            return finish(
                "failed",
                failure_reason=f"{type(exc).__name__}: {exc}",
                failure_stage="integration",
                failure_time_s=active_time,
            )

    disturbance_time = (
        None if not scenario.events else min(event.time_s for event in scenario.events)
    )
    try:
        performance, control_checks, metric_diagnostics = evaluate_closed_loop_acceptance(
            samples,
            control_config,
            disturbance_time_s=disturbance_time,
        )
    except Exception as exc:  # noqa: BLE001 - metric failure is explicit result data
        return finish(
            "failed",
            acceptance_checks={
                "plant_execution": True,
                "plant_conservation": True,
                "automatic_initialization_no_bump": False,
                "baseline_hold": False,
                "loop_performance": False,
                "true_inventory_safety": False,
            },
            failure_reason=f"{type(exc).__name__}: {exc}",
            failure_stage="performance_evaluation",
            failure_time_s=scenario.duration_s,
        )
    checks = {
        "plant_execution": True,
        "plant_conservation": True,
        **control_checks,
    }
    if all(checks.values()):
        return finish(
            "success",
            loop_performance=performance,
            acceptance_checks=checks,
            extra_diagnostics=metric_diagnostics,
        )
    failed_checks = ", ".join(name for name, passed in checks.items() if not passed)
    return finish(
        "failed",
        loop_performance=performance,
        acceptance_checks=checks,
        extra_diagnostics=metric_diagnostics,
        failure_reason="closed-loop acceptance failed: " + failed_checks,
        failure_stage="performance",
        failure_time_s=scenario.duration_s,
    )
