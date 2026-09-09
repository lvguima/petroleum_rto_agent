from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import ToolMessage
from test_native_protocol import Wire, call, chat

from petroleum_rto.assistant import native_tools
from petroleum_rto.assistant.native_tools import AgentDomainTools, ConfirmationInputError
from petroleum_rto.assistant.react import ReactAgent


def arguments(domain: AgentDomainTools, *, pressure: bool = True) -> dict[str, Any]:
    snapshot = domain.operating_context()["snapshot_ref"]
    return {
        "snapshot_ref": snapshot,
        "objectives": [{"metric_id": "valuable_distillate_yield", "sense": "maximize"}],
        "decision_variables": ["furnace_temperature_target_k"]
        + (["tower_top_pressure_target_pa_a"] if pressure else []),
        "previous_plan_ref": domain.pending.ref if domain.pending else None,
    }


def prepared(repo_root: Path) -> AgentDomainTools:
    domain = AgentDomainTools(repo_root)
    domain.begin_turn("提高收率，允许调温度和压力")
    domain.prepare(**arguments(domain))
    assert domain.displays(success=True)
    return domain


def decision(domain: AgentDomainTools, action: str = "confirm", **overrides: Any) -> dict[str, Any]:
    assert domain.pending
    return {
        "plan_ref": domain.pending.ref,
        "action": action,
        "user_turn_id": domain.turn_id,
        "user_message": domain.user_message,
        **overrides,
    }


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def solve(plan: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("M2", plan))
        return {"static_ref": "static-1", "status": "static_complete"}

    def verify(plan: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("M4", plan))
        return {"status": "complete", "result": {"status": "feasible_not_publishable"}}

    monkeypatch.setattr(native_tools, "solve_prepared_optimization", solve)
    monkeypatch.setattr(native_tools, "verify_prepared_optimization", verify)
    return calls


@pytest.mark.parametrize("confirmation", ["确认", "确认执行", " \n确认执行\t"])
def test_snapshot_binding_and_next_turn_confirmation(
    repo_root: Path, stages: list[Any], confirmation: str
) -> None:
    domain = AgentDomainTools(repo_root)
    domain.begin_turn("提高收率")
    domain.prepare(**arguments(domain))
    assert domain.pending
    plan = domain.pending
    with pytest.raises(ValueError):
        domain.manage(**decision(domain))
    with pytest.raises(ValueError):
        domain.solve(plan.ref)
    assert not stages
    assert "塔顶压力目标" in domain.displays(success=True)[0]
    domain.begin_turn(confirmation)
    domain.manage(**decision(domain))
    domain.snapshots.clear()  # execution uses the prepared object, never reloads by path/ref
    static = domain.solve(plan.ref)
    domain.verify(plan.ref, static["static_ref"])
    domain.solve(plan.ref)
    domain.verify(plan.ref, static["static_ref"])
    assert [s[0] for s in stages] == ["M2", "M4"]
    assert stages[0][1] is stages[1][1] is plan.prepared


@pytest.mark.parametrize(
    "message",
    [
        "确只调整出口温度，不调整 压力目标",
        "我说了不要调整压力，只调整温度啊",
        "只调整出口温度，不调整压力目标",
        "确认但不要调压力，只调温度",
        "确认\n不要调压力",
        "确认？",
        "确认。",
        "不确认",
        "他说‘确认’",
        "同意",
        "/confirm 只调温度",
    ],
)
def test_constraint_or_quoted_confirmation_cannot_authorize_even_matching_plan(
    repo_root: Path, stages: list[Any], message: str
) -> None:
    domain = AgentDomainTools(repo_root)
    domain.begin_turn("只调整温度")
    domain.prepare(**arguments(domain, pressure=False))
    domain.displays(success=True)
    plan = domain.pending
    domain.begin_turn(message)
    with pytest.raises(ConfirmationInputError):
        domain.manage(**decision(domain))
    assert not plan.authorized and not plan.eligible
    with pytest.raises(ValueError):
        domain.solve(plan.ref)
    domain.manage(**decision(domain, action="keep"))
    assert not plan.authorized and not plan.eligible
    domain.begin_turn("确认")
    with pytest.raises(ValueError):
        domain.manage(**decision(domain))
    assert not stages


def test_erroneous_native_confirm_is_reported_and_revision_can_recover(
    repo_root: Path, stages: list[Any]
) -> None:
    domain = prepared(repo_root)
    ref = domain.pending.ref
    message = "只调整出口温度，不调整压力目标"
    revise = arguments(domain, pressure=False)
    wire = Wire(
        [
            chat(
                None,
                calls=[
                    call(
                        "manage_optimization",
                        json.dumps(
                            {
                                "plan_ref": ref,
                                "action": "confirm",
                                "user_turn_id": 2,
                                "user_message": message,
                            }
                        ),
                    )
                ],
            ),
            chat(None, calls=[call("solve_optimization", json.dumps({"plan_ref": ref}), "c2")]),
            chat(None, calls=[call("prepare_optimization", json.dumps(revise), "c3")]),
            chat("已改为只调温度，请单独回复确认。"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    reply = runtime.handle(message)
    assert not reply.errors and not stages
    results = [m for m in runtime.messages if isinstance(m, ToolMessage)]
    assert results[0].status == results[1].status == "error"
    assert json.loads(str(results[0].content))["code"] == "confirmation-input-required"
    assert "confirmation-input-required" in json.dumps(wire.requests[1])
    assert domain.pending.ref != ref and domain.pending.eligible
    assert not domain.pending.authorized
    assert domain.pending.prepared.intent.decision_variables == ("furnace_temperature_target_k",)
    assert not runtime.handle("/confirm").errors
    assert [stage[0] for stage in stages] == ["M2", "M4"]
    assert stages[0][1] is domain.pending.prepared


def test_revision_excludes_pressure_and_invalidates_old_version(
    repo_root: Path, stages: list[Any]
) -> None:
    domain = prepared(repo_root)
    assert domain.pending
    old = domain.pending.ref
    domain.begin_turn("确认但只调温度，不调压力")
    domain.prepare(**arguments(domain, pressure=False))
    assert domain.pending.ref != old
    assert domain.pending.prepared.intent.decision_variables == ("furnace_temperature_target_k",)
    with pytest.raises(ValueError):
        domain.manage(**decision(domain))
    with pytest.raises(ValueError):
        domain.solve(old)
    summary = domain.displays(success=True)[0]
    assert "允许调整：炉出口温度目标\n" in summary
    assert "塔顶压力目标" not in summary
    assert not stages
    domain.begin_turn("确认")
    domain.manage(**decision(domain))
    domain.solve(domain.pending.ref)
    assert len(stages) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"decision_variables": ["invented_variable"]},
        {"snapshot_ref": "untrusted-snapshot"},
        {"previous_plan_ref": "stale-plan"},
        {"arbitrary_formula": "bad"},
    ],
)
def test_failed_revision_retains_data_but_revokes_confirmation(
    repo_root: Path, bad: dict[str, Any]
) -> None:
    domain = prepared(repo_root)
    old = domain.pending
    domain.begin_turn("只调温度")
    with pytest.raises((ValueError, KeyError)):
        domain.prepare(**(arguments(domain, pressure=False) | bad))
    assert domain.pending is old
    assert not domain.pending.eligible
    domain.begin_turn("确认")
    with pytest.raises(ValueError):
        domain.manage(**decision(domain))


def test_entire_current_user_message_and_turn_are_required(repo_root: Path) -> None:
    domain = prepared(repo_root)
    domain.begin_turn("确认，但是不要压力")
    with pytest.raises(ValueError):
        domain.manage(**decision(domain, user_message="确认"))
    with pytest.raises(ValueError):
        domain.manage(**decision(domain, user_turn_id=domain.turn_id - 1))


def test_confirmation_projection_distinguishes_display_and_current_turn_decision(
    repo_root: Path,
) -> None:
    domain = AgentDomainTools(repo_root)
    domain.begin_turn("提高收率，只调温度")
    domain.prepare(**arguments(domain, pressure=False))
    pending = domain.state()["pending_plan"]
    assert pending["confirmation_status"]["state"] == "awaiting_display"
    assert not pending["confirmation_available"]
    assert "下一轮" in pending["confirmation_status"]["message"]
    domain.displays(success=True)
    assert domain.state()["pending_plan"]["confirmation_status"]["state"] == "awaiting_confirmation"
    domain.begin_turn("这是什么装置？")
    pending = domain.inspect_result()["task"]["pending_plan"]
    assert pending["confirmation_status"]["state"] == "awaiting_turn_decision"
    assert not pending["confirmation_available"]
    domain.manage(**decision(domain, "keep"))
    assert domain.state()["pending_plan"]["confirmation_available"]
    assert "尚未执行" in domain.displays(success=True)[0]


def test_unresolved_reply_cannot_leave_a_stale_ready_projection(repo_root: Path) -> None:
    domain = prepared(repo_root)
    wire = Wire([chat("可直接确认执行。")])  # Deliberately wrong model statement.
    runtime = ReactAgent(wire.model(), domain)
    reply = runtime.handle("压力不对，我再想想")
    assert "目前不能直接确认执行" in reply.outputs[-1]
    pending = domain.inspect_result()["task"]["pending_plan"]
    assert not pending["confirmation_available"]
    assert pending["confirmation_status"]["state"] == "suspended"
    assert runtime.handle("/confirm").errors


def test_failed_preparation_display_does_not_advertise_ready_status(repo_root: Path) -> None:
    domain = AgentDomainTools(repo_root)
    domain.begin_turn("准备优化")
    domain.prepare(**arguments(domain))
    displays = domain.displays(success=False)
    assert "暂停" in displays[-1]
    assert domain.state()["pending_plan"]["confirmation_status"]["state"] == "suspended"
    assert not domain.state()["pending_plan"]["confirmation_available"]


def test_pending_query_keep_and_model_switch_preserve_plan(repo_root: Path) -> None:
    domain = prepared(repo_root)
    plan = domain.pending
    wire = Wire(
        [
            chat(
                None,
                calls=[
                    call(
                        "manage_optimization",
                        json.dumps(
                            {
                                "plan_ref": plan.ref,
                                "action": "keep",
                                "user_turn_id": 2,
                                "user_message": "这是什么装置？",
                            }
                        ),
                    )
                ],
            ),
            chat(None, calls=[call("get_plant_info", call_id="c2")]),
            chat("常压蒸馏"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    turn_id = domain.turn_id
    assert plan.eligible
    for text in ("/model", "99", "/model invalid-id", "4"):
        turn = runtime.handle(text)
        assert bool(turn.errors) == (text in ("99", "/model invalid-id"))
        assert domain.pending is plan and plan.eligible
        assert domain.turn_id == turn_id
        assert not runtime.messages and not wire.requests
    assert runtime.model.selection.profile.model_id == "deepseek-v4-pro-0813"
    assert not runtime.handle("/thinking off").errors
    assert runtime.domain.pending is plan
    assert not runtime.handle("这是什么装置？").errors
    assert domain.pending.eligible
    assert "confirmation_available" in json.dumps(wire.requests[0])


def test_leaving_model_selection_preserves_plan_but_cancel_still_cancels(repo_root: Path) -> None:
    domain = prepared(repo_root)
    plan = domain.pending
    wire = Wire([])
    runtime = ReactAgent(wire.model(), domain)
    turn_id = domain.turn_id
    runtime.handle("/model")
    closed = runtime.handle("0")
    assert not closed.errors and "已退出模型选择" in closed.outputs[0]
    assert domain.pending is plan and plan.eligible
    assert domain.turn_id == turn_id and not runtime.messages
    runtime.handle("/model")
    assert not runtime.handle("/cancel").errors
    assert domain.pending is None and not wire.requests


def test_unresolved_pending_turn_and_transport_failure_suspend(repo_root: Path) -> None:
    for reply in [chat("请澄清具体要求"), httpx.ConnectError("private")]:
        domain = prepared(repo_root)
        wire = Wire([reply])
        runtime = ReactAgent(wire.model(), domain)
        runtime.handle("那个不行，改一下")
        assert not domain.pending.eligible
        assert runtime.handle("/confirm").errors


def test_native_schema_failure_cannot_keep_old_confirmation(repo_root: Path) -> None:
    domain = prepared(repo_root)
    old = domain.pending
    wire = Wire(
        [
            chat(None, calls=[call("prepare_optimization", '{"unexpected":1}')]),
            chat(
                None,
                calls=[
                    call(
                        "manage_optimization",
                        json.dumps(
                            {
                                "plan_ref": old.ref,
                                "action": "keep",
                                "user_turn_id": 2,
                                "user_message": "只调温度",
                            }
                        ),
                        "c2",
                    )
                ],
            ),
            chat("修改失败"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    runtime.handle("只调温度")
    assert domain.pending is old and not old.eligible
    assert runtime.handle("/confirm").errors


def test_mutating_parallel_batch_is_rejected_before_any_execution(
    repo_root: Path, stages: list[Any]
) -> None:
    domain = prepared(repo_root)
    domain.begin_turn("确认")
    confirm = decision(domain)
    # Runtime will start turn 2; rewind only the synthetic fixture's turn counter.
    domain.turn_id -= 1
    domain.pending.eligible = True
    wire = Wire(
        [
            chat(
                None,
                calls=[
                    call("manage_optimization", json.dumps(confirm)),
                    call("solve_optimization", json.dumps({"plan_ref": domain.pending.ref}), "c2"),
                ],
            ),
            chat("需顺序调用"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    runtime.handle("确认")
    assert not stages
    results = [m for m in runtime.messages if isinstance(m, ToolMessage)]
    assert len(results) == 2 and all(m.status == "error" for m in results)


@pytest.mark.parametrize("confirmation", ["确认", "确认执行"])
def test_native_confirm_solve_verify_and_explanation_failure_preserves_result(
    repo_root: Path, stages: list[Any], confirmation: str
) -> None:
    domain = prepared(repo_root)
    ref = domain.pending.ref
    wire = Wire(
        [
            chat(
                None,
                calls=[
                    call(
                        "manage_optimization",
                        json.dumps(
                            {
                                "plan_ref": ref,
                                "action": "confirm",
                                "user_turn_id": 2,
                                "user_message": confirmation,
                            }
                        ),
                    )
                ],
            ),
            chat(None, calls=[call("solve_optimization", json.dumps({"plan_ref": ref}), "c2")]),
            chat(
                None,
                calls=[
                    call(
                        "verify_optimization",
                        json.dumps({"plan_ref": ref, "static_ref": "static-1"}),
                        "c3",
                    )
                ],
            ),
            httpx.ConnectError("private"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    result = runtime.handle(confirmation)
    assert result.errors and "feasible_not_publishable" in "".join(result.outputs)
    assert [stage[0] for stage in stages] == ["M2", "M4"]
    assert "static_complete" in json.dumps(wire.requests[2])
    assert "feasible_not_publishable" in runtime.handle("/result").outputs[0]
    runtime.handle("/cancel")
    assert domain.pending is None and domain.last_result is not None
    assert len(stages) == 2


def test_explicit_confirm_executes_without_model_then_cancel_preserves_result(
    repo_root: Path, stages: list[Any]
) -> None:
    domain = prepared(repo_root)
    wire = Wire([])
    runtime = ReactAgent(wire.model(), domain)
    result = runtime.handle("/confirm")
    assert not result.errors
    assert len(stages) == 2 and not wire.requests
    assert not runtime.handle("/confirm").errors
    assert len(stages) == 2
    runtime.handle("/cancel")
    assert domain.pending is None and domain.last_result


def test_native_preparation_and_conditional_revision_are_displayed_without_simulation(
    repo_root: Path, stages: list[Any]
) -> None:
    domain = AgentDomainTools(repo_root)
    first_args = arguments(domain)
    # Derive the expected program reference using an independent deterministic preparation.
    reference = prepared(repo_root).pending.ref
    second_args = {
        **first_args,
        "previous_plan_ref": reference,
        "decision_variables": ["furnace_temperature_target_k"],
    }
    wire = Wire(
        [
            chat(None, calls=[call("prepare_optimization", json.dumps(first_args))]),
            chat("已准备方案，请确认"),
            chat(None, calls=[call("prepare_optimization", json.dumps(second_args), "c2")]),
            chat("已改为只调整温度"),
        ]
    )
    runtime = ReactAgent(wire.model(), domain)
    first = runtime.handle("提高收率，允许调温度和压力")
    assert not first.errors and "允许调整：炉出口温度目标、塔顶压力目标" in "".join(first.outputs)
    second = runtime.handle("确只调整出口温度，不调整压力目标")
    assert not second.errors and "允许调整：炉出口温度目标\n" in "".join(second.outputs)
    assert domain.pending.eligible
    assert not stages
    assert len(wire.requests) == 4


def test_missing_or_corrupt_stored_result_is_a_safe_tool_error(tmp_path: Path) -> None:
    wire = Wire(
        [
            chat(
                None,
                calls=[
                    call(
                        "inspect_optimization",
                        json.dumps({"workflow_id": "offline-rto-0123456789abcdef"}),
                    )
                ],
            ),
            chat("未找到可校验的完整结果"),
        ]
    )
    runtime = ReactAgent(wire.model(), AgentDomainTools(tmp_path))
    assert not runtime.handle("查看这个结果").errors
    result = next(m for m in runtime.messages if isinstance(m, ToolMessage))
    assert result.status == "error" and str(tmp_path) not in str(result.content)
    assert runtime.handle("/result offline-rto-0123456789abcdef").errors
    assert not runtime.domain.last_result
