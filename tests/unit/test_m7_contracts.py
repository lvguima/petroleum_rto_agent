from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

import pytest

from petroleum_rto.cdu.runtime import contracts as runtime_contracts
from petroleum_rto.cdu.runtime.contracts import (
    RUN_REQUEST_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
    ErrorRecord,
    EventRecord,
    ExecutionPayload,
    JsonValue,
    RunRequest,
)

_FP_A = "a" * 64
_FP_B = "b" * 64


def _request(**changes: object) -> RunRequest:
    values: dict[str, object] = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "request_version": RUN_REQUEST_VERSION,
        "preset_id": "open-loop-feed-step",
        "run_type": "open_loop_dynamic",
        "random_seed": 0,
        "parameters": {"duration_s": 600.0},
        "overrides": {"feed_ratio": 1.05},
        "metadata": {"purpose": "deterministic test"},
        "run_id": "run-a",
        "requested_at_utc": "2026-08-18T00:00:00+00:00",
    }
    values.update(changes)
    return RunRequest.from_mapping(values)


def _event() -> EventRecord:
    return EventRecord(
        sequence=0,
        time_s=60.0,
        event_type="command_step",
        source="M3",
        stage="dynamic",
        message="feed command changed",
        details={"target": "fresh_feed_flow_kg_s", "ratio": 1.05},
    )


def _error() -> ErrorRecord:
    return ErrorRecord(
        sequence=0,
        error_type="solver_failure",
        stage="recycle",
        message="recycle did not converge",
        time_s=None,
        last_valid={"iteration": 9},
        retryable=False,
        details={"reason": "maximum_iterations"},
    )


def _terminal_failure_event() -> EventRecord:
    return EventRecord(
        sequence=0,
        time_s=None,
        event_type="execution_failure",
        source="M7_runtime",
        stage="recycle",
        message="recycle did not converge",
        details={"error_type": "solver_failure"},
    )


def _success_payload(**changes: object) -> ExecutionPayload:
    values: dict[str, object] = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "preset_id": "open-loop-feed-step",
        "run_type": "open_loop_dynamic",
        "runtime_status": "success",
        "request_fingerprint": _FP_A,
        "engine_status": "success",
        "raw_result_type": "DynamicSimulationResult",
        "summary": {"sample_count": 601, "final": {"feed_ratio": 1.05}},
        "timeseries": ({"time_s": 0.0, "feed_ratio": 1.0},),
        "events": (_event(),),
        "errors": (),
        "versions": {"software_version": "0.1.0", "simulation_stage": "M3"},
        "source_fingerprints": {"model": _FP_B},
        "effective_input_fingerprint": _FP_A,
        "synthetic": True,
        "data_origin": "M3_open_loop_simulation",
        "claim_scope": "engineering_simulation_only",
        "failure_stage": None,
        "failure_reason": None,
        "failure_time_s": None,
        "last_valid": None,
        "duration_s": 600.0,
        "time_step_s": 1.0,
        "diagnostics": {"conservation_passed": True},
    }
    values.update(changes)
    return ExecutionPayload(**values)  # type: ignore[arg-type]


def test_run_request_round_trip_and_ephemeral_fields_do_not_change_fingerprint() -> None:
    request = _request()
    other_identity = replace(
        request,
        run_id="run-b",
        requested_at_utc="2026-08-18T00:00:01Z",
    )

    assert request.request_fingerprint == other_identity.request_fingerprint
    assert RunRequest.from_mapping(request.as_dict()) == request
    assert isinstance(request.parameters, MappingProxyType)
    with pytest.raises(TypeError):
        request.parameters["duration_s"] = 1.0  # type: ignore[index]


@pytest.mark.parametrize(
    ("change", "expected_exception"),
    [
        ({"random_seed": True}, TypeError),
        ({"parameters": {"duration_s": False}}, TypeError),
        ({"overrides": {"feed_ratio": float("inf")}}, ValueError),
        ({"preset_id": "../escape"}, ValueError),
        ({"parameters": {"../duration": 1.0}}, ValueError),
        ({"requested_at_utc": "2026-08-18T00:00:00"}, ValueError),
    ],
)
def test_run_request_rejects_invalid_values(
    change: dict[str, object],
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        _request(**change)


def test_run_request_rejects_unknown_fields_and_fingerprint_tampering() -> None:
    payload = _request().as_dict()
    payload["unknown"] = 1
    with pytest.raises(ValueError, match="unknown"):
        RunRequest.from_mapping(payload)

    payload = _request().as_dict()
    payload["request_fingerprint"] = _FP_B
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        RunRequest.from_mapping(payload)


def test_event_and_error_records_are_strict_and_deeply_immutable() -> None:
    event = _event()
    error = _error()

    assert EventRecord.from_mapping(event.as_dict()) == event
    assert ErrorRecord.from_mapping(error.as_dict()) == error
    assert isinstance(event.details, MappingProxyType)
    assert error.last_valid is not None
    assert isinstance(error.last_valid, MappingProxyType)
    with pytest.raises(TypeError):
        event.details["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        EventRecord.from_mapping({**event.as_dict(), "time_s": True})
    with pytest.raises(TypeError):
        ErrorRecord.from_mapping({**error.as_dict(), "retryable": 1})


def test_execution_payload_round_trip_and_result_fingerprint() -> None:
    payload = _success_payload()
    rebuilt = ExecutionPayload.from_mapping(payload.as_dict())

    assert rebuilt == payload
    assert rebuilt.result_fingerprint == payload.result_fingerprint
    assert isinstance(payload.summary, MappingProxyType)
    assert isinstance(payload.timeseries[0], MappingProxyType)
    assert len(payload.result_fingerprint) == 64
    canonical = json.dumps(
        payload.fingerprint_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert payload.result_fingerprint == hashlib.sha256(canonical).hexdigest()


def test_from_mapping_freezes_each_timeseries_sample_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _success_payload().as_dict()
    original = runtime_contracts._freeze_json_mapping
    timeseries_freezes = 0

    def count_freezes(
        value: object,
        *,
        context: str,
    ) -> Mapping[str, JsonValue]:
        nonlocal timeseries_freezes
        if "timeseries[" in context:
            timeseries_freezes += 1
        return original(value, context=context)

    monkeypatch.setattr(runtime_contracts, "_freeze_json_mapping", count_freezes)
    decoded = ExecutionPayload.from_mapping(document)
    assert decoded.result_fingerprint == document["result_fingerprint"]
    assert timeseries_freezes == len(decoded.timeseries)


@pytest.mark.parametrize("runtime_status", ["success", "limited"])
def test_non_failure_execution_rejects_errors_and_failure_fields(runtime_status: str) -> None:
    with pytest.raises(ValueError):
        _success_payload(
            runtime_status=runtime_status,
            errors=(_error(),),
            failure_stage="recycle",
            failure_reason="bad",
        )
    for engine_status in ("failed", "exception", "rejected"):
        with pytest.raises(ValueError, match="engine_status='success'"):
            _success_payload(
                runtime_status=runtime_status,
                engine_status=engine_status,
            )

    with pytest.raises(ValueError, match="fixed synthetic runtime"):
        _success_payload(
            runtime_status=runtime_status,
            synthetic=False,
            data_origin="field_measurements",
            claim_scope="SIS_validated",
        )
    with pytest.raises(ValueError, match="cannot contain failure events"):
        _success_payload(
            runtime_status=runtime_status,
            events=(_terminal_failure_event(),),
        )


def test_runtime_status_must_agree_with_domain_status() -> None:
    with pytest.raises(ValueError, match="cannot have a limited or rejected domain"):
        _success_payload(diagnostics={"domain_status": "rejected"})
    with pytest.raises(ValueError, match="requires domain_status='limited'"):
        _success_payload(runtime_status="limited")
    limited = _success_payload(
        runtime_status="limited",
        diagnostics={"domain_status": "limited"},
    )
    assert limited.runtime_status == "limited"


@pytest.mark.parametrize("runtime_status", ["failed", "rejected", "not_converged"])
def test_failure_execution_requires_complete_failure_evidence(runtime_status: str) -> None:
    payload = _success_payload(
        runtime_status=runtime_status,
        engine_status=("exception" if runtime_status == "rejected" else runtime_status),
        events=(_terminal_failure_event(),),
        errors=(_error(),),
        failure_stage="recycle",
        failure_reason="recycle did not converge",
        failure_time_s=None,
        last_valid={"iteration": 9},
    )
    assert payload.runtime_status == runtime_status

    with pytest.raises(ValueError, match="requires an error"):
        _success_payload(
            runtime_status=runtime_status,
            engine_status=("exception" if runtime_status == "rejected" else runtime_status),
            events=(_terminal_failure_event(),),
        )

    if runtime_status == "rejected":
        with pytest.raises(ValueError, match="successful engine"):
            _success_payload(
                runtime_status="rejected",
                engine_status="success",
                events=(_terminal_failure_event(),),
                errors=(_error(),),
                failure_stage="recycle",
                failure_reason="recycle did not converge",
                last_valid={"iteration": 9},
            )
    else:
        with pytest.raises(ValueError, match="successful engine"):
            _success_payload(
                runtime_status=runtime_status,
                engine_status="success",
                events=(_terminal_failure_event(),),
                errors=(_error(),),
                failure_stage="recycle",
                failure_reason="recycle did not converge",
                last_valid={"iteration": 9},
            )


@pytest.mark.parametrize("runtime_status", ["failed", "not_converged"])
def test_failure_terminal_event_cannot_claim_structural_rejection(
    runtime_status: str,
) -> None:
    with pytest.raises(ValueError, match="terminal failure event"):
        _success_payload(
            runtime_status=runtime_status,
            engine_status=runtime_status,
            events=(
                replace(
                    _terminal_failure_event(),
                    event_type="structural_rejection",
                ),
            ),
            errors=(_error(),),
            failure_stage="recycle",
            failure_reason="recycle did not converge",
            last_valid={"iteration": 9},
        )


def test_failure_fields_must_match_error_and_terminal_event() -> None:
    with pytest.raises(ValueError, match="first error"):
        _success_payload(
            runtime_status="failed",
            engine_status="failed",
            events=(_terminal_failure_event(),),
            errors=(_error(),),
            failure_stage="recycle",
            failure_reason="different reason",
            last_valid={"iteration": 9},
        )


def test_execution_enforces_run_type_time_contract_and_boolean_type() -> None:
    with pytest.raises(ValueError, match="dynamic execution"):
        _success_payload(duration_s=None, time_step_s=None)
    with pytest.raises(ValueError, match="steady execution"):
        _success_payload(
            run_type="steady_recycle",
            data_origin="M2_steady_model_prediction",
        )
    with pytest.raises(TypeError, match="synthetic"):
        _success_payload(synthetic=1)


def test_execution_rejects_noncontiguous_sequences_and_tampered_fingerprint() -> None:
    late_event = replace(_event(), sequence=1)
    with pytest.raises(ValueError, match="event sequences"):
        _success_payload(events=(late_event,))

    reversed_event = replace(_event(), sequence=1, time_s=59.0)
    with pytest.raises(ValueError, match="event times must be nondecreasing"):
        _success_payload(events=(_event(), reversed_event))

    payload = _success_payload().as_dict()
    payload["result_fingerprint"] = _FP_B
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        ExecutionPayload.from_mapping(payload)


def test_execution_rejects_unknown_top_level_and_nested_nonfinite_json() -> None:
    payload = _success_payload().as_dict()
    payload["unknown"] = 1
    with pytest.raises(ValueError, match="unknown"):
        ExecutionPayload.from_mapping(payload)
    with pytest.raises(ValueError, match="non-finite"):
        _success_payload(summary={"bad": float("nan")})
