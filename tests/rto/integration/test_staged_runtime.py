from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_unified_offline_workflow import _PersistedSimulator

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.orchestration.service import OfflineRtoOrchestrator, read_offline_run
from petroleum_rto.rto.runtime import staged


@pytest.fixture
def staged_case(
    repo_root: Path, tmp_path: Path, make_bundle: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, Path]:
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    prepared = staged.prepare_optimization(
        repo_root=repo_root,
        context=context,
        objectives=[{"metric_id": "specific_furnace_fuel_energy_mj_per_t", "sense": "minimize"}],
        decision_variables=["furnace_temperature_target_k"],
    )
    simulator = _PersistedSimulator(
        tmp_path / "placeholder",
        context.model_ref.fingerprint,
        context.case_ref.fingerprint,
        make_bundle,
    )

    def factory(root: Path) -> Any:
        simulator._output_root = root
        return simulator

    orchestrator = OfflineRtoOrchestrator(CduM7RequestFactory(), factory)
    monkeypatch.setattr(staged, "_orchestrator", lambda: orchestrator)
    return prepared, simulator, tmp_path / "runs"


def test_separate_static_then_complete_shortlist_and_idempotent_replay(staged_case: Any) -> None:
    prepared, simulator, root = staged_case
    assert simulator.evaluate_calls == 0
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    assert static["status"] == "static_complete"
    assert not static["final_result_included"]
    calls = simulator.evaluate_calls
    run_dir = root / static["workflow_id"]
    assert calls > 0
    assert not (run_dir / "dynamic_evaluations.json").exists()
    assert not (run_dir / "manifest.json").exists()
    assert not (run_dir / "result.json").exists()
    assert (
        staged.solve_prepared_optimization(prepared, run_root=root)["physical_m2_executions"] == 0
    )
    assert simulator.evaluate_calls == calls
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root
    )
    assert result["physical_m2_executions"] == 0
    assert result["physical_m4_executions"] > 0
    dynamics = json.loads((run_dir / "dynamic_evaluations.json").read_text())
    assert len(dynamics["evaluations"]) == len(static["selection"]["shortlist_proposal_refs"])
    count = simulator.evaluate_calls
    repeated = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root
    )
    assert repeated["result"] == result["result"]
    assert repeated["physical_m4_executions"] == 0
    assert simulator.evaluate_calls == count
    record = read_offline_run(run_dir, request_factory=CduM7RequestFactory(), simulator=simulator)
    assert record.context is not None and record.problem == prepared.problem


def test_verify_cannot_start_static_search_or_accept_wrong_reference(staged_case: Any) -> None:
    prepared, simulator, root = staged_case
    with pytest.raises(ValueError):
        staged.verify_prepared_optimization(prepared, static_ref="invented", run_root=root)
    assert simulator.evaluate_calls == 0
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    count = simulator.evaluate_calls
    with pytest.raises(ValueError):
        staged.verify_prepared_optimization(prepared, static_ref="invented", run_root=root)
    assert simulator.evaluate_calls == count
    assert not (root / static["workflow_id"] / "dynamic_evaluations.json").exists()


def test_tampered_or_missing_checkpoint_is_rejected_without_simulation(staged_case: Any) -> None:
    prepared, simulator, root = staged_case
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    count = simulator.evaluate_calls
    path = root / static["workflow_id"] / "context.json"
    original = path.read_text()
    data = json.loads(original)
    data["data_timestamp"] = "2026-09-09T00:00:00+08:00"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        staged.verify_prepared_optimization(
            prepared, static_ref=static["static_ref"], run_root=root
        )
    assert simulator.evaluate_calls == count
    path.write_text(original)
    (root / static["workflow_id"] / "static_solve.json").unlink()
    with pytest.raises(ValueError):
        staged.verify_prepared_optimization(
            prepared, static_ref=static["static_ref"], run_root=root
        )
    assert simulator.evaluate_calls == count


def test_dynamic_interruption_keeps_static_and_resumes_without_repeating_m2(
    staged_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, simulator, root = staged_case
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    count = simulator.evaluate_calls
    original = simulator.preview

    def interrupt(request: Any) -> Any:
        if request.stage == "M4":
            raise KeyboardInterrupt
        return original(request)

    monkeypatch.setattr(simulator, "preview", interrupt)
    with pytest.raises(KeyboardInterrupt):
        staged.verify_prepared_optimization(
            prepared, static_ref=static["static_ref"], run_root=root
        )
    assert simulator.evaluate_calls == count
    assert not (root / static["workflow_id"] / "manifest.json").exists()
    monkeypatch.setattr(simulator, "preview", original)
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root
    )
    assert result["physical_m2_executions"] == 0 and result["physical_m4_executions"] > 0


def test_dynamic_adapter_failure_is_reported_as_system_error(
    staged_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, simulator, root = staged_case
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    original = simulator.preview

    def failed(request: Any) -> Any:
        if request.stage == "M4":
            raise OSError("synthetic adapter failure")
        return original(request)

    monkeypatch.setattr(simulator, "preview", failed)
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root
    )
    assert result["result"]["status"] == "evaluation_error"
    assert not result["result"]["recommended_adjustments"]
    assert result["physical_m2_executions"] == 0
