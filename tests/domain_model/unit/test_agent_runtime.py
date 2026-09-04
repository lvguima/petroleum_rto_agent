from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from petroleum_rto.assistant.dmx_intent_adapter import (
    ASSISTANT_TURN_SCHEMA_ID,
    ASSISTANT_TURN_SCHEMA_VERSION,
    AssistantTurnMode,
)
from petroleum_rto.assistant.runtime import (
    HELP,
    AgentRuntime,
    AssistantTurnModelPort,
)
from petroleum_rto.assistant.tools import AgentTools
from petroleum_rto.domain_model.chat import DmxChatError
from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DomainModelInvocationResult,
    DomainModelRequest,
    OptimizationIntent,
    ProviderAttempt,
    ProviderError,
)
from petroleum_rto.rto.runtime import build_intent_communication_service


class _FakeSession:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        clear_failure: Exception | None = None,
    ) -> None:
        self.messages: list[str] = []
        self.clear_calls = 0
        self.failure = failure
        self.clear_failure = clear_failure

    def ask(self, message: str) -> str:
        self.messages.append(message)
        if self.failure is not None:
            raise self.failure
        return f"回复{len(self.messages)}"

    def clear(self) -> None:
        self.clear_calls += 1
        if self.clear_failure is not None:
            raise self.clear_failure


class _FakeTools:
    def __init__(self, results: Mapping[str, Mapping[str, object]]) -> None:
        self.results = results
        self.calls: list[tuple[str, str | None]] = []
        self.intents: list[OptimizationIntent | None] = []
        self.failure: Exception | None = None

    def invoke(
        self,
        action: str,
        *,
        source: str | None = None,
        intent: OptimizationIntent | None = None,
    ) -> Mapping[str, object]:
        self.calls.append((action, source))
        self.intents.append(intent)
        if self.failure is not None:
            raise self.failure
        return self.results[action]


type _ResponseFactory = Callable[
    [DomainModelRequest, AssistantTurnMode, OptimizationIntent | None],
    object,
]


class _FakeTurnModel:
    def __init__(self, responses: list[object | _ResponseFactory]) -> None:
        self.responses = responses
        self.requests: list[DomainModelRequest] = []
        self.modes: list[AssistantTurnMode] = []
        self.pending_intents: list[OptimizationIntent | None] = []
        self.pending_summaries: list[str | None] = []
        self.outer_repairs: list[bool] = []

    def invoke(
        self,
        request: DomainModelRequest,
        *,
        mode: AssistantTurnMode = "routing",
        pending_intent: OptimizationIntent | None = None,
        pending_summary: str | None = None,
        repair_outer: bool = False,
    ) -> DomainModelInvocationResult:
        self.requests.append(request)
        self.modes.append(mode)
        self.pending_intents.append(pending_intent)
        self.pending_summaries.append(pending_summary)
        self.outer_repairs.append(repair_outer)
        if not self.responses:
            raise AssertionError("unexpected turn-model invocation")
        configured = self.responses.pop(0)
        raw = configured(request, mode, pending_intent) if callable(configured) else configured
        if isinstance(raw, DomainModelInvocationResult):
            return raw
        response = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id=f"fake-{request.request_id}",
            request_ref=request.ref,
            status="succeeded",
            attempts=(
                ProviderAttempt(
                    attempt_index=1,
                    provider_id="fake-provider",
                    provider_version="test-v1",
                    status="succeeded",
                    provider_request_id=None,
                    served_model="fake-turn-model",
                    finish_reason="stop",
                    duration_ms=1,
                    usage=None,
                    error=None,
                ),
            ),
            response=response,
            error=None,
        )


def _intent_raw(
    *,
    ambiguities: list[str] | None = None,
    decisions: list[str] | None = None,
    intent_id: str = "agent-energy-intent",
) -> dict[str, Any]:
    return {
        "schema_id": "optimization-intent",
        "schema_version": "1.0.0",
        "intent_id": intent_id,
        "objectives": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "sense": "minimize",
                "priority": 1,
            }
        ],
        "decision_variables": (
            [
                "furnace_temperature_target_k",
                "tower_top_pressure_target_pa_a",
            ]
            if decisions is None
            else decisions
        ),
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
        "ambiguities": [] if ambiguities is None else ambiguities,
    }


def _intent_response(
    request: DomainModelRequest,
    *,
    ambiguities: list[str] | None = None,
    decisions: list[str] | None = None,
    intent_id: str = "agent-energy-intent",
) -> dict[str, object]:
    return {
        "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
        "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
        "response_id": f"agent-response-t{request.turn_index}-a{request.model_attempt}",
        "request_ref": request.ref.as_dict(),
        "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
        "outcome": "intent",
        "intent": _intent_raw(
            ambiguities=ambiguities,
            decisions=decisions,
            intent_id=intent_id,
        ),
    }


def _turn_response(
    request: DomainModelRequest,
    routes: str | list[str],
    *,
    optimization_response: object | None = None,
    mode: AssistantTurnMode = "routing",
) -> dict[str, object]:
    response: dict[str, object] = {
        "schema_id": ASSISTANT_TURN_SCHEMA_ID,
        "schema_version": ASSISTANT_TURN_SCHEMA_VERSION,
        "request_ref": request.ref.as_dict(),
        "routes": [routes] if isinstance(routes, str) else routes,
    }
    if mode != "confirmation":
        response["capability_manifest_ref"] = request.capability_manifest_ref.as_dict()
        response["optimization_response"] = optimization_response
    return response


def _route(*routes: str) -> _ResponseFactory:
    return lambda request, mode, _pending: _turn_response(
        request,
        list(routes),
        mode=mode,
    )


def _failed_invocation(error: ProviderError) -> _ResponseFactory:
    def response(
        request: DomainModelRequest,
        _mode: AssistantTurnMode,
        _pending: OptimizationIntent | None,
    ) -> object:
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id=f"failed-{request.request_id}",
            request_ref=request.ref,
            status="failed",
            attempts=(
                ProviderAttempt(
                    attempt_index=1,
                    provider_id="fake-provider",
                    provider_version="test-v1",
                    status="failed",
                    provider_request_id=None,
                    served_model=None,
                    finish_reason=None,
                    duration_ms=1,
                    usage=None,
                    error=error,
                ),
            ),
            response=None,
            error=error,
        )

    return response


def _optimization(
    *,
    ambiguities: list[str] | None = None,
    decisions: list[str] | None = None,
    intent_id: str = "agent-energy-intent",
    routes: list[str] | None = None,
) -> _ResponseFactory:
    return lambda request, mode, _pending: _turn_response(
        request,
        "optimization" if routes is None else routes,
        optimization_response=_intent_response(
            request,
            ambiguities=ambiguities,
            decisions=decisions,
            intent_id=intent_id,
        ),
        mode=mode,
    )


def _yield_energy_optimization(
    *,
    ambiguities: list[str] | None = None,
    energy_first: bool = False,
) -> _ResponseFactory:
    def response(
        request: DomainModelRequest,
        mode: AssistantTurnMode,
        _pending: OptimizationIntent | None,
    ) -> object:
        intent = _intent_raw(intent_id="agent-yield-energy-intent")
        objective_rows = [
            ("valuable_distillate_yield", "maximize"),
            ("specific_furnace_fuel_energy_mj_per_t", "minimize"),
        ]
        if energy_first:
            objective_rows.reverse()
        intent["objectives"] = [
            {"metric_id": metric_id, "sense": sense, "priority": index}
            for index, (metric_id, sense) in enumerate(objective_rows, start=1)
        ]
        intent["preference"] = {
            "method": "lexicographic",
            "objective_order": [item[0] for item in objective_rows],
        }
        intent["ambiguities"] = [] if ambiguities is None else ambiguities
        intent["result_request"] = {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": True,
            "max_candidates": 5,
        }
        return _turn_response(
            request,
            "optimization",
            optimization_response={
                "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
                "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
                "response_id": "agent-yield-energy-response",
                "request_ref": request.ref.as_dict(),
                "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
                "outcome": "intent",
                "intent": intent,
            },
            mode=mode,
        )

    return response


def _capability_summary() -> dict[str, object]:
    return {
        "claim_scope": "engineering_simulation_only",
        "objectives": [
            {
                "business_name": "提高有价值馏分收率",
                "objective_id": "yield",
                "sense": "maximize",
                "unit": "1",
            },
            {
                "business_name": "降低单位进料炉燃料热负荷代理",
                "objective_id": "energy",
                "sense": "minimize",
                "unit": "MJ/t",
            },
            {
                "business_name": "减小产品质量代理偏离",
                "objective_id": "quality",
                "sense": "minimize",
                "unit": "1",
            },
        ],
        "decision_variables": [
            {
                "business_name": "炉出口温度目标",
                "decision_id": "temperature",
                "display_unit": "degC",
            },
            {
                "business_name": "塔顶压力目标",
                "decision_id": "pressure",
                "display_unit": "MPa(g)",
            },
        ],
        "supported_objective_count": {"minimum": 1, "maximum": 3},
        "output_kind": "steady_setpoint_vector",
        "solver_called": False,
    }


def _status_summary() -> dict[str, object]:
    return {
        "state_kind": "configured_simulation_context",
        "simulator_mode": "on_demand_offline",
        "simulator_state": "idle",
        "operating_mode": "normal-steady",
        "fresh_feed_load": {"kg_per_s": 113.1388888888889, "t_per_h": 407.3},
        "current_setpoints": [
            {
                "variable_id": "furnace_temperature_target_k",
                "value_k": 628.35,
                "value_deg_c": 355.2,
            },
            {
                "variable_id": "tower_top_pressure_target_pa_a",
                "value_pa_a": 152_325.0,
                "value_mpa_a": 0.152325,
                "value_mpa_g": 0.051,
            },
        ],
        "initial_inventory_ratios": {
            "flash_drum": 1.0,
            "reflux_drum": 1.0,
            "tower_bottom": 1.0,
        },
        "data_timestamp": "2026-06-04T09:16:00+08:00",
        "data_quality": "weak-time-alignment",
    }


def _result_summary() -> dict[str, object]:
    return {
        "status": "success",
        "targets": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "business_name": "降低单位进料炉燃料热负荷代理",
                "sense": "minimize",
                "priority": 1,
                "unit": "MJ/t",
            }
        ],
        "operating_context": {
            "operating_mode": "normal-steady",
            "fresh_feed_load_kg_s": 113.1388888888889,
        },
        "baseline_values": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "value": 188.38,
                "unit": "MJ/t",
            }
        ],
        "recommended_adjustments": [
            {
                "variable_id": "furnace_temperature_target_k",
                "business_name": "炉出口温度目标",
                "unit": "K",
                "baseline_value": 628.35,
                "recommended_value": 626.35,
                "adjustment": -2.0,
            }
        ],
        "predicted_effects": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "predicted_value": 183.99,
                "unit": "MJ/t",
                "directional_improvement": 4.39,
                "relative_improvement": 0.0233,
            }
        ],
        "alternative_candidates": [
            {
                "rank": 2,
                "adjustments": [
                    {
                        "variable_id": "furnace_temperature_target_k",
                        "business_name": "炉出口温度目标",
                        "unit": "K",
                        "baseline_value": 628.35,
                        "recommended_value": 626.85,
                        "adjustment": -1.5,
                    }
                ],
                "predicted_effects": [
                    {
                        "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                        "predicted_value": 184.5,
                        "unit": "MJ/t",
                        "directional_improvement": 3.88,
                        "relative_improvement": 0.0206,
                    }
                ],
                "verification_stage": "M2",
                "verification_status": "feasible",
            }
        ],
    }


def _run_summary() -> dict[str, object]:
    return {
        "workflow_id": "offline-rto-0123456789abcdef",
        "result_source": "offline-rto-0123456789abcdef/result.json",
        "result_summary": _result_summary(),
    }


def _runtime(
    repo_root: Path,
    responses: list[object | _ResponseFactory],
    *,
    tools: _FakeTools | None = None,
    session: _FakeSession | None = None,
) -> tuple[AgentRuntime, _FakeSession, _FakeTools, _FakeTurnModel]:
    actual_session = _FakeSession() if session is None else session
    actual_tools = (
        _FakeTools(
            {
                "show_capabilities": _capability_summary(),
                "show_simulation_status": _status_summary(),
                "run_offline": _run_summary(),
                "inspect_result": _result_summary(),
            }
        )
        if tools is None
        else tools
    )
    model = _FakeTurnModel(responses)
    runtime = AgentRuntime(
        actual_session,
        cast(AgentTools, actual_tools),
        build_intent_communication_service(repo_root=repo_root),
        cast(AssistantTurnModelPort, model),
    )
    return runtime, actual_session, actual_tools, model


def test_help_removes_preview_and_hash_confirmation() -> None:
    assert "/preview" not in HELP
    assert "preview-ref" not in HELP
    assert "/confirm" in HELP
    assert "自然语言" in HELP


def test_chat_route_is_chosen_by_one_strict_model_response(repo_root: Path) -> None:
    runtime, session, tools, model = _runtime(repo_root, [_route("chat")])

    turn = runtime.handle("如何降低常压装置能耗？")

    assert turn.outputs == ("模型> 回复1",)
    assert turn.errors == ()
    assert session.messages == ["如何降低常压装置能耗？"]
    assert tools.calls == []
    assert model.modes == ["routing"]
    assert model.requests[0].user_messages[0].text == "如何降低常压装置能耗？"


@pytest.mark.parametrize(
    "request_text",
    [
        "帮我找个更省燃料的操作点",
        "把燃料消耗压下来，给一组设定值",
        "炉子少烧一点，塔压和炉温都可以动",
        "算一下兼顾收率和能耗的工况",
    ],
)
def test_semantic_model_routes_paraphrased_optimization_without_keyword_gate(
    repo_root: Path,
    request_text: str,
) -> None:
    runtime, session, tools, model = _runtime(repo_root, [_optimization()])

    turn = runtime.handle(request_text)

    assert turn.errors == ()
    assert turn.outputs[0].startswith("请确认本次离线优化：")
    assert model.modes == ["routing"]
    assert model.requests[0].user_messages[0].text == request_text
    request_payload = json.dumps(model.requests[0].as_dict(), ensure_ascii=False)
    for forbidden in ("current_setpoints", "feed_composition", "solver_id", "algorithm_id"):
        assert forbidden not in request_payload
    assert session.messages == []
    assert tools.calls == []


def test_obvious_typo_and_trailing_start_normalize_to_published_multi_objective_intent(
    repo_root: Path,
) -> None:
    request_text = "我想提高超产率 降低能耗开始吧"
    runtime, _, tools, model = _runtime(repo_root, [_yield_energy_optimization()])

    proposal = runtime.handle(request_text)

    assert proposal.errors == ()
    assert "1. 提高有价值馏分收率" in proposal.outputs[0]
    assert "2. 降低单位进料炉燃料热负荷代理" in proposal.outputs[0]
    assert "你可以回复“确认”" in proposal.outputs[0]
    assert tools.calls == []
    assert model.modes == ["routing"]
    assert model.requests[0].user_messages[0].text == request_text

    runtime.handle("/confirm")
    intent = tools.intents[0]
    assert intent is not None
    assert tuple(item.metric_id for item in intent.objectives) == (
        "valuable_distillate_yield",
        "specific_furnace_fuel_energy_mj_per_t",
    )
    assert tuple(item.sense for item in intent.objectives) == ("maximize", "minimize")
    assert intent.preference.method == "lexicographic"


def test_colloquial_objectives_and_variable_abbreviations_use_the_same_semantic_mapping(
    repo_root: Path,
) -> None:
    request_text = "馏分多出一点，炉子少烧点，炉温和塔压都能动，算组操作点"
    runtime, _, tools, model = _runtime(
        repo_root,
        [_yield_energy_optimization()],
    )

    proposal = runtime.handle(request_text)

    assert "优化目标（按优先级）" in proposal.outputs[0]
    assert "允许调整：炉出口温度目标、塔顶压力目标" in proposal.outputs[0]
    assert "最多 4 个其他候选方案（共最多 5 个）" in proposal.outputs[0]
    assert proposal.errors == ()
    assert tools.calls == []
    assert model.modes == ["routing"]

    runtime.handle("/confirm")
    intent = tools.intents[0]
    assert intent is not None
    assert tuple(item.metric_id for item in intent.objectives) == (
        "valuable_distillate_yield",
        "specific_furnace_fuel_energy_mj_per_t",
    )
    assert intent.decision_variables == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )


def test_optimization_confirmation_hides_preview_hash_and_internal_fields(
    repo_root: Path,
) -> None:
    runtime, _, tools, _ = _runtime(repo_root, [_optimization()])

    turn = runtime.handle("给我算一个低能耗工况")

    displayed = "\n".join(turn.outputs + turn.errors)
    assert "优化问题预览" not in displayed
    assert "solver_called" not in displayed
    assert "artifact" not in displayed
    assert "problem-" not in displayed
    assert "@" not in displayed
    assert "/confirm " not in displayed
    assert "现场验证" not in displayed
    assert tools.calls == []


def test_capability_and_status_routes_do_not_use_local_keywords(repo_root: Path) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_route("capabilities"), _route("operating-status")],
    )

    capability = runtime.handle("这套系统能帮我算哪些东西？")
    status = runtime.handle("眼下装置情况怎样？")

    assert "提高有价值馏分收率" in capability.outputs[0]
    assert "本次未调用求解器" not in capability.outputs[0]
    assert "当前配置工况为正常稳态" in status.outputs[0]
    assert "407.3 t/h" in status.outputs[0]
    assert "355.2 °C" in status.outputs[0]
    assert "0.152325 MPa(a)" in status.outputs[0]
    assert session.messages == []
    assert tools.calls == [
        ("show_capabilities", None),
        ("show_simulation_status", None),
    ]
    assert model.modes == ["routing", "routing"]


@pytest.mark.parametrize("user_request", ["帮我优化一下", "给当前装置生成一条优化策略"])
def test_generic_optimization_request_gets_actionable_capability_guidance(
    repo_root: Path,
    user_request: str,
) -> None:
    runtime, session, tools, model = _runtime(repo_root, [_route("capabilities")])

    turn = runtime.handle(user_request)

    assert turn.errors == ()
    assert "当前可用的优化目标" in turn.outputs[0]
    assert "可调整的变量" in turn.outputs[0]
    assert "例如可以说" in turn.outputs[0]
    assert "给我一组离线稳态设定值" in turn.outputs[0]
    assert session.messages == []
    assert tools.calls == [("show_capabilities", None)]
    assert model.modes == ["routing"]
    assert runtime.handle("/confirm").outputs == ("当前没有待确认的优化计算。",)


def test_compound_capabilities_and_status_are_combined_without_chat(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_route("capabilities", "operating-status")],
    )

    turn = runtime.handle("能优化哪些目标，现在工况怎么样？")

    assert turn.errors == ()
    assert len(turn.outputs) == 1
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert "当前配置工况为正常稳态" in turn.outputs[0]
    assert session.messages == []
    assert tools.calls == [
        ("show_capabilities", None),
        ("show_simulation_status", None),
    ]
    assert model.modes == ["routing"]


def test_three_local_semantic_routes_are_combined_by_the_same_runtime_path(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_route("capabilities", "operating-status", "assistant-status")],
    )

    turn = runtime.handle("能优化什么、当前工况怎样，刚才助手有报错吗？")

    assert turn.errors == ()
    assert len(turn.outputs) == 1
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert "当前配置工况为正常稳态" in turn.outputs[0]
    assert "当前没有记录到上一轮模型调用失败" in turn.outputs[0]
    assert session.messages == []
    assert tools.calls == [
        ("show_capabilities", None),
        ("show_simulation_status", None),
    ]
    assert model.modes == ["routing"]


def test_compound_capabilities_and_chat_use_one_chat_call(
    repo_root: Path,
) -> None:
    runtime, session, tools, _ = _runtime(
        repo_root,
        [_route("capabilities", "chat")],
    )

    turn = runtime.handle("能优化哪些目标，多目标优化是什么原理？")

    assert turn.errors == ()
    assert len(turn.outputs) == 1
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert "模型> 回复1" in turn.outputs[0]
    assert len(session.messages) == 1
    assert "只回答用户剩余的一般问题" in session.messages[0]
    assert tools.calls == [("show_capabilities", None)]


def test_compound_local_success_is_kept_when_chat_fails(repo_root: Path) -> None:
    session = _FakeSession(failure=RuntimeError("private provider detail"))
    runtime, _, tools, _ = _runtime(
        repo_root,
        [_route("capabilities", "chat")],
        session=session,
    )

    turn = runtime.handle("能优化哪些目标，也介绍一下RTO？")

    assert len(turn.outputs) == 1
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert turn.errors == ("模型调用失败，请稍后重试。",)
    assert "private provider detail" not in "\n".join(turn.outputs + turn.errors)
    assert tools.calls == [("show_capabilities", None)]


def test_compound_optimization_creates_one_pending_intent_without_running(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [
            _optimization(routes=["capabilities", "optimization"]),
            _route("confirm"),
        ],
    )

    proposal = runtime.handle("系统能优化什么，并帮我找个低能耗操作点")

    assert len(proposal.outputs) == 1
    assert "提高有价值馏分收率" in proposal.outputs[0]
    assert "请确认" in proposal.outputs[0]
    assert tools.calls == [("show_capabilities", None)]
    assert session.messages == []

    completed = runtime.handle("确认开始")

    assert completed.outputs == ("模型> 回复1",)
    assert [call[0] for call in tools.calls] == ["show_capabilities", "run_offline"]
    assert sum(intent is not None for intent in tools.intents) == 1
    assert model.modes == ["routing", "confirmation"]


def test_chat_then_status_does_not_make_a_second_status_rendering_call(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_route("chat"), _route("operating-status")],
    )

    identity = runtime.handle("你是谁")
    status = runtime.handle("现在常压装置的状态是什么")

    assert identity.outputs == ("模型> 回复1",)
    assert "当前配置工况为正常稳态" in status.outputs[0]
    assert "数据时间 2026-06-04 09:16:00" in status.outputs[0]
    assert session.messages == ["你是谁"]
    assert tools.calls == [("show_simulation_status", None)]
    assert model.modes == ["routing", "routing"]


def test_real_status_projection_returns_without_using_chat_session(repo_root: Path) -> None:
    session = _FakeSession(failure=AssertionError("status must not call chat"))
    model = _FakeTurnModel([_route("operating-status")])
    runtime = AgentRuntime(
        session,
        AgentTools(repo_root),
        build_intent_communication_service(repo_root=repo_root),
        cast(AssistantTurnModelPort, model),
    )

    turn = runtime.handle("现在常压装置的状态是什么")

    assert turn.errors == ()
    assert "当前配置工况为正常稳态" in turn.outputs[0]
    assert "407.3 t/h" in turn.outputs[0]
    assert "355.2 °C" in turn.outputs[0]
    assert "0.152325 MPa(a)" in turn.outputs[0]
    assert session.messages == []
    assert model.modes == ["routing"]


def test_natural_confirmation_runs_without_preview_reference(repo_root: Path) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_optimization(), _route("confirm")],
    )

    proposal = runtime.handle("帮我找个更省燃料的操作点")
    completed = runtime.handle("可以，就按这个方案开始吧")

    assert proposal.outputs[0].startswith("请确认本次离线优化：")
    assert completed.outputs == ("模型> 回复1",)
    assert completed.errors == ()
    assert tools.calls == [("run_offline", None)]
    assert isinstance(tools.intents[0], OptimizationIntent)
    assert model.modes == ["routing", "confirmation"]
    assert model.pending_intents[1] == tools.intents[0]
    assert model.pending_summaries[1] == proposal.outputs[0]
    assert "626.35" in session.messages[0]
    for forbidden in ("workflow", "problem-", "严格重载", "现场验证", "下装"):
        assert forbidden not in "\n".join(completed.outputs + completed.errors)


def test_short_confirm_command_runs_current_intent(repo_root: Path) -> None:
    runtime, _, tools, model = _runtime(repo_root, [_optimization()])

    runtime.handle("计算一个节能操作点")
    completed = runtime.handle("/confirm")

    assert completed.outputs == ("模型> 回复1",)
    assert tools.calls == [("run_offline", None)]
    assert model.modes == ["routing"]


def test_exact_confirmation_runs_locally_without_a_second_model_call(
    repo_root: Path,
) -> None:
    runtime, _, tools, model = _runtime(repo_root, [_optimization()])

    runtime.handle("给我算一组低能耗设定值")
    completed = runtime.handle("确认")

    assert completed.outputs == ("模型> 回复1",)
    assert completed.errors == ()
    assert tools.calls == [("run_offline", None)]
    assert model.modes == ["routing"]


def test_exact_cancellation_is_local_and_discards_pending_intent(repo_root: Path) -> None:
    runtime, _, tools, model = _runtime(repo_root, [_optimization()])

    runtime.handle("给我算一组低能耗设定值")
    cancelled = runtime.handle("取消")
    nothing = runtime.handle("/confirm")

    assert cancelled.outputs == ("已取消本次优化。",)
    assert nothing.outputs == ("当前没有待确认的优化计算。",)
    assert tools.calls == []
    assert model.modes == ["routing"]


@pytest.mark.parametrize("user_reply", ["我还没有确认", "确认一下有哪些变量", "确认？"])
def test_negative_question_and_punctuated_confirmation_do_not_execute_locally(
    repo_root: Path,
    user_reply: str,
) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [_optimization(), _route("question")],
    )

    runtime.handle("给我算一组低能耗设定值")
    answer = runtime.handle(user_reply)

    assert answer.outputs == ("模型> 回复1",)
    assert tools.calls == []
    assert model.modes == ["routing", "confirmation"]

    runtime.handle("确认")
    assert tools.calls == [("run_offline", None)]
    assert model.modes == ["routing", "confirmation"]


def test_confirmation_classification_contract_failure_keeps_and_redisplays_pending(
    repo_root: Path,
) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [_optimization(), "not-json", "still-not-json"],
    )

    proposal = runtime.handle("给我算一组低能耗设定值")
    uncertain = runtime.handle("那就照这个办")

    assert uncertain.errors == ()
    assert "意图解析" not in uncertain.outputs[0]
    assert "刚才的优化方案仍然保留" in uncertain.outputs[0]
    assert "本次没有开始计算" in uncertain.outputs[0]
    assert f"原方案：\n{proposal.outputs[0]}" in uncertain.outputs[0]
    assert tools.calls == []
    assert model.modes == ["routing", "confirmation", "confirmation"]

    completed = runtime.handle("确认")
    assert completed.outputs == ("模型> 回复1",)
    assert tools.calls == [("run_offline", None)]
    assert model.modes == ["routing", "confirmation", "confirmation"]


def test_last_result_without_receipt_is_local_and_does_not_call_chat(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(repo_root, [_route("last-result")])

    turn = runtime.handle("刚才优化结果怎么样？")

    assert turn.outputs == ("当前会话还没有可解读的优化结果。",)
    assert turn.errors == ()
    assert session.messages == []
    assert tools.calls == []
    assert model.modes == ["routing"]


def test_compound_capabilities_and_missing_last_result_keep_local_success(
    repo_root: Path,
) -> None:
    runtime, session, tools, _ = _runtime(
        repo_root,
        [_route("last-result", "capabilities")],
    )

    turn = runtime.handle("刚才结果怎么样，还能优化哪些目标？")

    assert turn.errors == ()
    assert len(turn.outputs) == 1
    assert "当前会话还没有可解读的优化结果" in turn.outputs[0]
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert session.messages == []
    assert tools.calls == [("show_capabilities", None)]


def test_completed_run_receipt_survives_failed_initial_result_chat(
    repo_root: Path,
) -> None:
    session = _FakeSession(
        failure=DmxChatError("provider body", code="invalid-response", http_status=200)
    )
    runtime, _, tools, model = _runtime(
        repo_root,
        [_optimization(), _route("last-result")],
        session=session,
    )

    runtime.handle("计算一个节能操作点")
    failed_explanation = runtime.handle("/confirm")
    session.failure = None
    recovered = runtime.handle("刚才优化结果怎么样？")

    assert failed_explanation.errors == ("模型服务已响应，但返回内容未通过本地解析，请重试。",)
    assert recovered.outputs == ("模型> 回复2",)
    assert "626.35" in session.messages[1]
    assert "offline-rto-0123456789abcdef/result.json" not in session.messages[1]
    assert [call[0] for call in tools.calls] == ["run_offline"]
    assert model.modes == ["routing", "routing"]


def test_malformed_run_envelope_is_not_saved_as_recent_result(repo_root: Path) -> None:
    tools = _FakeTools(
        {
            "show_capabilities": _capability_summary(),
            "show_simulation_status": _status_summary(),
            "run_offline": {"result_summary": _result_summary()},
            "inspect_result": _result_summary(),
        }
    )
    runtime, session, _, _ = _runtime(
        repo_root,
        [_optimization(), _route("last-result")],
        tools=tools,
    )

    runtime.handle("计算一个节能操作点")
    failed = runtime.handle("/confirm")
    recent = runtime.handle("刚才结果怎么样？")

    assert failed.errors == ("这次优化计算没有完成，请重新发起。",)
    assert recent.outputs == ("当前会话还没有可解读的优化结果。",)
    assert session.messages == []


def test_last_result_receipt_is_frozen_before_source_mapping_changes(
    repo_root: Path,
) -> None:
    run_envelope = _run_summary()
    tools = _FakeTools(
        {
            "show_capabilities": _capability_summary(),
            "show_simulation_status": _status_summary(),
            "run_offline": run_envelope,
            "inspect_result": _result_summary(),
        }
    )
    runtime, session, _, _ = _runtime(
        repo_root,
        [_optimization(), _route("last-result")],
        tools=tools,
    )

    runtime.handle("计算一个节能操作点")
    runtime.handle("/confirm")
    result_summary = cast(dict[str, object], run_envelope["result_summary"])
    adjustments = cast(list[dict[str, object]], result_summary["recommended_adjustments"])
    adjustments[0]["recommended_value"] = 999.0
    runtime.handle("刚才优化结果怎么样？")

    assert "626.35" in session.messages[1]
    assert "999" not in session.messages[1]


def test_compound_last_result_and_capabilities_return_one_complete_output(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_optimization(), _route("last-result", "capabilities")],
    )

    runtime.handle("计算一个节能操作点")
    runtime.handle("/confirm")
    combined = runtime.handle("刚才结果怎么样，还能优化哪些目标？")

    assert combined.errors == ()
    assert len(combined.outputs) == 1
    assert "提高有价值馏分收率" in combined.outputs[0]
    assert "模型> 回复2" in combined.outputs[0]
    assert len(session.messages) == 2
    assert "626.35" in session.messages[1]
    assert [call[0] for call in tools.calls] == ["run_offline", "show_capabilities"]
    assert model.modes == ["routing", "routing"]


def test_clear_removes_recent_result_receipt(repo_root: Path) -> None:
    runtime, session, _, _ = _runtime(
        repo_root,
        [_optimization(), _route("last-result")],
    )

    runtime.handle("计算一个节能操作点")
    runtime.handle("/confirm")
    runtime.handle("/clear")
    recent = runtime.handle("刚才结果怎么样？")

    assert recent.outputs == ("当前会话还没有可解读的优化结果。",)
    assert len(session.messages) == 1
    assert session.clear_calls == 1


def test_natural_revision_replaces_intent_and_asks_again(repo_root: Path) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [
            _optimization(),
            _route("revise"),
            _optimization(
                decisions=["furnace_temperature_target_k"],
                intent_id="agent-energy-revised",
            ),
            _route("confirm"),
        ],
    )

    runtime.handle("帮我找个更省燃料的操作点")
    revised = runtime.handle("塔顶压力不要动，只调炉温")
    completed = runtime.handle("确认，可以算了")

    assert "优化目标：降低单位进料炉燃料热负荷代理" in revised.outputs[0]
    assert "允许调整：炉出口温度目标。" in revised.outputs[0]
    assert "塔顶压力目标" not in revised.outputs[0]
    assert completed.outputs == ("模型> 回复1",)
    assert tools.calls == [("run_offline", None)]
    assert tools.intents[0] is not None
    assert tools.intents[0].decision_variables == ("furnace_temperature_target_k",)
    assert model.modes == ["routing", "confirmation", "intent", "confirmation"]
    assert model.pending_intents[1] is not None
    assert model.pending_intents[2] is not None


def test_revision_contract_failure_keeps_original_pending_intent(repo_root: Path) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [_optimization(), _route("revise"), "not-json", "still-not-json"],
    )

    proposal = runtime.handle("给我算一组低能耗设定值")
    rejected_revision = runtime.handle("塔顶压力不要动，只调炉温")

    assert rejected_revision.errors == ()
    assert "刚才的优化方案仍然保留" in rejected_revision.outputs[0]
    assert f"原方案：\n{proposal.outputs[0]}" in rejected_revision.outputs[0]
    assert tools.calls == []
    assert model.modes == ["routing", "confirmation", "intent", "intent"]

    runtime.handle("确认")
    assert tools.calls == [("run_offline", None)]
    original = tools.intents[0]
    assert original is not None
    assert original.decision_variables == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )


def test_exact_confirmation_can_accept_original_while_revision_needs_clarification(
    repo_root: Path,
) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [
            _optimization(),
            _route("revise"),
            _optimization(ambiguities=["decision-variable-selection-ambiguous"]),
            _route("chat"),
        ],
    )

    runtime.handle("给我算一组低能耗设定值")
    clarification = runtime.handle("我想改一下可调变量")

    assert "请补充以下信息" in clarification.outputs[0]
    assert tools.calls == []

    completed = runtime.handle("确认")
    assert completed.outputs == ("模型> 回复1",)
    assert tools.calls == [("run_offline", None)]
    accepted = tools.intents[0]
    assert accepted is not None
    assert accepted.decision_variables == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )

    runtime.handle("1")
    assert model.modes == ["routing", "confirmation", "intent", "routing"]


def test_natural_cancel_discards_pending_calculation(repo_root: Path) -> None:
    runtime, _, tools, _ = _runtime(
        repo_root,
        [_optimization(), _route("cancel")],
    )

    runtime.handle("帮我算一个低能耗工况")
    cancelled = runtime.handle("先不算了")
    nothing = runtime.handle("/confirm")

    assert cancelled.outputs == ("已取消本次优化。",)
    assert nothing.outputs == ("当前没有待确认的优化计算。",)
    assert tools.calls == []


def test_question_keeps_pending_confirmation_until_later_confirmation(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [_optimization(), _route("question"), _route("confirm")],
    )

    proposal = runtime.handle("帮我找个低能耗操作点")
    answer = runtime.handle("为什么要同时调整这两个变量？")
    completed = runtime.handle("明白了，开始计算")

    assert proposal.outputs[0].startswith("请确认本次离线优化：")
    assert answer.outputs == ("模型> 回复1",)
    assert completed.outputs == ("模型> 回复2",)
    assert tools.calls == [("run_offline", None)]
    assert "确认内容：" in session.messages[0]
    assert model.modes == ["routing", "confirmation", "confirmation"]
    assert model.pending_intents[2] is not None


def test_ambiguous_intent_clarifies_then_returns_natural_confirmation(
    repo_root: Path,
) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [
            _optimization(ambiguities=["decision-variable-selection-ambiguous"]),
            _optimization(),
        ],
    )

    question = runtime.handle("开始优化，但我还没想好调整哪些变量")
    proposal = runtime.handle("1,2")

    assert "请补充以下信息" in question.outputs[0]
    assert "尚未加载受信工况" not in question.outputs[0]
    assert proposal.outputs[0].startswith("请确认本次离线优化：")
    assert tools.calls == []
    assert model.modes == ["routing", "intent"]
    assert model.requests[1].clarification_answers[0].values == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )


def test_two_objective_priority_clarification_accepts_one_business_labeled_choice(
    repo_root: Path,
) -> None:
    runtime, _, tools, model = _runtime(
        repo_root,
        [
            _yield_energy_optimization(
                ambiguities=["objective-priority-ambiguous"],
                energy_first=True,
            ),
            _yield_energy_optimization(energy_first=True),
        ],
    )

    question = runtime.handle("我想降低能耗，提高产率")
    proposal = runtime.handle("1")

    assert "请选择第一优先" in question.outputs[0]
    assert "降低单位进料炉燃料热负荷代理" in question.outputs[0]
    assert "提高有价值馏分收率" in question.outputs[0]
    assert "specific_furnace_fuel_energy_mj_per_t" not in question.outputs[0]
    assert "请回复一个选项编号" in question.outputs[0]
    assert "1. 降低单位进料炉燃料热负荷代理" in proposal.outputs[0]
    assert "2. 提高有价值馏分收率" in proposal.outputs[0]
    assert proposal.errors == ()
    assert model.requests[1].clarification_answers[0].values == (
        "specific_furnace_fuel_energy_mj_per_t",
    )
    assert tools.calls == []


def test_clarification_choice_is_retained_when_followup_model_call_fails(
    repo_root: Path,
) -> None:
    error = ProviderError(
        category="rate_limit",
        code="rate-limited",
        message="DMXAPI assistant invocation failed",
        retryable=True,
        http_status=429,
    )
    runtime, _, tools, model = _runtime(
        repo_root,
        [
            _yield_energy_optimization(
                ambiguities=["objective-priority-ambiguous"],
                energy_first=True,
            ),
            _failed_invocation(error),
            _yield_energy_optimization(energy_first=True),
        ],
    )

    runtime.handle("我想降低能耗，提高产率")
    failed = runtime.handle("1")
    recovered = runtime.handle("1")

    assert "已收到你的选择" in failed.errors[0]
    assert "请求较多" in failed.errors[0]
    assert "请再次回复相同选择" in failed.errors[0]
    assert recovered.outputs[0].startswith("请确认本次离线优化：")
    assert model.modes == ["routing", "intent", "intent"]
    assert tools.calls == []


def test_invalid_nested_intent_gets_one_full_replacement_repair(
    repo_root: Path,
) -> None:
    invalid = lambda request, _mode, _pending: _turn_response(
        request,
        "optimization",
        optimization_response={},
        mode=_mode,
    )
    runtime, _, tools, model = _runtime(repo_root, [invalid, _optimization()])

    turn = runtime.handle("给我算一个节能操作点")

    assert turn.errors == ()
    assert turn.outputs[0].startswith("请确认本次离线优化：")
    assert model.modes == ["routing", "intent"]
    assert model.requests[1].model_attempt == 2
    assert model.requests[1].feedback_issues[0].code == "invalid-model-response"
    assert tools.calls == []


@pytest.mark.parametrize(
    "invalid_outer",
    [
        "not-json",
        lambda request, _mode, _pending: {
            **_turn_response(request, "chat"),
            "extra": True,
        },
    ],
)
def test_first_invalid_outer_response_is_retried_as_full_replacement(
    repo_root: Path,
    invalid_outer: object | _ResponseFactory,
) -> None:
    runtime, session, tools, model = _runtime(
        repo_root,
        [invalid_outer, _optimization()],
    )

    turn = runtime.handle("给我算一个节能操作点")

    assert turn.errors == ()
    assert turn.outputs[0].startswith("请确认本次离线优化：")
    assert model.modes == ["routing", "routing"]
    assert model.outer_repairs == [False, True]
    assert model.requests[0] == model.requests[1]
    assert session.messages == []
    assert tools.calls == []


def test_two_invalid_outer_responses_return_capability_guidance_without_domain_action(
    repo_root: Path,
) -> None:
    runtime, session, tools, model = _runtime(repo_root, ["not-json", "still-not-json"])

    turn = runtime.handle("给我算一个节能操作点")

    assert turn.errors == ()
    assert "意图解析" not in turn.outputs[0]
    assert "没有创建待确认任务" in turn.outputs[0]
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert "炉出口温度目标" in turn.outputs[0]
    assert "例如" in turn.outputs[0]
    assert model.modes == ["routing", "routing"]
    assert model.outer_repairs == [False, True]
    assert session.messages == []
    assert tools.calls == []


def test_turn_model_transport_failure_is_not_retried(repo_root: Path) -> None:
    error = ProviderError(
        category="transport",
        code="dmx-chat-failed",
        message="DMXAPI assistant invocation failed",
        retryable=False,
        http_status=None,
    )
    runtime, session, tools, model = _runtime(repo_root, [_failed_invocation(error)])

    turn = runtime.handle("给我算一个节能操作点")

    assert turn.errors == ("本次模型调用没有完成，请稍后重试。",)
    assert model.modes == ["routing"]
    assert model.outer_repairs == [False]
    assert session.messages == []
    assert tools.calls == []


def test_retryable_transport_failure_gets_one_bounded_retry(repo_root: Path) -> None:
    error = ProviderError(
        category="transport",
        code="transport-connect",
        message="DMXAPI assistant invocation failed",
        retryable=True,
        http_status=None,
    )
    runtime, _, tools, model = _runtime(
        repo_root,
        [_failed_invocation(error), _optimization()],
    )

    turn = runtime.handle("给我算一个节能操作点")

    assert turn.errors == ()
    assert turn.outputs[0].startswith("请确认本次离线优化：")
    assert model.modes == ["routing", "routing"]
    assert model.outer_repairs == [False, False]
    assert tools.calls == []


def test_followup_about_provider_failure_uses_recorded_event_not_simulator_state(
    repo_root: Path,
) -> None:
    error = ProviderError(
        category="rate_limit",
        code="rate-limited",
        message="DMXAPI assistant invocation failed",
        retryable=True,
        http_status=429,
    )
    runtime, session, tools, model = _runtime(
        repo_root,
        [_failed_invocation(error), _route("assistant-status")],
    )

    failed = runtime.handle("我想降低能耗并提高产率")
    explanation = runtime.handle("什么情况，你服务不可用？")

    assert failed.errors == ("模型服务当前请求较多，请稍后重试。",)
    assert "请求限流" in explanation.outputs[0]
    assert "RTO模拟器" in explanation.outputs[0]
    assert "空闲状态无关" in explanation.outputs[0]
    assert session.messages == []
    assert tools.calls == []
    assert model.modes == ["routing", "routing"]


def test_plain_chat_preserves_safe_response_failure_for_later_explanation(
    repo_root: Path,
) -> None:
    session = _FakeSession(
        failure=DmxChatError(
            "safe provider response detail",
            code="invalid-response",
            http_status=200,
        )
    )
    runtime, _, tools, model = _runtime(
        repo_root,
        [_route("chat"), _route("assistant-status")],
        session=session,
    )

    failed = runtime.handle("你是谁")
    explanation = runtime.handle("刚才为什么失败？")

    assert failed.errors == ("模型服务已响应，但返回内容未通过本地解析，请重试。",)
    assert "普通问答未完成" in explanation.outputs[0]
    assert "已返回HTTP 200" in explanation.outputs[0]
    assert "未通过本地结构解析" in explanation.outputs[0]
    assert "safe provider response detail" not in "\n".join(
        failed.outputs + failed.errors + explanation.outputs + explanation.errors
    )
    assert tools.calls == []
    assert model.modes == ["routing", "routing"]


def test_unsupported_action_redirects_to_nearest_offline_capability(repo_root: Path) -> None:
    runtime, session, tools, _ = _runtime(repo_root, [_route("unsupported-action")])

    turn = runtime.handle("批准并下装这份策略")

    assert "不能直接完成正式策略审批、发布、下装或现场控制" in turn.outputs[0]
    assert "离线RTO设定点建议" in turn.outputs[0]
    assert "提高有价值馏分收率" in turn.outputs[0]
    assert "例如" in turn.outputs[0]
    assert session.messages == []
    assert tools.calls == []


def test_result_is_explained_without_dumping_json_or_evidence_fields(repo_root: Path) -> None:
    runtime, session, tools, _ = _runtime(repo_root, [])

    turn = runtime.handle("/result /safe/run-dir")

    assert turn.outputs == ("模型> 回复1",)
    assert tools.calls == [("inspect_result", "/safe/run-dir")]
    assert "626.35" in session.messages[0]
    assert "/safe/run-dir" not in session.messages[0]
    displayed = "\n".join(turn.outputs + turn.errors)
    assert "RTO设定值概要" not in displayed
    assert "strictly_reloaded" not in displayed
    assert "workflow" not in displayed


def test_explicit_result_read_becomes_recent_result_for_natural_followup(
    repo_root: Path,
) -> None:
    workflow_id = "offline-rto-0123456789abcdef"
    runtime, session, tools, model = _runtime(
        repo_root,
        [_route("last-result")],
    )

    loaded = runtime.handle(f"/result {workflow_id}")
    followup = runtime.handle("为什么这样设置？")

    assert loaded.outputs == ("模型> 回复1",)
    assert followup.outputs == ("模型> 回复2",)
    assert tools.calls == [("inspect_result", workflow_id)]
    assert "626.35" in session.messages[1]
    assert workflow_id not in session.messages[1]
    assert model.modes == ["routing"]


def test_clear_and_cancel_remove_pending_state(repo_root: Path) -> None:
    runtime, session, _, _ = _runtime(repo_root, [_optimization(), _optimization()])

    runtime.handle("先算一组低能耗设定值")
    assert runtime.handle("/cancel").outputs == ("已取消当前任务。",)
    assert runtime.handle("/confirm").outputs == ("当前没有待确认的优化计算。",)

    runtime.handle("再算一组低能耗设定值")
    assert runtime.handle("/clear").outputs == ("对话和待处理任务已清空。",)
    assert runtime.handle("/confirm").outputs == ("当前没有待确认的优化计算。",)
    assert session.clear_calls == 1


def test_model_and_tool_failures_do_not_echo_private_details(repo_root: Path) -> None:
    secret = "sk-secret-value-must-not-appear"
    session = _FakeSession(failure=RuntimeError(secret))
    tools = _FakeTools({"show_simulation_status": {}})
    tools.failure = ValueError(secret)
    runtime, _, _, _ = _runtime(
        repo_root,
        [_route("chat"), _route("operating-status")],
        tools=tools,
        session=session,
    )

    chat = runtime.handle("什么是RTO？")
    status = runtime.handle("现在装置情况怎样？")

    combined = "\n".join(chat.outputs + chat.errors + status.outputs + status.errors)
    assert secret not in combined
    assert "模型调用失败" in chat.errors[0]
    assert "无法读取工况" in status.errors[0]
