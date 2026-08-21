from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from petroleum_rto.assistant import cli


class _FakeSession:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.clear_calls = 0

    def ask(self, message: str) -> str:
        self.messages.append(message)
        return f"回复{len(self.messages)}"

    def clear(self) -> None:
        self.clear_calls += 1


def test_repl_shows_replies_and_supports_clear_help_and_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(cli, "_new_session", lambda: session)
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
    assert "对话已清空。" in captured.out
    assert "/result <run-dir|result.json>" in captured.out
    assert "/status" not in captured.out
    assert captured.err == ""


def test_result_displays_safe_summary_then_sends_only_normalized_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    source = tmp_path / "offline-rto-example"
    summary = {
        "status": "success",
        "selected_setpoints": [
            {
                "variable_id": "furnace_temperature_target_k",
                "value": 626.35,
                "unit": "K",
            }
        ],
        "publishable": True,
    }
    loaded: list[str] = []
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(
        cli,
        "_load_rto_result_summary",
        lambda value: loaded.append(value) or summary,
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(f"/result {source}\n这个温度代表什么？\n/exit\n"),
    )

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert loaded == [str(source)]
    assert "不可直接下装" not in captured.out
    assert "未经现场验证" not in captured.out
    assert "边界说明" not in captured.out
    assert "RTO设定值概要：" in captured.out
    assert "626.35" in captured.out
    assert "模型> 回复1" in captured.out
    assert session.messages[1] == "这个温度代表什么？"
    result_prompt = session.messages[0]
    for repeated_notice in ("快照", "合成工程仿真", "未经现场验证", "现场控制权"):
        assert repeated_notice not in result_prompt
    assert "不要输出JSON" in result_prompt
    assert "626.35" in result_prompt
    assert str(source) not in result_prompt
    assert "run_dir" not in result_prompt
    assert "fingerprint" not in result_prompt
    assert captured.err == ""


def test_natural_status_question_displays_only_the_model_explanation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    summary = {
        "state_kind": "configured_simulation_context",
        "simulator_mode": "on_demand_offline",
        "simulator_state": "idle",
        "fresh_feed_load": {"kg_per_s": 113.1388888888889, "t_per_h": 407.3},
        "operating_mode": "normal-steady",
    }
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(cli, "_load_simulation_status", lambda: summary)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("请告诉我当前常压装置工况\n这属于实时数据吗？\n/exit\n"),
    )

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert "本次查询未启动新仿真" not in captured.out
    assert "不是DCS" not in captured.out
    assert "当前仿真基准工况：" not in captured.out
    assert "407.3" not in captured.out
    assert "state_kind" not in captured.out
    assert "claim_scope" not in captured.out
    assert "模型> 回复1" in captured.out
    assert session.messages[1] == "这属于实时数据吗？"
    status_prompt = session.messages[0]
    for repeated_notice in (
        "快照",
        "本次查询没有启动新仿真",
        "不是DCS",
        "未经现场验证",
        "控制权",
    ):
        assert repeated_notice not in status_prompt
    assert "不要输出JSON" in status_prompt
    assert "407.3" in status_prompt
    assert "context_id" not in status_prompt
    assert "fingerprint" not in status_prompt
    assert "feed_composition" not in status_prompt
    assert "当前常压装置工况" in status_prompt
    assert captured.err == ""


def test_compound_status_and_optimization_request_cannot_imply_execution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    summary = {
        "simulator_state": "idle",
        "operating_mode": "normal-steady",
        "fresh_feed_load": {"kg_per_s": 113.1388888888889, "t_per_h": 407.3},
    }
    question = "现在常压装置什是什么状态，以降低能耗，提高利  产率为目标开始调整"
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(cli, "_load_simulation_status", lambda: summary)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{question}\n/exit\n"))

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert "模型> 回复1" in captured.out
    assert "407.3" not in captured.out
    assert len(session.messages) == 1
    prompt = session.messages[0]
    assert "407.3" in prompt
    assert question in prompt
    assert "下一步需要形成优化预览" in prompt
    assert "不得声称已经运行优化" in prompt
    assert captured.err == ""


@pytest.mark.parametrize(
    "question",
    [
        "当前常压装置工况怎么样？",
        "现在常压装置是什么状态？",
        "仿真软件现在是什么运行状态？",
        "现在进料量是多少？",
        "炉温当前值是多少？",
        "塔顶压力读数是多少？",
        "当前设定值是什么？",
        "常压装置工况怎么样？",
        "初始库存比是多少？",
        "进料呢？",
        "仿真软件现在运行吗？",
    ],
)
def test_operating_questions_receive_operating_context(question: str) -> None:
    assert cli._needs_operating_context(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "请介绍常压工况分析方法",
        "为什么炉温会影响能耗",
        "炉温控制原理是什么",
        "如何优化塔顶压力",
        "塔顶压力如何影响分馏",
        "库存模型怎么建立",
        "RTO当前运行状态如何",
        "帮我写一份仿真报告",
    ],
)
def test_non_status_questions_remain_normal_chat(question: str) -> None:
    assert cli._needs_operating_context(question) is False


def test_status_slash_command_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(
        cli,
        "_load_simulation_status",
        lambda: (_ for _ in ()).throw(AssertionError("must not load status")),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("/status\n/exit\n"))

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert session.messages == []
    assert "未知命令" in captured.err


def test_each_operating_question_refreshes_status_and_normal_chat_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    loads = 0

    def _status() -> dict[str, object]:
        nonlocal loads
        loads += 1
        return {"state_kind": "configured_simulation_context"}

    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(cli, "_load_simulation_status", _status)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            "请告诉我当前常压装置工况\n"
            "进料是多少？\n"
            "请介绍常压工况分析方法\n"
            "RTO当前运行状态如何\n"
            "/exit\n"
        ),
    )

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert loads == 2
    assert len(session.messages) == 4
    assert "当前常压装置工况" in session.messages[0]
    assert "进料是多少" in session.messages[1]
    assert session.messages[2:] == ["请介绍常压工况分析方法", "RTO当前运行状态如何"]
    assert captured.err == ""


def test_failed_status_load_never_calls_model_or_leaks_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    secret = "sk-secret-value-must-not-appear"
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(
        cli,
        "_load_simulation_status",
        lambda: (_ for _ in ()).throw(ValueError(secret)),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("目前仿真软件运行状态如何？\n/exit\n"))

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert session.messages == []
    assert secret not in captured.out
    assert secret not in captured.err
    assert "未向模型发送任何内容" in captured.err


def test_failed_result_validation_never_calls_model_or_leaks_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    secret = "sk-secret-value-must-not-appear"
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setattr(
        cli,
        "_load_rto_result_summary",
        lambda _value: (_ for _ in ()).throw(ValueError(secret)),
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(f"/result {tmp_path}\n/exit\n"),
    )

    assert cli.main([]) == 0

    captured = capsys.readouterr()
    assert session.messages == []
    assert secret not in captured.out
    assert secret not in captured.err
    assert "未向模型发送任何内容" in captured.err


def test_model_and_startup_failures_do_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-secret-value-must-not-appear"
    monkeypatch.setattr(
        cli,
        "_new_session",
        lambda: (_ for _ in ()).throw(ValueError(secret)),
    )
    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.err

    class _FailingSession(_FakeSession):
        def ask(self, message: str) -> str:
            raise RuntimeError(secret)

    monkeypatch.setattr(cli, "_new_session", _FailingSession)
    monkeypatch.setattr("sys.stdin", io.StringIO("你好\n/exit\n"))
    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "模型调用失败" in captured.err


def test_result_json_resolves_to_its_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "offline-rto-example"
    run_dir.mkdir()
    result = run_dir / "result.json"
    result.write_text(json.dumps({"status": "test"}), encoding="utf-8")

    assert cli._result_run_dir(str(result)) == run_dir.resolve()
    assert cli._result_run_dir(str(run_dir)) == run_dir.resolve()


def test_cli_rejects_arguments_and_eof_does_not_call_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(cli, "_new_session", lambda: session)

    assert cli.main(["legacy-subcommand"]) == 2
    assert "无需参数" in capsys.readouterr().err

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert session.messages == []
    assert captured.err == ""
