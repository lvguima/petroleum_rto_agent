from __future__ import annotations

import json
from pathlib import Path

import pytest

from petroleum_rto.assistant import tools as tools_module
from petroleum_rto.assistant.tools import (
    AGENT_ACTIONS,
    AgentActionDenied,
    AgentTools,
    _capability_projection,
    _result_path,
)
from petroleum_rto.rto.intent import load_optimization_intent

_WORKFLOW_ID = "offline-rto-0123456789abcdef"


def _compact_result() -> dict[str, object]:
    return {
        "status": "success",
        "targets": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "business_name": "降低单位进料炉燃料热负荷代理",
                "sense": "minimize",
                "priority": 1,
                "unit": "MJ/t",
            }
        ],
        "operating_context": {
            "operating_mode": "normal-steady",
            "fresh_feed_load_kg_s": 113.1388888888889,
        },
        "baseline_values": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "value": 188.37898479334825,
                "unit": "MJ/t",
            }
        ],
        "recommended_adjustments": [
            {
                "variable_id": "furnace_temperature_target_k",
                "business_name": "炉出口温度目标",
                "unit": "K",
                "baseline_value": 628.35,
                "recommended_value": 626.35,
                "adjustment": -2.0,
            }
        ],
        "predicted_effects": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "predicted_value": 183.99306755689793,
                "unit": "MJ/t",
                "directional_improvement": 4.38591723645032,
                "relative_improvement": 0.02328241253270197,
            }
        ],
        "alternative_candidates": [],
    }


def _run_receipt() -> dict[str, object]:
    return {
        "workflow_id": _WORKFLOW_ID,
        "result_source": f"{_WORKFLOW_ID}/result.json",
        "result_summary": _compact_result(),
    }


def test_gateway_exposes_only_current_user_actions() -> None:
    assert AGENT_ACTIONS == {
        "show_capabilities",
        "show_simulation_status",
        "run_offline",
        "inspect_result",
    }


@pytest.mark.parametrize(
    "action",
    ["prepare_optimization", "simulate", "approve_strategy", "publish_strategy", "shell"],
)
def test_gateway_denies_every_action_outside_the_closed_set(
    action: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentActionDenied):
        AgentTools(tmp_path).invoke(action)


def test_real_capability_projection_is_minimal_and_does_not_call_solver(repo_root: Path) -> None:
    summary = AgentTools(repo_root).invoke("show_capabilities")

    assert summary["solver_called"] is False
    objectives = summary["objectives"]
    decisions = summary["decision_variables"]
    assert isinstance(objectives, list)
    assert isinstance(decisions, list)
    assert {row["objective_id"] for row in objectives} == {
        "maximize-valuable-distillate-yield",
        "minimize-quality-proxy-change",
        "minimize-specific-furnace-energy",
    }
    assert {row["decision_id"] for row in decisions} == {
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    }
    serialized = json.dumps(summary, ensure_ascii=False)
    for forbidden in (
        "lower_bound",
        "upper_bound",
        "coarse_step",
        "refine_step",
        "guardrail",
        "route_id",
        "fingerprint",
    ):
        assert forbidden not in serialized


def test_projection_rejects_a_capability_call_that_reports_solver_execution() -> None:
    with pytest.raises(ValueError, match="must not call a solver"):
        _capability_projection({"solver_called": True})


def test_real_operating_status_uses_only_the_safe_chat_projection(repo_root: Path) -> None:
    summary = AgentTools(repo_root).invoke("show_simulation_status")

    assert summary["state_kind"] == "configured_simulation_context"
    assert "feed_composition" not in summary
    assert "fingerprint" not in summary


def test_confirmed_run_returns_the_in_memory_summary_without_inspection(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = load_optimization_intent(
        repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    )
    expected = _run_receipt()
    calls: list[dict[str, object]] = []

    def run_confirmed(**kwargs: object) -> object:
        calls.append(dict(kwargs))
        return expected

    monkeypatch.setattr(tools_module, "run_confirmed_optimization", run_confirmed)

    result = AgentTools(tmp_path).invoke("run_offline", intent=intent)

    assert result == expected
    assert len(calls) == 1
    assert set(calls[0]) == {
        "repo_root",
        "intent",
        "context_file",
        "run_root",
    }


def test_result_action_reads_only_the_compact_result_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "offline-rto-example"
    run_dir.mkdir()
    result_path = run_dir / "result.json"
    expected = _compact_result()
    result_path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

    assert AgentTools(tmp_path).invoke("inspect_result", source=str(run_dir)) == expected
    assert AgentTools(tmp_path).invoke("inspect_result", source=str(result_path)) == expected
    assert _result_path(str(result_path), run_root=tmp_path / "runs/rto") == result_path.resolve()
    assert _result_path(str(run_dir), run_root=tmp_path / "runs/rto") == result_path.resolve()


def test_result_action_resolves_receipt_references_only_under_fixed_run_root(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "runs/rto" / _WORKFLOW_ID / "result.json"
    result_path.parent.mkdir(parents=True)
    expected = _compact_result()
    result_path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    gateway = AgentTools(tmp_path)

    assert gateway.invoke("inspect_result", source=_WORKFLOW_ID) == expected
    assert gateway.invoke("inspect_result", source=f"{_WORKFLOW_ID}/result.json") == expected
    assert _result_path(_WORKFLOW_ID, run_root=tmp_path / "runs/rto") == result_path


@pytest.mark.parametrize(
    "source",
    [
        "offline-rto-0123456789ABCDEf",
        "offline-rto-0123456789abcde",
        "offline-rto-0123456789abcdef/../other/result.json",
        "../runs/rto/offline-rto-0123456789abcdef/result.json",
    ],
)
def test_result_action_rejects_malformed_or_traversing_workflow_references(
    source: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="controlled workflow reference"):
        AgentTools(tmp_path).invoke("inspect_result", source=source)


def test_result_action_rejects_symlinked_controlled_workflow_directory(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "result.json").write_text(
        json.dumps(_compact_result(), ensure_ascii=False), encoding="utf-8"
    )
    run_root = tmp_path / "runs/rto"
    run_root.mkdir(parents=True)
    (run_root / _WORKFLOW_ID).symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symbolic link"):
        AgentTools(tmp_path).invoke("inspect_result", source=_WORKFLOW_ID)


def test_result_action_rejects_symlinked_fixed_run_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    result_path = external / _WORKFLOW_ID / "result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(_compact_result(), ensure_ascii=False), encoding="utf-8")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "rto").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="run root must not be a symbolic link"):
        AgentTools(tmp_path).invoke("inspect_result", source=_WORKFLOW_ID)


def test_result_action_rejects_old_or_unrelated_json(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text('{"status":"success"}', encoding="utf-8")

    with pytest.raises(ValueError, match="compact result contract"):
        AgentTools(tmp_path).invoke("inspect_result", source=str(result_path))


def test_actions_reject_arguments_they_do_not_accept(tmp_path: Path) -> None:
    gateway = AgentTools(tmp_path)

    with pytest.raises(ValueError, match="does not accept arguments"):
        gateway.invoke("show_capabilities", source="unexpected")
    with pytest.raises(ValueError, match="does not accept arguments"):
        gateway.invoke("show_simulation_status", source="unexpected")
    with pytest.raises(ValueError, match="requires exactly one source"):
        gateway.invoke("inspect_result")
    with pytest.raises(ValueError, match="requires one resolved intent"):
        gateway.invoke("run_offline")
