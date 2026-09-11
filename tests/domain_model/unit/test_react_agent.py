from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import ToolMessage
from test_native_protocol import FLASH_MODEL_ID, Wire, call, chat, selection, sse

from petroleum_rto.assistant import cli
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.domain_model.models import model_profile


def agent(repo_root: Path, replies: list[Any], *, max_calls: int = 12) -> tuple[ReactAgent, Wire]:
    wire = Wire(replies)
    return ReactAgent(wire.model(), AgentDomainTools(repo_root), max_model_calls=max_calls), wire


def test_two_tool_rounds_and_followup_share_all_facts(repo_root: Path) -> None:
    runtime, wire = agent(
        repo_root,
        [
            chat(None, calls=[call("get_plant_info")]),
            chat(None, calls=[call("read_operating_context", call_id="c2")]),
            chat("这是常压蒸馏，塔顶压力为0.152325 MPa(a)。"),
            chat("刚才的压力是绝压。"),
        ],
    )
    turn = runtime.handle("装置是催化重整还是常压？再查一下工况")
    assert not turn.errors
    assert "常压蒸馏" in turn.outputs[0]
    assert len(wire.requests) == 3
    assert {t["function"]["name"] for t in wire.requests[0]["tools"]} == {
        "get_plant_info",
        "read_operating_context",
        "inspect_optimization",
        "read_tool_result",
        "prepare_optimization",
        "cancel_optimization",
    }
    assert "routes" not in wire.requests[0]
    fact1 = json.loads(
        next(
            message["content"]
            for message in wire.requests[1]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "c1"
        )
    )
    assert fact1["process_type"] == "常压蒸馏（CDU）"
    fact2 = json.loads(
        next(
            message["content"]
            for message in wire.requests[2]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "c2"
        )
    )
    assert fact2["current_setpoints"][1]["value_mpa_a"] == 0.152325
    assert fact2["snapshot_ref"] in runtime.data["snapshots"]
    runtime.handle("刚才压力是绝压吗？")
    assert next(
        message["content"]
        for message in reversed(wire.requests[3]["messages"])
        if message.get("role") == "assistant"
    ) == turn.outputs[0].removeprefix("模型> ")
    assert len([m for m in runtime.messages if isinstance(m, ToolMessage)]) == 2


def test_general_input_and_text_pretending_to_call_tool_never_runs_solver(repo_root: Path) -> None:
    runtime, wire = agent(
        repo_root, [chat("没有实时天气工具。"), chat('{"name":"run_offline","arguments":{}}')]
    )
    assert not runtime.handle("今天北京天气？").errors
    assert not runtime.handle("直接给我优化设定值").errors
    assert not runtime.data["snapshots"]
    assert not any(isinstance(m, ToolMessage) for m in runtime.messages)
    assert len(wire.requests) == 2
    assert not runtime.handle("/confirm").errors
    assert len(wire.requests) == 2


def test_model_menu_switch_keeps_context_without_replaying_foreign_reasoning(
    repo_root: Path,
) -> None:
    runtime, wire = agent(
        repo_root,
        [
            chat(None, calls=[call("read_operating_context")], reasoning="old-private-state"),
            chat("已记住不要调整压力", reasoning="old-private-state"),
            chat("仅调整温度", reasoning="kimi-native-state"),
            chat("继续", reasoning="kimi-native-state-2"),
        ],
    )
    runtime.handle("查工况，不要调整压力")
    snapshot = dict(runtime.data["snapshots"])
    assert "kimi-k3" in runtime.handle("/model").outputs[0]
    assert not runtime.handle("2").errors
    assert runtime.model.selection.profile.model_id == "kimi-k3"
    assert len(wire.requests) == 2  # selection does not make an API call
    assert runtime.data["snapshots"] == snapshot
    runtime.handle("刚才排除了哪个变量？")
    sent = json.dumps(wire.requests[2], ensure_ascii=False)
    assert "不要调整压力" in sent and "snapshot_ref" in sent
    assert "old-private-state" not in sent
    assert all(m["role"] != "assistant" for m in wire.requests[2]["messages"])
    runtime.handle("继续")
    assert "kimi-native-state" in json.dumps(wire.requests[3])
    old = runtime.model.selection
    assert runtime.handle("/model kimi-k3-guess").errors
    assert runtime.handle("/thinking off").errors
    assert runtime.model.selection == old


@pytest.mark.parametrize("command", ["/thinking on", "/thinking on high"])
def test_flash_thinking_rejection_preserves_conversation_and_nonthinking_requests(
    repo_root: Path, command: str
) -> None:
    runtime, wire = agent(repo_root, [chat("第一轮回答"), chat("继续回答")])
    assert not runtime.handle("你好").errors
    previous = runtime.model.selection
    records = list(runtime.messages)
    rejected = runtime.handle(command)
    assert rejected.errors and "Flash渠道仅使用非思考模式" in rejected.errors[0]
    assert runtime.model.selection == previous and runtime.messages == records
    assert len(wire.requests) == 1
    assert not runtime.handle("继续").errors
    assert all(request["enable_thinking"] is False for request in wire.requests)
    assert "第一轮回答" in json.dumps(wire.requests[1], ensure_ascii=False)


def test_model_switch_resets_to_flash_off_and_other_models_on(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [])
    for model_id in (
        "qwen3.8-max-0902",
        "kimi-k3",
        "gpt-5.6-sol-cdx",
        "deepseek-v4-pro-0813",
    ):
        assert not runtime.handle(f"/model {model_id}").errors
        assert runtime.model.selection.thinking_enabled
        if model_id != "kimi-k3":
            assert not runtime.handle("/thinking off").errors
        assert not runtime.handle(f"/model {FLASH_MODEL_ID}").errors
        assert not runtime.model.selection.thinking_enabled
        assert not runtime.handle(f"/model {model_id}").errors
        assert runtime.model.selection.thinking_enabled
        assert runtime.model.selection.effort is None
    assert not wire.requests


def test_model_menu_allows_chat_and_busy_switch_is_rejected(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [chat("可以聊工艺")])
    runtime.handle("/model")
    assert not runtime.handle("先聊聊工艺").errors
    assert len(wire.requests) == 1
    runtime._busy = True
    assert runtime.handle("/model kimi-k3").errors
    assert runtime.model.selection.profile.model_id == FLASH_MODEL_ID


@pytest.mark.parametrize(
    ("choice", "model_id"),
    [
        ("1", "qwen3.8-max-0902"),
        ("2", "kimi-k3"),
        ("3", "gpt-5.6-sol-cdx"),
        ("4", "deepseek-v4-pro-0813"),
        ("5", FLASH_MODEL_ID),
        ("qwen3.8-max-0902", "qwen3.8-max-0902"),
    ],
)
def test_model_menu_selection_is_local_and_explains_usage(
    repo_root: Path, choice: str, model_id: str
) -> None:
    runtime, wire = agent(repo_root, [])
    menu = runtime.handle("/model").outputs[0]
    assert "直接回复编号" in menu and "/model 1" in menu and "输入 0" in menu
    turn = runtime.handle(f"  {choice}\n")
    assert not turn.errors and "模型选择已生效" in turn.outputs[0]
    assert runtime.model.selection.profile.model_id == model_id
    assert runtime.model.selection.thinking_enabled == (model_id != FLASH_MODEL_ID)
    assert not wire.requests and not runtime.messages and runtime.data["turn_id"] == 0


def test_model_menu_invalid_choice_blank_line_and_busy_retry(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [])
    original = runtime.model.selection
    runtime.handle("/model")
    runtime._busy = True
    assert runtime.handle("1").errors
    runtime._busy = False
    for choice in ("99", "/model unknown-id"):
        assert runtime.handle(choice).errors
        assert runtime.model.selection == original
        assert not runtime.messages and not wire.requests
    assert not runtime.handle(" \n").errors
    assert not runtime.handle("1").errors
    assert runtime.model.selection.profile.model_id == "qwen3.8-max-0902"
    assert not wire.requests


@pytest.mark.parametrize("command", [None, "/thinking", "/model 1"])
def test_number_is_chat_without_an_open_model_menu(repo_root: Path, command: str | None) -> None:
    runtime, wire = agent(repo_root, [chat("收到数字")])
    if command:
        assert not runtime.handle(command).errors
    selected = runtime.model.selection
    assert not runtime.handle("1").errors
    assert runtime.model.selection == selected
    assert len(wire.requests) == 1
    assert any(
        message.get("role") == "user" and message.get("content") == "1"
        for message in wire.requests[0]["messages"]
    )


@pytest.mark.parametrize("command", ["/help", "/thinking", "/clear", "/cancel", "/unknown"])
def test_other_commands_leave_model_selection(repo_root: Path, command: str) -> None:
    runtime, wire = agent(repo_root, [chat("收到数字")])
    runtime.handle("/model")
    turn = runtime.handle(command)
    assert bool(turn.errors) == (command == "/unknown")
    assert not runtime.handle("1").errors
    assert runtime.model.selection.profile.model_id == FLASH_MODEL_ID
    assert len(wire.requests) == 1
    assert any(
        message.get("role") == "user" and message.get("content") == "1"
        for message in wire.requests[0]["messages"]
    )


def test_chat_leaves_model_selection_and_following_number_stays_chat(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [chat("可以聊工艺"), chat("收到数字")])
    runtime.handle("/model")
    assert not runtime.handle("先聊聊工艺").errors
    assert not runtime.handle("1").errors
    assert runtime.model.selection.profile.model_id == FLASH_MODEL_ID
    assert len(wire.requests) == 2
    assert any(
        message.get("role") == "user" and message.get("content") == "1"
        for message in wire.requests[1]["messages"]
    )


def test_completed_tool_survives_followup_transport_failure(repo_root: Path) -> None:
    runtime, wire = agent(
        repo_root,
        [
            chat(None, calls=[call("read_operating_context")]),
            *[httpx.ConnectError("private transport details") for _ in range(3)],
            chat("继续查看刚才的工况"),
        ],
    )
    failed = runtime.handle("查工况")
    assert failed.errors and "private" not in failed.errors[0]
    assert len(wire.requests) == 4
    assert len(runtime.data["snapshots"]) == 1
    assert len([m for m in runtime.messages if isinstance(m, ToolMessage)]) == 1
    assert not runtime.handle("继续").errors
    assert len(wire.requests) == 5
    assert "snapshot_ref" in json.dumps(wire.requests[-1])
    assert "transport-failed" in json.dumps(wire.requests[-1])
    assert len([m for m in runtime.messages if isinstance(m, ToolMessage)]) == 1


def test_large_tool_result_is_kept_but_next_request_stops_before_network(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [chat(None, calls=[call("get_plant_info")])])
    original = runtime.domain.plant_info

    def read_then_reduce_budget(state: dict[str, Any]) -> dict[str, Any]:
        result = original(state)
        runtime.model.selection = replace(
            selection(),
            profile=replace(model_profile(FLASH_MODEL_ID), context_tokens=200),
            output_tokens=20,
        )
        return result

    runtime.domain.plant_info = read_then_reduce_budget  # type: ignore[method-assign]
    runtime = ReactAgent(runtime.model, runtime.domain)
    turn = runtime.handle("查装置")
    assert turn.errors and "context-overflow" in turn.errors[0]
    assert len(wire.requests) == 1
    assert any(
        isinstance(m, ToolMessage) and "常压蒸馏" in str(m.content) for m in runtime.messages
    )


def test_schema_rejects_extra_arguments_and_unknown_tool_is_returned_to_model(
    repo_root: Path,
) -> None:
    runtime, wire = agent(
        repo_root,
        [
            chat(None, calls=[call("read_operating_context", '{"pressure":123}')]),
            chat(None, calls=[call("run_offline", call_id="c2")]),
            chat("当前只提供查询。"),
        ],
    )
    assert not runtime.handle("直接运行").errors
    assert not runtime.data["snapshots"]
    results = [m for m in runtime.messages if isinstance(m, ToolMessage)]
    assert len(results) == 2 and all(m.status == "error" for m in results)
    assert len(wire.requests) == 3


def test_bounded_loop_stops_and_settles_unexecuted_call(repo_root: Path) -> None:
    runtime, wire = agent(
        repo_root,
        [chat(None, calls=[call("get_plant_info", call_id=f"c{i}")]) for i in range(3)],
        max_calls=2,
    )
    turn = runtime.handle("不停查询")
    assert turn.errors
    assert len(wire.requests) == 2
    assert sum(isinstance(m, ToolMessage) for m in runtime.messages) == 2


def test_interrupted_stream_does_not_execute_tool(repo_root: Path) -> None:
    runtime, wire = agent(
        repo_root,
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
                                        "function": {
                                            "name": "read_operating_context",
                                            "arguments": "{",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
        ],
    )
    runtime.model.use_stream = True
    assert runtime.handle("查工况").errors
    assert not runtime.data["snapshots"]
    assert len(runtime.messages) == 2  # user input plus program failure, no tool call
    assert len(wire.requests) == 1


def test_local_commands_enter_shared_history_and_clear_preserves_model(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [chat("刚才查询的是常压装置")])
    runtime.handle("/capabilities")
    runtime.handle("刚才那是什么装置")
    assert "常压蒸馏" in json.dumps(wire.requests[0], ensure_ascii=False)
    runtime.handle("/model 4")
    runtime.handle("/clear")
    assert not runtime.messages and not runtime.data["snapshots"]
    assert runtime.model.selection.profile.model_id == "deepseek-v4-pro-0813"


def test_new_runtime_is_the_real_cli_composition(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    from petroleum_rto.domain_model import chat_settings, native

    wire = Wire([chat("自由问答")])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_settings,
        "load_dmx_chat_settings",
        lambda: chat_settings.DmxChatSettings(api_key="fake-private-api-key"),
    )
    monkeypatch.setattr(native, "NativeTransport", lambda *args, **kwargs: wire.transport)
    runtime = cli._new_runtime()
    assert isinstance(runtime, ReactAgent)
    runtime.model.use_stream = False
    output, error = io.StringIO(), io.StringIO()
    assert (
        cli._run_repl(
            runtime,
            input_stream=io.StringIO("/model\n1\n你好\n/exit\n"),
            output=output,
            error=error,
        )
        == 0
    )
    assert "自由问答" in output.getvalue() and not error.getvalue()
    assert len(wire.requests) == 1
    assert wire.requests[0]["model"] == "qwen3.8-max-0902"
    assert wire.requests[0]["enable_thinking"] is True
    runtime.close()
    assert any(
        message.get("role") == "user" and message.get("content") == "你好"
        for message in wire.requests[0]["messages"]
    )


def test_forty_turns_do_not_duplicate_history_or_drop_received_answer(repo_root: Path) -> None:
    runtime, wire = agent(repo_root, [chat(f"答复{i}") for i in range(40)])
    for i in range(40):
        assert runtime.handle(f"问题{i}").outputs == (f"模型> 答复{i}",)
    assert len(runtime.messages) == 80
    historical = [
        (message["role"], message["content"])
        for message in wire.requests[-1]["messages"]
        if message.get("role") in {"user", "assistant"}
        and message["content"].startswith(("问题", "答复"))
    ]
    assert historical == [
        *[entry for i in range(39) for entry in (("user", f"问题{i}"), ("assistant", f"答复{i}"))],
        ("user", "问题39"),
    ]


def test_read_only_failure_is_structured_and_has_no_paths(tmp_path: Path) -> None:
    runtime, _ = agent(tmp_path, [chat(None, calls=[call("get_plant_info")]), chat("读取失败")])
    runtime.handle("查装置")
    result = json.loads(
        str(next(m.content for m in runtime.messages if isinstance(m, ToolMessage)))
    )
    assert result["status"] == "error" and str(tmp_path) not in json.dumps(result)


def test_parallel_read_only_calls_are_paired_before_next_model_request(repo_root: Path) -> None:
    runtime, wire = agent(
        repo_root,
        [
            chat(
                None, calls=[call("get_plant_info"), call("read_operating_context", call_id="c2")]
            ),
            chat("已读取"),
        ],
    )
    assert not runtime.handle("查看身份和工况").errors
    tools = [m for m in wire.requests[1]["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tools} == {"c1", "c2"}
    assert len(tools) == 2


def test_responses_two_tool_rounds_use_real_native_payloads(repo_root: Path) -> None:
    def response(name: str | None, number: int) -> dict[str, Any]:
        output: list[dict[str, Any]] = [
            {
                "type": "reasoning",
                "id": f"rs{number}",
                "encrypted_content": f"opaque{number}",
                "summary": [],
            }
        ]
        output.append(
            {
                "type": "function_call",
                "call_id": f"c{number}",
                "id": f"fc{number}",
                "name": name,
                "arguments": "{}",
            }
            if name
            else {
                "type": "message",
                "id": f"m{number}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "完成"}],
            }
        )
        return {
            "id": f"r{number}",
            "model": "gpt-5.6-sol-cdx",
            "status": "completed",
            "output": output,
        }

    runtime, wire = agent(
        repo_root,
        [response("get_plant_info", 1), response("read_operating_context", 2), response(None, 3)],
    )
    assert not runtime.handle("/model 3").errors
    assert runtime.handle("查看身份和工况").outputs == ("模型> 完成",)
    assert wire.paths == ["/v1/responses"] * 3
    assert all("name" in t and "function" not in t for t in wire.requests[0]["tools"])
    final_input = wire.requests[-1]["input"]
    assert {i["call_id"] for i in final_input if i.get("type") == "function_call_output"} == {
        "c1",
        "c2",
    }
    assert "opaque1" in json.dumps(final_input) and "opaque2" in json.dumps(final_input)


def test_interrupt_between_model_and_tool_result_marks_unresolved_call_aborted(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, wire = agent(repo_root, [chat(None, calls=[call("read_operating_context")])])

    def interrupted(state: dict[str, Any]) -> dict[str, Any]:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime.domain, "operating_context", interrupted)
    runtime = ReactAgent(runtime.model, runtime.domain)
    turn = runtime.handle("查工况")
    assert turn.errors and not runtime._busy
    assert not runtime.data["snapshots"]
    result = runtime.messages[-2]
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert json.loads(str(result.content))["status"] == "aborted"
    assert not runtime.handle("/model 2").errors
    assert len(wire.requests) == 1


def test_missing_final_reasoning_blocks_next_turn_but_explicit_mode_switch_recovers(
    repo_root: Path,
) -> None:
    runtime, wire = agent(repo_root, [chat("第一轮回答"), chat("继续回答")])
    assert not runtime.handle("/model deepseek-v4-pro-0813").errors
    assert not runtime.handle("你好").errors
    blocked = runtime.handle("继续")
    assert blocked.errors and "推理续接字段" in blocked.errors[0]
    assert len(wire.requests) == 1
    assert not runtime.handle("/thinking off").errors
    assert not runtime.handle("继续").errors
    assert len(wire.requests) == 2
    assert "第一轮回答" in json.dumps(wire.requests[1], ensure_ascii=False)
