"""Strict, manifest-last evidence for provider-neutral domain-model invocations."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from errno import EACCES, EAGAIN, EWOULDBLOCK
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from ._json import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    canonical_json_bytes,
    decode_json_object,
    digest,
    identifier,
    integer,
    sha256_bytes,
    strict_keys,
    text,
    version,
)
from .models import ApiStyle
from .session import DomainIntentSessionState

EVIDENCE_SCHEMA_VERSION: Final[str] = "2.0.0"
TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID: Final[str] = "domain-model-transport-attempt-evidence"
INVOCATION_EVIDENCE_SCHEMA_ID: Final[str] = "domain-model-invocation-evidence"
SESSION_EVIDENCE_SCHEMA_ID: Final[str] = "domain-model-session-evidence"
EVIDENCE_MANIFEST_SCHEMA_ID: Final[str] = "domain-model-evidence-manifest"
SNAPSHOT_MANIFEST_SCHEMA_ID: Final[str] = "domain-model-session-snapshot-manifest"
EVALUATION_ARTIFACT_MANIFEST_SCHEMA_ID: Final[str] = "domain-model-evaluation-artifact-manifest"
DISCOVERY_ARTIFACT_MANIFEST_SCHEMA_ID: Final[str] = "domain-model-discovery-artifact-manifest"
_EVIDENCE_FILE: Final[str] = "invocations.json"
_STATE_FILE: Final[str] = "session.json"
_MANIFEST_FILE: Final[str] = "manifest.json"
_EVALUATION_REPORT_FILE: Final[str] = "report.json"
_DISCOVERY_REPORT_FILE: Final[str] = "discovery.json"
_MAX_EVIDENCE_BYTES: Final[int] = 1_000_000
_MAX_SAFE_TEXT_BYTES: Final[int] = 512
_SAFE_TEXT: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FORBIDDEN_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "analysis",
        "authorization",
        "chainofthought",
        "credential",
        "credentials",
        "password",
        "rawrequest",
        "rawresponse",
        "reasoning",
        "reasoningcontent",
        "reasoningtrace",
        "secret",
        "thinking",
        "thoughts",
        "tokenvalue",
    }
)
_FORBIDDEN_VALUE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}|"
    r"\bsk[-_][A-Za-z0-9_-]{8,}|"
    r"(?<![A-Za-z0-9_])(?:authorization|api[_ -]?key|access[_ -]?token|client[_ -]?secret|"
    r"password|secret|token)(?![A-Za-z0-9_])\s*[:=]\s*[\"']?[^\s\"',}]{4,}|"
    r"(?:DMX|API|访问|认证)?(?:令牌|密钥)\s*[:=：]\s*[\"']?[^\s\"',}]{4,}|"
    r"\b[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|CLIENT_SECRET)\b)"
)

type InvocationStatus = Literal["succeeded", "blocked", "failed"]
type ExecutionMode = Literal["production", "validation", "synthetic_test"]
type TransportAttemptStatus = Literal["succeeded", "failed"]
type ProviderErrorCategory = Literal[
    "authentication",
    "payment",
    "permission",
    "not_found",
    "invalid_request",
    "rate_limit",
    "provider_server",
    "transport",
    "timeout",
    "refusal",
    "truncated",
    "protocol",
    "model_mismatch",
]


class SessionConflictError(ValueError):
    """A safe, typed refusal for a stale or concurrently continued session."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = identifier(code, context="session conflict code")


_PROVIDER_ERROR_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "authentication",
        "payment",
        "permission",
        "not_found",
        "invalid_request",
        "rate_limit",
        "provider_server",
        "transport",
        "timeout",
        "refusal",
        "truncated",
        "protocol",
        "model_mismatch",
    }
)


def _api_style(value: object) -> ApiStyle:
    if value not in {"openai_chat", "openai_responses", "anthropic_messages"}:
        raise ValueError("invocation api_style is unsupported")
    assert isinstance(value, str)
    return value


def _status(value: object) -> InvocationStatus:
    if value not in {"succeeded", "blocked", "failed"}:
        raise ValueError("invocation status is unsupported")
    assert isinstance(value, str)
    return value


def _attempt_status(value: object) -> TransportAttemptStatus:
    if value not in {"succeeded", "failed"}:
        raise ValueError("transport attempt status is unsupported")
    assert isinstance(value, str)
    return value


def _execution_mode(value: object) -> ExecutionMode:
    if value not in {"production", "validation", "synthetic_test"}:
        raise ValueError("invocation execution_mode is unsupported")
    assert isinstance(value, str)
    return value


def _error_category(value: object) -> ProviderErrorCategory:
    if value not in _PROVIDER_ERROR_CATEGORIES:
        raise ValueError("transport attempt error_category is unsupported")
    assert isinstance(value, str)
    return cast(ProviderErrorCategory, value)


def _optional_digest(value: object, *, context: str) -> str | None:
    return None if value is None else digest(value, context=context)


def _optional_integer(value: object, *, context: str) -> int | None:
    return None if value is None else integer(value, context=context, minimum=0)


def _optional_boolean(value: object, *, context: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be boolean or null")
    return value


def _optional_http_status(value: object, *, context: str) -> int | None:
    if value is None:
        return None
    result = integer(value, context=context, minimum=100)
    if result > 599:
        raise ValueError(f"{context} must not exceed 599")
    return result


def _optional_safe_text(value: object, *, context: str) -> str | None:
    if value is None:
        return None
    result = text(value, context=context)
    if len(result.encode("utf-8")) > _MAX_SAFE_TEXT_BYTES:
        raise ValueError(f"{context} exceeds the safe evidence text limit")
    if not _SAFE_TEXT.fullmatch(result):
        raise ValueError(f"{context} contains unsafe characters")
    if _FORBIDDEN_VALUE.search(result):
        raise ValueError(f"{context} contains forbidden sensitive material")
    return result


def _assert_no_sensitive_evidence(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("evidence keys must be strings")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _FORBIDDEN_FIELD_NAMES:
                raise ValueError("evidence contains a forbidden sensitive field")
            _assert_no_sensitive_evidence(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_no_sensitive_evidence(item)
        return
    if isinstance(value, str) and _FORBIDDEN_VALUE.search(value):
        raise ValueError("evidence contains forbidden sensitive material")


@dataclass(frozen=True)
class TransportAttemptEvidence:
    """Safe metadata for one physical provider request.

    The contract intentionally omits response bodies, provider error messages,
    request headers, prompts, credentials, and model reasoning.
    """

    schema_id: str
    schema_version: str
    attempt_index: int
    status: TransportAttemptStatus
    provider_request_id: str | None
    served_model: str | None
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_category: ProviderErrorCategory | None
    error_code: str | None
    http_status: int | None
    retryable: bool | None

    def __post_init__(self) -> None:
        if self.schema_id != TRANSPORT_ATTEMPT_EVIDENCE_SCHEMA_ID:
            raise ValueError("schema_id differs from transport attempt evidence")
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from transport attempt evidence")
        object.__setattr__(
            self,
            "attempt_index",
            integer(self.attempt_index, context="attempt_index", minimum=1),
        )
        object.__setattr__(self, "status", _attempt_status(self.status))
        object.__setattr__(
            self,
            "provider_request_id",
            _optional_safe_text(self.provider_request_id, context="provider_request_id"),
        )
        object.__setattr__(
            self,
            "served_model",
            _optional_safe_text(self.served_model, context="served_model"),
        )
        object.__setattr__(
            self,
            "duration_ms",
            integer(self.duration_ms, context="duration_ms", minimum=0),
        )
        object.__setattr__(
            self,
            "input_tokens",
            _optional_integer(self.input_tokens, context="input_tokens"),
        )
        object.__setattr__(
            self,
            "output_tokens",
            _optional_integer(self.output_tokens, context="output_tokens"),
        )
        object.__setattr__(
            self,
            "total_tokens",
            _optional_integer(self.total_tokens, context="total_tokens"),
        )
        error_category = (
            None if self.error_category is None else _error_category(self.error_category)
        )
        object.__setattr__(self, "error_category", error_category)
        object.__setattr__(
            self,
            "error_code",
            _optional_safe_text(self.error_code, context="error_code"),
        )
        object.__setattr__(
            self,
            "http_status",
            _optional_http_status(self.http_status, context="http_status"),
        )
        object.__setattr__(
            self,
            "retryable",
            _optional_boolean(self.retryable, context="retryable"),
        )
        self._validate_usage()
        if self.status == "succeeded":
            if self.served_model is None:
                raise ValueError("succeeded transport attempt requires served_model")
            if any(
                item is not None
                for item in (
                    self.error_category,
                    self.error_code,
                    self.http_status,
                    self.retryable,
                )
            ):
                raise ValueError("succeeded transport attempt must not contain error metadata")
        elif self.error_category is None or self.error_code is None or self.retryable is None:
            raise ValueError("failed transport attempt requires normalized error metadata")
        _assert_no_sensitive_evidence(self.as_dict())

    def _validate_usage(self) -> None:
        known_parts = (0 if self.input_tokens is None else self.input_tokens) + (
            0 if self.output_tokens is None else self.output_tokens
        )
        if self.total_tokens is not None and self.total_tokens < known_parts:
            raise ValueError("total_tokens must not be below known token components")
        if (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")

    @classmethod
    def from_mapping(cls, value: object) -> TransportAttemptEvidence:
        raw = as_mapping(value, context="transport attempt evidence")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "attempt_index",
                "status",
                "provider_request_id",
                "served_model",
                "duration_ms",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "error_category",
                "error_code",
                "http_status",
                "retryable",
            },
            context="transport attempt evidence",
        )
        error_category = raw["error_category"]
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=version(raw["schema_version"], context="schema_version"),
            attempt_index=integer(raw["attempt_index"], context="attempt_index", minimum=1),
            status=_attempt_status(raw["status"]),
            provider_request_id=_optional_safe_text(
                raw["provider_request_id"], context="provider_request_id"
            ),
            served_model=_optional_safe_text(raw["served_model"], context="served_model"),
            duration_ms=integer(raw["duration_ms"], context="duration_ms", minimum=0),
            input_tokens=_optional_integer(raw["input_tokens"], context="input_tokens"),
            output_tokens=_optional_integer(raw["output_tokens"], context="output_tokens"),
            total_tokens=_optional_integer(raw["total_tokens"], context="total_tokens"),
            error_category=(None if error_category is None else _error_category(error_category)),
            error_code=_optional_safe_text(raw["error_code"], context="error_code"),
            http_status=_optional_http_status(raw["http_status"], context="http_status"),
            retryable=_optional_boolean(raw["retryable"], context="retryable"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "attempt_index": self.attempt_index,
            "status": self.status,
            "provider_request_id": self.provider_request_id,
            "served_model": self.served_model,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "http_status": self.http_status,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class InvocationEvidence:
    """Allow-listed invocation metadata; no raw prompt, response, secret, or reasoning.

    ``transport_attempts`` and ``duration_ms`` summarize the full physical-attempt
    tuple. Provider response, usage, and error summary fields mirror its final item.
    """

    schema_id: str
    schema_version: str
    session_id: str
    invocation_id: str
    status: InvocationStatus
    execution_mode: ExecutionMode
    provider_id: str
    provider_version: str
    provider_profile_fingerprint: str
    model_id: str
    model_profile_fingerprint: str
    served_model: str | None
    api_style: ApiStyle
    endpoint_path: str
    prompt_id: str
    prompt_version: str
    prompt_fingerprint: str
    egress_payload_fingerprint: str
    response_schema_id: str
    response_schema_version: str
    response_schema_fingerprint: str
    request_fingerprint: str
    response_fingerprint: str | None
    communication_result_fingerprint: str | None
    provider_request_id: str | None
    duration_ms: int
    transport_attempts: int
    attempts: tuple[TransportAttemptEvidence, ...]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_category: ProviderErrorCategory | None
    error_code: str | None
    http_status: int | None
    retryable: bool | None

    def __post_init__(self) -> None:
        if self.schema_id != INVOCATION_EVIDENCE_SCHEMA_ID:
            raise ValueError("schema_id differs from invocation evidence")
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from invocation evidence")
        for field_name in (
            "session_id",
            "invocation_id",
            "provider_id",
            "provider_version",
            "model_id",
            "prompt_id",
            "response_schema_id",
        ):
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
            self, "prompt_version", version(self.prompt_version, context="prompt_version")
        )
        object.__setattr__(
            self,
            "response_schema_version",
            version(self.response_schema_version, context="response_schema_version"),
        )
        object.__setattr__(self, "status", _status(self.status))
        object.__setattr__(self, "execution_mode", _execution_mode(self.execution_mode))
        object.__setattr__(self, "api_style", _api_style(self.api_style))
        for field_name in (
            "provider_profile_fingerprint",
            "model_profile_fingerprint",
            "prompt_fingerprint",
            "egress_payload_fingerprint",
            "response_schema_fingerprint",
            "request_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                digest(getattr(self, field_name), context=field_name),
            )
        object.__setattr__(
            self,
            "response_fingerprint",
            _optional_digest(self.response_fingerprint, context="response_fingerprint"),
        )
        object.__setattr__(
            self,
            "communication_result_fingerprint",
            _optional_digest(
                self.communication_result_fingerprint,
                context="communication_result_fingerprint",
            ),
        )
        if (
            not isinstance(self.endpoint_path, str)
            or not self.endpoint_path.startswith("/")
            or self.endpoint_path.startswith("//")
            or ".." in self.endpoint_path
        ):
            raise ValueError("endpoint_path must be one exact absolute path")
        object.__setattr__(
            self,
            "provider_request_id",
            _optional_safe_text(self.provider_request_id, context="provider_request_id"),
        )
        object.__setattr__(
            self,
            "served_model",
            _optional_safe_text(self.served_model, context="served_model"),
        )
        object.__setattr__(
            self,
            "duration_ms",
            integer(self.duration_ms, context="duration_ms", minimum=0),
        )
        object.__setattr__(
            self,
            "transport_attempts",
            integer(self.transport_attempts, context="transport_attempts", minimum=0),
        )
        attempts = tuple(self.attempts)
        if any(not isinstance(item, TransportAttemptEvidence) for item in attempts):
            raise TypeError("attempts must contain only TransportAttemptEvidence values")
        if tuple(item.attempt_index for item in attempts) != tuple(range(1, len(attempts) + 1)):
            raise ValueError("transport attempt indexes must be contiguous and start at one")
        for item in attempts[:-1]:
            if item.status != "failed" or item.retryable is not True:
                raise ValueError(
                    "only a retryable failed transport attempt may precede another attempt"
                )
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(
            self,
            "input_tokens",
            _optional_integer(self.input_tokens, context="input_tokens"),
        )
        object.__setattr__(
            self,
            "output_tokens",
            _optional_integer(self.output_tokens, context="output_tokens"),
        )
        object.__setattr__(
            self,
            "total_tokens",
            _optional_integer(self.total_tokens, context="total_tokens"),
        )
        error_category = (
            None if self.error_category is None else _error_category(self.error_category)
        )
        object.__setattr__(self, "error_category", error_category)
        object.__setattr__(
            self,
            "error_code",
            _optional_safe_text(self.error_code, context="error_code"),
        )
        object.__setattr__(
            self,
            "http_status",
            _optional_http_status(self.http_status, context="http_status"),
        )
        object.__setattr__(
            self,
            "retryable",
            _optional_boolean(self.retryable, context="retryable"),
        )
        self._validate_attempt_summary()
        if self.status == "succeeded" and self.response_fingerprint is None:
            raise ValueError("succeeded evidence requires response_fingerprint")
        if self.status == "succeeded" and not attempts:
            raise ValueError("succeeded evidence requires a transport attempt")
        if self.status == "succeeded" and self.served_model is None:
            raise ValueError("succeeded evidence requires served_model")
        if self.status == "succeeded" and self.communication_result_fingerprint is None:
            raise ValueError("succeeded evidence requires communication_result_fingerprint")
        if self.status == "failed" and self.response_fingerprint is not None:
            raise ValueError("failed evidence must not reference a provider response")
        if self.status != "succeeded" and self.communication_result_fingerprint is not None:
            raise ValueError("blocked or failed evidence must not reference a communication result")
        if (
            self.status == "blocked"
            and self.response_fingerprint is not None
            and (not attempts or attempts[-1].status != "succeeded")
        ):
            raise ValueError(
                "blocked evidence may reference a response only after a succeeded attempt"
            )
        if self.status == "succeeded" and any(
            item is not None
            for item in (
                self.error_category,
                self.error_code,
                self.http_status,
                self.retryable,
            )
        ):
            raise ValueError("succeeded evidence must not have error metadata")
        if self.status != "succeeded" and (
            self.error_category is None or self.error_code is None or self.retryable is None
        ):
            raise ValueError("blocked or failed evidence requires normalized error metadata")
        _assert_no_sensitive_evidence(self.as_dict())

    def _validate_attempt_summary(self) -> None:
        if self.transport_attempts != len(self.attempts):
            raise ValueError("transport_attempts differs from attempts length")
        if not self.attempts:
            if self.status == "succeeded":
                raise ValueError("succeeded evidence requires a transport attempt")
            if self.duration_ms != 0:
                raise ValueError("preflight failure duration_ms must be zero")
            if any(
                item is not None
                for item in (
                    self.served_model,
                    self.provider_request_id,
                    self.input_tokens,
                    self.output_tokens,
                    self.total_tokens,
                )
            ):
                raise ValueError("preflight failure must not contain transport metadata")
            return
        final = self.attempts[-1]
        if self.status == "succeeded" and final.status != "succeeded":
            raise ValueError("succeeded invocation differs from final transport attempt")
        if self.status == "failed" and final.status != "failed":
            raise ValueError("failed invocation differs from final transport attempt")
        if self.duration_ms != sum(item.duration_ms for item in self.attempts):
            raise ValueError("duration_ms differs from the transport attempt total")
        final_fields = {
            "served_model": (self.served_model, final.served_model),
            "provider_request_id": (self.provider_request_id, final.provider_request_id),
            "input_tokens": (self.input_tokens, final.input_tokens),
            "output_tokens": (self.output_tokens, final.output_tokens),
            "total_tokens": (self.total_tokens, final.total_tokens),
        }
        if self.status != "blocked":
            final_fields.update(
                {
                    "error_category": (self.error_category, final.error_category),
                    "error_code": (self.error_code, final.error_code),
                    "http_status": (self.http_status, final.http_status),
                    "retryable": (self.retryable, final.retryable),
                }
            )
        mismatched = sorted(
            field_name
            for field_name, (summary, attempt) in final_fields.items()
            if summary != attempt
        )
        if mismatched:
            raise ValueError(
                "invocation summary differs from final transport attempt: " + ", ".join(mismatched)
            )

    @classmethod
    def from_mapping(cls, value: object) -> InvocationEvidence:
        raw = as_mapping(value, context="invocation evidence")
        required = {
            "schema_id",
            "schema_version",
            "session_id",
            "invocation_id",
            "status",
            "execution_mode",
            "provider_id",
            "provider_version",
            "provider_profile_fingerprint",
            "model_id",
            "model_profile_fingerprint",
            "served_model",
            "api_style",
            "endpoint_path",
            "prompt_id",
            "prompt_version",
            "prompt_fingerprint",
            "egress_payload_fingerprint",
            "response_schema_id",
            "response_schema_version",
            "response_schema_fingerprint",
            "request_fingerprint",
            "response_fingerprint",
            "communication_result_fingerprint",
            "provider_request_id",
            "duration_ms",
            "transport_attempts",
            "attempts",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "error_category",
            "error_code",
            "http_status",
            "retryable",
        }
        strict_keys(raw, required=required, context="invocation evidence")
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=version(raw["schema_version"], context="schema_version"),
            session_id=identifier(raw["session_id"], context="session_id"),
            invocation_id=identifier(raw["invocation_id"], context="invocation_id"),
            status=_status(raw["status"]),
            execution_mode=_execution_mode(raw["execution_mode"]),
            provider_id=identifier(raw["provider_id"], context="provider_id"),
            provider_version=version(raw["provider_version"], context="provider_version"),
            provider_profile_fingerprint=digest(
                raw["provider_profile_fingerprint"], context="provider_profile_fingerprint"
            ),
            model_id=identifier(raw["model_id"], context="model_id"),
            model_profile_fingerprint=digest(
                raw["model_profile_fingerprint"], context="model_profile_fingerprint"
            ),
            served_model=_optional_safe_text(raw["served_model"], context="served_model"),
            api_style=_api_style(raw["api_style"]),
            endpoint_path=text(raw["endpoint_path"], context="endpoint_path"),
            prompt_id=identifier(raw["prompt_id"], context="prompt_id"),
            prompt_version=version(raw["prompt_version"], context="prompt_version"),
            prompt_fingerprint=digest(raw["prompt_fingerprint"], context="prompt_fingerprint"),
            egress_payload_fingerprint=digest(
                raw["egress_payload_fingerprint"], context="egress_payload_fingerprint"
            ),
            response_schema_id=identifier(raw["response_schema_id"], context="response_schema_id"),
            response_schema_version=version(
                raw["response_schema_version"], context="response_schema_version"
            ),
            response_schema_fingerprint=digest(
                raw["response_schema_fingerprint"], context="response_schema_fingerprint"
            ),
            request_fingerprint=digest(raw["request_fingerprint"], context="request_fingerprint"),
            response_fingerprint=_optional_digest(
                raw["response_fingerprint"], context="response_fingerprint"
            ),
            communication_result_fingerprint=_optional_digest(
                raw["communication_result_fingerprint"],
                context="communication_result_fingerprint",
            ),
            provider_request_id=_optional_safe_text(
                raw["provider_request_id"], context="provider_request_id"
            ),
            duration_ms=integer(raw["duration_ms"], context="duration_ms", minimum=0),
            transport_attempts=integer(
                raw["transport_attempts"], context="transport_attempts", minimum=0
            ),
            attempts=tuple(
                TransportAttemptEvidence.from_mapping(item)
                for item in as_sequence(raw["attempts"], context="attempts")
            ),
            input_tokens=_optional_integer(raw["input_tokens"], context="input_tokens"),
            output_tokens=_optional_integer(raw["output_tokens"], context="output_tokens"),
            total_tokens=_optional_integer(raw["total_tokens"], context="total_tokens"),
            error_category=(
                None if raw["error_category"] is None else _error_category(raw["error_category"])
            ),
            error_code=_optional_safe_text(raw["error_code"], context="error_code"),
            http_status=_optional_http_status(raw["http_status"], context="http_status"),
            retryable=_optional_boolean(raw["retryable"], context="retryable"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "status": self.status,
            "execution_mode": self.execution_mode,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_profile_fingerprint": self.provider_profile_fingerprint,
            "model_id": self.model_id,
            "model_profile_fingerprint": self.model_profile_fingerprint,
            "served_model": self.served_model,
            "api_style": self.api_style,
            "endpoint_path": self.endpoint_path,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_fingerprint": self.prompt_fingerprint,
            "egress_payload_fingerprint": self.egress_payload_fingerprint,
            "response_schema_id": self.response_schema_id,
            "response_schema_version": self.response_schema_version,
            "response_schema_fingerprint": self.response_schema_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "response_fingerprint": self.response_fingerprint,
            "communication_result_fingerprint": self.communication_result_fingerprint,
            "provider_request_id": self.provider_request_id,
            "duration_ms": self.duration_ms,
            "transport_attempts": self.transport_attempts,
            "attempts": [item.as_dict() for item in self.attempts],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "http_status": self.http_status,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class SessionEvidence:
    schema_id: str
    schema_version: str
    session_id: str
    invocations: tuple[InvocationEvidence, ...]

    def __post_init__(self) -> None:
        if self.schema_id != SESSION_EVIDENCE_SCHEMA_ID:
            raise ValueError("schema_id differs from session evidence")
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from session evidence")
        object.__setattr__(self, "session_id", identifier(self.session_id, context="session_id"))
        invocations = tuple(self.invocations)
        if not invocations or any(not isinstance(item, InvocationEvidence) for item in invocations):
            raise TypeError("session invocations must contain InvocationEvidence values")
        if any(item.session_id != self.session_id for item in invocations):
            raise ValueError("all invocation evidence must belong to the session")
        invocation_ids = tuple(item.invocation_id for item in invocations)
        if len(invocation_ids) != len(set(invocation_ids)):
            raise ValueError("invocation_id values must be unique within a session")
        object.__setattr__(self, "invocations", invocations)

    @classmethod
    def from_mapping(cls, value: object) -> SessionEvidence:
        raw = as_mapping(value, context="session evidence")
        strict_keys(
            raw,
            required={"schema_id", "schema_version", "session_id", "invocations"},
            context="session evidence",
        )
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=version(raw["schema_version"], context="schema_version"),
            session_id=identifier(raw["session_id"], context="session_id"),
            invocations=tuple(
                InvocationEvidence.from_mapping(item)
                for item in as_sequence(raw["invocations"], context="invocations")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "invocations": [item.as_dict() for item in self.invocations],
        }


@dataclass(frozen=True)
class EvidenceRecord:
    run_dir: Path
    evidence: SessionEvidence
    manifest_fingerprint: str
    session_state: DomainIntentSessionState | None = None


@dataclass(frozen=True)
class EvaluationArtifactRecord:
    run_dir: Path
    report: Mapping[str, object]
    report_fingerprint: str
    manifest_fingerprint: str


@dataclass(frozen=True)
class DiscoveryArtifactRecord:
    run_dir: Path
    report: Mapping[str, object]
    report_fingerprint: str
    manifest_fingerprint: str


class EvidenceStore:
    """Write immutable session evidence below ``runs/domain_model`` with manifest last."""

    def __init__(self, project_root: Path) -> None:
        root = Path(project_root).resolve()
        self.root = root / "runs" / "domain_model"

    def write_session(
        self,
        session_id: str,
        invocations: Sequence[InvocationEvidence],
    ) -> EvidenceRecord:
        normalized_session = identifier(session_id, context="session_id")
        evidence = SessionEvidence(
            schema_id=SESSION_EVIDENCE_SCHEMA_ID,
            schema_version=EVIDENCE_SCHEMA_VERSION,
            session_id=normalized_session,
            invocations=tuple(invocations),
        )
        _assert_no_sensitive_evidence(evidence.as_dict())
        evidence_payload = canonical_json_bytes(evidence.as_dict()) + b"\n"
        self._validate_payload_size(evidence_payload, context="session evidence")
        file_entry = self._file_entry(_EVIDENCE_FILE, evidence_payload)
        manifest_body = {
            "schema_id": EVIDENCE_MANIFEST_SCHEMA_ID,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "session_id": normalized_session,
            "files": [file_entry],
        }
        manifest_fingerprint = canonical_fingerprint(manifest_body)
        manifest = {**manifest_body, "manifest_fingerprint": manifest_fingerprint}
        manifest_payload = canonical_json_bytes(manifest) + b"\n"
        self._validate_payload_size(manifest_payload, context="evidence manifest")
        self.root.mkdir(parents=True, exist_ok=True)
        run_dir = self.root / normalized_session
        run_dir.mkdir(exist_ok=False)
        self._publish(run_dir / _EVIDENCE_FILE, evidence_payload)
        self._publish_manifest(run_dir, manifest_payload)
        return EvidenceRecord(run_dir, evidence, manifest_fingerprint)

    def write(self, evidence: InvocationEvidence | Mapping[str, object]) -> EvidenceRecord:
        invocation = (
            evidence
            if isinstance(evidence, InvocationEvidence)
            else InvocationEvidence.from_mapping(evidence)
        )
        return self.write_session(invocation.session_id, (invocation,))

    def write_evaluation_report(
        self,
        report: Mapping[str, object],
    ) -> EvaluationArtifactRecord:
        """Publish one immutable, reloadable aggregate evaluation report."""

        if not isinstance(report, Mapping) or any(not isinstance(key, str) for key in report):
            raise TypeError("evaluation report must be an object with string keys")
        report_body = dict(report)
        if report_body.get("schema_id") != "domain-model-evaluation-report":
            raise ValueError("evaluation report schema_id is unsupported")
        _assert_no_sensitive_evidence(report_body)
        report_fingerprint = canonical_fingerprint(report_body)
        report_payload = (
            canonical_json_bytes({**report_body, "report_fingerprint": report_fingerprint}) + b"\n"
        )
        self._validate_payload_size(report_payload, context="domain-model evaluation report")
        artifact_id = f"evaluation-{uuid4().hex}"
        file_entry = self._file_entry(_EVALUATION_REPORT_FILE, report_payload)
        manifest_body = {
            "schema_id": EVALUATION_ARTIFACT_MANIFEST_SCHEMA_ID,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "report_fingerprint": report_fingerprint,
            "files": [file_entry],
        }
        manifest_fingerprint = canonical_fingerprint(manifest_body)
        manifest_payload = (
            canonical_json_bytes({**manifest_body, "manifest_fingerprint": manifest_fingerprint})
            + b"\n"
        )
        self._validate_payload_size(
            manifest_payload,
            context="domain-model evaluation artifact manifest",
        )
        evaluations_root = self.root / "evaluations"
        evaluations_root.mkdir(parents=True, exist_ok=True)
        run_dir = evaluations_root / artifact_id
        run_dir.mkdir(exist_ok=False)
        self._publish(run_dir / _EVALUATION_REPORT_FILE, report_payload)
        self._publish_manifest(run_dir, manifest_payload)
        return EvaluationArtifactRecord(
            run_dir=run_dir,
            report=report_body,
            report_fingerprint=report_fingerprint,
            manifest_fingerprint=manifest_fingerprint,
        )

    def write_discovery_report(
        self,
        report: Mapping[str, object],
    ) -> DiscoveryArtifactRecord:
        """Publish one immutable, safe `/models` result and its attempt evidence."""

        if not isinstance(report, Mapping) or any(not isinstance(key, str) for key in report):
            raise TypeError("discovery report must be an object with string keys")
        report_body = dict(report)
        if report_body.get("schema_id") != "domain-model-discovery-result":
            raise ValueError("discovery report schema_id is unsupported")
        _assert_no_sensitive_evidence(report_body)
        report_fingerprint = canonical_fingerprint(report_body)
        report_payload = (
            canonical_json_bytes({**report_body, "report_fingerprint": report_fingerprint}) + b"\n"
        )
        self._validate_payload_size(report_payload, context="domain-model discovery report")
        artifact_id = f"discovery-{uuid4().hex}"
        manifest_body = {
            "schema_id": DISCOVERY_ARTIFACT_MANIFEST_SCHEMA_ID,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "report_fingerprint": report_fingerprint,
            "files": [self._file_entry(_DISCOVERY_REPORT_FILE, report_payload)],
        }
        manifest_fingerprint = canonical_fingerprint(manifest_body)
        manifest_payload = (
            canonical_json_bytes({**manifest_body, "manifest_fingerprint": manifest_fingerprint})
            + b"\n"
        )
        self._validate_payload_size(
            manifest_payload,
            context="domain-model discovery artifact manifest",
        )
        discoveries_root = self.root / "discoveries"
        discoveries_root.mkdir(parents=True, exist_ok=True)
        run_dir = discoveries_root / artifact_id
        run_dir.mkdir(exist_ok=False)
        self._publish(run_dir / _DISCOVERY_REPORT_FILE, report_payload)
        self._publish_manifest(run_dir, manifest_payload)
        return DiscoveryArtifactRecord(
            run_dir=run_dir,
            report=report_body,
            report_fingerprint=report_fingerprint,
            manifest_fingerprint=manifest_fingerprint,
        )

    def read_discovery_report(self, manifest_path: Path) -> DiscoveryArtifactRecord:
        """Strictly reload one model-discovery artifact."""

        manifest = Path(manifest_path).resolve()
        discoveries_root = (self.root / "discoveries").resolve()
        if manifest.name != _MANIFEST_FILE or not manifest.is_relative_to(discoveries_root):
            raise ValueError("discovery manifest must be below runs/domain_model/discoveries")
        run_dir = manifest.parent
        if {item.name for item in run_dir.iterdir()} != {
            _DISCOVERY_REPORT_FILE,
            _MANIFEST_FILE,
        }:
            raise ValueError("domain-model discovery artifact contains unexpected files")
        raw = decode_json_object(
            self._read_bounded_file(
                manifest,
                context="domain-model discovery artifact manifest",
            ),
            context="domain-model discovery artifact manifest",
            maximum_bytes=_MAX_EVIDENCE_BYTES,
        )
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "artifact_id",
                "report_fingerprint",
                "files",
                "manifest_fingerprint",
            },
            context="domain-model discovery artifact manifest",
        )
        if raw["schema_id"] != DISCOVERY_ARTIFACT_MANIFEST_SCHEMA_ID:
            raise ValueError("discovery artifact manifest schema_id is unsupported")
        if raw["schema_version"] != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("discovery artifact manifest schema_version is unsupported")
        artifact_id = identifier(raw["artifact_id"], context="discovery artifact_id")
        if run_dir.name != artifact_id:
            raise ValueError("discovery artifact path differs from its manifest")
        files = as_sequence(raw["files"], context="discovery artifact files")
        if len(files) != 1:
            raise ValueError("discovery artifact must declare exactly one report file")
        entry = as_mapping(files[0], context="discovery artifact report file")
        strict_keys(
            entry,
            required={"path", "size_bytes", "sha256"},
            context="discovery artifact report file",
        )
        if entry["path"] != _DISCOVERY_REPORT_FILE:
            raise ValueError("discovery artifact references an unsupported path")
        report_payload = self._read_bounded_file(
            run_dir / _DISCOVERY_REPORT_FILE,
            context="domain-model discovery report",
        )
        if integer(entry["size_bytes"], context="discovery report size") != len(report_payload):
            raise ValueError("discovery report size differs from its manifest")
        if digest(entry["sha256"], context="discovery report sha256") != sha256_bytes(
            report_payload
        ):
            raise ValueError("discovery report digest differs from its manifest")
        manifest_fingerprint = digest(raw["manifest_fingerprint"], context="manifest_fingerprint")
        manifest_body = {key: value for key, value in raw.items() if key != "manifest_fingerprint"}
        if canonical_fingerprint(manifest_body) != manifest_fingerprint:
            raise ValueError("discovery artifact manifest fingerprint is invalid")
        report_raw = dict(
            decode_json_object(
                report_payload,
                context="domain-model discovery report",
                maximum_bytes=_MAX_EVIDENCE_BYTES,
            )
        )
        supplied_report_fingerprint = digest(
            report_raw.pop("report_fingerprint", None),
            context="report_fingerprint",
        )
        if (
            supplied_report_fingerprint
            != digest(raw["report_fingerprint"], context="report_fingerprint")
            or canonical_fingerprint(report_raw) != supplied_report_fingerprint
        ):
            raise ValueError("discovery report fingerprint is invalid")
        _assert_no_sensitive_evidence(report_raw)
        return DiscoveryArtifactRecord(
            run_dir=run_dir,
            report=report_raw,
            report_fingerprint=supplied_report_fingerprint,
            manifest_fingerprint=manifest_fingerprint,
        )

    def read_evaluation_report(self, manifest_path: Path) -> EvaluationArtifactRecord:
        """Strictly reload an aggregate evaluation report from its manifest."""

        manifest = Path(manifest_path).resolve()
        evaluations_root = (self.root / "evaluations").resolve()
        if manifest.name != _MANIFEST_FILE or not manifest.is_relative_to(evaluations_root):
            raise ValueError("evaluation manifest must be below runs/domain_model/evaluations")
        run_dir = manifest.parent
        if {item.name for item in run_dir.iterdir()} != {
            _EVALUATION_REPORT_FILE,
            _MANIFEST_FILE,
        }:
            raise ValueError("domain-model evaluation artifact contains unexpected files")
        raw = decode_json_object(
            self._read_bounded_file(
                manifest,
                context="domain-model evaluation artifact manifest",
            ),
            context="domain-model evaluation artifact manifest",
            maximum_bytes=_MAX_EVIDENCE_BYTES,
        )
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "artifact_id",
                "report_fingerprint",
                "files",
                "manifest_fingerprint",
            },
            context="domain-model evaluation artifact manifest",
        )
        if raw["schema_id"] != EVALUATION_ARTIFACT_MANIFEST_SCHEMA_ID:
            raise ValueError("evaluation artifact manifest schema_id is unsupported")
        if raw["schema_version"] != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("evaluation artifact manifest schema_version is unsupported")
        artifact_id = identifier(raw["artifact_id"], context="evaluation artifact_id")
        if run_dir.name != artifact_id:
            raise ValueError("evaluation artifact path differs from its manifest")
        files = as_sequence(raw["files"], context="evaluation artifact files")
        if len(files) != 1:
            raise ValueError("evaluation artifact must declare exactly one report file")
        entry = as_mapping(files[0], context="evaluation artifact report file")
        strict_keys(
            entry,
            required={"path", "size_bytes", "sha256"},
            context="evaluation artifact report file",
        )
        if entry["path"] != _EVALUATION_REPORT_FILE:
            raise ValueError("evaluation artifact references an unsupported path")
        report_payload = self._read_bounded_file(
            run_dir / _EVALUATION_REPORT_FILE,
            context="domain-model evaluation report",
        )
        if integer(entry["size_bytes"], context="evaluation report size") != len(report_payload):
            raise ValueError("evaluation report size differs from its manifest")
        if digest(entry["sha256"], context="evaluation report sha256") != sha256_bytes(
            report_payload
        ):
            raise ValueError("evaluation report digest differs from its manifest")
        manifest_fingerprint = digest(raw["manifest_fingerprint"], context="manifest_fingerprint")
        manifest_body = {key: value for key, value in raw.items() if key != "manifest_fingerprint"}
        if canonical_fingerprint(manifest_body) != manifest_fingerprint:
            raise ValueError("evaluation artifact manifest fingerprint is invalid")
        report_raw = dict(
            decode_json_object(
                report_payload,
                context="domain-model evaluation report",
                maximum_bytes=_MAX_EVIDENCE_BYTES,
            )
        )
        supplied_report_fingerprint = digest(
            report_raw.pop("report_fingerprint", None),
            context="report_fingerprint",
        )
        if (
            supplied_report_fingerprint
            != digest(raw["report_fingerprint"], context="report_fingerprint")
            or canonical_fingerprint(report_raw) != supplied_report_fingerprint
        ):
            raise ValueError("evaluation report fingerprint is invalid")
        _assert_no_sensitive_evidence(report_raw)
        return EvaluationArtifactRecord(
            run_dir=run_dir,
            report=report_raw,
            report_fingerprint=supplied_report_fingerprint,
            manifest_fingerprint=manifest_fingerprint,
        )

    def write_snapshot(
        self,
        state: DomainIntentSessionState,
        invocations: Sequence[InvocationEvidence],
    ) -> EvidenceRecord:
        """Publish an immutable resumable snapshot below a stable session directory."""

        if not isinstance(state, DomainIntentSessionState):
            raise TypeError("state must be DomainIntentSessionState")
        evidence = SessionEvidence(
            schema_id=SESSION_EVIDENCE_SCHEMA_ID,
            schema_version=EVIDENCE_SCHEMA_VERSION,
            session_id=state.session_id,
            invocations=tuple(invocations),
        )
        self._validate_state_evidence_coherence(state, evidence)
        _assert_no_sensitive_evidence(evidence.as_dict())
        _assert_no_sensitive_evidence(state.as_dict())
        session_dir = self.root / "sessions" / state.session_id
        if state.snapshot_index != 1:
            previous_manifest = (
                session_dir / f"snapshot-{state.snapshot_index - 1:03d}" / _MANIFEST_FILE
            )
            previous = self.read_snapshot(previous_manifest)
            self._validate_snapshot_predecessor(state, evidence, previous)
        evidence_payload = canonical_json_bytes(evidence.as_dict()) + b"\n"
        state_payload = canonical_json_bytes(state.as_dict()) + b"\n"
        self._validate_payload_size(evidence_payload, context="session invocation evidence")
        self._validate_payload_size(state_payload, context="domain intent session state")
        files = [
            self._file_entry(_EVIDENCE_FILE, evidence_payload),
            self._file_entry(_STATE_FILE, state_payload),
        ]
        manifest_body = {
            "schema_id": SNAPSHOT_MANIFEST_SCHEMA_ID,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "session_id": state.session_id,
            "snapshot_index": state.snapshot_index,
            "state_fingerprint": state.fingerprint,
            "files": files,
        }
        manifest_fingerprint = canonical_fingerprint(manifest_body)
        manifest_payload = (
            canonical_json_bytes({**manifest_body, "manifest_fingerprint": manifest_fingerprint})
            + b"\n"
        )
        self._validate_payload_size(manifest_payload, context="session snapshot manifest")
        if state.snapshot_index == 1:
            session_dir.parent.mkdir(parents=True, exist_ok=True)
            session_dir.mkdir(exist_ok=False)
        run_dir = session_dir / f"snapshot-{state.snapshot_index:03d}"
        run_dir.mkdir(exist_ok=False)
        self._publish(run_dir / _EVIDENCE_FILE, evidence_payload)
        self._publish(run_dir / _STATE_FILE, state_payload)
        self._publish_manifest(run_dir, manifest_payload)
        return EvidenceRecord(run_dir, evidence, manifest_fingerprint, state)

    @contextmanager
    def initial_session_guard(self, session_id: str) -> Iterator[None]:
        """Claim a new session identifier before any first-turn provider I/O."""

        normalized = identifier(session_id, context="session_id")
        locks_root = self.root / "locks"
        locks_root.mkdir(parents=True, exist_ok=True)
        lock_path = locks_root / f"{normalized}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            self._acquire_session_lock(
                descriptor,
                code="session-initialization-busy",
            )
            if (self.root / "sessions" / normalized).exists():
                raise SessionConflictError(
                    "session-already-exists",
                    "domain-model session already has a first-turn snapshot",
                )
            yield
        finally:
            flock(descriptor, LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def continuation_guard(self, record: EvidenceRecord) -> Iterator[EvidenceRecord]:
        """Lock one resumable session and reject a stale snapshot before provider I/O."""

        state = record.session_state
        if state is None:
            raise ValueError("evidence record is not a resumable domain-intent snapshot")
        session_dir = self.root / "sessions" / state.session_id
        if record.run_dir.resolve().parent != session_dir.resolve():
            raise ValueError("session evidence record is outside its pinned session directory")
        lock_path = session_dir / ".continue.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            self._acquire_session_lock(
                descriptor,
                code="session-continuation-busy",
            )
            current = self.read_snapshot(record.run_dir / _MANIFEST_FILE)
            if current.manifest_fingerprint != record.manifest_fingerprint:
                raise ValueError("session continuation snapshot fingerprint has changed")
            current_state = current.session_state
            assert current_state is not None
            indexes: set[int] = set()
            for entry in session_dir.iterdir():
                if entry.name == ".continue.lock":
                    continue
                match = re.fullmatch(r"snapshot-(\d{3})", entry.name)
                if match is None or not entry.is_dir():
                    raise ValueError("session directory contains an unexpected entry")
                indexes.add(int(match.group(1)))
            expected_indexes = set(range(1, current_state.snapshot_index + 1))
            if indexes != expected_indexes:
                raise SessionConflictError(
                    "session-continuation-stale",
                    "session continuation snapshot is stale or incomplete",
                )
            yield current
        finally:
            flock(descriptor, LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _acquire_session_lock(descriptor: int, *, code: str) -> None:
        try:
            flock(descriptor, LOCK_EX | LOCK_NB)
        except OSError as exc:
            if exc.errno not in {EACCES, EAGAIN, EWOULDBLOCK}:
                raise
            raise SessionConflictError(
                code,
                "domain-model session operation is already in progress",
            ) from exc

    def read_snapshot(self, manifest_path: Path) -> EvidenceRecord:
        """Strictly reload one manifest-addressed session snapshot."""

        manifest = Path(manifest_path).resolve()
        sessions_root = (self.root / "sessions").resolve()
        if manifest.name != _MANIFEST_FILE or not manifest.is_relative_to(sessions_root):
            raise ValueError("session manifest must be below runs/domain_model/sessions")
        run_dir = manifest.parent
        if not manifest.is_file():
            raise ValueError("domain-model session snapshot is incomplete or missing")
        if {item.name for item in run_dir.iterdir()} != {
            _EVIDENCE_FILE,
            _STATE_FILE,
            _MANIFEST_FILE,
        }:
            raise ValueError("domain-model session snapshot contains unexpected files")
        raw = decode_json_object(
            self._read_bounded_file(
                manifest,
                context="domain-model session snapshot manifest",
            ),
            context="domain-model session snapshot manifest",
            maximum_bytes=_MAX_EVIDENCE_BYTES,
        )
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "session_id",
                "snapshot_index",
                "state_fingerprint",
                "files",
                "manifest_fingerprint",
            },
            context="domain-model session snapshot manifest",
        )
        if raw["schema_id"] != SNAPSHOT_MANIFEST_SCHEMA_ID:
            raise ValueError("session snapshot manifest schema_id is unsupported")
        if raw["schema_version"] != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("session snapshot manifest schema_version is unsupported")
        supplied_manifest_fingerprint = digest(
            raw["manifest_fingerprint"], context="manifest_fingerprint"
        )
        body = {key: value for key, value in raw.items() if key != "manifest_fingerprint"}
        if canonical_fingerprint(body) != supplied_manifest_fingerprint:
            raise ValueError("session snapshot manifest fingerprint is invalid")
        payloads = self._verify_snapshot_files(run_dir, raw["files"])
        state = DomainIntentSessionState.from_mapping(
            decode_json_object(
                payloads[_STATE_FILE],
                context="domain intent session state",
                maximum_bytes=_MAX_EVIDENCE_BYTES,
            )
        )
        evidence = SessionEvidence.from_mapping(
            decode_json_object(
                payloads[_EVIDENCE_FILE],
                context="domain-model session evidence",
                maximum_bytes=_MAX_EVIDENCE_BYTES,
            )
        )
        if raw["session_id"] != state.session_id or evidence.session_id != state.session_id:
            raise ValueError("session snapshot files reference another session")
        if (
            integer(raw["snapshot_index"], context="snapshot_index", minimum=1)
            != state.snapshot_index
        ):
            raise ValueError("session snapshot index differs from its state")
        if digest(raw["state_fingerprint"], context="state_fingerprint") != state.fingerprint:
            raise ValueError("session state differs from its declared fingerprint")
        self._validate_state_evidence_coherence(state, evidence)
        if run_dir.parent.name != state.session_id or run_dir.name != (
            f"snapshot-{state.snapshot_index:03d}"
        ):
            raise ValueError("session snapshot path differs from its state")
        if state.snapshot_index > 1:
            previous_manifest = (
                run_dir.parent / f"snapshot-{state.snapshot_index - 1:03d}" / _MANIFEST_FILE
            )
            previous = self.read_snapshot(previous_manifest)
            self._validate_snapshot_predecessor(state, evidence, previous)
        _assert_no_sensitive_evidence(state.as_dict())
        return EvidenceRecord(run_dir, evidence, supplied_manifest_fingerprint, state)

    def read_manifest(self, manifest_path: Path) -> EvidenceRecord:
        return self.read_snapshot(manifest_path)

    def read_session(self, session_id: str) -> EvidenceRecord:
        normalized_session = identifier(session_id, context="session_id")
        run_dir = self.root / normalized_session
        manifest_path = run_dir / _MANIFEST_FILE
        if not manifest_path.is_file():
            raise ValueError("domain-model evidence session is incomplete or missing")
        entries = {item.name for item in run_dir.iterdir()}
        if entries != {_EVIDENCE_FILE, _MANIFEST_FILE}:
            raise ValueError("domain-model evidence session contains unexpected files")
        manifest_raw = decode_json_object(
            self._read_bounded_file(
                manifest_path,
                context="domain-model evidence manifest",
            ),
            context="domain-model evidence manifest",
            maximum_bytes=_MAX_EVIDENCE_BYTES,
        )
        strict_keys(
            manifest_raw,
            required={
                "schema_id",
                "schema_version",
                "session_id",
                "files",
                "manifest_fingerprint",
            },
            context="domain-model evidence manifest",
        )
        if manifest_raw["schema_id"] != EVIDENCE_MANIFEST_SCHEMA_ID:
            raise ValueError("evidence manifest schema_id is unsupported")
        if manifest_raw["schema_version"] != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("evidence manifest schema_version is unsupported")
        if manifest_raw["session_id"] != normalized_session:
            raise ValueError("evidence manifest references another session")
        files = as_sequence(manifest_raw["files"], context="evidence manifest files")
        if len(files) != 1:
            raise ValueError("evidence manifest must contain exactly one evidence file")
        file_entry = as_mapping(files[0], context="evidence manifest file")
        strict_keys(
            file_entry,
            required={"path", "size_bytes", "sha256"},
            context="evidence manifest file",
        )
        if file_entry["path"] != _EVIDENCE_FILE:
            raise ValueError("evidence manifest references an unsupported path")
        evidence_payload = self._read_bounded_file(
            run_dir / _EVIDENCE_FILE,
            context="domain-model session evidence",
        )
        if integer(file_entry["size_bytes"], context="evidence size") != len(evidence_payload):
            raise ValueError("evidence file size differs from manifest")
        if digest(file_entry["sha256"], context="evidence sha256") != sha256_bytes(
            evidence_payload
        ):
            raise ValueError("evidence file digest differs from manifest")
        supplied_manifest_fingerprint = digest(
            manifest_raw["manifest_fingerprint"], context="manifest_fingerprint"
        )
        manifest_body = {
            key: value for key, value in manifest_raw.items() if key != "manifest_fingerprint"
        }
        if canonical_fingerprint(manifest_body) != supplied_manifest_fingerprint:
            raise ValueError("evidence manifest fingerprint is invalid")
        evidence = SessionEvidence.from_mapping(
            decode_json_object(
                evidence_payload,
                context="domain-model session evidence",
                maximum_bytes=_MAX_EVIDENCE_BYTES,
            )
        )
        if evidence.session_id != normalized_session:
            raise ValueError("session evidence references another session")
        _assert_no_sensitive_evidence(evidence.as_dict())
        return EvidenceRecord(run_dir, evidence, supplied_manifest_fingerprint)

    def read(self, session_id: str) -> EvidenceRecord:
        return self.read_session(session_id)

    @staticmethod
    def _file_entry(path: str, payload: bytes) -> dict[str, object]:
        return {
            "path": path,
            "size_bytes": len(payload),
            "sha256": sha256_bytes(payload),
        }

    @staticmethod
    def _validate_payload_size(payload: bytes, *, context: str) -> None:
        if len(payload) > _MAX_EVIDENCE_BYTES:
            raise ValueError(f"{context} exceeds the 1 MB evidence limit")

    @staticmethod
    def _validate_state_evidence_coherence(
        state: DomainIntentSessionState,
        evidence: SessionEvidence,
    ) -> None:
        if len(evidence.invocations) != len(state.steps):
            raise ValueError("session snapshot evidence count differs from its state")
        for step, invocation in zip(state.steps, evidence.invocations, strict=True):
            expected_result_fingerprint = (
                None if step.communication_result is None else step.communication_result.fingerprint
            )
            expected_statuses = (
                {"failed", "blocked"} if step.communication_result is None else {"succeeded"}
            )
            if (
                invocation.invocation_id != step.invocation_id
                or invocation.session_id != state.session_id
                or invocation.execution_mode != state.execution_mode
                or invocation.provider_id != state.provider_id
                or invocation.provider_version != state.provider_version
                or invocation.provider_profile_fingerprint != state.provider_profile_fingerprint
                or invocation.model_id != state.model_id
                or invocation.model_profile_fingerprint != state.model_profile_fingerprint
                or invocation.request_fingerprint != step.request.fingerprint
                or invocation.request_fingerprint != step.approved_egress.request_fingerprint
                or invocation.egress_payload_fingerprint != step.approved_egress.input_fingerprint
                or invocation.prompt_id != step.approved_egress.prompt_id
                or invocation.prompt_version != step.approved_egress.prompt_version
                or invocation.prompt_fingerprint != step.approved_egress.prompt_fingerprint
                or invocation.response_schema_id != step.approved_egress.schema_id
                or invocation.response_schema_version != step.approved_egress.schema_version
                or invocation.response_schema_fingerprint != step.approved_egress.schema_fingerprint
                or invocation.communication_result_fingerprint != expected_result_fingerprint
                or invocation.status not in expected_statuses
            ):
                raise ValueError("session snapshot invocation evidence differs from its state")
        if state.provider_error is not None:
            final = evidence.invocations[-1]
            if (
                final.error_category != state.provider_error.category
                or final.error_code != state.provider_error.code
                or final.http_status != state.provider_error.http_status
                or final.retryable != state.provider_error.retryable
            ):
                raise ValueError(
                    "session snapshot final provider error differs from invocation evidence"
                )

    @staticmethod
    def _validate_snapshot_predecessor(
        state: DomainIntentSessionState,
        evidence: SessionEvidence,
        previous: EvidenceRecord,
    ) -> None:
        prior = previous.session_state
        if prior is None:  # pragma: no cover - read_snapshot always returns state
            raise ValueError("previous snapshot has no session state")
        if state.previous_manifest_fingerprint != previous.manifest_fingerprint:
            raise ValueError("session snapshot previous manifest fingerprint is invalid")
        if state.snapshot_index != prior.snapshot_index + 1:
            raise ValueError("session snapshot indexes are not contiguous")
        if (
            state.session_id != prior.session_id
            or state.execution_mode != prior.execution_mode
            or state.provider_id != prior.provider_id
            or state.provider_version != prior.provider_version
            or state.provider_profile_fingerprint != prior.provider_profile_fingerprint
            or state.model_id != prior.model_id
            or state.model_profile_fingerprint != prior.model_profile_fingerprint
            or state.capability_manifest_ref != prior.capability_manifest_ref
            or state.communication_policy != prior.communication_policy
            or state.communication_policy_fingerprint != prior.communication_policy_fingerprint
        ):
            raise ValueError("session snapshot changes pinned provider, model, or policy state")
        if state.steps[: len(prior.steps)] != prior.steps or len(state.steps) <= len(prior.steps):
            raise ValueError("session snapshot does not append to the prior history")
        prior_invocations = previous.evidence.invocations
        if evidence.invocations[: len(prior_invocations)] != prior_invocations or len(
            evidence.invocations
        ) <= len(prior_invocations):
            raise ValueError("session snapshot evidence does not append to the prior history")

    @staticmethod
    def _verify_snapshot_files(
        run_dir: Path,
        value: object,
    ) -> Mapping[str, bytes]:
        entries = as_sequence(value, context="session snapshot files")
        if len(entries) != 2:
            raise ValueError("session snapshot must declare exactly two data files")
        payloads: dict[str, bytes] = {}
        for item in entries:
            entry = as_mapping(item, context="session snapshot file")
            strict_keys(
                entry,
                required={"path", "size_bytes", "sha256"},
                context="session snapshot file",
            )
            path = text(entry["path"], context="session snapshot file path")
            if path not in {_EVIDENCE_FILE, _STATE_FILE} or path in payloads:
                raise ValueError("session snapshot references an unsupported or duplicate path")
            payload = EvidenceStore._read_bounded_file(
                run_dir / path,
                context=f"domain-model session snapshot {path}",
            )
            if integer(entry["size_bytes"], context="snapshot file size") != len(payload):
                raise ValueError("session snapshot file size differs from manifest")
            if digest(entry["sha256"], context="snapshot file sha256") != sha256_bytes(payload):
                raise ValueError("session snapshot file digest differs from manifest")
            payloads[path] = payload
        if set(payloads) != {_EVIDENCE_FILE, _STATE_FILE}:
            raise ValueError("session snapshot files are incomplete")
        return payloads

    @staticmethod
    def _read_bounded_file(path: Path, *, context: str) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError(f"{context} is missing or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"{context} must be a regular file")
            if before.st_size > _MAX_EVIDENCE_BYTES:
                raise ValueError(f"{context} exceeds the 1 MB evidence limit")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                payload = stream.read(_MAX_EVIDENCE_BYTES + 1)
            after = os.fstat(descriptor)
            if len(payload) > _MAX_EVIDENCE_BYTES:
                raise ValueError(f"{context} exceeds the 1 MB evidence limit")
            if (
                len(payload) != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise ValueError(f"{context} changed while it was being read")
            return payload
        finally:
            os.close(descriptor)

    @staticmethod
    def _publish(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _publish_manifest(self, run_dir: Path, payload: bytes) -> None:
        self._publish(run_dir / _MANIFEST_FILE, payload)
