from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_chat_cli import _TTYStringIO
from test_native_protocol import Wire, selection, sse
from test_native_streaming import SegmentedBody, delta

from petroleum_rto.assistant import cli
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent


class StreamingRetryWire(Wire):
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.replies and isinstance(self.replies[0], httpx.SyncByteStream):
            self.requests.append(json.loads(request.content))
            self.paths.append(request.url.path)
            return httpx.Response(200, stream=self.replies.pop(0))
        return super().handle(request)


def partial(protocol: str, text: str) -> bytes:
    if protocol == "responses":
        return sse(
            {"type": "response.output_text.delta", "delta": text},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "unfinished-function",
                    "type": "function_call",
                    "call_id": "not-executable",
                    "name": "get_plant_info",
                    "arguments": "{",
                },
            },
        )
    return sse(
        delta(
            {
                "content": text,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "not-executable",
                        "function": {"name": "get_plant_info", "arguments": "{"},
                    }
                ],
            }
        )
    )


def completed(protocol: str, text: str) -> bytes:
    if protocol == "responses":
        return sse(
            {"type": "response.output_text.delta", "delta": text},
            {
                "type": "response.completed",
                "response": {
                    "id": "complete-answer",
                    "status": "completed",
                    "output": [
                        {
                            "id": "message",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                },
            },
        )
    return sse(delta({"content": text}), delta({}, "stop"), "[DONE]")


def failing(protocol: str, text: str) -> SegmentedBody:
    return SegmentedBody([partial(protocol, text), httpx.ReadError("private-upstream-detail")])


def agent(
    workspace: Path, protocol: str, replies: list[Any]
) -> tuple[ReactAgent, StreamingRetryWire]:
    wire = StreamingRetryWire(replies)
    selected = selection("gpt-5.6-sol-cdx") if protocol == "responses" else selection()
    return ReactAgent(wire.model(selected, stream=True), AgentDomainTools(workspace)), wire


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("progress_callback", [False, True])
def test_partial_stream_retry_is_separated_and_only_success_is_saved(
    tmp_path: Path,
    protocol: str,
    progress_callback: bool,
) -> None:
    runtime, wire = agent(
        tmp_path,
        protocol,
        [
            failing(protocol, "失败尝试的半句"),
            completed(protocol, "重新生成的完整正文"),
        ],
    )
    events: list[tuple[str, str]] = []
    try:
        result = runtime.handle(
            "合成流测试",
            on_text=lambda text: events.append(("text", text)),
            on_progress=(lambda text: events.append(("progress", text)))
            if progress_callback
            else None,
        )
        assert not result.errors and result.text_streamed
        rendered = "".join(text for _, text in events)
        assert rendered.index("失败尝试的半句") < rendered.index("上次回答未完整生成")
        assert rendered.index("第2/3次尝试") < rendered.index("重新生成的完整正文")
        assert "第3/3次尝试" not in rendered
        assert "private-upstream-detail" not in rendered
        if progress_callback:
            assert [text for kind, text in events if kind == "text"] == [
                "失败尝试的半句",
                "重新生成的完整正文",
            ]
        else:
            assert "\n\n[程序：" in rendered  # Text-only clients still get a labeled boundary.
        assert [m.text for m in runtime.messages if isinstance(m, AIMessage)] == [
            "重新生成的完整正文"
        ]
        assert not any(isinstance(m, ToolMessage) for m in runtime.messages)
        assert "失败尝试的半句" not in str(result.outputs)
        assert wire.transport.request_count == len(wire.requests) == 2
        assert wire.requests[0] == wire.requests[1]
        assert not (tmp_path / "runs/rto").exists()
    finally:
        runtime.close()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_three_partial_failures_are_bounded_and_never_commit_an_answer(
    tmp_path: Path,
    protocol: str,
) -> None:
    runtime, wire = agent(tmp_path, protocol, [failing(protocol, f"部分{i}") for i in range(3)])
    events: list[str] = []
    try:
        result = runtime.handle("合成测试", on_text=events.append, on_progress=events.append)
        assert result.errors and result.text_streamed
        assert "已尝试3次" in str(result.errors)
        rendered = "".join(events)
        assert rendered.count("上次回答未完整生成") == 2
        assert "第2/3次尝试" in rendered and "第3/3次尝试" in rendered
        assert "第4" not in rendered
        assert not any(isinstance(m, (AIMessage, ToolMessage)) for m in runtime.messages)
        assert wire.transport.request_count == len(wire.requests) == 3
    finally:
        runtime.close()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_cleanly_received_but_unfinished_provider_stream_is_not_retried(
    tmp_path: Path,
    protocol: str,
) -> None:
    runtime, wire = agent(tmp_path, protocol, [partial(protocol, "截断正文")])
    events: list[str] = []
    try:
        result = runtime.handle("合成测试", on_text=events.append, on_progress=events.append)
        assert result.errors and "incomplete-stream" in str(result.errors)
        assert events == ["截断正文"]
        assert wire.transport.request_count == len(wire.requests) == 1
        assert not any(isinstance(m, (AIMessage, ToolMessage)) for m in runtime.messages)
    finally:
        runtime.close()


@pytest.mark.parametrize("exhausted", [False, True])
def test_actual_interactive_cli_separates_retry_and_does_not_repeat_final_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exhausted: bool,
) -> None:
    replies: list[Any] = [failing("chat", "半句0")]
    replies += (
        [failing("chat", "半句1"), failing("chat", "半句2")]
        if exhausted
        else [completed("chat", "完整最终回答")]
    )
    runtime, wire = agent(tmp_path, "chat", replies)
    source, output, error = _TTYStringIO(), _TTYStringIO(), io.StringIO()
    inputs = iter(["合成测试", "/exit"])
    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setitem(sys.modules, "readline", object())
    try:
        assert cli._run_repl(runtime, input_stream=source, output=output, error=error) == 0
        rendered = output.getvalue()
        assert "半句0\n进度：上次回答未完整生成" in rendered
        assert rendered.count("半句0") == 1
        assert ("本次模型回答未完整生成。" in rendered) is exhausted
        assert bool(error.getvalue()) is exhausted
        if exhausted:
            assert "已尝试3次" in error.getvalue()
            assert wire.transport.request_count == 3
        else:
            assert rendered.count("完整最终回答") == 1
            assert rendered.index("第2/3次尝试") < rendered.index("完整最终回答")
            assert wire.transport.request_count == 2
    finally:
        runtime.close()
