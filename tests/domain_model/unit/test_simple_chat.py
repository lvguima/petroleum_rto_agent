from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

import petroleum_rto.domain_model.chat as chat_module
from petroleum_rto.domain_model.chat import (
    MAX_CHAT_HISTORY_BYTES,
    MAX_CHAT_HISTORY_MESSAGES,
    MAX_CHAT_REQUEST_BYTES,
    MAX_CHAT_RESPONSE_BYTES,
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
from petroleum_rto.domain_model.credentials import LocalCredentialError, load_local_dmx_api_key


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


def test_default_system_prompt_does_not_repeat_operational_disclaimers() -> None:
    assert "未经现场验证" not in DMX_SYSTEM_PROMPT
    assert "现场控制权" not in DMX_SYSTEM_PROMPT
    assert "快照" not in DMX_SYSTEM_PROMPT
    assert "不补造" in DMX_SYSTEM_PROMPT
    assert "已经执行" in DMX_SYSTEM_PROMPT


def test_failed_turn_is_not_committed_and_errors_do_not_reflect_key() -> None:
    key = "sk-secret-must-not-leak"
    transport = FakeHttpClient([_response("unused", status_code=401)])
    session = DmxChatSession(DmxChatClient(DmxChatSettings(api_key=key), http_client=transport))

    with pytest.raises(DmxChatError) as captured:
        session.ask("hello")

    assert key not in str(captured.value)
    assert captured.value.code == "authentication-failed"
    assert captured.value.retryable is False
    assert captured.value.http_status == 401
    assert session.messages == ({"role": "system", "content": DMX_SYSTEM_PROMPT},)
    assert key not in repr(session._client.settings)


def test_invalid_provider_shape_returns_one_safe_error() -> None:
    client = DmxChatClient(
        DmxChatSettings(api_key="sk-test-invalid-shape"),
        http_client=FakeHttpClient([DmxChatHttpResponse(200, {"choices": []})]),
    )

    with pytest.raises(DmxChatError, match="no assistant content") as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert captured.value.code == "invalid-response"
    assert captured.value.retryable is False
    assert captured.value.http_status == 200


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_retryable"),
    [
        (401, "authentication-failed", False),
        (403, "permission-denied", False),
        (429, "rate-limited", True),
        (503, "provider-server-error", True),
        (418, "http-error", False),
    ],
)
def test_http_failures_have_stable_safe_classification(
    status_code: int,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    key = "sk-secret-http-classification"
    client = DmxChatClient(
        DmxChatSettings(api_key=key),
        http_client=FakeHttpClient([_response(key, status_code=status_code)]),
    )

    with pytest.raises(DmxChatError) as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert captured.value.code == expected_code
    assert captured.value.retryable is expected_retryable
    assert captured.value.http_status == status_code
    assert key not in str(captured.value)


def test_transport_failure_is_retryable_without_exposing_exception_text() -> None:
    class _FailingHttpClient:
        def post(
            self,
            _url: str,
            *,
            headers: Mapping[str, str],
            payload: Mapping[str, object],
            timeout_seconds: float,
        ) -> DmxChatHttpResponse:
            del headers, payload, timeout_seconds
            raise RuntimeError("provider body and sk-secret-transport")

    client = DmxChatClient(
        DmxChatSettings(api_key="sk-test-transport"),
        http_client=_FailingHttpClient(),
    )

    with pytest.raises(DmxChatError) as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert str(captured.value) == "DMXAPI chat request failed"
    assert captured.value.code == "transport-connect"
    assert captured.value.retryable is True
    assert captured.value.http_status is None


def test_request_history_and_response_budgets_fail_before_state_is_committed() -> None:
    transport = FakeHttpClient([_response("unused")])
    client = DmxChatClient(DmxChatSettings(api_key="sk-test-budget-key"), http_client=transport)

    with pytest.raises(ValueError, match="history exceeds the byte limit"):
        client.complete([{"role": "user", "content": "x" * MAX_CHAT_HISTORY_BYTES}])
    with pytest.raises(ValueError, match="history exceeds the message limit"):
        client.complete(
            [
                {"role": "user", "content": f"message-{index}"}
                for index in range(MAX_CHAT_HISTORY_MESSAGES + 1)
            ]
        )
    assert transport.calls == []

    oversized_model = "m" * MAX_CHAT_REQUEST_BYTES
    oversized_client = DmxChatClient(
        DmxChatSettings(api_key="sk-test-request-budget", model=oversized_model),
        http_client=transport,
    )
    with pytest.raises(DmxChatError, match="request exceeds the byte limit") as request_error:
        oversized_client.complete([{"role": "user", "content": "hello"}])
    assert request_error.value.code == "invalid-request"
    assert request_error.value.retryable is False
    assert transport.calls == []

    response_transport = FakeHttpClient([_response("x" * MAX_CHAT_RESPONSE_BYTES)])
    response_client = DmxChatClient(
        DmxChatSettings(api_key="sk-test-response-budget"),
        http_client=response_transport,
    )
    with pytest.raises(DmxChatError, match="response exceeds the byte limit") as response_error:
        response_client.complete([{"role": "user", "content": "hello"}])
    assert response_error.value.code == "invalid-response"
    assert response_error.value.retryable is False
    assert response_error.value.http_status == 200


def test_active_credential_is_blocked_in_requests_and_provider_responses() -> None:
    key = "sk-secret-active-credential"
    transport = FakeHttpClient([_response(key)])
    client = DmxChatClient(DmxChatSettings(api_key=key), http_client=transport)

    with pytest.raises(DmxChatError) as outbound:
        client.complete([{"role": "user", "content": f"repeat {key}"}])
    assert key not in str(outbound.value)
    assert transport.calls == []

    with pytest.raises(DmxChatError) as reflected:
        client.complete([{"role": "user", "content": "hello"}])
    assert key not in str(reflected.value)
    assert reflected.value.code == "invalid-response"
    assert reflected.value.retryable is False
    assert reflected.value.http_status == 200

    encoded_transport = FakeHttpClient(
        [_response(base64.b64encode(key.encode("ascii")).decode("ascii"))]
    )
    encoded_client = DmxChatClient(DmxChatSettings(api_key=key), http_client=encoded_transport)
    with pytest.raises(DmxChatError, match="credential material") as encoded:
        encoded_client.complete([{"role": "user", "content": "hello"}])
    assert encoded.value.code == "invalid-response"
    assert encoded.value.retryable is False
    assert encoded.value.http_status == 200


def test_http_200_non_json_payload_preserves_status_without_exposing_payload() -> None:
    secret = "provider-secret-payload"

    class _NonJsonValue:
        def __repr__(self) -> str:
            return secret

    client = DmxChatClient(
        DmxChatSettings(api_key="sk-test-non-json-payload"),
        http_client=FakeHttpClient(
            [DmxChatHttpResponse(status_code=200, payload={"opaque": _NonJsonValue()})]
        ),
    )

    with pytest.raises(DmxChatError, match="not finite UTF-8 JSON") as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert captured.value.code == "invalid-response"
    assert captured.value.retryable is False
    assert captured.value.http_status == 200
    assert secret not in str(captured.value)


def test_http_boundary_stops_reading_an_oversized_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StreamingResponse:
        status_code = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> tuple[bytes, ...]:
            return (b"x" * (MAX_CHAT_RESPONSE_BYTES + 1),)

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: object, **_kwargs: object) -> _StreamingResponse:
            return _StreamingResponse()

    monkeypatch.setattr(chat_module, "import_module", lambda _name: SimpleNamespace(Client=_Client))
    client = DmxChatClient(DmxChatSettings(api_key="sk-test-raw-response-budget"))

    with pytest.raises(DmxChatError, match="response exceeds the byte limit") as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert captured.value.code == "invalid-response"
    assert captured.value.retryable is False
    assert captured.value.http_status == 200


def test_http_200_malformed_raw_body_preserves_status_without_exposing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider-secret-not-json"

    class _StreamingResponse:
        status_code = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> tuple[bytes, ...]:
            return (secret.encode("ascii"),)

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: object, **_kwargs: object) -> _StreamingResponse:
            return _StreamingResponse()

    monkeypatch.setattr(chat_module, "import_module", lambda _name: SimpleNamespace(Client=_Client))
    client = DmxChatClient(DmxChatSettings(api_key="sk-test-malformed-raw-body"))

    with pytest.raises(DmxChatError, match="response body is invalid") as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert captured.value.code == "invalid-response"
    assert captured.value.retryable is False
    assert captured.value.http_status == 200
    assert secret not in str(captured.value)


def test_missing_http_dependency_has_non_retryable_local_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_import(_name: str) -> object:
        raise ImportError("local import detail")

    monkeypatch.setattr(chat_module, "import_module", fail_to_import)
    client = DmxChatClient(DmxChatSettings(api_key="sk-test-missing-httpx"))

    with pytest.raises(DmxChatError, match="httpx is required") as captured:
        client.complete([{"role": "user", "content": "hello"}])

    assert captured.value.code == "local-dependency-unavailable"
    assert captured.value.retryable is False
    assert captured.value.http_status is None


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

    with pytest.raises(DmxChatError, match="local configuration is unavailable") as captured:
        DmxChatClient.from_local_config()

    assert captured.value.code == "local-configuration-unavailable"
    assert captured.value.retryable is False
    assert captured.value.http_status is None


def test_chat_error_legacy_constructor_has_safe_default_metadata() -> None:
    error = DmxChatError("safe message")

    assert str(error) == "safe message"
    assert error.code == "dmx-chat-failed"
    assert error.retryable is False
    assert error.http_status is None


@pytest.mark.parametrize(
    "payload",
    [
        "sk-local-bare-key\n",
        '{"api_key":"sk-local-json-key"}\n',
    ],
)
def test_local_credential_file_accepts_only_protected_bare_or_strict_json(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "dmx_api.json"
    path.write_text(payload, encoding="ascii")
    path.chmod(0o600)

    assert load_local_dmx_api_key(path) in {"sk-local-bare-key", "sk-local-json-key"}

    path.write_text('{"api_key":"sk-one","api_key":"sk-two"}', encoding="ascii")
    with pytest.raises(LocalCredentialError, match="JSON contract"):
        load_local_dmx_api_key(path)

    path.write_text('{"api_key":"sk-local-key","extra":true}', encoding="ascii")
    with pytest.raises(LocalCredentialError, match="JSON contract"):
        load_local_dmx_api_key(path)


def test_local_credential_file_rejects_broad_permissions_and_symbolic_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dmx_api.json"
    path.write_text("sk-local-protected-key", encoding="ascii")
    path.chmod(0o644)
    with pytest.raises(LocalCredentialError, match="permissions"):
        load_local_dmx_api_key(path)

    path.chmod(0o600)
    link = tmp_path / "linked-key.json"
    link.symlink_to(path)
    with pytest.raises(LocalCredentialError, match="opened safely"):
        load_local_dmx_api_key(link)
