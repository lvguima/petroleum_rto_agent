from __future__ import annotations

from collections.abc import Mapping

import pytest

import petroleum_rto.domain_model.chat as chat_module
from petroleum_rto.domain_model.chat import (
    DmxChatClient,
    DmxChatError,
    DmxChatHttpResponse,
    DmxChatSession,
)
from petroleum_rto.domain_model.chat_settings import (
    DMX_CHAT_MODEL,
    DMX_CHAT_URL,
    DMX_SYSTEM_PROMPT,
    DmxChatSettings,
    DmxChatSettingsError,
    load_dmx_chat_settings,
)


class FakeHttpClient:
    def __init__(self, responses: list[DmxChatHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> DmxChatHttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)


def _response(text: str, *, status_code: int = 200) -> DmxChatHttpResponse:
    return DmxChatHttpResponse(
        status_code=status_code,
        payload={"choices": [{"message": {"content": text}}]},
    )


def test_complete_uses_exact_minimal_payload_and_raw_authorization_key() -> None:
    key = "sk-test-direct-authorization"
    transport = FakeHttpClient([_response("你好")])
    client = DmxChatClient(DmxChatSettings(api_key=key), http_client=transport)

    assert client.complete([{"role": "user", "content": "你好"}]) == "你好"
    assert transport.calls == [
        {
            "url": DMX_CHAT_URL,
            "headers": {
                "Authorization": key,
                "Content-Type": "application/json",
            },
            "payload": {
                "model": DMX_CHAT_MODEL,
                "messages": [{"role": "user", "content": "你好"}],
            },
            "timeout_seconds": 45.0,
        }
    ]
    sent_headers = transport.calls[0]["headers"]
    assert isinstance(sent_headers, dict)
    authorization = sent_headers["Authorization"]
    assert isinstance(authorization, str)
    assert not authorization.startswith("Bearer ")


def test_session_keeps_successful_turns_and_clear_retains_system_prompt() -> None:
    transport = FakeHttpClient([_response("第一答"), _response("第二答")])
    settings = DmxChatSettings(
        api_key="sk-test-session-key",
        system_prompt="你是炼化助手。",
    )
    session = DmxChatSession(DmxChatClient(settings, http_client=transport))

    assert session.ask("第一问") == "第一答"
    assert session.ask("第二问") == "第二答"
    assert transport.calls[1]["payload"] == {
        "model": DMX_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "你是炼化助手。"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ],
    }

    session.clear()

    assert session.messages == ({"role": "system", "content": "你是炼化助手。"},)


def test_failed_turn_is_not_committed_and_errors_do_not_reflect_key() -> None:
    key = "sk-secret-must-not-leak"
    transport = FakeHttpClient([_response("unused", status_code=401)])
    session = DmxChatSession(DmxChatClient(DmxChatSettings(api_key=key), http_client=transport))

    with pytest.raises(DmxChatError) as captured:
        session.ask("hello")

    assert key not in str(captured.value)
    assert session.messages == ({"role": "system", "content": DMX_SYSTEM_PROMPT},)
    assert key not in repr(session._client.settings)


def test_invalid_provider_shape_returns_one_safe_error() -> None:
    client = DmxChatClient(
        DmxChatSettings(api_key="sk-test-invalid-shape"),
        http_client=FakeHttpClient([DmxChatHttpResponse(200, {"choices": []})]),
    )

    with pytest.raises(DmxChatError, match="no assistant content"):
        client.complete([{"role": "user", "content": "hello"}])


def test_settings_loader_uses_injected_key_loader_without_exposing_failure() -> None:
    settings = load_dmx_chat_settings(key_loader=lambda: "sk-loaded-locally")

    assert settings.model == DMX_CHAT_MODEL
    assert settings.url == DMX_CHAT_URL

    with pytest.raises(DmxChatSettingsError) as captured:
        load_dmx_chat_settings(key_loader=lambda: None)
    assert "sk-" not in str(captured.value)


def test_from_local_config_normalizes_settings_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_load() -> DmxChatSettings:
        raise DmxChatSettingsError("safe settings failure")

    monkeypatch.setattr(chat_module, "load_dmx_chat_settings", fail_to_load)

    with pytest.raises(DmxChatError, match="local configuration is unavailable"):
        DmxChatClient.from_local_config()
