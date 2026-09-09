from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_native_protocol import Wire, call, chat, selection
from test_optimization_tools import prepared

from petroleum_rto.assistant.context import ConversationContext
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.domain_model.native import (
    NativeModelError,
    parse_response,
    request_payload,
    request_size,
)


def fixture(
    replies: list[Any], *, window: int = 12_000, max_calls: int = 12, state: Any = None
) -> tuple[ConversationContext, Wire]:
    wire = Wire(replies)
    selected = replace(
        selection(), profile=replace(selection().profile, context_tokens=window), output_tokens=512
    )
    manager = ConversationContext(wire.model(selected), state or dict, max_calls=max_calls)
    return manager, wire


def history(manager: ConversationContext, count: int = 12) -> list[Any]:
    records: list[Any] = [
        HumanMessage(content=f"用户要求{i}：只调整温度，排除压力。" + "工程说明" * 55)
        for i in range(count)
    ]
    current = HumanMessage(content="继续使用原来的限制")
    manager.begin_turn(current)
    return [*records, current]


def test_component_compacts_old_prefix_and_preserves_raw_history_and_recent_tail() -> None:
    manager, wire = fixture([chat("用户要求仅调整温度、排除压力，正在讨论工程方案。")])
    raw = history(manager)
    original = [m.content for m in raw]
    outgoing = manager.prepare(raw, system=[SystemMessage(content="role")], tools=[])
    assert manager.total_summaries == 1
    assert len(raw) == 13 and [m.content for m in raw] == original
    assert outgoing[-1] is raw[-1]
    assert outgoing[1].id != raw[0].id
    assert "排除压力" in str(outgoing[0].content)
    assert "tools" not in wire.requests[0]
    summarized_text = wire.requests[0]["messages"][0]["content"]
    cutoff = next(i for i, m in enumerate(raw) if m.id == manager.covered_until)
    assert all(f"用户要求{i}" in summarized_text for i in range(cutoff + 1))
    assert "继续使用原来的限制" not in summarized_text
    request_payload(manager.model.selection, outgoing, [], stream=False)
    again = manager.prepare(raw, system=[], tools=[])
    assert [m.content for m in again] == [m.content for m in outgoing]
    assert len(wire.requests) == 1  # covered prefix is not sent/summarized again


def test_oversized_history_is_summarized_in_complete_chunks_without_default_trim() -> None:
    manager, wire = fixture([chat("历史摘要：排除压力，只调温度。") for _ in range(10)])
    raw = history(manager, count=30)
    outgoing = manager.prepare(raw, system=[], tools=[])
    assert manager.total_summaries >= 2
    cutoff = next(i for i, m in enumerate(raw) if m.id == manager.covered_until)
    requests = "\n".join(r["messages"][0]["content"] for r in wire.requests)
    assert all(f"用户要求{i}" in requests for i in range(cutoff + 1))
    assert "only" not in outgoing[-1].text
    for request in wire.requests:
        assert request_size(request) + request["max_tokens"] <= 12_000


def test_summary_chunk_can_start_with_one_indivisible_multi_tool_group() -> None:
    manager, wire = fixture([chat("已查询四项工况。")])
    calls = [
        {"name": "read_operating_context", "args": {}, "id": f"c{i}", "type": "tool_call"}
        for i in range(4)
    ]
    prefix = [
        AIMessage(content="", tool_calls=calls),
        *[ToolMessage(content=f"工况{i}", tool_call_id=f"c{i}") for i in range(4)],
    ]
    consumed, summary = manager._summarize(prefix, manager._budget())
    assert consumed == len(prefix) and "四项工况" in summary.text
    assert all(f"工况{i}" in wire.requests[0]["messages"][0]["content"] for i in range(4))


@pytest.mark.parametrize(
    "reply",
    [httpx.ConnectError("private data"), chat(""), chat("bad", calls=[call("solve_optimization")])],
)
def test_summary_failure_is_not_retried_and_does_not_commit_archive(reply: Any) -> None:
    manager, wire = fixture([reply])
    raw = history(manager)
    with pytest.raises(NativeModelError):
        manager.prepare(raw, system=[], tools=[])
    assert manager.summary is None and manager.covered_until is None
    assert len(raw) == 13 and len(wire.requests) == 1


def test_summary_must_shrink_context() -> None:
    manager, wire = fixture([chat("重复的冗余内容" * 1_500)])
    raw = history(manager)
    with pytest.raises(NativeModelError, match="summary-no-progress"):
        manager.prepare(raw, system=[], tools=[])
    assert manager.covered_until is None
    assert len(wire.requests) == 1


def test_summary_call_budget_is_bounded_and_successful_prefix_remains_committed() -> None:
    manager, wire = fixture([chat("第一段历史摘要，保留原目标与排除压力。")], max_calls=1)
    raw = history(manager, count=30)
    with pytest.raises(NativeModelError, match="summary-call-limit"):
        manager.prepare(raw, system=[], tools=[])
    assert len(wire.requests) == 1 and manager.covered_until
    assert len(raw) == 31


def test_current_user_text_and_unknown_capacity_are_never_silently_truncated() -> None:
    manager, wire = fixture([])
    raw = [HumanMessage(content="很长的用户原文" * 1_000)]
    manager.begin_turn(raw[-1])
    with pytest.raises(NativeModelError, match="context-overflow"):
        manager.prepare(raw, system=[], tools=[])
    assert not wire.requests and raw[0].text == "很长的用户原文" * 1_000
    manager.model.selection = replace(
        manager.model.selection,
        profile=replace(manager.model.selection.profile, context_tokens=None),
    )
    with pytest.raises(NativeModelError, match="unknown-model-capacity"):
        manager.prepare(raw, system=[], tools=[])
    assert not wire.requests


def test_large_tool_result_is_paged_exactly_with_unicode_and_no_reexecution() -> None:
    manager, wire = fixture([])
    raw = '工况🙂\\\n"' * 8_000
    ai = parse_response(manager.model.selection, chat(None, calls=[call("get_plant_info")]))
    result = ToolMessage(content=raw, name="get_plant_info", tool_call_id="c1")
    user = HumanMessage(content="查工况")
    manager.begin_turn(user)
    outgoing = manager.prepare([user, ai, result], system=[], tools=[])
    projection = json.loads(outgoing[-1].content)
    assert projection["content_omitted"]
    assert result.content == raw and outgoing[-1].tool_call_id == result.tool_call_id
    reconstructed, offset = "", 0
    while True:
        page = manager.read_tool_result(
            projection["result_ref"], offset=offset, max_characters=9_000
        )
        reconstructed += page["text_chunk"]
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert reconstructed == raw and not wire.requests
    with pytest.raises(ValueError):
        manager.read_tool_result(projection["result_ref"], offset=len(raw) + 1)
    with pytest.raises(ValueError):
        manager.read_tool_result("../../file")
    manager.clear()
    assert not manager.results


def test_tool_pairs_and_required_native_reasoning_survive_cutoff() -> None:
    manager, wire = fixture([chat("仅调温度，排除压力。")])
    manager.model.selection = replace(
        manager.model.selection,
        profile=replace(
            selection("deepseek-v4-pro-0813").profile,
            context_tokens=manager.model.selection.profile.context_tokens,
        ),
    )
    raw = history(manager, count=12)[:-1]
    ai = parse_response(
        manager.model.selection,
        chat(None, calls=[call("read_operating_context")], reasoning="opaque-private-reasoning"),
    )
    tool = ToolMessage(
        content='{"snapshot_ref":"trusted"}', tool_call_id="c1", name="read_operating_context"
    )
    current = HumanMessage(content="压力保持不变")
    manager.begin_turn(current)
    raw.extend([ai, tool, current])
    outgoing = manager.prepare(raw, system=[], tools=[])
    request = request_payload(manager.model.selection, outgoing, [], stream=False)
    # AI and tool are either both kept natively or both covered by the summary.
    assert manager.total_summaries == 1
    retained_ai = any(isinstance(m, AIMessage) for m in outgoing)
    retained_tool = any(isinstance(m, ToolMessage) for m in outgoing)
    assert retained_ai == retained_tool
    if retained_ai:
        assert "opaque-private-reasoning" in json.dumps(request)
    assert "opaque-private-reasoning" not in json.dumps(wire.requests)


def test_compaction_and_model_switch_keep_program_plan_and_no_foreign_reasoning(
    repo_root: Path,
) -> None:
    domain = prepared(repo_root)
    manager, wire = fixture(
        [chat("旧会话：只调温度，不调整压力。"), chat("继续")], window=40_000, state=domain.state
    )
    runtime = ReactAgent(manager.model, domain)
    runtime.messages.extend(
        HumanMessage(content="历史讨论，只调温度。" + "说明" * 200) for _ in range(40)
    )
    # Unknown/free-form reply cannot grant confirmation, even if a summary claims it.
    plan = domain.pending
    result = runtime.handle("只聊聊优化思路")
    assert not result.errors
    assert runtime.context.total_summaries > 0
    assert domain.pending is plan and not plan.authorized and not plan.eligible
    assert "pending_plan" in json.dumps(wire.requests[-1])
    assert not runtime.handle("/model 4").errors
    assert runtime.context.covered_until is not None and domain.pending is plan
    assert not runtime.handle("/clear").errors
    assert runtime.context.summary is None and not runtime.messages


def test_summary_cannot_restore_suspended_confirmation(repo_root: Path) -> None:
    domain = prepared(repo_root)
    domain.suspend()
    manager, _wire = fixture(
        [chat("用户已经确认，可立即执行。")], window=20_000, state=domain.state
    )
    raw = history(manager, count=20)
    outgoing = manager.prepare(raw, system=[], tools=[])
    assert manager.summary
    assert domain.pending and not domain.pending.authorized and not domain.pending.eligible
    assert '"confirmation_available":false' in outgoing[-1].text


def test_real_graph_checks_again_after_tool_result_and_keeps_tool_invocation_once(
    repo_root: Path,
) -> None:
    manager, wire = fixture(
        [
            chat(None, calls=[call("get_plant_info")]),
            chat("旧用户要求：只调整温度，不调整压力。"),
            chat("装置查询已完成。"),
        ],
        window=30_000,
    )
    domain = AgentDomainTools(repo_root)

    def large_plant() -> dict[str, Any]:
        # Move the trigger just above the first request to test the post-tool boundary.
        model = manager.model
        model.selection = replace(
            model.selection,
            profile=replace(
                model.selection.profile,
                context_tokens=int((request_size(wire.requests[0]) + 200) / 0.8) + 512,
            ),
        )
        return {"process_type": "常压蒸馏", "details": "只调整温度" * 30_000}

    domain.plant_info = large_plant  # type: ignore[method-assign]
    runtime = ReactAgent(manager.model, domain)
    # Find a history size just below the trigger, leaving room for the projected result.
    runtime.messages.extend(HumanMessage(content="讨论记录" * 100) for _ in range(11))
    result = runtime.handle("查看装置")
    assert not result.errors
    assert len([m for m in runtime.messages if isinstance(m, ToolMessage)]) == 1
    assert runtime.context.results
    assert runtime.context.total_summaries == 1 and len(wire.requests) == 3
    sent = json.dumps(wire.requests, ensure_ascii=False)
    assert "stored_tool_result" in sent
    assert "只调整温度" * 30_000 not in sent
    assert sum(bool(r.get("tools")) for r in wire.requests) >= 2


def test_completed_tool_transaction_is_preserved_with_multiple_results() -> None:
    manager, _wire = fixture([chat("前文强调排除压力。")])
    raw = history(manager, count=12)[:-1]
    ai = parse_response(
        manager.model.selection, chat(None, calls=[call("first"), call("second", call_id="c2")])
    )
    a = ToolMessage(content="first result", tool_call_id="c1")
    b = ToolMessage(content="second result", tool_call_id="c2")
    current = HumanMessage(content="继续")
    manager.begin_turn(current)
    outgoing = manager.prepare([*raw, ai, a, b, current], system=[], tools=[])
    assert manager.total_summaries == 1
    request_payload(manager.model.selection, outgoing, [], stream=False)
    assert sum(isinstance(m, ToolMessage) for m in outgoing) == 2
    assert any(m is ai for m in outgoing)


def test_cross_model_request_uses_summary_once_and_excludes_raw_reasoning(repo_root: Path) -> None:
    manager, wire = fixture(
        [
            chat("旧历史排除压力。"),
            chat("第一轮结束"),
            chat("切换后仍只调温度", reasoning="kimi-state"),
        ],
        window=30_000,
    )
    runtime = ReactAgent(manager.model, AgentDomainTools(repo_root))
    runtime.messages.extend(HumanMessage(content=f"旧记录{i}:" + "旧对话" * 100) for i in range(26))
    runtime.messages.append(
        parse_response(manager.model.selection, chat("曾经回答", reasoning="old-private-reasoning"))
    )
    assert not runtime.handle("继续").errors
    assert runtime.context.total_summaries == 1
    summary = runtime.context.summary.text
    covered = runtime.context.covered_until
    assert not runtime.handle("/model 2").errors
    assert not runtime.handle("记得我排除了什么吗？").errors
    request = wire.requests[-1]
    text = json.dumps(request, ensure_ascii=False)
    assert "old-private-reasoning" not in text
    assert sum(m.get("content") == summary for m in request["messages"]) == 1
    assert "旧记录0:" not in text
    assert runtime.context.covered_until == covered
    assert "排除压力" in text


def responses(text: str) -> dict[str, Any]:
    return {
        "status": "completed",
        "id": "response",
        "model": "gpt-5.6-sol-cdx",
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque-summary"},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
    }


def test_responses_compaction_has_no_tools_and_preserves_native_items() -> None:
    manager, wire = fixture([responses("只调温度，排除压力。")])
    manager.model.selection = replace(
        selection("gpt-5.6-sol-cdx"),
        profile=replace(selection("gpt-5.6-sol-cdx").profile, context_tokens=12_000),
        output_tokens=512,
    )
    raw = history(manager, count=12)[:-1]
    ai = parse_response(manager.model.selection, responses("近期的合法回复"))
    current = HumanMessage(content="继续")
    manager.begin_turn(current)
    outgoing = manager.prepare([*raw, ai, current], system=[], tools=[])
    assert manager.total_summaries == 1 and wire.paths == ["/v1/responses"]
    assert "tools" not in wire.requests[0]
    assert "opaque-summary" not in json.dumps(wire.requests[0])
    payload = request_payload(manager.model.selection, outgoing, [], stream=False)
    assert any(item.get("type") == "reasoning" for item in payload["input"])
    assert "max_output_tokens" in wire.requests[0]


def test_system_and_tool_schema_budget_cannot_be_hidden_by_summary() -> None:
    manager, wire = fixture([])
    raw = history(manager, count=12)
    with pytest.raises(NativeModelError, match="context-overflow"):
        manager.prepare(raw, system=[SystemMessage(content="role" * 4_000)], tools=[])
    assert not wire.requests


def test_invalid_summary_watermark_never_silently_reuses_unrelated_archive() -> None:
    manager, wire = fixture([])
    raw = history(manager)
    manager.covered_until = "not-in-transcript"
    with pytest.raises(NativeModelError, match="summary-history-mismatch"):
        manager.prepare(raw, system=[], tools=[])
    assert not wire.requests


@pytest.mark.parametrize("model_id", ["qwen3.8-max-0902", "kimi-k3", "deepseek-v4-pro-0813"])
def test_thinking_model_summary_uses_its_own_protocol_without_replaying_private_state(
    model_id: str,
) -> None:
    manager, wire = fixture([chat("只调温度，不调压力。", reasoning="summary-private-state")])
    selected = selection(model_id)
    manager.model.selection = replace(
        selected, profile=replace(selected.profile, context_tokens=12_000), output_tokens=512
    )
    raw = history(manager, count=12)
    outgoing = manager.prepare(raw, system=[], tools=[])
    assert manager.total_summaries == 1
    assert wire.requests[0]["model"] == model_id
    assert "tools" not in wire.requests[0]
    assert "summary-private-state" not in "\n".join(m.text for m in outgoing)
    request_payload(manager.model.selection, outgoing, [], stream=False)


def test_paging_is_a_real_native_tool_roundtrip_and_does_not_repeat_original_query(
    repo_root: Path,
) -> None:
    manager, wire = fixture(
        [chat(None, calls=[call("get_plant_info")]), chat("结果已保留，可分页查看。")],
        window=30_000,
    )
    domain = AgentDomainTools(repo_root)
    reads = 0

    def large_plant() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"detail": "ABC中文" * 10_000}

    domain.plant_info = large_plant  # type: ignore[method-assign]
    runtime = ReactAgent(manager.model, domain)
    assert not runtime.handle("查工况").errors
    ref = next(iter(runtime.context.results))
    wire.replies.extend(
        [
            chat(
                None,
                calls=[
                    call(
                        "read_tool_result",
                        json.dumps({"result_ref": ref, "offset": 0, "max_characters": 100}),
                        "c2",
                    )
                ],
            ),
            chat("已读取第一段。"),
        ]
    )
    assert not runtime.handle("读取第一段").errors
    page = json.loads(
        next(m.content for m in reversed(runtime.messages) if isinstance(m, ToolMessage))
    )
    assert page["status"] == "ok" and page["next_offset"] == 100
    assert reads == 1
    assert "read_tool_result" in {t["function"]["name"] for t in wire.requests[0]["tools"]}
