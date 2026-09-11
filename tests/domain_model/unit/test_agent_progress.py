from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from langchain_core.messages import ToolMessage
from test_native_protocol import Wire, call, chat
from test_optimization_tools import arguments, prepared

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.rto.progress import RtoProgress


def test_tool_progress_is_visible_before_the_tool_returns(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = Event()
    domain = AgentDomainTools(repo_root)
    original = domain.operating_context

    def read(state: dict[str, Any]) -> dict[str, Any]:
        assert observed.wait(2), "tool start was buffered until completion"
        return original(state)

    monkeypatch.setattr(domain, "operating_context", read)
    wire = Wire([chat(None, calls=[call("read_operating_context")]), chat("已读取工况")])
    runtime = ReactAgent(wire.model(), domain)
    progress: list[str] = []

    def report(text: str) -> None:
        progress.append(text)
        if "正在读取配置工况" in text:
            observed.set()

    turn = runtime.handle("读取工况", on_progress=report)
    assert not turn.errors
    assert progress == ["进度：正在读取配置工况。", "进度：读取配置工况完成。"]
    assert turn.outputs == ("模型> 已读取工况",)
    assert len([m for m in runtime.messages if isinstance(m, ToolMessage)]) == 1
    assert "进度：" not in json.dumps(wire.requests, ensure_ascii=False)
    assert not runtime.handle("/help", on_progress=progress.append).errors
    assert len(progress) == 2
    runtime.close()


def test_parallel_query_batch_still_executes_serially_with_ordered_events(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain = AgentDomainTools(repo_root)
    active: list[str] = []
    entered = Event()
    progress: list[str] = []

    def query(name: str) -> dict[str, Any]:
        assert not active, "domain tools overlapped"
        active.append(name)
        assert entered.wait(2)
        active.pop()
        return {"status": "ok"}

    monkeypatch.setattr(domain, "plant_info", lambda state: query("plant"))
    monkeypatch.setattr(domain, "operating_context", lambda state: query("context"))
    wire = Wire(
        [
            chat(
                None, calls=[call("get_plant_info"), call("read_operating_context", call_id="c2")]
            ),
            chat("查询完成"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)

    def report(text: str) -> None:
        progress.append(text)
        entered.set()

    assert not runtime.handle("查询", on_progress=report).errors
    assert len(progress) == 4
    assert "正在" in progress[0] and "完成" in progress[1]
    assert "正在" in progress[2] and "完成" in progress[3]
    assert len([m for m in runtime.messages if isinstance(m, ToolMessage)]) == 2
    runtime.close()


def test_prepare_progress_never_claims_calculation_and_schema_error_never_claims_success(
    repo_root: Path,
) -> None:
    domain = AgentDomainTools(repo_root)
    args = arguments(domain)
    wire = Wire(
        [
            chat(None, calls=[call("read_operating_context", call_id="context")]),
            chat(None, calls=[call("prepare_optimization", json.dumps(args))]),
            chat("方案已准备"),
            chat(
                None,
                calls=[call("prepare_optimization", '{"secret-field":"sensitive-value"}', "c2")],
            ),
            chat("参数需要修正"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    progress: list[str] = []
    first = runtime.handle("准备方案", on_progress=progress.append)
    assert not first.errors
    assert progress == [
        "进度：正在读取配置工况。",
        "进度：读取配置工况完成。",
        "进度：正在准备优化方案。",
        "进度：准备优化方案完成，未运行仿真。",
    ]
    assert runtime.data["pending"]["status"] == "awaiting_confirmation"
    progress.clear()
    runtime.handle("修改方案", on_progress=progress.append)
    assert len(progress) == 1 and "失败" in progress[0]
    assert "sensitive" not in progress[0] and "secret" not in progress[0]
    assert runtime.data["pending"]["status"] == "revision_required"
    runtime.close()


@pytest.mark.parametrize("confirmation", ["/confirm", "确认", "确认执行"])
@pytest.mark.parametrize("outcome", ["completed", "no_feasible", "error"])
def test_every_confirmation_input_forwards_fixed_stage_progress_once_and_keeps_result(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, confirmation: str, outcome: str
) -> None:
    wire = Wire(
        [
            chat("结果已保存，说明仅依据核验报告。"),
            *[httpx.ConnectError("private-error") for _ in range(3)],
        ]
    )
    runtime = prepared(repo_root, wire=wire)
    preparation_requests = len(wire.requests)
    stage_calls: list[str] = []
    static = {"status": "static_complete", "static_ref": "static-1"}
    status = {
        "completed": "feasible_not_publishable",
        "no_feasible": "no_verified_candidate",
        "error": "evaluation_error",
    }[outcome]
    final = {"status": "complete", "result": {"status": status}}

    def solve(plan: Any, **kwargs: Any) -> dict[str, Any]:
        stage_calls.append("m2")
        report = kwargs["on_progress"]
        for event in [
            RtoProgress("m2", "started"),
            RtoProgress("m2", "progress", 1),
            RtoProgress("m2", "completed", 1, 1),
        ]:
            report(event)
        return dict(static)

    def verify(plan: Any, **kwargs: Any) -> dict[str, Any]:
        stage_calls.append("m4")
        report = kwargs["on_progress"]
        for event in [
            RtoProgress("m4", "started", 0, 1),
            RtoProgress("m4", "progress", 1, 1),
            RtoProgress("m4", outcome, 1, 1),
        ]:
            report(event)
        return dict(final)

    monkeypatch.setattr(react, "solve_prepared_optimization", solve)
    monkeypatch.setattr(react, "verify_prepared_optimization", verify)
    monkeypatch.setattr(react, "read_prepared_static", lambda *a, **kw: dict(static))
    monkeypatch.setattr(react, "read_prepared_result", lambda *a, **kw: dict(final))
    progress: list[str] = []
    result = runtime.handle(confirmation, on_progress=progress.append)
    assert stage_calls == ["m2", "m4"]
    stage_progress = [p for p in progress if "M2" in p or "M4" in p]
    assert len(stage_progress) == 6 and len(set(stage_progress)) == 6
    assert "1个搜索点" in stage_progress[1] and "1/1个入围候选" in stage_progress[4]
    assert ("核验结果已就绪" in stage_progress[-1]) == (outcome == "completed")
    assert "优化结果：" in "".join(result.outputs)
    assert len(wire.requests) == preparation_requests + 1 and not result.errors
    assert not wire.requests[-1].get("tools")
    assert runtime.data["pending"]["status"] == "completed"
    explanation = runtime.handle("解释刚才的核验结果", on_progress=progress.append)
    assert explanation.errors  # A later explanation failure must not erase the fixed result.
    assert len(wire.requests) == preparation_requests + 4 and not wire.replies
    assert "private-error" not in "".join(explanation.errors)
    assert runtime.data["last_result"] == final and stage_calls == ["m2", "m4"]
    count = len(wire.requests)
    progress.clear()
    assert not runtime.handle("/confirm", on_progress=progress.append).errors
    assert stage_calls == ["m2", "m4"] and len(wire.requests) == count
    assert len(progress) == 1 and "复用" in progress[0] and "未重新计算" in progress[0]
    runtime.close()


def test_stage_failure_does_not_repeat_on_followup_and_explicit_resume_reports_again(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire([chat("方案尚未完成，已批准的范围保持不变。")])
    runtime = prepared(repo_root, wire=wire)
    stage_calls: list[str] = []

    def solve(plan: Any, **kwargs: Any) -> dict[str, Any]:
        stage_calls.append("m2")
        kwargs["on_progress"](RtoProgress("m2", "error"))
        raise ValueError("private-evidence-path")

    monkeypatch.setattr(react, "solve_prepared_optimization", solve)
    progress: list[str] = []
    first = runtime.handle("确认", on_progress=progress.append)
    assert first.errors and stage_calls == ["m2"]
    assert not runtime.handle("刚才进行到哪里了？", on_progress=progress.append).errors
    assert stage_calls == ["m2"]
    assert runtime.handle("/confirm", on_progress=progress.append).errors
    assert stage_calls == ["m2"]
    resumed = runtime.handle("/resume", on_progress=progress.append)
    assert resumed.errors and stage_calls == ["m2", "m2"]
    failures = [p for p in progress if "M2" in p]
    assert len(failures) == 2
    assert all("未成功" in failure for failure in failures)
    assert "private-evidence-path" not in "".join([*progress, *first.errors, *resumed.errors])
    assert runtime.data["pending"]["static"] is None
    assert runtime.data["pending"]["status"] == "approved"
    runtime.close()
