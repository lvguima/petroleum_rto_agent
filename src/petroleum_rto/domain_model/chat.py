"""Small DMXAPI Chat Completions client with an in-memory conversation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol

from .chat_settings import DmxChatSettings, DmxChatSettingsError, load_dmx_chat_settings


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
            with client_type(
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                verify=True,
            ) as client:
                response = client.post(
                    url,
                    headers=dict(headers),
                    json=dict(payload),
                )
                status_code = getattr(response, "status_code", None)
                json_method = getattr(response, "json", None)
                if not isinstance(status_code, int) or not callable(json_method):
                    raise TypeError("invalid HTTP response")
                response_payload: object = json_method()
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
        self._messages = [*pending, {"role": "assistant", "content": reply}]
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
    return normalized


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
    "DmxChatClient",
    "DmxChatError",
    "DmxChatHttpClient",
    "DmxChatHttpResponse",
    "DmxChatSession",
]
