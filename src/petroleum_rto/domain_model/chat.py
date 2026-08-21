"""Small DMXAPI Chat Completions client with an in-memory conversation."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Final, Protocol
from urllib.parse import quote

from .chat_settings import DmxChatSettings, DmxChatSettingsError, load_dmx_chat_settings

MAX_CHAT_HISTORY_MESSAGES: Final[int] = 64
MAX_CHAT_HISTORY_BYTES: Final[int] = 128 * 1024
MAX_CHAT_REQUEST_BYTES: Final[int] = 256 * 1024
MAX_CHAT_RESPONSE_BYTES: Final[int] = 128 * 1024


class DmxChatError(RuntimeError):
    """Safe chat failure whose message never includes credentials or response bodies."""


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
                status_code = getattr(response, "status_code", None)
                if not isinstance(status_code, int):
                    raise TypeError("invalid HTTP response")
                if status_code != 200:
                    return DmxChatHttpResponse(status_code=status_code, payload=None)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if not isinstance(chunk, bytes):
                        raise TypeError("invalid HTTP response body")
                    body.extend(chunk)
                    if len(body) > MAX_CHAT_RESPONSE_BYTES:
                        raise DmxChatError("DMXAPI chat response exceeds the byte limit")
                if not body:
                    raise TypeError("empty HTTP response body")
                response_payload = json.loads(body)
        except ImportError:
            raise DmxChatError("httpx is required for DMXAPI chat") from None
        except DmxChatError:
            raise
        except Exception:  # noqa: BLE001 - never expose headers, key, or response body
            raise DmxChatError("DMXAPI chat request failed") from None
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
            raise DmxChatError("DMXAPI local configuration is unavailable") from None
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
            raise DmxChatError("DMXAPI chat request contains credential material")
        if _json_size(request_payload) > MAX_CHAT_REQUEST_BYTES:
            raise DmxChatError("DMXAPI chat request exceeds the byte limit")
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
            raise DmxChatError("DMXAPI chat request failed") from None
        if response.status_code != 200:
            raise DmxChatError(f"DMXAPI chat returned HTTP {response.status_code}")
        if _json_size(response.payload) > MAX_CHAT_RESPONSE_BYTES:
            raise DmxChatError("DMXAPI chat response exceeds the byte limit")
        if _contains_credential(response.payload, self._settings.api_key):
            raise DmxChatError("DMXAPI chat response contained credential material")
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


def _json_size(value: object) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise DmxChatError("DMXAPI chat payload is not finite UTF-8 JSON") from None
    return len(encoded)


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
        raise DmxChatError("DMXAPI chat response has no assistant content") from None
    return content


__all__ = [
    "MAX_CHAT_HISTORY_BYTES",
    "MAX_CHAT_HISTORY_MESSAGES",
    "MAX_CHAT_REQUEST_BYTES",
    "MAX_CHAT_RESPONSE_BYTES",
    "DmxChatClient",
    "DmxChatError",
    "DmxChatHttpClient",
    "DmxChatHttpResponse",
    "DmxChatSession",
]
