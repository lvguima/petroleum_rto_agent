"""Completed calculation survives the separate, tool-free model explanation."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_native_protocol import Wire, call, chat, sse
from test_native_streaming import delta
from test_optimization_tools import prepared

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.presentation import render_optimization_result
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionStore


@pytest.fixture
def finished_stages(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace only physical execution/readers, retaining real prepare and graph."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    summary = {
        "status": "success",
        "targets": [
            {
                "metric_id": "valuable_distillate_yield",
                "business_name": "有价值馏分收率",
                "sense": "maximize",
                "priority": 1,
                "unit": "mass_fraction",
            }
        ],
        "operating_context": {
            "operating_mode": "normal-steady",
            "fresh_feed_load_kg_s": 100.0,
            "fresh_feed_load_t_per_h": 360.0,
            "data_timestamp": "2026-09-11T08:00:00Z",
            "data_quality": "trusted_synthetic_fixture",
        },
        "baseline_values": [
            {
                "metric_id": "valuable_distillate_yield",
                "value": 0.81234567891,
                "unit": "mass_fraction",
            }
        ],
        "recommended_adjustments": [
            {
                "variable_id": "furnace_temperature_target_k",
                "business_name": "炉出口温度目标",
                "unit": "K",
                "baseline_value": 650.123456789,
                "recommended_value": 651.234567891,
                "adjustment": 1.111111102,
            }
        ],
        "predicted_effects": [
            {
                "metric_id": "valuable_distillate_yield",
                "predicted_value": 0.82345678912,
                "unit": "mass_fraction",
                "directional_improvement": 0.01111111021,
                "relative_improvement": 0.01367781,
            }
        ],
        "alternative_candidates": [],
    }
    final = {
        "status": "complete",
        "workflow_id": "offline-rto-" + "a" * 16,
        "result_source": "offline-rto-" + "a" * 16 + "/result.json",
        "result": summary,
        "physical_m2_executions": 0,
        "physical_m4_executions": 2,
    }
    static = {"status": "static_complete", "static_ref": "synthetic-static-1"}
    stages = SimpleNamespace(final=final, static=static, calls=[], interrupt_m4=False)

    def solve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        stages.calls.append("M2")
        return copy.deepcopy(static)

    def verify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        stages.calls.append("M4")
        if stages.interrupt_m4:
            raise KeyboardInterrupt("synthetic interruption before physical computation")
        return copy.deepcopy(final)

    def inspect(
        self: AgentDomainTools, state: dict[str, Any], requested: str | None = None
    ) -> dict[str, Any]:
        assert requested in {None, final["workflow_id"]}
        if requested is None:
            return {"status": "ok", "task": self.project(state), "result": state["last_result"]}
        return {
            "status": "ok",
            "workflow_id": final["workflow_id"],
            "result": copy.deepcopy(summary),
        }

    monkeypatch.setattr(react, "solve_prepared_optimization", solve)
    monkeypatch.setattr(react, "verify_prepared_optimization", verify)
    monkeypatch.setattr(react, "read_prepared_static", lambda *a, **kw: copy.deepcopy(static))
    monkeypatch.setattr(react, "read_prepared_result", lambda *a, **kw: copy.deepcopy(final))
    monkeypatch.setattr(AgentDomainTools, "inspect_result", inspect)
    return stages


class ExplanationWire(Wire):
    before_explanation: Callable[[], None] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not json.loads(request.content).get("tools") and self.before_explanation:
            self.before_explanation()
        return super().handle(request)


@pytest.mark.parametrize("callback_mode", ["none", "both", "text-only"])
def test_report_is_saved_and_shown_before_same_model_explains_without_tools(
    repo_root: Path, finished_stages: SimpleNamespace, callback_mode: str
) -> None:
    explanation = "本次推荐小幅提高炉温，预测收率改善；仅为合成工程结果。"
    wire = ExplanationWire([sse(delta({"content": explanation}), delta({}, "stop"), "[DONE]")])
    runtime = prepared(repo_root, wire=wire)
    runtime.model.use_stream = True
    before = len(wire.requests)
    report = render_optimization_result(finished_stages.final)
    events: list[tuple[str, str]] = []

    def before_request() -> None:
        assert runtime.data["last_result"] == finished_stages.final
        assert runtime.data["pending"]["status"] == "completed"
        if callback_mode == "both":
            assert ("program", report) in events
        elif callback_mode == "text-only":
            assert report in "".join(text for kind, text in events if kind == "text")
        events.append(("request", "explanation"))

    wire.before_explanation = before_request
    original = copy.deepcopy(finished_stages.final)
    try:
        result = runtime.handle(
            "/confirm",
            on_progress=(lambda text: events.append(("program", text)))
            if callback_mode == "both"
            else None,
            on_text=(lambda text: events.append(("text", text)))
            if callback_mode != "none"
            else None,
        )
        assert not result.errors
        assert result.outputs == (report, "模型> " + explanation)
        assert result.streamed_outputs == ((report,) if callback_mode != "none" else ())
        assert len(wire.requests) == before + 1
        request = wire.requests[-1]
        assert not request.get("tools")
        assert request["model"] == runtime.model.selection.profile.model_id
        assert [m["role"] for m in request["messages"]] == ["system", "user"]
        assert request["messages"][-1]["content"] == report
        assert finished_stages.calls == ["M2", "M4"]
        assert runtime.data["last_result"] == original == finished_stages.final
        assert "650.123456789" not in report and "650.12" in report
        if callback_mode == "both":
            assert events.index(("program", report)) < events.index(("request", "explanation"))
            assert events.index(("request", "explanation")) < events.index(("text", explanation))
        elif callback_mode == "text-only":
            rendered = "".join(text for kind, text in events if kind == "text")
            assert rendered.count(report) == 1
            assert rendered.index("[程序结果]") < rendered.index(report)
            assert (
                rendered.index(report) < rendered.index("[模型说明]") < rendered.index(explanation)
            )
            assert events.index(("request", "explanation")) < events.index(("text", explanation))
    finally:
        runtime.close()


@pytest.mark.parametrize("succeeds", [False, True])
def test_three_explanation_attempts_never_repeat_stages_or_lose_reusable_result(
    repo_root: Path, finished_stages: SimpleNamespace, succeeds: bool
) -> None:
    replies: list[Any] = [
        503,
        httpx.ReadTimeout("private-provider-detail"),
        chat("第三次说明成功。") if succeeds else 429,
    ]
    wire = Wire(replies)
    runtime = prepared(repo_root, wire=wire)
    before = len(wire.requests)
    report = render_optimization_result(finished_stages.final)
    try:
        result = runtime.handle("确认")
        assert bool(result.errors) is not succeeds
        assert result.outputs[0] == report
        assert "private-provider-detail" not in str(result)
        assert len(wire.requests) == before + 3 and not wire.replies
        assert wire.requests[-1] == wire.requests[-2] == wire.requests[-3]
        assert all(not r.get("tools") for r in wire.requests[-3:])
        assert finished_stages.calls == ["M2", "M4"]
        assert runtime.data["last_result"] == finished_stages.final
        assert runtime.data["pending"]["status"] == "completed"
        if not succeeds:
            assert "优化计算已完成" in str(result.errors)
            assert not any(output.startswith("模型> ") for output in result.outputs)
        count = len(wire.requests)
        for command in ["/confirm", "/result", "/result " + finished_stages.final["workflow_id"]]:
            reused = runtime.handle(command)
            assert not reused.errors and reused.outputs == (report,)
        assert runtime.handle("/resume").errors
        assert len(wire.requests) == count and finished_stages.calls == ["M2", "M4"]
        assert runtime.data["last_result"]["result"] == finished_stages.final["result"]
    finally:
        runtime.close()


def test_explanation_tool_call_is_rejected_without_execution_or_saved_orphan(
    repo_root: Path, finished_stages: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire([chat(None, calls=[call("get_plant_info", call_id="forbidden-explanation-tool")])])
    runtime = prepared(repo_root, wire=wire)
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("unexpected")
        return {"status": "ok"}

    monkeypatch.setattr(runtime.domain, "plant_info", forbidden)
    before = len(wire.requests)
    original_tools = [m for m in runtime.messages if isinstance(m, ToolMessage)]
    try:
        result = runtime.handle("确认执行")
        assert result.errors and "invalid-result-explanation" in str(result.errors)
        assert result.outputs[0] == render_optimization_result(finished_stages.final)
        assert not calls and finished_stages.calls == ["M2", "M4"]
        assert len(wire.requests) == before + 1
        assert not wire.requests[-1].get("tools")
        assert [m for m in runtime.messages if isinstance(m, ToolMessage)] == original_tools
        assert not any(
            c["id"] == "forbidden-explanation-tool"
            for m in runtime.messages
            if isinstance(m, AIMessage)
            for c in m.tool_calls
        )
        assert runtime.data["last_result"] == finished_stages.final
        assert runtime.data["pending"]["status"] == "completed"
    finally:
        runtime.close()


def test_sqlite_resume_finishes_m4_and_explains_once_without_repeating_m2(
    repo_root: Path, tmp_path: Path, finished_stages: SimpleNamespace
) -> None:
    path = tmp_path / "result-session.sqlite"
    first_wire = Wire([])
    runtime = prepared(repo_root, wire=first_wire, store=SessionStore(path))
    finished_stages.interrupt_m4 = True
    try:
        count = len(first_wire.requests)
        assert runtime.handle("/confirm").errors
        assert len(first_wire.requests) == count
        assert runtime.data["pending"]["status"] == "approved"
        assert finished_stages.calls == ["M2", "M4"]
    finally:
        runtime.close()
    finished_stages.interrupt_m4 = False
    wire = Wire([chat("恢复完成，下面解释已经保存的结果。")])
    runtime = ReactAgent(wire.model(), AgentDomainTools(repo_root), store=SessionStore(path))
    try:
        assert runtime.startup() and not wire.requests
        result = runtime.handle("/resume")
        assert not result.errors
        assert result.outputs[0] == render_optimization_result(finished_stages.final)
        assert result.outputs[-1] == "模型> 恢复完成，下面解释已经保存的结果。"
        assert len(wire.requests) == 1 and not wire.requests[0].get("tools")
        assert finished_stages.calls == ["M2", "M4", "M4"]
        assert runtime.data["last_result"] == finished_stages.final
        assert runtime.data["pending"]["status"] == "completed"
        assert not runtime.handle("/confirm").errors
        assert not runtime.handle("/result").errors
        assert runtime.handle("/resume").errors
        assert len(wire.requests) == 1 and finished_stages.calls == ["M2", "M4", "M4"]
    finally:
        runtime.close()
