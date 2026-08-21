from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from petroleum_rto.domain_model._json import (
    canonical_fingerprint,
    canonical_json_bytes,
    decode_json_object,
    sha256_bytes,
)
from petroleum_rto.domain_model.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    INVOCATION_EVIDENCE_SCHEMA_ID,
    TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID,
    EvidenceStore,
    InvocationEvidence,
    TransportAttemptEvidence,
)
from petroleum_rto.domain_model.prompt import PromptCompiler
from petroleum_rto.domain_model.session import (
    DOMAIN_INTENT_SESSION_SCHEMA_ID,
    DOMAIN_INTENT_SESSION_SCHEMA_VERSION,
    DomainIntentSessionState,
    SessionStepState,
)
from petroleum_rto.rto.communication import ProviderError
from petroleum_rto.rto.runtime import build_intent_communication_service

_DIGEST = "a" * 64


def _succeeded_attempt(
    *,
    attempt_index: int = 1,
    duration_ms: int = 123,
    provider_request_id: str = "request-1",
) -> TransportAttemptEvidence:
    return TransportAttemptEvidence(
        schema_id=TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID,
        schema_version=EVIDENCE_SCHEMA_VERSION,
        attempt_index=attempt_index,
        status="succeeded",
        provider_request_id=provider_request_id,
        served_model="gpt-5.6-sol",
        duration_ms=duration_ms,
        input_tokens=100,
        output_tokens=30,
        total_tokens=130,
        error_category=None,
        error_code=None,
        http_status=None,
        retryable=None,
    )


def _failed_attempt(
    *,
    attempt_index: int = 1,
    duration_ms: int = 40,
    provider_request_id: str | None = "request-failed-1",
    retryable: bool = True,
) -> TransportAttemptEvidence:
    return TransportAttemptEvidence(
        schema_id=TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID,
        schema_version=EVIDENCE_SCHEMA_VERSION,
        attempt_index=attempt_index,
        status="failed",
        provider_request_id=provider_request_id,
        served_model=None,
        duration_ms=duration_ms,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        error_category="rate_limit",
        error_code="provider-rate-limit",
        http_status=429,
        retryable=retryable,
    )


def _invocation(
    *,
    session_id: str = "session-1",
    status: Literal["succeeded", "blocked", "failed"] = "succeeded",
    attempts: tuple[TransportAttemptEvidence, ...] | None = None,
) -> InvocationEvidence:
    physical_attempts = (_succeeded_attempt(),) if attempts is None else attempts
    final = physical_attempts[-1]
    succeeded = status == "succeeded"
    return InvocationEvidence(
        schema_id=INVOCATION_EVIDENCE_SCHEMA_ID,
        schema_version=EVIDENCE_SCHEMA_VERSION,
        session_id=session_id,
        invocation_id="invocation-1",
        status=status,
        execution_mode="synthetic_test",
        provider_id="dmx-cn",
        provider_version="1.0.0",
        provider_profile_fingerprint=_DIGEST,
        model_id="gpt-5.6-sol",
        model_profile_fingerprint=_DIGEST,
        served_model=final.served_model,
        api_style="openai_responses",
        endpoint_path="/responses",
        prompt_id="rto-business-intent",
        prompt_version="1.0.0",
        prompt_fingerprint=_DIGEST,
        egress_payload_fingerprint=_DIGEST,
        response_schema_id="domain-model-intent-response",
        response_schema_version="1.0.0",
        response_schema_fingerprint=_DIGEST,
        request_fingerprint=_DIGEST,
        response_fingerprint=_DIGEST if succeeded else None,
        communication_result_fingerprint=_DIGEST if succeeded else None,
        provider_request_id=final.provider_request_id,
        duration_ms=sum(item.duration_ms for item in physical_attempts),
        transport_attempts=len(physical_attempts),
        attempts=physical_attempts,
        input_tokens=final.input_tokens,
        output_tokens=final.output_tokens,
        total_tokens=final.total_tokens,
        error_category=final.error_category,
        error_code=final.error_code,
        http_status=final.http_status,
        retryable=final.retryable,
    )


def _snapshot_material(
    repo_root: Path,
) -> tuple[
    DomainIntentSessionState,
    InvocationEvidence,
    SessionStepState,
    InvocationEvidence,
]:
    communication = build_intent_communication_service(repo_root=repo_root)
    first_request = communication.start(
        session_id="snapshot-session",
        message_id="user-1",
        user_text="降低单位进料炉燃料能耗。",
    )
    repair = communication.evaluate_response(first_request, "{}")
    assert repair.status == "repair_required"
    second_request = communication.build_repair_retry(first_request, repair)
    compiler = PromptCompiler()
    first_prompt = compiler.compile(first_request)
    second_prompt = compiler.compile(second_request)
    first_step = SessionStepState(
        request=first_request,
        approved_egress=first_prompt,
        invocation_id="invocation-1",
        communication_result=None,
    )
    second_step = SessionStepState(
        request=second_request,
        approved_egress=second_prompt,
        invocation_id="invocation-2",
        communication_result=None,
    )
    provider_error = ProviderError(
        category="rate_limit",
        code="provider-rate-limit",
        message="normalized failure for snapshot tests",
        retryable=False,
        http_status=429,
    )
    state = DomainIntentSessionState(
        schema_id=DOMAIN_INTENT_SESSION_SCHEMA_ID,
        schema_version=DOMAIN_INTENT_SESSION_SCHEMA_VERSION,
        session_id="snapshot-session",
        snapshot_index=1,
        previous_manifest_fingerprint=None,
        execution_mode="synthetic_test",
        provider_id="dmx-cn",
        provider_version="1.0.0",
        provider_profile_fingerprint=_DIGEST,
        model_id="gpt-5.6-sol",
        model_profile_fingerprint=_DIGEST,
        capability_manifest_ref=first_request.capability_manifest_ref,
        communication_policy=communication.policy,
        communication_policy_fingerprint=canonical_fingerprint(communication.policy.as_dict()),
        steps=(first_step,),
        status="provider_failed",
        provider_error=provider_error,
        final_communication_result_ref=None,
    )
    first_attempt = replace(
        _failed_attempt(retryable=False),
        provider_request_id="provider-request-1",
    )
    second_attempt = replace(
        _failed_attempt(retryable=False),
        provider_request_id="provider-request-2",
    )
    first_invocation = replace(
        _invocation(status="failed", attempts=(first_attempt,)),
        session_id=state.session_id,
        invocation_id=first_step.invocation_id,
        prompt_id=first_prompt.prompt_id,
        prompt_version=first_prompt.prompt_version,
        prompt_fingerprint=first_prompt.prompt_fingerprint,
        egress_payload_fingerprint=first_prompt.input_fingerprint,
        response_schema_id=first_prompt.schema_id,
        response_schema_version=first_prompt.schema_version,
        response_schema_fingerprint=first_prompt.schema_fingerprint,
        request_fingerprint=first_request.fingerprint,
    )
    second_invocation = replace(
        _invocation(status="failed", attempts=(second_attempt,)),
        session_id=state.session_id,
        invocation_id=second_step.invocation_id,
        prompt_id=second_prompt.prompt_id,
        prompt_version=second_prompt.prompt_version,
        prompt_fingerprint=second_prompt.prompt_fingerprint,
        egress_payload_fingerprint=second_prompt.input_fingerprint,
        response_schema_id=second_prompt.schema_id,
        response_schema_version=second_prompt.schema_version,
        response_schema_fingerprint=second_prompt.schema_fingerprint,
        request_fingerprint=second_request.fingerprint,
    )
    return state, first_invocation, second_step, second_invocation


def _rewrite_snapshot_evidence(
    record_dir: Path,
    evidence: dict[str, object],
) -> None:
    evidence_path = record_dir / "invocations.json"
    manifest_path = record_dir / "manifest.json"
    evidence_payload = canonical_json_bytes(evidence) + b"\n"
    evidence_path.write_bytes(evidence_payload)
    manifest = dict(
        decode_json_object(
            manifest_path.read_bytes(),
            context="test snapshot manifest",
            maximum_bytes=1_000_000,
        )
    )
    files = manifest["files"]
    assert isinstance(files, list)
    evidence_entry = next(
        item for item in files if isinstance(item, dict) and item.get("path") == "invocations.json"
    )
    evidence_entry["size_bytes"] = len(evidence_payload)
    evidence_entry["sha256"] = sha256_bytes(evidence_payload)
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_fingerprint"}
    manifest["manifest_fingerprint"] = canonical_fingerprint(manifest_body)
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")


def test_store_is_fixed_under_runs_and_round_trips_manifest_last(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)

    record = store.write(_invocation())

    assert record.run_dir == tmp_path / "runs/domain_model/session-1"
    assert [item.name for item in record.run_dir.iterdir()] == [
        "invocations.json",
        "manifest.json",
    ]
    assert store.read("session-1") == record
    serialized = record.evidence.as_dict()
    assert "authorization" not in str(serialized).lower()
    assert "chain_of_thought" not in str(serialized).lower()


def test_every_physical_transport_attempt_round_trips_as_safe_metadata(
    tmp_path: Path,
) -> None:
    attempts = (
        _failed_attempt(),
        _succeeded_attempt(
            attempt_index=2,
            duration_ms=83,
            provider_request_id="request-success-2",
        ),
    )
    invocation = _invocation(attempts=attempts)

    record = EvidenceStore(tmp_path).write(invocation)

    reloaded = record.evidence.invocations[0]
    assert reloaded == invocation
    assert reloaded.transport_attempts == 2
    assert reloaded.duration_ms == 123
    assert reloaded.attempts[0].error_category == "rate_limit"
    assert reloaded.attempts[0].http_status == 429
    assert reloaded.attempts[0].retryable is True
    assert reloaded.provider_request_id == "request-success-2"
    serialized = reloaded.as_dict()
    assert "error_message" not in str(serialized).lower()
    assert "raw_response" not in str(serialized).lower()


def test_failed_invocation_preserves_only_normalized_final_error_metadata() -> None:
    final = _failed_attempt(retryable=False)

    invocation = _invocation(status="failed", attempts=(final,))

    assert invocation.error_category == "rate_limit"
    assert invocation.error_code == "provider-rate-limit"
    assert invocation.http_status == 429
    assert invocation.retryable is False
    assert invocation.response_fingerprint is None
    assert invocation.communication_result_fingerprint is None


def test_attempt_contract_rejects_unknown_sensitive_or_reasoning_fields() -> None:
    payload = _failed_attempt().as_dict()
    payload["error_message"] = "Bearer sensitive-token-value"

    with pytest.raises(ValueError, match="fields differ"):
        TransportAttemptEvidence.from_mapping(payload)


def test_attempt_contract_rejects_secret_like_metadata_values() -> None:
    payload = _failed_attempt().as_dict()
    payload["error_code"] = "sk-secretvalue123"

    with pytest.raises(ValueError, match="unsafe|forbidden"):
        TransportAttemptEvidence.from_mapping(payload)


def test_attempt_contract_rejects_inconsistent_usage() -> None:
    payload = _succeeded_attempt().as_dict()
    payload["total_tokens"] = 129

    with pytest.raises(ValueError, match="total_tokens"):
        TransportAttemptEvidence.from_mapping(payload)


def test_invocation_rejects_non_contiguous_transport_attempts() -> None:
    attempts = (_failed_attempt(), _succeeded_attempt(attempt_index=3))

    with pytest.raises(ValueError, match="contiguous"):
        _invocation(attempts=attempts)


def test_invocation_rejects_non_retryable_attempt_before_another_attempt() -> None:
    attempts = (
        _failed_attempt(retryable=False),
        _succeeded_attempt(attempt_index=2),
    )

    with pytest.raises(ValueError, match="retryable failed"):
        _invocation(attempts=attempts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transport_attempts", 2),
        ("duration_ms", 124),
        ("served_model", "another-model"),
        ("provider_request_id", "another-request"),
        ("input_tokens", 101),
        ("output_tokens", 31),
        ("total_tokens", 131),
    ],
)
def test_invocation_rejects_transport_summary_tampering(
    field: str,
    value: object,
) -> None:
    payload = _invocation().as_dict()
    payload[field] = value

    with pytest.raises(ValueError, match="attempts length|attempt total|summary differs"):
        InvocationEvidence.from_mapping(payload)


def test_invocation_rejects_pre_attempt_schema_version() -> None:
    payload = _invocation().as_dict()
    payload["schema_version"] = "1.0.0"

    with pytest.raises(ValueError, match="schema_version differs"):
        InvocationEvidence.from_mapping(payload)


def test_invocation_rejects_unknown_execution_mode() -> None:
    payload = _invocation().as_dict()
    payload["execution_mode"] = "pending"

    with pytest.raises(ValueError, match="execution_mode"):
        InvocationEvidence.from_mapping(payload)


def test_invocation_requires_a_valid_egress_payload_fingerprint() -> None:
    payload = _invocation().as_dict()
    payload["egress_payload_fingerprint"] = "not-a-digest"

    with pytest.raises(ValueError, match="egress_payload_fingerprint"):
        InvocationEvidence.from_mapping(payload)


def test_manifest_failure_leaves_an_explicitly_incomplete_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EvidenceStore(tmp_path)

    def fail_manifest(_run_dir: Path, _payload: bytes) -> None:
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(store, "_publish_manifest", fail_manifest)
    with pytest.raises(OSError, match="simulated"):
        store.write(_invocation())

    run_dir = tmp_path / "runs/domain_model/session-1"
    assert (run_dir / "invocations.json").is_file()
    assert not (run_dir / "manifest.json").exists()
    with pytest.raises(ValueError, match="incomplete"):
        store.read("session-1")


def test_manifest_detects_tampering_and_store_never_overwrites(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    record = store.write(_invocation())

    with pytest.raises(FileExistsError):
        store.write(_invocation())
    evidence_path = record.run_dir / "invocations.json"
    evidence_path.write_bytes(evidence_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="size differs"):
        store.read("session-1")


def test_reader_rejects_rehashed_attempt_index_tampering(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    attempts = (_failed_attempt(), _succeeded_attempt(attempt_index=2))
    record = store.write(_invocation(attempts=attempts))
    evidence_path = record.run_dir / "invocations.json"
    manifest_path = record.run_dir / "manifest.json"
    evidence = dict(
        decode_json_object(
            evidence_path.read_bytes(),
            context="test evidence",
            maximum_bytes=1_000_000,
        )
    )
    invocations = evidence["invocations"]
    assert isinstance(invocations, list)
    invocation = invocations[0]
    assert isinstance(invocation, dict)
    physical_attempts = invocation["attempts"]
    assert isinstance(physical_attempts, list)
    second_attempt = physical_attempts[1]
    assert isinstance(second_attempt, dict)
    second_attempt["attempt_index"] = 3
    evidence_payload = canonical_json_bytes(evidence) + b"\n"
    evidence_path.write_bytes(evidence_payload)

    manifest = dict(
        decode_json_object(
            manifest_path.read_bytes(),
            context="test manifest",
            maximum_bytes=1_000_000,
        )
    )
    files = manifest["files"]
    assert isinstance(files, list)
    file_entry = files[0]
    assert isinstance(file_entry, dict)
    file_entry["size_bytes"] = len(evidence_payload)
    file_entry["sha256"] = sha256_bytes(evidence_payload)
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_fingerprint"}
    manifest["manifest_fingerprint"] = canonical_fingerprint(manifest_body)
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")

    with pytest.raises(ValueError, match="contiguous"):
        store.read("session-1")


def test_snapshot_writer_rejects_rewriting_prior_attempt_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    state, first_invocation, second_step, second_invocation = _snapshot_material(repo_root)
    store = EvidenceStore(tmp_path)
    first_record = store.write_snapshot(state, (first_invocation,))
    second_state = replace(
        state,
        snapshot_index=2,
        previous_manifest_fingerprint=first_record.manifest_fingerprint,
        steps=(*state.steps, second_step),
    )
    rewritten_attempt = replace(
        first_invocation.attempts[0],
        provider_request_id="rewritten-provider-request",
    )
    rewritten_invocation = replace(
        first_invocation,
        provider_request_id=rewritten_attempt.provider_request_id,
        attempts=(rewritten_attempt,),
    )

    with pytest.raises(ValueError, match="evidence does not append"):
        store.write_snapshot(
            second_state,
            (rewritten_invocation, second_invocation),
        )


def test_snapshot_reader_rejects_rehashed_prior_attempt_rewrite(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    state, first_invocation, second_step, second_invocation = _snapshot_material(repo_root)
    store = EvidenceStore(tmp_path)
    first_record = store.write_snapshot(state, (first_invocation,))
    second_state = replace(
        state,
        snapshot_index=2,
        previous_manifest_fingerprint=first_record.manifest_fingerprint,
        steps=(*state.steps, second_step),
    )
    second_record = store.write_snapshot(
        second_state,
        (first_invocation, second_invocation),
    )
    evidence = dict(
        decode_json_object(
            (second_record.run_dir / "invocations.json").read_bytes(),
            context="test snapshot evidence",
            maximum_bytes=1_000_000,
        )
    )
    invocations = evidence["invocations"]
    assert isinstance(invocations, list)
    first = invocations[0]
    assert isinstance(first, dict)
    attempts = first["attempts"]
    assert isinstance(attempts, list)
    first_attempt = attempts[0]
    assert isinstance(first_attempt, dict)
    first["provider_request_id"] = "rewritten-provider-request"
    first_attempt["provider_request_id"] = "rewritten-provider-request"
    _rewrite_snapshot_evidence(second_record.run_dir, evidence)

    with pytest.raises(ValueError, match="evidence does not append"):
        store.read_snapshot(second_record.run_dir / "manifest.json")


@pytest.mark.parametrize(
    "field",
    ["request_fingerprint", "egress_payload_fingerprint", "model_profile_fingerprint"],
)
def test_snapshot_reader_rejects_rehashed_cross_file_evidence_tampering(
    field: str,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    state, first_invocation, _, _ = _snapshot_material(repo_root)
    store = EvidenceStore(tmp_path)
    record = store.write_snapshot(state, (first_invocation,))
    evidence = dict(
        decode_json_object(
            (record.run_dir / "invocations.json").read_bytes(),
            context="test snapshot evidence",
            maximum_bytes=1_000_000,
        )
    )
    invocations = evidence["invocations"]
    assert isinstance(invocations, list)
    invocation = invocations[0]
    assert isinstance(invocation, dict)
    invocation[field] = "b" * 64
    _rewrite_snapshot_evidence(record.run_dir, evidence)

    with pytest.raises(ValueError, match="differs from its state"):
        store.read_snapshot(record.run_dir / "manifest.json")


@pytest.mark.parametrize("field", ["authorization", "chain_of_thought", "raw_response"])
def test_evidence_contract_rejects_sensitive_or_reasoning_fields(field: str) -> None:
    payload = _invocation().as_dict()
    payload[field] = "must-not-be-recorded"

    with pytest.raises(ValueError, match="fields differ"):
        InvocationEvidence.from_mapping(payload)


def test_evidence_rejects_secret_like_provider_request_id() -> None:
    payload = _invocation().as_dict()
    payload["provider_request_id"] = "Bearer secret-token-value"

    with pytest.raises(ValueError, match="unsafe|forbidden"):
        InvocationEvidence.from_mapping(payload)


def test_preflight_failure_records_zero_physical_attempts() -> None:
    failed = _invocation(
        status="failed",
        attempts=(_failed_attempt(retryable=False),),
    )
    preflight = replace(
        failed,
        served_model=None,
        provider_request_id=None,
        duration_ms=0,
        transport_attempts=0,
        attempts=(),
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
    )

    assert preflight.transport_attempts == 0
    assert preflight.attempts == ()


def test_evidence_store_rejects_oversized_payload_before_creating_run(
    tmp_path: Path,
) -> None:
    session_id = "oversized-session"
    template = _invocation(session_id=session_id)
    invocations = tuple(
        replace(template, invocation_id=f"invocation-{index}") for index in range(1, 701)
    )
    store = EvidenceStore(tmp_path)

    with pytest.raises(ValueError, match="1 MB evidence limit"):
        store.write_session(session_id, invocations)

    assert not store.root.exists()


@pytest.mark.parametrize("filename", ["manifest.json", "invocations.json"])
def test_evidence_store_rejects_oversized_files_before_json_decode(
    tmp_path: Path,
    filename: str,
) -> None:
    store = EvidenceStore(tmp_path)
    record = store.write_session(
        "bounded-read-session",
        (_invocation(session_id="bounded-read-session"),),
    )
    (record.run_dir / filename).write_bytes(b"x" * 1_000_001)

    with pytest.raises(ValueError, match="1 MB evidence limit"):
        store.read_session("bounded-read-session")
