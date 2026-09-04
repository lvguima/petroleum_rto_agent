"""Small DMXAPI Chat Completions client with an in-memory conversation."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Final, Literal, Protocol
from urllib.parse import quote

from .chat_settings import DmxChatSettings, DmxChatSettingsError, load_dmx_chat_settings

MAX_CHAT_HISTORY_MESSAGES: Final[int] = 64
MAX_CHAT_HISTORY_BYTES: Final[int] = 128 * 1024
MAX_CHAT_REQUEST_BYTES: Final[int] = 256 * 1024
MAX_CHAT_RESPONSE_BYTES: Final[int] = 128 * 1024

DmxChatErrorCode = Literal[
    "dmx-chat-failed",
    "local-configuration-unavailable",
    "local-dependency-unavailable",
    "invalid-request",
    "authentication-failed",
    "permission-denied",
    "rate-limited",
    "provider-server-error",
    "http-error",
    "transport-connect",
    "invalid-response",
]


class DmxChatError(RuntimeError):
    """Safe categorized failure without credentials or provider response bodies."""

    def __init__(
        self,
        message: str,
        *,
        code: DmxChatErrorCode = "dmx-chat-failed",
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class DmxChatHttpResponse:
    """Minimal buffered response used by the injectable HTTP boundary."""

    status_code: int
    payload: object


class DmxChatHttpClient(Protocol):
    """The only HTTP operation required by the chat client."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> DmxChatHttpResponse: ...


class _HttpxChatClient:
    """Lazy httpx implementation; importing this module remains dependency-free."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> DmxChatHttpResponse:
        try:
            httpx = import_module("httpx")
        except ImportError:
            raise DmxChatError(
                "httpx is required for DMXAPI chat",
                code="local-dependency-unavailable",
            ) from None

        response_started = False
        received_status: int | None = None
        try:
            client_type = httpx.Client
            with (
                client_type(
                    timeout=timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    verify=True,
                ) as client,
                client.stream(
                    "POST",
                    url,
                    headers=dict(headers),
                    json=dict(payload),
                ) as response,
            ):
                response_started = True
                status_code = getattr(response, "status_code", None)
                if (
                    isinstance(status_code, bool)
                    or not isinstance(status_code, int)
                    or not 100 <= status_code <= 599
                ):
                    raise DmxChatError(
                        "DMXAPI chat response is invalid",
                        code="invalid-response",
                    )
                received_status = status_code
                if status_code != 200:
                    return DmxChatHttpResponse(status_code=status_code, payload=None)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if not isinstance(chunk, bytes):
                        raise DmxChatError(
                            "DMXAPI chat response body is invalid",
                            code="invalid-response",
                            http_status=200,
                        )
                    body.extend(chunk)
                    if len(body) > MAX_CHAT_RESPONSE_BYTES:
                        raise DmxChatError(
                            "DMXAPI chat response exceeds the byte limit",
                            code="invalid-response",
                            http_status=200,
                        )
                if not body:
                    raise DmxChatError(
                        "DMXAPI chat response body is invalid",
                        code="invalid-response",
                        http_status=200,
                    )
                try:
                    response_payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise DmxChatError(
                        "DMXAPI chat response body is invalid",
                        code="invalid-response",
                        http_status=200,
                    ) from None
        except DmxChatError:
            raise
        except Exception as exc:  # noqa: BLE001 - never expose provider exception details
            request_error_type = getattr(httpx, "RequestError", None)
            is_transport_error = isinstance(request_error_type, type) and isinstance(
                exc, request_error_type
            )
            if is_transport_error or not response_started:
                raise DmxChatError(
                    "DMXAPI chat request failed",
                    code="transport-connect",
                    retryable=True,
                ) from None
            raise DmxChatError(
                "DMXAPI chat response body is invalid",
                code="invalid-response",
                http_status=received_status,
            ) from None
        return DmxChatHttpResponse(status_code=status_code, payload=response_payload)


class DmxChatClient:
    """Send plain OpenAI-style messages to DMXAPI Chat Completions."""

    def __init__(
        self,
        settings: DmxChatSettings,
        *,
        http_client: DmxChatHttpClient | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        if not isinstance(settings, DmxChatSettings):
            raise TypeError("settings must be DmxChatSettings")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._settings = settings
        self._http_client = _HttpxChatClient() if http_client is None else http_client
        self._timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_local_config(cls) -> DmxChatClient:
        """Create the production client from the protected local credential file."""

        try:
            settings = load_dmx_chat_settings()
        except DmxChatSettingsError:
            raise DmxChatError(
                "DMXAPI local configuration is unavailable",
                code="local-configuration-unavailable",
            ) from None
        return cls(settings)

    @property
    def settings(self) -> DmxChatSettings:
        return self._settings

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        """Return ``choices[0].message.content`` for one complete message history."""

        normalized = _normalize_messages(messages)
        request_payload: dict[str, object] = {
            "model": self._settings.model,
            "messages": normalized,
        }
        if _contains_credential(request_payload, self._settings.api_key):
            raise DmxChatError(
                "DMXAPI chat request contains credential material",
                code="invalid-request",
            )
        if _json_size(request_payload) > MAX_CHAT_REQUEST_BYTES:
            raise DmxChatError(
                "DMXAPI chat request exceeds the byte limit",
                code="invalid-request",
            )
        headers = {
            "Authorization": self._settings.api_key,
            "Content-Type": "application/json",
        }
        try:
            response = self._http_client.post(
                self._settings.url,
                headers=headers,
                payload=request_payload,
                timeout_seconds=self._timeout_seconds,
            )
        except DmxChatError:
            raise
        except Exception:  # noqa: BLE001 - injected clients are also an untrusted boundary
            raise DmxChatError(
                "DMXAPI chat request failed",
                code="transport-connect",
                retryable=True,
            ) from None
        if not isinstance(response, DmxChatHttpResponse):
            raise DmxChatError(
                "DMXAPI chat response is invalid",
                code="invalid-response",
            )
        status_code = response.status_code
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise DmxChatError(
                "DMXAPI chat response is invalid",
                code="invalid-response",
            )
        if status_code != 200:
            raise _http_status_error(status_code)
        if (
            _json_size(
                response.payload,
                error_code="invalid-response",
                http_status=200,
            )
            > MAX_CHAT_RESPONSE_BYTES
        ):
            raise DmxChatError(
                "DMXAPI chat response exceeds the byte limit",
                code="invalid-response",
                http_status=200,
            )
        if _contains_credential(response.payload, self._settings.api_key):
            raise DmxChatError(
                "DMXAPI chat response contained credential material",
                code="invalid-response",
                http_status=200,
            )
        return _response_content(response.payload)


class DmxChatSession:
    """One process-local multi-turn conversation with no disk persistence."""

    def __init__(self, client: DmxChatClient) -> None:
        if not isinstance(client, DmxChatClient):
            raise TypeError("client must be DmxChatClient")
        self._client = client
        self._messages: list[dict[str, str]] = []
        self.clear()

    @property
    def messages(self) -> tuple[Mapping[str, str], ...]:
        """Return detached copies of the current in-memory history."""

        return tuple(dict(message) for message in self._messages)

    def ask(self, text: str) -> str:
        """Append one successful user/assistant turn and return the assistant reply."""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("chat text must be non-empty")
        pending = [*self._messages, {"role": "user", "content": text}]
        reply = self._client.complete(pending)
        committed = _normalize_messages([*pending, {"role": "assistant", "content": reply}])
        self._messages = committed
        return reply

    def clear(self) -> None:
        """Clear prior turns while retaining the optional configured system prompt."""

        prompt = self._client.settings.system_prompt
        self._messages = [] if prompt is None else [{"role": "system", "content": prompt}]


def _normalize_messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence) or not messages:
        raise ValueError("messages must be a non-empty sequence")
    normalized: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ValueError("each message must contain only role and content")
        role = message["role"]
        content = message["content"]
        if role not in {"system", "user", "assistant"}:
            raise ValueError("message role is unsupported")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("message content must be non-empty text")
        normalized.append({"role": role, "content": content})
    if len(normalized) > MAX_CHAT_HISTORY_MESSAGES:
        raise ValueError("chat history exceeds the message limit; use /clear")
    if _json_size(normalized) > MAX_CHAT_HISTORY_BYTES:
        raise ValueError("chat history exceeds the byte limit; use /clear")
    return normalized


def _json_size(
    value: object,
    *,
    error_code: DmxChatErrorCode = "invalid-request",
    http_status: int | None = None,
) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise DmxChatError(
            "DMXAPI chat payload is not finite UTF-8 JSON",
            code=error_code,
            http_status=http_status,
        ) from None
    return len(encoded)


def _http_status_error(status_code: int) -> DmxChatError:
    code: DmxChatErrorCode
    retryable = False
    if status_code == 401:
        code = "authentication-failed"
    elif status_code == 403:
        code = "permission-denied"
    elif status_code == 429:
        code = "rate-limited"
        retryable = True
    elif 500 <= status_code <= 599:
        code = "provider-server-error"
        retryable = True
    else:
        code = "http-error"
    return DmxChatError(
        f"DMXAPI chat returned HTTP {status_code}",
        code=code,
        retryable=retryable,
        http_status=status_code,
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


def _response_content(payload: object) -> str:
    try:
        if not isinstance(payload, Mapping):
            raise TypeError
        choices = payload["choices"]
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence) or not choices:
            raise TypeError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise TypeError
        message = choice["message"]
        if not isinstance(message, Mapping):
            raise TypeError
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise TypeError
    except (KeyError, IndexError, TypeError):
        raise DmxChatError(
            "DMXAPI chat response has no assistant content",
            code="invalid-response",
            http_status=200,
        ) from None
    return content


__all__ = [
    "MAX_CHAT_HISTORY_BYTES",
    "MAX_CHAT_HISTORY_MESSAGES",
    "MAX_CHAT_REQUEST_BYTES",
    "MAX_CHAT_RESPONSE_BYTES",
    "DmxChatClient",
    "DmxChatError",
    "DmxChatErrorCode",
    "DmxChatHttpClient",
    "DmxChatHttpResponse",
    "DmxChatSession",
]
