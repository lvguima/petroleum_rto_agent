"""Bounded orchestration from natural language to the strict RTO intent contract."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol, cast, runtime_checkable
from uuid import uuid4

from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    ClarificationAnswer,
    CommunicationResult,
    DomainModelInvocationResult,
    DomainModelPort,
    DomainModelRequest,
    IntentCommunicationService,
    ProviderError,
)

from ._json import canonical_fingerprint, identifier
from .egress import EgressViolation
from .evidence import (
    EVIDENCE_SCHEMA_VERSION,
    INVOCATION_EVIDENCE_SCHEMA_ID,
    TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID,
    EvidenceRecord,
    EvidenceStore,
    InvocationEvidence,
    TransportAttemptEvidence,
)
from .models import ModelProfile, ProviderProfile
from .prompt import CompiledPrompt, PromptCompiler
from .session import (
    DOMAIN_INTENT_SESSION_SCHEMA_ID,
    DOMAIN_INTENT_SESSION_SCHEMA_VERSION,
    DomainIntentSessionState,
    SessionStatus,
    SessionStepState,
)

type DomainIntentRuntimeStatus = Literal[
    "resolved",
    "needs_clarification",
    "unsupported",
    "failed",
    "provider_failed",
    "egress_blocked",
]

_SAFE_EVIDENCE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SENSITIVE_EVIDENCE_TEXT = re.compile(
    r"(?i)(?:\bsk[-_][A-Za-z0-9_-]{8,}|api[_-]?key|access[_-]?token|secret|password)"
)


def _safe_evidence_text(value: str | None) -> str | None:
    if value is None:
        return None
    return (
        value
        if _SAFE_EVIDENCE_TEXT.fullmatch(value) and not _SENSITIVE_EVIDENCE_TEXT.search(value)
        else None
    )


def _safe_served_model(value: str) -> str:
    return _safe_evidence_text(value) or "unsafe-served-model-redacted"


def _safe_provider_error(error: ProviderError) -> ProviderError:
    safe_code = _safe_evidence_text(error.code) or "provider-error-code-redacted"
    return ProviderError(
        category=error.category,
        code=safe_code,
        message=f"domain-model provider reported a {error.category} failure",
        retryable=error.retryable,
        http_status=error.http_status,
    )


def _local_failure_invocation(
    request: DomainModelRequest,
    error: ProviderError,
) -> DomainModelInvocationResult:
    return DomainModelInvocationResult(
        schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
        schema_version=COMMUNICATION_SCHEMA_VERSION,
        invocation_id=f"local-failure-{uuid4().hex}",
        request_ref=request.ref,
        status="failed",
        attempts=(),
        response=None,
        error=error,
    )


@runtime_checkable
class _DeadlineAwareDomainModelPort(Protocol):
    """Optional provider capability for enforcing the caller's remaining round budget."""

    def invoke_with_timeout(
        self,
        request: DomainModelRequest,
        *,
        timeout_seconds: float,
    ) -> DomainModelInvocationResult: ...


def _new_session_id() -> str:
    return f"intent-session-{uuid4().hex}"


@dataclass(frozen=True)
class RuntimeStep:
    """One semantic model attempt and the deterministic interpretation of its result."""

    request: DomainModelRequest
    compiled_prompt: CompiledPrompt
    invocation: DomainModelInvocationResult
    communication_result: CommunicationResult | None
    boundary_error: ProviderError | None = None

    def __post_init__(self) -> None:
        if self.compiled_prompt.request_fingerprint != self.request.fingerprint:
            raise ValueError("compiled prompt references another domain-model request")
        if self.invocation.request_ref != self.request.ref and (
            self.boundary_error is None
            or self.boundary_error.code != "invocation-request-ref-mismatch"
        ):
            raise ValueError("provider invocation references another domain-model request")
        if self.communication_result is not None:
            if self.communication_result.request_ref != self.request.ref:
                raise ValueError("communication result references another domain-model request")
            if self.invocation.status != "succeeded":
                raise ValueError("a failed provider invocation cannot have a communication result")
            if self.boundary_error is not None:
                raise ValueError("a communication result cannot overlap a boundary error")
        if self.boundary_error is not None and not isinstance(self.boundary_error, ProviderError):
            raise TypeError("boundary_error must be ProviderError or None")

    def as_dict(self) -> dict[str, object]:
        """Return only egress-approved and locally normalized session state."""

        invocation = self.invocation
        safe_attempts = []
        for item in invocation.attempts:
            safe_error = None if item.error is None else _safe_provider_error(item.error)
            safe_attempts.append(
                {
                    "attempt_index": item.attempt_index,
                    "provider_id": item.provider_id,
                    "provider_version": item.provider_version,
                    "status": item.status,
                    "provider_request_id": _safe_evidence_text(item.provider_request_id),
                    "served_model": _safe_evidence_text(item.served_model),
                    "finish_reason": _safe_evidence_text(item.finish_reason),
                    "duration_ms": item.duration_ms,
                    "usage": None if item.usage is None else item.usage.as_dict(),
                    "error": (
                        None
                        if safe_error is None
                        else {
                            "category": safe_error.category,
                            "code": safe_error.code,
                            "retryable": safe_error.retryable,
                            "http_status": safe_error.http_status,
                        }
                    ),
                }
            )
        return {
            "request": self.request.as_dict(),
            "approved_egress": self.compiled_prompt.as_dict(),
            "invocation_summary": {
                "schema_id": invocation.schema_id,
                "schema_version": invocation.schema_version,
                "invocation_id": invocation.invocation_id,
                "request_ref": invocation.request_ref.as_dict(),
                "status": invocation.status,
                "attempts": safe_attempts,
                "response_included": False,
                "response_fingerprint": (
                    None
                    if invocation.response is None
                    else canonical_fingerprint(invocation.response)
                ),
                "error": (
                    None
                    if invocation.error is None
                    else {
                        "category": invocation.error.category,
                        "code": _safe_provider_error(invocation.error).code,
                        "retryable": invocation.error.retryable,
                        "http_status": invocation.error.http_status,
                    }
                ),
            },
            "communication_result": (
                None if self.communication_result is None else self.communication_result.as_dict()
            ),
            "boundary_error": (
                None
                if self.boundary_error is None
                else {
                    "category": self.boundary_error.category,
                    "code": _safe_provider_error(self.boundary_error).code,
                    "retryable": self.boundary_error.retryable,
                    "http_status": self.boundary_error.http_status,
                }
            ),
        }


@dataclass(frozen=True)
class DomainIntentOutcome:
    """Final outcome of one user turn; provider and business failures cannot overlap."""

    session_id: str
    provider_id: str
    provider_version: str
    model_id: str
    status: DomainIntentRuntimeStatus
    steps: tuple[RuntimeStep, ...]
    communication_result: CommunicationResult | None
    provider_error: ProviderError | None
    evidence_manifest: Path | None
    evidence_fingerprint: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", identifier(self.session_id, context="session_id"))
        steps = tuple(self.steps)
        if any(not isinstance(item, RuntimeStep) for item in steps):
            raise TypeError("steps must contain RuntimeStep values")
        object.__setattr__(self, "steps", steps)
        if self.status in {"provider_failed", "egress_blocked"}:
            if not isinstance(self.provider_error, ProviderError):
                raise ValueError("provider or egress failure requires ProviderError")
            if self.communication_result is not None:
                raise ValueError("provider or egress failure cannot contain a business result")
        else:
            if self.provider_error is not None:
                raise ValueError("a business result cannot contain a provider error")
            if not isinstance(self.communication_result, CommunicationResult):
                raise ValueError("a business outcome requires CommunicationResult")
            if self.communication_result.status != self.status:
                raise ValueError("runtime status differs from the communication result")
        if (self.evidence_manifest is None) != (self.evidence_fingerprint is None):
            raise ValueError(
                "evidence path and fingerprint must either both exist or both be absent"
            )

    @property
    def final_request(self) -> DomainModelRequest | None:
        return None if not self.steps else self.steps[-1].request

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": "domain-intent-runtime-outcome",
            "schema_version": "1.0.0",
            "session_id": self.session_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "model_id": self.model_id,
            "status": self.status,
            "provider_error": (
                None if self.provider_error is None else self.provider_error.as_dict()
            ),
            "communication_result": (
                None if self.communication_result is None else self.communication_result.as_dict()
            ),
            "evidence_manifest": (
                None if self.evidence_manifest is None else str(self.evidence_manifest)
            ),
            "evidence_fingerprint": self.evidence_fingerprint,
            "semantic_attempts": len(self.steps),
        }


class DomainIntentRuntime:
    """Call one pinned provider/model and pass only successful output to D0 validation."""

    def __init__(
        self,
        *,
        provider_profile: ProviderProfile,
        model_profile: ModelProfile,
        port: DomainModelPort,
        communication_service: IntentCommunicationService,
        prompt_compiler: PromptCompiler,
        evidence_store: EvidenceStore | None = None,
        execution_mode: Literal["production", "validation", "synthetic_test"] = "production",
        clock: Callable[[], float] = monotonic,
        session_id_factory: Callable[[], str] = _new_session_id,
    ) -> None:
        if provider_profile.model(model_profile.model_id) != model_profile:
            raise ValueError("model profile is not pinned by the provider profile")
        if port.provider_id != provider_profile.provider_id:
            raise ValueError("domain-model port provider_id differs from its profile")
        if port.provider_version != provider_profile.profile_version:
            raise ValueError("domain-model port provider_version differs from its profile")
        self._provider = provider_profile
        self._model = model_profile
        self._port = port
        self._communication = communication_service
        self._compiler = prompt_compiler
        self._evidence_store = evidence_store
        if execution_mode not in {"production", "validation", "synthetic_test"}:
            raise ValueError("execution_mode is unsupported")
        self._execution_mode = execution_mode
        self._clock = clock
        self._session_id_factory = session_id_factory

    @property
    def provider_profile(self) -> ProviderProfile:
        return self._provider

    @property
    def model_profile(self) -> ModelProfile:
        return self._model

    def interpret(
        self,
        user_text: str,
        *,
        session_id: str | None = None,
        message_id: str = "user-1",
    ) -> DomainIntentOutcome:
        normalized_session = identifier(
            self._session_id_factory() if session_id is None else session_id,
            context="session_id",
        )
        request = self._communication.start(
            session_id=normalized_session,
            message_id=message_id,
            user_text=user_text,
        )
        return self.execute_request(request)

    def execute_request(self, request: DomainModelRequest) -> DomainIntentOutcome:
        """Execute one user turn, including at most one policy-authorized semantic repair."""

        if self._evidence_store is None:
            return self._execute_request(request, previous_record=None)
        with self._evidence_store.initial_session_guard(request.session_id):
            return self._execute_request(request, previous_record=None)

    def continue_session(
        self,
        record: EvidenceRecord,
        *,
        message_id: str,
        user_text: str,
        answers: Sequence[ClarificationAnswer],
    ) -> DomainIntentOutcome:
        """Resume only a strictly verified clarification snapshot with the same model."""

        if self._evidence_store is None:
            raise ValueError("resumable sessions require an evidence store")
        with self._evidence_store.continuation_guard(record) as current:
            return self._continue_locked_session(
                current,
                message_id=message_id,
                user_text=user_text,
                answers=answers,
            )

    def _continue_locked_session(
        self,
        record: EvidenceRecord,
        *,
        message_id: str,
        user_text: str,
        answers: Sequence[ClarificationAnswer],
    ) -> DomainIntentOutcome:
        """Build, invoke, and publish one continuation while its session lock is held."""

        state = record.session_state
        if state is None:
            raise ValueError("evidence record is not a resumable domain-intent snapshot")
        self._validate_resumption_state(state)
        if state.status != "needs_clarification":
            raise ValueError("only a needs_clarification session can be continued")
        previous_step = state.steps[-1]
        previous_result = previous_step.communication_result
        assert previous_result is not None
        request = self._communication.build_clarification_followup(
            previous_step.request,
            previous_result,
            message_id=message_id,
            user_text=user_text,
            answers=answers,
        )
        return self._execute_request(request, previous_record=record)

    def _execute_request(
        self,
        request: DomainModelRequest,
        *,
        previous_record: EvidenceRecord | None,
    ) -> DomainIntentOutcome:
        """Run the current user turn while carrying only verified prior snapshot state."""

        started = self._clock()
        steps: list[RuntimeStep] = []
        current = request
        while True:
            remaining_seconds = self._provider.round_timeout_seconds - (self._clock() - started)
            if remaining_seconds <= 0:
                return self._provider_failure(
                    request.session_id,
                    steps,
                    ProviderError(
                        category="timeout",
                        code="round-deadline-exceeded",
                        message="domain-model user-turn deadline was exceeded",
                        retryable=False,
                        http_status=None,
                    ),
                    previous_record=previous_record,
                )
            try:
                compiled = self._compiler.compile(current)
            except EgressViolation as exc:
                return DomainIntentOutcome(
                    session_id=request.session_id,
                    provider_id=self._provider.provider_id,
                    provider_version=self._provider.profile_version,
                    model_id=self._model.model_id,
                    status="egress_blocked",
                    steps=tuple(steps),
                    communication_result=None,
                    provider_error=ProviderError(
                        category="invalid_request",
                        code=f"egress-{exc.code}",
                        message="domain-model request was blocked by outbound policy",
                        retryable=False,
                        http_status=None,
                    ),
                    evidence_manifest=None,
                    evidence_fingerprint=None,
                )
            remaining_seconds = self._provider.round_timeout_seconds - (self._clock() - started)
            if remaining_seconds <= 0:
                return self._provider_failure(
                    request.session_id,
                    steps,
                    ProviderError(
                        category="timeout",
                        code="round-deadline-exceeded",
                        message="domain-model user-turn deadline was exceeded",
                        retryable=False,
                        http_status=None,
                    ),
                    previous_record=previous_record,
                )
            try:
                invocation = (
                    self._port.invoke_with_timeout(
                        current,
                        timeout_seconds=remaining_seconds,
                    )
                    if isinstance(self._port, _DeadlineAwareDomainModelPort)
                    else self._port.invoke(current)
                )
            except Exception as exc:  # noqa: BLE001 - provider boundary must return structure
                error = ProviderError(
                    category="transport",
                    code="adapter-raised-exception",
                    message=(
                        "domain-model adapter failed before returning evidence "
                        f"({type(exc).__name__})"
                    ),
                    retryable=False,
                    http_status=None,
                )
                steps.append(
                    RuntimeStep(
                        current,
                        compiled,
                        _local_failure_invocation(current, error),
                        None,
                    )
                )
                return self._provider_failure(
                    request.session_id,
                    steps,
                    error,
                    previous_record=previous_record,
                )
            if not isinstance(invocation, DomainModelInvocationResult):
                invalid_result_error = ProviderError(
                    category="protocol",
                    code="adapter-invalid-result",
                    message="domain-model adapter returned an invalid invocation result",
                    retryable=False,
                    http_status=None,
                )
                steps.append(
                    RuntimeStep(
                        current,
                        compiled,
                        _local_failure_invocation(current, invalid_result_error),
                        None,
                    )
                )
                return self._provider_failure(
                    request.session_id,
                    steps,
                    invalid_result_error,
                    previous_record=previous_record,
                )
            if self._clock() - started >= self._provider.round_timeout_seconds:
                timeout_error = ProviderError(
                    category="timeout",
                    code="round-deadline-exceeded",
                    message="domain-model user-turn deadline was exceeded",
                    retryable=False,
                    http_status=None,
                )
                steps.append(RuntimeStep(current, compiled, invocation, None, timeout_error))
                return self._provider_failure(
                    request.session_id,
                    steps,
                    timeout_error,
                    previous_record=previous_record,
                )
            boundary_error = self._validate_invocation(current, invocation)
            if boundary_error is not None:
                steps.append(RuntimeStep(current, compiled, invocation, None, boundary_error))
                return self._provider_failure(
                    request.session_id,
                    steps,
                    boundary_error,
                    previous_record=previous_record,
                )
            if invocation.status == "failed":
                assert invocation.error is not None
                if (
                    invocation.error.category == "invalid_request"
                    and invocation.error.code.startswith("egress-")
                ):
                    return DomainIntentOutcome(
                        session_id=request.session_id,
                        provider_id=self._provider.provider_id,
                        provider_version=self._provider.profile_version,
                        model_id=self._model.model_id,
                        status="egress_blocked",
                        steps=tuple(steps),
                        communication_result=None,
                        provider_error=_safe_provider_error(invocation.error),
                        evidence_manifest=None,
                        evidence_fingerprint=None,
                    )
                steps.append(RuntimeStep(current, compiled, invocation, None))
                return self._provider_failure(
                    request.session_id,
                    steps,
                    invocation.error,
                    previous_record=previous_record,
                )
            assert invocation.response is not None
            communication = self._communication.evaluate_response(current, invocation.response)
            steps.append(RuntimeStep(current, compiled, invocation, communication))
            if communication.status != "repair_required":
                return self._business_outcome(
                    request.session_id,
                    steps,
                    communication,
                    previous_record=previous_record,
                )
            current = self._communication.build_repair_retry(current, communication)

    def _validate_invocation(
        self,
        request: DomainModelRequest,
        invocation: DomainModelInvocationResult,
    ) -> ProviderError | None:
        if invocation.request_ref != request.ref:
            return ProviderError(
                category="protocol",
                code="invocation-request-ref-mismatch",
                message="provider invocation references another domain-model request",
                retryable=False,
                http_status=None,
            )
        if len(invocation.attempts) > self._provider.maximum_physical_attempts:
            return ProviderError(
                category="protocol",
                code="invocation-attempt-limit-exceeded",
                message="provider invocation exceeded the pinned physical-attempt limit",
                retryable=False,
                http_status=None,
            )
        for item in invocation.attempts[:-1]:
            error = item.error
            assert error is not None
            retry_allowed = (
                (
                    error.category == "transport"
                    and error.code == "transport-connect"
                    and error.http_status is None
                )
                or (error.category == "rate_limit" and error.http_status == 429)
                or (
                    error.category == "provider_server"
                    and error.http_status is not None
                    and 500 <= error.http_status <= 599
                )
            )
            if not retry_allowed:
                return ProviderError(
                    category="protocol",
                    code="invocation-retry-policy-violation",
                    message="provider invocation used a retry outside the pinned policy",
                    retryable=False,
                    http_status=None,
                )
        if any(
            item.provider_id != self._provider.provider_id
            or item.provider_version != self._provider.profile_version
            for item in invocation.attempts
        ):
            return ProviderError(
                category="protocol",
                code="invocation-provider-mismatch",
                message="provider invocation evidence differs from the pinned provider",
                retryable=False,
                http_status=None,
            )
        if invocation.status == "succeeded":
            served_model = invocation.attempts[-1].served_model
            if served_model not in self._model.allowed_served_model_ids:
                return ProviderError(
                    category="model_mismatch",
                    code="served-model-mismatch",
                    message="provider served a model different from the pinned model",
                    retryable=False,
                    http_status=None,
                )
        return None

    def _business_outcome(
        self,
        session_id: str,
        steps: list[RuntimeStep],
        communication: CommunicationResult,
        *,
        previous_record: EvidenceRecord | None,
    ) -> DomainIntentOutcome:
        if communication.status == "repair_required":
            raise ValueError("repair_required is not a final runtime outcome")
        record = self._persist(
            session_id,
            steps,
            status=cast(SessionStatus, communication.status),
            communication=communication,
            provider_error=None,
            previous_record=previous_record,
        )
        return DomainIntentOutcome(
            session_id=session_id,
            provider_id=self._provider.provider_id,
            provider_version=self._provider.profile_version,
            model_id=self._model.model_id,
            status=cast(DomainIntentRuntimeStatus, communication.status),
            steps=tuple(steps),
            communication_result=communication,
            provider_error=None,
            evidence_manifest=(None if record is None else record.run_dir / "manifest.json"),
            evidence_fingerprint=(None if record is None else record.manifest_fingerprint),
        )

    def _provider_failure(
        self,
        session_id: str,
        steps: list[RuntimeStep],
        error: ProviderError,
        *,
        previous_record: EvidenceRecord | None,
    ) -> DomainIntentOutcome:
        error = _safe_provider_error(error)
        record = self._persist(
            session_id,
            steps,
            status="provider_failed",
            communication=None,
            provider_error=error,
            previous_record=previous_record,
        )
        return DomainIntentOutcome(
            session_id=session_id,
            provider_id=self._provider.provider_id,
            provider_version=self._provider.profile_version,
            model_id=self._model.model_id,
            status="provider_failed",
            steps=tuple(steps),
            communication_result=None,
            provider_error=error,
            evidence_manifest=(None if record is None else record.run_dir / "manifest.json"),
            evidence_fingerprint=(None if record is None else record.manifest_fingerprint),
        )

    def _persist(
        self,
        session_id: str,
        steps: list[RuntimeStep],
        *,
        status: SessionStatus,
        communication: CommunicationResult | None,
        provider_error: ProviderError | None,
        previous_record: EvidenceRecord | None,
    ) -> EvidenceRecord | None:
        if self._evidence_store is None or not steps:
            return None
        previous_state = None if previous_record is None else previous_record.session_state
        if previous_record is not None and previous_state is None:
            raise ValueError("previous evidence record has no resumable session state")
        prior_steps = () if previous_state is None else previous_state.steps
        prior_evidence = () if previous_record is None else previous_record.evidence.invocations
        new_step_states = tuple(
            SessionStepState(
                request=item.request,
                approved_egress=item.compiled_prompt,
                invocation_id=item.invocation.invocation_id,
                communication_result=item.communication_result,
            )
            for item in steps
        )
        state = DomainIntentSessionState(
            schema_id=DOMAIN_INTENT_SESSION_SCHEMA_ID,
            schema_version=DOMAIN_INTENT_SESSION_SCHEMA_VERSION,
            session_id=session_id,
            snapshot_index=1 if previous_state is None else previous_state.snapshot_index + 1,
            previous_manifest_fingerprint=(
                None if previous_record is None else previous_record.manifest_fingerprint
            ),
            execution_mode=self._execution_mode,
            provider_id=self._provider.provider_id,
            provider_version=self._provider.profile_version,
            provider_profile_fingerprint=self._provider.fingerprint,
            model_id=self._model.model_id,
            model_profile_fingerprint=self._model.fingerprint,
            capability_manifest_ref=self._communication.capability_manifest.ref,
            communication_policy=self._communication.policy,
            communication_policy_fingerprint=canonical_fingerprint(
                self._communication.policy.as_dict()
            ),
            steps=(*prior_steps, *new_step_states),
            status=status,
            provider_error=provider_error,
            final_communication_result_ref=(None if communication is None else communication.ref),
        )
        new_evidence = tuple(self._invocation_evidence(session_id, item) for item in steps)
        return self._evidence_store.write_snapshot(state, (*prior_evidence, *new_evidence))

    def _validate_resumption_state(self, state: DomainIntentSessionState) -> None:
        if state.provider_id != self._provider.provider_id:
            raise ValueError("session provider differs from the configured provider")
        if state.provider_version != self._provider.profile_version:
            raise ValueError("session provider version differs from the configured provider")
        if state.provider_profile_fingerprint != self._provider.fingerprint:
            raise ValueError("session provider profile has changed")
        if state.model_id != self._model.model_id:
            raise ValueError("session model differs from the configured model")
        if state.model_profile_fingerprint != self._model.fingerprint:
            raise ValueError("session model profile has changed")
        if state.capability_manifest_ref != self._communication.capability_manifest.ref:
            raise ValueError("session capability manifest has changed")
        if state.communication_policy != self._communication.policy:
            raise ValueError("session communication policy has changed")
        if state.execution_mode != self._execution_mode:
            raise ValueError("session execution mode differs from the configured runtime")

    def _invocation_evidence(
        self,
        session_id: str,
        step: RuntimeStep,
    ) -> InvocationEvidence:
        attempt = None if not step.invocation.attempts else step.invocation.attempts[-1]
        usage = None if attempt is None else attempt.usage
        response_fingerprint = (
            None
            if step.invocation.response is None
            else canonical_fingerprint(step.invocation.response)
        )
        raw_error = step.boundary_error or step.invocation.error
        error = None if raw_error is None else _safe_provider_error(raw_error)
        attempt_evidence = tuple(
            TransportAttemptEvidence(
                schema_id=TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID,
                schema_version=EVIDENCE_SCHEMA_VERSION,
                attempt_index=item.attempt_index,
                status=item.status,
                provider_request_id=_safe_evidence_text(item.provider_request_id),
                served_model=(
                    None if item.served_model is None else _safe_served_model(item.served_model)
                ),
                duration_ms=item.duration_ms,
                input_tokens=None if item.usage is None else item.usage.input_tokens,
                output_tokens=None if item.usage is None else item.usage.output_tokens,
                total_tokens=None if item.usage is None else item.usage.total_tokens,
                error_category=None if item.error is None else item.error.category,
                error_code=(None if item.error is None else _safe_provider_error(item.error).code),
                http_status=None if item.error is None else item.error.http_status,
                retryable=None if item.error is None else item.error.retryable,
            )
            for item in step.invocation.attempts
        )
        return InvocationEvidence(
            schema_id=INVOCATION_EVIDENCE_SCHEMA_ID,
            schema_version=EVIDENCE_SCHEMA_VERSION,
            session_id=session_id,
            invocation_id=step.invocation.invocation_id,
            status=(
                "blocked"
                if step.boundary_error is not None
                else ("succeeded" if step.invocation.status == "succeeded" else "failed")
            ),
            execution_mode=self._execution_mode,
            provider_id=self._provider.provider_id,
            provider_version=self._provider.profile_version,
            provider_profile_fingerprint=self._provider.fingerprint,
            model_id=self._model.model_id,
            model_profile_fingerprint=self._model.fingerprint,
            served_model=(
                None
                if attempt is None or attempt.served_model is None
                else _safe_served_model(attempt.served_model)
            ),
            api_style=self._model.api_style,
            endpoint_path=self._model.endpoint_path,
            prompt_id=step.compiled_prompt.prompt_id,
            prompt_version=step.compiled_prompt.prompt_version,
            prompt_fingerprint=step.compiled_prompt.prompt_fingerprint,
            egress_payload_fingerprint=step.compiled_prompt.input_fingerprint,
            response_schema_id=step.compiled_prompt.schema_id,
            response_schema_version=step.compiled_prompt.schema_version,
            response_schema_fingerprint=step.compiled_prompt.schema_fingerprint,
            request_fingerprint=step.request.fingerprint,
            response_fingerprint=response_fingerprint,
            communication_result_fingerprint=(
                None if step.communication_result is None else step.communication_result.fingerprint
            ),
            provider_request_id=(
                None if attempt is None else _safe_evidence_text(attempt.provider_request_id)
            ),
            duration_ms=sum(item.duration_ms for item in step.invocation.attempts),
            transport_attempts=len(step.invocation.attempts),
            attempts=attempt_evidence,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            total_tokens=None if usage is None else usage.total_tokens,
            error_category=None if error is None else error.category,
            error_code=None if error is None else error.code,
            http_status=None if error is None else error.http_status,
            retryable=None if error is None else error.retryable,
        )
