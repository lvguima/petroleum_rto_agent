from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from petroleum_rto.assistant.dmx_intent_adapter import (
    ASSISTANT_TURN_SCHEMA_ID,
    ASSISTANT_TURN_SCHEMA_VERSION,
    AssistantTurnMode,
    DmxIntentAdapter,
    decode_assistant_turn_decision,
)
from petroleum_rto.domain_model.chat import (
    DmxChatClient,
    DmxChatHttpClient,
    DmxChatHttpResponse,
)
from petroleum_rto.domain_model.chat_settings import DmxChatSettings
from petroleum_rto.rto.communication import (
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DomainModelRequest,
    OptimizationIntent,
)
from petroleum_rto.rto.runtime import build_intent_communication_service


def _intent() -> dict[str, object]:
    return {
        "schema_id": "optimization-intent",
        "schema_version": "1.0.0",
        "intent_id": "adapter-energy-intent",
        "objectives": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "sense": "minimize",
                "priority": 1,
            }
        ],
        "decision_variables": [
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        ],
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
    }


def _optimization_response(request: DomainModelRequest) -> dict[str, object]:
    return {
        "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
        "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
        "response_id": "adapter-response-1",
        "request_ref": request.ref.as_dict(),
        "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
        "outcome": "intent",
        "intent": _intent(),
    }


def _turn_response(
    request: DomainModelRequest,
    *,
    routes: list[str],
    optimization_response: object | None = None,
    mode: AssistantTurnMode = "routing",
) -> dict[str, object]:
    response: dict[str, object] = {
        "schema_id": ASSISTANT_TURN_SCHEMA_ID,
        "schema_version": ASSISTANT_TURN_SCHEMA_VERSION,
        "request_ref": request.ref.as_dict(),
        "routes": routes,
    }
    if mode != "confirmation":
        response["capability_manifest_ref"] = request.capability_manifest_ref.as_dict()
        response["optimization_response"] = optimization_response
    return response


class _RecordingHttpClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.payloads: list[Mapping[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> DmxChatHttpResponse:
        self.payloads.append(payload)
        return DmxChatHttpResponse(
            status_code=200,
            payload={"choices": [{"message": {"content": self.content}}]},
        )


class _FailingHttpClient:
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> DmxChatHttpResponse:
        raise RuntimeError("sk-secret-must-not-appear")


class _StatusHttpClient:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> DmxChatHttpResponse:
        return DmxChatHttpResponse(status_code=self.status_code, payload=None)


def _client(http_client: DmxChatHttpClient) -> DmxChatClient:
    return DmxChatClient(
        DmxChatSettings(
            api_key="sk-test-only-not-real",
            model="test-intent-model",
            system_prompt=None,
        ),
        http_client=http_client,
    )


def _request(repo_root: Path, text: str = "帮我找个更省燃料的操作点") -> DomainModelRequest:
    return build_intent_communication_service(repo_root=repo_root).start(
        session_id="adapter-session",
        message_id="user-1",
        user_text=text,
    )


def test_adapter_uses_one_closed_response_for_routing_and_intent(repo_root: Path) -> None:
    assert ASSISTANT_TURN_SCHEMA_VERSION == "3.0.0"
    request = _request(repo_root)
    raw = _turn_response(
        request,
        routes=["optimization"],
        optimization_response=_optimization_response(request),
    )
    http = _RecordingHttpClient(json.dumps(raw, ensure_ascii=False))
    adapter = DmxIntentAdapter(_client(http))

    invocation = adapter.invoke(request)
    decision = decode_assistant_turn_decision(request, invocation.response or "", mode="routing")

    assert invocation.status == "succeeded"
    assert decision.routes == ("optimization",)
    assert decision.optimization_response is not None
    service = build_intent_communication_service(repo_root=repo_root)
    assert service.evaluate_response(request, decision.optimization_response).status == "resolved"
    assert len(http.payloads) == 1
    messages = http.payloads[0]["messages"]
    assert isinstance(messages, list)
    assert [item["role"] for item in messages] == ["system", "user"]
    system_prompt = messages[0]["content"]
    assert "【任务】" in system_prompt
    assert "【路由】" in system_prompt
    assert "【理解优化需求】" in system_prompt
    assert "【严格意图】" in system_prompt
    assert "不输出工具名或路径" in system_prompt
    assert "last-result" in system_prompt
    assert "口语、简称、近义表达及明显错写" in system_prompt
    assert "生成一条优化策略" in system_prompt
    assert "未给出任何具体目标的泛化请求选capabilities" in system_prompt
    assert "不构造空目标Intent" in system_prompt
    assert "按提及顺序" in system_prompt
    assert "用户所选项为priority=1" in system_prompt
    assert "我想提高超产率" not in system_prompt
    prompt = json.loads(messages[1]["content"])
    assert prompt["mode"] == "routing"
    assert prompt["turn_contract"]["routes"]["json_type"] == "array_of_strings"
    assert prompt["turn_contract"]["routes"]["unique_items"] is True
    assert set(prompt["turn_contract"]["routes"]["allowed_values"]) == {
        "chat",
        "capabilities",
        "operating-status",
        "assistant-status",
        "last-result",
        "optimization",
        "unsupported-action",
    }
    defaults = prompt["optimization_response_contract"]["intent_variant"]["intent_contract"][
        "interpretation_defaults"
    ]
    assert "mention order" in defaults["multiple_objectives_in_mention_order"]
    common_rules = prompt["optimization_response_contract"]["common_field_rules"]
    assert common_rules["request_ref"]["exact_value"] == request.ref.as_dict()
    assert common_rules["capability_manifest_ref"]["exact_value"] == (
        request.capability_manifest_ref.as_dict()
    )
    request_payload = prompt["request"]
    assert "operating_context" not in request_payload
    serialized = json.dumps(request_payload, ensure_ascii=False)
    for forbidden in (
        "current_setpoints",
        "feed_composition",
        "solver_id",
        "algorithm_id",
    ):
        assert forbidden not in serialized


def test_outer_repair_requests_one_full_replacement_without_echoing_prior_output(
    repo_root: Path,
) -> None:
    request = _request(repo_root)
    raw = _turn_response(request, routes=["chat"])
    http = _RecordingHttpClient(json.dumps(raw, ensure_ascii=False))
    adapter = DmxIntentAdapter(_client(http))

    invocation = adapter.invoke(request, repair_outer=True)

    assert invocation.status == "succeeded"
    messages = http.payloads[0]["messages"]
    assert isinstance(messages, list)
    prompt = json.loads(messages[1]["content"])
    assert prompt["outer_repair"] == {
        "reason": "previous response failed strict outer decoding",
        "required_action": "return-full-assistant-turn-decision",
    }
    assert "prior_response" not in prompt
    assert "raw_response" not in prompt


def test_chat_route_has_no_optimization_payload(repo_root: Path) -> None:
    request = _request(repo_root, "什么是RTO？")
    raw = _turn_response(request, routes=["chat"])
    decision = decode_assistant_turn_decision(
        request,
        json.dumps(raw, ensure_ascii=False),
        mode="routing",
    )

    assert decision.routes == ("chat",)
    assert decision.optimization_response is None


def test_routing_mode_accepts_multiple_read_only_semantic_routes(repo_root: Path) -> None:
    request = _request(repo_root, "刚才结果怎么样，这套系统还能优化什么？")
    raw = _turn_response(request, routes=["last-result", "capabilities"])

    decision = decode_assistant_turn_decision(
        request,
        json.dumps(raw, ensure_ascii=False),
        mode="routing",
    )

    assert decision.routes == ("last-result", "capabilities")


def test_routing_mode_accepts_maximum_six_compatible_routes(repo_root: Path) -> None:
    request = _request(repo_root)
    routes = [
        "chat",
        "capabilities",
        "operating-status",
        "assistant-status",
        "last-result",
        "unsupported-action",
    ]
    raw = _turn_response(request, routes=routes)

    decision = decode_assistant_turn_decision(
        request,
        json.dumps(raw, ensure_ascii=False),
        mode="routing",
    )

    assert decision.routes == tuple(routes)
    assert decision.optimization_response is None


@pytest.mark.parametrize(
    "routes",
    [
        [],
        ["chat", "chat"],
        ["show_capabilities"],
        ["optimization", "unsupported-action"],
    ],
)
def test_turn_decoder_rejects_empty_duplicate_unknown_or_unsafe_route_sets(
    repo_root: Path,
    routes: list[str],
) -> None:
    request = _request(repo_root)
    raw = _turn_response(request, routes=routes)

    with pytest.raises((TypeError, ValueError)):
        decode_assistant_turn_decision(
            request,
            json.dumps(raw, ensure_ascii=False),
            mode="routing",
        )


def test_confirmation_mode_sends_only_summary_and_small_classification_contract(
    repo_root: Path,
) -> None:
    request = _request(repo_root, "可以，开始算吧")
    pending_intent = OptimizationIntent.from_mapping(_intent())
    raw = _turn_response(request, routes=["confirm"], mode="confirmation")
    http = _RecordingHttpClient(json.dumps(raw, ensure_ascii=False))
    adapter = DmxIntentAdapter(_client(http))

    invocation = adapter.invoke(
        request,
        mode="confirmation",
        pending_intent=pending_intent,
        pending_summary="请确认是否以降低能耗为目标并调整炉温和塔压？",
    )
    decision = decode_assistant_turn_decision(
        request,
        invocation.response or "",
        mode="confirmation",
    )

    assert decision.routes == ("confirm",)
    messages = http.payloads[0]["messages"]
    assert isinstance(messages, list)
    prompt = json.loads(messages[1]["content"])
    assert set(prompt["turn_contract"]["routes"]["allowed_values"]) == {
        "confirm",
        "revise",
        "cancel",
        "question",
    }
    assert prompt["turn_contract"]["routes"]["exact_items"] == 1
    assert prompt["pending_summary"] == "请确认是否以降低能耗为目标并调整炉温和塔压？"
    assert prompt["user_message"] == {"message_id": "user-1", "text": "可以，开始算吧"}
    assert set(prompt) == {
        "mode",
        "outer_repair",
        "pending_summary",
        "turn_contract",
        "user_message",
    }
    serialized = json.dumps(prompt, ensure_ascii=False)
    for forbidden in (
        "optimization_response_contract",
        "capability_manifest",
        "prior_intent",
        "current_setpoints",
        "solver_id",
        "algorithm_id",
        "internal_path",
    ):
        assert forbidden not in serialized
    assert "含否定、犹豫或含糊表达不得判为confirm" in messages[0]["content"]


def test_confirmation_mode_rejects_multiple_routes(repo_root: Path) -> None:
    request = _request(repo_root, "确认，同时告诉我当前工况")
    raw = _turn_response(
        request,
        routes=["confirm", "question"],
        mode="confirmation",
    )

    with pytest.raises(ValueError, match="exactly one route"):
        decode_assistant_turn_decision(
            request,
            json.dumps(raw, ensure_ascii=False),
            mode="confirmation",
        )


def test_revision_intent_mode_receives_prior_intent_only_after_revision_classification(
    repo_root: Path,
) -> None:
    request = _request(repo_root, "不要调塔压，只调炉温")
    pending_intent = OptimizationIntent.from_mapping(_intent())
    raw = _turn_response(
        request,
        routes=["optimization"],
        optimization_response=_optimization_response(request),
        mode="intent",
    )
    http = _RecordingHttpClient(json.dumps(raw, ensure_ascii=False))
    adapter = DmxIntentAdapter(_client(http))

    invocation = adapter.invoke(
        request,
        mode="intent",
        pending_intent=pending_intent,
        pending_summary="本次目标为降低能耗，允许调整炉温和塔压。",
    )

    assert invocation.status == "succeeded"
    messages = http.payloads[0]["messages"]
    assert isinstance(messages, list)
    prompt = json.loads(messages[1]["content"])
    assert prompt["mode"] == "intent"
    assert prompt["revision_context"]["prior_intent"] == pending_intent.as_dict()
    assert prompt["revision_context"]["displayed_summary"].startswith("本次目标")
    assert "optimization_response_contract" in prompt


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: {**raw, "extra": True},
        lambda raw: {**raw, "routes": "chat"},
        lambda raw: {**raw, "routes": ["run-offline"]},
        lambda raw: {**raw, "request_ref": {"object_id": "wrong", "fingerprint": "0" * 64}},
        lambda raw: {
            **raw,
            "routes": ["chat"],
            "optimization_response": _intent(),
        },
    ],
)
def test_turn_decoder_rejects_unknown_fields_routes_refs_and_variant_overlap(
    repo_root: Path,
    mutate: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    request = _request(repo_root)
    raw = _turn_response(request, routes=["chat"])
    changed = mutate(raw)

    with pytest.raises((TypeError, ValueError)):
        decode_assistant_turn_decision(
            request,
            json.dumps(changed, ensure_ascii=False),
            mode="routing",
        )


def test_turn_decoder_rejects_model_generated_confirmation_text(
    repo_root: Path,
) -> None:
    request = _request(repo_root)
    raw = _turn_response(
        request,
        routes=["optimization"],
        optimization_response=_optimization_response(request),
    )
    raw["confirmation_text"] = "模型自由生成的确认文字"

    with pytest.raises(ValueError):
        decode_assistant_turn_decision(
            request,
            json.dumps(raw, ensure_ascii=False),
            mode="routing",
        )


def test_adapter_normalizes_provider_failure_without_echoing_details(repo_root: Path) -> None:
    request = _request(repo_root)
    adapter = DmxIntentAdapter(_client(_FailingHttpClient()))

    invocation = adapter.invoke(request)

    assert invocation.status == "failed"
    assert invocation.response is None
    assert invocation.error is not None
    assert invocation.error.category == "transport"
    assert invocation.error.code == "transport-connect"
    assert invocation.error.retryable is True
    serialized = json.dumps(invocation.as_dict(), ensure_ascii=False)
    assert "sk-secret-must-not-appear" not in serialized


def test_adapter_preserves_safe_rate_limit_classification(repo_root: Path) -> None:
    request = _request(repo_root)
    adapter = DmxIntentAdapter(_client(_StatusHttpClient(429)))

    invocation = adapter.invoke(request)

    assert invocation.status == "failed"
    assert invocation.error is not None
    assert invocation.error.category == "rate_limit"
    assert invocation.error.code == "rate-limited"
    assert invocation.error.retryable is True
    assert invocation.error.http_status == 429
