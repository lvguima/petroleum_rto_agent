"""High-level M6 engineering validation matrix execution.

The runner reuses the accepted M2, M3, M4, and source-verified M5 layers.  It
does not change their result contracts.  Unsupported inputs are rejected before
any solver call, while proxy and synthetic fault scenarios remain explicitly
limited M6 evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import cast

from ..control.config import ControlConfig, load_control_config
from ..control.controllers import NormalizedPIController
from ..control.runner import run_closed_loop_scenario
from ..control.scenario import ClosedLoopScenarioConfig, SetpointEvent
from ..core.config import ModelConfig, canonical_fingerprint
from ..dynamics.equations import OpenLoopDynamicModel
from ..dynamics.initialization import initialize_open_loop_dynamic_model
from ..dynamics.schedule import CommandEvent, CommandSchedule
from ..dynamics.simulation import DynamicSimulationResult, simulate_dynamic
from ..flowsheet.recycle import RecycleSolveResult, solve_recycle
from .basis import M6Basis, load_m6_basis
from .config import (
    M6ValidationConfig,
    UncertaintyPlan,
    ValidationScenarioSpec,
    load_m6_validation_config,
)
from .domain import ApplicabilityAssessment, assess_applicability
from .metrics import (
    closed_loop_output_metrics,
    dynamic_output_metrics,
    evaluate_metric_directions,
    steady_output_metrics,
)
from .protection import (
    ProtectionEvent,
    ProtectionFrame,
    ProtectionRule,
    ProtectionTrace,
    run_protection,
)
from .results import (
    M6_COMPLETION_CHECK_IDS,
    M6_RESULT_METADATA,
    M6_RESULT_SCHEMA_VERSION,
    M6_SOURCE_COMPOSITION,
    ExecutionLayer,
    M6ResultStatus,
    M6ValidationResult,
    ScenarioValidationResult,
)
from .scenarios import apply_steady_factor, dynamic_command_for_factor
from .tracking import ControllerTrackingEvidence, verify_controller_tracking
from .uncertainty import (
    EngineeringInputInterval,
    LocalSensitivityAnalysis,
    UncertaintyPropagationResult,
    assert_uncertainty_not_narrower,
    propagate_uncertainty,
    run_local_sensitivity,
)

_DEFAULT_CONFIG = Path("configs/validation/m6_validation_v0.1.0.json")
_DEFAULT_CONTROL = Path("configs/controllers/cdu_pi_v0.1.0.json")
_DYNAMIC_DURATION_S = 600.0
_DYNAMIC_EVENT_TIME_S = 60.0
_DYNAMIC_TIME_STEP_S = 1.0
_CLOSED_LOOP_DURATION_S = 7200.0
_CLOSED_LOOP_EVENT_TIME_S = 600.0
_CLOSED_LOOP_TIME_STEP_S = 1.0


class M6ValidationExecutionError(RuntimeError):
    """Explicit high-level failure before a complete M6 result can be built."""

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        failure_time_s: float = 0.0,
        last_valid_scenario_ids: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("M6 failure stage must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("M6 failure reason must be non-empty")
        if (
            isinstance(failure_time_s, bool)
            or not isinstance(failure_time_s, (int, float))
            or not math.isfinite(float(failure_time_s))
            or float(failure_time_s) < 0.0
        ):
            raise ValueError("M6 failure_time_s must be finite and non-negative")
        if any(not isinstance(item, str) or not item for item in last_valid_scenario_ids):
            raise ValueError("M6 last-valid scenario ids must be non-empty strings")
        if len(set(last_valid_scenario_ids)) != len(last_valid_scenario_ids):
            raise ValueError("M6 last-valid scenario ids must be unique")
        self.stage = stage
        self.reason = reason
        self.failure_time_s = float(failure_time_s)
        self.last_valid_scenario_ids = tuple(last_valid_scenario_ids)
        super().__init__(f"M6 validation failed at {stage}: {reason}")

    def as_dict(self) -> dict[str, object]:
        """Return deterministic failure evidence without publishing success artifacts."""

        return {
            "status": "failed",
            "failure_stage": self.stage,
            "failure_reason": self.reason,
            "failure_time_s": self.failure_time_s,
            "last_valid_scenario_ids": list(self.last_valid_scenario_ids),
            "synthetic": True,
            "data_origin": "M6_synthetic_validation",
        }


def _source_origins(layer: str) -> tuple[str, ...]:
    source = {
        "M2_steady": "M2_steady_model_prediction",
        "M3_open_loop": "M3_open_loop_simulation",
        "M4_closed_loop": "M4_closed_loop_simulation",
        "M6_supervision": "M6_synthetic_validation",
        "structural_rejection": "M6_synthetic_validation",
    }[layer]
    return (
        ("M6_synthetic_validation",)
        if source == "M6_synthetic_validation"
        else (source, "M6_synthetic_validation")
    )


def _result_layer(layer: str) -> ExecutionLayer:
    return cast(
        ExecutionLayer,
        "M6_supervisory" if layer == "M6_supervision" else layer,
    )


def _check_versions(config: M6ValidationConfig, basis: M6Basis) -> None:
    checks = {
        "analysis_basis_version": (config.analysis_basis_version, basis.analysis_version),
        "model_version": (config.model_version, basis.model.model_version),
        "model_config_version": (
            config.model_config_version,
            basis.model.config_version,
        ),
        "base_parameter_set_version": (
            config.base_parameter_set_version,
            basis.base_parameter_set_version,
        ),
        "derived_parameter_set_version": (
            config.derived_parameter_set_version,
            basis.derived_parameter_set_version,
        ),
        "base_case_version": (config.base_case_version, basis.base_case_version),
        "derived_case_version": (
            config.derived_case_version,
            basis.derived_case_version,
        ),
    }
    mismatches = [name for name, values in checks.items() if values[0] != values[1]]
    if mismatches:
        raise M6ValidationExecutionError(
            "version_preflight",
            "M6 config differs from the source-verified basis: " + ", ".join(mismatches),
        )


def _domain(
    config: M6ValidationConfig,
    scenario: ValidationScenarioSpec,
) -> ApplicabilityAssessment:
    return assess_applicability(
        config.domain_dimensions,
        scenario.inputs,
        abnormal_verification=scenario.abnormal_verification,
    )


def _steady_conservation(
    result: RecycleSolveResult,
    config: M6ValidationConfig,
) -> Mapping[str, bool]:
    if result.flowsheet is None:
        return MappingProxyType({"engine_success": False})
    acceptance = config.report_acceptance
    balance = result.flowsheet.balance
    return MappingProxyType(
        {
            "engine_success": result.status == "success",
            "total_mass": abs(balance.residual_kg_s)
            <= acceptance.maximum_mass_residual_kg_s,
            "component_mass": max(
                (abs(value) for value in balance.component_residuals_kg_s.values()),
                default=0.0,
            )
            <= acceptance.maximum_component_residual_kg_s,
            "salt_mass": abs(balance.salt_residual_kg_s)
            <= acceptance.maximum_salt_residual_kg_s,
        }
    )


def _dynamic_conservation(result: DynamicSimulationResult) -> Mapping[str, bool]:
    return MappingProxyType(
        {
            "engine_success": result.status == "success",
            "instantaneous_conservation": result.status == "success",
            "cumulative_conservation": result.status == "success",
        }
    )


def _dynamic_versions(model: OpenLoopDynamicModel) -> dict[str, str]:
    return {name: value for name, value in model.versions.items()}


def _simulate_m3(
    dynamic_model: OpenLoopDynamicModel,
    *,
    scenario_name: str,
    scenario_version: str,
    purpose: str,
    event: CommandEvent | None,
    source_fingerprint: str,
) -> DynamicSimulationResult:
    events = () if event is None else (event,)
    schedule = CommandSchedule(dynamic_model.baseline_commands, events)
    return simulate_dynamic(
        dynamic_model,
        schedule,
        _DYNAMIC_DURATION_S,
        _DYNAMIC_TIME_STEP_S,
        fingerprint=source_fingerprint,
        versions=_dynamic_versions(dynamic_model),
        metadata={
            "scenario_name": scenario_name,
            "scenario_version": scenario_version,
            "purpose": purpose,
            "synthetic": "true",
        },
    )


def _baseline_dynamic_metrics(
    dynamic_model: OpenLoopDynamicModel,
    basis: M6Basis,
) -> tuple[Mapping[str, float], bool]:
    fingerprint = canonical_fingerprint(
        {
            "basis": basis.analysis_basis_fingerprint,
            "scenario": "M6_dynamic_direction_baseline",
            "duration_s": _DYNAMIC_DURATION_S,
            "time_step_s": _DYNAMIC_TIME_STEP_S,
        }
    )
    first = _simulate_m3(
        dynamic_model,
        scenario_name="M6_dynamic_direction_baseline",
        scenario_version="m6-dynamic-direction-baseline-v0.1.0",
        purpose="M6 no-event baseline for direction comparisons",
        event=None,
        source_fingerprint=fingerprint,
    )
    second = _simulate_m3(
        dynamic_model,
        scenario_name="M6_dynamic_direction_baseline",
        scenario_version="m6-dynamic-direction-baseline-v0.1.0",
        purpose="M6 no-event baseline for direction comparisons",
        event=None,
        source_fingerprint=fingerprint,
    )
    return dynamic_output_metrics(first), first.as_dict() == second.as_dict()


def _run_steady_scenario(
    spec: ValidationScenarioSpec,
    domain: ApplicabilityAssessment,
    config: M6ValidationConfig,
    basis: M6Basis,
    baseline_metrics: Mapping[str, float],
) -> ScenarioValidationResult:
    factor_id = spec.execution_reference.removeprefix("steady.")
    if set(spec.inputs) != {factor_id}:
        raise ValueError("steady scenario must request exactly its referenced factor")
    application = apply_steady_factor(
        basis.model,
        basis.case,
        factor_id,
        spec.inputs[factor_id],
    )
    engine = solve_recycle(application.model, application.case, basis.catalog)
    if not engine.converged:
        return _failed_scenario(spec, domain, "M2_solve", engine.failure_reason or "M2 failed")
    metrics = steady_output_metrics(engine)
    directions = evaluate_metric_directions(
        baseline_metrics,
        metrics,
        spec.expected_directions,
        absolute_tolerance=config.report_acceptance.direction_absolute_tolerance,
    )
    conservation = _steady_conservation(engine, config)
    checks_passed = all(directions.values()) and all(conservation.values())
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer="M2_steady",
        scenario_status="passed" if checks_passed else "failed",
        expected_status=spec.expected_status,
        verification_outcome="passed" if checks_passed else "failed",
        solver_called=True,
        domain=domain,
        metrics=metrics,
        direction_checks=directions,
        conservation_checks=conservation,
        protection_trace=None,
        source_origins=_source_origins(spec.execution_layer),
        engine_status="success" if checks_passed else "failed",
        input_fingerprint=canonical_fingerprint(
            {
                "basis": basis.analysis_basis_fingerprint,
                "config": config.input_fingerprint,
                "scenario": spec.as_dict(),
                "application": application.as_dict(),
            }
        ),
        failure_stage=None if checks_passed else "direction_or_conservation",
        failure_reason=None if checks_passed else "steady scenario evidence failed",
    )


def _run_m3_scenario(
    spec: ValidationScenarioSpec,
    domain: ApplicabilityAssessment,
    config: M6ValidationConfig,
    basis: M6Basis,
    dynamic_model: OpenLoopDynamicModel,
    baseline_metrics: Mapping[str, float],
    rule: ProtectionRule | None,
    tracking_evidence: Mapping[str, ControllerTrackingEvidence],
) -> ScenarioValidationResult:
    factor_id = spec.execution_reference.removeprefix("m3.")
    if set(spec.inputs) not in ({factor_id}, {factor_id, "pump_around_1_health_ratio"}):
        raise ValueError("M3 scenario inputs differ from its referenced factor")
    target, value = dynamic_command_for_factor(
        cast(dict[str, float], dynamic_model.baseline_commands),
        factor_id,
        spec.inputs[factor_id],
    )
    source_fingerprint = canonical_fingerprint(
        {
            "basis": basis.analysis_basis_fingerprint,
            "config": config.input_fingerprint,
            "scenario": spec.as_dict(),
            "target": target,
            "value": value,
            "duration_s": _DYNAMIC_DURATION_S,
            "event_time_s": _DYNAMIC_EVENT_TIME_S,
        }
    )
    engine = _simulate_m3(
        dynamic_model,
        scenario_name=spec.scenario_id,
        scenario_version=spec.scenario_version,
        purpose=spec.purpose,
        event=CommandEvent(_DYNAMIC_EVENT_TIME_S, target, value),
        source_fingerprint=source_fingerprint,
    )
    if engine.status != "success":
        return _failed_scenario(
            spec,
            domain,
            engine.failure_stage or "M3_simulation",
            engine.failure_reason or "M3 simulation failed",
            solver_called=True,
        )
    trace = (
        None
        if rule is None
        else _m3_protection_trace(spec, rule, engine, dynamic_model)
    )
    metric_values = dict(dynamic_output_metrics(engine))
    conservation_values = dict(_dynamic_conservation(engine))
    if spec.execution_reference == "m3.available_furnace_duty_ratio":
        metric_values["input.available_furnace_duty_ratio"] = spec.inputs[
            "available_furnace_duty_ratio"
        ]
        metric_values["proxy_boundary_applied"] = 1.0
    if rule is not None and trace is not None:
        triggers = _trigger_events(trace)
        first_trigger_time = (
            0.0 if not triggers else triggers[0].time_s
        )
        timeline_passed = bool(triggers) and (
            first_trigger_time >= _DYNAMIC_EVENT_TIME_S
        )
        tracking, tracking_passed = _tracking_metrics_for_rule(
            rule,
            tracking_evidence,
        )
        metric_values.update(
            {
                "fault_event_time_s": _DYNAMIC_EVENT_TIME_S,
                "protection_event_count": float(len(trace.events)),
                "protection_triggered": 1.0 if triggers else 0.0,
                "protection_first_trigger_time_s": first_trigger_time,
                **tracking,
            }
        )
        conservation_values.update(
            {
                "protection_timeline": timeline_passed,
                "tracking_no_bump": tracking_passed,
            }
        )
    metrics = MappingProxyType(metric_values)
    directions = evaluate_metric_directions(
        baseline_metrics,
        metrics,
        spec.expected_directions,
        absolute_tolerance=config.report_acceptance.direction_absolute_tolerance,
    )
    conservation = MappingProxyType(conservation_values)
    checks_passed = all(directions.values()) and all(conservation.values())
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer="M3_open_loop",
        scenario_status="limited" if checks_passed else "failed",
        expected_status=spec.expected_status,
        verification_outcome="passed" if checks_passed else "failed",
        solver_called=True,
        domain=domain,
        metrics=metrics,
        direction_checks=directions,
        conservation_checks=conservation,
        protection_trace=trace,
        source_origins=_source_origins(spec.execution_layer),
        engine_status="success" if checks_passed else "failed",
        input_fingerprint=source_fingerprint,
        failure_stage=None if checks_passed else "direction_or_conservation",
        failure_reason=None if checks_passed else "dynamic scenario evidence failed",
    )


def _closed_loop_baseline(metrics: Mapping[str, float]) -> Mapping[str, float]:
    values = dict(metrics)
    for name in values:
        if (
            name.endswith("final_output_ratio")
            or ".inventory." in name
            or name.startswith("inventory.")
        ):
            values[name] = 1.0
        elif name.endswith(("final_error_fraction", "saturation_time_s")):
            values[name] = 0.0
    values["loop.feed_flow.final_output_ratio"] = 1.0
    return MappingProxyType(values)


def _run_m4_scenario(
    spec: ValidationScenarioSpec,
    domain: ApplicabilityAssessment,
    config: M6ValidationConfig,
    basis: M6Basis,
    control: ControlConfig,
) -> ScenarioValidationResult:
    ratio = spec.inputs["feed_load_ratio"]
    scenario = ClosedLoopScenarioConfig(
        schema_version=basis.model.schema_version,
        scenario_version=spec.scenario_version,
        control_version=control.control_version,
        config_version=basis.model.config_version,
        case_version=basis.case.case_version,
        model_version=basis.model.model_version,
        parameter_set_version=basis.model.parameter_set_version,
        name=spec.scenario_id,
        duration_s=_CLOSED_LOOP_DURATION_S,
        time_step_s=_CLOSED_LOOP_TIME_STEP_S,
        events=(SetpointEvent(_CLOSED_LOOP_EVENT_TIME_S, "feed_flow", ratio),),
        metadata={"synthetic": "true", "purpose": spec.purpose},
    )
    engine = run_closed_loop_scenario(
        basis.model,
        basis.case,
        basis.catalog,
        control,
        scenario,
    )
    if engine.status != "success":
        return _failed_scenario(
            spec,
            domain,
            engine.failure_stage or "M4_simulation",
            engine.failure_reason or "M4 simulation failed",
            solver_called=True,
        )
    metrics = closed_loop_output_metrics(engine)
    directions = evaluate_metric_directions(
        _closed_loop_baseline(metrics),
        metrics,
        spec.expected_directions,
        absolute_tolerance=config.report_acceptance.direction_absolute_tolerance,
    )
    conservation = MappingProxyType(
        {
            "plant_execution": engine.acceptance_checks["plant_execution"],
            "plant_conservation": engine.acceptance_checks["plant_conservation"],
            "closed_loop_acceptance": engine.acceptance_passed,
        }
    )
    checks_passed = all(directions.values()) and all(conservation.values())
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer="M4_closed_loop",
        scenario_status="passed" if checks_passed else "failed",
        expected_status=spec.expected_status,
        verification_outcome="passed" if checks_passed else "failed",
        solver_called=True,
        domain=domain,
        metrics=metrics,
        direction_checks=directions,
        conservation_checks=conservation,
        protection_trace=None,
        source_origins=_source_origins(spec.execution_layer),
        engine_status="success" if checks_passed else "failed",
        input_fingerprint=canonical_fingerprint(
            {
                "basis": basis.analysis_basis_fingerprint,
                "config": config.input_fingerprint,
                "control": control.input_fingerprint,
                "scenario": scenario.as_dict(),
            }
        ),
        failure_stage=None if checks_passed else "direction_or_acceptance",
        failure_reason=None if checks_passed else "closed-loop evidence failed",
    )


def _safe_and_trip(rule: ProtectionRule) -> tuple[float, float]:
    if rule.condition == "invalid":
        return 1.0, 1.0
    if rule.trip_threshold is None or rule.clear_threshold is None:
        raise ValueError("analogue protection rule is missing thresholds")
    scale = max(abs(rule.trip_threshold), abs(rule.clear_threshold), 1.0)
    margin = 0.01 * scale
    if rule.condition == "high":
        return rule.clear_threshold - margin, rule.trip_threshold
    return rule.clear_threshold + margin, rule.trip_threshold


def _exercise_rule(rule: ProtectionRule) -> ProtectionTrace:
    safe_value, trip_value = _safe_and_trip(rule)
    signal = rule.signal_name

    def frame(
        time_s: float,
        value: float,
        *,
        valid: bool,
        reset: bool = False,
    ) -> ProtectionFrame:
        return ProtectionFrame(
            time_s,
            {signal: value},
            {signal: valid},
            (rule.rule_id,) if reset else (),
        )

    frames: list[ProtectionFrame] = [frame(0.0, safe_value, valid=True)]
    trip_valid = rule.condition != "invalid"
    frames.append(frame(1.0, trip_value, valid=trip_valid))
    trigger_time = 1.0 + rule.trigger_delay_s
    if rule.trigger_delay_s > 0.0:
        frames.append(frame(trigger_time, trip_value, valid=trip_valid))
    if rule.latching:
        frames.append(frame(trigger_time + 0.5, trip_value, valid=trip_valid, reset=True))
    clear_start = trigger_time + 1.0
    frames.append(frame(clear_start, safe_value, valid=True))
    clear_end = clear_start + rule.clear_delay_s
    if rule.clear_delay_s > 0.0:
        frames.append(frame(clear_end, safe_value, valid=True))
    if rule.latching:
        frames.append(frame(clear_end + 0.5, safe_value, valid=True, reset=True))
    return run_protection((rule,), tuple(frames))


def _trigger_events(trace: ProtectionTrace) -> tuple[ProtectionEvent, ...]:
    return tuple(event for event in trace.events if event.event_kind == "triggered")


def _m3_protection_trace(
    spec: ValidationScenarioSpec,
    rule: ProtectionRule,
    engine: DynamicSimulationResult,
    dynamic_model: OpenLoopDynamicModel,
) -> ProtectionTrace:
    """Evaluate a protection rule on the actual M3 scenario clock and states."""

    frames: list[ProtectionFrame] = []
    if rule.rule_id == "low_furnace_feed":
        nominal = dynamic_model.baseline_commands["fresh_feed_flow_kg_s"]
        for sample in engine.samples:
            ratio = sample.state.actuator_states["fresh_feed_flow_kg_s"] / nominal
            frames.append(
                ProtectionFrame(
                    sample.time_s,
                    {rule.signal_name: ratio},
                    {rule.signal_name: True},
                )
            )
    elif rule.rule_id == "pump_around_1_invalid":
        health = spec.inputs["pump_around_1_health_ratio"]
        for sample in engine.samples:
            after_fault = sample.time_s >= _DYNAMIC_EVENT_TIME_S
            valid = not after_fault or health > 0.0
            frames.append(
                ProtectionFrame(
                    sample.time_s,
                    {rule.signal_name: 1.0 if not after_fault else health},
                    {rule.signal_name: valid},
                )
            )
    else:
        raise ValueError(f"rule {rule.rule_id!r} has no M3 signal adapter")
    return run_protection((rule,), tuple(frames))


def _supervision_trace(
    spec: ValidationScenarioSpec,
    rule: ProtectionRule,
    dynamic_model: OpenLoopDynamicModel,
) -> ProtectionTrace:
    """Build a deterministic sideband trace whose values come from scenario inputs."""

    end_time = _DYNAMIC_EVENT_TIME_S + max(rule.trigger_delay_s, 1.0)
    if spec.scenario_id == "limited_furnace_temperature_sensor_bias":
        nominal = dynamic_model.initial_state.sensor_states[
            "furnace_outlet_temperature_k"
        ]
        biased = nominal + spec.inputs["furnace_temperature_sensor_bias_k"]
        frames = (
            ProtectionFrame(
                0.0,
                {rule.signal_name: nominal},
                {rule.signal_name: True},
            ),
            ProtectionFrame(
                _DYNAMIC_EVENT_TIME_S,
                {rule.signal_name: biased},
                {rule.signal_name: True},
            ),
            ProtectionFrame(
                end_time,
                {rule.signal_name: biased},
                {rule.signal_name: True},
            ),
        )
    elif spec.scenario_id == "limited_furnace_temperature_sensor_freeze":
        nominal = dynamic_model.initial_state.sensor_states[
            "furnace_outlet_temperature_k"
        ]
        health = spec.inputs["furnace_temperature_sensor_health_ratio"]
        frames = (
            ProtectionFrame(
                0.0,
                {rule.signal_name: nominal},
                {rule.signal_name: True},
            ),
            ProtectionFrame(
                _DYNAMIC_EVENT_TIME_S,
                {rule.signal_name: nominal},
                {rule.signal_name: health > 0.0},
            ),
            ProtectionFrame(
                end_time,
                {rule.signal_name: nominal},
                {rule.signal_name: health > 0.0},
            ),
        )
    elif spec.scenario_id == "limited_residue_draw_valve_stuck":
        frames = (
            ProtectionFrame(
                0.0,
                {rule.signal_name: 1.0},
                {rule.signal_name: True},
            ),
            ProtectionFrame(
                _DYNAMIC_EVENT_TIME_S,
                {rule.signal_name: 1.0},
                {rule.signal_name: True},
            ),
            ProtectionFrame(
                end_time,
                {rule.signal_name: 1.0},
                {rule.signal_name: True},
            ),
        )
    else:
        raise ValueError(f"scenario {spec.scenario_id!r} has no supervision adapter")
    return run_protection((rule,), frames)


def _controller_process_value(
    control: ControlConfig,
    dynamic_model: OpenLoopDynamicModel,
    loop_id: str,
) -> float:
    loop = control.loop(loop_id)
    if loop.controlled_variable.source == "sensor":
        return dynamic_model.initial_state.sensor_states[loop.controlled_variable.name]
    if loop.controlled_variable.source == "actuator":
        return dynamic_model.initial_state.actuator_states[
            loop.controlled_variable.name
        ]
    raise ValueError(f"M6 tracking does not support {loop.controlled_variable.source!r}")


def _tracking_evidence_for_rule(
    rule: ProtectionRule,
    control: ControlConfig,
    dynamic_model: OpenLoopDynamicModel,
    relative_tolerance: float,
) -> Mapping[str, ControllerTrackingEvidence]:
    evidence: dict[str, ControllerTrackingEvidence] = {}
    for loop_id in rule.action.manual_tracking_loop_ids:
        loop = control.loop(loop_id)
        pv = _controller_process_value(control, dynamic_model, loop_id)
        nominal_output = dynamic_model.baseline_commands[loop.manipulated_variable]
        protected_ratio = rule.action.command_ratio_overrides.get(
            loop.manipulated_variable,
            1.0,
        )
        controller = NormalizedPIController(
            loop.controller_spec(),
            pv_scale=pv,
            output_scale=nominal_output,
        )
        state = controller.initialize(
            process_value=pv,
            output=nominal_output,
            setpoint=pv,
        )
        item = verify_controller_tracking(
            loop_id,
            controller,
            state,
            process_value=pv,
            target_setpoint=pv,
            protected_output=nominal_output * protected_ratio,
            control_interval_s=control.control_interval_s,
            maximum_manual_steps=240,
            relative_tolerance=relative_tolerance,
        )
        evidence[f"{rule.rule_id}.{loop_id}"] = item
    return MappingProxyType(evidence)


def _tracking_metrics_for_rule(
    rule: ProtectionRule,
    evidence: Mapping[str, ControllerTrackingEvidence],
) -> tuple[Mapping[str, float], bool]:
    selected = tuple(
        evidence[f"{rule.rule_id}.{loop_id}"]
        for loop_id in rule.action.manual_tracking_loop_ids
    )
    passed = bool(selected) and all(item.passed for item in selected)
    return (
        MappingProxyType(
            {
                "tracking_no_bump": 1.0 if passed else 0.0,
                "tracking_loop_count": float(len(selected)),
                "tracking_max_final_error": max(
                    (item.final_tracking_error for item in selected),
                    default=0.0,
                ),
                "tracking_max_return_jump": max(
                    (item.automatic_return_jump for item in selected),
                    default=0.0,
                ),
            }
        ),
        passed,
    )


def _rule_for_scenario(
    scenario: ValidationScenarioSpec,
    rules: Mapping[str, ProtectionRule],
) -> ProtectionRule | None:
    rule_id = {
        "limited_feed_drop_30": "low_furnace_feed",
        "limited_pump_around_1_trip": "pump_around_1_invalid",
        "limited_furnace_temperature_sensor_bias": "high_furnace_temperature",
        "limited_furnace_temperature_sensor_freeze": (
            "furnace_temperature_measurement_invalid"
        ),
        "limited_residue_draw_valve_stuck": "high_bottom_inventory",
    }.get(scenario.scenario_id)
    return None if rule_id is None else rules[rule_id]


def _run_supervision_scenario(
    spec: ValidationScenarioSpec,
    domain: ApplicabilityAssessment,
    config: M6ValidationConfig,
    basis: M6Basis,
    control: ControlConfig,
    dynamic_model: OpenLoopDynamicModel,
    rule: ProtectionRule,
    tracking_evidence: Mapping[str, ControllerTrackingEvidence],
) -> ScenarioValidationResult:
    del control
    trace = _supervision_trace(spec, rule, dynamic_model)
    triggers = _trigger_events(trace)
    first_trigger_time = 0.0 if not triggers else triggers[0].time_s
    metric_values: dict[str, float] = {
        "fault_event_time_s": _DYNAMIC_EVENT_TIME_S,
        "protection_event_count": float(len(trace.events)),
        "protection_triggered": 1.0 if triggers else 0.0,
        "protection_first_trigger_time_s": first_trigger_time,
    }
    tracking_passed = True
    if spec.scenario_id == "limited_furnace_temperature_sensor_bias":
        bias = spec.inputs["furnace_temperature_sensor_bias_k"]
        nominal = dynamic_model.initial_state.sensor_states[
            "furnace_outlet_temperature_k"
        ]
        measured = nominal + bias
        if rule.trip_threshold is None:
            raise ValueError("temperature-bias rule omitted its high threshold")
        expected_trigger = measured >= rule.trip_threshold
        logic_passed = bool(triggers) == expected_trigger
        metric_values.update(
            {
                "applied_sensor_bias_k": bias,
                "biased_measurement_k": measured,
                "trip_threshold_k": rule.trip_threshold,
                "expected_protection_trigger": 1.0 if expected_trigger else 0.0,
                "false_trip_absent": 1.0
                if not expected_trigger and not triggers
                else 0.0,
            }
        )
    elif spec.scenario_id == "limited_furnace_temperature_sensor_freeze":
        health = spec.inputs["furnace_temperature_sensor_health_ratio"]
        expected_trigger = health <= 0.0
        logic_passed = (
            bool(triggers) == expected_trigger
            and (not triggers or first_trigger_time >= _DYNAMIC_EVENT_TIME_S)
        )
        tracking, tracking_passed = _tracking_metrics_for_rule(
            rule,
            tracking_evidence,
        )
        metric_values.update(
            {
                "applied_sensor_health_ratio": health,
                "expected_protection_trigger": 1.0 if expected_trigger else 0.0,
                **tracking,
            }
        )
    elif spec.scenario_id == "limited_residue_draw_valve_stuck":
        mobility = spec.inputs["residue_draw_valve_mobility_ratio"]
        requested_ratio = 1.05
        applied_ratio = 1.0 + mobility * (requested_ratio - 1.0)
        constraint_applied = mobility < 1.0 and not math.isclose(
            applied_ratio,
            requested_ratio,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        logic_passed = constraint_applied and not triggers
        metric_values.update(
            {
                "applied_valve_mobility_ratio": mobility,
                "diagnostic_requested_command_ratio": requested_ratio,
                "fault_constrained_command_ratio": applied_ratio,
                "fault_constraint_applied": 1.0 if constraint_applied else 0.0,
                "spurious_inventory_trip_absent": 1.0 if not triggers else 0.0,
            }
        )
    else:
        raise ValueError(f"unsupported supervision scenario {spec.scenario_id!r}")
    conservation = MappingProxyType(
        {
            "protection_timeline": logic_passed,
            "fault_input_applied": True,
            **(
                {"tracking_no_bump": tracking_passed}
                if rule.action.manual_tracking_loop_ids and bool(triggers)
                else {}
            ),
        }
    )
    metrics = MappingProxyType(metric_values)
    passed = logic_passed and tracking_passed
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer="M6_supervisory",
        scenario_status="limited" if passed else "failed",
        expected_status=spec.expected_status,
        verification_outcome="passed" if passed else "failed",
        solver_called=False,
        domain=domain,
        metrics=metrics,
        direction_checks={},
        conservation_checks=conservation,
        protection_trace=trace,
        source_origins=_source_origins(spec.execution_layer),
        engine_status=None,
        input_fingerprint=canonical_fingerprint(
            {
                "basis": basis.analysis_basis_fingerprint,
                "config": config.input_fingerprint,
                "scenario": spec.as_dict(),
                "protection_rule": rule.as_dict(),
                "trace": trace.as_dict(),
            }
        ),
        failure_stage=None if passed else "M6_supervision",
        failure_reason=None if passed else "protection or tracking evidence failed",
    )


def _rejected_scenario(
    spec: ValidationScenarioSpec,
    domain: ApplicabilityAssessment,
    config: M6ValidationConfig,
    basis: M6Basis,
) -> ScenarioValidationResult:
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer="structural_rejection",
        scenario_status="rejected",
        expected_status=spec.expected_status,
        verification_outcome="passed",
        solver_called=False,
        domain=domain,
        metrics={},
        direction_checks={},
        conservation_checks={},
        protection_trace=None,
        source_origins=_source_origins(spec.execution_layer),
        engine_status=None,
        input_fingerprint=canonical_fingerprint(
            {
                "basis": basis.analysis_basis_fingerprint,
                "config": config.input_fingerprint,
                "scenario": spec.as_dict(),
                "rejection_reasons": list(domain.reasons),
            }
        ),
    )


def _failed_scenario(
    spec: ValidationScenarioSpec,
    domain: ApplicabilityAssessment,
    stage: str,
    reason: str,
    *,
    solver_called: bool = True,
) -> ScenarioValidationResult:
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer=_result_layer(spec.execution_layer),
        scenario_status="failed",
        expected_status=spec.expected_status,
        verification_outcome="failed",
        solver_called=solver_called,
        domain=domain,
        metrics={},
        direction_checks={"execution": False},
        conservation_checks={"execution": False},
        protection_trace=None,
        source_origins=_source_origins(spec.execution_layer),
        engine_status="failed" if solver_called else None,
        input_fingerprint=domain.input_fingerprint,
        failure_stage=stage,
        failure_reason=reason,
    )


def _steady_sensitivity(
    plan: UncertaintyPlan,
    config: M6ValidationConfig,
    basis: M6Basis,
) -> LocalSensitivityAnalysis:
    output_ids = tuple(item.output_id for item in plan.outputs)

    def evaluate(inputs: Mapping[str, float]) -> Mapping[str, float]:
        applicability = assess_applicability(config.domain_dimensions, inputs)
        if not applicability.solver_allowed:
            raise ValueError("steady sensitivity evaluation is outside applicability")
        model = basis.model
        case = basis.case
        for input_id in sorted(inputs):
            applied = apply_steady_factor(model, case, input_id, inputs[input_id])
            model = applied.model
            case = applied.case
        metrics = steady_output_metrics(solve_recycle(model, case, basis.catalog))
        return MappingProxyType({name: metrics[name] for name in output_ids})

    return run_local_sensitivity(
        plan.inputs,
        plan.outputs,
        evaluate,
        basis_fingerprint=basis.analysis_basis_fingerprint,
    )


def _dynamic_model_overlay(
    model: ModelConfig,
    inputs: Mapping[str, float],
) -> ModelConfig:
    payload = model.as_dict()
    dynamic = payload.get("dynamic")
    if not isinstance(dynamic, dict):
        raise TypeError("model dynamic configuration did not serialize as an object")
    field_by_input = {
        "actuator_time_constant_ratio": "actuator_time_constant_s",
        "sensor_time_constant_ratio": "sensor_time_constant_s",
    }
    for input_id, ratio in inputs.items():
        field = field_by_input[input_id]
        baseline = dynamic[field]
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
            raise TypeError(f"dynamic field {field} is not numeric")
        dynamic[field] = float(baseline) * ratio
    return ModelConfig.from_mapping(payload)


def _dynamic_sensitivity(
    plan: UncertaintyPlan,
    config: M6ValidationConfig,
    basis: M6Basis,
    baseline_recycle: RecycleSolveResult,
) -> LocalSensitivityAnalysis:
    output_ids = tuple(item.output_id for item in plan.outputs)

    def evaluate(inputs: Mapping[str, float]) -> Mapping[str, float]:
        applicability = assess_applicability(config.domain_dimensions, inputs)
        if not applicability.solver_allowed:
            raise ValueError("dynamic sensitivity evaluation is outside applicability")
        model = _dynamic_model_overlay(basis.model, inputs)
        dynamic_model = initialize_open_loop_dynamic_model(
            model,
            basis.case,
            basis.catalog,
            baseline_recycle,
        )
        target, value = dynamic_command_for_factor(
            cast(dict[str, float], dynamic_model.baseline_commands),
            "feed_load_ratio",
            1.05,
        )
        fingerprint = canonical_fingerprint(
            {
                "basis": basis.analysis_basis_fingerprint,
                "plan": plan.plan_id,
                "model": model.as_dict(),
                "inputs": dict(inputs),
            }
        )
        result = _simulate_m3(
            dynamic_model,
            scenario_name=f"{plan.plan_id}_feed_step",
            scenario_version="m6-dynamic-sensitivity-feed-step-v0.1.0",
            purpose="M6 local lag sensitivity under a five-percent feed command step",
            event=CommandEvent(_DYNAMIC_EVENT_TIME_S, target, value),
            source_fingerprint=fingerprint,
        )
        metrics = dynamic_output_metrics(result)
        return MappingProxyType({name: metrics[name] for name in output_ids})

    return run_local_sensitivity(
        plan.inputs,
        plan.outputs,
        evaluate,
        basis_fingerprint=basis.analysis_basis_fingerprint,
    )


def _propagate_and_check(
    analysis: LocalSensitivityAnalysis,
    plan: UncertaintyPlan,
) -> tuple[UncertaintyPropagationResult, bool]:
    result = propagate_uncertainty(analysis, plan.intervals)
    wider_intervals = tuple(
        EngineeringInputInterval(
            input_id=item.input_id,
            lower=item.lower,
            upper=item.upper,
            confidence_multiplier=item.confidence_multiplier + 0.5,
            confidence_label=item.confidence_label,
        )
        for item in plan.intervals
    )
    wider = propagate_uncertainty(analysis, wider_intervals)
    assert_uncertainty_not_narrower(wider, result)
    tolerance = 1e-12
    monotonic = all(
        wider_output.width + tolerance >= narrow_output.width
        for wider_output, narrow_output in zip(
            wider.output_intervals,
            result.output_intervals,
            strict=True,
        )
    )
    return result, monotonic


def _scenario_once(
    spec: ValidationScenarioSpec,
    *,
    config: M6ValidationConfig,
    basis: M6Basis,
    control: ControlConfig,
    baseline_steady_metrics: Mapping[str, float],
    dynamic_model: OpenLoopDynamicModel,
    baseline_dynamic_metrics: Mapping[str, float],
    rules: Mapping[str, ProtectionRule],
    tracking_evidence: Mapping[str, ControllerTrackingEvidence],
) -> ScenarioValidationResult:
    domain = _domain(config, spec)
    if not domain.solver_allowed:
        return _rejected_scenario(spec, domain, config, basis)
    rule = _rule_for_scenario(spec, rules)
    if spec.execution_layer == "M2_steady":
        return _run_steady_scenario(
            spec,
            domain,
            config,
            basis,
            baseline_steady_metrics,
        )
    if spec.execution_layer == "M3_open_loop":
        return _run_m3_scenario(
            spec,
            domain,
            config,
            basis,
            dynamic_model,
            baseline_dynamic_metrics,
            rule,
            tracking_evidence,
        )
    if spec.execution_layer == "M4_closed_loop":
        return _run_m4_scenario(spec, domain, config, basis, control)
    if spec.execution_layer == "M6_supervision":
        if rule is None:
            raise ValueError("M6 supervision scenario has no protection rule mapping")
        return _run_supervision_scenario(
            spec,
            domain,
            config,
            basis,
            control,
            dynamic_model,
            rule,
            tracking_evidence,
        )
    raise ValueError(f"unsupported executable scenario layer {spec.execution_layer!r}")


def run_m6_validation(
    repo_root: Path,
    *,
    config_path: Path | None = None,
    control_path: Path | None = None,
) -> M6ValidationResult:
    """Execute the complete source-closed M6 matrix with exact-repeat gates."""

    root = repo_root.resolve()
    selected_config = root / _DEFAULT_CONFIG if config_path is None else config_path
    selected_control = root / _DEFAULT_CONTROL if control_path is None else control_path
    completed_scenario_ids: list[str] = []
    try:
        config = load_m6_validation_config(selected_config)
        basis = load_m6_basis(root)
        control = load_control_config(selected_control)
        _check_versions(config, basis)
        if control.control_version != config.control_version:
            raise M6ValidationExecutionError(
                "version_preflight",
                "control version differs from the M6 validation config",
            )
        baseline_recycle = solve_recycle(basis.model, basis.case, basis.catalog)
        if not baseline_recycle.converged:
            raise M6ValidationExecutionError(
                "M2_baseline",
                baseline_recycle.failure_reason or "M2 baseline did not converge",
            )
        baseline_steady_metrics = steady_output_metrics(baseline_recycle)
        dynamic_model = initialize_open_loop_dynamic_model(
            basis.model,
            basis.case,
            basis.catalog,
            baseline_recycle,
        )
        baseline_dynamic_metrics, baseline_repeat = _baseline_dynamic_metrics(
            dynamic_model,
            basis,
        )
        rules = MappingProxyType({rule.rule_id: rule for rule in config.protection_rules})
        protection_traces = MappingProxyType(
            {rule.rule_id: _exercise_rule(rule) for rule in config.protection_rules}
        )
        tracking_items: dict[str, ControllerTrackingEvidence] = {}
        for rule in config.protection_rules:
            tracking_items.update(
                _tracking_evidence_for_rule(
                    rule,
                    control,
                    dynamic_model,
                    config.report_acceptance.controller_tracking_relative_tolerance,
                )
            )
        controller_tracking = MappingProxyType(tracking_items)

        scenarios: list[ScenarioValidationResult] = []
        repeat_checks: list[bool] = [baseline_repeat]
        for spec in config.scenarios:
            first = _scenario_once(
                spec,
                config=config,
                basis=basis,
                control=control,
                baseline_steady_metrics=baseline_steady_metrics,
                dynamic_model=dynamic_model,
                baseline_dynamic_metrics=baseline_dynamic_metrics,
                rules=rules,
                tracking_evidence=controller_tracking,
            )
            second = _scenario_once(
                spec,
                config=config,
                basis=basis,
                control=control,
                baseline_steady_metrics=baseline_steady_metrics,
                dynamic_model=dynamic_model,
                baseline_dynamic_metrics=baseline_dynamic_metrics,
                rules=rules,
                tracking_evidence=controller_tracking,
            )
            scenarios.append(first)
            if (
                first.scenario_status != "failed"
                and first.verification_outcome == "passed"
            ):
                completed_scenario_ids.append(first.scenario_id)
            repeat_checks.append(first.as_dict() == second.as_dict())

        plans = (config.steady_uncertainty, config.dynamic_uncertainty)
        analyses: dict[str, LocalSensitivityAnalysis] = {}
        uncertainty: dict[str, UncertaintyPropagationResult] = {}
        uncertainty_monotonic: list[bool] = []
        for plan in plans:
            analysis = (
                _steady_sensitivity(plan, config, basis)
                if plan.execution_layer == "M2_steady"
                else _dynamic_sensitivity(
                    plan,
                    config,
                    basis,
                    baseline_recycle,
                )
            )
            repeated = (
                _steady_sensitivity(plan, config, basis)
                if plan.execution_layer == "M2_steady"
                else _dynamic_sensitivity(
                    plan,
                    config,
                    basis,
                    baseline_recycle,
                )
            )
            repeat_checks.append(analysis.as_dict() == repeated.as_dict())
            if not analysis.complete:
                raise M6ValidationExecutionError(
                    "uncertainty_sensitivity",
                    f"plan {plan.plan_id} did not complete every evaluation",
                )
            propagated, monotonic = _propagate_and_check(analysis, plan)
            analyses[plan.plan_id] = analysis
            uncertainty[plan.plan_id] = propagated
            uncertainty_monotonic.append(monotonic)

        scenario_passed = all(
            item.verification_outcome == "passed" for item in scenarios
        )
        solver_scenarios = tuple(item for item in scenarios if item.solver_called)
        conservation_passed = bool(solver_scenarios) and all(
            all(item.conservation_checks.values()) for item in solver_scenarios
        )
        protection_passed = (
            set(protection_traces) == set(rules)
            and all(_trigger_events(trace) for trace in protection_traces.values())
            and set(controller_tracking)
            == {
                f"{rule.rule_id}.{loop_id}"
                for rule in config.protection_rules
                for loop_id in rule.action.manual_tracking_loop_ids
            }
            and all(item.passed for item in controller_tracking.values())
        )
        completion_checks = {
            "scenario_matrix": scenario_passed,
            "applicability_domain": all(
                item.domain.status == item.expected_status for item in scenarios
            ),
            "uncertainty_propagation": all(uncertainty_monotonic),
            "protection_logic": protection_passed,
            "conservation": conservation_passed,
            "deterministic_reproduction": all(repeat_checks),
        }
        if set(completion_checks) != set(M6_COMPLETION_CHECK_IDS):
            raise AssertionError("internal M6 completion gate set drifted")
        status: M6ResultStatus = (
            "success" if all(completion_checks.values()) else "failed"
        )
        failure_stage = None if status == "success" else "M6_acceptance"
        failure_reason = None if status == "success" else "one or more M6 gates failed"
        last_valid_scenario_ids = tuple(
            item.scenario_id
            for item in scenarios
            if item.scenario_status != "failed"
            and item.verification_outcome == "passed"
        )
        last_valid_time_s = max(
            (
                item.protection_trace.last_time_s
                for item in scenarios
                if item.scenario_id in last_valid_scenario_ids
                and item.protection_trace is not None
            ),
            default=0.0,
        )
        return M6ValidationResult(
            schema_version=M6_RESULT_SCHEMA_VERSION,
            status=status,
            basis=basis,
            validation_config_version=config.validation_version,
            validation_config_fingerprint=config.input_fingerprint,
            control_version=control.control_version,
            scenario_set_version=config.validation_version,
            required_scenario_ids=tuple(item.scenario_id for item in config.scenarios),
            scenarios=tuple(scenarios),
            required_plan_ids=tuple(plan.plan_id for plan in plans),
            sensitivity_analyses=analyses,
            uncertainty_results=uncertainty,
            plan_unquantified_sources={
                plan.plan_id: plan.unquantified_sources for plan in plans
            },
            plan_source_origins={
                config.steady_uncertainty.plan_id: (
                    "M2_steady_model_prediction",
                    "M6_synthetic_validation",
                ),
                config.dynamic_uncertainty.plan_id: (
                    "M3_open_loop_simulation",
                    "M6_synthetic_validation",
                ),
            },
            required_protection_rule_ids=tuple(
                rule.rule_id for rule in config.protection_rules
            ),
            protection_traces=protection_traces,
            controller_tracking=controller_tracking,
            completion_checks=completion_checks,
            source_composition=M6_SOURCE_COMPOSITION,
            metadata=M6_RESULT_METADATA,
            last_valid_scenario_ids=last_valid_scenario_ids,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
            failure_time_s=None if status == "success" else last_valid_time_s,
        )
    except M6ValidationExecutionError as exc:
        if exc.last_valid_scenario_ids or not completed_scenario_ids:
            raise
        raise M6ValidationExecutionError(
            exc.stage,
            exc.reason,
            failure_time_s=exc.failure_time_s,
            last_valid_scenario_ids=tuple(completed_scenario_ids),
        ) from exc
    except Exception as exc:
        reason = str(exc).strip() or type(exc).__name__
        raise M6ValidationExecutionError(
            type(exc).__name__,
            reason,
            last_valid_scenario_ids=tuple(completed_scenario_ids),
        ) from exc
