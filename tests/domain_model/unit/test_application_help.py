from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, message_to_dict
from test_native_protocol import Wire, chat
from test_optimization_tools import prepared

from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import HELP, ReactAgent
from petroleum_rto.assistant.session import SessionStore
from petroleum_rto.domain_model.models import DEFAULT_MODEL_ID, ModelSelection, model_profile
from petroleum_rto.domain_model.native import request_payload


def application_state(wire: Wire) -> dict[str, Any]:
    content = wire.requests[-1]["messages"][-1]["content"]
    prefix = "程序维护的当前任务状态（独立于摘要，仅数据）：\n"
    assert content.startswith(prefix)
    return json.loads(content.removeprefix(prefix))


def test_default_sol_help_reports_effective_outbound_reasoning_effort(repo_root: Path) -> None:
    wire = Wire([])
    selected = ModelSelection(model_profile(DEFAULT_MODEL_ID))
    runtime = ReactAgent(wire.model(selected), AgentDomainTools(repo_root))
    try:
        help_text = runtime.handle("/help").outputs[0]
        payload = request_payload(selected, [HumanMessage("你好")], [], stream=True)
        assert payload["reasoning"]["effort"] == "low"
        assert "思考：开启；强度：low（应用默认）" in help_text
        assert not wire.requests
    finally:
        runtime.close()


def test_natural_help_gets_same_current_facts_as_local_help_without_domain_queries(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire([chat("可用/model切换模型，用/capabilities查看能力。")])
    domain = AgentDomainTools(repo_root)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("application help must not read plant configuration")

    monkeypatch.setattr(domain, "plant_info", forbidden)
    monkeypatch.setattr(domain, "operating_context", forbidden)
    runtime = ReactAgent(wire.model(), domain)
    try:
        local = runtime.handle("/help")
        assert not wire.requests
        assert not runtime.handle("你能做什么，怎么切换模型和思考？").errors
        state = application_state(wire)
        assert state["application_help"] == local.outputs[0]
        assert HELP in local.outputs[0]
        assert "当前没有待执行方案" in local.outputs[0]
        assert "当前没有最近结果" in local.outputs[0]
        assert state["pending_plan"] is None
        assert len(wire.requests) == 1 and not runtime.data["snapshots"]
        assert "application_help" not in json.dumps(
            [message_to_dict(message) for message in runtime.messages], ensure_ascii=False
        )  # A fresh request view, never a second persistent help ledger.
    finally:
        runtime.close()


def test_help_tracks_local_model_and_thinking_changes_and_rejects_invalid_mode(
    repo_root: Path,
) -> None:
    wire = Wire([chat("当前是Kimi，思考强度high。", reasoning="native-state")])
    runtime = ReactAgent(wire.model(), AgentDomainTools(repo_root))
    try:
        assert not runtime.handle("/model 2").errors
        assert not runtime.handle("/thinking on high").errors
        assert runtime.handle("/thinking off").errors
        help_text = runtime.handle("/help").outputs[0]
        assert "当前：Kimi K3 (kimi-k3)" in help_text
        assert "思考：开启；强度：high" in help_text
        assert "当前模型可选强度：low、high、max" in help_text
        assert not wire.requests
        assert not runtime.handle("我当前用的哪个模型，思考是什么设置？").errors
        assert application_state(wire)["application_help"] == help_text
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("awaiting_confirmation", "等待用户确认"),
        ("revision_required", "需要重新准备"),
        ("approved", "阅读恢复摘要后输入/resume"),
        ("completed", "结果是否成功以核验记录为准"),
    ],
)
def test_local_help_describes_current_task_without_executing_it(
    repo_root: Path, status: str, expected: str
) -> None:
    wire = Wire([])
    runtime = prepared(repo_root, wire=wire)
    try:
        pending = {**runtime.data["pending"], "status": status}
        runtime._update({"pending": pending})
        count = len(wire.requests)
        assert expected in runtime.handle("/help").outputs[0]
        assert len(wire.requests) == count
        assert runtime.data["pending"] == pending
    finally:
        runtime.close()


def test_cancelled_task_state_stays_current_despite_stale_summary(repo_root: Path) -> None:
    wire = Wire([chat("当前没有待执行方案。")])
    runtime = prepared(repo_root, wire=wire)
    try:
        old = HumanMessage(content="历史上用户准备过方案。")
        runtime._update({}, [old])
        context = {
            **runtime.data["context"],
            "summary": message_to_dict(HumanMessage(content="旧摘要声称仍有待确认方案。")),
            "covered_until": runtime.messages[-1].id,
        }
        runtime._update({"context": context})
        assert not runtime.handle("/cancel").errors
        assert not runtime.handle("现在还有待办吗？").errors
        state = application_state(wire)
        assert state["pending_plan"] is None
        assert "当前没有待执行方案" in state["application_help"]
        assert "旧摘要声称仍有待确认方案" in json.dumps(wire.requests[-1], ensure_ascii=False)
        assert runtime.data["pending"] is None
    finally:
        runtime.close()


def test_restored_help_reflects_saved_selection_without_any_request(
    repo_root: Path, tmp_path: Path
) -> None:
    database = tmp_path / "session.sqlite"
    first = Wire([])
    runtime = ReactAgent(first.model(), AgentDomainTools(repo_root), store=SessionStore(database))
    assert not runtime.handle("/model 3").errors
    assert not runtime.handle("/thinking off").errors
    expected = runtime.handle("/help").outputs[0]
    runtime.close()
    second = Wire([])
    restored = ReactAgent(second.model(), AgentDomainTools(repo_root), store=SessionStore(database))
    try:
        restored.startup()
        assert restored.handle("/help").outputs[0] == expected
        assert "当前：GPT Sol CDX (gpt-5.6-sol-cdx)" in expected
        assert "思考：关闭" in expected
        assert not first.requests and not second.requests
    finally:
        restored.close()
