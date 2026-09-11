from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_unified_offline_workflow import _PersistedSimulator

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.simulation import SimulationRunBundle
from petroleum_rto.rto.orchestration.service import OfflineRtoOrchestrator, read_offline_run
from petroleum_rto.rto.progress import RtoProgress
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
    events: list[RtoProgress] = []
    assert (
        staged.solve_prepared_optimization(prepared, run_root=root, on_progress=events.append)[
            "physical_m2_executions"
        ]
        == 0
    )
    assert [event.status for event in events] == ["reused"]
    assert events[0].stage == "m2"
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
        prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
    )
    assert repeated["result"] == result["result"]
    assert repeated["physical_m4_executions"] == 0
    assert simulator.evaluate_calls == count
    assert [event.status for event in events] == ["reused", "reused"]
    assert events[-1].stage == "m4"
    record = read_offline_run(run_dir, request_factory=CduM7RequestFactory(), simulator=simulator)
    assert record.context is not None and record.problem == prepared.problem


def test_verify_cannot_start_static_search_or_accept_wrong_reference(staged_case: Any) -> None:
    prepared, simulator, root = staged_case
    events: list[RtoProgress] = []
    with pytest.raises(ValueError):
        staged.verify_prepared_optimization(
            prepared, static_ref="invented", run_root=root, on_progress=events.append
        )
    assert simulator.evaluate_calls == 0
    assert events == [RtoProgress("m4", "error")]
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
    events: list[RtoProgress] = []
    with pytest.raises(ValueError):
        staged.verify_prepared_optimization(
            prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
        )
    assert simulator.evaluate_calls == count
    assert events == [RtoProgress("m4", "error")]
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
    events: list[RtoProgress] = []
    with pytest.raises(KeyboardInterrupt):
        staged.verify_prepared_optimization(
            prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
        )
    assert [event.status for event in events] == ["started", "error"]
    assert all(event.stage == "m4" for event in events)
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
    events: list[RtoProgress] = []
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
    )
    assert result["result"]["status"] == "evaluation_error"
    assert not result["result"]["recommended_adjustments"]
    assert result["physical_m2_executions"] == 0
    assert events[-1].status == "error"
    assert not any(event.status == "completed" for event in events)


@pytest.mark.parametrize("multi", [False, True])
def test_progress_counts_completed_evaluations_during_search_and_verification(
    staged_case: Any, repo_root: Path, multi: bool
) -> None:
    prepared, simulator, root = staged_case
    if multi:
        prepared = staged.prepare_optimization(
            repo_root=repo_root,
            context=prepared.context,
            objectives=[
                {"metric_id": "specific_furnace_fuel_energy_mj_per_t", "sense": "minimize"},
                {"metric_id": "valuable_distillate_yield", "sense": "maximize"},
            ],
            decision_variables=["furnace_temperature_target_k"],
        )
    events: list[RtoProgress] = []

    def observe(event: RtoProgress) -> None:
        events.append(event)
        if event.status == "started" and event.stage == "m2":
            assert simulator.evaluate_calls == 0
        if event.status == "progress":
            # The callback runs before this stage is committed, after real evaluation.
            artifact = "static_solve.json" if event.stage == "m2" else "dynamic_evaluations.json"
            assert not list(root.glob(f"*/{artifact}"))
            assert simulator.evaluate_calls > 0

    static = staged.solve_prepared_optimization(prepared, run_root=root, on_progress=observe)
    evaluated = len(static["m2_evaluations"])
    assert events[0] == RtoProgress("m2", "started", 0)
    assert events[-1] == RtoProgress("m2", "completed", evaluated, evaluated)
    points = [event for event in events if event.status == "progress"]
    assert [event.completed for event in points] == list(range(1, evaluated + 1))
    assert all(event.stage == "m2" and event.total is None for event in points)
    events.clear()
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root, on_progress=observe
    )
    shortlisted = len(static["selection"]["shortlist_proposal_refs"])
    assert events == [
        RtoProgress("m4", "started", 0, shortlisted),
        *(RtoProgress("m4", "progress", index, shortlisted) for index in range(1, shortlisted + 1)),
        RtoProgress("m4", "completed", shortlisted, shortlisted),
    ]
    assert result["result"]["status"] in {"success", "feasible_not_publishable"}


def test_no_static_feasible_progress_does_not_start_dynamic_simulation(
    staged_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, simulator, root = staged_case
    original = simulator._make_bundle

    def violates_conservation(*args: Any, **kwargs: Any) -> SimulationRunBundle:
        bundle = original(*args, **kwargs)
        data = bundle.as_dict()
        data["summary"]["flowsheet"]["diagnostics"]["conservation_gate_passed"] = 0.0
        return SimulationRunBundle.from_mapping(data)

    monkeypatch.setattr(simulator, "_make_bundle", violates_conservation)
    events: list[RtoProgress] = []
    static = staged.solve_prepared_optimization(prepared, run_root=root, on_progress=events.append)
    assert static["selection"]["status"] == "no_feasible"
    assert events[-1].status == "no_feasible"
    assert not any(event.status == "completed" for event in events)
    calls = simulator.evaluate_calls
    events.clear()
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
    )
    assert result["result"]["status"] == "no_feasible"
    assert simulator.evaluate_calls == calls
    assert events == [RtoProgress("m4", "no_feasible", 0, 0)]


def test_no_verified_candidate_progress_and_reuse_preserve_failure_outcome(
    staged_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, simulator, root = staged_case
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    original = simulator._make_bundle
    dynamic_runs = 0

    def fails_dynamic_acceptance(*args: Any, **kwargs: Any) -> SimulationRunBundle:
        nonlocal dynamic_runs
        dynamic_runs += 1
        # Keep the paired baseline valid; every shortlisted candidate fails M4.
        kwargs["accepted"] = dynamic_runs == 1
        return original(*args, **kwargs)

    monkeypatch.setattr(simulator, "_make_bundle", fails_dynamic_acceptance)
    events: list[RtoProgress] = []
    result = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
    )
    assert result["result"]["status"] == "no_verified_candidate"
    assert events[-1].status == "no_feasible"
    assert not any(event.status == "completed" for event in events)
    calls = simulator.evaluate_calls
    events.clear()
    repeated = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root, on_progress=events.append
    )
    assert repeated["result"] == result["result"]
    assert simulator.evaluate_calls == calls
    assert [event.status for event in events] == ["reused", "no_feasible"]
