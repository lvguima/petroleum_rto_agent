"""Fixed-order orchestration through a fake point runner and the real strict evidence reader."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from point_boundary_helpers import boundary_for
from test_point_evidence import point  # noqa: F401 - complete synthetic point artifacts
from test_snapshot import snapshot  # noqa: F401 - dependency of the shared point fixture

from petroleum_rto.simulation import point_evidence as evidence
from petroleum_rto.simulation.boundary import DEFAULT_BOUNDARY, write_boundary_snapshot
from petroleum_rto.simulation.models import write_snapshot
from scripts.simulation import verify_t39_order as order


@pytest.fixture
def experiment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: SimpleNamespace,  # noqa: F811
) -> SimpleNamespace:
    directory = tmp_path / "baseline"
    directory.mkdir()
    payload = (point.directory / "candidate.hsc").read_bytes()
    catalog_bytes = (point.directory / "variables.json").read_bytes()
    (directory / "baseline.hsc").write_bytes(payload)
    (directory / "variables.json").write_bytes(catalog_bytes)
    write_snapshot(directory / "snapshot.json", point.baseline)
    manifest = directory / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_id": "hysys-baseline",
                "schema_version": "1.0.0",
                "source_unchanged": True,
                "reopen_verified": False,
                "eligible_for_optimization": False,
                "files": {
                    name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                    for name in ("baseline.hsc", "snapshot.json", "variables.json")
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    state = SimpleNamespace(
        manifest=manifest,
        baseline=point.baseline,
        directory=directory,
        output=tmp_path / "order",
        calls=[],
        failed_at=None,
        raise_at=None,
        error_type=RuntimeError,
        difference_at=None,
        foreign_baseline_at=None,
        foreign_catalog_at=None,
        foreign_manifest_at=None,
        tamper_at=None,
        unexpected_path_at=None,
        before_run=lambda index: None,
        after_run=lambda index: None,
    )

    def run_point(actual_manifest: Path, target: float, output: Path) -> Path:
        index = len(state.calls) + 1
        assert actual_manifest == manifest
        assert not output.exists(), "each point requires a new directory"
        state.calls.append((index, target, output))
        state.before_run(index)
        if state.raise_at == index:
            raise state.error_type("synthetic point failure")
        output.mkdir()
        baseline = point.baseline
        if state.foreign_baseline_at == index:
            baseline = replace(
                baseline,
                stages=(replace(baseline.stages[0], temperature_C=160.0), *baseline.stages[1:]),
            )
        before = replace(
            baseline,
            source_case_path=str(output / "candidate.hsc"),
            source_disk_sha256=point.digest,
            memory_is_dirty=False,
        )
        changed = evidence._expected_changed(before, target)
        changed = replace(
            changed,
            specifications=tuple(
                replace(item, current=target) if item.name == evidence.SPECIFICATION else item
                for item in changed.specifications
            ),
        )
        if state.difference_at == index:
            changed = replace(
                changed,
                stages=(
                    replace(
                        changed.stages[0], temperature_C=changed.stages[0].temperature_C + 1e-8
                    ),
                    *changed.stages[1:],
                ),
            )
        values = {
            "baseline_snapshot.json": baseline,
            "source_before.json": baseline,
            "source_after.json": baseline,
            "A_before.json": before,
            "B_changed.json": changed,
            "A_restored.json": replace(before, source_case_path=str(output / "restored.hsc")),
        }
        for name, value in values.items():
            write_snapshot(output / name, value)
        (output / "boundary_definition.json").write_bytes(DEFAULT_BOUNDARY.read_bytes())
        write_boundary_snapshot(output / "B_boundary.json", boundary_for(changed))
        for name in ("candidate.hsc", "restored.hsc"):
            (output / name).write_bytes(payload)
        (output / "variables.json").write_bytes(
            catalog_bytes + (b"\n" if state.foreign_catalog_at == index else b"")
        )
        raw = {
            **point.raw,
            "baseline_manifest_sha256": "a" * 64
            if state.foreign_manifest_at == index
            else manifest_hash,
            "target_temperature_C": target,
            "actual_temperature_C": target,
            "specification_before": evidence._specification_values(before),
            "specification_after": evidence._specification_values(changed),
        }
        if state.failed_at == index:
            raw.update(
                status="failed",
                change_error={
                    "phase": "write-target",
                    "message": "synthetic failed point",
                    "hresult": -1,
                    "scode": -2,
                },
            )
        path = evidence.write_point_result(output, target, point.digest, raw)
        if state.tamper_at == index:
            changed_path = output / "B_changed.json"
            changed_path.write_bytes(changed_path.read_bytes() + b"\n")
        state.after_run(index)
        return output / "unexpected.json" if state.unexpected_path_at == index else path

    monkeypatch.setattr(order, "run_t39_point", run_point)
    state.run = lambda: json.loads(
        order.verify_t39_order(manifest, state.output).read_text(encoding="utf-8")
    )
    return state


def test_fixed_order_strictly_reloads_each_new_point_and_compares_full_snapshots(
    experiment: SimpleNamespace,
) -> None:
    report = experiment.run()
    assert [target for _, target, _ in experiment.calls] == [156.8, 156.9, 156.9, 156.8]
    assert [path.name for _, _, path in experiment.calls] == ["01-A", "02-B", "03-B", "04-A"]
    assert report["execution_order"] == ["A", "B", "B", "A"]
    assert report["status"] == "passed" and not report["errors"]
    assert report["eligible_for_optimization"] is False
    for comparison in report["comparisons"].values():
        assert comparison == {
            "equivalent": True,
            "input_differences": [],
            "output_differences": [],
            "state_differences": [],
        }
    for run in report["runs"]:
        assert run["status"] == "passed"
        assert (
            run["result_sha256"]
            == hashlib.sha256(Path(run["result_path"]).read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_failed_point_stops_without_retry_and_keeps_its_error(
    experiment: SimpleNamespace, index: int
) -> None:
    experiment.failed_at = index
    report = experiment.run()
    assert len(experiment.calls) == index
    assert report["status"] == "failed"
    assert report["runs"][-1]["point_errors"][0]["message"] == "synthetic failed point"
    assert report["errors"][0]["phase"] == "strict-reload-point"
    assert report["errors"][0]["point_index"] == index


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_runner_exception_or_interruption_stops_and_preserves_completed_runs(
    experiment: SimpleNamespace, error_type: type[BaseException]
) -> None:
    experiment.raise_at = 2
    experiment.error_type = error_type
    report = experiment.run()
    assert len(experiment.calls) == 2
    assert report["status"] == "failed"
    assert report["runs"][0]["status"] == "passed"
    assert report["runs"][1]["status"] == "not-completed"
    assert report["errors"][0]["exception_type"] == error_type.__name__


@pytest.mark.parametrize(("index", "comparison"), [(3, "B_B"), (4, "A_A")])
def test_any_same_target_output_difference_fails_zero_tolerance(
    experiment: SimpleNamespace, index: int, comparison: str
) -> None:
    experiment.difference_at = index
    report = experiment.run()
    assert len(experiment.calls) == 4
    assert all(run["status"] == "passed" for run in report["runs"])
    assert report["status"] == "failed"
    assert not report["comparisons"][comparison]["equivalent"]
    assert report["comparisons"][comparison]["output_differences"]


@pytest.mark.parametrize(
    "field", ["foreign_baseline_at", "foreign_catalog_at", "foreign_manifest_at"]
)
def test_each_point_must_use_the_same_frozen_input(experiment: SimpleNamespace, field: str) -> None:
    setattr(experiment, field, 2)
    report = experiment.run()
    assert len(experiment.calls) == 2
    assert report["status"] == "failed"
    assert "same frozen input" in report["errors"][0]["message"]


def test_tampered_point_payload_is_rejected_before_next_run(experiment: SimpleNamespace) -> None:
    experiment.tamper_at = 2
    report = experiment.run()
    assert len(experiment.calls) == 2 and report["status"] == "failed"
    assert report["errors"][0]["phase"] == "strict-reload-point"


@pytest.mark.parametrize("file", ["report.json", "B_changed.json"])
def test_prior_result_hash_or_payload_drift_prevents_further_runs(
    experiment: SimpleNamespace, file: str
) -> None:
    def mutate_previous(index: int) -> None:
        if index == 2:
            path = experiment.calls[0][2] / file
            path.write_bytes(path.read_bytes() + b"\n")

    experiment.after_run = mutate_previous
    report = experiment.run()
    assert len(experiment.calls) == 2 and report["status"] == "failed"
    assert report["errors"][0]["phase"] == "verify-prior-evidence"


@pytest.mark.parametrize(
    "file", ["baseline.hsc", "snapshot.json", "variables.json", "manifest.json"]
)
def test_polluted_frozen_input_is_rejected_before_output_or_point_api(
    experiment: SimpleNamespace, file: str
) -> None:
    (experiment.directory / file).write_bytes(b"corrupt")
    with pytest.raises((ValueError, OSError)):
        experiment.run()
    assert experiment.calls == [] and not experiment.output.exists()


def test_frozen_file_drift_during_a_point_stops_next_point(experiment: SimpleNamespace) -> None:
    experiment.after_run = lambda index: (experiment.directory / "variables.json").write_bytes(
        b"changed"
    )
    report = experiment.run()
    assert len(experiment.calls) == 1 and report["status"] == "failed"
    assert report["errors"][0]["phase"] == "verify-frozen-input-after-point"


def test_preexisting_output_is_not_reused(experiment: SimpleNamespace) -> None:
    experiment.output.mkdir()
    marker = experiment.output / "keep.txt"
    marker.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        experiment.run()
    assert experiment.calls == [] and marker.read_bytes() == b"keep"


def test_polluted_later_point_directory_is_not_given_to_runner(experiment: SimpleNamespace) -> None:
    def pollute(index: int) -> None:
        if index == 1:
            (experiment.output / "02-B").mkdir()

    experiment.after_run = pollute
    report = experiment.run()
    assert len(experiment.calls) == 1 and report["status"] == "failed"
    assert report["errors"][0]["exception_type"] == "FileExistsError"


def test_unexpected_result_path_is_not_followed(experiment: SimpleNamespace) -> None:
    experiment.unexpected_path_at = 1
    report = experiment.run()
    assert len(experiment.calls) == 1 and report["status"] == "failed"
    assert "unexpected result path" in report["errors"][0]["message"]


@pytest.mark.parametrize("condition", ["unqualified", "indistinguishable_step"])
def test_invalid_fixed_order_input_is_rejected_before_point_api(
    experiment: SimpleNamespace, condition: str
) -> None:
    snapshot_path = experiment.directory / "snapshot.json"
    value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if condition == "unqualified":
        value["solver"]["can_solve"] = False
    else:
        reading = next(
            item for item in value["variables"] if item["variable_id"] == evidence.VARIABLE_ID
        )
        reading["value"] = reading["internal_value"] = 1e20
        spec = next(
            item for item in value["specifications"] if item["name"] == evidence.SPECIFICATION
        )
        spec["goal"] = 1e20
    snapshot_path.write_text(json.dumps(value), encoding="utf-8")
    manifest = json.loads(experiment.manifest.read_text(encoding="utf-8"))
    manifest["files"]["snapshot.json"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    experiment.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        experiment.run()
    assert experiment.calls == [] and not experiment.output.exists()


def test_cli_has_only_baseline_and_output_arguments(
    experiment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_t39_order",
            "--baseline",
            str(experiment.manifest),
            "--output",
            str(experiment.output),
        ],
    )
    assert order.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_cli_does_not_accept_a_custom_target(
    experiment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_t39_order",
            "--baseline",
            str(experiment.manifest),
            "--output",
            str(experiment.output),
            "--target",
            "157.0",
        ],
    )
    with pytest.raises(SystemExit) as failure:
        order.main()
    assert failure.value.code == 2
    assert experiment.calls == [] and not experiment.output.exists()
