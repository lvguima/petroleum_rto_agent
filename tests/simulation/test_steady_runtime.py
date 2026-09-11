"""Historical v1 plan execution and evidence reload with synthetic physical runs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_point_evidence import point  # noqa: F401
from test_snapshot import snapshot  # noqa: F401
from test_t39_order import experiment, order  # noqa: F401

from petroleum_rto.rto.runtime import steady


@pytest.fixture
def flow(
    experiment: SimpleNamespace,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    plan = steady.load_prepared_comparison(
        {
            "schema_id": "steady-comparison-plan",
            "schema_version": "1.0.0",
            "context": experiment.baseline.to_dict(),
            "target_c": 156.9,
            "boundary_sha256": steady.backend.definition_hash(),
        }
    )
    root = tmp_path / "rto"
    captures = []

    def capture(context, directory, workspace):
        captures.append(context)
        shutil.copytree(experiment.directory, directory)

    monkeypatch.setattr(steady.backend, "capture", capture)
    monkeypatch.setattr(
        steady.backend,
        "run_point",
        lambda baseline, target, output: order.run_t39_point(experiment.manifest, target, output),
    )
    return SimpleNamespace(
        plan=plan,
        experiment=experiment,
        captures=captures,
        root=root,
        directory=root / steady.workflow_id(plan),
        run=lambda: steady.execute_comparison(plan, run_root=root, workspace=tmp_path),
    )


def test_full_pair_rebuilds_own_boundary_metrics_and_reloads_without_reexecution(flow):
    result = flow.run()
    assert result["result"]["status"] == "comparison_only"
    assert result["result"]["eligible_for_optimization"] is False
    assert len(result["result"]["comparisons"]) == 12
    assert [x[1] for x in flow.experiment.calls] == [156.8, 156.9]
    assert steady.inspect_comparison(flow.directory) == result
    assert flow.run() == result and len(flow.experiment.calls) == 2 and len(flow.captures) == 1


def test_failed_baseline_stops_before_candidate_and_retains_failure(flow):
    flow.experiment.failed_at = 1
    result = flow.run()["result"]
    assert result["status"] == "evaluation_error" and result["candidate"] is None
    assert len(flow.experiment.calls) == 1
    assert flow.run()["result"] == result and len(flow.experiment.calls) == 1


def test_completed_baseline_survives_interruption_without_repeating_it(flow):
    flow.experiment.raise_at = 2
    with pytest.raises(RuntimeError):
        flow.run()
    flow.experiment.raise_at = None
    assert flow.run()["result"]["status"] == "comparison_only"
    assert [x[1] for x in flow.experiment.calls] == [156.8, 156.9, 156.9]
    # The first candidate attempt failed before creating or modifying its case.
    assert len(flow.captures) == 1


def test_partial_candidate_directory_is_never_reexecuted_or_overwritten(flow):
    flow.experiment.raise_at = 2
    with pytest.raises(RuntimeError):
        flow.run()
    (flow.directory / "candidate").mkdir()
    flow.experiment.raise_at = None
    with pytest.raises(FileNotFoundError):
        flow.run()
    assert len(flow.experiment.calls) == 2


@pytest.mark.parametrize(
    "field", ["foreign_baseline_at", "foreign_catalog_at", "foreign_manifest_at", "tamper_at"]
)
def test_foreign_or_tampered_candidate_is_not_a_process_failure(flow, field):
    setattr(flow.experiment, field, 2)
    with pytest.raises(ValueError):
        flow.run()
    assert not (flow.directory / "result.json").exists()


def test_changed_boundary_definition_requires_new_preparation(flow, monkeypatch):
    monkeypatch.setattr(steady.backend, "definition_hash", lambda: "e" * 64)
    with pytest.raises(ValueError, match="prepare again"):
        flow.run()
    assert not flow.experiment.calls and not flow.captures


def test_result_summary_is_recomputed_not_trusted(flow):
    flow.run()
    path = flow.directory / "result.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["comparisons"][0]["delta"] += 1000
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="physical evidence"):
        steady.inspect_comparison(flow.directory)


def test_plan_rejects_old_contract_and_disallowed_target(flow):
    raw = flow.plan.as_dict()
    with pytest.raises(ValueError):
        steady.load_prepared_comparison(raw | {"schema_version": "0.1.0"})
    with pytest.raises(ValueError):
        steady.prepare_comparison(raw["context"], 160)
