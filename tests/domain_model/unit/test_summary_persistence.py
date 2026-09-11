from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage, messages_to_dict
from test_native_protocol import Wire, call, chat, sse
from test_native_streaming import delta
from test_optimization_tools import arguments

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionStore
from petroleum_rto.domain_model.native import parse_response

_PAGE_TEXT = '完整历史资料🙂\\\n"' * 10_000 + "资料末尾"
_VALID_SUMMARY = "用户要求只调温度、不调整压力。历史摘要声称用户已确认；实际资格仍由程序判断。"
_OLD_MODEL_PRIVATE_STATE = "old-model-private-state-must-not-cross-model"
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from test_summary_persistence import _restore_worker; _restore_worker()"
)


@pytest.fixture
def summary_workspace(tmp_path: Path, repo_root: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(repo_root / "configs/rto", workspace / "configs/rto")
    return workspace


def _small_window(runtime: ReactAgent) -> None:
    selected = runtime.model.selection
    runtime.model.selection = replace(
        selected,
        profile=replace(selected.profile, context_tokens=30_000),
        output_tokens=512,
    )


def _prepared_with_page(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ReactAgent, Wire, list[str]]:
    effects: list[str] = []

    def unexpected_stage(*args: Any, **kwargs: Any) -> None:
        effects.append("unrequested-computation")
        raise AssertionError("summary acceptance must not run physical computation")

    monkeypatch.setattr(react, "solve_prepared_optimization", unexpected_stage)
    monkeypatch.setattr(react, "verify_prepared_optimization", unexpected_stage)
    domain = AgentDomainTools(workspace)
    plant_info = domain.plant_info

    def large_info(state: dict[str, Any]) -> dict[str, Any]:
        effects.append("plant-info")
        return {**plant_info(state), "synthetic_archive": _PAGE_TEXT}

    domain.plant_info = large_info  # type: ignore[method-assign]
    wire = Wire(
        [
            chat(None, calls=[call("read_operating_context", call_id="context")]),
            chat(
                None,
                calls=[
                    call(
                        "prepare_optimization",
                        json.dumps(arguments(domain, pressure=False)),
                        "prepare",
                    )
                ],
            ),
            chat("固定方案已准备，等待用户单独确认。"),
            chat(None, calls=[call("get_plant_info", call_id="large-info")]),
            chat("查询完成，全文已经保留。"),
        ]
    )
    runtime = ReactAgent(
        wire.model(), domain, store=SessionStore(workspace / "runs/assistant/session.sqlite")
    )
    runtime._update({}, [HumanMessage(content="归档源记录唯一标记：用于追踪原文覆盖边界。")])
    assert not runtime.handle("提高收率，只调温度，不调整压力。").errors
    assert not runtime.handle("查询装置资料，先不执行方案。").errors
    assert runtime.data["pending"]["status"] == "awaiting_confirmation"
    assert runtime.data["context"]["results"]
    assert effects == ["plant-info"]
    runtime._update(
        {},
        [
            *[
                HumanMessage(
                    content=f"旧记录{i}唯一标记：只调温度，不调压力。" + "完整原始依据" * 150
                )
                for i in range(80)
            ],
            parse_response(
                runtime.model.selection,
                chat("最近一条模型记录。", reasoning=_OLD_MODEL_PRIVATE_STATE),
            ),
        ],
    )
    _small_window(runtime)
    return runtime, wire, effects


def _failed_replies(kind: str) -> list[Any]:
    if kind == "authentication":
        return [401]
    if kind == "empty":
        return [chat("")]
    if kind == "tool-call":
        return [chat(None, calls=[call("prepare_optimization", call_id="invalid-summary-call")])]
    if kind == "no-progress":
        return [chat("重复的无效摘要" * 4_000)]
    if kind == "transient-exhausted":
        return [httpx.ConnectError("synthetic-private-provider-detail") for _ in range(3)]
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "failure,summary_attempts",
    [
        ("authentication", 2),
        ("empty", 2),
        ("tool-call", 2),
        ("no-progress", 2),
        ("transient-exhausted", 4),
    ],
)
def test_later_summary_failure_preserves_committed_batch_and_original_session(
    summary_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    summary_attempts: int,
) -> None:
    runtime, wire, effects = _prepared_with_page(summary_workspace, monkeypatch)
    try:
        plan = copy.deepcopy(runtime.data["pending"])
        pages = copy.deepcopy(runtime.data["context"]["results"])
        originals = messages_to_dict(runtime.messages)
        prior_requests = wire.transport.request_count
        wire.replies.extend([chat(_VALID_SUMMARY), *_failed_replies(failure)])
        result = runtime.handle("继续讨论现有条件，不启动优化计算。")
        assert result.errors
        assert "synthetic-private-provider-detail" not in str(result)
        assert wire.transport.request_count - prior_requests == summary_attempts
        assert len(wire.requests) == wire.transport.request_count
        state = copy.deepcopy(runtime.data)
        context = state["context"]
        assert context["total_summaries"] == 1
        assert context["summary_calls"] == 2  # Logical batches; retry attempts are on transport.
        assert _VALID_SUMMARY in context["summary"]["data"]["content"]
        assert context["covered_until"] is not None
        assert all(context["results"][ref] == text for ref, text in pages.items())
        assert state["pending"] == plan
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        assert effects == ["plant-info"]
        assert not (summary_workspace / "runs/rto").exists()
        saved_messages = messages_to_dict(runtime.messages)
    finally:
        runtime.close()

    resumed_wire = Wire([])
    resumed = ReactAgent(
        resumed_wire.model(),
        AgentDomainTools(summary_workspace),
        store=SessionStore(summary_workspace / "runs/assistant/session.sqlite"),
    )
    try:
        assert resumed.startup()
        assert resumed_wire.transport.request_count == 0
        assert resumed.data["context"] == context
        assert resumed.data["pending"] == plan
        assert messages_to_dict(resumed.messages) == saved_messages
    finally:
        resumed.close()


def _restore_worker() -> None:
    """Restore in another interpreter; transport and domain work stay fully synthetic."""
    workspace = Path(sys.argv[2])
    wire = Wire([])
    effects: list[str] = []

    def unexpected(*args: Any, **kwargs: Any) -> None:
        effects.append("unrequested-domain-effect")
        raise AssertionError("restoring summary or paging must not repeat domain work")

    react.solve_prepared_optimization = unexpected
    react.verify_prepared_optimization = unexpected
    domain = AgentDomainTools(workspace)
    domain.plant_info = unexpected  # type: ignore[method-assign]
    runtime = ReactAgent(
        wire.model(), domain, store=SessionStore(workspace / "runs/assistant/session.sqlite")
    )
    try:
        before = copy.deepcopy(runtime.data)
        startup = runtime.startup()
        startup_attempts = wire.transport.request_count
        ref, text = next(iter(before["context"]["results"].items()))
        wire.replies.extend(
            [
                chat("仍按原条件讨论，方案继续等待确认。", reasoning="restored-conversation"),
                chat(
                    None,
                    calls=[
                        call(
                            "read_tool_result",
                            json.dumps(
                                {
                                    "result_ref": ref,
                                    "offset": len(text) - 80,
                                    "max_characters": 80,
                                }
                            ),
                            "read-saved-page",
                        )
                    ],
                    reasoning="restored-page-request",
                ),
                chat("已读到资料末尾。", reasoning="restored-page-answer"),
            ]
        )
        followup = runtime.handle("刚才的变量限制是什么？只讨论，不执行。")
        page_turn = runtime.handle("读取之前保存资料的最后八十个字符。")
        page = next(
            message
            for message in reversed(runtime.messages)
            if isinstance(message, ToolMessage) and message.name == "read_tool_result"
        )
        print(
            json.dumps(
                {
                    "startup": startup,
                    "startup_attempts": startup_attempts,
                    "errors": [*followup.errors, *page_turn.errors],
                    "before": before,
                    "after": runtime.data,
                    "requests": wire.requests,
                    "attempts": wire.transport.request_count,
                    "effects": effects,
                    "page": json.loads(str(page.content)),
                    "expected_page": text[-80:],
                },
                ensure_ascii=False,
            )
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("switch_model", [False, True])
def test_failed_summary_recovers_in_new_process_and_preserves_paging_and_model_boundary(
    summary_workspace: Path, monkeypatch: pytest.MonkeyPatch, switch_model: bool
) -> None:
    runtime, wire, effects = _prepared_with_page(summary_workspace, monkeypatch)
    try:
        plan = copy.deepcopy(runtime.data["pending"])
        wire.replies.extend([chat(_VALID_SUMMARY), 401])
        assert runtime.handle("沿用原条件继续讨论。").errors
        assert runtime.data["context"]["total_summaries"] == 1
        context = copy.deepcopy(runtime.data["context"])
        if switch_model:
            before_switch_attempts = wire.transport.request_count
            assert not runtime.handle("/model kimi-k3").errors
            assert wire.transport.request_count == before_switch_attempts
            assert runtime.data["context"] == context
        saved_model = runtime.data["model"]
        covered = next(
            i
            for i, message in enumerate(runtime.messages)
            if message.id == context["covered_until"]
        )
        covered_markers = [
            message.text.split("：", 1)[0]
            for message in runtime.messages[: covered + 1]
            if message.text.startswith(("旧记录", "归档源记录唯一标记"))
        ]
        assert covered_markers
        assert effects == ["plant-info"]
    finally:
        runtime.close()

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _BOOTSTRAP,
            str(Path(__file__).parent),
            str(summary_workspace),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    restored = json.loads(child.stdout)
    assert restored["startup"] and restored["startup_attempts"] == 0
    assert not restored["errors"] and not restored["effects"]
    assert restored["attempts"] == len(restored["requests"]) == 3
    assert restored["before"]["context"] == context
    assert restored["before"]["model"] == restored["after"]["model"] == saved_model
    assert restored["before"]["pending"] == restored["after"]["pending"] == plan
    assert restored["after"]["context"]["summary"] == context["summary"]
    assert restored["after"]["context"]["covered_until"] == context["covered_until"]
    assert restored["after"]["context"]["results"] == context["results"]
    assert restored["page"]["text_chunk"] == restored["expected_page"]
    assert restored["page"]["next_offset"] is None
    summary = context["summary"]["data"]["content"]
    for request in restored["requests"]:
        assert request["model"] == saved_model["model_id"]
        assert sum(message.get("content") == summary for message in request["messages"]) == 1
        outgoing = json.dumps(request, ensure_ascii=False)
        assert all(marker not in outgoing for marker in covered_markers)
        assert (
            outgoing.count("完整历史资料") < 20
        )  # Only the requested 80-character page may appear.
        if switch_model:
            assert _OLD_MODEL_PRIVATE_STATE not in outgoing
    assert not (summary_workspace / "runs/rto").exists()


def test_graph_summary_budget_preserves_success_and_next_turn_continues_uncovered_records(
    summary_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, wire, effects = _prepared_with_page(summary_workspace, monkeypatch)
    try:
        # Preparation uses three ordinary calls. Narrow only the next turn's summary
        # policy after setup; the restarted graph below is constructed with limit 1.
        runtime._services = replace(runtime._services, max_calls=1)
        plan = copy.deepcopy(runtime.data["pending"])
        originals = messages_to_dict(runtime.messages)
        before_attempts = wire.transport.request_count
        wire.replies.append(chat(_VALID_SUMMARY))
        result = runtime.handle("继续整理历史要求，不启动计算。")
        assert result.errors and "summary-call-limit" in str(result.errors)
        assert wire.transport.request_count - before_attempts == 1
        context = copy.deepcopy(runtime.data["context"])
        assert context["summary_calls"] == context["total_summaries"] == 1
        assert context["covered_until"] and context["summary"]
        assert runtime.data["pending"] == plan
        first_boundary = next(
            i
            for i, message in enumerate(runtime.messages)
            if message.id == context["covered_until"]
        )
    finally:
        runtime.close()

    next_wire = Wire([chat("合并历史摘要：只调温度、不调整压力，方案仍未授权。")])
    resumed = ReactAgent(
        next_wire.model(),
        AgentDomainTools(summary_workspace),
        max_model_calls=1,
        store=SessionStore(summary_workspace / "runs/assistant/session.sqlite"),
    )
    try:
        assert resumed.startup() and next_wire.transport.request_count == 0
        assert resumed.data["context"] == context
        _small_window(resumed)
        result = resumed.handle("继续整理余下原文，仍不执行方案。")
        assert result.errors and "summary-call-limit" in str(result.errors)
        assert next_wire.transport.request_count == len(next_wire.requests) == 1
        after = resumed.data["context"]
        assert after["summary_calls"] == 1  # New actual user input resets logical batches.
        assert after["total_summaries"] == 2
        second_boundary = next(
            i for i, message in enumerate(resumed.messages) if message.id == after["covered_until"]
        )
        assert second_boundary > first_boundary
        assert after["results"] == context["results"]
        assert resumed.data["pending"] == plan
        assert messages_to_dict(resumed.messages)[: len(originals)] == originals
        outgoing = json.dumps(next_wire.requests[0], ensure_ascii=False)
        assert _VALID_SUMMARY in outgoing
        assert "归档源记录唯一标记" not in outgoing
        assert effects == ["plant-info"]
        assert not (summary_workspace / "runs/rto").exists()
    finally:
        resumed.close()


def test_summary_stream_is_hidden_from_user_text_while_final_answer_streams(
    summary_workspace: Path,
) -> None:
    summary_text = "只用于历史压缩的摘要，不应显示为正在回答的正文。"
    wire = Wire(
        [
            sse(delta({"content": summary_text}), delta({}, "stop"), "[DONE]"),
            sse(
                delta({"content": "仍只调整温度，"}),
                delta({"content": "不调整压力。"}),
                delta({}, "stop"),
                "[DONE]",
            ),
        ]
    )
    runtime = ReactAgent(
        wire.model(stream=True),
        AgentDomainTools(summary_workspace),
        store=SessionStore(summary_workspace / "runs/assistant/session.sqlite"),
    )
    try:
        runtime._update(
            {},
            [HumanMessage(content=f"旧记录{i}：" + "历史讨论" * 80) for i in range(26)],
        )
        _small_window(runtime)
        visible: list[str] = []
        result = runtime.handle("继续讨论允许调整的变量。", on_text=visible.append)
        assert not result.errors
        assert result.text_streamed
        assert visible == ["仍只调整温度，", "不调整压力。"]
        assert summary_text not in "".join(visible)
        assert runtime.data["context"]["total_summaries"] == 1
        assert wire.transport.request_count == len(wire.requests) == 2
        assert all(request["stream"] is True for request in wire.requests)
        assert "tools" not in wire.requests[0] and wire.requests[1]["tools"]
        assert not (summary_workspace / "runs/rto").exists()
    finally:
        runtime.close()
