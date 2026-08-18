"""Pure M7 execution dispatcher over the accepted M2, M3, M4 and M6 layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import cast

from petroleum_rto import __version__ as SOFTWARE_VERSION

from ..control.config import ControlConfig
from ..control.controllers import NormalizedPIController
from ..control.runner import run_closed_loop_scenario
from ..control.scenario import ClosedLoopScenarioConfig
from ..control.simulation import simulate_closed_loop
from ..core.config import (
    ScenarioConfig,
    canonical_fingerprint,
    validate_config_compatibility,
)
from ..dynamics.equations import OpenLoopDynamicModel
from ..dynamics.initialization import initialize_open_loop_dynamic_model
from ..dynamics.runner import run_dynamic_scenario
from ..dynamics.schedule import CommandEvent, CommandSchedule
from ..dynamics.simulation import DynamicSimulationResult, simulate_dynamic
from ..flowsheet.recycle import RecycleSolveResult, solve_recycle
from ..validation.config import M6ValidationConfig, ValidationScenarioSpec
from ..validation.domain import assess_applicability
from ..validation.metrics import dynamic_output_metrics, evaluate_metric_directions
from ..validation.protection import (
    ProtectionFrame,
    ProtectionRule,
    ProtectionTrace,
    run_protection,
)
from ..validation.results import ScenarioValidationResult
from ..validation.scenarios import dynamic_command_for_factor
from ..validation.tracking import (
    ControllerTrackingEvidence,
    verify_controller_tracking,
)
from .adapters import (
    adapt_closed_loop_result,
    adapt_dynamic_result,
    adapt_exception,
    adapt_recycle_result,
    adapt_validation_scenario,
    validation_command_event,
)
from .contracts import ExecutionPayload, JsonValue, RunRequest, RuntimeStatus
from .custom_inputs import (
    ResolvedRuntimeInputs,
    apply_initial_inventory_ratios,
    resolve_runtime_inputs,
    validate_runtime_request_shape,
)
from .presets import RuntimePreset, get_preset
from .resources import RuntimeResourceBundle, load_runtime_resource_bundle

_M6_DURATION_S = 600.0
_M6_EVENT_TIME_S = 60.0
_M6_TIME_STEP_S = 1.0
_M6_PUMP_SCENARIO_ID = "limited_pump_around_1_trip"
_M6_REJECTION_SCENARIO_ID = "rejected_stripping_steam_request"
_M6_PUMP_RULE_ID = "pump_around_1_invalid"


def _json_mapping(value: Mapping[str, object]) -> Mapping[str, JsonValue]:
    return cast(Mapping[str, JsonValue], value)


def _common_versions(bundle: RuntimeResourceBundle) -> dict[str, str]:
    basis = bundle.m6_basis
    return {
        "analysis_basis_version": basis.analysis_version,
        "base_case_version": basis.base_case_version,
        "base_parameter_set_version": basis.base_parameter_set_version,
        "case_version": bundle.effective_case.case_version,
        "control_version": bundle.control.control_version,
        "derived_case_version": basis.derived_case_version,
        "derived_parameter_set_version": basis.derived_parameter_set_version,
        "m5_overlay_version": bundle.m5_overlay.overlay_version,
        "model_config_version": bundle.effective_model.config_version,
        "model_version": bundle.effective_model.model_version,
        "parameter_set_version": bundle.effective_model.parameter_set_version,
        "scenario_version": "not_applicable",
        "software_version": SOFTWARE_VERSION,
        "validation_version": bundle.validation_config.validation_version,
    }


def _source_fingerprints(bundle: RuntimeResourceBundle) -> dict[str, str]:
    fingerprints = dict(bundle.resource_fingerprints)
    fingerprints.update(
        {
            "m5_manifest": bundle.m6_basis.m5_manifest_sha256,
            "m5_pipeline_result": bundle.m6_basis.m5_pipeline_fingerprint,
            "m6_formal_result": bundle.m6_result_fingerprint,
        }
    )
    fingerprints.update(
        {
            f"m5_artifact.{name}": digest
            for name, digest in bundle.m6_basis.m5_artifact_sha256.items()
        }
    )
    fingerprints.update(
        {
            f"effective_object.{name}": digest
            for name, digest in bundle.m6_basis.effective_object_fingerprints.items()
        }
    )
    return {name: fingerprints[name] for name in sorted(fingerprints)}


def _resolved_source_fingerprints(
    bundle: RuntimeResourceBundle,
    resolved: ResolvedRuntimeInputs,
) -> dict[str, str]:
    fingerprints = _source_fingerprints(bundle)
    if resolved.is_custom:
        fingerprints.update(
            {
                "runtime_custom_input_preview": resolved.preview_fingerprint,
                **{
                    f"runtime_effective_object.{name}": digest
                    for name, digest in resolved.effective_object_fingerprints.items()
                },
            }
        )
    return {name: fingerprints[name] for name in sorted(fingerprints)}


def _effective_fingerprint(
    bundle: RuntimeResourceBundle,
    request: RunRequest,
    *,
    extra: Mapping[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "request": request.fingerprint_payload(),
        "effective_model": bundle.effective_model.as_dict(),
        "effective_case": bundle.effective_case.as_dict(),
        "component_catalog": bundle.catalog.as_dict(),
        "m5_analysis_basis": bundle.m6_basis.analysis_basis_fingerprint,
        "resource_fingerprints": dict(bundle.resource_fingerprints),
    }
    if extra is not None:
        payload.update(extra)
    return canonical_fingerprint(payload)


def _open_loop_scenario(
    bundle: RuntimeResourceBundle,
    preset: RuntimePreset,
) -> ScenarioConfig:
    for scenario in bundle.open_loop_scenarios.values():
        if scenario.scenario_version == preset.scenario_id:
            return scenario
    raise ValueError(f"packaged open-loop scenario {preset.scenario_id!r} is unavailable")


def _closed_loop_scenario(
    bundle: RuntimeResourceBundle,
    preset: RuntimePreset,
) -> ClosedLoopScenarioConfig:
    for scenario in bundle.closed_loop_scenarios.values():
        if scenario.scenario_version == preset.scenario_id:
            return scenario
    raise ValueError(f"packaged closed-loop scenario {preset.scenario_id!r} is unavailable")


def _validation_scenario(
    config: M6ValidationConfig,
    scenario_id: str,
) -> ValidationScenarioSpec:
    try:
        return next(item for item in config.scenarios if item.scenario_id == scenario_id)
    except StopIteration as exc:
        raise ValueError(f"packaged M6 config has no scenario {scenario_id!r}") from exc


def _protection_rule(
    config: M6ValidationConfig,
    rule_id: str,
) -> ProtectionRule:
    try:
        return next(item for item in config.protection_rules if item.rule_id == rule_id)
    except StopIteration as exc:
        raise ValueError(f"packaged M6 config has no rule {rule_id!r}") from exc


def _execute_steady(
    request: RunRequest,
    bundle: RuntimeResourceBundle,
) -> ExecutionPayload:
    effective = _effective_fingerprint(bundle, request)
    result = solve_recycle(
        bundle.effective_model,
        bundle.effective_case,
        bundle.catalog,
    )
    return adapt_recycle_result(
        request,
        result,
        versions={**_common_versions(bundle), "simulation_stage": "M2"},
        source_fingerprints=_source_fingerprints(bundle),
        effective_input_fingerprint=effective,
    )


def _execute_open_loop(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
) -> ExecutionPayload:
    scenario = _open_loop_scenario(bundle, preset)
    effective = _effective_fingerprint(
        bundle,
        request,
        extra={"scenario": scenario.as_dict()},
    )
    result = run_dynamic_scenario(
        bundle.effective_model,
        bundle.effective_case,
        bundle.catalog,
        scenario,
    )
    return adapt_dynamic_result(
        request,
        scenario,
        result,
        versions={
            **_common_versions(bundle),
            "scenario_version": scenario.scenario_version,
            "simulation_stage": "M3",
        },
        source_fingerprints=_source_fingerprints(bundle),
        effective_input_fingerprint=effective,
    )


def _execute_closed_loop(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
) -> ExecutionPayload:
    scenario = _closed_loop_scenario(bundle, preset)
    effective = _effective_fingerprint(
        bundle,
        request,
        extra={
            "control": bundle.control.as_dict(),
            "scenario": scenario.as_dict(),
        },
    )
    result = run_closed_loop_scenario(
        bundle.effective_model,
        bundle.effective_case,
        bundle.catalog,
        bundle.control,
        scenario,
    )
    return adapt_closed_loop_result(
        request,
        scenario,
        result,
        versions={
            **_common_versions(bundle),
            "control_version": bundle.control.control_version,
            "scenario_version": scenario.scenario_version,
            "simulation_stage": "M4",
        },
        source_fingerprints=_source_fingerprints(bundle),
        effective_input_fingerprint=effective,
    )


def _resolved_version_mapping(
    resolved: ResolvedRuntimeInputs,
    bundle: RuntimeResourceBundle,
) -> dict[str, str]:
    versions = validate_config_compatibility(
        resolved.model,
        resolved.case,
        software_version=SOFTWARE_VERSION,
        catalog=bundle.catalog,
    )
    return {name: value for name, value in versions.as_dict().items() if value is not None}


def _execute_custom_steady(
    request: RunRequest,
    bundle: RuntimeResourceBundle,
    resolved: ResolvedRuntimeInputs,
) -> ExecutionPayload:
    result = solve_recycle(resolved.model, resolved.case, bundle.catalog)
    return adapt_recycle_result(
        request,
        result,
        versions={**_common_versions(bundle), "simulation_stage": "M2"},
        source_fingerprints=_resolved_source_fingerprints(bundle, resolved),
        effective_input_fingerprint=resolved.execution_input_fingerprint,
    )


def _resolved_open_loop_events(
    resolved: ResolvedRuntimeInputs,
    dynamic_model: OpenLoopDynamicModel,
) -> tuple[CommandEvent, ...]:
    if resolved.event_requests is None:
        return ()
    events: list[CommandEvent] = []
    for event in resolved.event_requests:
        value = (
            event.value
            if event.value_basis == "absolute"
            else dynamic_model.baseline_commands[event.target] * event.value
        )
        events.append(
            CommandEvent(
                event.time_s,
                event.target,
                value,
                event.duration_s,
            )
        )
    return tuple(events)


def _execute_custom_open_loop(
    request: RunRequest,
    bundle: RuntimeResourceBundle,
    resolved: ResolvedRuntimeInputs,
) -> ExecutionPayload:
    scenario = resolved.open_loop_scenario
    if scenario is None or resolved.duration_s is None or resolved.time_step_s is None:
        raise ValueError("custom open-loop execution lacks a resolved scenario")
    recycle = solve_recycle(resolved.model, resolved.case, bundle.catalog)
    if not recycle.converged:
        stage = recycle.failure_stage or "unknown"
        reason = recycle.failure_reason or "M2 prerequisite did not converge"
        raise RuntimeError(f"M3 prerequisite failed at {stage}: {reason}")
    nominal_model = initialize_open_loop_dynamic_model(
        resolved.model,
        resolved.case,
        bundle.catalog,
        recycle,
    )
    actual_state = apply_initial_inventory_ratios(
        nominal_model.initial_state,
        resolved.initial_inventory_ratios,
    )
    dynamic_model = (
        nominal_model
        if actual_state == nominal_model.initial_state
        else replace(nominal_model, initial_state=actual_state)
    )
    events = _resolved_open_loop_events(resolved, dynamic_model)
    actual_scenario = replace(
        scenario,
        events=tuple(
            {
                "time_s": event.time_s,
                "target": event.target,
                "value": event.value,
                **({} if event.duration_s is None else {"duration_s": event.duration_s}),
            }
            for event in events
        ),
    )
    versions = _resolved_version_mapping(resolved, bundle)
    versions["simulation_stage"] = "M3"
    result = simulate_dynamic(
        dynamic_model,
        CommandSchedule(dynamic_model.baseline_commands, events),
        resolved.duration_s,
        resolved.time_step_s,
        fingerprint=canonical_fingerprint(
            {
                "execution_input_fingerprint": resolved.execution_input_fingerprint,
                "dynamic_model": dynamic_model.input_fingerprint,
                "initial_state": actual_state.as_dict(),
                "scenario": actual_scenario.as_dict(),
            }
        ),
        versions=versions,
        metadata={
            **actual_scenario.metadata,
            "scenario_name": actual_scenario.name,
            "scenario_version": actual_scenario.scenario_version,
        },
    )
    return adapt_dynamic_result(
        request,
        actual_scenario,
        result,
        versions={
            **_common_versions(bundle),
            "scenario_version": actual_scenario.scenario_version,
            "simulation_stage": "M3",
        },
        source_fingerprints=_resolved_source_fingerprints(bundle, resolved),
        effective_input_fingerprint=resolved.execution_input_fingerprint,
    )


def _execute_custom_closed_loop(
    request: RunRequest,
    bundle: RuntimeResourceBundle,
    resolved: ResolvedRuntimeInputs,
) -> ExecutionPayload:
    scenario = resolved.closed_loop_scenario
    if scenario is None:
        raise ValueError("custom closed-loop execution lacks a resolved scenario")
    recycle = solve_recycle(resolved.model, resolved.case, bundle.catalog)
    if not recycle.converged:
        stage = recycle.failure_stage or "unknown"
        reason = recycle.failure_reason or "M2 prerequisite did not converge"
        raise RuntimeError(f"M4 prerequisite failed at {stage}: {reason}")
    dynamic_model = initialize_open_loop_dynamic_model(
        resolved.model,
        resolved.case,
        bundle.catalog,
        recycle,
    )
    actual_state = apply_initial_inventory_ratios(
        dynamic_model.initial_state,
        resolved.initial_inventory_ratios,
    )
    result = simulate_closed_loop(
        dynamic_model,
        bundle.control,
        scenario,
        versions=_resolved_version_mapping(resolved, bundle),
        plant_initial_state=actual_state,
    )
    return adapt_closed_loop_result(
        request,
        scenario,
        result,
        versions={
            **_common_versions(bundle),
            "control_version": bundle.control.control_version,
            "scenario_version": scenario.scenario_version,
            "simulation_stage": "M4",
        },
        source_fingerprints=_resolved_source_fingerprints(bundle, resolved),
        effective_input_fingerprint=resolved.execution_input_fingerprint,
    )


def _simulate_m6_dynamic(
    dynamic_model: OpenLoopDynamicModel,
    *,
    spec: ValidationScenarioSpec,
    event: CommandEvent | None,
    source_fingerprint: str,
    scenario_suffix: str,
) -> DynamicSimulationResult:
    return simulate_dynamic(
        dynamic_model,
        CommandSchedule(
            dynamic_model.baseline_commands,
            () if event is None else (event,),
        ),
        _M6_DURATION_S,
        _M6_TIME_STEP_S,
        fingerprint=source_fingerprint,
        versions=dynamic_model.versions,
        metadata={
            "scenario_name": f"{spec.scenario_id}_{scenario_suffix}",
            "scenario_version": f"{spec.scenario_version}-{scenario_suffix}",
            "purpose": spec.purpose,
            "synthetic": "true",
        },
    )


def _m6_pump_trace(
    rule: ProtectionRule,
    result: DynamicSimulationResult,
    *,
    health_ratio: float,
) -> ProtectionTrace:
    frames = tuple(
        ProtectionFrame(
            sample.time_s,
            {rule.signal_name: (1.0 if sample.time_s < _M6_EVENT_TIME_S else health_ratio)},
            {rule.signal_name: (sample.time_s < _M6_EVENT_TIME_S or health_ratio > 0.0)},
        )
        for sample in result.samples
    )
    return run_protection((rule,), frames)


def _controller_process_value(
    control: ControlConfig,
    dynamic_model: OpenLoopDynamicModel,
    loop_id: str,
) -> float:
    variable = control.loop(loop_id).controlled_variable
    if variable.source == "sensor":
        return dynamic_model.initial_state.sensor_states[variable.name]
    if variable.source == "actuator":
        return dynamic_model.initial_state.actuator_states[variable.name]
    raise ValueError(f"portable M6 tracking does not support source {variable.source!r}")


def _tracking_evidence(
    rule: ProtectionRule,
    control: ControlConfig,
    dynamic_model: OpenLoopDynamicModel,
    *,
    relative_tolerance: float,
) -> tuple[tuple[ControllerTrackingEvidence, ...], bool]:
    evidence: list[ControllerTrackingEvidence] = []
    for loop_id in rule.action.manual_tracking_loop_ids:
        loop = control.loop(loop_id)
        process_value = _controller_process_value(control, dynamic_model, loop_id)
        nominal_output = dynamic_model.baseline_commands[loop.manipulated_variable]
        protected_ratio = rule.action.command_ratio_overrides.get(
            loop.manipulated_variable,
            1.0,
        )
        controller = NormalizedPIController(
            loop.controller_spec(),
            pv_scale=process_value,
            output_scale=nominal_output,
        )
        state = controller.initialize(
            process_value=process_value,
            output=nominal_output,
            setpoint=process_value,
        )
        evidence.append(
            verify_controller_tracking(
                loop_id,
                controller,
                state,
                process_value=process_value,
                target_setpoint=process_value,
                protected_output=nominal_output * protected_ratio,
                control_interval_s=control.control_interval_s,
                maximum_manual_steps=240,
                relative_tolerance=relative_tolerance,
            )
        )
    frozen = tuple(evidence)
    return frozen, bool(frozen) and all(item.passed for item in frozen)


def _failed_m6_scenario(
    spec: ValidationScenarioSpec,
    config: M6ValidationConfig,
    *,
    stage: str,
    reason: str,
    input_fingerprint: str,
) -> ScenarioValidationResult:
    domain = assess_applicability(
        config.domain_dimensions,
        spec.inputs,
        abnormal_verification=spec.abnormal_verification,
    )
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer="M3_open_loop",
        scenario_status="failed",
        expected_status=spec.expected_status,
        verification_outcome="failed",
        solver_called=True,
        domain=domain,
        metrics={},
        direction_checks={"execution": False},
        conservation_checks={"execution": False},
        protection_trace=None,
        source_origins=("M3_open_loop_simulation", "M6_synthetic_validation"),
        engine_status="failed",
        input_fingerprint=input_fingerprint,
        failure_stage=stage,
        failure_reason=reason,
    )


def _m6_versions(
    bundle: RuntimeResourceBundle,
    spec: ValidationScenarioSpec,
) -> dict[str, str]:
    return {
        **_common_versions(bundle),
        "control_version": bundle.control.control_version,
        "execution_profile": "portable_selected_scenario_replay",
        "scenario_version": spec.scenario_version,
        "simulation_stage": "M6",
        "validation_version": bundle.validation_config.validation_version,
    }


def _execute_m6_structural_rejection(
    request: RunRequest,
    bundle: RuntimeResourceBundle,
) -> ExecutionPayload:
    config = bundle.validation_config
    spec = _validation_scenario(config, _M6_REJECTION_SCENARIO_ID)
    domain = assess_applicability(
        config.domain_dimensions,
        spec.inputs,
        abnormal_verification=spec.abnormal_verification,
    )
    if domain.status != "rejected":
        raise ValueError("packaged structural-rejection scenario is not rejected")
    effective = _effective_fingerprint(
        bundle,
        request,
        extra={
            "m6_config": config.as_dict(),
            "scenario": spec.as_dict(),
            "domain": domain.as_dict(),
            "execution_profile": "portable_selected_scenario_replay",
        },
    )
    result = ScenarioValidationResult(
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
        source_origins=("M6_synthetic_validation",),
        engine_status=None,
        input_fingerprint=effective,
    )
    return adapt_validation_scenario(
        request,
        result,
        versions=_m6_versions(bundle, spec),
        source_fingerprints=_source_fingerprints(bundle),
        effective_input_fingerprint=effective,
        formal_m6_result_fingerprint=bundle.m6_result_fingerprint,
        duration_s=None,
        time_step_s=None,
    )


def _m6_prerequisite_failure(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
    recycle: RecycleSolveResult,
    *,
    effective_input_fingerprint: str,
) -> ExecutionPayload:
    runtime_status = cast(RuntimeStatus, recycle.status)
    if runtime_status == "success":  # pragma: no cover - guarded by caller
        raise AssertionError("successful recycle is not a prerequisite failure")
    last_valid = None if recycle.flowsheet is None else _json_mapping(recycle.flowsheet.as_dict())
    return adapt_exception(
        request,
        RuntimeError(recycle.failure_reason or "M2 prerequisite failed"),
        runtime_status=runtime_status,
        stage=recycle.failure_stage or "M2_prerequisite",
        versions={**_common_versions(bundle), "simulation_stage": "M6"},
        source_fingerprints=_source_fingerprints(bundle),
        effective_input_fingerprint=effective_input_fingerprint,
        last_valid=last_valid,
        duration_s=preset.duration_s,
        time_step_s=preset.time_step_s,
        engine_status=recycle.status,
        raw_result_type=type(recycle).__name__,
        summary=_json_mapping(
            {
                "prerequisite": recycle.as_dict(),
                "message": recycle.failure_reason or "M2 prerequisite failed",
            }
        ),
    )


def _execute_m6_pump_trip(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
) -> ExecutionPayload:
    config = bundle.validation_config
    spec = _validation_scenario(config, _M6_PUMP_SCENARIO_ID)
    rule = _protection_rule(config, _M6_PUMP_RULE_ID)
    domain = assess_applicability(
        config.domain_dimensions,
        spec.inputs,
        abnormal_verification=spec.abnormal_verification,
    )
    if domain.status != "limited":
        raise ValueError("packaged pump-trip scenario is not in the limited domain")
    effective = _effective_fingerprint(
        bundle,
        request,
        extra={
            "control": bundle.control.as_dict(),
            "m6_config": config.as_dict(),
            "scenario": spec.as_dict(),
            "protection_rule": rule.as_dict(),
            "execution_profile": "portable_selected_scenario_replay",
        },
    )
    recycle = solve_recycle(
        bundle.effective_model,
        bundle.effective_case,
        bundle.catalog,
    )
    if not recycle.converged:
        return _m6_prerequisite_failure(
            request,
            preset,
            bundle,
            recycle,
            effective_input_fingerprint=effective,
        )
    dynamic_model = initialize_open_loop_dynamic_model(
        bundle.effective_model,
        bundle.effective_case,
        bundle.catalog,
        recycle,
    )
    factor_id = spec.execution_reference.removeprefix("m3.")
    target, value = dynamic_command_for_factor(
        cast(MappingProxyType[str, float], dynamic_model.baseline_commands),
        factor_id,
        spec.inputs[factor_id],
    )
    baseline_fingerprint = canonical_fingerprint(
        {
            "effective_input": effective,
            "scenario": spec.as_dict(),
            "variant": "baseline",
            "duration_s": _M6_DURATION_S,
            "time_step_s": _M6_TIME_STEP_S,
        }
    )
    candidate_fingerprint = canonical_fingerprint(
        {
            "effective_input": effective,
            "scenario": spec.as_dict(),
            "target": target,
            "value": value,
            "event_time_s": _M6_EVENT_TIME_S,
            "duration_s": _M6_DURATION_S,
            "time_step_s": _M6_TIME_STEP_S,
        }
    )
    baseline = _simulate_m6_dynamic(
        dynamic_model,
        spec=spec,
        event=None,
        source_fingerprint=baseline_fingerprint,
        scenario_suffix="baseline",
    )
    if baseline.status != "success":
        failed = _failed_m6_scenario(
            spec,
            config,
            stage=baseline.failure_stage or "M3_baseline",
            reason=baseline.failure_reason or "portable M6 baseline failed",
            input_fingerprint=candidate_fingerprint,
        )
        return adapt_validation_scenario(
            request,
            failed,
            timeseries=(sample.as_dict() for sample in baseline.samples),
            sample_count=len(baseline.samples),
            completed_time_s=baseline.completed_time_s,
            versions=_m6_versions(bundle, spec),
            source_fingerprints={
                **_source_fingerprints(bundle),
                "m6_portable_baseline": baseline.source_fingerprint,
            },
            effective_input_fingerprint=effective,
            formal_m6_result_fingerprint=bundle.m6_result_fingerprint,
            duration_s=_M6_DURATION_S,
            time_step_s=_M6_TIME_STEP_S,
            failure_time_s=baseline.failure_time_s,
            last_valid=(
                None if not baseline.samples else _json_mapping(baseline.samples[-1].as_dict())
            ),
        )
    candidate = _simulate_m6_dynamic(
        dynamic_model,
        spec=spec,
        event=CommandEvent(_M6_EVENT_TIME_S, target, value),
        source_fingerprint=candidate_fingerprint,
        scenario_suffix="candidate",
    )
    command_event = validation_command_event(
        time_s=_M6_EVENT_TIME_S,
        target=target,
        value=value,
    )
    if candidate.status != "success":
        failed = _failed_m6_scenario(
            spec,
            config,
            stage=candidate.failure_stage or "M3_candidate",
            reason=candidate.failure_reason or "portable M6 candidate failed",
            input_fingerprint=candidate_fingerprint,
        )
        return adapt_validation_scenario(
            request,
            failed,
            timeseries=(sample.as_dict() for sample in candidate.samples),
            sample_count=len(candidate.samples),
            completed_time_s=candidate.completed_time_s,
            configured_events=(command_event,),
            versions=_m6_versions(bundle, spec),
            source_fingerprints={
                **_source_fingerprints(bundle),
                "m6_portable_baseline": baseline.source_fingerprint,
                "m6_portable_candidate": candidate.source_fingerprint,
            },
            effective_input_fingerprint=effective,
            formal_m6_result_fingerprint=bundle.m6_result_fingerprint,
            duration_s=_M6_DURATION_S,
            time_step_s=_M6_TIME_STEP_S,
            failure_time_s=candidate.failure_time_s,
            last_valid=(
                None if not candidate.samples else _json_mapping(candidate.samples[-1].as_dict())
            ),
        )

    baseline_metrics = dynamic_output_metrics(baseline)
    candidate_metrics = dict(dynamic_output_metrics(candidate))
    health_ratio = spec.inputs["pump_around_1_health_ratio"]
    trace = _m6_pump_trace(rule, candidate, health_ratio=health_ratio)
    trigger_events = tuple(event for event in trace.events if event.event_kind == "triggered")
    first_trigger_time = 0.0 if not trigger_events else trigger_events[0].time_s
    timeline_passed = bool(trigger_events) and first_trigger_time >= _M6_EVENT_TIME_S
    tracking, tracking_passed = _tracking_evidence(
        rule,
        bundle.control,
        dynamic_model,
        relative_tolerance=config.report_acceptance.controller_tracking_relative_tolerance,
    )
    candidate_metrics.update(
        {
            "fault_event_time_s": _M6_EVENT_TIME_S,
            "protection_event_count": float(len(trace.events)),
            "protection_first_trigger_time_s": first_trigger_time,
            "protection_triggered": 1.0 if trigger_events else 0.0,
            "tracking_loop_count": float(len(tracking)),
            "tracking_max_final_error": max(
                (item.final_tracking_error for item in tracking),
                default=0.0,
            ),
            "tracking_max_return_jump": max(
                (item.automatic_return_jump for item in tracking),
                default=0.0,
            ),
            "tracking_no_bump": 1.0 if tracking_passed else 0.0,
        }
    )
    directions = evaluate_metric_directions(
        baseline_metrics,
        candidate_metrics,
        spec.expected_directions,
        absolute_tolerance=config.report_acceptance.direction_absolute_tolerance,
    )
    conservation = {
        "cumulative_conservation": candidate.status == "success",
        "engine_success": candidate.status == "success",
        "instantaneous_conservation": candidate.status == "success",
        "protection_timeline": timeline_passed,
        "tracking_no_bump": tracking_passed,
    }
    checks_passed = all(directions.values()) and all(conservation.values())
    result = ScenarioValidationResult(
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
        metrics=candidate_metrics,
        direction_checks=directions,
        conservation_checks=conservation,
        protection_trace=trace,
        source_origins=("M3_open_loop_simulation", "M6_synthetic_validation"),
        engine_status="success" if checks_passed else "failed",
        input_fingerprint=candidate_fingerprint,
        failure_stage=None if checks_passed else "direction_or_conservation",
        failure_reason=(None if checks_passed else "portable M6 pump-trip evidence failed"),
    )
    return adapt_validation_scenario(
        request,
        result,
        timeseries=(sample.as_dict() for sample in candidate.samples),
        sample_count=len(candidate.samples),
        completed_time_s=candidate.completed_time_s,
        configured_events=(command_event,),
        versions=_m6_versions(bundle, spec),
        source_fingerprints={
            **_source_fingerprints(bundle),
            "m6_portable_baseline": baseline.source_fingerprint,
            "m6_portable_candidate": candidate.source_fingerprint,
        },
        effective_input_fingerprint=effective,
        formal_m6_result_fingerprint=bundle.m6_result_fingerprint,
        duration_s=_M6_DURATION_S,
        time_step_s=_M6_TIME_STEP_S,
        extra_diagnostics={
            "baseline_source_fingerprint": baseline.source_fingerprint,
            "candidate_source_fingerprint": candidate.source_fingerprint,
            "tracking": tuple(item.as_dict() for item in tracking),
        },
    )


def _execute_validation(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
) -> ExecutionPayload:
    if preset.scenario_id == _M6_PUMP_SCENARIO_ID:
        return _execute_m6_pump_trip(request, preset, bundle)
    if preset.scenario_id == _M6_REJECTION_SCENARIO_ID:
        return _execute_m6_structural_rejection(request, bundle)
    raise ValueError(f"unsupported portable M6 scenario {preset.scenario_id!r}")


def _fallback_grid(request: RunRequest) -> tuple[float | None, float | None]:
    if request.run_type in {"open_loop_dynamic", "closed_loop_dynamic"}:
        return 7200.0, 1.0
    return None, None


def _rejected_request(
    request: RunRequest,
    exception: Exception,
    *,
    preset: RuntimePreset | None,
) -> ExecutionPayload:
    duration_s, time_step_s = (
        _fallback_grid(request)
        if preset is None or preset.run_type != request.run_type
        else (preset.duration_s, preset.time_step_s)
    )
    return adapt_exception(
        request,
        exception,
        runtime_status="rejected",
        stage="request_preflight",
        duration_s=duration_s,
        time_step_s=time_step_s,
    )


def _exception_status(exception: Exception) -> RuntimeStatus:
    message = str(exception).lower()
    if isinstance(exception, RuntimeError) and (
        "prerequisite failed" in message
        or "did not converge" in message
        or "not converged" in message
    ):
        return "not_converged"
    return "failed"


def execute(request: RunRequest) -> ExecutionPayload:
    """Execute one strict request without creating directories or wall-clock data."""

    if not isinstance(request, RunRequest):
        raise TypeError("execute requires a RunRequest")
    try:
        preset = get_preset(request.preset_id)
    except (KeyError, TypeError) as exc:
        return _rejected_request(request, exc, preset=None)
    if request.run_type != preset.run_type:
        return _rejected_request(
            request,
            ValueError(
                f"request run_type {request.run_type!r} differs from preset {preset.run_type!r}"
            ),
            preset=preset,
        )
    try:
        validate_runtime_request_shape(request)
    except (KeyError, TypeError, ValueError) as exc:
        return _rejected_request(request, exc, preset=preset)
    try:
        bundle = load_runtime_resource_bundle()
    except Exception as exc:  # noqa: BLE001 - resource failure is outcome evidence
        return adapt_exception(
            request,
            exc,
            runtime_status="failed",
            stage="resource_loading",
            duration_s=preset.duration_s,
            time_step_s=preset.time_step_s,
        )
    try:
        resolved = resolve_runtime_inputs(request, bundle=bundle)
    except (KeyError, TypeError, ValueError) as exc:
        duration_s = (
            preset.duration_s
            if request.scenario is None or request.scenario.duration_s is None
            else request.scenario.duration_s
        )
        time_step_s = (
            preset.time_step_s
            if request.scenario is None or request.scenario.time_step_s is None
            else request.scenario.time_step_s
        )
        return adapt_exception(
            request,
            exc,
            runtime_status="rejected",
            stage="request_preflight",
            source_fingerprints=dict(bundle.resource_fingerprints),
            duration_s=duration_s,
            time_step_s=time_step_s,
        )
    try:
        if resolved.is_custom:
            if preset.engine_layer == "M2":
                return _execute_custom_steady(request, bundle, resolved)
            if preset.engine_layer == "M3":
                return _execute_custom_open_loop(request, bundle, resolved)
            if preset.engine_layer == "M4":
                return _execute_custom_closed_loop(request, bundle, resolved)
            raise ValueError("portable M6 presets do not accept custom inputs")
        if preset.engine_layer == "M2":
            return _execute_steady(request, bundle)
        if preset.engine_layer == "M3":
            return _execute_open_loop(request, preset, bundle)
        if preset.engine_layer == "M4":
            return _execute_closed_loop(request, preset, bundle)
        return _execute_validation(request, preset, bundle)
    except Exception as exc:  # noqa: BLE001 - model failure is normalized evidence
        sources = (
            _resolved_source_fingerprints(bundle, resolved)
            if resolved.is_custom
            else _source_fingerprints(bundle)
        )
        effective = (
            resolved.execution_input_fingerprint
            if resolved.is_custom
            else _effective_fingerprint(bundle, request)
        )
        return adapt_exception(
            request,
            exc,
            runtime_status=_exception_status(exc),
            stage="model_execution",
            versions=_common_versions(bundle),
            source_fingerprints=sources,
            effective_input_fingerprint=effective,
            duration_s=resolved.duration_s,
            time_step_s=resolved.time_step_s,
        )


execute_request = execute

__all__ = ["execute", "execute_request"]
