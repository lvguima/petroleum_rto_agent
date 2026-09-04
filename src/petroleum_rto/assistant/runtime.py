"""Semantic single-turn runtime for the local engineering assistant."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from petroleum_rto.domain_model.chat import DmxChatError
from petroleum_rto.rto.communication import (
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRequest,
    CommunicationResult,
    DomainModelInvocationResult,
    DomainModelRequest,
    IntentCommunicationService,
    OptimizationIntent,
    ProviderError,
)

from .dmx_intent_adapter import (
    AssistantTurnDecision,
    AssistantTurnMode,
    _provider_error_from_chat_error,
    decode_assistant_turn_decision,
)
from .tools import AgentTools

HELP = """可用命令：
  /capabilities                  查看当前可用的离线RTO能力
  /confirm                       确认当前优化方案并开始计算
  /result <结果编号|目录|result.json>  读取并解释一份已有的离线RTO结果
  /cancel                        取消当前优化或意图澄清
  /clear                         清空本次内存对话和待处理状态
  /help                          显示帮助
  /exit                          退出

也可以直接用自然语言提问、查询工况、提出优化需求或确认计算。
"""

_RESULT_INSTRUCTION = (
    "以下是程序读取的RTO结果。请直接用简洁、自然的中文解释用户关心的结果，"
    "保留推荐设定值、目标变化和单位，不要输出JSON或复述输入结构，"
    "alternative_candidates非空时再说明其他候选的调整、预测效果、"
    "verification_stage和verification_status；M2表示仅完成稳态评价，M4表示已进行动态验证，"
    "status必须按原值如实解释。不得把候选称为策略，也不得把M2候选说成已完成M4验证。"
    "不要增加与结果无关的提示或免责声明。\n"
    "结果数据："
)

_CONFIRMED_RESULT_INSTRUCTION = (
    "以下是刚完成的RTO计算结果。请直接给出推荐设定值和预测的目标改善，"
    "保留数值与单位。alternative_candidates非空时再列出其他候选，"
    "并准确说明每项的verification_stage和verification_status。"
    "M2表示仅完成稳态评价，M4表示已进行动态验证，status按原值如实解释；候选不是策略，"
    "M2候选不得表述为已完成M4验证。"
    "不要输出JSON、内部标识、运行过程或免责声明。\n"
    "结果数据："
)

_CONFIRMATION_QUESTION_INSTRUCTION = (
    "当前有一项待确认的优化计算。请根据确认内容简洁回答用户的问题，"
    "不要改变其中的目标或变量，也不要添加内部过程、权限或免责声明。\n"
)

_LAST_RESULT_INSTRUCTION = (
    "以下是当前会话最近一次完成的RTO计算结果。请直接用简洁、自然的中文回答用户关于该结果的"
    "问题，保留推荐设定值、目标变化和单位。用户询问其他候选时，"
    "仅解释alternative_candidates中的项：M2是仅完成稳态评价，M4是已进行动态验证，"
    "status按原值如实解释；"
    "不得把候选称为策略。不要输出JSON、内部标识、路径、运行过程或免责声明。\n"
)

_COMPOUND_CHAT_INSTRUCTION = (
    "这是一个复合问题。同一回答中的能力、工况、助手状态或不支持动作已由本地代码另行回答；"
    "请不要重复或改写这些部分，只回答用户剩余的一般问题。\n用户问题："
)

type _ModelFailureKind = Literal["provider", "response-contract", "chat"]


@dataclass(frozen=True, slots=True)
class _LastModelFailure:
    kind: _ModelFailureKind
    phase: str
    provider_error: ProviderError | None = None


def _provider_failure_text(error: ProviderError | None) -> str:
    if error is None:
        return "本次模型调用没有完成，请稍后重试。"
    if error.category == "rate_limit":
        return "模型服务当前请求较多，请稍后重试。"
    if error.category in {"authentication", "payment", "permission", "not_found"}:
        return "模型服务配置异常，当前无法完成请求。"
    if error.category == "invalid_request":
        return "本次模型请求未被服务接受，请稍后重试。"
    if error.category in {"protocol", "truncated", "model_mismatch"}:
        if error.http_status == 200:
            return "模型服务已响应，但返回内容未通过本地解析，请重试。"
        return "模型响应异常，本次请求没有完成，请重试。"
    return "本次模型调用没有完成，请稍后重试。"


class ChatSession(Protocol):
    def ask(self, message: str) -> str: ...

    def clear(self) -> None: ...


class AssistantTurnModelPort(Protocol):
    def invoke(
        self,
        request: DomainModelRequest,
        *,
        mode: AssistantTurnMode = "routing",
        pending_intent: OptimizationIntent | None = None,
        pending_summary: str | None = None,
        repair_outer: bool = False,
    ) -> DomainModelInvocationResult: ...


@dataclass(frozen=True, slots=True)
class AgentTurn:
    outputs: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    should_exit: bool = False


@dataclass(frozen=True, slots=True)
class _PendingClarification:
    request: DomainModelRequest
    result: CommunicationResult


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    intent: OptimizationIntent
    summary: str


@dataclass(frozen=True, slots=True)
class _LastResultReceipt:
    workflow_id: str | None
    result_source: str | None
    result_summary_json: str


def _normalized_summary(summary: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(summary),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("chat summary must contain finite JSON values") from exc


def _combine_turns(turns: Sequence[AgentTurn]) -> AgentTurn:
    outputs = tuple(output for turn in turns for output in turn.outputs)
    errors = tuple(error for turn in turns for error in turn.errors)
    if len(outputs) > 1:
        outputs = ("\n\n".join(outputs),)
    return AgentTurn(outputs=outputs, errors=errors)


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    return value


def _rows(value: object, *, context: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be a sequence")
    return tuple(_mapping(item, context=f"{context}[{index}]") for index, item in enumerate(value))


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{context} must be non-empty text")
    return value.strip()


def _number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _display_number(value: float) -> str:
    return format(value, ".12g")


def _format_operating_status(summary: Mapping[str, object]) -> str:
    if summary.get("state_kind") != "configured_simulation_context":
        raise ValueError("unsupported operating status kind")
    if summary.get("simulator_mode") != "on_demand_offline":
        raise ValueError("unsupported simulator mode")
    if summary.get("simulator_state") != "idle":
        raise ValueError("unsupported simulator state")
    if summary.get("operating_mode") != "normal-steady":
        raise ValueError("unsupported operating mode")

    feed = _mapping(summary.get("fresh_feed_load"), context="fresh_feed_load")
    feed_t_per_h = _number(feed.get("t_per_h"), context="fresh_feed_load.t_per_h")
    setpoints = _rows(summary.get("current_setpoints"), context="current_setpoints")
    by_id: dict[str, Mapping[str, object]] = {}
    for index, row in enumerate(setpoints):
        variable_id = _text(
            row.get("variable_id"),
            context=f"current_setpoints[{index}].variable_id",
        )
        if variable_id in by_id:
            raise ValueError("operating status repeats a setpoint")
        by_id[variable_id] = row
    expected_ids = {
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    }
    if set(by_id) != expected_ids:
        raise ValueError("operating status setpoints differ from the supported set")
    furnace_deg_c = _number(
        by_id["furnace_temperature_target_k"].get("value_deg_c"),
        context="furnace_temperature_target_k.value_deg_c",
    )
    pressure_row = by_id["tower_top_pressure_target_pa_a"]
    pressure_mpa_a = _number(
        pressure_row.get("value_mpa_a"),
        context="tower_top_pressure_target_pa_a.value_mpa_a",
    )
    pressure_mpa_g = _number(
        pressure_row.get("value_mpa_g"),
        context="tower_top_pressure_target_pa_a.value_mpa_g",
    )
    timestamp = _text(summary.get("data_timestamp"), context="data_timestamp")
    try:
        observed_at = datetime.fromisoformat(timestamp)
    except ValueError:
        raise ValueError("data_timestamp must be ISO 8601 text") from None
    if observed_at.tzinfo is None:
        raise ValueError("data_timestamp must include a timezone")
    if summary.get("data_quality") != "weak-time-alignment":
        raise ValueError("unsupported data quality")

    return (
        "当前配置工况为正常稳态。"
        f"进料负荷 {_display_number(feed_t_per_h)} t/h，"
        f"炉出口温度设定 {_display_number(furnace_deg_c)} °C，"
        f"塔顶压力设定 {_display_number(pressure_mpa_a)} MPa(a)"
        f"（{_display_number(pressure_mpa_g)} MPa(g)）。"
        "模拟器为按需离线模式，当前空闲；"
        f"数据时间 {observed_at.strftime('%Y-%m-%d %H:%M:%S')}，时间对齐较弱。"
    )


def _format_capabilities(summary: Mapping[str, object]) -> str:
    objectives = summary.get("objectives")
    decisions = summary.get("decision_variables")
    counts = summary.get("supported_objective_count")
    if not isinstance(objectives, list) or not isinstance(decisions, list):
        raise TypeError("capability rows must be lists")
    if not isinstance(counts, Mapping):
        raise TypeError("supported_objective_count must be a mapping")
    if summary.get("claim_scope") != "engineering_simulation_only":
        raise ValueError("unsupported capability claim scope")
    if summary.get("solver_called") is not False:
        raise ValueError("capability query unexpectedly called a solver")

    def names(rows: list[object], *, context: str) -> str:
        values: list[str] = []
        for index, row in enumerate(rows):
            item = _mapping(row, context=f"{context}[{index}]")
            values.append(_text(item.get("business_name"), context=f"{context}.business_name"))
        if not values:
            raise ValueError(f"{context} must not be empty")
        return "、".join(values)

    minimum = counts.get("minimum")
    maximum = counts.get("maximum")
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        raise TypeError("minimum objective count must be an integer")
    if isinstance(maximum, bool) or not isinstance(maximum, int):
        raise TypeError("maximum objective count must be an integer")
    if minimum <= 0 or minimum > maximum or maximum > len(objectives):
        raise ValueError("objective counts are inconsistent with available objectives")
    if summary.get("output_kind") != "steady_setpoint_vector":
        raise ValueError("unsupported capability output kind")
    objective_names = names(objectives, context="objectives")
    decision_names = names(decisions, context="decision_variables")
    first_objective = _text(
        _mapping(objectives[0], context="objectives[0]").get("business_name"),
        context="objectives[0].business_name",
    )
    return (
        "当前可用的优化目标包括"
        f"{objective_names}；"
        "可调整的变量包括"
        f"{decision_names}。"
        f"每次可组合 {minimum} 至 {maximum} 个目标。\n"
        f"例如可以说：“以{first_objective}为主，允许调整{decision_names}，"
        "给我一组离线稳态设定值。”"
    )


def _manifest_business_names(
    rows: Sequence[Mapping[str, object]],
    *,
    rows_name: Literal["objectives", "decisions"],
    identity_name: Literal["metric_id", "decision_id"],
) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if row.get("availability") != "available":
            continue
        identity = _text(row.get(identity_name), context=f"{rows_name}[{index}].{identity_name}")
        business_name = _text(
            row.get("business_name"),
            context=f"{rows_name}[{index}].business_name",
        )
        values.append((identity, business_name))
    if not values or len(values) != len({item[0] for item in values}):
        raise ValueError(f"available {rows_name} must be non-empty and unique")
    return tuple(values)


def _format_intent_confirmation(
    intent: OptimizationIntent,
    request: DomainModelRequest,
) -> str:
    manifest = request.capability_manifest
    objective_names = dict(
        _manifest_business_names(
            manifest.objectives,
            rows_name="objectives",
            identity_name="metric_id",
        )
    )
    decision_names = dict(
        _manifest_business_names(
            manifest.decisions,
            rows_name="decisions",
            identity_name="decision_id",
        )
    )
    try:
        selected_objectives = tuple(objective_names[item.metric_id] for item in intent.objectives)
        selected_decisions = tuple(decision_names[item] for item in intent.decision_variables)
    except KeyError as exc:
        raise ValueError("intent references an unavailable business capability") from exc

    if len(selected_objectives) == 1:
        objective_line = f"优化目标：{selected_objectives[0]}。"
    else:
        ordered = "；".join(
            f"{index}. {name}" for index, name in enumerate(selected_objectives, start=1)
        )
        objective_line = f"优化目标（按优先级）：{ordered}。"

    candidate_count = intent.result_request.max_candidates
    if intent.result_request.include_alternatives and candidate_count > 1:
        output_line = (
            "输出：1 个推荐稳态设定点方案，"
            f"并提供最多 {candidate_count - 1} 个其他候选方案"
            f"（共最多 {candidate_count} 个）。"
        )
    else:
        output_line = "输出：1 个推荐稳态设定点方案。"
    selected_decision_text = "、".join(selected_decisions)

    return "\n".join(
        (
            "请确认本次离线优化：",
            objective_line,
            f"允许调整：{selected_decision_text}。",
            output_line,
            "确认后将读取当前配置的离线工况并开始计算。",
            "你可以回复“确认”、直接提出修改，或回复“取消”。",
        )
    )


def _format_capability_guidance(
    request: DomainModelRequest,
    *,
    pending: _PendingConfirmation | None = None,
) -> str:
    manifest = request.capability_manifest
    objectives = tuple(
        name
        for _, name in _manifest_business_names(
            manifest.objectives,
            rows_name="objectives",
            identity_name="metric_id",
        )
    )
    decisions = tuple(
        name
        for _, name in _manifest_business_names(
            manifest.decisions,
            rows_name="decisions",
            identity_name="decision_id",
        )
    )
    objective_text = "、".join(objectives)
    decision_text = "、".join(decisions)
    if pending is not None:
        return (
            "刚才的优化方案仍然保留，本次没有开始计算。"
            "你可以直接回复“确认”或“取消”；也可以说明要修改的目标或变量，"
            "或继续提问。\n"
            f"当前可用目标：{objective_text}。"
            f"可调变量：{decision_text}。\n\n"
            f"原方案：\n{pending.summary}"
        )
    return (
        "我还没有把这句话整理成可执行的请求，因此没有开始计算，"
        "也没有创建待确认任务。\n"
        f"你可以查看当前配置工况，或从以下目标中选择：{objective_text}。"
        f"可调变量：{decision_text}。\n"
        f"例如：“以{objectives[0]}为主，允许调整{decision_text}，"
        "给我一组离线稳态设定值。”"
    )


def _direct_confirmation_action(message: str) -> Literal["confirm", "cancel"] | None:
    if message == "确认":
        return "confirm"
    if message == "取消":
        return "cancel"
    return None


def _format_clarification(clarification: ClarificationRequest) -> str:
    lines = ["请补充以下信息："]
    for question_index, question in enumerate(clarification.questions, start=1):
        lines.append(f"{question_index}. {question.prompt}")
        for option_index, option in enumerate(question.options, start=1):
            lines.append(f"   {option_index}) {option.label}")
    if len(clarification.questions) == 1:
        question = clarification.questions[0]
        if question.answer_kind == "single-select":
            lines.append("请回复一个选项编号，例如：1。")
        elif question.answer_kind == "ordered-select":
            lines.append("请按优先级完整回复所有编号，例如：1,2。")
        else:
            lines.append("请回复一个或多个选项编号，例如：1,2。")
    else:
        lines.append("请按问题编号回复，例如：1=1,2；2=1。")
    return "\n".join(lines)


def _answer_sections(text: str, question_count: int) -> Mapping[int, str]:
    matches = tuple(re.finditer(r"(?:^|[；;\n])\s*(\d+)\s*[=：:]\s*", text))
    if not matches:
        if question_count > 1 and re.fullmatch(r"[\d\s,，、/]+", text):
            raise ValueError("multiple questions require explicit question numbers")
        return {index: text for index in range(1, question_count + 1)}
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        question_index = int(match.group(1))
        if question_index < 1 or question_index > question_count or question_index in sections:
            raise ValueError("clarification question number is invalid")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[question_index] = text[match.end() : end].strip(" ；;\n")
    if set(sections) != set(range(1, question_count + 1)):
        raise ValueError("clarification answers must cover every question")
    return sections


def _answer_values(text: str, question: ClarificationQuestion) -> tuple[str, ...]:
    answer = text.strip()
    if not answer:
        raise ValueError("clarification answer is empty")
    if question.answer_kind == "free-text":
        return (answer,)
    tokens = tuple(item for item in re.split(r"[\s,，、/]+", answer) if item)
    exact: dict[str, str] = {}
    for index, option in enumerate(question.options, start=1):
        exact[str(index)] = option.value
        exact[option.value.casefold()] = option.value
        exact[option.label.casefold()] = option.value
    if tokens and all(token.casefold() in exact for token in tokens):
        values = tuple(exact[token.casefold()] for token in tokens)
    else:
        positions: list[tuple[int, int, str]] = []
        folded = answer.casefold()
        for option_index, option in enumerate(question.options):
            found = tuple(
                position
                for candidate in (option.label.casefold(), option.value.casefold())
                if (position := folded.find(candidate)) >= 0
            )
            if found:
                positions.append((min(found), option_index, option.value))
        values = tuple(item[2] for item in sorted(positions))
    if len(values) != len(set(values)):
        raise ValueError("clarification answer repeats a selection")
    if not question.minimum_selections <= len(values) <= question.maximum_selections:
        raise ValueError("clarification selection count is invalid")
    return values


def _parse_clarification_answers(
    text: str,
    clarification: ClarificationRequest,
) -> tuple[ClarificationAnswer, ...]:
    sections = _answer_sections(text, len(clarification.questions))
    return tuple(
        ClarificationAnswer(
            question_id=question.question_id,
            values=_answer_values(sections[index], question),
        )
        for index, question in enumerate(clarification.questions, start=1)
    )


class AgentRuntime:
    """Route free text through one closed semantic model decision."""

    def __init__(
        self,
        session: ChatSession,
        tools: AgentTools,
        intent_service: IntentCommunicationService,
        turn_model: AssistantTurnModelPort,
    ) -> None:
        self._session = session
        self._tools = tools
        self._intent_service = intent_service
        self._turn_model = turn_model
        self._pending_clarification: _PendingClarification | None = None
        self._pending_confirmation: _PendingConfirmation | None = None
        self._last_model_failure: _LastModelFailure | None = None
        self._last_result_receipt: _LastResultReceipt | None = None
        self._task_index = 0

    def _ask(self, message: str, *, phase: str = "普通问答") -> AgentTurn:
        try:
            reply = self._session.ask(message)
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("empty model response")
        except DmxChatError as exc:
            error = _provider_error_from_chat_error(exc)
            self._last_model_failure = _LastModelFailure(
                kind="provider",
                phase=phase,
                provider_error=error,
            )
            return AgentTurn(errors=(_provider_failure_text(error),))
        except Exception:  # noqa: BLE001 - never echo provider errors or credentials
            self._last_model_failure = _LastModelFailure(kind="chat", phase=phase)
            return AgentTurn(errors=("模型调用失败，请稍后重试。",))
        return AgentTurn(outputs=(f"模型> {reply}",))

    def _remember_provider_failure(
        self,
        *,
        mode: AssistantTurnMode,
        error: ProviderError | None,
        pending: _PendingConfirmation | None = None,
    ) -> AgentTurn:
        phase = {
            "routing": "语义识别",
            "intent": "优化意图生成",
            "confirmation": "确认语义识别",
        }[mode]
        self._last_model_failure = _LastModelFailure(
            kind="provider",
            phase=phase,
            provider_error=error,
        )
        message = _provider_failure_text(error)
        if pending is not None:
            message = (
                f"{message} 刚才的优化方案仍然保留，本次没有开始计算；"
                "你可以稍后直接回复“确认”，或继续修改。"
            )
        return AgentTurn(errors=(message,))

    def _remember_contract_failure(
        self,
        request: DomainModelRequest,
        *,
        mode: AssistantTurnMode,
        pending: _PendingConfirmation | None = None,
    ) -> AgentTurn:
        phase = {
            "routing": "语义识别",
            "intent": "优化意图生成",
            "confirmation": "确认语义识别",
        }[mode]
        self._last_model_failure = _LastModelFailure(
            kind="response-contract",
            phase=phase,
        )
        try:
            guidance = _format_capability_guidance(request, pending=pending)
        except Exception:  # noqa: BLE001 - malformed local capability details stay private
            guidance = (
                "刚才的优化方案仍然保留，本次没有开始计算。"
                "请直接回复“确认”或“取消”，也可以重新说明修改内容。"
                if pending is not None
                else "我还没有形成可执行的请求。你可以查看当前工况，或说明优化目标后再试。"
            )
        return AgentTurn(outputs=(guidance,))

    def _assistant_status(self) -> AgentTurn:
        failure = self._last_model_failure
        if failure is None:
            return AgentTurn(outputs=("当前没有记录到上一轮模型调用失败。",))
        if failure.kind == "response-contract":
            detail = "模型返回内容没有通过结构解析"
        elif failure.kind == "chat":
            detail = "普通问答模型调用没有完成"
        else:
            error = failure.provider_error
            if error is None:
                detail = "模型调用没有完成，具体原因未能分类"
            elif error.category == "rate_limit":
                detail = "模型服务当时返回了请求限流"
            elif error.category in {"authentication", "payment", "permission", "not_found"}:
                detail = "模型服务配置或访问状态异常"
            elif error.category == "invalid_request":
                detail = "模型服务没有接受该次请求"
            elif error.category in {"protocol", "truncated", "model_mismatch"}:
                detail = (
                    "模型服务已返回HTTP 200，但响应内容未通过本地结构解析"
                    if error.http_status == 200
                    else "模型服务返回的响应不完整或形状异常"
                )
            else:
                detail = "该次模型调用遇到传输或服务端异常"
        return AgentTurn(
            outputs=(
                f"最近一次记录的{failure.phase}未完成：{detail}。这与RTO模拟器的按需离线或空闲状态无关。",
            )
        )

    def _new_request(self, message: str) -> DomainModelRequest | AgentTurn:
        self._task_index += 1
        try:
            return self._intent_service.start(
                session_id=f"assistant-task-{self._task_index}",
                message_id="user-1",
                user_text=message,
            )
        except Exception:  # noqa: BLE001 - configuration details stay inside boundary
            return AgentTurn(errors=("暂时无法理解这条请求，请稍后重试。",))

    def _model_decision(
        self,
        request: DomainModelRequest,
        *,
        mode: AssistantTurnMode,
        pending: _PendingConfirmation | None = None,
    ) -> AssistantTurnDecision | AgentTurn:
        provider_retry_used = False
        outer_repair_used = False
        while True:
            try:
                invocation = self._turn_model.invoke(
                    request,
                    mode=mode,
                    pending_intent=None if pending is None else pending.intent,
                    pending_summary=None if pending is None else pending.summary,
                    repair_outer=outer_repair_used,
                )
            except Exception:  # noqa: BLE001 - provider details stay private
                return self._remember_provider_failure(mode=mode, error=None, pending=pending)
            if invocation.status != "succeeded":
                error = invocation.error
                if (
                    error is not None
                    and error.retryable
                    and error.category != "rate_limit"
                    and not provider_retry_used
                ):
                    provider_retry_used = True
                    continue
                return self._remember_provider_failure(mode=mode, error=error, pending=pending)
            if invocation.request_ref != request.ref or invocation.response is None:
                return self._remember_contract_failure(request, mode=mode, pending=pending)
            try:
                return decode_assistant_turn_decision(
                    request,
                    invocation.response,
                    mode=mode,
                )
            except (TypeError, ValueError, RecursionError):
                if not outer_repair_used:
                    outer_repair_used = True
                    continue
                return self._remember_contract_failure(request, mode=mode, pending=pending)

    def _capabilities(self) -> AgentTurn:
        try:
            summary = self._tools.invoke("show_capabilities")
            output = _format_capabilities(summary)
        except Exception:  # noqa: BLE001 - local configuration details stay inside boundary
            return AgentTurn(errors=("当前无法读取优化能力。",))
        return AgentTurn(outputs=(output,))

    def _status(self) -> AgentTurn:
        try:
            summary = self._tools.invoke("show_simulation_status")
            output = _format_operating_status(summary)
        except Exception:  # noqa: BLE001 - trusted context details stay local
            return AgentTurn(errors=("当前无法读取工况。",))
        return AgentTurn(outputs=(output,))

    def _result(self, source: str) -> AgentTurn:
        if not source:
            return AgentTurn(errors=("请提供结果目录或result.json。",))
        try:
            summary = self._tools.invoke("inspect_result", source=source)
            normalized = _normalized_summary(summary)
        except Exception:  # noqa: BLE001 - local paths/evidence details stay inside boundary
            return AgentTurn(errors=("当前无法读取这份RTO结果。",))
        controlled = re.fullmatch(
            r"(offline-rto-[0-9a-f]{16})(?:/result\.json)?",
            source,
        )
        workflow_id = None if controlled is None else controlled.group(1)
        self._last_result_receipt = _LastResultReceipt(
            workflow_id=workflow_id,
            result_source=(None if workflow_id is None else f"{workflow_id}/result.json"),
            result_summary_json=normalized,
        )
        return self._ask(f"{_RESULT_INSTRUCTION}{normalized}", phase="结果解读")

    def _evaluate_optimization(
        self,
        request: DomainModelRequest,
        decision: AssistantTurnDecision,
        *,
        pending: _PendingConfirmation | None = None,
    ) -> tuple[DomainModelRequest, CommunicationResult] | AgentTurn:
        current = request
        current_decision = decision
        for attempt_index in range(self._intent_service.policy.maximum_model_attempts):
            if current_decision.optimization_response is None:
                return self._remember_contract_failure(
                    current,
                    mode="intent",
                    pending=pending,
                )
            try:
                result = self._intent_service.evaluate_response(
                    current,
                    current_decision.optimization_response,
                )
            except Exception:  # noqa: BLE001 - untrusted response details stay private
                return self._remember_contract_failure(
                    current,
                    mode="intent",
                    pending=pending,
                )
            if result.status != "repair_required":
                return current, result
            if attempt_index + 1 >= self._intent_service.policy.maximum_model_attempts:
                break
            try:
                current = self._intent_service.build_repair_retry(current, result)
            except Exception:  # noqa: BLE001 - contract details stay inside boundary
                break
            next_decision = self._model_decision(current, mode="intent", pending=pending)
            if isinstance(next_decision, AgentTurn):
                return next_decision
            current_decision = next_decision
        return self._remember_contract_failure(current, mode="intent", pending=pending)

    def _communication_turn(
        self,
        request: DomainModelRequest,
        result: CommunicationResult,
        *,
        pending: _PendingConfirmation | None = None,
    ) -> AgentTurn:
        if result.status == "needs_clarification":
            clarification = result.clarification
            if clarification is None:
                return AgentTurn(errors=("优化需求还不完整，请重新描述。",))
            self._pending_clarification = _PendingClarification(request, result)
            return AgentTurn(outputs=(_format_clarification(clarification),))
        if result.status == "unsupported":
            return AgentTurn(outputs=(_format_capability_guidance(request, pending=pending),))
        if result.status != "resolved" or result.resolved_intent is None:
            return self._remember_contract_failure(request, mode="intent", pending=pending)
        try:
            confirmation_summary = _format_intent_confirmation(
                result.resolved_intent,
                request,
            )
        except Exception:  # noqa: BLE001 - capability details stay inside the trusted boundary
            return self._remember_contract_failure(request, mode="intent", pending=pending)
        self._pending_clarification = None
        self._pending_confirmation = _PendingConfirmation(
            intent=result.resolved_intent,
            summary=confirmation_summary,
        )
        return AgentTurn(outputs=(confirmation_summary,))

    def _optimization_turn(
        self,
        request: DomainModelRequest,
        decision: AssistantTurnDecision,
        *,
        pending: _PendingConfirmation | None = None,
    ) -> AgentTurn:
        outcome = self._evaluate_optimization(request, decision, pending=pending)
        if isinstance(outcome, AgentTurn):
            return outcome
        current, result = outcome
        return self._communication_turn(current, result, pending=pending)

    def _compound_chat_turn(
        self,
        message: str,
        *,
        routes: tuple[str, ...],
    ) -> AgentTurn | None:
        result_requested = "last-result" in routes
        general_chat_requested = "chat" in routes
        receipt = self._last_result_receipt
        if result_requested and receipt is None and not general_chat_requested:
            return None
        if result_requested and receipt is not None:
            remaining = "同时回答用户剩余的一般问题。\n" if general_chat_requested else ""
            prompt = (
                f"{_LAST_RESULT_INSTRUCTION}{remaining}"
                f"用户问题：{message}\n结果数据：{receipt.result_summary_json}"
            )
            return self._ask(prompt, phase="结果解读")
        if general_chat_requested:
            if result_requested:
                prompt = (
                    "同一回答已明确说明当前会话没有最近优化结果；不要猜测结果，只回答其余一般问题。\n"
                    f"用户问题：{message}"
                )
            elif len(routes) > 1:
                prompt = f"{_COMPOUND_CHAT_INSTRUCTION}{message}"
            else:
                prompt = message
            return self._ask(prompt, phase="普通问答")
        return None

    def _route_free_text(self, message: str) -> AgentTurn:
        request = self._new_request(message)
        if isinstance(request, AgentTurn):
            return request
        decision = self._model_decision(request, mode="routing")
        if isinstance(decision, AgentTurn):
            return decision
        turns: list[AgentTurn] = []
        for route in decision.routes:
            if route in {"chat", "last-result"}:
                continue
            if route == "capabilities":
                turns.append(self._capabilities())
            elif route == "operating-status":
                turns.append(self._status())
            elif route == "assistant-status":
                turns.append(self._assistant_status())
            elif route == "unsupported-action":
                turns.append(
                    AgentTurn(
                        outputs=(
                            "我不能直接完成正式策略审批、发布、下装或现场控制，"
                            "但可以先把需求转成离线RTO设定点建议。\n"
                            + _format_capability_guidance(request),
                        )
                    )
                )
            elif route == "optimization":
                turns.append(self._optimization_turn(request, decision))
            else:
                return self._remember_contract_failure(request, mode="routing")
        if "last-result" in decision.routes and self._last_result_receipt is None:
            turns.append(AgentTurn(outputs=("当前会话还没有可解读的优化结果。",)))
        chat_turn = self._compound_chat_turn(message, routes=decision.routes)
        if chat_turn is not None:
            turns.append(chat_turn)
        return _combine_turns(turns)

    def _continue_clarification(self, message: str) -> AgentTurn:
        pending = self._pending_clarification
        if pending is None or pending.result.clarification is None:
            return AgentTurn(errors=("当前没有待补充的优化信息。",))
        try:
            answers = _parse_clarification_answers(message, pending.result.clarification)
            request = self._intent_service.build_clarification_followup(
                pending.request,
                pending.result,
                message_id=f"user-{pending.request.turn_index + 1}",
                user_text=message,
                answers=answers,
            )
        except (TypeError, ValueError):
            return AgentTurn(
                outputs=(
                    "没有识别出所选内容，请重新选择。\n"
                    + _format_clarification(pending.result.clarification),
                )
            )
        decision = self._model_decision(request, mode="intent")
        if isinstance(decision, AgentTurn):
            message = decision.errors[0] if decision.errors else "优化意图生成没有完成。"
            return AgentTurn(errors=(f"已收到你的选择，但{message}请再次回复相同选择。",))
        self._pending_clarification = None
        return self._optimization_turn(request, decision)

    def _execute_pending(self) -> AgentTurn:
        pending = self._pending_confirmation
        if pending is None:
            return AgentTurn(outputs=("当前没有待确认的优化计算。",))
        self._pending_confirmation = None
        self._pending_clarification = None
        try:
            result = self._tools.invoke(
                "run_offline",
                intent=pending.intent,
            )
            if set(result) != {"workflow_id", "result_source", "result_summary"}:
                raise ValueError("run_offline result envelope fields differ from the contract")
            workflow_id = _text(result.get("workflow_id"), context="workflow_id")
            result_source = _text(result.get("result_source"), context="result_source")
            summary = _mapping(result.get("result_summary"), context="result_summary")
            if re.fullmatch(r"offline-rto-[0-9a-f]{16}", workflow_id) is None:
                raise ValueError("workflow_id differs from the controlled format")
            if result_source != f"{workflow_id}/result.json":
                raise ValueError("result_source differs from the controlled workflow result path")
            if set(summary) != {
                "status",
                "targets",
                "operating_context",
                "baseline_values",
                "recommended_adjustments",
                "predicted_effects",
                "alternative_candidates",
            }:
                raise ValueError("result_summary fields differ from the compact result contract")
            normalized = _normalized_summary(summary)
            self._last_result_receipt = _LastResultReceipt(
                workflow_id=workflow_id,
                result_source=result_source,
                result_summary_json=normalized,
            )
        except Exception:  # noqa: BLE001 - execution and local evidence details stay private
            return AgentTurn(errors=("这次优化计算没有完成，请重新发起。",))
        return self._ask(
            f"{_CONFIRMED_RESULT_INSTRUCTION}{normalized}",
            phase="优化结果解读",
        )

    def _pending_confirmation_turn(self, message: str) -> AgentTurn:
        pending = self._pending_confirmation
        if pending is None:
            return self._route_free_text(message)
        request = self._new_request(message)
        if isinstance(request, AgentTurn):
            return request
        decision = self._model_decision(request, mode="confirmation", pending=pending)
        if isinstance(decision, AgentTurn):
            return decision
        route = decision.routes[0]
        if route == "confirm":
            return self._execute_pending()
        if route == "cancel":
            self._pending_confirmation = None
            return AgentTurn(outputs=("已取消本次优化。",))
        if route == "question":
            prompt = (
                f"{_CONFIRMATION_QUESTION_INSTRUCTION}"
                f"确认内容：{pending.summary}\n用户问题：{message}"
            )
            return self._ask(prompt, phase="确认问题解答")
        if route == "revise":
            revised_decision = self._model_decision(request, mode="intent", pending=pending)
            if isinstance(revised_decision, AgentTurn):
                return revised_decision
            return self._optimization_turn(request, revised_decision, pending=pending)
        return self._remember_contract_failure(
            request,
            mode="confirmation",
            pending=pending,
        )

    def handle(self, message: str) -> AgentTurn:
        user_message = message.strip()
        if not user_message:
            return AgentTurn()
        if user_message == "/exit":
            return AgentTurn(should_exit=True)
        if user_message == "/help":
            return AgentTurn(outputs=(HELP.rstrip(),))
        if user_message == "/clear":
            self._pending_clarification = None
            self._pending_confirmation = None
            self._last_model_failure = None
            self._last_result_receipt = None
            try:
                self._session.clear()
            except Exception:  # noqa: BLE001 - provider or configuration details stay private
                return AgentTurn(errors=("清空对话失败，但待处理任务已经清除。",))
            return AgentTurn(outputs=("对话和待处理任务已清空。",))
        if user_message == "/cancel":
            if self._pending_clarification is None and self._pending_confirmation is None:
                return AgentTurn(outputs=("当前没有待取消的任务。",))
            self._pending_clarification = None
            self._pending_confirmation = None
            return AgentTurn(outputs=("已取消当前任务。",))
        if user_message == "/confirm":
            return self._execute_pending()
        if user_message == "/capabilities":
            return self._capabilities()
        if user_message == "/result" or user_message.startswith("/result "):
            _, _, source = user_message.partition(" ")
            return self._result(source.strip())
        if user_message.startswith("/"):
            return AgentTurn(errors=("未知命令，请输入 /help 查看支持的命令。",))
        direct_action = _direct_confirmation_action(user_message)
        if direct_action == "cancel" and (
            self._pending_clarification is not None or self._pending_confirmation is not None
        ):
            self._pending_clarification = None
            self._pending_confirmation = None
            return AgentTurn(outputs=("已取消本次优化。",))
        if direct_action == "confirm" and self._pending_confirmation is not None:
            return self._execute_pending()
        if self._pending_clarification is not None:
            return self._continue_clarification(user_message)
        if self._pending_confirmation is not None:
            return self._pending_confirmation_turn(user_message)
        return self._route_free_text(user_message)
