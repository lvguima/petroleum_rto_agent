from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

from petroleum_rto.domain_model import load_provider_catalog
from petroleum_rto.domain_model.evidence import EvidenceStore
from petroleum_rto.domain_model.prompt import PromptCompiler
from petroleum_rto.domain_model.runtime import DomainIntentRuntime
from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    ClarificationAnswer,
    ContractRef,
    DomainModelInvocationResult,
    DomainModelPort,
    DomainModelRequest,
    ProviderAttempt,
    ProviderError,
    ProviderUsage,
)
from petroleum_rto.rto.runtime import build_intent_communication_service


def _intent(
    *,
    valid: bool = True,
    ambiguities: list[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_id": "optimization-intent",
        "schema_version": "1.0.0",
        "intent_id": "runtime-energy-intent",
        "objectives": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "sense": "minimize",
                "priority": 1,
            }
        ],
        "decision_variables": [
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        ],
        "constraints": [],
        "preference": {
            "method": "single-objective",
            "objective_order": ["specific_furnace_fuel_energy_mj_per_t"],
        },
        "result_request": {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": False,
            "max_candidates": 1,
        },
        "ambiguities": ambiguities or [],
    }
    if not valid:
        del result["preference"]
    return result


def _response(
    request: DomainModelRequest,
    *,
    valid: bool = True,
    ambiguities: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
            "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
            "response_id": f"response-{request.turn_index}-{request.model_attempt}",
            "request_ref": request.ref.as_dict(),
            "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
            "outcome": "intent",
            "intent": _intent(valid=valid, ambiguities=ambiguities),
        },
        ensure_ascii=False,
    )


def _success(
    request: DomainModelRequest,
    response: str,
    *,
    invocation_index: int,
    served_model: str = "deepseek-v4-flash-0731",
) -> DomainModelInvocationResult:
    return DomainModelInvocationResult(
        schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
        schema_version=COMMUNICATION_SCHEMA_VERSION,
        invocation_id=f"fake-invocation-{invocation_index}",
        request_ref=request.ref,
        status="succeeded",
        attempts=(
            ProviderAttempt(
                attempt_index=1,
                provider_id="dmx-cn",
                provider_version="1.0.0",
                status="succeeded",
                provider_request_id=f"provider-request-{invocation_index}",
                served_model=served_model,
                finish_reason="stop",
                duration_ms=12,
                usage=ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150),
                error=None,
            ),
        ),
        response=response,
        error=None,
    )


class _FakePort:
    provider_id = "dmx-cn"
    provider_version = "1.0.0"

    def __init__(
        self,
        invoke: Callable[[DomainModelRequest, int], DomainModelInvocationResult],
    ) -> None:
        self._invoke = invoke
        self.requests: list[DomainModelRequest] = []

    def invoke(self, request: DomainModelRequest) -> DomainModelInvocationResult:
        self.requests.append(request)
        return self._invoke(request, len(self.requests))


class _RoundClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _DeadlineAwareFakePort(_FakePort):
    def __init__(
        self,
        invoke: Callable[[DomainModelRequest, int], DomainModelInvocationResult],
    ) -> None:
        super().__init__(invoke)
        self.timeouts: list[float] = []

    def invoke_with_timeout(
        self,
        request: DomainModelRequest,
        *,
        timeout_seconds: float,
    ) -> DomainModelInvocationResult:
        self.timeouts.append(timeout_seconds)
        return self.invoke(request)


def _runtime(
    repo_root: Path,
    evidence_root: Path,
    port: DomainModelPort,
    *,
    clock: Callable[[], float] | None = None,
) -> DomainIntentRuntime:
    catalog = load_provider_catalog(repo_root)
    provider = catalog.provider("dmx-cn")
    model = provider.model("deepseek-v4-flash-0731")
    return DomainIntentRuntime(
        provider_profile=provider,
        model_profile=model,
        port=port,
        communication_service=build_intent_communication_service(repo_root=repo_root),
        prompt_compiler=PromptCompiler(),
        evidence_store=EvidenceStore(evidence_root),
        execution_mode="synthetic_test",
        clock=clock if clock is not None else monotonic,
        session_id_factory=lambda: "runtime-session",
    )


def test_runtime_resolves_and_persists_only_normalized_invocation_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    port = _FakePort(
        lambda request, index: _success(request, _response(request), invocation_index=index)
    )
    runtime = _runtime(repo_root, tmp_path, port)

    outcome = runtime.interpret("降低单位进料炉燃料能耗。")

    assert outcome.status == "resolved"
    assert outcome.communication_result is not None
    assert outcome.communication_result.resolved_intent is not None
    assert outcome.provider_error is None
    assert len(outcome.steps) == 1
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    assert record.manifest_fingerprint == outcome.evidence_fingerprint
    assert record.evidence.invocations[0].provider_request_id == "provider-request-1"
    serialized = json.dumps(record.evidence.as_dict(), ensure_ascii=False).lower()
    assert "authorization" not in serialized
    assert "降低单位进料" not in serialized
    assert "raw_response" not in serialized
    with pytest.raises(ValueError, match="already has a first-turn snapshot"):
        runtime.interpret("再次降低单位进料炉燃料能耗。")
    assert len(port.requests) == 1
    state_path = record.run_dir / "session.json"
    state_path.write_bytes(state_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="size differs"):
        EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)


def test_concurrent_first_turn_claim_invokes_provider_only_once(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    invoked = Event()
    release = Event()

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        invoked.set()
        assert release.wait(timeout=5.0)
        return _success(request, _response(request), invocation_index=index)

    port = _FakePort(invoke)
    first_runtime = _runtime(repo_root, tmp_path, port)
    second_runtime = _runtime(repo_root, tmp_path, port)
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = executor.submit(first_runtime.interpret, "降低能耗。")
        assert invoked.wait(timeout=5.0)
        rejected = executor.submit(second_runtime.interpret, "提高收率。")
        release.set()
        assert accepted.result(timeout=5.0).status == "resolved"
        with pytest.raises(ValueError, match="already in progress|already has"):
            rejected.result(timeout=5.0)

    assert len(port.requests) == 1


def test_runtime_performs_one_full_semantic_repair(repo_root: Path, tmp_path: Path) -> None:
    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        return _success(
            request,
            (_response(request) if index == 2 else "RAW-PROVIDER-THOUGHT-MUST-NOT-BE-PERSISTED"),
            invocation_index=index,
        )

    port = _FakePort(invoke)
    runtime = _runtime(repo_root, tmp_path, port)

    outcome = runtime.interpret("降低能耗。")

    assert outcome.status == "resolved"
    assert [item.model_attempt for item in port.requests] == [1, 2]
    assert len(outcome.steps) == 2
    first_result = outcome.steps[0].communication_result
    assert first_result is not None and first_result.status == "repair_required"
    assert "RAW-PROVIDER-THOUGHT" not in json.dumps(outcome.steps[0].as_dict(), ensure_ascii=False)
    assert outcome.steps[1].request.feedback_issues == first_result.issues
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    assert len(record.evidence.invocations) == 2
    persisted = b"".join(item.read_bytes() for item in record.run_dir.iterdir())
    assert b"RAW-PROVIDER-THOUGHT" not in persisted


def test_runtime_repairs_deep_json_and_persists_the_physical_attempts(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    deeply_nested = "[" * 20_000 + "0" + "]" * 20_000

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        return _success(
            request,
            deeply_nested if index == 1 else _response(request),
            invocation_index=index,
        )

    outcome = _runtime(repo_root, tmp_path, _FakePort(invoke)).interpret("降低能耗。")

    assert outcome.status == "resolved"
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    assert len(record.evidence.invocations) == 2
    assert all(item.transport_attempts == 1 for item in record.evidence.invocations)


def test_runtime_passes_one_shared_round_deadline_across_semantic_repair(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    clock = _RoundClock()

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        clock.advance(80.0 if index == 1 else 10.0)
        return _success(
            request,
            _response(request, valid=index == 2),
            invocation_index=index,
        )

    port = _DeadlineAwareFakePort(invoke)
    outcome = _runtime(repo_root, tmp_path, port, clock=clock).interpret("降低能耗。")

    assert outcome.status == "resolved"
    assert port.timeouts == pytest.approx([120.0, 40.0])
    assert clock.now == 90.0


def test_runtime_rejects_a_non_deadline_aware_port_that_overruns_the_round(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    clock = _RoundClock()

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        clock.advance(121.0)
        return _success(request, _response(request), invocation_index=index)

    outcome = _runtime(
        repo_root,
        tmp_path,
        _FakePort(invoke),
        clock=clock,
    ).interpret("降低能耗。")

    assert outcome.status == "provider_failed"
    assert outcome.provider_error is not None
    assert outcome.provider_error.code == "round-deadline-exceeded"
    assert outcome.communication_result is None
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    invocation = record.evidence.invocations[-1]
    assert invocation.status == "blocked"
    assert invocation.error_code == "round-deadline-exceeded"
    assert invocation.transport_attempts == 1


def test_provider_failure_never_becomes_business_unsupported(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    failure = ProviderError(
        category="authentication",
        code="credential-missing-or-invalid",
        message="provider authentication failed",
        retryable=False,
        http_status=401,
    )

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        attempt = ProviderAttempt(
            attempt_index=1,
            provider_id="dmx-cn",
            provider_version="1.0.0",
            status="failed",
            provider_request_id=None,
            served_model=None,
            finish_reason=None,
            duration_ms=1,
            usage=None,
            error=failure,
        )
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id=f"fake-invocation-{index}",
            request_ref=request.ref,
            status="failed",
            attempts=(attempt,),
            response=None,
            error=failure,
        )

    runtime = _runtime(repo_root, tmp_path, _FakePort(invoke))

    outcome = runtime.interpret("降低能耗。")

    assert outcome.status == "provider_failed"
    assert outcome.communication_result is None
    assert outcome.provider_error is not None
    assert outcome.provider_error.category == failure.category
    assert outcome.provider_error.code == failure.code
    assert outcome.provider_error.retryable == failure.retryable
    assert outcome.provider_error.http_status == failure.http_status
    assert outcome.provider_error.message != failure.message
    assert outcome.as_dict()["status"] != "unsupported"


def test_adapter_exception_and_invalid_return_are_structured_with_local_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    callbacks = (
        (
            lambda request, index: (_ for _ in ()).throw(RuntimeError("untrusted detail")),
            "adapter-raised-exception",
        ),
        (lambda request, index: None, "adapter-invalid-result"),
    )
    for callback, expected_code in callbacks:
        evidence_root = tmp_path / expected_code
        runtime = _runtime(
            evidence_root=evidence_root, repo_root=repo_root, port=_FakePort(callback)
        )  # type: ignore[arg-type]

        outcome = runtime.interpret("降低能耗。")

        assert outcome.status == "provider_failed"
        assert outcome.provider_error is not None
        assert outcome.provider_error.code == expected_code
        assert outcome.evidence_manifest is not None
        record = EvidenceStore(evidence_root).read_snapshot(outcome.evidence_manifest)
        assert record.evidence.invocations[-1].transport_attempts == 0


def test_adapter_egress_failure_does_not_persist_the_sensitive_prompt(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    error = ProviderError(
        category="invalid_request",
        code="egress-suspected-credential",
        message="domain-model request was blocked by outbound policy",
        retryable=False,
        http_status=None,
    )

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id=f"egress-blocked-{index}",
            request_ref=request.ref,
            status="failed",
            attempts=(),
            response=None,
            error=error,
        )

    outcome = _runtime(repo_root, tmp_path, _FakePort(invoke)).interpret(
        "请处理 test-only-key 并降低能耗。"
    )

    assert outcome.status == "egress_blocked"
    assert outcome.evidence_manifest is None
    assert not (tmp_path / "runs" / "domain_model" / "sessions").exists()


def test_egress_guard_blocks_secret_before_call_or_persistence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    port = _FakePort(
        lambda request, index: _success(request, _response(request), invocation_index=index)
    )
    runtime = _runtime(repo_root, tmp_path, port)

    outcome = runtime.interpret("请使用 api_key=abcd1234 处理这个目标。")

    assert outcome.status == "egress_blocked"
    assert outcome.provider_error is not None
    assert outcome.provider_error.code == "egress-suspected-credential"
    assert port.requests == []
    assert outcome.evidence_manifest is None
    assert not (tmp_path / "runs" / "domain_model" / "sessions").exists()


def test_runtime_stops_on_served_model_drift(repo_root: Path, tmp_path: Path) -> None:
    port = _FakePort(
        lambda request, index: _success(
            request,
            _response(request),
            invocation_index=index,
            served_model="unexpected-model",
        )
    )
    runtime = _runtime(repo_root, tmp_path, port)

    outcome = runtime.interpret("降低能耗。")

    assert outcome.status == "provider_failed"
    assert outcome.provider_error is not None
    assert outcome.provider_error.category == "model_mismatch"
    assert outcome.communication_result is None


def test_unsafe_served_model_is_redacted_in_mismatch_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    port = _FakePort(
        lambda request, index: _success(
            request,
            _response(request),
            invocation_index=index,
            served_model="vendor/bad-model",
        )
    )

    outcome = _runtime(repo_root, tmp_path, port).interpret("降低能耗。")

    assert outcome.status == "provider_failed"
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    invocation = record.evidence.invocations[-1]
    assert invocation.status == "blocked"
    assert invocation.served_model == "unsafe-served-model-redacted"
    assert invocation.attempts[-1].served_model == "unsafe-served-model-redacted"


def test_runtime_structures_and_persists_invocation_request_ref_mismatch(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    def mismatched(
        request: DomainModelRequest,
        index: int,
    ) -> DomainModelInvocationResult:
        invocation = _success(request, _response(request), invocation_index=index)
        return replace(invocation, request_ref=ContractRef("wrong-request", "0" * 64))

    outcome = _runtime(repo_root, tmp_path, _FakePort(mismatched)).interpret("降低能耗。")

    assert outcome.status == "provider_failed"
    assert outcome.provider_error is not None
    assert outcome.provider_error.code == "invocation-request-ref-mismatch"
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    assert record.evidence.invocations[-1].status == "blocked"
    assert record.evidence.invocations[-1].error_code == "invocation-request-ref-mismatch"


def test_clarification_continues_from_manifest_without_switching_model(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    first_port = _FakePort(
        lambda request, index: _success(
            request,
            _response(request, ambiguities=["result-alternatives-ambiguous"]),
            invocation_index=index,
        )
    )
    first_runtime = _runtime(repo_root, tmp_path, first_port)
    first = first_runtime.interpret("是否返回备选方案还没决定。")
    assert first.status == "needs_clarification"
    assert first.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_manifest(first.evidence_manifest)
    state = record.session_state
    assert state is not None
    assert state.final_communication_result_ref is not None
    result = state.steps[-1].communication_result
    assert result is not None and result.clarification is not None
    question = result.clarification.questions[0]

    blocked_port = _FakePort(
        lambda request, index: _success(request, _response(request), invocation_index=index + 1)
    )
    blocked_runtime = _runtime(repo_root, tmp_path, blocked_port)
    blocked = blocked_runtime.continue_session(
        record,
        message_id="user-2-blocked",
        user_text="请使用 api_key=abcd1234 后只返回最终方案。",
        answers=(
            ClarificationAnswer(
                question_id=question.question_id,
                values=("selected-only",),
            ),
        ),
    )
    assert blocked.status == "egress_blocked"
    assert blocked.evidence_manifest is None
    assert blocked.evidence_fingerprint is None
    assert blocked_port.requests == []
    unchanged = EvidenceStore(tmp_path).read_manifest(first.evidence_manifest)
    assert unchanged.session_state is not None
    assert unchanged.session_state.status == "needs_clarification"

    second_port = _FakePort(
        lambda request, index: _success(request, _response(request), invocation_index=index + 1)
    )
    second_runtime = _runtime(repo_root, tmp_path, second_port)
    second = second_runtime.continue_session(
        record,
        message_id="user-2",
        user_text="只返回最终方案。",
        answers=(
            ClarificationAnswer(
                question_id=question.question_id,
                values=("selected-only",),
            ),
        ),
    )

    assert second.status == "resolved"
    assert second.evidence_manifest is not None
    continued = EvidenceStore(tmp_path).read_manifest(second.evidence_manifest)
    continued_state = continued.session_state
    assert continued_state is not None
    assert continued_state.snapshot_index == 2
    assert continued_state.previous_manifest_fingerprint == record.manifest_fingerprint
    assert continued_state.provider_id == state.provider_id == "dmx-cn"
    assert continued_state.model_id == state.model_id == "deepseek-v4-flash-0731"
    assert [item.request.turn_index for item in continued_state.steps] == [1, 2]
    assert len(continued.evidence.invocations) == 2

    requests_after_first_continuation = len(second_port.requests)
    with pytest.raises(ValueError, match="stale or incomplete"):
        second_runtime.continue_session(
            record,
            message_id="user-2-replay",
            user_text="仍然只返回最终方案。",
            answers=(
                ClarificationAnswer(
                    question_id=question.question_id,
                    values=("selected-only",),
                ),
            ),
        )
    assert len(second_port.requests) == requests_after_first_continuation

    first.evidence_manifest.unlink()
    with pytest.raises(ValueError, match="incomplete or missing"):
        EvidenceStore(tmp_path).read_manifest(second.evidence_manifest)


def test_concurrent_clarification_replay_invokes_provider_only_once(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    first = _runtime(
        repo_root,
        tmp_path,
        _FakePort(
            lambda request, index: _success(
                request,
                _response(request, ambiguities=["result-alternatives-ambiguous"]),
                invocation_index=index,
            )
        ),
    ).interpret("是否返回备选方案还没决定。")
    assert first.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(first.evidence_manifest)
    state = record.session_state
    assert state is not None
    result = state.steps[-1].communication_result
    assert result is not None and result.clarification is not None
    question = result.clarification.questions[0]
    invoked = Event()
    release = Event()

    def invoke(request: DomainModelRequest, index: int) -> DomainModelInvocationResult:
        invoked.set()
        assert release.wait(timeout=5.0)
        return _success(request, _response(request), invocation_index=index + 1)

    port = _FakePort(invoke)
    first_runtime = _runtime(repo_root, tmp_path, port)
    second_runtime = _runtime(repo_root, tmp_path, port)
    answers = (
        ClarificationAnswer(
            question_id=question.question_id,
            values=("selected-only",),
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = executor.submit(
            first_runtime.continue_session,
            record,
            message_id="user-2-a",
            user_text="只返回最终方案。",
            answers=answers,
        )
        assert invoked.wait(timeout=5.0)
        rejected = executor.submit(
            second_runtime.continue_session,
            record,
            message_id="user-2-b",
            user_text="只返回最终方案。",
            answers=answers,
        )
        release.set()
        assert accepted.result(timeout=5.0).status == "resolved"
        with pytest.raises(ValueError, match="already in progress|stale or incomplete"):
            rejected.result(timeout=5.0)

    assert len(port.requests) == 1
