from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event, current_thread
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict
from langgraph.runtime import RunControl, get_runtime
from test_native_protocol import chat, selection, sse

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.domain_model.native import DmxNativeModel, NativeTransport

_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from test_model_retry_interrupt import _signal_worker; _signal_worker()"
)


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    content = path.read_text()
    lines = content.splitlines()
    if not content.endswith("\n"):
        lines = lines[:-1]
    return [json.loads(line) for line in lines]


def _signal_worker() -> None:
    """A real SIGINT reaches the main thread while framework workers are blocked."""
    workspace, source, phase = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    workspace.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(workspace / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    cancelled = Event()
    requests: list[dict[str, Any]] = []
    controls: list[RunControl] = []
    later_turn = False
    ready = False
    original_sleep = time.sleep

    def record(event: str, **values: Any) -> None:
        os.write(log_fd, (json.dumps({"event": event, **values}) + "\n").encode())

    class ObservedControl(RunControl):
        def __init__(self) -> None:
            super().__init__()
            controls.append(self)
            assert not self.drain_requested

        def request_drain(self, reason: str = "shutdown") -> None:
            super().request_drain(reason)
            record("drain", reason=reason)
            cancelled.set()

    def controlled_sleep(seconds: float) -> None:
        nonlocal ready
        if seconds <= 0:
            return
        if phase == "backoff" and not later_turn and not ready:
            ready = True
            record("ready", phase="backoff", thread=current_thread().name, delay=seconds)
            if not cancelled.wait(timeout=10):
                raise AssertionError("parent did not signal the retry backoff")
        # Delay length is irrelevant: the real official retry loop still owns attempts.

    def mock(request: httpx.Request) -> httpx.Response:
        nonlocal ready
        payload = json.loads(request.content)
        requests.append(payload)
        record("request", later_turn=later_turn, after_cancel=cancelled.is_set())
        if not later_turn:
            if phase == "http" and not ready:
                ready = True
                record("ready", phase="http", thread=current_thread().name)
                if not cancelled.wait(timeout=10):
                    raise AssertionError("parent did not signal the active HTTP request")
            return httpx.Response(503)
        text = "下一轮正常回答。" if payload.get("tools") else "旧资料摘要：仅调温度。"
        return httpx.Response(200, json=chat(text))

    def no_computation(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("interrupt acceptance must not run physical computation")

    react.RunControl = ObservedControl
    react.solve_prepared_optimization = no_computation
    react.verify_prepared_optimization = no_computation
    time.sleep = controlled_sleep
    transport = NativeTransport("synthetic-test-key", http_transport=httpx.MockTransport(mock))
    selected = selection()
    if source == "summary":
        selected = replace(
            selected, profile=replace(selected.profile, context_tokens=12_000), output_tokens=512
        )
    runtime = ReactAgent(
        DmxNativeModel(transport=transport, selection=selected, use_stream=False),
        AgentDomainTools(workspace),
    )
    try:
        original_handler = signal.getsignal(signal.SIGINT)
        assert original_handler is signal.default_int_handler
        if source == "summary":
            runtime._update(
                {},
                [HumanMessage("旧资料" + "数据" * 300, id=f"old-{i}") for i in range(30)],
            )
        originals = messages_to_dict(runtime.messages)
        interrupted = runtime.handle("本轮原始用户请求")
        assert interrupted.errors and "中止" in str(interrupted.errors)
        assert signal.getsignal(signal.SIGINT) is original_handler
        assert transport.request_count == len(requests) == 1
        assert bool(requests[0].get("tools")) is (source == "ordinary")
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        assert any(message.text == "本轮原始用户请求" for message in runtime.messages)
        assert not any(isinstance(message, AIMessage) for message in runtime.messages)
        assert controls[0].drain_requested
        record("first-returned", count=transport.request_count)

        later_turn = True
        result = runtime.handle("下一轮请正常回答")
        assert not result.errors and "下一轮正常回答。" in str(result.outputs)
        assert signal.getsignal(signal.SIGINT) is original_handler
        assert len(controls) == 2 and controls[0] is not controls[1]
        assert messages_to_dict(runtime.messages)[: len(originals)] == originals
        assert transport.request_count == len(requests) > 1
        assert not (workspace / "runs/rto").exists()
        print(
            json.dumps({"next_turn_ok": True, "handler_restored": True, "requests": len(requests)})
        )
    finally:
        runtime.close()
        time.sleep = original_sleep
        os.close(log_fd)


@pytest.mark.parametrize("source", ["ordinary", "summary"])
@pytest.mark.parametrize("phase", ["backoff", "http"])
def test_sigint_stops_background_retry_and_next_turn_has_fresh_control(
    tmp_path: Path, source: str, phase: str
) -> None:
    workspace = tmp_path / "worker"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _BOOTSTRAP,
            str(Path(__file__).parent),
            str(workspace),
            source,
            phase,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            events = _events(workspace / "events.jsonl")
            ready = next((event for event in events if event["event"] == "ready"), None)
            if ready is not None:
                break
            if process.poll() is not None:
                pytest.fail(str(process.communicate()))
            time.sleep(0.01)
        else:
            pytest.fail("child did not reach its HTTP/backoff cancellation barrier")
        assert ready["phase"] == phase and ready["thread"].startswith("ThreadPoolExecutor")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["next_turn_ok"] and result["handler_restored"]
        events = _events(workspace / "events.jsonl")
        assert any(event == {"event": "drain", "reason": "user-interrupt"} for event in events)
        assert [
            event for event in events if event["event"] == "request" and not event["later_turn"]
        ] == [{"event": "request", "later_turn": False, "after_cancel": False}]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_callback_interrupt_drains_before_waiting_for_stream_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    observed_drain: list[bool] = []

    class DrainAwareBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield sse(
                {"choices": [{"index": 0, "delta": {"content": "部分正文"}, "finish_reason": None}]}
            )
            deadline = time.monotonic() + 5
            pause = Event()
            while time.monotonic() < deadline:
                if get_runtime().drain_requested:
                    observed_drain.append(True)
                    raise httpx.ReadError("synthetic interrupted body")
                pause.wait(timeout=0.01)
            raise AssertionError("stream close waited without signalling its active worker")

    transport = NativeTransport(
        "synthetic-test-key",
        http_transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=DrainAwareBody())),
    )
    runtime = ReactAgent(
        DmxNativeModel(transport=transport, selection=selection(), use_stream=True),
        AgentDomainTools(tmp_path),
    )
    original_handler = signal.getsignal(signal.SIGINT)
    visible: list[str] = []

    def abandon(text: str) -> None:
        visible.append(text)
        raise KeyboardInterrupt

    try:
        result = runtime.handle("保留这条原始用户请求", on_text=abandon)
        assert result.errors and "中止" in str(result.errors)
        assert visible == ["部分正文"] and observed_drain == [True]
        assert transport.request_count == 1
        assert signal.getsignal(signal.SIGINT) is original_handler
        assert any(message.text == "保留这条原始用户请求" for message in runtime.messages)
        assert not any(isinstance(message, AIMessage) for message in runtime.messages)
    finally:
        runtime.close()
