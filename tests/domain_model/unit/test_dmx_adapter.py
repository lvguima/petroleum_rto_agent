from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Self

import pytest

from petroleum_rto.domain_model import DMX_CREDENTIAL_ENV, load_provider_catalog
from petroleum_rto.domain_model.adapters import (
    DMXAPI_BASE_URL,
    DmxApiAdapter,
    DmxApiError,
    HttpRequest,
    HttpResponse,
    HttpTransportFailure,
    HttpxTransport,
)
from petroleum_rto.domain_model.evidence import EvidenceStore
from petroleum_rto.domain_model.prompt import PromptCompiler
from petroleum_rto.domain_model.runtime import DomainIntentRuntime
from petroleum_rto.rto.capabilities import load_capability_bundle
from petroleum_rto.rto.communication import (
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DomainModelInvocationResult,
    DomainModelPort,
    DomainModelRequest,
    IntentCommunicationService,
)


class FakeClock:
    def __init__(self, *, wall_time: float = 1_800_000_000.0) -> None:
        self.now_ns = 0
        self.wall_time = wall_time
        self.sleeps: list[float] = []

    def monotonic_ns(self) -> int:
        return self.now_ns

    def time(self) -> float:
        return self.wall_time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)
        self.wall_time += seconds


TransportAction = HttpResponse | HttpTransportFailure | Callable[[HttpRequest], HttpResponse]


@pytest.fixture(autouse=True)
def _set_test_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DMX_CREDENTIAL_ENV, "test-only-key")


class FakeTransport:
    def __init__(self, *actions: TransportAction) -> None:
        self.actions = list(actions)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.actions:
            raise AssertionError("fake transport has no response")
        action = self.actions.pop(0)
        if isinstance(action, HttpTransportFailure):
            raise action
        if callable(action):
            return action(request)
        return action


def _request(
    repo_root: Path, *, user_text: str = "降低单位进料炉燃料热负荷。"
) -> DomainModelRequest:
    service = IntentCommunicationService.from_bundle(load_capability_bundle(repo_root))
    return service.start(
        session_id="session-dmx-adapter",
        message_id="user-1",
        user_text=user_text,
    )


def _domain_response(request: DomainModelRequest) -> dict[str, object]:
    return {
        "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
        "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
        "response_id": "response-dmx-1",
        "request_ref": request.ref.as_dict(),
        "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
        "outcome": "intent",
        "intent": {
            "schema_id": "optimization-intent",
            "schema_version": "1.0.0",
            "intent_id": "intent-dmx-1",
            "objectives": [
                {
                    "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                    "sense": "minimize",
                    "priority": 1,
                }
            ],
            "decision_variables": ["furnace_temperature_target_k"],
            "constraints": [],
            "preference": {
                "method": "single-objective",
                "objective_order": ["specific_furnace_fuel_energy_mj_per_t"],
            },
            "result_request": {
                "output_kind": "steady-setpoint-vector",
                "include_alternatives": False,
                "max_candidates": 1,
            },
            "ambiguities": [],
        },
    }


def _completion_response(
    api_style: str,
    model_id: str,
    request: DomainModelRequest,
    *,
    served_model: str | None = None,
    finish_reason: str | None = None,
    output_text: str | None = None,
    refusal: str | None = None,
) -> HttpResponse:
    output = output_text or json.dumps(
        _domain_response(request), ensure_ascii=False, separators=(",", ":")
    )
    model = served_model or model_id
    if api_style == "openai_chat":
        payload: dict[str, Any] = {
            "id": "body-request-chat",
            "model": model,
            "choices": [
                {
                    "message": {"role": "assistant", "content": output, "refusal": refusal},
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    elif api_style == "openai_responses":
        payload = {
            "id": "body-request-responses",
            "model": model,
            "status": finish_reason or "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": output}],
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
    else:
        payload = {
            "id": "body-request-messages",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": output}],
            "stop_reason": finish_reason or "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    return HttpResponse(
        status_code=200,
        headers={"X-Request-ID": f"header-request-{api_style}"},
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
    )


def _adapter(
    repo_root: Path,
    api_style: str,
    transport: FakeTransport,
    *,
    clock: FakeClock | None = None,
) -> tuple[DmxApiAdapter, str]:
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == api_style)
    effective_clock = clock or FakeClock()
    adapter = DmxApiAdapter(
        provider_profile=provider,
        model_profile=model,
        transport=transport,
        clock_ns=effective_clock.monotonic_ns,
        wall_clock=effective_clock.time,
        sleeper=effective_clock.sleep,
        invocation_id_factory=lambda: "invocation-dmx-test",
    )
    port: DomainModelPort = adapter
    assert port.provider_id == "dmx-cn"
    return adapter, model.model_id


@pytest.mark.parametrize(
    ("api_style", "path", "finish_reason"),
    [
        ("openai_chat", "/chat/completions", "stop"),
        ("openai_responses", "/responses", "completed"),
        ("anthropic_messages", "/messages", "end_turn"),
    ],
)
def test_three_non_streaming_shapes_normalize_success_evidence(
    repo_root: Path,
    api_style: str,
    path: str,
    finish_reason: str,
) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == api_style)
    transport = FakeTransport(_completion_response(api_style, model.model_id, request))
    adapter, model_id = _adapter(repo_root, api_style, transport)

    result = adapter.invoke(request)

    assert result.status == "succeeded"
    assert result.error is None
    assert isinstance(result.response, str)
    assert json.loads(result.response) == _domain_response(request)
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.status == "succeeded"
    assert attempt.provider_request_id == f"header-request-{api_style}"
    assert attempt.served_model == model_id
    assert attempt.finish_reason == finish_reason
    assert attempt.usage is not None
    assert attempt.usage.as_dict() == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.url == f"{DMXAPI_BASE_URL}{path}"
    assert sent.headers["authorization"] == "test-only-key"
    assert sent.connect_timeout_seconds == provider.connect_timeout_seconds == 5.0
    assert sent.read_timeout_seconds == provider.read_timeout_seconds == 45.0
    assert sent.max_response_bytes == provider.maximum_raw_response_bytes == 128 * 1024
    assert sent.body is not None
    wire = json.loads(sent.body)
    assert wire["model"] == model_id
    assert wire["stream"] is False
    assert "url" not in wire
    assert wire.get("max_tokens", wire.get("max_output_tokens")) == 4096
    if api_style == "openai_chat":
        assert "response_format" not in wire
    if api_style == "openai_responses":
        assert "text" not in wire
    if api_style == "anthropic_messages":
        assert sent.headers["anthropic-version"] == "2023-06-01"


def test_public_constructor_has_no_untrusted_dependency_or_hidden_mode_bypass() -> None:
    parameters = inspect.signature(DmxApiAdapter).parameters

    assert "environ" not in parameters
    assert "_validation_authority" not in parameters
    assert "prompt_compiler" not in parameters
    assert "egress_guard" not in parameters


@pytest.mark.parametrize("api_style", ["openai_chat", "openai_responses"])
@pytest.mark.parametrize("output_mode", ["json_object", "json_schema_strict"])
def test_verified_structured_output_mode_is_sent_explicitly(
    repo_root: Path,
    api_style: str,
    output_mode: str,
) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    original = next(item for item in provider.models if item.api_style == api_style)
    configured = replace(
        original,
        output_mode=output_mode,
        json_object=output_mode == "json_object",
        json_schema_strict=output_mode == "json_schema_strict",
    )
    configured_provider = replace(
        provider,
        models=tuple(
            configured if item.model_id == configured.model_id else item for item in provider.models
        ),
    )
    transport = FakeTransport(_completion_response(api_style, configured.model_id, request))
    adapter = DmxApiAdapter(
        provider_profile=configured_provider,
        model_profile=configured,
        transport=transport,
        invocation_id_factory=lambda: "invocation-structured-output",
    )

    result = adapter.invoke(request)

    assert result.status == "succeeded"
    body = transport.requests[0].body
    assert body is not None
    wire = json.loads(body)
    response_format = (
        wire["response_format"] if api_style == "openai_chat" else wire["text"]["format"]
    )
    expected_wire_type = "json_schema" if output_mode == "json_schema_strict" else output_mode
    assert response_format["type"] == expected_wire_type
    if output_mode == "json_schema_strict":
        strict_format = (
            response_format["json_schema"] if api_style == "openai_chat" else response_format
        )
        assert strict_format["strict"] is True


def test_concurrency_limit_fails_fast_without_queueing_or_second_transport(
    repo_root: Path,
) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = provider.model("deepseek-v4-flash-0731")
    nested: list[DomainModelInvocationResult] = []
    adapter: DmxApiAdapter

    def invoke_while_slot_is_held(_http_request: HttpRequest) -> HttpResponse:
        nested.append(adapter.invoke(request))
        return _completion_response("openai_chat", model.model_id, request)

    transport = FakeTransport(invoke_while_slot_is_held)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    outer = adapter.invoke(request)

    assert outer.status == "succeeded"
    assert len(transport.requests) == 1
    assert len(nested) == 1
    error = nested[0].error
    assert error is not None
    assert error.code == "local-concurrency-limit"


def test_concurrency_limit_is_shared_across_adapter_instances(repo_root: Path) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = provider.model("deepseek-v4-flash-0731")
    inner_transport = FakeTransport(_completion_response("openai_chat", model.model_id, request))
    inner, _ = _adapter(repo_root, "openai_chat", inner_transport)
    nested: list[DomainModelInvocationResult] = []

    def invoke_other_adapter(_http_request: HttpRequest) -> HttpResponse:
        nested.append(inner.invoke(request))
        return _completion_response("openai_chat", model.model_id, request)

    outer_transport = FakeTransport(invoke_other_adapter)
    outer, _ = _adapter(repo_root, "openai_chat", outer_transport)

    assert outer.invoke(request).status == "succeeded"
    assert nested[0].error is not None
    assert nested[0].error.code == "local-concurrency-limit"
    assert inner_transport.requests == []


def test_models_discovery_uses_only_fixed_get_endpoint(repo_root: Path) -> None:
    response = HttpResponse(
        status_code=200,
        headers={},
        body=json.dumps(
            {
                "object": "list",
                "data": [
                    {"id": "model-a", "object": "model", "owned_by": "dmx"},
                    {"id": "model-b", "object": "model", "created": 123},
                ],
            }
        ).encode(),
    )
    transport = FakeTransport(response)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    models = adapter.list_models()

    assert tuple(item.id for item in models) == ("model-a", "model-b")
    assert models[0].owned_by == "dmx"
    assert models[1].metadata["created"] == 123
    sent = transport.requests[0]
    assert sent.method == "GET"
    assert sent.url == f"{DMXAPI_BASE_URL}/models"
    assert sent.body is None


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (401, "authentication"),
        (402, "payment"),
        (403, "permission"),
        (404, "not_found"),
        (422, "invalid_request"),
    ],
)
def test_terminal_http_errors_are_never_retried(
    repo_root: Path,
    status_code: int,
    category: str,
) -> None:
    transport = FakeTransport(HttpResponse(status_code, {"x-request-id": "failure-id"}, b"{}"))
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.category == category
    assert result.error.http_status == status_code
    assert result.response is None
    assert len(transport.requests) == len(result.attempts) == 1
    assert result.attempts[0].provider_request_id == "failure-id"


@pytest.mark.parametrize("kind", ["read-timeout", "write-timeout", "transport"])
def test_ambiguous_transport_failures_are_never_retried(repo_root: Path, kind: str) -> None:
    failure = HttpTransportFailure(kind, "unsafe raw detail", retryable_before_send=False)  # type: ignore[arg-type]
    transport = FakeTransport(failure)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.category == ("timeout" if "timeout" in kind else "transport")
    assert result.error.retryable is False
    assert len(transport.requests) == 1
    assert "unsafe raw detail" not in result.error.message


def test_pre_send_connect_failure_gets_exactly_one_retry(repo_root: Path) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == "openai_chat")
    transport = FakeTransport(
        HttpTransportFailure("connect", "dns detail", retryable_before_send=True),
        _completion_response("openai_chat", model.model_id, request),
    )
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(request)

    assert result.status == "succeeded"
    assert tuple(item.status for item in result.attempts) == ("failed", "succeeded")
    assert result.attempts[0].error is not None
    assert result.attempts[0].error.retryable is True
    assert len(transport.requests) == 2


@pytest.mark.parametrize(("status_code", "retry_after"), [(429, "2"), (503, "2")])
def test_retryable_http_status_respects_retry_after_seconds(
    repo_root: Path,
    status_code: int,
    retry_after: str,
) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == "openai_chat")
    clock = FakeClock()
    transport = FakeTransport(
        HttpResponse(status_code, {"Retry-After": retry_after}, b"{}"),
        _completion_response("openai_chat", model.model_id, request),
    )
    adapter, _ = _adapter(repo_root, "openai_chat", transport, clock=clock)

    result = adapter.invoke(request)

    assert result.status == "succeeded"
    assert clock.sleeps == [2.0]
    assert result.attempts[0].duration_ms == 2_000
    assert len(transport.requests) == 2


def test_retry_after_http_date_and_safety_cap_apply_to_models(repo_root: Path) -> None:
    clock = FakeClock()
    future = datetime.fromtimestamp(clock.wall_time + 999, tz=UTC)
    transport = FakeTransport(
        HttpResponse(503, {"retry-after": format_datetime(future, usegmt=True)}, b"{}"),
        HttpResponse(200, {}, b'{"data":[{"id":"model-a"}]}'),
    )
    adapter, _ = _adapter(repo_root, "openai_chat", transport, clock=clock)

    models = adapter.list_models()

    assert tuple(item.id for item in models) == ("model-a",)
    assert clock.sleeps == [20.0]
    assert len(transport.requests) == 2


def test_balance_error_inside_5xx_is_not_retried(repo_root: Path) -> None:
    response = HttpResponse(
        503,
        {"retry-after": "0"},
        b'{"error":{"code":"insufficient_balance"}}',
    )
    transport = FakeTransport(response, response)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root))

    assert result.error is not None
    assert result.error.code == "provider-balance-or-quota-insufficient"
    assert result.error.retryable is False
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"code":"invalid_model"}}',
        b'{"error":{"code":"model_not_found"}}',
        b'{"error":{"code":"configuration_error"}}',
        b'{"error":{"message":"configuration error: no route available"}}',
        b'{"error":{"message":"upstream configuration error: no route available"}}',
    ],
)
def test_explicit_configuration_error_inside_5xx_is_not_retried(
    repo_root: Path,
    body: bytes,
) -> None:
    response = HttpResponse(
        503,
        {"retry-after": "0"},
        body,
    )
    transport = FakeTransport(response, response)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root))

    assert result.error is not None
    assert result.error.code == "provider-configuration-error"
    assert result.error.retryable is False
    assert len(transport.requests) == 1


def test_ordinary_route_failure_inside_5xx_remains_retryable(repo_root: Path) -> None:
    response = HttpResponse(
        503,
        {"retry-after": "0"},
        b'{"error":{"message":"upstream route temporarily unavailable"}}',
    )
    transport = FakeTransport(response, response)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root))

    assert result.error is not None
    assert result.error.code == "http-5xx-provider-server"
    assert result.error.retryable is True
    assert len(transport.requests) == 2


def test_total_deadline_prevents_a_second_physical_attempt(repo_root: Path) -> None:
    clock = FakeClock()

    def slow_failure(_: HttpRequest) -> HttpResponse:
        clock.advance(71.0)
        return HttpResponse(503, {"retry-after": "0"}, b"{}")

    transport = FakeTransport(slow_failure)
    adapter, _ = _adapter(repo_root, "openai_chat", transport, clock=clock)

    result = adapter.invoke(_request(repo_root))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.category == "provider_server"
    assert result.error.retryable is True
    assert len(transport.requests) == len(result.attempts) == 1
    assert clock.sleeps == []


def test_caller_remaining_round_budget_bounds_adapter_retries(repo_root: Path) -> None:
    clock = FakeClock()

    def slow_failure(_: HttpRequest) -> HttpResponse:
        clock.advance(11.0)
        return HttpResponse(503, {"retry-after": "0"}, b"{}")

    transport = FakeTransport(slow_failure)
    adapter, _ = _adapter(repo_root, "openai_chat", transport, clock=clock)

    result = adapter.invoke_with_timeout(_request(repo_root), timeout_seconds=60.0)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.category == "provider_server"
    assert len(transport.requests) == 1


def test_response_returned_after_caller_deadline_is_not_accepted(repo_root: Path) -> None:
    clock = FakeClock()
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == "openai_chat")

    def late_success(_: HttpRequest) -> HttpResponse:
        clock.advance(70.0)
        return _completion_response("openai_chat", model.model_id, request)

    adapter, _ = _adapter(
        repo_root,
        "openai_chat",
        FakeTransport(late_success),
        clock=clock,
    )
    result = adapter.invoke_with_timeout(request, timeout_seconds=60.0)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "semantic-call-deadline-exceeded"
    assert result.attempts[-1].duration_ms == 70_000


@pytest.mark.parametrize(
    ("status_code", "category"),
    [(429, "rate_limit"), (503, "provider_server")],
)
def test_retryable_failure_stops_after_one_transport_retry(
    repo_root: Path,
    status_code: int,
    category: str,
) -> None:
    transport = FakeTransport(
        HttpResponse(status_code, {}, b"{}"),
        HttpResponse(status_code, {}, b"{}"),
    )
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.category == category
    assert result.error.retryable is True
    assert len(transport.requests) == len(result.attempts) == 2


@pytest.mark.parametrize(
    ("response_factory", "category"),
    [
        (
            lambda model, request: _completion_response(
                "openai_chat", model, request, refusal="cannot comply"
            ),
            "refusal",
        ),
        (
            lambda model, request: _completion_response(
                "openai_chat", model, request, finish_reason="length"
            ),
            "truncated",
        ),
        (lambda _model, _request: HttpResponse(200, {}, b"not-json"), "protocol"),
        (
            lambda model, request: _completion_response(
                "openai_chat", model, request, served_model="different-model"
            ),
            "model_mismatch",
        ),
    ],
)
def test_model_failures_never_become_business_status_or_transport_retry(
    repo_root: Path,
    response_factory: Callable[[str, DomainModelRequest], HttpResponse],
    category: str,
) -> None:
    request = _request(repo_root)
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == "openai_chat")
    transport = FakeTransport(response_factory(model.model_id, request))
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(request)

    assert result.status == "failed"
    assert result.response is None
    assert result.error is not None
    assert result.error.category == category
    assert len(transport.requests) == 1
    assert result.attempts[0].finish_reason is None
    if category in {"refusal", "truncated", "model_mismatch"}:
        assert result.attempts[0].provider_request_id == "header-request-openai_chat"
        assert result.attempts[0].usage is not None


@pytest.mark.parametrize("invalid_kind", ["markdown", "missing-fields", "stale-reference"])
def test_semantic_protocol_errors_reach_communication_repair_instead_of_provider_failure(
    repo_root: Path,
    invalid_kind: str,
) -> None:
    service = IntentCommunicationService.from_bundle(load_capability_bundle(repo_root))
    request = service.start(
        session_id="session-semantic-repair",
        message_id="user-1",
        user_text="降低能耗。",
    )
    if invalid_kind == "markdown":
        output = "```json\n{}\n```"
    elif invalid_kind == "missing-fields":
        output = "{}"
    else:
        payload = _domain_response(request)
        request_ref = payload["request_ref"]
        assert isinstance(request_ref, dict)
        request_ref["object_id"] = "stale-request"
        output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == "openai_chat")
    transport = FakeTransport(
        _completion_response(
            "openai_chat",
            model.model_id,
            request,
            output_text=output,
        )
    )
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    invocation = adapter.invoke(request)

    assert invocation.status == "succeeded"
    assert invocation.error is None
    assert invocation.response == output
    communication = service.evaluate_response(request, invocation.response)
    assert communication.status == "repair_required"
    assert communication.repair is not None
    assert communication.repair.next_model_attempt == 2


def test_raw_response_limit_and_redirect_are_protocol_failures(repo_root: Path) -> None:
    maximum = load_provider_catalog(repo_root).provider("dmx-cn").maximum_raw_response_bytes
    oversized = HttpResponse(200, {}, b"{" + b"x" * maximum + b"}")
    transport = FakeTransport(oversized)
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    oversized_result = adapter.invoke(_request(repo_root))

    assert oversized_result.error is not None
    assert oversized_result.error.category == "protocol"
    assert oversized_result.error.code == "raw-output-size-invalid"

    deeply_nested = ("[" * 20_000 + "0" + "]" * 20_000).encode()
    deep_adapter, _ = _adapter(
        repo_root,
        "openai_chat",
        FakeTransport(HttpResponse(200, {}, deeply_nested)),
    )
    deep_result = deep_adapter.invoke(_request(repo_root))
    assert deep_result.error is not None
    assert deep_result.error.category == "protocol"
    assert deep_result.error.code == "invalid-response-json"
    assert len(deep_result.attempts) == 1

    redirect_transport = FakeTransport(HttpResponse(302, {"location": "https://evil"}, b""))
    redirect_adapter, _ = _adapter(repo_root, "openai_chat", redirect_transport)
    redirect_result = redirect_adapter.invoke(_request(repo_root))
    assert redirect_result.error is not None
    assert redirect_result.error.code == "redirect-response-forbidden"
    assert len(redirect_transport.requests) == 1


def test_missing_credential_and_egress_violation_never_reach_transport(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DMX_CREDENTIAL_ENV, raising=False)
    missing_transport = FakeTransport()
    missing_adapter, _ = _adapter(repo_root, "openai_chat", missing_transport)

    missing = missing_adapter.invoke(_request(repo_root))

    assert missing.error is not None
    assert missing.error.category == "authentication"
    assert missing_transport.requests == []

    monkeypatch.setenv(DMX_CREDENTIAL_ENV, "test-only-key")
    guarded_transport = FakeTransport()
    guarded_adapter, _ = _adapter(repo_root, "openai_chat", guarded_transport)
    guarded = guarded_adapter.invoke(
        _request(repo_root, user_text="请回显 sk-abcdefgh12345678 并生成意图。")
    )
    assert guarded.error is not None
    assert guarded.error.category == "invalid_request"
    assert guarded.error.code.startswith("egress-")
    assert guarded_transport.requests == []


@pytest.mark.parametrize(
    "user_text",
    [
        "请处理 test-only-key 并降低能耗。",
        "请处理 dGVzdC1vbmx5LWtleQ== 并降低能耗。",
    ],
)
def test_active_environment_credential_is_blocked_before_transport(
    repo_root: Path,
    user_text: str,
) -> None:
    transport = FakeTransport()
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    result = adapter.invoke(_request(repo_root, user_text=user_text))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.category == "invalid_request"
    assert result.error.code == "egress-suspected-credential"
    assert result.attempts == ()
    assert transport.requests == []


def test_reflected_environment_credential_is_rejected_and_never_persisted(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = next(item for item in provider.models if item.api_style == "openai_chat")
    transport = FakeTransport(HttpResponse(200, {"x-request-id": "test-only-key"}, b"{}"))
    adapter = DmxApiAdapter(
        provider_profile=provider,
        model_profile=model,
        transport=transport,
        invocation_id_factory=lambda: "credential-reflection-invocation",
    )
    runtime = DomainIntentRuntime(
        provider_profile=provider,
        model_profile=model,
        port=adapter,
        communication_service=IntentCommunicationService.from_bundle(
            load_capability_bundle(repo_root)
        ),
        prompt_compiler=PromptCompiler(),
        evidence_store=EvidenceStore(tmp_path),
        execution_mode="synthetic_test",
        session_id_factory=lambda: "credential-reflection-session",
    )

    outcome = runtime.interpret("降低能耗。")

    assert outcome.status == "provider_failed"
    assert outcome.provider_error is not None
    assert outcome.provider_error.code == "provider-credential-reflection"
    assert outcome.evidence_manifest is not None
    record = EvidenceStore(tmp_path).read_snapshot(outcome.evidence_manifest)
    persisted = b"".join(path.read_bytes() for path in record.run_dir.iterdir())
    assert b"test-only-key" not in persisted

    escaped_body = (
        '{"id":"test\\u002donly\\u002dkey","model":"' + model.model_id + '","choices":[]}'
    ).encode()
    escaped_adapter = DmxApiAdapter(
        provider_profile=provider,
        model_profile=model,
        transport=FakeTransport(HttpResponse(200, {}, escaped_body)),
        invocation_id_factory=lambda: "escaped-credential-reflection",
    )
    escaped = escaped_adapter.invoke(_request(repo_root))
    assert escaped.error is not None
    assert escaped.error.code == "provider-credential-reflection"
    assert escaped.attempts[-1].provider_request_id is None
    assert "test-only-key" not in json.dumps(escaped.as_dict())

    inner = json.dumps(
        _domain_response(_request(repo_root)), ensure_ascii=False, separators=(",", ":")
    ).replace("response-dmx-1", "test\\u002donly\\u002dkey")
    nested_adapter, _ = _adapter(
        repo_root,
        "openai_chat",
        FakeTransport(
            _completion_response(
                "openai_chat",
                model.model_id,
                _request(repo_root),
                output_text=inner,
            )
        ),
    )
    nested = nested_adapter.invoke(_request(repo_root))
    assert nested.error is not None
    assert nested.error.code == "provider-credential-reflection"
    assert nested.attempts[-1].provider_request_id is None
    assert "test-only-key" not in json.dumps(nested.as_dict())


def test_model_discovery_rejects_decoded_credential_in_metadata(repo_root: Path) -> None:
    body = b'{"data":[{"id":"model-a","metadata":"test\\u002donly\\u002dkey"}]}'
    adapter, _ = _adapter(
        repo_root,
        "openai_chat",
        FakeTransport(HttpResponse(200, {}, body)),
    )

    with pytest.raises(DmxApiError) as caught:
        adapter.list_models()

    assert caught.value.error.code == "provider-credential-reflection"


def test_model_discovery_rejects_excessive_metadata_depth(repo_root: Path) -> None:
    nested: object = "leaf"
    for _ in range(100):
        nested = {"nested": nested}
    body = json.dumps({"data": [{"id": "model-a", "metadata": nested}]}).encode()
    adapter, _ = _adapter(
        repo_root,
        "openai_chat",
        FakeTransport(HttpResponse(200, {}, body)),
    )

    with pytest.raises(DmxApiError) as caught:
        adapter.list_models()

    assert caught.value.error.code == "invalid-model-discovery-response"


def test_model_discovery_errors_expose_provider_error(repo_root: Path) -> None:
    transport = FakeTransport(HttpResponse(404, {}, b"{}"))
    adapter, _ = _adapter(repo_root, "openai_chat", transport)

    with pytest.raises(DmxApiError) as caught:
        adapter.list_models()

    assert caught.value.error.category == "not_found"
    assert caught.value.error.retryable is False
    assert len(transport.requests) == 1


def test_httpx_transport_disables_redirects_proxy_environment_and_insecure_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"X-Request-ID": "req-1"}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> tuple[bytes, ...]:
            return (b"{}",)

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

        @staticmethod
        def Timeout(**kwargs: object) -> object:
            captured["timeout_args"] = kwargs
            return object()

    from petroleum_rto.domain_model.adapters import transport as transport_module

    monkeypatch.setattr(transport_module, "import_module", lambda _name: FakeHttpx)
    response = HttpxTransport().send(
        HttpRequest(
            method="GET",
            url=f"{DMXAPI_BASE_URL}/models",
            headers={},
            body=None,
            connect_timeout_seconds=5.0,
            read_timeout_seconds=45.0,
            total_timeout_seconds=50.0,
            max_response_bytes=128 * 1024,
        )
    )

    assert response.status_code == 200
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False
    assert captured["verify"] is True
    assert captured["timeout_args"] == {
        "connect": 5.0,
        "read": 45.0,
        "write": 45.0,
        "pool": 5.0,
    }


def test_httpx_transport_enforces_cumulative_response_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0

    def clock() -> float:
        return elapsed

    class SlowResponse:
        status_code = 200

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> Any:
            nonlocal elapsed
            elapsed = 0.6
            yield b"{"
            elapsed = 1.1
            yield b"}"

    class SlowClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: object, **_kwargs: object) -> SlowResponse:
            return SlowResponse()

    class FakeHttpx:
        Client = SlowClient

        @staticmethod
        def Timeout(**_kwargs: object) -> object:
            return object()

    from petroleum_rto.domain_model.adapters import transport as transport_module

    monkeypatch.setattr(transport_module, "import_module", lambda _name: FakeHttpx)
    with pytest.raises(HttpTransportFailure) as caught:
        HttpxTransport(clock=clock).send(
            HttpRequest(
                method="GET",
                url=f"{DMXAPI_BASE_URL}/models",
                headers={},
                body=None,
                connect_timeout_seconds=0.5,
                read_timeout_seconds=1.0,
                total_timeout_seconds=1.0,
                max_response_bytes=1024,
            )
        )

    assert caught.value.kind == "read-timeout"
    assert caught.value.retryable_before_send is False
