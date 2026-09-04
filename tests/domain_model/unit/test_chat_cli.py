from __future__ import annotations

import io

import pytest

from petroleum_rto.assistant import cli
from petroleum_rto.assistant.runtime import HELP, AgentTurn


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

    def handle(self, message: str) -> AgentTurn:
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
    assert "/result <结果编号|目录|result.json>" in captured.out
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
