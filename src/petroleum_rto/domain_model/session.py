"""Strict resumable session snapshots without raw provider text or credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

from petroleum_rto.rto.communication import (
    CommunicationResult,
    ContractRef,
    DomainModelRequest,
    IntentCommunicationPolicy,
    ProviderError,
)

from ._json import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    identifier,
    integer,
    strict_keys,
    version,
)
from .prompt import CompiledPrompt

DOMAIN_INTENT_SESSION_SCHEMA_ID: Final[str] = "domain-intent-session-snapshot"
DOMAIN_INTENT_SESSION_SCHEMA_VERSION: Final[str] = "1.1.0"

type SessionExecutionMode = Literal["production", "validation", "synthetic_test"]

type SessionStatus = Literal[
    "resolved",
    "needs_clarification",
    "unsupported",
    "failed",
    "provider_failed",
]

_SESSION_STATUSES = {
    "resolved",
    "needs_clarification",
    "unsupported",
    "failed",
    "provider_failed",
}


def _session_status(value: object) -> SessionStatus:
    if value not in _SESSION_STATUSES:
        raise ValueError("session status is unsupported")
    assert isinstance(value, str)
    return cast(SessionStatus, value)


def _execution_mode(value: object) -> SessionExecutionMode:
    if value not in {"production", "validation", "synthetic_test"}:
        raise ValueError("session execution_mode is unsupported")
    assert isinstance(value, str)
    return value


@dataclass(frozen=True)
class SessionStepState:
    """Safe state needed to inspect or resume a semantic attempt."""

    request: DomainModelRequest
    approved_egress: CompiledPrompt
    invocation_id: str
    communication_result: CommunicationResult | None

    def __post_init__(self) -> None:
        if not isinstance(self.request, DomainModelRequest):
            raise TypeError("session step request must be DomainModelRequest")
        if not isinstance(self.approved_egress, CompiledPrompt):
            raise TypeError("session step approved_egress must be CompiledPrompt")
        if self.approved_egress.request_fingerprint != self.request.fingerprint:
            raise ValueError("approved egress references another domain-model request")
        object.__setattr__(
            self,
            "invocation_id",
            identifier(self.invocation_id, context="invocation_id"),
        )
        if self.communication_result is not None:
            if not isinstance(self.communication_result, CommunicationResult):
                raise TypeError("communication_result must be CommunicationResult or None")
            if self.communication_result.request_ref != self.request.ref:
                raise ValueError("communication result references another domain-model request")

    @classmethod
    def from_mapping(cls, value: object) -> SessionStepState:
        raw = as_mapping(value, context="domain intent session step")
        strict_keys(
            raw,
            required={
                "request",
                "approved_egress",
                "invocation_id",
                "communication_result",
            },
            context="domain intent session step",
        )
        result = raw["communication_result"]
        return cls(
            request=DomainModelRequest.from_mapping(raw["request"]),
            approved_egress=CompiledPrompt.from_mapping(raw["approved_egress"]),
            invocation_id=identifier(raw["invocation_id"], context="invocation_id"),
            communication_result=(
                None if result is None else CommunicationResult.from_mapping(result)
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "request": self.request.as_dict(),
            "approved_egress": self.approved_egress.as_dict(),
            "invocation_id": self.invocation_id,
            "communication_result": (
                None if self.communication_result is None else self.communication_result.as_dict()
            ),
        }


@dataclass(frozen=True)
class DomainIntentSessionState:
    """One manifest-addressed snapshot; later snapshots preserve the same provider/model."""

    schema_id: str
    schema_version: str
    session_id: str
    snapshot_index: int
    previous_manifest_fingerprint: str | None
    execution_mode: SessionExecutionMode
    provider_id: str
    provider_version: str
    provider_profile_fingerprint: str
    model_id: str
    model_profile_fingerprint: str
    capability_manifest_ref: ContractRef
    communication_policy: IntentCommunicationPolicy
    communication_policy_fingerprint: str
    steps: tuple[SessionStepState, ...]
    status: SessionStatus
    provider_error: ProviderError | None
    final_communication_result_ref: ContractRef | None

    def __post_init__(self) -> None:
        if self.schema_id != DOMAIN_INTENT_SESSION_SCHEMA_ID:
            raise ValueError("schema_id differs from the domain intent session contract")
        if self.schema_version != DOMAIN_INTENT_SESSION_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the domain intent session contract")
        object.__setattr__(self, "execution_mode", _execution_mode(self.execution_mode))
        for field_name in ("session_id", "provider_id", "model_id"):
            object.__setattr__(
                self,
                field_name,
                identifier(getattr(self, field_name), context=field_name),
            )
        object.__setattr__(
            self,
            "provider_version",
            version(self.provider_version, context="provider_version"),
        )
        object.__setattr__(
            self,
            "snapshot_index",
            integer(self.snapshot_index, context="snapshot_index", minimum=1),
        )
        if self.snapshot_index == 1:
            if self.previous_manifest_fingerprint is not None:
                raise ValueError("the first snapshot cannot reference a previous manifest")
        elif self.previous_manifest_fingerprint is None:
            raise ValueError("later snapshots require previous_manifest_fingerprint")
        if self.previous_manifest_fingerprint is not None:
            digest(
                self.previous_manifest_fingerprint,
                context="previous_manifest_fingerprint",
            )
        for field_name in (
            "provider_profile_fingerprint",
            "model_profile_fingerprint",
            "communication_policy_fingerprint",
        ):
            digest(getattr(self, field_name), context=field_name)
        if not isinstance(self.capability_manifest_ref, ContractRef):
            raise TypeError("capability_manifest_ref must be ContractRef")
        if not isinstance(self.communication_policy, IntentCommunicationPolicy):
            raise TypeError("communication_policy must be IntentCommunicationPolicy")
        if (
            canonical_fingerprint(self.communication_policy.as_dict())
            != self.communication_policy_fingerprint
        ):
            raise ValueError("communication policy differs from its fingerprint")
        steps = tuple(self.steps)
        if not steps or any(not isinstance(item, SessionStepState) for item in steps):
            raise TypeError("session steps must contain SessionStepState values")
        self._validate_steps(steps)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "status", _session_status(self.status))
        final_result = steps[-1].communication_result
        if self.status == "provider_failed":
            if not isinstance(self.provider_error, ProviderError):
                raise ValueError("provider_failed session requires ProviderError")
            if final_result is not None or self.final_communication_result_ref is not None:
                raise ValueError("provider_failed session cannot contain a final business result")
        else:
            if self.provider_error is not None:
                raise ValueError("business session status cannot contain ProviderError")
            if final_result is None or final_result.status != self.status:
                raise ValueError("session status differs from its final communication result")
            if self.final_communication_result_ref != final_result.ref:
                raise ValueError("final communication result reference is inconsistent")

    def _validate_steps(self, steps: tuple[SessionStepState, ...]) -> None:
        invocation_ids = tuple(item.invocation_id for item in steps)
        if len(invocation_ids) != len(set(invocation_ids)):
            raise ValueError("session invocation_id values must be unique")
        first_request = steps[0].request
        if first_request.turn_index != 1 or first_request.model_attempt != 1:
            raise ValueError("session history must start at turn one, model attempt one")
        previous: DomainModelRequest | None = None
        for item in steps:
            request = item.request
            if request.session_id != self.session_id:
                raise ValueError("session step belongs to another session")
            if request.capability_manifest_ref != self.capability_manifest_ref:
                raise ValueError("session step references another capability manifest")
            if (
                request.output_policy.maximum_model_attempts
                != self.communication_policy.maximum_model_attempts
            ):
                raise ValueError("session step references another communication policy")
            if previous is not None:
                same_turn = request.turn_index == previous.turn_index
                next_turn = request.turn_index == previous.turn_index + 1
                if same_turn and request.model_attempt != previous.model_attempt + 1:
                    raise ValueError("semantic repair attempts must be contiguous")
                if next_turn and request.model_attempt != 1:
                    raise ValueError("a clarification turn must start at model attempt one")
                if not same_turn and not next_turn:
                    raise ValueError("session turn indexes must be contiguous")
            previous = request

    @classmethod
    def from_mapping(cls, value: object) -> DomainIntentSessionState:
        raw = as_mapping(value, context="domain intent session state")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "session_id",
                "snapshot_index",
                "previous_manifest_fingerprint",
                "execution_mode",
                "provider_id",
                "provider_version",
                "provider_profile_fingerprint",
                "model_id",
                "model_profile_fingerprint",
                "capability_manifest_ref",
                "communication_policy",
                "communication_policy_fingerprint",
                "steps",
                "status",
                "provider_error",
                "final_communication_result_ref",
            },
            context="domain intent session state",
        )
        previous = raw["previous_manifest_fingerprint"]
        provider_error = raw["provider_error"]
        final_ref = raw["final_communication_result_ref"]
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=version(raw["schema_version"], context="schema_version"),
            session_id=identifier(raw["session_id"], context="session_id"),
            snapshot_index=integer(raw["snapshot_index"], context="snapshot_index", minimum=1),
            previous_manifest_fingerprint=(
                None
                if previous is None
                else digest(previous, context="previous_manifest_fingerprint")
            ),
            execution_mode=_execution_mode(raw["execution_mode"]),
            provider_id=identifier(raw["provider_id"], context="provider_id"),
            provider_version=version(raw["provider_version"], context="provider_version"),
            provider_profile_fingerprint=digest(
                raw["provider_profile_fingerprint"],
                context="provider_profile_fingerprint",
            ),
            model_id=identifier(raw["model_id"], context="model_id"),
            model_profile_fingerprint=digest(
                raw["model_profile_fingerprint"], context="model_profile_fingerprint"
            ),
            capability_manifest_ref=ContractRef.from_mapping(
                as_mapping(raw["capability_manifest_ref"], context="capability_manifest_ref")
            ),
            communication_policy=IntentCommunicationPolicy.from_mapping(
                raw["communication_policy"]
            ),
            communication_policy_fingerprint=digest(
                raw["communication_policy_fingerprint"],
                context="communication_policy_fingerprint",
            ),
            steps=tuple(
                SessionStepState.from_mapping(item)
                for item in as_sequence(raw["steps"], context="steps")
            ),
            status=_session_status(raw["status"]),
            provider_error=(
                None if provider_error is None else ProviderError.from_mapping(provider_error)
            ),
            final_communication_result_ref=(
                None
                if final_ref is None
                else ContractRef.from_mapping(
                    as_mapping(final_ref, context="final_communication_result_ref")
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "snapshot_index": self.snapshot_index,
            "previous_manifest_fingerprint": self.previous_manifest_fingerprint,
            "execution_mode": self.execution_mode,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_profile_fingerprint": self.provider_profile_fingerprint,
            "model_id": self.model_id,
            "model_profile_fingerprint": self.model_profile_fingerprint,
            "capability_manifest_ref": self.capability_manifest_ref.as_dict(),
            "communication_policy": self.communication_policy.as_dict(),
            "communication_policy_fingerprint": self.communication_policy_fingerprint,
            "steps": [item.as_dict() for item in self.steps],
            "status": self.status,
            "provider_error": (
                None if self.provider_error is None else self.provider_error.as_dict()
            ),
            "final_communication_result_ref": (
                None
                if self.final_communication_result_ref is None
                else self.final_communication_result_ref.as_dict()
            ),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())
