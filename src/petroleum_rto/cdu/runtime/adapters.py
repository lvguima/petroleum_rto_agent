"""Pure adapters from accepted M2/M3/M4/M6 results to the M7 contract."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from ..control.results import ClosedLoopSimulationResult
from ..control.scenario import ClosedLoopScenarioConfig
from ..core.config import ScenarioConfig
from ..dynamics.simulation import DynamicSimulationResult
from ..flowsheet.recycle import RecycleSolveResult
from ..validation.protection import ProtectionTrace
from ..validation.results import ScenarioValidationResult
from .contracts import (
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
    ErrorRecord,
    EventRecord,
    ExecutionPayload,
    JsonValue,
    RunRequest,
    RuntimeStatus,
)

_TOKEN_PARTS = re.compile(r"[^A-Za-z0-9._-]+")


def _json_mapping(value: Mapping[str, object]) -> Mapping[str, JsonValue]:
    """Narrow after the runtime contract performs the authoritative JSON check."""

    return cast(Mapping[str, JsonValue], value)


def _json_timeseries(
    values: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, JsonValue], ...]:
    return cast(
        tuple[Mapping[str, JsonValue], ...],
        (_json_mapping(value) for value in values),
    )


def _token(value: str | None, *, fallback: str) -> str:
    if value is None or not value.strip():
        return fallback
    candidate = _TOKEN_PARTS.sub("_", value.strip()).strip("._-")
    if not candidate or ".." in candidate:
        return fallback
    return candidate


def _versions(
    engine_versions: Mapping[str, str],
    supplied: Mapping[str, str],
) -> dict[str, str]:
    merged = dict(supplied)
    merged.update(engine_versions)
    return {name: merged[name] for name in sorted(merged)}


def _fingerprints(
    supplied: Mapping[str, str],
    **extra: str,
) -> dict[str, str]:
    merged = dict(supplied)
    merged.update(extra)
    return {name: merged[name] for name in sorted(merged)}


def _failure_event(
    sequence: int,
    *,
    stage: str,
    reason: str,
    time_s: float | None,
    error_type: str,
) -> EventRecord:
    return EventRecord(
        sequence=sequence,
        time_s=time_s,
        event_type="execution_failure",
        source="M7_runtime",
        stage=_token(stage, fallback="execution"),
        message=reason,
        details={"error_type": error_type},
    )


def _error_record(
    *,
    error_type: str,
    stage: str,
    reason: str,
    time_s: float | None,
    last_valid: Mapping[str, JsonValue] | None,
    retryable: bool,
    details: Mapping[str, JsonValue] | None = None,
) -> ErrorRecord:
    return ErrorRecord(
        sequence=0,
        error_type=_token(error_type, fallback="RuntimeFailure"),
        stage=_token(stage, fallback="execution"),
        message=reason,
        time_s=time_s,
        last_valid=last_valid,
        retryable=retryable,
        details={} if details is None else details,
    )


def open_loop_event_records(scenario: ScenarioConfig) -> tuple[EventRecord, ...]:
    """Convert versioned M3 command events without changing their values."""

    records: list[EventRecord] = []
    for sequence, event in enumerate(scenario.events):
        target = str(event["target"])
        records.append(
            EventRecord(
                sequence=sequence,
                time_s=float(cast(float, event["time_s"])),
                event_type="command_change",
                source="M3_scenario",
                stage="M3_open_loop",
                message=f"Apply configured command change to {target}.",
                details=_json_mapping(dict(event)),
            )
        )
    return tuple(records)


def closed_loop_event_records(
    scenario: ClosedLoopScenarioConfig,
) -> tuple[EventRecord, ...]:
    """Convert versioned M4 setpoint events without changing their values."""

    return tuple(
        EventRecord(
            sequence=sequence,
            time_s=event.time_s,
            event_type="setpoint_change",
            source="M4_scenario",
            stage="M4_closed_loop",
            message=f"Apply configured setpoint change to {event.loop_id}.",
            details=_json_mapping(event.as_dict()),
        )
        for sequence, event in enumerate(scenario.events)
    )


def _events_completed_by(
    records: Sequence[EventRecord],
    completed_time_s: float,
) -> tuple[tuple[EventRecord, ...], tuple[Mapping[str, object], ...]]:
    """Separate executed events from future configuration without time travel."""

    tolerance = 1.0e-12 * max(abs(completed_time_s), 1.0)
    executed: list[EventRecord] = []
    unexecuted: list[Mapping[str, object]] = []
    for record in records:
        if record.time_s is not None and record.time_s > completed_time_s + tolerance:
            unexecuted.append(record.as_dict())
            continue
        executed.append(
            EventRecord(
                sequence=len(executed),
                time_s=record.time_s,
                event_type=record.event_type,
                source=record.source,
                stage=record.stage,
                message=record.message,
                details=record.details,
            )
        )
    return tuple(executed), tuple(unexecuted)


def validation_command_event(
    *,
    time_s: float,
    target: str,
    value: float,
) -> EventRecord:
    """Create the portable M6 scenario's actual M3 command event."""

    return EventRecord(
        sequence=0,
        time_s=time_s,
        event_type="fault_command",
        source="M6_scenario",
        stage="M6_portable",
        message=f"Apply packaged abnormal command to {target}.",
        details={"target": target, "value": value},
    )


def protection_event_records(
    trace: ProtectionTrace,
    *,
    start_sequence: int = 0,
) -> tuple[EventRecord, ...]:
    """Normalize every M6 protection transition into the common event contract."""

    return tuple(
        EventRecord(
            sequence=start_sequence + offset,
            time_s=event.time_s,
            event_type=event.event_kind,
            source="M6_protection",
            stage="M6_portable",
            message=event.reason,
            details=_json_mapping(event.as_dict()),
        )
        for offset, event in enumerate(trace.events)
    )


def adapt_recycle_result(
    request: RunRequest,
    result: RecycleSolveResult,
    *,
    versions: Mapping[str, str],
    source_fingerprints: Mapping[str, str],
    effective_input_fingerprint: str,
) -> ExecutionPayload:
    """Preserve all four M2 terminal states under the five-state M7 vocabulary."""

    runtime_status = cast(RuntimeStatus, result.status)
    failed = runtime_status != "success"
    stage = _token(result.failure_stage, fallback="M2_recycle")
    reason = result.failure_reason or "M2 recycle execution failed"
    last_valid = (
        None
        if not failed or result.flowsheet is None
        else _json_mapping(result.flowsheet.as_dict())
    )
    errors = (
        (
            _error_record(
                error_type={
                    "not_converged": "NotConverged",
                    "rejected": "RejectedInput",
                    "failed": "EngineFailure",
                }[runtime_status],
                stage=stage,
                reason=reason,
                time_s=None,
                last_valid=last_valid,
                retryable=runtime_status != "rejected",
                details={"iterations": result.iterations},
            ),
        )
        if failed
        else ()
    )
    events = (
        (
            _failure_event(
                0,
                stage=stage,
                reason=reason,
                time_s=None,
                error_type=errors[0].error_type,
            ),
        )
        if errors
        else ()
    )
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status=runtime_status,
        request_fingerprint=request.request_fingerprint,
        engine_status=result.status,
        raw_result_type=type(result).__name__,
        summary=_json_mapping(result.as_dict()),
        timeseries=(),
        events=events,
        errors=errors,
        versions=_versions({}, versions),
        source_fingerprints=source_fingerprints,
        effective_input_fingerprint=effective_input_fingerprint,
        synthetic=True,
        data_origin="M2_steady_model_prediction",
        claim_scope="engineering_simulation_only",
        failure_stage=stage if failed else None,
        failure_reason=reason if failed else None,
        failure_time_s=None,
        last_valid=last_valid,
        duration_s=None,
        time_step_s=None,
        diagnostics={
            "converged": result.converged,
            "iterations": result.iterations,
            "final_residual": result.final_residual,
        },
    )


def adapt_dynamic_result(
    request: RunRequest,
    scenario: ScenarioConfig,
    result: DynamicSimulationResult,
    *,
    versions: Mapping[str, str],
    source_fingerprints: Mapping[str, str],
    effective_input_fingerprint: str,
) -> ExecutionPayload:
    """Adapt M3 while keeping samples separately iterable for JSONL writing."""

    runtime_status: RuntimeStatus = "success" if result.status == "success" else "failed"
    summary: dict[str, object] = {
        "status": result.status,
        "balance": result.balance.as_dict(),
        "conservation_tolerances": result.conservation_tolerances.as_dict(),
        "diagnostics": dict(result.diagnostics),
        "versions": dict(result.versions),
        "metadata": dict(result.metadata),
        "source_fingerprint": result.source_fingerprint,
        "input_fingerprint": result.input_fingerprint,
        "requested_duration_s": result.requested_duration_s,
        "time_step_s": result.time_step_s,
        "completed_time_s": result.completed_time_s,
        "failure_reason": result.failure_reason,
        "failure_stage": result.failure_stage,
        "failure_time_s": result.failure_time_s,
    }
    configured_events = open_loop_event_records(scenario)
    executed_events, unexecuted_events = _events_completed_by(
        configured_events,
        result.completed_time_s,
    )
    events = list(executed_events)
    last_valid = (
        None
        if runtime_status == "success" or not result.samples
        else _json_mapping(result.samples[-1].as_dict())
    )
    errors: tuple[ErrorRecord, ...] = ()
    if runtime_status == "failed":
        stage = _token(result.failure_stage, fallback="M3_simulation")
        reason = result.failure_reason or "M3 dynamic execution failed"
        error = _error_record(
            error_type="DynamicSimulationFailure",
            stage=stage,
            reason=reason,
            time_s=result.failure_time_s,
            last_valid=last_valid,
            retryable=True,
        )
        errors = (error,)
        events.append(
            _failure_event(
                len(events),
                stage=stage,
                reason=reason,
                time_s=result.failure_time_s,
                error_type=error.error_type,
            )
        )
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status=runtime_status,
        request_fingerprint=request.request_fingerprint,
        engine_status=result.status,
        raw_result_type=type(result).__name__,
        summary=_json_mapping(summary),
        timeseries=_json_timeseries(sample.as_dict() for sample in result.samples),
        events=tuple(events),
        errors=errors,
        versions=_versions(result.versions, versions),
        source_fingerprints=_fingerprints(
            source_fingerprints,
            engine_source=result.source_fingerprint,
        ),
        effective_input_fingerprint=effective_input_fingerprint,
        synthetic=True,
        data_origin=result.metadata["data_origin"],
        claim_scope="engineering_simulation_only",
        failure_stage=(
            _token(result.failure_stage, fallback="M3_simulation")
            if runtime_status == "failed"
            else None
        ),
        failure_reason=(
            result.failure_reason or "M3 dynamic execution failed"
            if runtime_status == "failed"
            else None
        ),
        failure_time_s=result.failure_time_s,
        last_valid=last_valid,
        duration_s=result.requested_duration_s,
        time_step_s=result.time_step_s,
        diagnostics=_json_mapping(
            {
                **dict(result.diagnostics),
                "completed_time_s": result.completed_time_s,
                "configured_event_count": len(configured_events),
                "executed_configured_event_count": len(executed_events),
                "sample_count": len(result.samples),
                "unexecuted_configured_events": unexecuted_events,
            }
        ),
    )


def adapt_closed_loop_result(
    request: RunRequest,
    scenario: ClosedLoopScenarioConfig,
    result: ClosedLoopSimulationResult,
    *,
    versions: Mapping[str, str],
    source_fingerprints: Mapping[str, str],
    effective_input_fingerprint: str,
) -> ExecutionPayload:
    """Adapt M4 without weakening its seven-loop success contract."""

    runtime_status: RuntimeStatus = "success" if result.status == "success" else "failed"
    summary = {
        "status": result.status,
        "balance": result.balance.as_dict(),
        "conservation_tolerances": result.conservation_tolerances.as_dict(),
        "loop_performance": {
            loop_id: value.as_dict() for loop_id, value in result.loop_performance.items()
        },
        "acceptance_checks": dict(result.acceptance_checks),
        "acceptance_passed": result.acceptance_passed,
        "diagnostics": dict(result.diagnostics),
        "versions": dict(result.versions),
        "metadata": dict(result.metadata),
        "source_fingerprint": result.source_fingerprint,
        "control_fingerprint": result.control_fingerprint,
        "input_fingerprint": result.input_fingerprint,
        "requested_duration_s": result.requested_duration_s,
        "time_step_s": result.time_step_s,
        "control_interval_s": result.control_interval_s,
        "completed_time_s": result.completed_time_s,
        "failure_reason": result.failure_reason,
        "failure_stage": result.failure_stage,
        "failure_time_s": result.failure_time_s,
    }
    configured_events = closed_loop_event_records(scenario)
    executed_events, unexecuted_events = _events_completed_by(
        configured_events,
        result.completed_time_s,
    )
    events = list(executed_events)
    last_valid = (
        None
        if runtime_status == "success" or not result.samples
        else _json_mapping(result.samples[-1].as_dict())
    )
    errors: tuple[ErrorRecord, ...] = ()
    if runtime_status == "failed":
        stage = _token(result.failure_stage, fallback="M4_simulation")
        reason = result.failure_reason or "M4 closed-loop execution failed"
        error = _error_record(
            error_type="ClosedLoopSimulationFailure",
            stage=stage,
            reason=reason,
            time_s=result.failure_time_s,
            last_valid=last_valid,
            retryable=True,
        )
        errors = (error,)
        events.append(
            _failure_event(
                len(events),
                stage=stage,
                reason=reason,
                time_s=result.failure_time_s,
                error_type=error.error_type,
            )
        )
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status=runtime_status,
        request_fingerprint=request.request_fingerprint,
        engine_status=result.status,
        raw_result_type=type(result).__name__,
        summary=_json_mapping(summary),
        timeseries=_json_timeseries(sample.as_dict() for sample in result.samples),
        events=tuple(events),
        errors=errors,
        versions=_versions(result.versions, versions),
        source_fingerprints=_fingerprints(
            source_fingerprints,
            engine_source=result.source_fingerprint,
            control_input=result.control_fingerprint,
        ),
        effective_input_fingerprint=effective_input_fingerprint,
        synthetic=True,
        data_origin=result.metadata["data_origin"],
        claim_scope="engineering_simulation_only",
        failure_stage=(
            _token(result.failure_stage, fallback="M4_simulation")
            if runtime_status == "failed"
            else None
        ),
        failure_reason=(
            result.failure_reason or "M4 closed-loop execution failed"
            if runtime_status == "failed"
            else None
        ),
        failure_time_s=result.failure_time_s,
        last_valid=last_valid,
        duration_s=result.requested_duration_s,
        time_step_s=result.time_step_s,
        diagnostics=_json_mapping(
            {
                **dict(result.diagnostics),
                "acceptance_passed": result.acceptance_passed,
                "completed_time_s": result.completed_time_s,
                "configured_event_count": len(configured_events),
                "executed_configured_event_count": len(executed_events),
                "sample_count": len(result.samples),
                "unexecuted_configured_events": unexecuted_events,
            }
        ),
    )


def adapt_validation_scenario(
    request: RunRequest,
    result: ScenarioValidationResult,
    *,
    timeseries: Iterable[Mapping[str, object]] = (),
    sample_count: int | None = None,
    completed_time_s: float | None = None,
    configured_events: tuple[EventRecord, ...] = (),
    versions: Mapping[str, str],
    source_fingerprints: Mapping[str, str],
    effective_input_fingerprint: str,
    formal_m6_result_fingerprint: str,
    duration_s: float | None,
    time_step_s: float | None,
    extra_diagnostics: Mapping[str, object] | None = None,
    failure_time_s: float | None = None,
    last_valid: Mapping[str, JsonValue] | None = None,
) -> ExecutionPayload:
    """Adapt one packaged M6 scenario without claiming a fresh full-M6 run."""

    runtime_status: RuntimeStatus = (
        "success" if result.scenario_status == "passed" else result.scenario_status
    )
    if sample_count is None:
        if not isinstance(timeseries, Sequence):
            raise TypeError("lazy validation timeseries requires sample_count")
        resolved_sample_count = len(timeseries)
    else:
        resolved_sample_count = sample_count
    if resolved_sample_count < 0:
        raise ValueError("sample_count must be non-negative")
    summary = result.as_dict()
    summary.update(
        {
            "execution_profile": "portable_selected_scenario_replay",
            "formal_m6_result_fingerprint": formal_m6_result_fingerprint,
        }
    )
    timeline_events = list(configured_events)
    if result.protection_trace is not None:
        timeline_events.extend(
            protection_event_records(
                result.protection_trace,
                start_sequence=len(timeline_events),
            )
        )
    if runtime_status == "failed":
        event_horizon_s = (
            (0.0 if completed_time_s is None else completed_time_s)
            if failure_time_s is None
            else failure_time_s
        )
    elif runtime_status == "rejected":
        event_horizon_s = 0.0
    else:
        event_horizon_s = (
            (0.0 if completed_time_s is None else completed_time_s)
            if duration_s is None
            else duration_s
        )
    executed_events, unexecuted_events = _events_completed_by(
        timeline_events,
        event_horizon_s,
    )
    events = list(executed_events)
    failure_stage: str | None = None
    failure_reason: str | None = None
    errors: tuple[ErrorRecord, ...] = ()
    if runtime_status == "rejected":
        failure_stage = "applicability_preflight"
        failure_reason = "; ".join(result.domain.reasons) or "M6 input was rejected"
        error = _error_record(
            error_type="StructuralRejection",
            stage=failure_stage,
            reason=failure_reason,
            time_s=None,
            last_valid=None,
            retryable=False,
            details={"solver_called": result.solver_called},
        )
        errors = (error,)
        events.append(
            EventRecord(
                sequence=len(events),
                time_s=None,
                event_type="structural_rejection",
                source="M6_applicability",
                stage=failure_stage,
                message=failure_reason,
                details={"reasons": tuple(result.domain.reasons)},
            )
        )
    elif runtime_status == "failed":
        failure_stage = _token(result.failure_stage, fallback="M6_scenario")
        failure_reason = result.failure_reason or "M6 selected scenario failed"
        error = _error_record(
            error_type="ValidationScenarioFailure",
            stage=failure_stage,
            reason=failure_reason,
            time_s=failure_time_s,
            last_valid=last_valid,
            retryable=True,
        )
        errors = (error,)
        events.append(
            _failure_event(
                len(events),
                stage=failure_stage,
                reason=failure_reason,
                time_s=failure_time_s,
                error_type=error.error_type,
            )
        )
    diagnostics: dict[str, object] = {
        "domain_status": result.domain.status,
        "configured_event_count": len(configured_events),
        "executed_timeline_event_count": len(executed_events),
        "execution_profile": "portable_selected_scenario_replay",
        "sample_count": resolved_sample_count,
        "solver_called": result.solver_called,
        "unexecuted_timeline_events": unexecuted_events,
        "verification_outcome": result.verification_outcome,
    }
    if extra_diagnostics is not None:
        diagnostics.update(extra_diagnostics)
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status=runtime_status,
        request_fingerprint=request.request_fingerprint,
        engine_status=result.engine_status or "not_called",
        raw_result_type=type(result).__name__,
        summary=_json_mapping(summary),
        timeseries=_json_timeseries(timeseries),
        events=tuple(events),
        errors=errors,
        versions=_versions({}, versions),
        source_fingerprints=_fingerprints(
            source_fingerprints,
            formal_m6_result=formal_m6_result_fingerprint,
        ),
        effective_input_fingerprint=effective_input_fingerprint,
        synthetic=True,
        data_origin="M6_synthetic_validation",
        claim_scope="engineering_validation_only",
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        failure_time_s=failure_time_s if runtime_status == "failed" else None,
        last_valid=last_valid if runtime_status == "failed" else None,
        duration_s=duration_s,
        time_step_s=time_step_s,
        diagnostics=_json_mapping(diagnostics),
    )


def adapt_exception(
    request: RunRequest,
    exception: Exception,
    *,
    runtime_status: RuntimeStatus,
    stage: str,
    versions: Mapping[str, str] | None = None,
    source_fingerprints: Mapping[str, str] | None = None,
    effective_input_fingerprint: str | None = None,
    prior_events: tuple[EventRecord, ...] = (),
    last_valid: Mapping[str, JsonValue] | None = None,
    failure_time_s: float | None = None,
    duration_s: float | None = None,
    time_step_s: float | None = None,
    engine_status: str = "exception",
    raw_result_type: str | None = None,
    summary: Mapping[str, JsonValue] | None = None,
) -> ExecutionPayload:
    """Turn a caught execution exception into deterministic failure evidence."""

    if runtime_status in {"success", "limited"}:
        raise ValueError("an exception cannot be adapted as a non-failure status")
    error_type = _token(type(exception).__name__, fallback="RuntimeException")
    safe_stage = _token(stage, fallback="execution")
    reason = str(exception).strip() or f"{error_type} raised without a message"
    error = _error_record(
        error_type=error_type,
        stage=safe_stage,
        reason=reason,
        time_s=failure_time_s,
        last_valid=last_valid,
        retryable=runtime_status != "rejected",
    )
    events = (
        *prior_events,
        _failure_event(
            len(prior_events),
            stage=safe_stage,
            reason=reason,
            time_s=failure_time_s,
            error_type=error.error_type,
        ),
    )
    origins = {
        "steady_recycle": "M2_steady_model_prediction",
        "open_loop_dynamic": "M3_open_loop_simulation",
        "closed_loop_dynamic": "M4_closed_loop_simulation",
        "validation_scenario": "M6_synthetic_validation",
    }
    claim_scopes = {
        "steady_recycle": "engineering_simulation_only",
        "open_loop_dynamic": "engineering_simulation_only",
        "closed_loop_dynamic": "engineering_simulation_only",
        "validation_scenario": "engineering_validation_only",
    }
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status=runtime_status,
        request_fingerprint=request.request_fingerprint,
        engine_status=engine_status,
        raw_result_type=error_type if raw_result_type is None else raw_result_type,
        summary=({"exception_type": error_type, "message": reason} if summary is None else summary),
        timeseries=(),
        events=events,
        errors=(error,),
        versions={} if versions is None else versions,
        source_fingerprints=({} if source_fingerprints is None else source_fingerprints),
        effective_input_fingerprint=(
            request.request_fingerprint
            if effective_input_fingerprint is None
            else effective_input_fingerprint
        ),
        synthetic=True,
        data_origin=origins[request.run_type],
        claim_scope=claim_scopes[request.run_type],
        failure_stage=safe_stage,
        failure_reason=reason,
        failure_time_s=failure_time_s,
        last_valid=last_valid,
        duration_s=duration_s,
        time_step_s=time_step_s,
        diagnostics={"exception_normalized": True},
    )


__all__ = [
    "adapt_closed_loop_result",
    "adapt_dynamic_result",
    "adapt_exception",
    "adapt_recycle_result",
    "adapt_validation_scenario",
    "closed_loop_event_records",
    "open_loop_event_records",
    "protection_event_records",
    "validation_command_event",
]
