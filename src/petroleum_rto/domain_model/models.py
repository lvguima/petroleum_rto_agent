"""Strict provider and model profiles with exact endpoint allow-listing."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast
from urllib.parse import urlsplit

from ._json import (
    JsonValue,
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    freeze_json_mapping,
    identifier,
    strict_keys,
    text,
    thaw_json,
    version,
)

PROVIDER_CATALOG_SCHEMA_ID: Final[str] = "domain-model-provider-catalog"
PROVIDER_CATALOG_SCHEMA_VERSION: Final[str] = "1.3.0"
DMX_PROVIDER_ID: Final[str] = "dmx-cn"
DMX_ENDPOINT_ID: Final[str] = "dmx-cn-v1"
DMX_BASE_URL: Final[str] = "https://www.dmxapi.cn/v1"
DMX_CREDENTIAL_ENV: Final[str] = "PETROLEUM_RTO_DOMAIN_MODEL_API_KEY"

type ApiStyle = Literal["openai_chat", "openai_responses", "anthropic_messages"]
type OutputMode = Literal["prompt_json", "json_object", "json_schema_strict"]

_API_PATHS: Final[Mapping[ApiStyle, str]] = MappingProxyType(
    {
        "openai_chat": "/chat/completions",
        "openai_responses": "/responses",
        "anthropic_messages": "/messages",
    }
)
_DMX_ALLOWED_PATHS: Final[frozenset[str]] = frozenset({"/models", *_API_PATHS.values()})
_ENV_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OUTPUT_MODES: Final[frozenset[str]] = frozenset(
    {"prompt_json", "json_object", "json_schema_strict"}
)

_DMX_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
_DMX_READ_TIMEOUT_SECONDS: Final[float] = 45.0
_DMX_ROUND_TIMEOUT_SECONDS: Final[float] = 120.0
_DMX_MAXIMUM_PHYSICAL_ATTEMPTS: Final[int] = 2
_DMX_MAXIMUM_CONCURRENCY: Final[int] = 1
_DMX_MAXIMUM_RETRY_AFTER_SECONDS: Final[float] = 20.0
_DMX_MAXIMUM_RAW_RESPONSE_BYTES: Final[int] = 128 * 1024


def _api_style(value: object, *, context: str) -> ApiStyle:
    if value not in _API_PATHS:
        raise ValueError(f"{context} is unsupported")
    assert isinstance(value, str)
    return cast(ApiStyle, value)


def _output_mode(value: object, *, context: str) -> OutputMode:
    if value not in _OUTPUT_MODES:
        raise ValueError(f"{context} is unsupported")
    assert isinstance(value, str)
    return cast(OutputMode, value)


def _positive_float(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be a finite positive number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{context} must be a finite positive number")
    return normalized


def _positive_integer(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be a positive integer")
    if value < 1:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _boolean(value: object, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be boolean")
    return value


def _endpoint_path(value: object, *, context: str) -> str:
    path = text(value, context=context)
    parts = urlsplit(path)
    if (
        parts.scheme
        or parts.netloc
        or parts.query
        or parts.fragment
        or not path.startswith("/")
        or path.startswith("//")
        or ".." in path
        or path.endswith("/")
    ):
        raise ValueError(f"{context} must be one exact absolute URL path")
    return path


def _base_url(value: object, *, context: str) -> str:
    base_url = text(value, context=context)
    parts = urlsplit(base_url)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or base_url.endswith("/")
    ):
        raise ValueError(f"{context} must be one canonical HTTPS base URL")
    return base_url


@dataclass(frozen=True)
class ModelProfile:
    """One pinned model identifier and its provider API dialect."""

    model_id: str
    upstream_family: str
    profile_version: str
    api_style: ApiStyle
    endpoint_path: str
    maximum_output_tokens: int
    allowed_served_model_ids: tuple[str, ...]
    output_mode: OutputMode
    json_object: bool
    json_schema_strict: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", identifier(self.model_id, context="model_id"))
        object.__setattr__(
            self,
            "upstream_family",
            identifier(self.upstream_family, context="upstream_family"),
        )
        object.__setattr__(
            self,
            "profile_version",
            version(self.profile_version, context="model profile_version"),
        )
        api_style = _api_style(self.api_style, context="model api_style")
        endpoint_path = _endpoint_path(self.endpoint_path, context="model endpoint_path")
        if endpoint_path != _API_PATHS[api_style]:
            raise ValueError("model endpoint_path differs from its API style")
        maximum_output_tokens = _positive_integer(
            self.maximum_output_tokens,
            context="model maximum_output_tokens",
        )
        if maximum_output_tokens > 4096:
            raise ValueError("model maximum_output_tokens must not exceed 4096")
        allowed_served_model_ids = tuple(
            identifier(item, context=f"allowed_served_model_ids[{index}]")
            for index, item in enumerate(self.allowed_served_model_ids)
        )
        if not allowed_served_model_ids or len(allowed_served_model_ids) != len(
            set(allowed_served_model_ids)
        ):
            raise ValueError("allowed_served_model_ids must be non-empty and unique")
        output_mode = _output_mode(self.output_mode, context="model output_mode")
        json_object = _boolean(self.json_object, context="model json_object")
        json_schema_strict = _boolean(
            self.json_schema_strict,
            context="model json_schema_strict",
        )
        if output_mode == "json_object" and not json_object:
            raise ValueError("json_object output mode requires the json_object capability")
        if output_mode == "json_schema_strict" and not json_schema_strict:
            raise ValueError(
                "json_schema_strict output mode requires the json_schema_strict capability"
            )
        if api_style == "anthropic_messages" and output_mode != "prompt_json":
            raise ValueError("Anthropic Messages currently supports prompt_json mode only")
        object.__setattr__(self, "api_style", api_style)
        object.__setattr__(self, "endpoint_path", endpoint_path)
        object.__setattr__(self, "maximum_output_tokens", maximum_output_tokens)
        object.__setattr__(self, "allowed_served_model_ids", allowed_served_model_ids)
        object.__setattr__(self, "output_mode", output_mode)
        object.__setattr__(self, "json_object", json_object)
        object.__setattr__(self, "json_schema_strict", json_schema_strict)

    @classmethod
    def from_mapping(cls, value: object) -> ModelProfile:
        raw = as_mapping(value, context="model profile")
        strict_keys(
            raw,
            required={
                "model_id",
                "upstream_family",
                "profile_version",
                "api_style",
                "endpoint_path",
                "maximum_output_tokens",
                "allowed_served_model_ids",
                "output_mode",
                "json_object",
                "json_schema_strict",
            },
            context="model profile",
        )
        return cls(
            model_id=identifier(raw["model_id"], context="model_id"),
            upstream_family=identifier(raw["upstream_family"], context="upstream_family"),
            profile_version=version(raw["profile_version"], context="model profile_version"),
            api_style=_api_style(raw["api_style"], context="model api_style"),
            endpoint_path=_endpoint_path(raw["endpoint_path"], context="model endpoint_path"),
            maximum_output_tokens=_positive_integer(
                raw["maximum_output_tokens"], context="model maximum_output_tokens"
            ),
            allowed_served_model_ids=tuple(
                identifier(item, context=f"allowed_served_model_ids[{index}]")
                for index, item in enumerate(
                    as_sequence(
                        raw["allowed_served_model_ids"],
                        context="allowed_served_model_ids",
                    )
                )
            ),
            output_mode=_output_mode(raw["output_mode"], context="model output_mode"),
            json_object=_boolean(raw["json_object"], context="model json_object"),
            json_schema_strict=_boolean(
                raw["json_schema_strict"], context="model json_schema_strict"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "upstream_family": self.upstream_family,
            "profile_version": self.profile_version,
            "api_style": self.api_style,
            "endpoint_path": self.endpoint_path,
            "maximum_output_tokens": self.maximum_output_tokens,
            "allowed_served_model_ids": list(self.allowed_served_model_ids),
            "output_mode": self.output_mode,
            "json_object": self.json_object,
            "json_schema_strict": self.json_schema_strict,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class ProviderProfile:
    """One provider allow-list; credentials are referenced only by environment name."""

    provider_id: str
    profile_version: str
    endpoint_id: str
    base_url: str
    credential_env: str
    allowed_paths: tuple[str, ...]
    connect_timeout_seconds: float
    read_timeout_seconds: float
    round_timeout_seconds: float
    maximum_physical_attempts: int
    maximum_concurrency: int
    maximum_retry_after_seconds: float
    maximum_raw_response_bytes: int
    models: tuple[ModelProfile, ...]

    def __post_init__(self) -> None:
        provider_id = identifier(self.provider_id, context="provider_id")
        profile_version = version(self.profile_version, context="provider profile_version")
        endpoint_id = identifier(self.endpoint_id, context="endpoint_id")
        base_url = _base_url(self.base_url, context="provider base_url")
        if not isinstance(self.credential_env, str) or not _ENV_NAME.fullmatch(self.credential_env):
            raise ValueError("credential_env must be an uppercase environment variable name")
        allowed_paths = tuple(
            _endpoint_path(item, context=f"allowed_paths[{index}]")
            for index, item in enumerate(self.allowed_paths)
        )
        if not allowed_paths or len(allowed_paths) != len(set(allowed_paths)):
            raise ValueError("allowed_paths must be non-empty and unique")
        models = tuple(self.models)
        if not models or any(not isinstance(item, ModelProfile) for item in models):
            raise TypeError("models must contain ModelProfile values")
        model_ids = tuple(item.model_id for item in models)
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model_id values must be unique within a provider")
        chat_profiles = tuple(item for item in models if item.api_style == "openai_chat")
        for index, left in enumerate(chat_profiles):
            for right in chat_profiles[index + 1 :]:
                if set(left.allowed_served_model_ids) & set(right.allowed_served_model_ids):
                    raise ValueError(
                        "OpenAI Chat model profiles must have disjoint served-model allow-lists"
                    )
        if any(item.endpoint_path not in allowed_paths for item in models):
            raise ValueError("each model endpoint_path must be in the provider allow-list")
        connect_timeout_seconds = _positive_float(
            self.connect_timeout_seconds,
            context="provider connect_timeout_seconds",
        )
        read_timeout_seconds = _positive_float(
            self.read_timeout_seconds,
            context="provider read_timeout_seconds",
        )
        round_timeout_seconds = _positive_float(
            self.round_timeout_seconds,
            context="provider round_timeout_seconds",
        )
        if round_timeout_seconds < connect_timeout_seconds + read_timeout_seconds:
            raise ValueError("round_timeout_seconds cannot fit one physical attempt")
        maximum_physical_attempts = _positive_integer(
            self.maximum_physical_attempts,
            context="provider maximum_physical_attempts",
        )
        if maximum_physical_attempts > 2:
            raise ValueError("maximum_physical_attempts must not exceed the D1 safety limit of 2")
        maximum_concurrency = _positive_integer(
            self.maximum_concurrency,
            context="provider maximum_concurrency",
        )
        maximum_retry_after_seconds = _positive_float(
            self.maximum_retry_after_seconds,
            context="provider maximum_retry_after_seconds",
        )
        maximum_raw_response_bytes = _positive_integer(
            self.maximum_raw_response_bytes,
            context="provider maximum_raw_response_bytes",
        )
        if maximum_raw_response_bytes > 128 * 1024:
            raise ValueError("maximum_raw_response_bytes must not exceed 128 KiB")
        if provider_id == DMX_PROVIDER_ID:
            expected_transport_policy = (
                endpoint_id == DMX_ENDPOINT_ID
                and connect_timeout_seconds == _DMX_CONNECT_TIMEOUT_SECONDS
                and read_timeout_seconds == _DMX_READ_TIMEOUT_SECONDS
                and round_timeout_seconds == _DMX_ROUND_TIMEOUT_SECONDS
                and maximum_physical_attempts == _DMX_MAXIMUM_PHYSICAL_ATTEMPTS
                and maximum_concurrency == _DMX_MAXIMUM_CONCURRENCY
                and maximum_retry_after_seconds == _DMX_MAXIMUM_RETRY_AFTER_SECONDS
                and maximum_raw_response_bytes == _DMX_MAXIMUM_RAW_RESPONSE_BYTES
            )
            if not expected_transport_policy:
                raise ValueError("dmx-cn endpoint and transport policy are fixed")
            if base_url != DMX_BASE_URL:
                raise ValueError("dmx-cn base_url is fixed")
            if self.credential_env != DMX_CREDENTIAL_ENV:
                raise ValueError("dmx-cn credential_env is fixed")
            if set(allowed_paths) != _DMX_ALLOWED_PATHS:
                raise ValueError("dmx-cn allowed_paths must be the four fixed exact paths")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "endpoint_id", endpoint_id)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "allowed_paths", allowed_paths)
        object.__setattr__(self, "connect_timeout_seconds", connect_timeout_seconds)
        object.__setattr__(self, "read_timeout_seconds", read_timeout_seconds)
        object.__setattr__(self, "round_timeout_seconds", round_timeout_seconds)
        object.__setattr__(self, "maximum_physical_attempts", maximum_physical_attempts)
        object.__setattr__(self, "maximum_concurrency", maximum_concurrency)
        object.__setattr__(
            self,
            "maximum_retry_after_seconds",
            maximum_retry_after_seconds,
        )
        object.__setattr__(self, "maximum_raw_response_bytes", maximum_raw_response_bytes)
        object.__setattr__(self, "models", models)

    @classmethod
    def from_mapping(cls, value: object) -> ProviderProfile:
        raw = as_mapping(value, context="provider profile")
        strict_keys(
            raw,
            required={
                "provider_id",
                "profile_version",
                "endpoint_id",
                "base_url",
                "credential_env",
                "allowed_paths",
                "connect_timeout_seconds",
                "read_timeout_seconds",
                "round_timeout_seconds",
                "maximum_physical_attempts",
                "maximum_concurrency",
                "maximum_retry_after_seconds",
                "maximum_raw_response_bytes",
                "models",
            },
            context="provider profile",
        )
        return cls(
            provider_id=identifier(raw["provider_id"], context="provider_id"),
            profile_version=version(raw["profile_version"], context="provider profile_version"),
            endpoint_id=identifier(raw["endpoint_id"], context="endpoint_id"),
            base_url=_base_url(raw["base_url"], context="provider base_url"),
            credential_env=text(raw["credential_env"], context="credential_env"),
            allowed_paths=tuple(
                _endpoint_path(item, context=f"allowed_paths[{index}]")
                for index, item in enumerate(
                    as_sequence(raw["allowed_paths"], context="allowed_paths")
                )
            ),
            connect_timeout_seconds=_positive_float(
                raw["connect_timeout_seconds"],
                context="provider connect_timeout_seconds",
            ),
            read_timeout_seconds=_positive_float(
                raw["read_timeout_seconds"],
                context="provider read_timeout_seconds",
            ),
            round_timeout_seconds=_positive_float(
                raw["round_timeout_seconds"],
                context="provider round_timeout_seconds",
            ),
            maximum_physical_attempts=_positive_integer(
                raw["maximum_physical_attempts"],
                context="provider maximum_physical_attempts",
            ),
            maximum_concurrency=_positive_integer(
                raw["maximum_concurrency"],
                context="provider maximum_concurrency",
            ),
            maximum_retry_after_seconds=_positive_float(
                raw["maximum_retry_after_seconds"],
                context="provider maximum_retry_after_seconds",
            ),
            maximum_raw_response_bytes=_positive_integer(
                raw["maximum_raw_response_bytes"],
                context="provider maximum_raw_response_bytes",
            ),
            models=tuple(
                ModelProfile.from_mapping(item)
                for item in as_sequence(raw["models"], context="models")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "profile_version": self.profile_version,
            "endpoint_id": self.endpoint_id,
            "base_url": self.base_url,
            "credential_env": self.credential_env,
            "allowed_paths": list(self.allowed_paths),
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "read_timeout_seconds": self.read_timeout_seconds,
            "round_timeout_seconds": self.round_timeout_seconds,
            "maximum_physical_attempts": self.maximum_physical_attempts,
            "maximum_concurrency": self.maximum_concurrency,
            "maximum_retry_after_seconds": self.maximum_retry_after_seconds,
            "maximum_raw_response_bytes": self.maximum_raw_response_bytes,
            "models": [item.as_dict() for item in self.models],
        }

    def model(self, model_id: str) -> ModelProfile:
        normalized = identifier(model_id, context="model_id")
        for item in self.models:
            if item.model_id == normalized:
                return item
        raise KeyError(f"provider {self.provider_id!r} has no configured model {normalized!r}")

    def endpoint(self, model_id: str) -> str:
        model = self.model(model_id)
        if model.endpoint_path not in self.allowed_paths:  # pragma: no cover - constructor guard
            raise ValueError("model endpoint is outside the provider allow-list")
        return f"{self.base_url}{model.endpoint_path}"

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class ProviderCatalog:
    """Strict top-level provider configuration loaded from the package bundle."""

    schema_id: str
    schema_version: str
    catalog_id: str
    providers: tuple[ProviderProfile, ...]

    def __post_init__(self) -> None:
        if self.schema_id != PROVIDER_CATALOG_SCHEMA_ID:
            raise ValueError("schema_id differs from the provider catalog contract")
        if self.schema_version != PROVIDER_CATALOG_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the provider catalog contract")
        object.__setattr__(self, "catalog_id", identifier(self.catalog_id, context="catalog_id"))
        providers = tuple(self.providers)
        if not providers or any(not isinstance(item, ProviderProfile) for item in providers):
            raise TypeError("providers must contain ProviderProfile values")
        provider_ids = tuple(item.provider_id for item in providers)
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider_id values must be unique")
        object.__setattr__(self, "providers", providers)

    @classmethod
    def from_mapping(cls, value: object) -> ProviderCatalog:
        raw = as_mapping(value, context="provider catalog")
        strict_keys(
            raw,
            required={"schema_id", "schema_version", "catalog_id", "providers"},
            context="provider catalog",
        )
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=version(raw["schema_version"], context="schema_version"),
            catalog_id=identifier(raw["catalog_id"], context="catalog_id"),
            providers=tuple(
                ProviderProfile.from_mapping(item)
                for item in as_sequence(raw["providers"], context="providers")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "catalog_id": self.catalog_id,
            "providers": [item.as_dict() for item in self.providers],
        }

    def provider(self, provider_id: str) -> ProviderProfile:
        normalized = identifier(provider_id, context="provider_id")
        for item in self.providers:
            if item.provider_id == normalized:
                return item
        raise KeyError(f"provider catalog has no configured provider {normalized!r}")

    def model(self, provider_id: str, model_id: str) -> ModelProfile:
        return self.provider(provider_id).model(model_id)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class ProviderModelInfo:
    """Strict, non-authoritative row returned by a provider's model discovery endpoint."""

    id: str
    owned_by: str | None
    metadata: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", identifier(self.id, context="provider model id"))
        if self.owned_by is not None:
            object.__setattr__(self, "owned_by", text(self.owned_by, context="owned_by"))
        object.__setattr__(
            self,
            "metadata",
            freeze_json_mapping(self.metadata, context="provider model metadata"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> ProviderModelInfo:
        raw = as_mapping(value, context="provider model info")
        strict_keys(
            raw,
            required={"id", "owned_by", "metadata"},
            context="provider model info",
        )
        owned_by = raw["owned_by"]
        if owned_by is not None and not isinstance(owned_by, str):
            raise TypeError("owned_by must be text or null")
        return cls(
            id=identifier(raw["id"], context="provider model id"),
            owned_by=owned_by,
            metadata=freeze_json_mapping(raw["metadata"], context="provider model metadata"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "owned_by": self.owned_by,
            "metadata": thaw_json(self.metadata),
        }
