from __future__ import annotations

import io
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_native_protocol import Wire, responses_stream_events, sse

from petroleum_rto.assistant import cli
from petroleum_rto.assistant.react import HELP
from petroleum_rto.assistant.session import SessionError, SessionStore
from petroleum_rto.assistant.turn import AgentTurn


@pytest.fixture
def configured_default_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Wire]:
    """Use actual default settings and CLI construction, with synthetic HTTP only."""
    from petroleum_rto.domain_model import chat_settings, native

    monkeypatch.chdir(tmp_path)
    original_loader = chat_settings.load_dmx_chat_settings
    monkeypatch.setattr(
        chat_settings,
        "load_dmx_chat_settings",
        lambda: original_loader(key_loader=lambda: "fake-private-api-key"),
    )
    wires: list[Wire] = []

    def transport(api_key: str, *, base_url: str) -> native.NativeTransport:
        assert api_key == "fake-private-api-key"
        assert base_url == chat_settings.DMX_CHAT_URL.removesuffix("/chat/completions")
        wire = Wire([])
        wires.append(wire)
        return wire.transport

    monkeypatch.setattr(native, "NativeTransport", transport)
    return wires


def test_new_cli_session_defaults_to_sol_cdx_and_sends_responses_request(
    configured_default_cli: list[Wire],
) -> None:
    from petroleum_rto.domain_model.chat_settings import DMX_CHAT_MODEL
    from petroleum_rto.domain_model.models import DEFAULT_MODEL_ID

    runtime = cli._new_runtime()
    try:
        assert DMX_CHAT_MODEL == DEFAULT_MODEL_ID == "gpt-5.6-sol-cdx"
        assert runtime.model.selection.profile.model_id == "gpt-5.6-sol-cdx"
        assert runtime.model.selection.profile.context_tokens == 256_000
        assert runtime.model.selection.thinking_enabled
        wire = configured_default_cli[-1]
        assert not runtime.startup() and wire.transport.request_count == 0
        output = [
            {
                "id": "reasoning-default",
                "type": "reasoning",
                "encrypted_content": "synthetic-default-model-state",
                "summary": [],
            },
            {
                "id": "message-default",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "默认模型合成检查完成。"}],
            },
        ]
        wire.replies.append(sse(*responses_stream_events(output)))
        visible: list[str] = []
        result = runtime.handle("请完成一次合成启动检查。", on_text=visible.append)
        assert not result.errors and result.text_streamed
        assert "".join(visible) == "默认模型合成检查完成。"
        assert wire.paths == ["/v1/responses"] and wire.transport.request_count == 1
        request = wire.requests[0]
        assert request["model"] == "gpt-5.6-sol-cdx"
        assert request["stream"] is True
        assert request["reasoning"] == {"effort": "low", "summary": "auto"}
        assert "enable_thinking" not in request
    finally:
        runtime.close()


@pytest.mark.parametrize("saved_model", ["deepseek-v4-flash-0731", "kimi-k3"])
def test_default_model_change_does_not_override_saved_explicit_selection(
    configured_default_cli: list[Wire], saved_model: str
) -> None:
    runtime = cli._new_runtime()
    try:
        assert not runtime.handle(f"/model {saved_model}").errors
        if saved_model == "kimi-k3":
            assert not runtime.handle("/thinking on high").errors
        selected = runtime.data["model"]
        assert configured_default_cli[-1].transport.request_count == 0
    finally:
        runtime.close()

    restored = cli._new_runtime()
    try:
        assert restored.startup()
        assert restored.data["model"] == selected
        assert restored.model.selection.profile.model_id == saved_model
        assert restored.model.selection.thinking_enabled == (
            saved_model != "deepseek-v4-flash-0731"
        )
        assert all(wire.transport.request_count == 0 for wire in configured_default_cli)
    finally:
        restored.close()


class _FakeSession:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.clear_calls = 0

    def ask(self, message: str) -> str:
        self.messages.append(message)
        return f"回复{len(self.messages)}"

    def clear(self) -> None:
        self.clear_calls += 1


class _FakeRuntime:
    def __init__(self, session: _FakeSession, *, fail_model: bool = False) -> None:
        self.session = session
        self.fail_model = fail_model

    def handle(
        self,
        message: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> AgentTurn:
        assert on_progress is None
        assert on_text is None
        text = message.strip()
        if text == "/exit":
            return AgentTurn(should_exit=True)
        if text == "/help":
            return AgentTurn(outputs=(HELP.rstrip(),))
        if text == "/clear":
            self.session.clear()
            return AgentTurn(outputs=("对话和待处理任务已清空。",))
        if text.startswith("/"):
            return AgentTurn(errors=("未知命令，请输入 /help 查看支持的命令。",))
        if self.fail_model:
            return AgentTurn(errors=("模型调用失败，请检查本地配置和网络后重试。",))
        return AgentTurn(outputs=(f"模型> {self.session.ask(text)}",))


def test_cli_keeps_ordinary_chat_help_clear_and_exit_behavior(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(cli, "_new_runtime", lambda: _FakeRuntime(session))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("第一问\n第二问\n/clear\n/help\n/exit\n"),
    )

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert session.messages == ["第一问", "第二问"]
    assert session.clear_calls == 1
    assert "你> " in captured.out
    assert "模型> 回复1" in captured.out
    assert "模型> 回复2" in captured.out
    assert "对话和待处理任务已清空。" in captured.out
    assert "/capabilities" in captured.out
    assert "/preview" not in captured.out
    assert "/confirm" in captured.out
    assert "preview-ref" not in captured.out
    assert "/cancel" in captured.out
    assert "/result [结果编号]" in captured.out
    assert "/model" in captured.out
    assert "/thinking" in captured.out
    assert "/status" not in captured.out
    assert captured.err == ""


def test_status_slash_command_remains_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(cli, "_new_runtime", lambda: _FakeRuntime(session))
    monkeypatch.setattr("sys.stdin", io.StringIO("/status\n/exit\n"))

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert session.messages == []
    assert "未知命令" in captured.err


def test_model_and_startup_failures_do_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-secret-value-must-not-appear"
    monkeypatch.setattr(
        cli,
        "_new_runtime",
        lambda: (_ for _ in ()).throw(ValueError(secret)),
    )
    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.err

    monkeypatch.setattr(
        cli,
        "_new_runtime",
        lambda: _FakeRuntime(_FakeSession(), fail_model=True),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("你好\n/exit\n"))
    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "模型调用失败" in captured.err


def test_cli_rejects_arguments_and_eof_does_not_call_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(cli, "_new_runtime", lambda: _FakeRuntime(session))

    assert cli.main(["legacy-subcommand"]) == 2
    assert "无需参数" in capsys.readouterr().err

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert session.messages == []
    assert captured.err == ""


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_progress_is_visible_before_final_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _TTYStringIO()
    output = _TTYStringIO()
    error = io.StringIO()

    class Runtime:
        def handle(
            self,
            message: str,
            *,
            on_progress: Callable[[str], None] | None = None,
            on_text: Callable[[str], None] | None = None,
        ) -> AgentTurn:
            assert message == "确认\n"
            assert on_progress is not None
            assert on_text is not None
            on_progress("M2搜索：已评价候选1。")
            assert "M2搜索：已评价候选1。\n" in output.getvalue()
            assert "模型> 最终回答" not in output.getvalue()
            return AgentTurn(outputs=("模型> 最终回答",), should_exit=True)

    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr("builtins.input", lambda prompt: "确认")
    monkeypatch.setitem(sys.modules, "readline", object())

    assert cli._run_repl(Runtime(), input_stream=source, output=output, error=error) == 0
    assert output.getvalue() == ("输入 /help 查看命令。\nM2搜索：已评价候选1。\n模型> 最终回答\n")
    assert error.getvalue() == ""


def test_noninteractive_runtime_receives_no_progress_argument() -> None:
    source = io.StringIO("确认\n")
    output = io.StringIO()
    error = io.StringIO()

    class Runtime:
        def handle(self, message: str, **kwargs: object) -> AgentTurn:
            assert message == "确认\n"
            assert kwargs == {}
            return AgentTurn(outputs=("模型> 最终回答",), should_exit=True)

    assert cli._run_repl(Runtime(), input_stream=source, output=output, error=error) == 0
    assert output.getvalue() == "输入 /help 查看命令。\n你> 模型> 最终回答\n"
    assert error.getvalue() == ""


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"), [(False, False), (True, False), (False, True)]
)
def test_redirected_streams_keep_line_input(
    monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool
) -> None:
    source = (_TTYStringIO if stdin_tty else io.StringIO)("第一问\n第二问\n/exit\n")
    output = (_TTYStringIO if stdout_tty else io.StringIO)()
    error = io.StringIO()
    session = _FakeSession()
    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", output)
    # Non-interactive use must not require the optional platform readline module.
    monkeypatch.setitem(sys.modules, "readline", None)

    assert (
        cli._run_repl(_FakeRuntime(session), input_stream=source, output=output, error=error) == 0
    )

    assert session.messages == ["第一问", "第二问"]
    assert output.getvalue().count("你> ") == 3
    assert error.getvalue() == ""


def test_missing_terminal_editor_stops_before_reading_input_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()

    class Runtime(_FakeRuntime):
        closed = False

        def close(self) -> None:
            self.closed = True

    runtime = Runtime(session)
    source = _TTYStringIO("不应提交\n")
    output = _TTYStringIO()
    error = io.StringIO()
    monkeypatch.setattr(cli, "_new_runtime", lambda: runtime)
    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", error)
    monkeypatch.setitem(sys.modules, "readline", None)

    assert cli.main([]) == 1

    assert session.messages == []
    assert source.tell() == 0
    assert runtime.closed
    assert "缺少终端行编辑支持" in error.getvalue()


@pytest.mark.parametrize("failure", [False, True])
def test_interactive_text_flushes_once_and_preserves_verified_outputs(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    source, output, error = _TTYStringIO(), _TTYStringIO(), io.StringIO()

    class Runtime:
        def handle(
            self,
            message: str,
            *,
            on_progress: Callable[[str], None] | None = None,
            on_text: Callable[[str], None] | None = None,
        ) -> AgentTurn:
            assert message == "合成测试\n"
            assert on_progress is not None and on_text is not None
            on_text("中文🙂")
            assert output.getvalue().endswith("模型> 中文🙂")
            if not failure:
                on_progress("工具已完成。")
                assert output.getvalue().endswith("中文🙂\n工具已完成。\n")
                on_text("最终正文")
            return AgentTurn(
                outputs=("模型> 中文🙂最终正文", "程序核验：结果已保存。"),
                errors=("模型调用失败。",) if failure else (),
                should_exit=True,
                text_streamed=True,
            )

    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr("builtins.input", lambda prompt: "合成测试")
    monkeypatch.setitem(sys.modules, "readline", object())
    assert cli._run_repl(Runtime(), input_stream=source, output=output, error=error) == 0
    rendered = output.getvalue()
    assert rendered.count("模型> ") == 1
    assert rendered.count("中文🙂") == 1
    assert rendered.count("最终正文") == (0 if failure else 1)
    assert "程序核验：结果已保存。" in rendered
    assert ("本次模型回答未完整生成。" in rendered) is failure
    assert error.getvalue() == ("错误：模型调用失败。\n" if failure else "")


@pytest.mark.parametrize("interactive", [True, False])
def test_completed_report_and_explanation_are_each_displayed_once(
    monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    source = _TTYStringIO() if interactive else io.StringIO("确认\n")
    output = _TTYStringIO() if interactive else io.StringIO()
    error = io.StringIO()
    report = "优化结果：推荐调整已保存。"
    explanation = "仅根据已核验结果解释。"

    class Runtime:
        def handle(
            self,
            message: str,
            *,
            on_progress: Callable[[str], None] | None = None,
            on_text: Callable[[str], None] | None = None,
        ) -> AgentTurn:
            assert message == "确认\n"
            if interactive:
                assert on_progress is not None and on_text is not None
                on_progress(report)
                assert output.getvalue().endswith(report + "\n")
                assert explanation not in output.getvalue()
                on_text(explanation)
            else:
                assert on_progress is None and on_text is None
            return AgentTurn(
                outputs=(report, "模型> " + explanation),
                should_exit=True,
                text_streamed=interactive,
                streamed_outputs=(report,),
            )

    if interactive:
        monkeypatch.setattr(sys, "stdin", source)
        monkeypatch.setattr(sys, "stdout", output)
        monkeypatch.setattr("builtins.input", lambda prompt: "确认")
        monkeypatch.setitem(sys.modules, "readline", object())

    assert cli._run_repl(Runtime(), input_stream=source, output=output, error=error) == 0
    rendered = output.getvalue()
    assert rendered.count(report) == rendered.count(explanation) == 1
    assert rendered.index(report) < rendered.index("模型> " + explanation)
    assert error.getvalue() == ""


def test_startup_recovery_is_displayed_without_dispatch_or_resume() -> None:
    source, output, error = io.StringIO(), io.StringIO(), io.StringIO()

    class Runtime:
        starts = 0

        def startup(self) -> tuple[str, ...]:
            self.starts += 1
            return ("已恢复本机会话。", "任务已批准但未完成；输入 /resume 后继续。")

        def handle(self, message: str, **kwargs: Any) -> AgentTurn:
            pytest.fail("startup must not dispatch a model request or approved task")

    runtime = Runtime()
    assert cli._run_repl(runtime, input_stream=source, output=output, error=error) == 0
    assert runtime.starts == 1
    assert output.getvalue().startswith(
        "已恢复本机会话。\n任务已批准但未完成；输入 /resume 后继续。\n"
    )
    assert error.getvalue() == ""


def test_session_startup_error_is_not_reported_as_dmx_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise SessionError("session-in-use", "本机会话已被另一进程打开。")

    monkeypatch.setattr(cli, "_new_runtime", fail)
    assert cli.main([]) == 1
    result = capsys.readouterr()
    assert "本机会话已被另一进程打开。" in result.err
    assert "DMX配置" not in result.err


@pytest.mark.parametrize("failure_stage", ["store", "agent"])
def test_runtime_startup_failure_closes_transport_and_releases_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    from petroleum_rto.assistant import native_tools, react
    from petroleum_rto.domain_model import chat_settings, native
    from petroleum_rto.domain_model.models import DEFAULT_MODEL_ID

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_settings,
        "load_dmx_chat_settings",
        lambda: SimpleNamespace(
            url="https://test.invalid/v1/chat/completions",
            api_key="fake-private-api-key",
            model=DEFAULT_MODEL_ID,
            system_prompt=None,
        ),
    )
    monkeypatch.setattr(native_tools, "AgentDomainTools", lambda path: object())
    original_transport = native.NativeTransport
    transports: list[native.NativeTransport] = []

    def make_transport(*args: Any, **kwargs: Any) -> native.NativeTransport:
        transport = original_transport(*args, **kwargs)
        transports.append(transport)
        return transport

    monkeypatch.setattr(native, "NativeTransport", make_transport)
    session_path = tmp_path / "runs/assistant/session.sqlite"

    def fail_agent(model: native.DmxNativeModel, tools: Any, **kwargs: Any) -> None:
        assert model.use_stream is True
        assert kwargs["store"].path == session_path
        raise SessionError("invalid-session-data", "本机会话内容不能恢复。")

    monkeypatch.setattr(react, "ReactAgent", fail_agent)
    held_store = SessionStore(session_path) if failure_stage == "store" else None
    try:
        with pytest.raises(SessionError):
            cli._new_runtime()
        assert len(transports) == 1
        assert transports[0]._client.is_closed
        assert transports[0].request_count == 0
    finally:
        if held_store:
            held_store.close()
    with SessionStore(session_path):
        pass  # Neither failure path leaves a session lock behind.
