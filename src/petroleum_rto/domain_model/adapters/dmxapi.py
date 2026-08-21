"""DMXAPI adapter for the provider-neutral domain-model invocation port."""

from __future__ import annotations

import base64
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic_ns, sleep, time
from typing import Literal, cast
from urllib.parse import quote
from uuid import uuid4

from petroleum_rto.domain_model._json import JsonValue, canonical_json_bytes
from petroleum_rto.domain_model.credentials import LocalCredentialError, load_local_dmx_api_key
from petroleum_rto.domain_model.egress import EgressGuard, EgressViolation
from petroleum_rto.domain_model.models import (
    DMX_BASE_URL,
    DMX_PROVIDER_ID,
    ApiStyle,
    ModelProfile,
    ProviderModelInfo,
    ProviderProfile,
)
from petroleum_rto.domain_model.prompt import CompiledPrompt, PromptCompiler
from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    DomainModelInvocationResult,
    DomainModelRequest,
    ProviderAttempt,
    ProviderError,
    ProviderErrorCategory,
    ProviderUsage,
)

from .transport import (
    HttpRequest,
    HttpTransport,
    HttpTransportFailure,
    HttpxTransport,
)

DMXAPI_ORIGIN = "https://www.dmxapi.cn"
DMXAPI_BASE_URL = f"{DMXAPI_ORIGIN}/v1"
DMXAPI_MODELS_PATH = "/models"
DMXAPI_CHAT_PATH = "/chat/completions"
DMXAPI_RESPONSES_PATH = "/responses"
DMXAPI_MESSAGES_PATH = "/messages"
DMXAPI_ALLOWED_PATHS = frozenset(
    {
        DMXAPI_MODELS_PATH,
        DMXAPI_CHAT_PATH,
        DMXAPI_RESPONSES_PATH,
        DMXAPI_MESSAGES_PATH,
    }
)
_DMXAPI_CONCURRENCY = BoundedSemaphore(1)
_EVIDENCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_EXPLICIT_CONFIGURATION_ERROR = re.compile(
    r"(?<![a-z0-9])(?:configuration[\s_-]*error|invalid[\s_-]*api[\s_-]*key|"
    r"invalid[\s_-]*configuration|invalid[\s_-]*model|model[\s_-]*not[\s_-]*found|"
    r"unsupported[\s_-]*model)(?![a-z0-9])",
    re.IGNORECASE,
)


def _credential_variants(credential: str) -> frozenset[str]:
    encoded = base64.b64encode(credential.encode("ascii")).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(credential.encode("ascii")).decode("ascii")
    return frozenset(
        {
            credential,
            encoded,
            encoded.rstrip("="),
            urlsafe,
            urlsafe.rstrip("="),
            quote(credential, safe=""),
        }
    )


def _contains_credential(value: object, credential: str) -> bool:
    variants = _credential_variants(credential)

    def contains(item: object) -> bool:
        if isinstance(item, str):
            return any(variant and variant in item for variant in variants)
        if isinstance(item, Mapping):
            return any(contains(key) or contains(nested) for key, nested in item.items())
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return any(contains(nested) for nested in item)
        return False

    return contains(value)


def _contains_credential_bytes(value: bytes, credential: str) -> bool:
    return any(variant.encode("ascii") in value for variant in _credential_variants(credential))


@dataclass(frozen=True)
class _RawUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class _NormalizedCompletion:
    output_text: str
    provider_request_id: str
    served_model: str
    finish_reason: str
    usage: _RawUsage


class _ResponseFailure(ValueError):
    def __init__(
        self,
        category: ProviderErrorCategory,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code


def _endpoint_url(provider: ProviderProfile, path: str) -> str:
    if path not in DMXAPI_ALLOWED_PATHS or path not in provider.allowed_paths:
        raise ValueError("DMXAPI endpoint path is not allow-listed")
    return f"{provider.base_url}{path}"


def _reject_constant(value: str) -> object:
    raise _ResponseFailure(
        "protocol",
        "non-finite-json",
        f"provider response contains non-finite JSON constant {value!r}",
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _ResponseFailure(
                "protocol",
                "duplicate-json-key",
                f"provider response contains duplicate JSON key {key!r}",
            )
        result[key] = value
    return result


def _decode_json_object(value: bytes, *, maximum_bytes: int) -> Mapping[str, object]:
    if not 0 < len(value) <= maximum_bytes:
        raise _ResponseFailure(
            "protocol",
            "raw-output-size-invalid",
            "provider response violates the configured byte limit",
        )
    try:
        source = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ResponseFailure(
            "protocol",
            "invalid-response-encoding",
            "provider response must be valid UTF-8 JSON",
        ) from exc
    try:
        decoded = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _ResponseFailure(
            "protocol",
            "invalid-response-json",
            "provider response must be valid UTF-8 JSON",
        ) from exc
    if not isinstance(decoded, Mapping) or any(not isinstance(key, str) for key in decoded):
        raise _ResponseFailure(
            "protocol",
            "invalid-response-object",
            "provider response JSON must contain one object",
        )
    return cast(Mapping[str, object], decoded)


def _decode_json_for_credential_scan(value: str, *, maximum_bytes: int) -> object | None:
    encoded = value.encode("utf-8")
    if not 0 < len(encoded) <= maximum_bytes:
        return None
    try:
        return cast(
            object,
            json.loads(
                value,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            ),
        )
    except (json.JSONDecodeError, RecursionError, ValueError, _ResponseFailure):
        return None


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _ResponseFailure(
            "protocol",
            "invalid-response-shape",
            f"provider response field {field!r} must be an object",
        )
    return cast(Mapping[str, object], value)


def _items(value: object, *, field: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _ResponseFailure(
            "protocol",
            "invalid-response-shape",
            f"provider response field {field!r} must be an array",
        )
    return tuple(value)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _ResponseFailure(
            "protocol",
            "invalid-response-shape",
            f"provider response field {field!r} must be non-empty text",
        )
    return value


def _optional_token_count(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _ResponseFailure(
            "protocol",
            "invalid-usage",
            f"provider usage field {field!r} must be a non-negative integer or null",
        )
    return value


def _usage(
    value: object,
    *,
    input_field: str,
    output_field: str,
    total_field: str,
) -> _RawUsage:
    if value is None:
        return _RawUsage(None, None, None)
    raw = _mapping(value, field="usage")
    input_tokens = _optional_token_count(raw.get(input_field), field=input_field)
    output_tokens = _optional_token_count(raw.get(output_field), field=output_field)
    total_tokens = _optional_token_count(raw.get(total_field), field=total_field)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return _RawUsage(input_tokens, output_tokens, total_tokens)


def _provider_request_id(
    payload: Mapping[str, object],
    headers: Mapping[str, str],
) -> str:
    for header_name in ("x-request-id", "request-id"):
        header_value = headers.get(header_name)
        if isinstance(header_value, str) and header_value.strip():
            return _evidence_identifier(header_value, field=header_name)
    return _evidence_identifier(_required_text(payload.get("id"), field="id"), field="id")


def _parse_openai_chat(
    payload: Mapping[str, object],
    headers: Mapping[str, str],
) -> _NormalizedCompletion:
    choices = _items(payload.get("choices"), field="choices")
    if len(choices) != 1:
        raise _ResponseFailure(
            "protocol",
            "invalid-choice-count",
            "OpenAI chat response must contain exactly one choice",
        )
    choice = _mapping(choices[0], field="choices[0]")
    message = _mapping(choice.get("message"), field="choices[0].message")
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise _ResponseFailure("refusal", "model-refusal", "provider model refused the request")
    finish_reason = _required_text(choice.get("finish_reason"), field="choices[0].finish_reason")
    if finish_reason in {"length", "max_tokens"}:
        raise _ResponseFailure(
            "truncated", "model-output-truncated", "provider model output was truncated"
        )
    if finish_reason in {"content_filter", "refusal"}:
        raise _ResponseFailure("refusal", "model-refusal", "provider model refused the request")
    if finish_reason != "stop":
        raise _ResponseFailure(
            "protocol",
            "unexpected-finish-reason",
            f"OpenAI chat response ended with unsupported reason {finish_reason!r}",
        )
    return _NormalizedCompletion(
        output_text=_required_text(message.get("content"), field="choices[0].message.content"),
        provider_request_id=_provider_request_id(payload, headers),
        served_model=_required_text(payload.get("model"), field="model"),
        finish_reason=finish_reason,
        usage=_usage(
            payload.get("usage"),
            input_field="prompt_tokens",
            output_field="completion_tokens",
            total_field="total_tokens",
        ),
    )


def _parse_openai_responses(
    payload: Mapping[str, object],
    headers: Mapping[str, str],
) -> _NormalizedCompletion:
    status = _required_text(payload.get("status"), field="status")
    if status == "incomplete":
        raise _ResponseFailure(
            "truncated", "model-output-truncated", "provider model output was incomplete"
        )
    if status != "completed":
        raise _ResponseFailure(
            "protocol",
            "unexpected-response-status",
            f"OpenAI Responses request ended with unsupported status {status!r}",
        )
    texts: list[str] = []
    for output_index, raw_output in enumerate(_items(payload.get("output"), field="output")):
        output = _mapping(raw_output, field=f"output[{output_index}]")
        output_type = _required_text(output.get("type"), field=f"output[{output_index}].type")
        if output_type == "reasoning":
            continue
        if output_type != "message":
            raise _ResponseFailure(
                "protocol",
                "unexpected-output-item",
                f"OpenAI Responses returned unsupported output item {output_type!r}",
            )
        for content_index, raw_content in enumerate(
            _items(output.get("content"), field=f"output[{output_index}].content")
        ):
            content = _mapping(
                raw_content,
                field=f"output[{output_index}].content[{content_index}]",
            )
            content_type = _required_text(
                content.get("type"),
                field=f"output[{output_index}].content[{content_index}].type",
            )
            if content_type == "refusal":
                raise _ResponseFailure(
                    "refusal", "model-refusal", "provider model refused the request"
                )
            if content_type != "output_text":
                raise _ResponseFailure(
                    "protocol",
                    "unexpected-content-item",
                    f"OpenAI Responses returned unsupported content item {content_type!r}",
                )
            texts.append(
                _required_text(
                    content.get("text"),
                    field=f"output[{output_index}].content[{content_index}].text",
                )
            )
    if not texts:
        raise _ResponseFailure(
            "protocol", "missing-output-text", "OpenAI Responses returned no output text"
        )
    return _NormalizedCompletion(
        output_text="".join(texts),
        provider_request_id=_provider_request_id(payload, headers),
        served_model=_required_text(payload.get("model"), field="model"),
        finish_reason=status,
        usage=_usage(
            payload.get("usage"),
            input_field="input_tokens",
            output_field="output_tokens",
            total_field="total_tokens",
        ),
    )


def _parse_anthropic_messages(
    payload: Mapping[str, object],
    headers: Mapping[str, str],
) -> _NormalizedCompletion:
    stop_reason = _required_text(payload.get("stop_reason"), field="stop_reason")
    if stop_reason == "max_tokens":
        raise _ResponseFailure(
            "truncated", "model-output-truncated", "provider model output was truncated"
        )
    if stop_reason == "refusal":
        raise _ResponseFailure("refusal", "model-refusal", "provider model refused the request")
    if stop_reason not in {"end_turn", "stop_sequence"}:
        raise _ResponseFailure(
            "protocol",
            "unexpected-stop-reason",
            f"Anthropic Messages response ended with unsupported reason {stop_reason!r}",
        )
    texts: list[str] = []
    for index, raw_content in enumerate(_items(payload.get("content"), field="content")):
        content = _mapping(raw_content, field=f"content[{index}]")
        content_type = _required_text(content.get("type"), field=f"content[{index}].type")
        if content_type != "text":
            raise _ResponseFailure(
                "protocol",
                "unexpected-content-item",
                f"Anthropic Messages returned unsupported content item {content_type!r}",
            )
        texts.append(_required_text(content.get("text"), field=f"content[{index}].text"))
    if not texts:
        raise _ResponseFailure(
            "protocol", "missing-output-text", "Anthropic Messages returned no text content"
        )
    return _NormalizedCompletion(
        output_text="".join(texts),
        provider_request_id=_provider_request_id(payload, headers),
        served_model=_required_text(payload.get("model"), field="model"),
        finish_reason=stop_reason,
        usage=_usage(
            payload.get("usage"),
            input_field="input_tokens",
            output_field="output_tokens",
            total_field="total_tokens",
        ),
    )


def _parse_completion(
    api_style: ApiStyle,
    body: bytes,
    headers: Mapping[str, str],
    *,
    maximum_bytes: int,
) -> _NormalizedCompletion:
    payload = _decode_json_object(body, maximum_bytes=maximum_bytes)
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    if api_style == "openai_chat":
        return _parse_openai_chat(payload, normalized_headers)
    if api_style == "openai_responses":
        return _parse_openai_responses(payload, normalized_headers)
    return _parse_anthropic_messages(payload, normalized_headers)


def _parse_models(body: bytes, *, maximum_bytes: int) -> tuple[ProviderModelInfo, ...]:
    payload = _decode_json_object(body, maximum_bytes=maximum_bytes)
    raw_models = _items(payload.get("data"), field="data")
    models: list[ProviderModelInfo] = []
    for index, item in enumerate(raw_models):
        raw = _mapping(item, field=f"data[{index}]")
        model_id = _required_text(raw.get("id"), field=f"data[{index}].id")
        owned_by = raw.get("owned_by")
        if owned_by is not None:
            owned_by = _required_text(owned_by, field=f"data[{index}].owned_by")
        metadata = {
            key: cast(JsonValue, value)
            for key, value in raw.items()
            if key not in {"id", "owned_by"}
        }
        models.append(
            ProviderModelInfo(
                id=model_id,
                owned_by=owned_by,
                metadata=metadata,
            )
        )
    model_ids = tuple(item.id for item in models)
    if len(model_ids) != len(set(model_ids)):
        raise _ResponseFailure(
            "protocol", "duplicate-model-id", "provider models response contains duplicate ids"
        )
    return tuple(models)


@dataclass(frozen=True)
class _FailureEvidence:
    provider_request_id: str | None
    served_model: str | None
    usage: ProviderUsage | None


class _AdapterFailure(RuntimeError):
    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class ModelDiscoveryAttempt:
    """Safe evidence for one physical `/models` request."""

    attempt_index: int
    status: Literal["succeeded", "failed"]
    provider_request_id: str | None
    duration_ms: int
    error: ProviderError | None

    def __post_init__(self) -> None:
        if self.attempt_index < 1:
            raise ValueError("model discovery attempt_index must be positive")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("model discovery attempt status is unsupported")
        if self.provider_request_id is not None:
            object.__setattr__(
                self,
                "provider_request_id",
                _evidence_identifier(self.provider_request_id, field="provider_request_id"),
            )
        if self.duration_ms < 0:
            raise ValueError("model discovery duration_ms must not be negative")
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("succeeded model discovery attempt cannot contain an error")
        if self.status == "failed" and not isinstance(self.error, ProviderError):
            raise ValueError("failed model discovery attempt requires ProviderError")

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_index": self.attempt_index,
            "status": self.status,
            "provider_request_id": self.provider_request_id,
            "duration_ms": self.duration_ms,
            "error": None if self.error is None else self.error.as_dict(),
        }


@dataclass(frozen=True)
class ModelDiscoveryInvocationResult:
    """One `/models` operation, including every physical attempt and no raw body."""

    invocation_id: str
    provider_id: str
    provider_version: str
    status: Literal["succeeded", "failed"]
    attempts: tuple[ModelDiscoveryAttempt, ...]
    models: tuple[ProviderModelInfo, ...]
    error: ProviderError | None

    def __post_init__(self) -> None:
        if not _EVIDENCE_IDENTIFIER.fullmatch(self.invocation_id):
            raise ValueError("model discovery invocation_id is unsafe")
        if self.provider_id != DMX_PROVIDER_ID:
            raise ValueError("model discovery provider_id is unsupported")
        attempts = tuple(self.attempts)
        if tuple(item.attempt_index for item in attempts) != tuple(range(1, len(attempts) + 1)):
            raise ValueError("model discovery attempts must be contiguous")
        models = tuple(self.models)
        if any(not isinstance(item, ProviderModelInfo) for item in models):
            raise TypeError("model discovery models must contain ProviderModelInfo values")
        if self.status == "succeeded":
            if self.error is not None or not attempts or attempts[-1].status != "succeeded":
                raise ValueError("succeeded model discovery result has an invalid shape")
        elif self.status == "failed":
            if not isinstance(self.error, ProviderError) or models:
                raise ValueError("failed model discovery result has an invalid shape")
            if attempts and attempts[-1].error != self.error:
                raise ValueError("model discovery final attempt differs from its error")
        else:
            raise ValueError("model discovery status is unsupported")
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "models", models)

    def as_dict(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "endpoint_path": DMXAPI_MODELS_PATH,
            "status": self.status,
            "attempts": [item.as_dict() for item in self.attempts],
            "models": [{"model_id": item.id, "owned_by": item.owned_by} for item in self.models],
            "error": None if self.error is None else self.error.as_dict(),
        }


class DmxApiError(RuntimeError):
    """A provider-neutral failure from the model-discovery operation."""

    def __init__(
        self,
        error: ProviderError,
        *,
        invocation: ModelDiscoveryInvocationResult | None = None,
        evidence_manifest: str | None = None,
        evidence_fingerprint: str | None = None,
    ) -> None:
        super().__init__(error.message)
        self.error = error
        self.invocation = invocation
        self.evidence_manifest = evidence_manifest
        self.evidence_fingerprint = evidence_fingerprint


def _optional_header_request_id(headers: Mapping[str, str]) -> str | None:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    for name in ("x-request-id", "request-id"):
        value = normalized.get(name)
        if value is not None and value.strip():
            try:
                return _evidence_identifier(value, field=name)
            except _ResponseFailure:
                return None
    return None


def _evidence_identifier(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not _EVIDENCE_IDENTIFIER.fullmatch(normalized):
        raise _ResponseFailure(
            "protocol",
            "invalid-provider-request-id",
            f"provider response field {field!r} is not a safe evidence identifier",
        )
    return normalized


def _retry_after_seconds(
    headers: Mapping[str, str],
    *,
    wall_time: float,
    maximum_seconds: float,
) -> float:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    raw = normalized.get("retry-after")
    if raw is None or len(raw) > 128:
        return 0.0
    value = raw.strip()
    if value.isascii() and value.isdigit():
        return min(float(value), maximum_seconds)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            return 0.0
        delay = retry_at.timestamp() - wall_time
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(delay) or delay <= 0:
        return 0.0
    return min(delay, maximum_seconds)


def _provider_usage(raw: _RawUsage) -> ProviderUsage | None:
    if raw == _RawUsage(None, None, None):
        return None
    try:
        return ProviderUsage(
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            total_tokens=raw.total_tokens,
        )
    except (TypeError, ValueError) as exc:
        raise _ResponseFailure(
            "protocol",
            "invalid-usage",
            "provider returned inconsistent token usage",
        ) from exc


def _failure_evidence(
    api_style: ApiStyle,
    body: bytes,
    headers: Mapping[str, str],
    *,
    maximum_bytes: int,
) -> _FailureEvidence:
    request_id = _optional_header_request_id(headers)
    try:
        payload = _decode_json_object(body, maximum_bytes=maximum_bytes)
    except _ResponseFailure:
        return _FailureEvidence(request_id, None, None)
    if request_id is None:
        raw_id = payload.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            try:
                request_id = _evidence_identifier(raw_id, field="id")
            except _ResponseFailure:
                request_id = None
    raw_model = payload.get("model")
    served_model = (
        raw_model.strip()
        if isinstance(raw_model, str)
        and raw_model.strip()
        and len(raw_model.strip()) <= 256
        and all(33 <= ord(character) <= 126 for character in raw_model.strip())
        else None
    )
    try:
        if api_style == "openai_chat":
            usage = _usage(
                payload.get("usage"),
                input_field="prompt_tokens",
                output_field="completion_tokens",
                total_field="total_tokens",
            )
        else:
            usage = _usage(
                payload.get("usage"),
                input_field="input_tokens",
                output_field="output_tokens",
                total_field="total_tokens",
            )
        normalized_usage = _provider_usage(usage)
    except _ResponseFailure:
        normalized_usage = None
    return _FailureEvidence(request_id, served_model, normalized_usage)


def _transport_error(failure: HttpTransportFailure) -> ProviderError:
    if failure.kind in {"read-timeout", "write-timeout"}:
        category: ProviderErrorCategory = "timeout"
    elif failure.kind == "response-too-large":
        category = "protocol"
    else:
        category = "transport"
    retryable = failure.kind == "connect" and failure.retryable_before_send
    return ProviderError(
        category=category,
        code=f"transport-{failure.kind}",
        message=f"domain-model transport failed ({failure.kind})",
        retryable=retryable,
        http_status=None,
    )


def _http_error(status_code: int) -> ProviderError:
    if not 100 <= status_code <= 599:
        return ProviderError(
            category="protocol",
            code="invalid-http-status",
            message="provider returned an invalid HTTP status",
            retryable=False,
            http_status=None,
        )
    if status_code == 401:
        category: ProviderErrorCategory = "authentication"
        code = "http-401-authentication"
    elif status_code == 402:
        category = "payment"
        code = "http-402-payment"
    elif status_code == 403:
        category = "permission"
        code = "http-403-permission"
    elif status_code == 404:
        category = "not_found"
        code = "http-404-not-found"
    elif status_code == 408:
        category = "timeout"
        code = "http-408-timeout"
    elif status_code == 422:
        category = "invalid_request"
        code = "http-422-invalid-request"
    elif status_code == 429:
        category = "rate_limit"
        code = "http-429-rate-limit"
    elif 500 <= status_code <= 599:
        category = "provider_server"
        code = "http-5xx-provider-server"
    elif 100 <= status_code <= 299:
        category = "protocol"
        code = "unexpected-http-status"
    elif 300 <= status_code <= 399:
        category = "protocol"
        code = "redirect-response-forbidden"
    else:
        category = "invalid_request"
        code = "http-invalid-request"
    return ProviderError(
        category=category,
        code=code,
        message=f"provider returned HTTP {status_code}",
        retryable=status_code == 429 or 500 <= status_code <= 599,
        http_status=status_code,
    )


def _http_error_with_body(
    status_code: int,
    body: bytes,
    *,
    maximum_bytes: int,
) -> ProviderError:
    default = _http_error(status_code)
    if not 500 <= status_code <= 599:
        return default
    try:
        payload = _decode_json_object(body, maximum_bytes=maximum_bytes)
    except _ResponseFailure:
        return default
    markers = {
        re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", item.lower()) for item in _flatten_text(payload)
    }
    if markers.intersection(
        {
            "insufficientbalance",
            "balanceinsufficient",
            "insufficientquota",
            "quotaexceeded",
            "余额不足",
            "额度不足",
            "配额不足",
            "欠费",
        }
    ):
        return ProviderError(
            category="provider_server",
            code="provider-balance-or-quota-insufficient",
            message="provider rejected the request because balance or quota is insufficient",
            retryable=False,
            http_status=status_code,
        )
    explicit_configuration_error = any(
        _EXPLICIT_CONFIGURATION_ERROR.search(item)
        or any(marker in item for marker in ("配置错误", "模型不存在", "模型不支持"))
        for item in _flatten_string_values(payload)
    )
    if explicit_configuration_error:
        return ProviderError(
            category="provider_server",
            code="provider-configuration-error",
            message="provider rejected a model or account configuration",
            retryable=False,
            http_status=status_code,
        )
    return default


def _flatten_text(value: object) -> tuple[str, ...]:
    result: list[str] = []

    def visit(item: object, *, depth: int) -> None:
        if depth > 32:
            return
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, Mapping):
            for key, nested in item.items():
                result.append(str(key))
                visit(nested, depth=depth + 1)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested, depth=depth + 1)

    visit(value, depth=0)
    return tuple(result)


def _flatten_string_values(value: object) -> tuple[str, ...]:
    result: list[str] = []

    def visit(item: object, *, depth: int) -> None:
        if depth > 32:
            return
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, Mapping):
            for nested in item.values():
                visit(nested, depth=depth + 1)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested, depth=depth + 1)

    visit(value, depth=0)
    return tuple(result)


def _response_error(failure: _ResponseFailure) -> ProviderError:
    return ProviderError(
        category=failure.category,
        code=failure.code,
        message=f"provider response failed validation ({failure.code})",
        retryable=False,
        http_status=None,
    )


def _default_invocation_id() -> str:
    return f"dmx-invocation-{uuid4().hex}"


class DmxApiAdapter:
    """DMXAPI implementation of the provider-neutral domain-model port."""

    def __init__(
        self,
        *,
        provider_profile: ProviderProfile,
        model_profile: ModelProfile,
        transport: HttpTransport | None = None,
        clock_ns: Callable[[], int] = monotonic_ns,
        wall_clock: Callable[[], float] = time,
        sleeper: Callable[[float], None] = sleep,
        invocation_id_factory: Callable[[], str] = _default_invocation_id,
        credential_file: Path | None = None,
    ) -> None:
        if provider_profile.provider_id != DMX_PROVIDER_ID:
            raise ValueError("DmxApiAdapter requires the fixed dmx-cn provider profile")
        if (
            provider_profile.base_url != DMX_BASE_URL
            or provider_profile.base_url != DMXAPI_BASE_URL
        ):
            raise ValueError("DmxApiAdapter requires the fixed DMXAPI base URL")
        if set(provider_profile.allowed_paths) != set(DMXAPI_ALLOWED_PATHS):
            raise ValueError("DmxApiAdapter requires the complete fixed endpoint allow-list")
        if provider_profile.model(model_profile.model_id) != model_profile:
            raise ValueError("model profile is not pinned by the provider profile")
        endpoint = provider_profile.endpoint(model_profile.model_id)
        if endpoint != _endpoint_url(provider_profile, model_profile.endpoint_path):
            raise ValueError("model endpoint differs from the fixed DMXAPI endpoint")
        selected_transport = transport or HttpxTransport()
        self._provider = provider_profile
        self._model = model_profile
        self._egress_guard = EgressGuard()
        self._prompt_compiler = PromptCompiler(egress_guard=self._egress_guard)
        self._transport = selected_transport
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._invocation_id_factory = invocation_id_factory
        if credential_file is not None and not isinstance(credential_file, Path):
            raise TypeError("credential_file must be pathlib.Path or None")
        self._credential_file = credential_file
        if provider_profile.maximum_concurrency != 1:  # pragma: no cover - profile guard
            raise ValueError("DMXAPI process concurrency is fixed at one")
        self._concurrency = _DMXAPI_CONCURRENCY

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    @property
    def provider_version(self) -> str:
        return self._provider.profile_version

    @property
    def model_id(self) -> str:
        return self._model.model_id

    def invoke(self, request: DomainModelRequest) -> DomainModelInvocationResult:
        return self.invoke_with_timeout(
            request,
            timeout_seconds=self._provider.round_timeout_seconds,
        )

    def invoke_with_timeout(
        self,
        request: DomainModelRequest,
        *,
        timeout_seconds: float,
    ) -> DomainModelInvocationResult:
        """Invoke within both the profile ceiling and the caller's remaining round budget."""

        invocation_id = self._invocation_id_factory()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            return self._local_failure(invocation_id, request, self._deadline_error())
        bounded_timeout = min(float(timeout_seconds), self._provider.round_timeout_seconds)
        if not self._concurrency.acquire(blocking=False):
            return self._local_failure(invocation_id, request, self._concurrency_error())
        try:
            return self._invoke_with_slot(
                request,
                invocation_id=invocation_id,
                timeout_seconds=bounded_timeout,
            )
        finally:
            self._concurrency.release()

    def _invoke_with_slot(
        self,
        request: DomainModelRequest,
        *,
        invocation_id: str,
        timeout_seconds: float,
    ) -> DomainModelInvocationResult:
        credential = self._credential_if_valid()
        if credential is not None and _contains_credential(request.as_dict(), credential):
            return self._local_failure(
                invocation_id,
                request,
                ProviderError(
                    category="invalid_request",
                    code="egress-suspected-credential",
                    message="domain-model request was blocked by outbound policy",
                    retryable=False,
                    http_status=None,
                ),
            )
        deadline_ns = self._deadline_ns(timeout_seconds)
        try:
            if credential is None:
                credential = self._credential()
            compiled = self._prompt_compiler.compile(request)
            if compiled.request_fingerprint != request.fingerprint:
                raise _AdapterFailure(
                    ProviderError(
                        category="protocol",
                        code="compiled-request-fingerprint-mismatch",
                        message="compiled prompt references another domain-model request",
                        retryable=False,
                        http_status=None,
                    )
                )
            body = self._compile_wire_body(compiled)
        except _AdapterFailure as failure:
            return self._local_failure(invocation_id, request, failure.error)
        except EgressViolation as failure:
            error = ProviderError(
                category="invalid_request",
                code=f"egress-{failure.code}",
                message="domain-model request was blocked by outbound policy",
                retryable=False,
                http_status=None,
            )
            return self._local_failure(invocation_id, request, error)

        http_request = HttpRequest(
            method="POST",
            url=self._provider.endpoint(self._model.model_id),
            headers=self._headers(credential),
            body=body,
            connect_timeout_seconds=self._provider.connect_timeout_seconds,
            read_timeout_seconds=self._provider.read_timeout_seconds,
            total_timeout_seconds=(
                self._provider.connect_timeout_seconds + self._provider.read_timeout_seconds
            ),
            max_response_bytes=self._provider.maximum_raw_response_bytes,
        )
        attempts: list[ProviderAttempt] = []
        for attempt_index in range(1, self._provider.maximum_physical_attempts + 1):
            if not self._has_attempt_budget(deadline_ns):
                error = self._deadline_error()
                if attempts:
                    return self._failed_result(
                        invocation_id,
                        request,
                        attempts,
                        cast(ProviderError, attempts[-1].error),
                    )
                return self._local_failure(invocation_id, request, error)
            started_ns = self._clock_ns()
            try:
                response = self._transport.send(http_request)
            except HttpTransportFailure as failure:
                error = _transport_error(failure)
                attempts.append(
                    self._failed_attempt(
                        attempt_index,
                        started_ns,
                        error,
                        provider_request_id=None,
                        served_model=None,
                        usage=None,
                    )
                )
                if (
                    error.retryable
                    and attempt_index < self._provider.maximum_physical_attempts
                    and self._has_attempt_budget(deadline_ns)
                ):
                    continue
                return self._failed_result(invocation_id, request, attempts, error)

            if _contains_credential(response.headers, credential) or _contains_credential_bytes(
                response.body, credential
            ):
                error = ProviderError(
                    category="protocol",
                    code="provider-credential-reflection",
                    message="provider response contained credential material",
                    retryable=False,
                    http_status=None,
                )
                attempts.append(
                    self._failed_attempt(
                        attempt_index,
                        started_ns,
                        error,
                        provider_request_id=None,
                        served_model=None,
                        usage=None,
                    )
                )
                return self._failed_result(invocation_id, request, attempts, error)

            if self._clock_ns() >= deadline_ns:
                error = self._elapsed_deadline_error()
                attempts.append(
                    self._failed_attempt(
                        attempt_index,
                        started_ns,
                        error,
                        provider_request_id=_optional_header_request_id(response.headers),
                        served_model=None,
                        usage=None,
                    )
                )
                return self._failed_result(invocation_id, request, attempts, error)

            if response.status_code != 200:
                error = _http_error_with_body(
                    response.status_code,
                    response.body,
                    maximum_bytes=self._provider.maximum_raw_response_bytes,
                )
                attempts.append(
                    self._failed_attempt(
                        attempt_index,
                        started_ns,
                        error,
                        provider_request_id=_optional_header_request_id(response.headers),
                        served_model=None,
                        usage=None,
                    )
                )
                if (
                    error.retryable
                    and attempt_index < self._provider.maximum_physical_attempts
                    and self._wait_for_retry(response.headers, deadline_ns)
                ):
                    # Attribute provider-directed backoff to the failed attempt
                    # so invocation evidence reflects wall-clock elapsed time.
                    attempts[-1] = replace(
                        attempts[-1],
                        duration_ms=self._duration_ms(started_ns),
                    )
                    continue
                return self._failed_result(invocation_id, request, attempts, error)

            try:
                completion = _parse_completion(
                    self._model.api_style,
                    response.body,
                    response.headers,
                    maximum_bytes=self._provider.maximum_raw_response_bytes,
                )
                if _contains_credential(
                    (
                        completion.output_text,
                        _decode_json_for_credential_scan(
                            completion.output_text,
                            maximum_bytes=self._provider.maximum_raw_response_bytes,
                        ),
                        completion.provider_request_id,
                        completion.served_model,
                        completion.finish_reason,
                    ),
                    credential,
                ):
                    raise _ResponseFailure(
                        "protocol",
                        "provider-credential-reflection",
                        "provider response contained credential material",
                    )
                usage = _provider_usage(completion.usage)
                if completion.served_model not in self._model.allowed_served_model_ids:
                    raise _ResponseFailure(
                        "model_mismatch",
                        "served-model-mismatch",
                        "provider served a model different from the pinned model",
                    )
            except _ResponseFailure as parse_failure:
                error = _response_error(parse_failure)
                evidence = _failure_evidence(
                    self._model.api_style,
                    response.body,
                    response.headers,
                    maximum_bytes=self._provider.maximum_raw_response_bytes,
                )
                if parse_failure.code == "provider-credential-reflection":
                    evidence = _FailureEvidence(None, None, None)
                elif _contains_credential(
                    (evidence.provider_request_id, evidence.served_model),
                    credential,
                ):
                    error = _response_error(
                        _ResponseFailure(
                            "protocol",
                            "provider-credential-reflection",
                            "provider response contained credential material",
                        )
                    )
                    evidence = _FailureEvidence(None, None, None)
                attempts.append(
                    self._failed_attempt(
                        attempt_index,
                        started_ns,
                        error,
                        provider_request_id=evidence.provider_request_id,
                        served_model=evidence.served_model,
                        usage=evidence.usage,
                    )
                )
                return self._failed_result(invocation_id, request, attempts, error)

            attempts.append(
                ProviderAttempt(
                    attempt_index=attempt_index,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status="succeeded",
                    provider_request_id=completion.provider_request_id,
                    served_model=completion.served_model,
                    finish_reason=completion.finish_reason,
                    duration_ms=self._duration_ms(started_ns),
                    usage=usage,
                    error=None,
                )
            )
            return DomainModelInvocationResult(
                schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
                schema_version=COMMUNICATION_SCHEMA_VERSION,
                invocation_id=invocation_id,
                request_ref=request.ref,
                status="succeeded",
                attempts=tuple(attempts),
                response=completion.output_text,
                error=None,
            )
        raise AssertionError("fixed transport-attempt loop did not return")

    def list_models(self) -> tuple[ProviderModelInfo, ...]:
        """Discover provider models without treating discovery as authoritative config."""

        result = self.discover_models()
        if result.status == "failed":
            assert result.error is not None
            raise DmxApiError(result.error, invocation=result)
        return result.models

    def discover_models(self) -> ModelDiscoveryInvocationResult:
        """Return safe attempt evidence together with non-authoritative model rows."""

        invocation_id = self._invocation_id_factory()
        if not self._concurrency.acquire(blocking=False):
            return self._discovery_failure(
                invocation_id,
                (),
                self._concurrency_error(),
            )
        try:
            return self._list_models_with_slot(invocation_id)
        finally:
            self._concurrency.release()

    def _list_models_with_slot(self, invocation_id: str) -> ModelDiscoveryInvocationResult:
        deadline_ns = self._deadline_ns()
        try:
            credential = self._credential()
        except _AdapterFailure as failure:
            return self._discovery_failure(invocation_id, (), failure.error)
        request = HttpRequest(
            method="GET",
            url=_endpoint_url(self._provider, DMXAPI_MODELS_PATH),
            headers=self._headers(credential),
            body=None,
            connect_timeout_seconds=self._provider.connect_timeout_seconds,
            read_timeout_seconds=self._provider.read_timeout_seconds,
            total_timeout_seconds=(
                self._provider.connect_timeout_seconds + self._provider.read_timeout_seconds
            ),
            max_response_bytes=self._provider.maximum_raw_response_bytes,
        )
        attempts: list[ModelDiscoveryAttempt] = []
        for attempt_index in range(1, self._provider.maximum_physical_attempts + 1):
            if not self._has_attempt_budget(deadline_ns):
                return self._discovery_failure(invocation_id, attempts, self._deadline_error())
            started_ns = self._clock_ns()
            try:
                response = self._transport.send(request)
            except HttpTransportFailure as failure:
                error = _transport_error(failure)
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="failed",
                        provider_request_id=None,
                        duration_ms=self._duration_ms(started_ns),
                        error=error,
                    )
                )
                if (
                    error.retryable
                    and attempt_index < self._provider.maximum_physical_attempts
                    and self._has_attempt_budget(deadline_ns)
                ):
                    continue
                return self._discovery_failure(invocation_id, attempts, error)
            if _contains_credential(response.headers, credential) or _contains_credential_bytes(
                response.body, credential
            ):
                error = ProviderError(
                    category="protocol",
                    code="provider-credential-reflection",
                    message="provider response contained credential material",
                    retryable=False,
                    http_status=None,
                )
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="failed",
                        provider_request_id=None,
                        duration_ms=self._duration_ms(started_ns),
                        error=error,
                    )
                )
                return self._discovery_failure(invocation_id, attempts, error)
            if self._clock_ns() >= deadline_ns:
                error = self._elapsed_deadline_error()
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="failed",
                        provider_request_id=_optional_header_request_id(response.headers),
                        duration_ms=self._duration_ms(started_ns),
                        error=error,
                    )
                )
                return self._discovery_failure(invocation_id, attempts, error)
            if response.status_code != 200:
                error = _http_error_with_body(
                    response.status_code,
                    response.body,
                    maximum_bytes=self._provider.maximum_raw_response_bytes,
                )
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="failed",
                        provider_request_id=_optional_header_request_id(response.headers),
                        duration_ms=self._duration_ms(started_ns),
                        error=error,
                    )
                )
                if (
                    error.retryable
                    and attempt_index < self._provider.maximum_physical_attempts
                    and self._wait_for_retry(response.headers, deadline_ns)
                ):
                    attempts[-1] = replace(
                        attempts[-1],
                        duration_ms=self._duration_ms(started_ns),
                    )
                    continue
                return self._discovery_failure(invocation_id, attempts, error)
            try:
                models = _parse_models(
                    response.body,
                    maximum_bytes=self._provider.maximum_raw_response_bytes,
                )
                if _contains_credential(
                    tuple(item.as_dict() for item in models),
                    credential,
                ):
                    raise _ResponseFailure(
                        "protocol",
                        "provider-credential-reflection",
                        "provider response contained credential material",
                    )
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="succeeded",
                        provider_request_id=_optional_header_request_id(response.headers),
                        duration_ms=self._duration_ms(started_ns),
                        error=None,
                    )
                )
                return ModelDiscoveryInvocationResult(
                    invocation_id=invocation_id,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status="succeeded",
                    attempts=tuple(attempts),
                    models=models,
                    error=None,
                )
            except _ResponseFailure as failure:
                error = _response_error(failure)
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="failed",
                        provider_request_id=(
                            None
                            if failure.code == "provider-credential-reflection"
                            else _optional_header_request_id(response.headers)
                        ),
                        duration_ms=self._duration_ms(started_ns),
                        error=error,
                    )
                )
                return self._discovery_failure(invocation_id, attempts, error)
            except (TypeError, ValueError, RecursionError):
                normalized = _ResponseFailure(
                    "protocol",
                    "invalid-model-discovery-response",
                    "provider model discovery response failed strict validation",
                )
                error = _response_error(normalized)
                attempts.append(
                    ModelDiscoveryAttempt(
                        attempt_index=attempt_index,
                        status="failed",
                        provider_request_id=_optional_header_request_id(response.headers),
                        duration_ms=self._duration_ms(started_ns),
                        error=error,
                    )
                )
                return self._discovery_failure(invocation_id, attempts, error)
        raise AssertionError("fixed model-discovery attempt loop did not return")

    def _discovery_failure(
        self,
        invocation_id: str,
        attempts: Sequence[ModelDiscoveryAttempt],
        error: ProviderError,
    ) -> ModelDiscoveryInvocationResult:
        return ModelDiscoveryInvocationResult(
            invocation_id=invocation_id,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status="failed",
            attempts=tuple(attempts),
            models=(),
            error=error,
        )

    def _credential_if_valid(self) -> str | None:
        credential = os.environ.get(self._provider.credential_env)
        if credential is None and self._credential_file is not None:
            try:
                credential = load_local_dmx_api_key(self._credential_file)
            except LocalCredentialError:
                return None
        if (
            not isinstance(credential, str)
            or len(credential) < 8
            or len(credential) > 2048
            or any(not 33 <= ord(character) <= 126 for character in credential)
        ):
            return None
        return credential

    def _credential(self) -> str:
        credential = self._credential_if_valid()
        if credential is None:
            raise _AdapterFailure(
                ProviderError(
                    category="authentication",
                    code="credential-missing-or-invalid",
                    message="domain-model provider credential is unavailable or invalid",
                    retryable=False,
                    http_status=None,
                )
            )
        return credential

    def _compile_wire_body(self, compiled: CompiledPrompt) -> bytes:
        # CompiledPrompt pins the trusted instruction byte-for-byte and scans
        # its untrusted input JSON. User-data heuristics must not scan the
        # instruction itself because it names the forbidden concepts it guards.
        if self._model.api_style == "openai_chat":
            payload: dict[str, object] = {
                "model": self._model.model_id,
                "messages": [
                    {"role": "system", "content": compiled.system_instruction},
                    {"role": "user", "content": compiled.input_json},
                ],
                "max_tokens": self._model.maximum_output_tokens,
                "stream": False,
            }
            if self._model.output_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            elif self._model.output_mode == "json_schema_strict":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": compiled.schema_id,
                        "strict": True,
                        "schema": compiled.response_schema,
                    },
                }
        elif self._model.api_style == "openai_responses":
            payload = {
                "model": self._model.model_id,
                "instructions": compiled.system_instruction,
                "input": compiled.input_json,
                "max_output_tokens": self._model.maximum_output_tokens,
                "stream": False,
            }
            if self._model.output_mode == "json_object":
                payload["text"] = {"format": {"type": "json_object"}}
            elif self._model.output_mode == "json_schema_strict":
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": compiled.schema_id,
                        "strict": True,
                        "schema": compiled.response_schema,
                    }
                }
        else:
            payload = {
                "model": self._model.model_id,
                "system": compiled.system_instruction,
                "messages": [{"role": "user", "content": compiled.input_json}],
                "max_tokens": self._model.maximum_output_tokens,
                "stream": False,
            }
        try:
            body = canonical_json_bytes(payload)
        except (TypeError, ValueError) as exc:  # pragma: no cover - trusted construction
            raise EgressViolation(
                "invalid-json-request",
                "provider request must contain only finite JSON values",
            ) from exc
        if len(body) > self._egress_guard.max_request_bytes:
            raise EgressViolation(
                "request-too-large",
                "provider request exceeds the 256 KiB policy",
            )
        return body

    def _headers(self, credential: str) -> Mapping[str, str]:
        headers = {
            "accept": "application/json",
            "authorization": credential,
            "content-type": "application/json",
        }
        if self._model.api_style == "anthropic_messages":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _deadline_ns(self, timeout_seconds: float | None = None) -> int:
        effective_timeout = (
            self._provider.round_timeout_seconds
            if timeout_seconds is None
            else min(timeout_seconds, self._provider.round_timeout_seconds)
        )
        return self._clock_ns() + int(effective_timeout * 1_000_000_000)

    def _has_attempt_budget(self, deadline_ns: int) -> bool:
        minimum_ns = int(
            (self._provider.connect_timeout_seconds + self._provider.read_timeout_seconds)
            * 1_000_000_000
        )
        return deadline_ns - self._clock_ns() >= minimum_ns

    def _wait_for_retry(self, headers: Mapping[str, str], deadline_ns: int) -> bool:
        delay = _retry_after_seconds(
            headers,
            wall_time=self._wall_clock(),
            maximum_seconds=self._provider.maximum_retry_after_seconds,
        )
        required_seconds = (
            self._provider.connect_timeout_seconds + self._provider.read_timeout_seconds + delay
        )
        remaining_ns = deadline_ns - self._clock_ns()
        if remaining_ns < int(required_seconds * 1_000_000_000):
            return False
        if delay > 0:
            self._sleeper(delay)
        return self._has_attempt_budget(deadline_ns)

    @staticmethod
    def _deadline_error() -> ProviderError:
        return ProviderError(
            category="timeout",
            code="semantic-call-deadline-exhausted",
            message="domain-model semantic call has insufficient deadline for another attempt",
            retryable=False,
            http_status=None,
        )

    @staticmethod
    def _elapsed_deadline_error() -> ProviderError:
        return ProviderError(
            category="timeout",
            code="semantic-call-deadline-exceeded",
            message="domain-model semantic call exceeded its absolute deadline",
            retryable=False,
            http_status=None,
        )

    @staticmethod
    def _concurrency_error() -> ProviderError:
        return ProviderError(
            category="rate_limit",
            code="local-concurrency-limit",
            message="domain-model provider concurrency limit is already in use",
            retryable=True,
            http_status=None,
        )

    def _duration_ms(self, started_ns: int) -> int:
        return max(0, (self._clock_ns() - started_ns) // 1_000_000)

    def _failed_attempt(
        self,
        attempt_index: int,
        started_ns: int,
        error: ProviderError,
        *,
        provider_request_id: str | None,
        served_model: str | None,
        usage: ProviderUsage | None,
    ) -> ProviderAttempt:
        return ProviderAttempt(
            attempt_index=attempt_index,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status="failed",
            provider_request_id=provider_request_id,
            served_model=served_model,
            finish_reason=None,
            duration_ms=self._duration_ms(started_ns),
            usage=usage,
            error=error,
        )

    def _local_failure(
        self,
        invocation_id: str,
        request: DomainModelRequest,
        error: ProviderError,
    ) -> DomainModelInvocationResult:
        return self._failed_result(invocation_id, request, (), error)

    @staticmethod
    def _failed_result(
        invocation_id: str,
        request: DomainModelRequest,
        attempts: Sequence[ProviderAttempt],
        error: ProviderError,
    ) -> DomainModelInvocationResult:
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id=invocation_id,
            request_ref=request.ref,
            status="failed",
            attempts=tuple(attempts),
            response=None,
            error=error,
        )
