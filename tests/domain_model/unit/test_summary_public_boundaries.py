from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage, message_to_dict, messages_to_dict
from test_native_protocol import Wire, call, chat, selection, sse

from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.domain_model.native import parse_response, request_size

_SUMMARY = '内部摘要：只调温度，排除压力。{"literal":"{messages}"}'
_ANSWER = "本轮回答完成。"


class SummaryBoundaryWire(Wire):
    """Distinguish real summary and ordinary requests at the synthetic HTTP boundary."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.paths.append(request.url.path)
        text = _ANSWER if payload.get("tools") else _SUMMARY
        if payload["stream"]:
            return httpx.Response(
                200,
                content=sse(
                    {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    "[DONE]",
                ),
            )
        return httpx.Response(200, json=chat(text))


def small_agent(workspace: Path, *, stream: bool) -> tuple[ReactAgent, SummaryBoundaryWire]:
    wire = SummaryBoundaryWire([])
    selected = replace(
        selection(), profile=replace(selection().profile, context_tokens=12_000), output_tokens=512
    )
    return ReactAgent(wire.model(selected, stream=stream), AgentDomainTools(workspace)), wire


@pytest.mark.parametrize("stream", [False, True])
def test_real_graph_never_resummarizes_only_old_summary_and_continues_below_capacity(
    tmp_path: Path, stream: bool
) -> None:
    runtime, wire = small_agent(tmp_path, stream=stream)
    try:
        raw = [
            HumanMessage(f"原始标记{i}：" + "完整事实" * 100, id=f"original-{i}") for i in range(36)
        ]
        runtime._update({}, raw)
        originals = messages_to_dict(runtime.messages)
        visible: list[str] = []
        turn = runtime.handle("本轮受保护输入", on_text=visible.append if stream else None)
        assert not turn.errors
        assert tuple(text for text in turn.outputs if text.startswith("模型> ")) == (
            "模型> " + _ANSWER,
        )
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        assert "".join(visible) == (_ANSWER if stream else "")
        summaries = [payload for payload in wire.requests if not payload.get("tools")]
        assert len(summaries) > 1
        assert runtime.data["context"]["total_summaries"] == len(summaries)
        assert runtime.data["context"]["summary_calls"] == len(summaries)
        assert len(wire.requests) == wire.transport.request_count == len(summaries) + 1
        for payload in summaries:
            prompt = payload["messages"][0]["content"]
            assert "原始标记" in prompt  # Every request consumes new canonical source records.
            assert "本轮受保护输入" not in prompt
        assert _SUMMARY in summaries[1]["messages"][0]["content"]
        assert all(
            request_size(payload) + payload["max_tokens"] <= 12_000 for payload in wire.requests
        )
    finally:
        runtime.close()


def test_first_remaining_tool_group_is_summarized_whole_with_prior_summary(
    tmp_path: Path,
) -> None:
    runtime, wire = small_agent(tmp_path, stream=False)
    try:
        covered = HumanMessage("已被摘要覆盖的原始记录", id="covered-original")
        tool_calls = [
            call(f"historical_lookup_{i}", call_id=f"historical-call-{i}") for i in range(4)
        ]
        ai = parse_response(runtime.model.selection, chat(None, calls=tool_calls))
        results = [
            ToolMessage(
                content=f"工具原始标记{i}：" + "A" * 1_000,
                tool_call_id=f"historical-call-{i}",
                id=f"historical-tool-{i}",
            )
            for i in range(4)
        ]
        context: dict[str, Any] = {
            **runtime.data["context"],
            "summary": message_to_dict(HumanMessage(_SUMMARY, id="prior-summary")),
            "covered_until": covered.id,
            "total_summaries": 1,
        }
        runtime._update({"context": context, "reported_summaries": 1}, [covered, ai, *results])
        originals = messages_to_dict(runtime.messages)
        turn = runtime.handle("本轮受保护输入")
        assert not turn.errors
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        summaries = [payload for payload in wire.requests if not payload.get("tools")]
        assert len(summaries) == 1
        prompt = summaries[0]["messages"][0]["content"]
        assert _SUMMARY in prompt
        assert all(f"工具原始标记{i}" in prompt for i in range(4))
        assert "本轮受保护输入" not in prompt
        assert "已被摘要覆盖的原始记录" not in prompt
        assert runtime.data["context"]["covered_until"] == results[-1].id
        assert runtime.data["context"]["total_summaries"] == 2
        assert len(wire.requests) == wire.transport.request_count == 2
        assert all(
            request_size(payload) + payload["max_tokens"] <= 12_000 for payload in wire.requests
        )
    finally:
        runtime.close()
