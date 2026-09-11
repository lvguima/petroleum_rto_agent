from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_staged_runtime import staged_case as staged_case  # noqa: PLC0414

from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.common import canonical_fingerprint
from petroleum_rto.rto.runtime import (
    OperatingContext,
    dump_prepared_optimization,
    load_prepared_optimization,
    read_prepared_result,
    read_prepared_static,
    staged,
)


def _resign(data: dict[str, Any]) -> None:
    data["prepared_fingerprint"] = canonical_fingerprint(
        {key: value for key, value in data.items() if key != "prepared_fingerprint"}
    )


def test_saved_problem_round_trip_is_complete_and_does_not_read_live_inputs(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = tmp_path / "checkout"
    configs = isolated / "configs/rto/capabilities"
    shutil.copytree(repo_root / "configs/rto/capabilities", configs)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    original = staged.prepare_optimization(
        repo_root=isolated,
        context=context,
        objectives=[{"metric_id": "specific_furnace_fuel_energy_mj_per_t", "sense": "minimize"}],
        decision_variables=["furnace_temperature_target_k"],
    )
    saved = json.loads(json.dumps(dump_prepared_optimization(original), allow_nan=False))
    assert saved["capability_bundle"]["catalog"] == original.bundle.catalog.as_dict()
    assert saved["capability_bundle"]["system_policy"] == original.bundle.system_policy.as_dict()
    assert OperatingContext.from_mapping(saved["context"]) == original.context
    (configs / "system_policy.json").write_text('{"changed": true}')
    (configs / "catalog.json").unlink()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("loading a saved problem must not access live inputs or simulation")

    monkeypatch.setattr(staged, "load_capability_bundle", forbidden)
    monkeypatch.setattr(staged, "_orchestrator", forbidden)
    restored = load_prepared_optimization(saved)
    assert restored == original
    assert restored.problem.ref == original.problem.ref
    assert staged.render_confirmation(restored, 3) == staged.render_confirmation(original, 3)


@pytest.mark.parametrize(
    "field",
    ["schema_id", "schema_version", "capability_bundle", "intent", "context", "problem"],
)
def test_saved_problem_rejects_missing_required_content(staged_case: Any, field: str) -> None:
    prepared, simulator, _ = staged_case
    saved = dump_prepared_optimization(prepared)
    del saved[field]
    with pytest.raises(ValueError, match="missing"):
        load_prepared_optimization(saved)
    assert simulator.evaluate_calls == 0


@pytest.mark.parametrize("target", ["outer", "context", "problem", "capability_bundle", "intent"])
def test_saved_problem_rejects_unknown_fields_even_with_updated_digest(
    staged_case: Any, target: str
) -> None:
    prepared, simulator, _ = staged_case
    saved: dict[str, Any] = dump_prepared_optimization(prepared)
    data = saved if target == "outer" else saved[target]
    data["invented"] = True
    _resign(saved)
    with pytest.raises(ValueError, match="unknown"):
        load_prepared_optimization(saved)
    assert simulator.evaluate_calls == 0


@pytest.mark.parametrize("target", ["outer", "context", "problem", "capability_bundle", "intent"])
def test_saved_problem_rejects_unsupported_versions(staged_case: Any, target: str) -> None:
    prepared, simulator, _ = staged_case
    saved: dict[str, Any] = dump_prepared_optimization(prepared)
    data = saved if target == "outer" else saved[target]
    data["schema_version"] = "999.0.0"
    _resign(saved)
    with pytest.raises(ValueError):
        load_prepared_optimization(saved)
    assert simulator.evaluate_calls == 0


def test_saved_problem_rejects_corruption_and_a_valid_but_unbound_problem(staged_case: Any) -> None:
    prepared, simulator, _ = staged_case
    saved: dict[str, Any] = dump_prepared_optimization(prepared)
    saved["context"]["data_timestamp"] = "2026-09-11T10:00:00+08:00"
    with pytest.raises(ValueError, match="prepared_fingerprint"):
        load_prepared_optimization(saved)
    del saved["context"]["context_fingerprint"]
    saved["context"] = OperatingContext.from_mapping(saved["context"]).as_dict()
    _resign(saved)
    with pytest.raises(ValueError, match="deterministic reconstruction"):
        load_prepared_optimization(saved)
    assert simulator.evaluate_calls == 0


def test_saved_problem_requires_nested_fingerprints(staged_case: Any) -> None:
    prepared, _, _ = staged_case
    saved: dict[str, Any] = dump_prepared_optimization(prepared)
    del saved["problem"]["problem_fingerprint"]
    _resign(saved)
    with pytest.raises(ValueError, match="incomplete"):
        load_prepared_optimization(saved)


def test_read_static_and_final_receipts_never_advance_the_workflow(
    staged_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, simulator, root = staged_case
    for reader in (read_prepared_static, read_prepared_result):
        with pytest.raises(ValueError):
            reader(prepared, run_root=root)
    assert not root.exists()
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    calls = simulator.evaluate_calls
    reloaded_static = read_prepared_static(prepared, run_root=root)
    assert reloaded_static == {**static, "physical_m2_executions": 0}
    with pytest.raises(ValueError, match="manifest.json"):
        read_prepared_result(prepared, run_root=root)
    assert simulator.evaluate_calls == calls
    assert not (root / static["workflow_id"] / "dynamic_evaluations.json").exists()
    final = staged.verify_prepared_optimization(
        prepared, static_ref=static["static_ref"], run_root=root
    )
    calls = simulator.evaluate_calls

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("readers must never invoke simulation preview or evaluation")

    monkeypatch.setattr(simulator, "preview", forbidden)
    monkeypatch.setattr(simulator, "evaluate", forbidden)
    assert read_prepared_static(prepared, run_root=root) == reloaded_static
    assert read_prepared_result(prepared, run_root=root) == {
        **final,
        "physical_m2_executions": 0,
        "physical_m4_executions": 0,
    }
    assert simulator.evaluate_calls == calls


@pytest.mark.parametrize("artifact", ["static_solve.json", "static_selection.json", "events.jsonl"])
def test_static_reader_rejects_missing_stage_without_repair(
    staged_case: Any, artifact: str
) -> None:
    prepared, simulator, root = staged_case
    receipt = staged.solve_prepared_optimization(prepared, run_root=root)
    run_dir = root / receipt["workflow_id"]
    (run_dir / artifact).unlink()
    before = {str(path): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    calls = simulator.evaluate_calls
    with pytest.raises(ValueError):
        read_prepared_static(prepared, run_root=root)
    after = {str(path): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    assert after == before
    assert simulator.evaluate_calls == calls


@pytest.mark.parametrize("stage", ["static", "complete"])
def test_readers_require_physical_evidence_not_only_saved_receipts(
    staged_case: Any, stage: str
) -> None:
    prepared, simulator, root = staged_case
    static = staged.solve_prepared_optimization(prepared, run_root=root)
    reader = read_prepared_static
    if stage == "complete":
        staged.verify_prepared_optimization(
            prepared, static_ref=static["static_ref"], run_root=root
        )
        reader = read_prepared_result
    calls = simulator.evaluate_calls
    run_dir = root / static["workflow_id"]
    evidence = next((run_dir / "simulator").rglob("bundle.json"))
    evidence.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        reader(prepared, run_root=root)
    assert simulator.evaluate_calls == calls
