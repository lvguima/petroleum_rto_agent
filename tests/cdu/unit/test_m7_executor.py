from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

import pytest

from petroleum_rto.cdu.control.results import ClosedLoopSimulationResult
from petroleum_rto.cdu.control.scenario import SetpointEvent
from petroleum_rto.cdu.core.math_utils import ConvergenceError
from petroleum_rto.cdu.dynamics.simulation import DynamicSimulationResult
from petroleum_rto.cdu.flowsheet.recycle import RecycleSolveResult
from petroleum_rto.cdu.runtime import executor
from petroleum_rto.cdu.runtime.adapters import (
    _events_completed_by,
    adapt_validation_scenario,
    validation_command_event,
)
from petroleum_rto.cdu.runtime.contracts import EventRecord, RunRequest
from petroleum_rto.cdu.runtime.executor import execute
from petroleum_rto.cdu.runtime.presets import load_preset
from petroleum_rto.cdu.runtime.resources import load_runtime_resource_bundle


def test_steady_execution_uses_m5_effective_basis_and_is_deterministic() -> None:
    request = load_preset("steady-baseline")
    first = execute(request)
    second = execute(request)

    assert first.runtime_status == "success"
    assert first.engine_status == "success"
    assert first.result_fingerprint == second.result_fingerprint
    assert first.summary == second.summary
    assert not first.timeseries
    assert first.versions["derived_parameter_set_version"].startswith("cdu-parameters-m5-")
    assert "overlay.m5" in first.source_fingerprints
    assert "m6_formal_result" not in first.source_fingerprints
    assert "validation.m6" not in first.source_fingerprints


def test_rejected_requests_never_load_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_load(*args: object) -> None:
        raise AssertionError("resource loader must not run")

    monkeypatch.setattr(executor, "load_runtime_resource_bundle", unexpected_load)
    unknown = replace(load_preset("steady-baseline"), preset_id="unknown")
    mismatch = replace(
        load_preset("steady-baseline"),
        run_type="open_loop_dynamic",
    )
    overridden = replace(load_preset("steady-baseline"), overrides={"x": 1.0})

    for request in (unknown, mismatch, overridden):
        outcome = execute(request)
        assert outcome.runtime_status == "rejected"
        assert outcome.failure_stage == "request_preflight"
        assert len(outcome.errors) == 1
        assert outcome.events[-1].event_type == "execution_failure"


def test_m2_not_converged_status_and_last_valid_fields_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = RecycleSolveResult(
        status="not_converged",
        flowsheet=None,
        iterations=3,
        final_residual=0.1,
        residual_history=(0.3, 0.2, 0.1),
        reflux=None,
        failure_reason="fixed point did not converge",
        failure_stage="recycle_iteration",
    )
    monkeypatch.setattr(executor, "solve_recycle", lambda *args, **kwargs: result)

    outcome = execute(load_preset("steady-baseline"))

    assert outcome.runtime_status == "not_converged"
    assert outcome.engine_status == "not_converged"
    assert outcome.failure_stage == "recycle_iteration"
    assert outcome.failure_reason == "fixed point did not converge"
    assert outcome.last_valid is None
    assert outcome.errors[0].retryable


def test_structured_convergence_error_maps_to_not_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_with_status(*args: object, **kwargs: object) -> None:
        raise ConvergenceError("structured model convergence failure")

    monkeypatch.setattr(executor, "_execute_steady", fail_with_status)

    outcome = execute(load_preset("steady-baseline"))

    assert outcome.runtime_status == "not_converged"
    assert outcome.failure_reason == "structured model convergence failure"


def test_runtime_error_text_cannot_impersonate_not_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_without_status(*args: object, **kwargs: object) -> None:
        raise RuntimeError("prerequisite failed because a service did not converge")

    monkeypatch.setattr(executor, "_execute_steady", fail_without_status)

    outcome = execute(load_preset("steady-baseline"))

    assert outcome.runtime_status == "failed"
    assert outcome.failure_reason == ("prerequisite failed because a service did not converge")


def test_short_m3_and_m4_dispatch_keep_complete_timeseries_and_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_runtime_resource_bundle()
    open_scenario = bundle.open_loop_scenarios["feed_step"]
    short_open = replace(
        open_scenario,
        duration_s=2.0,
        time_step_s=1.0,
        events=(
            MappingProxyType(
                {
                    "time_s": 1.0,
                    "target": "fresh_feed_flow_kg_s",
                    "value": bundle.effective_case.feed.mass_flow_kg_s * 1.05,
                }
            ),
        ),
    )
    closed_scenario = bundle.closed_loop_scenarios["feed_step"]
    short_closed = replace(
        closed_scenario,
        duration_s=2.0,
        time_step_s=1.0,
        events=(SetpointEvent(1.0, "feed_flow", 1.05),),
    )
    monkeypatch.setattr(executor, "_open_loop_scenario", lambda *_: short_open)
    monkeypatch.setattr(executor, "_closed_loop_scenario", lambda *_: short_closed)

    def forbidden_dynamic_as_dict(
        result: DynamicSimulationResult,
    ) -> dict[str, object]:
        del result
        raise AssertionError("adapter must not materialize the full M3 result")

    def forbidden_closed_as_dict(
        result: ClosedLoopSimulationResult,
    ) -> dict[str, object]:
        del result
        raise AssertionError("adapter must not materialize the full M4 result")

    monkeypatch.setattr(DynamicSimulationResult, "as_dict", forbidden_dynamic_as_dict)
    monkeypatch.setattr(
        ClosedLoopSimulationResult,
        "as_dict",
        forbidden_closed_as_dict,
    )

    open_outcome = execute(load_preset("open-loop-feed-step"))
    closed_outcome = execute(load_preset("closed-loop-feed-step"))

    assert open_outcome.runtime_status == "success"
    assert open_outcome.raw_result_type == "DynamicSimulationResult"
    assert len(open_outcome.timeseries) == 3
    assert [sample["time_s"] for sample in open_outcome.timeseries] == [0.0, 1.0, 2.0]
    assert open_outcome.events[0].time_s == 1.0
    assert open_outcome.events[0].event_type == "command_change"

    assert closed_outcome.raw_result_type == "ClosedLoopSimulationResult"
    assert len(closed_outcome.timeseries) == 3
    assert [sample["time_s"] for sample in closed_outcome.timeseries] == [0.0, 1.0, 2.0]
    assert closed_outcome.events[0].time_s == 1.0
    assert closed_outcome.events[0].event_type == "setpoint_change"


def test_portable_m6_scenarios_preserve_limited_and_rejected_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limited_request = load_preset("m6-abnormal-pump-trip")
    first = execute(limited_request)
    second = execute(limited_request)

    assert first.runtime_status == "limited"
    assert first.engine_status == "success"
    assert not first.errors
    assert len(first.timeseries) == 601
    assert first.result_fingerprint == second.result_fingerprint
    assert first.summary["execution_profile"] == ("portable_selected_scenario_replay")
    assert [(event.time_s, event.event_type) for event in first.events] == [
        (60.0, "fault_command"),
        (60.0, "trip_pending"),
        (62.0, "triggered"),
    ]
    assert all(event.time_s is None or event.time_s >= 60.0 for event in first.events)

    def solver_must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("structural rejection must precede solver execution")

    monkeypatch.setattr(executor, "solve_recycle", solver_must_not_run)
    rejected = execute(load_preset("m6-structural-rejection"))
    assert rejected.runtime_status == "rejected"
    assert rejected.engine_status == "not_called"
    assert rejected.summary["solver_called"] is False
    assert rejected.failure_stage == "applicability_preflight"
    assert rejected.errors[0].retryable is False


def test_resource_failure_is_a_failed_outcome_not_a_false_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_loader(*args: object) -> None:
        raise ValueError("resource hash mismatch")

    monkeypatch.setattr(executor, "load_runtime_resource_bundle", broken_loader)
    request: RunRequest = load_preset("open-loop-feed-step")
    first = execute(request)
    second = execute(request)

    assert first.runtime_status == "failed"
    assert first.failure_stage == "resource_loading"
    assert first.failure_reason == "resource hash mismatch"
    assert first.duration_s == 7200.0
    assert first.time_step_s == 1.0
    assert first.result_fingerprint == second.result_fingerprint


def test_failed_dynamic_timeline_does_not_claim_future_configured_events() -> None:
    configured = (
        EventRecord(
            sequence=0,
            time_s=600.0,
            event_type="command_change",
            source="M3_scenario",
            stage="M3_open_loop",
            message="future feed step",
            details={"target": "fresh_feed_flow_kg_s"},
        ),
    )

    executed, unexecuted = _events_completed_by(configured, 100.0)
    assert executed == ()
    assert len(unexecuted) == 1
    assert unexecuted[0]["time_s"] == 600.0

    executed_at_event, remaining = _events_completed_by(configured, 600.0)
    assert len(executed_at_event) == 1
    assert executed_at_event[0].sequence == 0
    assert remaining == ()


def test_failed_m6_adapter_does_not_claim_future_fault_command() -> None:
    bundle = load_runtime_resource_bundle()
    spec = bundle.validation_config.scenario("limited_pump_around_1_trip")
    failed = executor._failed_m6_scenario(
        spec,
        bundle.validation_config,
        stage="candidate_simulation",
        reason="candidate failed before the fault",
        input_fingerprint="a" * 64,
    )
    payload = adapt_validation_scenario(
        load_preset("m6-abnormal-pump-trip"),
        failed,
        timeseries=({"time_s": 0.0}, {"time_s": 9.0}),
        configured_events=(
            validation_command_event(
                time_s=60.0,
                target="pump_around_1_duty_w",
                value=0.0,
            ),
        ),
        versions={"software_version": "0.1.0"},
        source_fingerprints={"fixture": "b" * 64},
        effective_input_fingerprint="c" * 64,
        formal_m6_result_fingerprint="d" * 64,
        duration_s=600.0,
        time_step_s=1.0,
        failure_time_s=10.0,
        last_valid={"time_s": 9.0},
    )

    assert [(event.time_s, event.event_type) for event in payload.events] == [
        (10.0, "execution_failure")
    ]
    unexecuted = payload.diagnostics["unexecuted_timeline_events"]
    assert isinstance(unexecuted, tuple)
    first_unexecuted = unexecuted[0]
    assert isinstance(first_unexecuted, Mapping)
    assert first_unexecuted["time_s"] == 60.0
