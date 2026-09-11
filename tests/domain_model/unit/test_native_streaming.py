from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langchain_core.tools import StructuredTool
from test_native_protocol import (
    Wire,
    chat,
    responses_output,
    responses_stream_events,
    selection,
    sse,
)

from petroleum_rto.domain_model.models import MODELS, ModelSelection
from petroleum_rto.domain_model.native import (
    DmxNativeModel,
    NativeModelError,
    NativeTransport,
    request_payload,
)


def delta(value: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": value, "finish_reason": finish}]}


def aggregate(chunks: list[AIMessageChunk]) -> AIMessage:
    result = chunks[0]
    for chunk in chunks[1:]:
        result += chunk
    message = message_chunk_to_message(result)
    assert isinstance(message, AIMessage)
    return message


class SegmentedBody(httpx.SyncByteStream):
    def __init__(self, parts: list[bytes | Exception]) -> None:
        self.parts = parts
        self.read_count = 0

    def __iter__(self) -> Iterator[bytes]:
        for part in self.parts:
            self.read_count += 1
            if isinstance(part, Exception):
                raise part
            yield part


def test_chinese_text_streams_before_next_network_read_and_byte_splits_are_lossless() -> None:
    event = (
        "data: " + json.dumps(delta({"content": "中文🙂"}), ensure_ascii=False) + "\n\n"
    ).encode()
    split = event.index("中".encode()) + 1
    body = SegmentedBody(
        [event[:split], event[split:], sse(delta({"content": "已完成"}, "stop"), "[DONE]")]
    )
    transport = NativeTransport(
        "fake-private-api-key",
        http_transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
    )
    model = DmxNativeModel(transport=transport, selection=selection())
    stream = model.stream("合成测试")
    first = next(stream)
    assert first.content == "中文🙂"
    assert body.read_count == 2  # The final response has not been read yet.
    chunks = [first, *stream]
    assert [chunk.content for chunk in chunks] == ["中文🙂", "已完成", ""]
    assert aggregate(chunks).text == "中文🙂已完成"
    assert chunks[-1].chunk_position == "last"
    assert transport.request_count == 1


def test_tool_arguments_are_only_exposed_after_finished_strict_parse() -> None:
    wire = Wire(
        [
            sse(
                delta(
                    {
                        "content": "正在查询。",
                        "reasoning_content": "原生",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "lookup", "arguments": '{"查询":'},
                            }
                        ],
                    }
                ),
                delta(
                    {
                        "reasoning_content": "续接",
                        "tool_calls": [{"index": 0, "function": {"arguments": '"中文"}'}}],
                    }
                ),
                delta({}, "tool_calls"),
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 7,
                        "total_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 3},
                    },
                },
                "[DONE]",
            )
        ]
    )
    configured = selection("kimi-k3")
    chunks = list(wire.model(configured, stream=True).stream("查询"))
    assert all(not chunk.tool_calls and not chunk.tool_call_chunks for chunk in chunks[:-1])
    result = aggregate(chunks)
    assert result.tool_calls[0]["args"] == {"查询": "中文"}
    assert result.additional_kwargs["dmx_native"]["reasoning_content"] == "原生续接"
    assert result.response_metadata["finish_reason"] == "tool_calls"
    assert result.usage_metadata == {
        "input_tokens": 13,
        "output_tokens": 7,
        "total_tokens": 20,
        "input_token_details": {"cache_read": 4},
        "output_token_details": {"reasoning": 3},
    }
    payload = request_payload(
        configured,
        [HumanMessage("查询"), result, ToolMessage("返回值", tool_call_id="c1")],
        [],
        stream=True,
    )
    assert payload["messages"][1]["reasoning_content"] == "原生续接"
    assert payload["messages"][2]["tool_call_id"] == "c1"


def test_responses_empty_body_tool_and_opaque_reasoning_survive_standard_chunks() -> None:
    output = responses_output()
    events = responses_stream_events(output)
    events.insert(
        3,
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "item_id": "fc1",
            "delta": '{"marker":',
        },
    )
    events[-1]["response"]["usage"] = {
        "input_tokens": 10,
        "output_tokens": 9,
        "total_tokens": 19,
        "input_tokens_details": {"cached_tokens": 2},
        "output_tokens_details": {"reasoning_tokens": 5},
    }
    configured = selection("gpt-5.6-sol-cdx")
    wire = Wire([sse(*events)])
    chunks = list(wire.model(configured, stream=True).stream("查询"))
    assert len(chunks) == 1 and chunks[0].content == ""
    result = aggregate(chunks)
    assert result.additional_kwargs["dmx_native"] == output
    assert result.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 9,
        "total_tokens": 19,
        "input_token_details": {"cache_read": 2},
        "output_token_details": {"reasoning": 5},
    }
    payload = request_payload(
        configured,
        [HumanMessage("查询"), result, ToolMessage("返回值", tool_call_id="c1")],
        [],
        stream=True,
    )
    assert payload["input"][1:3] == output
    assert payload["input"][3]["call_id"] == "c1"


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("failure", ["incomplete", "transport", "invalid-arguments"])
def test_visible_partial_answer_never_exposes_tools_when_stream_fails(
    protocol: str, failure: str
) -> None:
    configured = selection("gpt-5.6-sol-cdx") if protocol == "responses" else selection()
    if protocol == "chat":
        prefix = sse(
            delta(
                {
                    "content": "已显示的部分正文",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "lookup", "arguments": '{"x":1,"x":2}'},
                        }
                    ],
                }
            )
        )
        suffix = sse(delta({}, "tool_calls"), "[DONE]")
    else:
        prefix = sse({"type": "response.output_text.delta", "delta": "已显示的部分正文"})
        output = responses_output()
        output[1]["arguments"] = '{"x":1,"x":2}'
        suffix = sse(*responses_stream_events(output))
    parts: list[bytes | Exception] = [prefix]
    if failure == "transport":
        parts.append(httpx.ReadError("private upstream body"))
    elif failure == "invalid-arguments":
        parts.append(suffix)
    body = SegmentedBody(parts)
    transport = NativeTransport(
        "fake-private-api-key",
        http_transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
    )
    stream = DmxNativeModel(transport=transport, selection=configured).stream("合成测试")
    chunk = next(stream)
    assert chunk.content == "已显示的部分正文"
    assert not chunk.tool_calls and not chunk.tool_call_chunks
    with pytest.raises(NativeModelError):
        list(stream)
    assert transport.request_count == 1


@pytest.mark.parametrize("encoding", ["raw", "base64", "url"])
def test_fragmented_credential_never_reaches_a_text_chunk(encoding: str) -> None:
    key = "fake/private-api-key"
    secret = {
        "raw": key,
        "base64": base64.b64encode(key.encode()).decode(),
        "url": quote(key, safe=""),
    }[encoding]
    response = sse(
        delta({"content": "合成回复。"}),
        *[delta({"content": character}) for character in secret],
        delta({}, "stop"),
        "[DONE]",
    )
    transport = NativeTransport(
        key, http_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=response))
    )
    chunks: list[AIMessageChunk] = []
    with pytest.raises(NativeModelError, match="credential-in-response"):
        for chunk in DmxNativeModel(transport=transport, selection=selection()).stream("合成测试"):
            chunks.append(chunk)  # noqa: PERF402 - retain already-yielded chunks after failure
    assert "".join(chunk.text for chunk in chunks) == "合成回复。"


@pytest.mark.parametrize("usage", [None, {"total_tokens": 27}, {"prompt_tokens": 5}])
def test_missing_usage_breakdown_is_preserved_without_fabrication(usage: Any) -> None:
    reply = chat()
    reply["usage"] = usage
    wire = Wire([reply])
    result = aggregate(list(wire.model().stream("合成测试")))
    assert result.response_metadata["usage"] == usage
    assert result.usage_metadata is None


@pytest.mark.parametrize("configured", [ModelSelection(profile) for profile in MODELS])
def test_standard_capacity_profile_tracks_selected_model_and_generation_reserve(
    configured: ModelSelection,
) -> None:
    wire = Wire([])
    model = wire.model()
    model.selection = configured
    assert model.profile is not None
    assert model.profile["name"] == configured.profile.model_id
    assert model.profile["max_input_tokens"] == (
        configured.profile.context_tokens - configured.output_tokens
    )
    assert model.profile["max_output_tokens"] == configured.output_tokens
    model.selection = replace(configured, output_tokens=8192)
    assert model.profile["max_input_tokens"] == configured.profile.context_tokens - 8192
    assert model.profile["max_output_tokens"] == 8192


def test_actual_request_counter_includes_http_failures_and_excludes_local_rejection() -> None:
    wire = Wire([429, chat()])
    model = wire.model()
    with pytest.raises(NativeModelError, match="credential-in-request"):
        list(model.stream("fake-private-api-key"))
    assert wire.transport.request_count == 0
    with pytest.raises(NativeModelError, match="rate-limited"):
        list(model.stream("合成测试"))
    assert wire.transport.request_count == 1
    model.invoke("再次测试")
    assert wire.transport.request_count == 2


def test_responses_final_content_conflict_rejects_terminal_without_duplicate_text() -> None:
    output = [
        {
            "id": "m1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "不同正文"}],
        }
    ]
    events = responses_stream_events(output)
    events.insert(1, {"type": "response.output_text.delta", "delta": "已显示正文"})
    wire = Wire([sse(*events)])
    stream = wire.model(selection("gpt-5.6-sol-cdx"), stream=True).stream("合成测试")
    assert next(stream).text == "已显示正文"
    with pytest.raises(NativeModelError, match="invalid-response"):
        list(stream)


@pytest.mark.parametrize(
    "configured",
    [ModelSelection(profile) for profile in MODELS]
    + [
        replace(selection(model), thinking="off")
        for model in ("qwen3.8-max-0902", "deepseek-v4-pro-0813", "gpt-5.6-sol-cdx")
    ],
    ids=lambda selected: f"{selected.profile.model_id}-{selected.thinking}",
)
def test_framework_messages_stream_preserves_two_tool_rounds_and_emits_text_once(
    configured: ModelSelection,
) -> None:
    protocol = configured.profile.protocol
    replies: list[bytes] = []
    for index, (name, args) in enumerate(
        [("read_marker", "{}"), ("echo_marker", '{"marker":"synthetic-令牌"}'), ("", "")]
    ):
        if protocol == "chat":
            message: dict[str, Any] = {}
            if configured.thinking_enabled:
                message["reasoning_content"] = f"native-reasoning-{index}"
            if name:
                message["tool_calls"] = [
                    {
                        "index": 0,
                        "id": f"c{index}",
                        "function": {"name": name, "arguments": args},
                    }
                ]
            else:
                message["content"] = "合成结果"
            replies.append(sse(delta(message, "tool_calls" if name else "stop"), "[DONE]"))
        else:
            output: list[dict[str, Any]] = []
            if configured.thinking_enabled:
                output.append(
                    {
                        "id": f"r{index}",
                        "type": "reasoning",
                        "encrypted_content": f"opaque-reasoning-{index}",
                        "summary": [],
                    }
                )
            if name:
                output.append(
                    {
                        "id": f"f{index}",
                        "type": "function_call",
                        "call_id": f"c{index}",
                        "name": name,
                        "arguments": args,
                        "status": "completed",
                    }
                )
            else:
                output.append(
                    {
                        "id": "m1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "合成结果"}],
                    }
                )
            events = responses_stream_events(output)
            if not name:
                events.insert(-1, {"type": "response.output_text.delta", "delta": "合成结果"})
            replies.append(sse(*events))
    executed: list[str] = []

    def read_marker() -> str:
        executed.append("read")
        return "synthetic-令牌"

    def echo_marker(marker: str) -> str:
        assert marker == "synthetic-令牌"
        executed.append("echo")
        return marker

    tools = [
        StructuredTool.from_function(read_marker, description="Read a synthetic marker"),
        StructuredTool.from_function(echo_marker, description="Echo the synthetic marker"),
    ]
    wire = Wire(replies)
    graph = create_agent(model=wire.model(configured, stream=True), tools=tools)
    events = list(
        graph.stream({"messages": [HumanMessage("合成测试")]}, stream_mode=["messages", "updates"])
    )
    text = "".join(
        event[0].text
        for mode, event in events
        if mode == "messages" and isinstance(event[0], AIMessageChunk)
    )
    assert text == "合成结果"
    assert executed == ["read", "echo"]
    assert wire.transport.request_count == 3
    for payload in wire.requests:
        assert payload["model"] == configured.profile.model_id
        assert payload["stream"] is True
    final_payload = wire.requests[-1]
    if protocol == "chat":
        assistants = [
            record for record in final_payload["messages"] if record["role"] == "assistant"
        ]
        if configured.thinking_enabled:
            assert [record["reasoning_content"] for record in assistants] == [
                "native-reasoning-0",
                "native-reasoning-1",
            ]
        else:
            assert all("reasoning_content" not in record for record in assistants)
        assert final_payload["messages"][-1]["tool_call_id"] == "c1"
    else:
        reasoning = [
            record for record in final_payload["input"] if record.get("type") == "reasoning"
        ]
        assert [record["encrypted_content"] for record in reasoning] == (
            ["opaque-reasoning-0", "opaque-reasoning-1"] if configured.thinking_enabled else []
        )
        assert final_payload["input"][-1]["call_id"] == "c1"
