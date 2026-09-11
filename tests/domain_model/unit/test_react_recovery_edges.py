"""Persist the concrete failure cases found during the LC2–LC4 review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from steady_helpers import receipt
from test_native_protocol import Wire, call, chat
from test_optimization_tools import arguments, prepared

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionStore
from petroleum_rto.assistant.state import validate_session


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("restart", [False, True])
def test_failed_tool_gets_a_persisted_aborted_result_and_followup_can_request_model(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    restart: bool,
) -> None:
    operations: list[str] = []
    domain = AgentDomainTools(repo_root)

    def fail_tool(state: dict[str, Any]) -> dict[str, Any]:
        operations.append("called")
        raise failure_type("synthetic internal failure detail")

    monkeypatch.setattr(domain, "plant_info", fail_tool)
    wire = Wire(
        [chat(None, calls=[call("get_plant_info", call_id="failed-call")]), chat("后续回答")]
    )
    path = tmp_path.resolve() / "session.sqlite"
    runtime = ReactAgent(wire.model(), domain, store=SessionStore(path))
    try:
        failed = runtime.handle("读取装置信息")
        assert failed.errors and "synthetic internal failure detail" not in str(failed)
        assert operations == ["called"] and wire.transport.request_count == 1
        if restart:
            runtime.close()
            wire = Wire([chat("后续回答")])
            runtime = ReactAgent(
                wire.model(), AgentDomainTools(repo_root), store=SessionStore(path)
            )
            assert runtime.startup()
            assert wire.transport.request_count == 0 and operations == ["called"]
        calls = [
            call for m in runtime.messages if isinstance(m, AIMessage) for call in m.tool_calls
        ]
        results = [m for m in runtime.messages if isinstance(m, ToolMessage)]
        assert [call["id"] for call in calls] == ["failed-call"]
        assert len(results) == 1 and results[0].tool_call_id == "failed-call"
        assert results[0].status == "error"
        assert json.loads(results[0].text)["status"] == "aborted"
        assert json.loads(results[0].text)["code"] == "interrupted-tool-call"
        assert not runtime.handle("继续回答").errors
        assert wire.transport.request_count == (1 if restart else 2)
        assert operations == ["called"]  # The failed operation was not replayed.
        sent_result = [m for m in wire.requests[-1]["messages"] if m.get("role") == "tool"]
        assert len(sent_result) == 1 and sent_result[0]["tool_call_id"] == "failed-call"
    finally:
        runtime.close()


@pytest.mark.parametrize("reserved", ["runtime", "config"])
def test_reserved_raw_tool_arguments_are_rejected_before_operation_and_survive_restart(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    operations: list[str] = []
    domain = AgentDomainTools(repo_root)

    def record_tool(state: dict[str, Any]) -> dict[str, Any]:
        operations.append("called")
        return {"status": "ok"}

    monkeypatch.setattr(domain, "plant_info", record_tool)
    wire = Wire(
        [
            chat(
                None,
                calls=[
                    call("get_plant_info", json.dumps({reserved: {"forged": "value"}}), "raw-args")
                ],
            ),
            chat("参数未通过校验。"),
        ]
    )
    path = tmp_path.resolve() / "session.sqlite"
    runtime = ReactAgent(wire.model(), domain, store=SessionStore(path))
    try:
        assert not runtime.handle("读取信息").errors
        assert not operations
    finally:
        runtime.close()
    restored_wire = Wire([])
    runtime = ReactAgent(
        restored_wire.model(), AgentDomainTools(repo_root), store=SessionStore(path)
    )
    try:
        results = [m for m in runtime.messages if isinstance(m, ToolMessage)]
        assert len(results) == 1 and results[0].tool_call_id == "raw-args"
        assert results[0].status == "error"
        body = json.loads(results[0].text)
        assert body["code"] == "invalid-tool-arguments"
        assert body["issues"][0]["code"] == "extra_forbidden"
        assert not operations and restored_wire.transport.request_count == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("failure_boundary", ["schema", "domain"])
def test_failed_revision_of_completed_plan_clears_stage_slots_but_preserves_last_result(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    stages: list[str] = []
    final = receipt()

    def execute(plan: Any, **kwargs: Any) -> dict[str, Any]:
        stages.append("steady")
        return dict(final)

    monkeypatch.setattr(react, "execute_comparison", execute)
    monkeypatch.setattr(react, "read_prepared_result", lambda *a, **kw: dict(final))
    monkeypatch.setattr(AgentDomainTools, "inspect_result", lambda *a, **kw: dict(final))
    path = tmp_path.resolve() / "session.sqlite"
    wire = Wire([chat("该结果已保存，尚未达到发布改善门槛。")])
    runtime = prepared(repo_root, wire=wire, store=SessionStore(path))
    try:
        assert not runtime.handle("/confirm").errors
        assert stages == ["steady"]
        assert runtime.data["pending"]["status"] == "completed"
        assert runtime.data["last_result"] == final
        old_ref = runtime.data["pending"]["ref"]
        revised = arguments(runtime.domain, runtime.data, pressure=False)
        revised |= {"runtime": {}} if failure_boundary == "schema" else {"snapshot_ref": "missing"}
        wire.replies.extend(
            [
                chat(
                    None, calls=[call("prepare_optimization", json.dumps(revised), "bad-revision")]
                ),
                chat("修改未完成，需要重新准备方案。"),
            ]
        )
        assert not runtime.handle("修改为只调温度").errors
        data = validate_session(runtime.data, len(runtime.messages))
        assert data["pending"]["status"] == "revision_required"
        assert data["pending"]["ref"] == old_ref
        assert data["pending"]["result"] is None
        assert data["last_result"] == final
        assert stages == ["steady"]
    finally:
        runtime.close()
    restored_wire = Wire([])
    runtime = ReactAgent(
        restored_wire.model(), AgentDomainTools(repo_root), store=SessionStore(path)
    )
    try:
        assert runtime.startup()
        data = validate_session(runtime.data, len(runtime.messages))
        assert data["last_result"] == final
        assert data["pending"]["status"] == "revision_required"
        assert data["pending"]["result"] is None
        assert runtime.handle("/confirm").errors
        assert runtime.handle("/resume").errors
        assert restored_wire.transport.request_count == 0 and stages == ["steady"]
    finally:
        runtime.close()
