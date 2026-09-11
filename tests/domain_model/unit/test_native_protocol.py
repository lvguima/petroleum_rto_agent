from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from petroleum_rto.domain_model.models import ModelSelection, model_profile
from petroleum_rto.domain_model.native import (
    DmxNativeModel,
    NativeModelError,
    NativeTransport,
    request_payload,
)

FLASH_MODEL_ID = "deepseek-v4-flash-0731"


def selection(model: str = FLASH_MODEL_ID) -> ModelSelection:
    """Chat fixtures select Flash explicitly, independently of the startup default."""
    return ModelSelection(model_profile(model))


def chat(
    content: str | None = "你好",
    *,
    calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
    finish: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "response-1",
        "model": "response-declared-model",
        "usage": {"total_tokens": 27},
        "choices": [
            {"message": message, "finish_reason": finish or ("tool_calls" if calls else "stop")}
        ],
    }


def call(name: str = "lookup", args: str = "{}", call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


class Wire:
    def __init__(self, replies: list[dict[str, Any] | bytes | int | Exception]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.transport = NativeTransport(
            "fake-private-api-key", http_transport=httpx.MockTransport(self.handle)
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.paths.append(request.url.path)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, text="fake-private-api-key provider body")
        if isinstance(reply, bytes):
            return httpx.Response(200, content=reply, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=reply)

    def model(
        self, selected: ModelSelection | None = None, *, stream: bool = False
    ) -> DmxNativeModel:
        return DmxNativeModel(
            transport=self.transport, selection=selected or selection(), use_stream=stream
        )


def sse(*items: dict[str, Any] | str) -> bytes:
    return "".join(
        "data: " + (x if isinstance(x, str) else json.dumps(x)) + "\n\n" for x in items
    ).encode()


def test_native_empty_content_tool_call_and_full_thinking_round_trip() -> None:
    wire = Wire(
        [
            chat(None, calls=[call(args='{"token":"first"}')], reasoning="state to retain"),
            chat("完成", reasoning="second state"),
        ]
    )
    model = wire.model(selection("kimi-k3"))
    first = model.bind_tools(
        [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    ).invoke([HumanMessage("查询")])
    assert first.content == ""
    model.invoke(
        [HumanMessage("查询"), first, ToolMessage(content="random-marker", tool_call_id="c1")]
    )
    payload = wire.requests[1]
    assert payload["messages"][1]["reasoning_content"] == "state to retain"
    assert payload["messages"][2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "random-marker",
    }
    assert first.response_metadata["requested_model"] == "kimi-k3"
    assert first.response_metadata["response_model"] == "response-declared-model"
    assert first.response_metadata["usage"]["total_tokens"] == 27


@pytest.mark.parametrize(
    "response,code",
    [
        (chat("partial", finish="length"), "incomplete-response"),
        (chat(False, calls=[call()]), "unsupported-content"),
        (chat(None, calls=[call(args='{"x":1,"x":2}')]), "invalid-response"),
        (chat(None, calls=[call(args='{"x":NaN}')]), "invalid-response"),
        (chat(None, calls=[call(), call()]), "invalid-tool-call-id"),
        (chat(None, calls=[call(args="[]")]), "invalid-tool-arguments"),
        (chat("fake-private-api-key"), "credential-in-response"),
        (401, "authentication-failed"),
        (403, "permission-denied"),
        (429, "rate-limited"),
        (500, "http-error"),
    ],
)
def test_invalid_responses_and_http_errors_are_safe(response: Any, code: str) -> None:
    wire = Wire([response])
    with pytest.raises(NativeModelError, match=code) as error:
        wire.model().invoke("hi")
    assert "fake-private-api-key" not in str(error.value)
    assert len(wire.requests) == 1  # no blind retries


def test_capacity_and_credentials_checked_before_network_and_no_64_message_limit() -> None:
    wire = Wire([chat()])
    model = wire.model()
    model.invoke([HumanMessage("hi") for _ in range(130)])
    assert len(wire.requests[0]["messages"]) == 130
    with pytest.raises(NativeModelError, match="credential-in-request"):
        model.invoke("fake-private-api-key")
    model.selection = replace(
        selection(), profile=replace(selection().profile, context_tokens=100), output_tokens=20
    )
    with pytest.raises(NativeModelError, match="context-overflow"):
        model.invoke("中" * 100)
    assert len(wire.requests) == 1


def test_cdx_budget_accepts_request_and_reserves_output_without_changing_id() -> None:
    wire = Wire([])
    selected = selection("gpt-5.6-sol-cdx")
    payload = request_payload(selected, [HumanMessage("hi")], [], stream=False)
    assert selected.profile.context_tokens == 256_000
    assert payload["model"] == "gpt-5.6-sol-cdx"
    assert payload["max_output_tokens"] == selected.output_tokens
    assert "input" in payload and "messages" not in payload
    with pytest.raises(NativeModelError, match="context-overflow"):
        wire.model(selected).invoke("x" * (256_000 - selected.output_tokens))
    assert not wire.requests


def test_invalid_tool_history_is_rejected_before_network() -> None:
    wire = Wire([chat(None, calls=[call()])])
    model = wire.model()
    first = model.invoke("hi")
    with pytest.raises(NativeModelError, match="unfinished-tool-call"):
        model.invoke([HumanMessage("hi"), first])
    with pytest.raises(NativeModelError, match="orphan-tool-result"):
        model.invoke([ToolMessage(content="result", tool_call_id="missing")])
    model.selection = selection("kimi-k3")
    with pytest.raises(NativeModelError, match="foreign-model-history"):
        model.invoke([first, ToolMessage(content="result", tool_call_id="c1")])
    assert len(wire.requests) == 1


def test_chat_sse_accumulates_arguments_reasoning_and_usage() -> None:
    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "model": "wire-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    wire = Wire(
        [
            sse(
                chunk(
                    {
                        "reasoning_content": "part1",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"token":'},
                            }
                        ],
                    }
                ),
                chunk(
                    {
                        "reasoning_content": "part2",
                        "tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}],
                    }
                ),
                chunk({}, "tool_calls"),
                {"choices": [], "usage": {"total_tokens": 44}},
                "[DONE]",
            )
        ]
    )
    message = wire.model(selection("kimi-k3"), stream=True).invoke("hi")
    assert message.tool_calls[0]["args"] == {"token": "x"}
    assert message.additional_kwargs["dmx_native"]["reasoning_content"] == "part1part2"
    assert message.response_metadata["usage"] == {"total_tokens": 44}


@pytest.mark.parametrize("end", [b"", b"data: [DONE]\n\n"])
def test_chat_sse_cut_off_before_finish_is_not_a_call(end: bytes) -> None:
    wire = Wire(
        [
            sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "function": {"name": "lookup", "arguments": "{"},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
            + end
        ]
    )
    with pytest.raises(NativeModelError, match="incomplete-stream"):
        wire.model(stream=True).invoke("hi")


def test_responses_sse_preserves_opaque_output_and_call_id() -> None:
    configured = selection("gpt-5.6-sol-cdx")
    output = [
        {"id": "rs1", "type": "reasoning", "encrypted_content": "opaque-state", "summary": []},
        {
            "id": "fc1",
            "type": "function_call",
            "call_id": "c1",
            "name": "lookup",
            "arguments": "{}",
            "status": "completed",
        },
    ]
    wire = Wire(
        [
            sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "r1",
                        "model": "declared-cdx",
                        "status": "completed",
                        "output": output,
                    },
                }
            )
        ]
    )
    model = wire.model(configured, stream=True)
    message = model.invoke("hi")
    payload = request_payload(
        configured,
        [HumanMessage("hi"), message, ToolMessage(content="marker", tool_call_id="c1")],
        [],
        stream=True,
    )
    assert payload["input"][1:3] == output
    assert payload["input"][3] == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "marker",
    }
    assert wire.paths == ["/v1/responses"]
    assert payload["model"] == "gpt-5.6-sol-cdx"
    assert "previous_response_id" not in payload and payload["store"] is False


@pytest.mark.parametrize(
    "model,mode,expected",
    [
        (FLASH_MODEL_ID, "default", {"enable_thinking": False}),
        (FLASH_MODEL_ID, "off", {"enable_thinking": False}),
        ("deepseek-v4-pro-0813", "off", {"thinking": {"type": "disabled"}}),
        ("qwen3.8-max-0902", "on", {"enable_thinking": True, "preserve_thinking": True}),
        ("kimi-k3", "default", {}),
    ],
)
def test_model_specific_thinking_parameters(
    model: str, mode: Any, expected: dict[str, Any]
) -> None:
    assert ModelSelection(model_profile(model), thinking=mode).parameters() == expected
    with pytest.raises(ValueError):
        ModelSelection(model_profile("kimi-k3"), thinking="off")
    with pytest.raises(ValueError):
        ModelSelection(model_profile(model), effort="invented-level")


def test_flash_thinking_is_rejected_before_constructing_a_request() -> None:
    with pytest.raises(ValueError, match="Flash渠道仅使用非思考模式"):
        ModelSelection(model_profile(FLASH_MODEL_ID), thinking="on")


def test_responses_incomplete_event_never_becomes_executable_output() -> None:
    configured = selection("gpt-5.6-sol-cdx")
    wire = Wire(
        [
            sse(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc1",
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "lookup",
                        "arguments": "{}",
                    },
                },
                {"type": "response.incomplete", "response": {"status": "incomplete"}},
            )
        ]
    )
    with pytest.raises(NativeModelError, match="incomplete-stream"):
        wire.model(configured, stream=True).invoke("hi")


def responses_output() -> list[dict[str, Any]]:
    return [
        {"id": "rs1", "type": "reasoning", "encrypted_content": "opaque-state", "summary": []},
        {
            "id": "fc1",
            "type": "function_call",
            "call_id": "c1",
            "name": "lookup",
            "arguments": '{"marker":"synthetic"}',
            "status": "completed",
        },
    ]


def responses_stream_events(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, item in enumerate(output):
        started = {**item, "status": "in_progress"}
        if item["type"] == "function_call":
            started["arguments"] = ""
        events.extend(
            [
                {"type": "response.output_item.added", "output_index": index, "item": started},
                {"type": "response.output_item.done", "output_index": index, "item": item},
            ]
        )
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": "r1",
                "model": "declared-cdx",
                "status": "completed",
                "output": [],
                "usage": {"total_tokens": 27},
            },
        }
    )
    return events


@pytest.mark.parametrize("terminal_has_output", [False, True])
def test_responses_sse_done_items_survive_empty_terminal_and_round_trip(
    terminal_has_output: bool,
) -> None:
    configured = selection("gpt-5.6-sol-cdx")
    output = responses_output()
    events = responses_stream_events(output)
    if terminal_has_output:
        events[-1]["response"]["output"] = output
    wire = Wire([sse(*events)])
    answer = wire.model(configured, stream=True).invoke("Read the synthetic marker")
    assert answer.tool_calls == [
        {"name": "lookup", "args": {"marker": "synthetic"}, "id": "c1", "type": "tool_call"}
    ]
    assert answer.additional_kwargs["dmx_native"] == output
    assert answer.response_metadata["usage"] == {"total_tokens": 27}
    payload = request_payload(
        configured,
        [HumanMessage("Read the synthetic marker"), answer, ToolMessage("ok", tool_call_id="c1")],
        [],
        stream=True,
    )
    assert payload["input"][1:3] == output
    assert payload["input"][3]["call_id"] == "c1"


def test_responses_sse_final_message_uses_completed_content_without_doubling() -> None:
    output = [
        {
            "id": "msg1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "合成最终结果", "annotations": []}],
        }
    ]
    events = responses_stream_events(output)
    events.insert(
        1,
        {"type": "response.output_text.delta", "output_index": 0, "delta": "合成最终结果"},
    )
    wire = Wire([sse(*events)])
    answer = wire.transport.complete(selection("gpt-5.6-sol-cdx"), {"stream": True})
    assert answer.text == "合成最终结果"
    assert answer.additional_kwargs["dmx_native"] == output


@pytest.mark.parametrize(
    "failure,code",
    [
        ("duplicate-done", "invalid-response"),
        ("duplicate-added", "invalid-response"),
        ("missing-done", "incomplete-stream"),
        ("index-gap", "incomplete-stream"),
        ("boolean-index", "invalid-response"),
        ("identity-change", "invalid-response"),
        ("name-change", "invalid-response"),
        ("duplicate-item-id", "invalid-response"),
        ("terminal-conflict", "invalid-response"),
        ("missing-encrypted-reasoning", "missing-reasoning"),
        ("tool-not-completed", "incomplete-response"),
        ("cut-off", "incomplete-stream"),
    ],
)
def test_responses_sse_rejects_incomplete_or_conflicting_output(failure: str, code: str) -> None:
    events = responses_stream_events(responses_output())
    if failure == "duplicate-done":
        events.insert(-1, events[3])
    elif failure == "duplicate-added":
        events.insert(1, events[0])
    elif failure == "missing-done":
        del events[3]
    elif failure == "index-gap":
        events[2]["output_index"] = events[3]["output_index"] = 2
    elif failure == "boolean-index":
        events[2]["output_index"] = True
    elif failure == "identity-change":
        events[3]["item"]["id"] = "different-id"
    elif failure == "name-change":
        events[3]["item"]["name"] = "different-tool"
    elif failure == "duplicate-item-id":
        events[2]["item"]["id"] = events[3]["item"]["id"] = "rs1"
    elif failure == "terminal-conflict":
        events[-1]["response"]["output"] = [events[1]["item"]]
    elif failure == "missing-encrypted-reasoning":
        del events[1]["item"]["encrypted_content"]
    elif failure == "tool-not-completed":
        events[3]["item"]["status"] = "in_progress"
    elif failure == "cut-off":
        events.pop()
    wire = Wire([sse(*events)])
    with pytest.raises(NativeModelError, match=code):
        wire.transport.complete(selection("gpt-5.6-sol-cdx"), {"stream": True})


def test_required_reasoning_missing_is_rejected_before_tool_execution() -> None:
    for name in ("kimi-k3", "deepseek-v4-pro-0813", "qwen3.8-max-0902"):
        wire = Wire([chat(None, calls=[call()])])
        with pytest.raises(NativeModelError, match="missing-reasoning"):
            wire.model(selection(name)).invoke("hi")


@pytest.mark.parametrize("encoding", ["raw", "base64", "url"])
def test_migrated_credential_guard_blocks_encoded_nested_request_and_response(
    encoding: str,
) -> None:
    import base64
    from urllib.parse import quote

    key = "fake-private-api-key"
    encoded = {
        "raw": key,
        "base64": base64.b64encode(key.encode()).decode(),
        "url": quote(key, safe=""),
    }[encoding]
    wire = Wire([chat("reply")])
    payload = request_payload(selection(), [HumanMessage(content="hello")], [], stream=False)
    payload["metadata"] = {encoded: ["data"]}
    with pytest.raises(NativeModelError, match="credential-in-request"):
        wire.transport.complete(selection(), payload)
    assert not wire.requests

    reply = chat("reply")
    reply["nested_metadata"] = [{"value": encoded}]
    reflected = Wire([reply])
    with pytest.raises(NativeModelError, match="credential-in-response") as captured:
        reflected.model().invoke([HumanMessage(content="hello")])
    assert key not in str(captured.value) and encoded not in str(captured.value)


@pytest.mark.parametrize("invalid", [None, "role", "arguments", "unknown"])
def test_flash_sse_nullable_fragments_preserve_call_and_reject_actual_invalid_values(
    invalid: str | None,
) -> None:
    fragment = {"name": None, "arguments": "{}"}
    delta = {
        "role": None,
        "content": None,
        "tool_calls": [{"index": 0, "id": None, "function": fragment}],
    }
    if invalid == "role":
        delta["role"] = "user"
    elif invalid == "arguments":
        fragment["arguments"] = 42
    elif invalid == "unknown":
        fragment["unexpected"] = None

    def chunk(value: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {"choices": [{"index": 0, "delta": value, "finish_reason": finish}]}

    wire = Wire(
        [
            sse(
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": ""},
                            }
                        ],
                    }
                ),
                chunk(delta),
                chunk({}, "tool_calls"),
                "[DONE]",
            )
        ]
    )
    if invalid:
        with pytest.raises(NativeModelError, match="invalid-response"):
            wire.model(stream=True).invoke("合成查询")
    else:
        result = wire.model(stream=True).invoke("合成查询")
        assert result.tool_calls == [
            {"name": "lookup", "args": {}, "id": "c1", "type": "tool_call"}
        ]


@pytest.mark.parametrize("stream", [False, True])
def test_deepseek_thinking_final_without_reasoning_cannot_be_replayed_with_tools(
    stream: bool,
) -> None:
    selected = selection("deepseek-v4-pro-0813")
    response = chat("已解释工况")
    if stream:
        response = sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "已解释工况"},
                        "finish_reason": "stop",
                    }
                ]
            },
            "[DONE]",
        )
    wire = Wire([response])
    model = wire.model(selected, stream=stream)
    bound = model.bind_tools(
        [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    )
    first = bound.invoke([HumanMessage("查工况")])
    assert first.text == "已解释工况"
    with pytest.raises(NativeModelError, match="missing-reasoning-history"):
        bound.invoke([HumanMessage("查工况"), first, HumanMessage("继续查询")])
    assert len(wire.requests) == 1
    assert "reasoning_content" not in first.additional_kwargs["dmx_native"]
    # A request without tools does not require historical reasoning; off mode does not either.
    request_payload(selected, [first, HumanMessage("聊聊")], [], stream=False)
    request_payload(
        replace(selected, thinking="off"),
        [first],
        [{"type": "function", "function": {"name": "lookup"}}],
        stream=False,
    )


def test_deepseek_thinking_replays_final_reasoning_exactly_on_next_user_turn() -> None:
    selected = selection("deepseek-v4-pro-0813")
    wire = Wire(
        [
            chat("已解释工况", reasoning="opaque-final-reasoning"),
            chat("继续", reasoning="new-state"),
        ]
    )
    bound = wire.model(selected).bind_tools(
        [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    )
    first = bound.invoke([HumanMessage("查工况")])
    bound.invoke([HumanMessage("查工况"), first, HumanMessage("继续")])
    assert wire.requests[1]["messages"][1]["reasoning_content"] == "opaque-final-reasoning"
