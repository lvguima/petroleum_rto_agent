from __future__ import annotations

import copy
import json
import shutil
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict
from test_native_protocol import Wire, call, chat
from test_optimization_tools import prepared

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionStore

_PRIVATE_DETAIL = "synthetic-private-upstream-detail"


@pytest.fixture(autouse=True)
def no_delay_or_computation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Exercise the actual retry middleware and HTTP attempts without waiting for
    # backoff wall time. Summary RunnableRetry uses the same standard sleep boundary.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    computations: list[str] = []

    def unexpected(*args: Any, **kwargs: Any) -> None:
        computations.append("unrequested-computation")
        raise AssertionError("model retry acceptance cannot run physical computation")

    monkeypatch.setattr(react, "solve_prepared_optimization", unexpected)
    monkeypatch.setattr(react, "verify_prepared_optimization", unexpected)
    yield
    assert not computations


@pytest.fixture
def retry_workspace(tmp_path: Path, repo_root: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(repo_root / "configs/rto", workspace / "configs/rto")
    return workspace


def test_two_transient_failures_then_success_repeat_only_the_same_model_request(
    retry_workspace: Path,
) -> None:
    wire = Wire([httpx.ConnectError(_PRIVATE_DETAIL), 429, chat("第三次请求已完成。")])
    runtime = ReactAgent(wire.model(), AgentDomainTools(retry_workspace), max_model_calls=1)
    try:
        result = runtime.handle("请回答一个合成测试问题。")
        assert not result.errors
        assert "第三次请求已完成。" in str(result.outputs)
        assert _PRIVATE_DETAIL not in str(result)
        assert wire.transport.request_count == len(wire.requests) == 3
        assert wire.requests[0] == wire.requests[1] == wire.requests[2]
        assert {request["model"] for request in wire.requests} == {
            runtime.model.selection.profile.model_id
        }
        assert sum(isinstance(message, AIMessage) for message in runtime.messages) == 1
        assert runtime.data["context"]["summary_calls"] == 0
        assert not (retry_workspace / "runs/rto").exists()
    finally:
        runtime.close()


def test_three_transient_failures_end_with_error_without_a_fabricated_model_answer(
    retry_workspace: Path,
) -> None:
    wire = Wire([503, httpx.ReadTimeout(_PRIVATE_DETAIL), 429, chat("必须保留而非第四次请求")])
    runtime = ReactAgent(wire.model(), AgentDomainTools(retry_workspace), max_model_calls=1)
    try:
        result = runtime.handle("请处理合成测试问题。")
        assert result.errors
        assert _PRIVATE_DETAIL not in str(result) and "fake-private-api-key" not in str(result)
        assert wire.transport.request_count == len(wire.requests) == 3
        assert len(wire.replies) == 1
        assert not any(isinstance(message, AIMessage) for message in runtime.messages)
        assert any(
            isinstance(message, HumanMessage) and message.text == "请处理合成测试问题。"
            for message in runtime.messages
        )
        assert not any(output.startswith("模型> ") for output in result.outputs)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "failure,code",
    [
        (401, "authentication-failed"),
        (403, "permission-denied"),
        (400, "http-error"),
        ({"choices": "invalid-shape"}, "invalid-response"),
        (chat(""), "empty-response"),
        (chat("fake-private-api-key"), "credential-in-response"),
    ],
)
def test_permanent_failure_is_not_retried(retry_workspace: Path, failure: Any, code: str) -> None:
    wire = Wire([failure, chat("永久错误后不得发送这个请求")])
    runtime = ReactAgent(wire.model(), AgentDomainTools(retry_workspace))
    try:
        result = runtime.handle("仅作合成接口测试。")
        assert result.errors and code in str(result.errors)
        assert "fake-private-api-key" not in str(result)
        assert wire.transport.request_count == len(wire.requests) == 1
        assert len(wire.replies) == 1
        assert not any(isinstance(message, AIMessage) for message in runtime.messages)
    finally:
        runtime.close()


@pytest.mark.parametrize("reason", ["capacity", "credential"])
def test_local_rejection_does_not_consume_any_http_attempt(
    retry_workspace: Path, reason: str
) -> None:
    wire = Wire([chat("本地拒绝时不应发送")])
    runtime = ReactAgent(wire.model(), AgentDomainTools(retry_workspace))
    try:
        if reason == "capacity":
            selected = runtime.model.selection
            runtime.model.selection = replace(
                selected,
                profile=replace(selected.profile, context_tokens=1_024),
                output_tokens=512,
            )
            text = "当前输入和必需说明必须完整保留。"
        else:
            text = "fake-private-api-key"
        result = runtime.handle(text)
        assert result.errors
        assert wire.transport.request_count == len(wire.requests) == 0
        assert len(wire.replies) == 1
        if reason == "capacity":
            assert "context-overflow" in str(result.errors)
            assert any(message.text == text for message in runtime.messages)
        else:
            assert not runtime.messages
    finally:
        runtime.close()


class InterruptWire(Wire):
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.replies and isinstance(self.replies[0], KeyboardInterrupt):
            self.requests.append(json.loads(request.content))
            self.paths.append(request.url.path)
            raise self.replies.pop(0)
        return super().handle(request)


def test_user_interrupt_is_not_retried(retry_workspace: Path) -> None:
    replies: list[Any] = [KeyboardInterrupt(_PRIVATE_DETAIL), chat("中止后不得继续发送")]
    wire = InterruptWire(replies)
    runtime = ReactAgent(wire.model(), AgentDomainTools(retry_workspace))
    try:
        result = runtime.handle("测试请求进行中用户中止。")
        assert result.errors and "中止" in str(result.errors)
        assert _PRIVATE_DETAIL not in str(result)
        assert wire.transport.request_count == len(wire.requests) == 1
        assert len(wire.replies) == 1
        assert not any(isinstance(message, AIMessage) for message in runtime.messages)
    finally:
        runtime.close()


@pytest.mark.parametrize("eventually_succeeds", [False, True])
def test_retry_after_completed_query_never_reexecutes_tool_or_changes_saved_plan(
    retry_workspace: Path, eventually_succeeds: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire([])
    session_path = retry_workspace / "runs/assistant/session.sqlite"
    queries: list[str] = []

    def query(_domain: AgentDomainTools, state: dict[str, Any]) -> dict[str, Any]:
        queries.append("plant-info")
        return {"source": "synthetic-query", "detail": "已查询的固定资料"}

    monkeypatch.setattr(AgentDomainTools, "plant_info", query)
    runtime = prepared(retry_workspace, wire=wire, store=SessionStore(session_path))
    try:
        plan = copy.deepcopy(runtime.data["pending"])
        originals = messages_to_dict(runtime.messages)
        prior_requests = wire.transport.request_count
        wire.replies.extend(
            [
                chat(None, calls=[call("get_plant_info", call_id="one-query")]),
                httpx.ConnectError(_PRIVATE_DETAIL),
                503,
                chat("已按查询结果完成回答，方案仍待确认。") if eventually_succeeds else 429,
            ]
        )
        result = runtime.handle("查询装置资料，继续等待方案审批。")
        assert bool(result.errors) is not eventually_succeeds
        assert wire.transport.request_count - prior_requests == 4
        assert queries == ["plant-info"]
        assert wire.requests[-1] == wire.requests[-2] == wire.requests[-3]
        for request in wire.requests[-3:]:
            assert (
                sum(
                    message.get("role") == "tool" and message.get("tool_call_id") == "one-query"
                    for message in request["messages"]
                )
                == 1
            )
        assert runtime.data["pending"] == plan
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        assert (
            sum(
                isinstance(message, ToolMessage) and message.tool_call_id == "one-query"
                for message in runtime.messages
            )
            == 1
        )
        saved_messages = messages_to_dict(runtime.messages)
        assert not (retry_workspace / "runs/rto").exists()
    finally:
        runtime.close()

    restored_wire = Wire([])
    restored = ReactAgent(
        restored_wire.model(), AgentDomainTools(retry_workspace), store=SessionStore(session_path)
    )
    try:
        assert restored.startup() and restored_wire.transport.request_count == 0
        assert restored.data["pending"] == plan
        assert messages_to_dict(restored.messages) == saved_messages
    finally:
        restored.close()


@pytest.mark.parametrize("logical_limit", [1, 2])
def test_agent_logical_limit_bounds_total_ordinary_http_attempts(
    retry_workspace: Path, logical_limit: int
) -> None:
    replies: list[Any] = []
    for index in range(logical_limit):
        replies.extend(
            [
                429,
                503,
                chat(None, calls=[call("get_plant_info", call_id=f"bounded-{index}")]),
            ]
        )
    replies.append(chat("超过逻辑调用上限时不得发送"))
    wire = Wire(replies)
    domain = AgentDomainTools(retry_workspace)
    queries: list[str] = []

    def query(state: dict[str, Any]) -> dict[str, Any]:
        queries.append("plant-info")
        return {"source": "synthetic-query"}

    domain.plant_info = query  # type: ignore[method-assign]
    runtime = ReactAgent(wire.model(), domain, max_model_calls=logical_limit)
    try:
        result = runtime.handle("完成有界查询测试。")
        assert result.errors and "上限" in str(result.errors)
        assert wire.transport.request_count == len(wire.requests) == 3 * logical_limit
        assert len(wire.replies) == 1
        assert len(queries) == logical_limit
        assert (
            sum(isinstance(message, ToolMessage) for message in runtime.messages) == logical_limit
        )
        assert runtime.data["context"]["summary_calls"] == 0
        assert not (retry_workspace / "runs/rto").exists()
    finally:
        runtime.close()


@pytest.mark.parametrize("summary_succeeds", [False, True])
def test_summary_retries_and_ordinary_retries_have_separate_nonmultiplying_budgets(
    retry_workspace: Path, summary_succeeds: bool
) -> None:
    replies: list[Any] = [429, httpx.ConnectError(_PRIVATE_DETAIL)]
    replies.append(chat("历史要求只调温度，不调压力。") if summary_succeeds else 503)
    if summary_succeeds:
        replies.extend([503, httpx.ReadTimeout(_PRIVATE_DETAIL), chat("仍保持原来的变量限制。")])
    replies.append(chat("摘要失败或达到上限后不得额外发送"))
    wire = Wire(replies)
    runtime = ReactAgent(wire.model(), AgentDomainTools(retry_workspace), max_model_calls=1)
    try:
        runtime._update(
            {}, [HumanMessage(content=f"旧记录{i}：" + "历史讨论" * 80) for i in range(26)]
        )
        originals = messages_to_dict(runtime.messages)
        selected = runtime.model.selection
        runtime.model.selection = replace(
            selected,
            profile=replace(selected.profile, context_tokens=30_000),
            output_tokens=512,
        )
        result = runtime.handle("继续讨论允许调整的变量。")
        assert bool(result.errors) is not summary_succeeds
        expected_attempts = 6 if summary_succeeds else 3
        assert wire.transport.request_count == len(wire.requests) == expected_attempts
        assert len(wire.replies) == 1
        assert all("tools" not in request for request in wire.requests[:3])
        assert wire.requests[0] == wire.requests[1] == wire.requests[2]
        if summary_succeeds:
            assert all(request["tools"] for request in wire.requests[3:])
            assert wire.requests[3] == wire.requests[4] == wire.requests[5]
        assert runtime.data["context"]["summary_calls"] == 1
        assert runtime.data["context"]["total_summaries"] == int(summary_succeeds)
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        assert not (retry_workspace / "runs/rto").exists()
    finally:
        runtime.close()
