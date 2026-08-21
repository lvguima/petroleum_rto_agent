"""Supplier-neutral evidence contracts for one domain-model invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

from ..contracts.common import (
    as_mapping,
    as_sequence,
    boolean,
    identifier,
    integer,
    strict_keys,
    text,
)
from ..contracts.reference import ContractRef
from .models import COMMUNICATION_SCHEMA_VERSION

DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID = "domain-model-invocation-result"
MAX_DOMAIN_MODEL_OUTPUT_BYTES: Final[int] = 128 * 1024

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
type ProviderAttemptStatus = Literal["succeeded", "failed"]
type DomainModelInvocationStatus = Literal["succeeded", "failed"]

_PROVIDER_ERROR_CATEGORIES = {
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


@dataclass(frozen=True)
class ProviderError:
    """Normalized provider failure without provider SDK objects or credentials."""

    category: ProviderErrorCategory
    code: str
    message: str
    retryable: bool
    http_status: int | None

    def __post_init__(self) -> None:
        if self.category not in _PROVIDER_ERROR_CATEGORIES:
            raise ValueError("unsupported provider error category")
        object.__setattr__(self, "code", identifier(self.code, context="provider error code"))
        object.__setattr__(
            self,
            "message",
            text(self.message, context="provider error message"),
        )
        object.__setattr__(
            self,
            "retryable",
            boolean(self.retryable, context="provider error retryable"),
        )
        if self.http_status is not None:
            status = integer(self.http_status, context="provider error http_status", minimum=100)
            if status > 599:
                raise ValueError("provider error http_status must not exceed 599")
            object.__setattr__(self, "http_status", status)
            expected_category: ProviderErrorCategory
            if status == 401:
                expected_category = "authentication"
            elif status == 402:
                expected_category = "payment"
            elif status == 403:
                expected_category = "permission"
            elif status == 404:
                expected_category = "not_found"
            elif status == 408:
                expected_category = "timeout"
            elif status == 422:
                expected_category = "invalid_request"
            elif status == 429:
                expected_category = "rate_limit"
            elif 500 <= status <= 599:
                expected_category = "provider_server"
            elif 200 <= status <= 399:
                expected_category = "protocol"
            else:
                expected_category = "invalid_request"
            if self.category != expected_category:
                raise ValueError("provider error category differs from its HTTP status")
        if self.retryable:
            retry_allowed = (
                (
                    self.category == "transport"
                    and self.code == "transport-connect"
                    and self.http_status is None
                )
                or (
                    self.category == "rate_limit"
                    and (
                        self.http_status == 429
                        or (self.http_status is None and self.code == "local-concurrency-limit")
                    )
                )
                or (
                    self.category == "provider_server"
                    and self.http_status is not None
                    and 500 <= self.http_status <= 599
                )
            )
            if not retry_allowed:
                raise ValueError("provider error is not retryable under the fixed policy")

    @classmethod
    def from_mapping(cls, value: object) -> ProviderError:
        raw = as_mapping(value, context="provider error")
        strict_keys(
            raw,
            required={"category", "code", "message", "retryable", "http_status"},
            context="provider error",
        )
        category = raw["category"]
        if category not in _PROVIDER_ERROR_CATEGORIES:
            raise ValueError("unsupported provider error category")
        http_status = raw["http_status"]
        return cls(
            category=cast(ProviderErrorCategory, category),
            code=identifier(raw["code"], context="provider error code"),
            message=text(raw["message"], context="provider error message"),
            retryable=boolean(raw["retryable"], context="provider error retryable"),
            http_status=(
                None
                if http_status is None
                else integer(http_status, context="provider error http_status", minimum=100)
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized token accounting reported by a provider."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None

    def __post_init__(self) -> None:
        input_tokens = _optional_token_count(self.input_tokens, context="input_tokens")
        output_tokens = _optional_token_count(self.output_tokens, context="output_tokens")
        total_tokens = _optional_token_count(self.total_tokens, context="total_tokens")
        known_parts = (0 if input_tokens is None else input_tokens) + (
            0 if output_tokens is None else output_tokens
        )
        if total_tokens is not None and total_tokens < known_parts:
            raise ValueError("total_tokens must not be below known token components")
        if (
            input_tokens is not None
            and output_tokens is not None
            and total_tokens is not None
            and total_tokens != input_tokens + output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        object.__setattr__(self, "input_tokens", input_tokens)
        object.__setattr__(self, "output_tokens", output_tokens)
        object.__setattr__(self, "total_tokens", total_tokens)

    @classmethod
    def from_mapping(cls, value: object) -> ProviderUsage:
        raw = as_mapping(value, context="provider usage")
        strict_keys(
            raw,
            required={"input_tokens", "output_tokens", "total_tokens"},
            context="provider usage",
        )
        return cls(
            input_tokens=_optional_token_count(raw["input_tokens"], context="input_tokens"),
            output_tokens=_optional_token_count(raw["output_tokens"], context="output_tokens"),
            total_tokens=_optional_token_count(raw["total_tokens"], context="total_tokens"),
        )

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class ProviderAttempt:
    """One provider call, including normalized provenance and outcome evidence."""

    attempt_index: int
    provider_id: str
    provider_version: str
    status: ProviderAttemptStatus
    provider_request_id: str | None
    served_model: str | None
    finish_reason: str | None
    duration_ms: int
    usage: ProviderUsage | None
    error: ProviderError | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attempt_index",
            integer(self.attempt_index, context="provider attempt_index", minimum=1),
        )
        object.__setattr__(
            self,
            "provider_id",
            identifier(self.provider_id, context="provider_id"),
        )
        object.__setattr__(
            self,
            "provider_version",
            text(self.provider_version, context="provider_version"),
        )
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("unsupported provider attempt status")
        if self.provider_request_id is not None:
            object.__setattr__(
                self,
                "provider_request_id",
                text(self.provider_request_id, context="provider_request_id"),
            )
        if self.served_model is not None:
            object.__setattr__(
                self,
                "served_model",
                text(self.served_model, context="served_model"),
            )
        if self.finish_reason is not None:
            object.__setattr__(
                self,
                "finish_reason",
                text(self.finish_reason, context="finish_reason"),
            )
        object.__setattr__(
            self,
            "duration_ms",
            integer(self.duration_ms, context="provider duration_ms", minimum=0),
        )
        if self.usage is not None and not isinstance(self.usage, ProviderUsage):
            raise TypeError("usage must be ProviderUsage or None")
        if self.status == "succeeded":
            if self.error is not None:
                raise ValueError("a succeeded provider attempt must not contain an error")
            if self.served_model is None or self.finish_reason is None:
                raise ValueError(
                    "a succeeded provider attempt requires served_model and finish_reason"
                )
        else:
            if not isinstance(self.error, ProviderError):
                raise ValueError("a failed provider attempt must contain a ProviderError")
            if self.finish_reason is not None:
                raise ValueError("a failed provider attempt must not contain a finish_reason")

    @classmethod
    def from_mapping(cls, value: object) -> ProviderAttempt:
        raw = as_mapping(value, context="provider attempt")
        strict_keys(
            raw,
            required={
                "attempt_index",
                "provider_id",
                "provider_version",
                "status",
                "provider_request_id",
                "served_model",
                "finish_reason",
                "duration_ms",
                "usage",
                "error",
            },
            context="provider attempt",
        )
        status = raw["status"]
        if status not in {"succeeded", "failed"}:
            raise ValueError("unsupported provider attempt status")
        provider_request_id = raw["provider_request_id"]
        served_model = raw["served_model"]
        finish_reason = raw["finish_reason"]
        usage = raw["usage"]
        error = raw["error"]
        return cls(
            attempt_index=integer(
                raw["attempt_index"], context="provider attempt_index", minimum=1
            ),
            provider_id=identifier(raw["provider_id"], context="provider_id"),
            provider_version=text(raw["provider_version"], context="provider_version"),
            status=status,
            provider_request_id=(
                None
                if provider_request_id is None
                else text(provider_request_id, context="provider_request_id")
            ),
            served_model=(
                None if served_model is None else text(served_model, context="served_model")
            ),
            finish_reason=(
                None if finish_reason is None else text(finish_reason, context="finish_reason")
            ),
            duration_ms=integer(raw["duration_ms"], context="provider duration_ms", minimum=0),
            usage=None if usage is None else ProviderUsage.from_mapping(usage),
            error=None if error is None else ProviderError.from_mapping(error),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_index": self.attempt_index,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "status": self.status,
            "provider_request_id": self.provider_request_id,
            "served_model": self.served_model,
            "finish_reason": self.finish_reason,
            "duration_ms": self.duration_ms,
            "usage": None if self.usage is None else self.usage.as_dict(),
            "error": None if self.error is None else self.error.as_dict(),
        }


@dataclass(frozen=True)
class DomainModelInvocationResult:
    """Provider-neutral result whose success and failure shapes cannot overlap."""

    schema_id: str
    schema_version: str
    invocation_id: str
    request_ref: ContractRef
    status: DomainModelInvocationStatus
    attempts: tuple[ProviderAttempt, ...]
    response: str | None
    error: ProviderError | None

    def __post_init__(self) -> None:
        if self.schema_id != DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID:
            raise ValueError("schema_id differs from the domain model invocation contract")
        if self.schema_version != COMMUNICATION_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the communication contract")
        object.__setattr__(
            self,
            "invocation_id",
            identifier(self.invocation_id, context="invocation_id"),
        )
        if not isinstance(self.request_ref, ContractRef):
            raise TypeError("request_ref must be ContractRef")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("unsupported domain model invocation status")
        attempts = tuple(self.attempts)
        if any(not isinstance(item, ProviderAttempt) for item in attempts):
            raise TypeError("attempts must contain only ProviderAttempt values")
        if tuple(item.attempt_index for item in attempts) != tuple(range(1, len(attempts) + 1)):
            raise ValueError("provider attempt indexes must be contiguous and start at one")
        providers = {(item.provider_id, item.provider_version) for item in attempts}
        if attempts and len(providers) != 1:
            raise ValueError("one invocation must not mix provider identities or versions")
        for item in attempts[:-1]:
            if item.status != "failed" or item.error is None or not item.error.retryable:
                raise ValueError("only a retryable failed attempt may precede another attempt")
        object.__setattr__(self, "attempts", attempts)

        if self.status == "succeeded":
            if not attempts:
                raise ValueError("succeeded invocation requires a provider attempt")
            if attempts[-1].status != "succeeded" or self.error is not None:
                raise ValueError("succeeded invocation contains inconsistent attempt or error")
            if self.response is None:
                raise ValueError("succeeded invocation must contain a response")
            response = text(self.response, context="domain model invocation response")
            try:
                response_size = len(response.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("domain model invocation response must be valid UTF-8") from exc
            if response_size > MAX_DOMAIN_MODEL_OUTPUT_BYTES:
                raise ValueError("domain model invocation response exceeds 128 KiB")
            object.__setattr__(self, "response", response)
            return

        if self.response is not None:
            raise ValueError("failed invocation must not contain a response")
        if not isinstance(self.error, ProviderError):
            raise TypeError("failed invocation must contain a final ProviderError")
        if attempts and attempts[-1].status != "failed":
            raise ValueError("failed invocation contains a succeeded final attempt")
        if attempts and attempts[-1].error != self.error:
            raise ValueError("invocation error must equal the final attempt error")

    @classmethod
    def from_mapping(cls, value: object) -> DomainModelInvocationResult:
        raw = as_mapping(value, context="domain model invocation result")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "invocation_id",
                "request_ref",
                "status",
                "attempts",
                "response",
                "error",
            },
            context="domain model invocation result",
        )
        status = raw["status"]
        if status not in {"succeeded", "failed"}:
            raise ValueError("unsupported domain model invocation status")
        response = raw["response"]
        error = raw["error"]
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            invocation_id=identifier(raw["invocation_id"], context="invocation_id"),
            request_ref=ContractRef.from_mapping(
                as_mapping(raw["request_ref"], context="request_ref")
            ),
            status=status,
            attempts=tuple(
                ProviderAttempt.from_mapping(item)
                for item in as_sequence(raw["attempts"], context="provider attempts")
            ),
            response=(
                None
                if response is None
                else text(response, context="domain model invocation response")
            ),
            error=None if error is None else ProviderError.from_mapping(error),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "request_ref": self.request_ref.as_dict(),
            "status": self.status,
            "attempts": [item.as_dict() for item in self.attempts],
            "response": self.response,
            "error": None if self.error is None else self.error.as_dict(),
        }


def _optional_token_count(value: object, *, context: str) -> int | None:
    return None if value is None else integer(value, context=context, minimum=0)
