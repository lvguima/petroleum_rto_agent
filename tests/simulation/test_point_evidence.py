"""Offline point evidence boundaries; no COM objects or HYSYS process are used."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from point_boundary_helpers import boundary_for
from test_snapshot import snapshot  # noqa: F401 - shared immutable synthetic observation

from petroleum_rto.simulation import point_evidence as evidence
from petroleum_rto.simulation.boundary import DEFAULT_BOUNDARY, write_boundary_snapshot
from petroleum_rto.simulation.hysys import DEFAULT_CATALOG
from petroleum_rto.simulation.models import (
    OperatingSnapshot,
    SpecificationReading,
    VariableReading,
    write_snapshot,
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def _replace_snapshot(point: SimpleNamespace, name: str, value: OperatingSnapshot) -> None:
    _write_json(point.directory / name, value.to_dict())


def _target_snapshot(before: OperatingSnapshot, target: float) -> OperatingSnapshot:
    changed = evidence._expected_changed(before, target)
    return replace(
        changed,
        specifications=tuple(
            replace(item, current=target) if item.name == evidence.SPECIFICATION else item
            for item in changed.specifications
        ),
    )


@pytest.fixture
def point(tmp_path: Path, snapshot: OperatingSnapshot) -> SimpleNamespace:  # noqa: F811
    directory = tmp_path / "point"
    directory.mkdir()
    payload = b"synthetic frozen HYSYS file"
    digest = hashlib.sha256(payload).hexdigest()
    catalog_bytes = DEFAULT_CATALOG.read_bytes()
    catalog = json.loads(catalog_bytes)
    variables = []
    specs = []
    for binding in catalog["variables"]:
        value = 156.8 if binding["variable_id"] == evidence.VARIABLE_ID else 42.0
        variables.append(
            VariableReading(
                **{
                    name: binding[name]
                    for name in (
                        "variable_id",
                        "row",
                        "role",
                        "object_name",
                        "property_name",
                        "quantity_type",
                        "unit",
                    )
                },
                value=value,
                internal_value=value,
                state=1 if binding["role"] == "mv" else 0,
                can_modify=binding["role"] == "mv",
            )
        )
        if binding["column_specification"] is not None:
            specs.append(
                SpecificationReading(
                    binding["column_specification"],
                    "ColumnTemperatureSpec"
                    if binding["quantity_type"] == "temperature"
                    else "ColumnSpec",
                    True,
                    True,
                    binding["quantity_type"],
                    binding["unit"],
                    value,
                    value,
                )
            )
    baseline = replace(
        snapshot,
        case_id=catalog["case_id"],
        source_case_path=str(tmp_path / "source.hsc"),
        variables=tuple(variables),
        specifications=tuple(specs),
    )
    before = replace(
        baseline,
        source_case_path=str(directory / "candidate.hsc"),
        source_disk_sha256=digest,
        memory_is_dirty=False,
    )
    changed = _target_snapshot(before, 156.9)
    restored = replace(before, source_case_path=str(directory / "restored.hsc"))
    (directory / "variables.json").write_bytes(catalog_bytes)
    (directory / "boundary_definition.json").write_bytes(DEFAULT_BOUNDARY.read_bytes())
    write_boundary_snapshot(directory / "B_boundary.json", boundary_for(changed))
    for name in ("candidate.hsc", "restored.hsc"):
        (directory / name).write_bytes(payload)
    for name, value in {
        "baseline_snapshot.json": baseline,
        "source_before.json": baseline,
        "source_after.json": replace(baseline, observed_at_utc="2026-09-11T08:31:00+00:00"),
        "A_before.json": before,
        "B_changed.json": changed,
        "A_restored.json": restored,
    }.items():
        write_snapshot(directory / name, value)
    raw = {
        "status": "passed",
        "source_unchanged": True,
        "source_disk_unchanged": True,
        "files_unchanged": True,
        "change_error": None,
        "restore_error": None,
        "cleanup_errors": [],
        "baseline_sha256": digest,
        "baseline_manifest_sha256": "b" * 64,
        "implementation_sha256": "c" * 64,
        "writer": "ColumnTemperatureSpec.GoalValue",
        "solver_action": "Reset_then_Run_in_working_case",
        "eligible_for_optimization": False,
        "isolation": "separate_cases_same_application",
        "target_temperature_C": 156.9,
        "actual_temperature_C": 156.9,
        "specification_before": evidence._specification_values(before),
        "specification_after": evidence._specification_values(changed),
    }
    return SimpleNamespace(
        directory=directory,
        digest=digest,
        baseline=baseline,
        before=before,
        changed=changed,
        restored=restored,
        raw=raw,
    )


def _save(point: SimpleNamespace, target: float = 156.9) -> Path:
    return evidence.write_point_result(point.directory, target, point.digest, point.raw)


def test_v2_requires_own_boundary_even_when_runtime_reports_success(point: SimpleNamespace) -> None:
    (point.directory / "B_boundary.json").unlink()
    result = evidence.read_point_result(_save(point))
    assert result.status == "failed" and result.boundary is None


def test_boundary_from_a_different_target_cannot_qualify_point(point: SimpleNamespace) -> None:
    _write_json(point.directory / "B_boundary.json", boundary_for(point.before).to_dict())
    result = evidence.read_point_result(_save(point))
    assert result.status == "failed"


def test_legacy_v1_reader_does_not_invent_boundary(point: SimpleNamespace) -> None:
    for name in ("B_boundary.json", "boundary_definition.json"):
        (point.directory / name).unlink()
    report = _save(point)
    raw = json.loads(report.read_text())
    raw["schema_version"] = "1.0.0"
    raw["checks"], _, _, passed = evidence._evaluate(
        point.directory,
        raw["files"],
        156.9,
        point.digest,
        raw["runtime_observations"],
        (),
        version="1.0.0",
    )
    raw["status"] = "passed" if passed else "failed"
    _write_json(report, raw)
    legacy = evidence.read_point_result(report)
    assert legacy.status == "passed" and legacy.boundary is None
    (point.directory / "boundary_definition.json").write_bytes(DEFAULT_BOUNDARY.read_bytes())
    with pytest.raises(ValueError):
        evidence.read_point_result(report)


@pytest.mark.parametrize("target", [156.8, 156.9, 156.7])
def test_passed_result_recomputes_target_including_unchanged_or_lower_goal(
    point: SimpleNamespace, target: float
) -> None:
    changed = _target_snapshot(point.before, target)
    _replace_snapshot(point, "B_changed.json", changed)
    _write_json(point.directory / "B_boundary.json", boundary_for(changed).to_dict())
    point.raw.update(
        target_temperature_C=target,
        actual_temperature_C=target,
        specification_after=evidence._specification_values(changed),
    )
    result = evidence.read_point_result(_save(point, target))
    assert result.status == "passed"
    assert result.target_c == target
    assert result.baseline_sha256 == point.digest
    assert result.before == point.before
    assert result.restored == point.restored
    assert result.eligible_for_optimization is False
    with pytest.raises(FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.baseline.case_id = "changed"  # type: ignore[misc]
    report = json.loads((point.directory / "report.json").read_text())
    assert report["historical_com_lifecycle_proven_offline"] is False
    assert report["checks"]["target_response"]["within_tolerance"] is True


@pytest.mark.parametrize(
    "name",
    [
        "source_before.json",
        "A_before.json",
        "B_changed.json",
        "A_restored.json",
        "source_after.json",
        "candidate.hsc",
        "restored.hsc",
    ],
)
def test_missing_stage_cannot_inherit_reported_success(point: SimpleNamespace, name: str) -> None:
    (point.directory / name).unlink()
    result = evidence.read_point_result(_save(point))
    assert result.status == "failed"
    report = json.loads((point.directory / "report.json").read_text())
    assert name in report["checks"]["missing_files"]


def test_boundary_failure_without_observations_is_still_readable(point: SimpleNamespace) -> None:
    for name in evidence._SNAPSHOTS[1:]:
        (point.directory / name).unlink()
    point.raw = {
        "status": "failed",
        "error": {
            "phase": "connect",
            "message": "COM unavailable",
            "hresult": -1,
            "scode": None,
        },
    }
    result = evidence.read_point_result(_save(point))
    assert result.status == "failed"
    assert result.before is result.changed is result.restored is None
    assert result.errors[0].stage == "boundary"
    assert result.errors[0].hresult == -1


def test_change_restore_and_cleanup_errors_all_survive(point: SimpleNamespace) -> None:
    error = {"phase": "test", "message": "failure", "hresult": -1, "scode": -2}
    point.raw.update(
        change_error=error, restore_error=error, cleanup_errors=[error], status="passed"
    )
    result = evidence.read_point_result(_save(point))
    assert result.status == "failed"
    assert [item.stage for item in result.errors] == ["change", "restore", "cleanup"]


@pytest.mark.parametrize("name", ["candidate.hsc", "restored.hsc"])
def test_actual_file_drift_is_preserved_as_failed_evidence(
    point: SimpleNamespace, name: str
) -> None:
    (point.directory / name).write_bytes(b"changed during execution")
    result = evidence.read_point_result(_save(point))
    assert result.status == "failed"
    report = json.loads((point.directory / "report.json").read_text())
    assert report["checks"]["case_files_match_baseline"][name] is False
    assert report["files"][name] != point.digest


@pytest.mark.parametrize(
    "failure", ["initial", "other_mv", "restore", "state", "residual", "source", "path"]
)
def test_independent_checks_reject_false_success(point: SimpleNamespace, failure: str) -> None:
    name, value = "B_changed.json", point.changed
    if failure in ("initial", "restore", "other_mv"):
        if failure == "initial":
            name, value = "A_before.json", point.before
        elif failure == "restore":
            name, value = "A_restored.json", point.restored
        value = replace(
            value, variables=(replace(value.variables[0], value=999), *value.variables[1:])
        )
    elif failure == "state":
        value = replace(value, consistent_observation=False)
    elif failure == "residual":
        value = replace(
            value,
            specifications=tuple(
                replace(item, current=156.8) if item.name == evidence.SPECIFICATION else item
                for item in value.specifications
            ),
        )
    elif failure == "source":
        name, value = "source_after.json", replace(point.baseline, memory_is_dirty=False)
    else:
        value = replace(value, source_case_path=point.baseline.source_case_path)
    _replace_snapshot(point, name, value)
    assert evidence.read_point_result(_save(point)).status == "failed"


@pytest.mark.parametrize("field", ["source_unchanged", "source_disk_unchanged", "files_unchanged"])
def test_negative_or_unknown_runtime_protection_vetoes_success(
    point: SimpleNamespace, field: str
) -> None:
    point.raw.pop(field)
    assert evidence.read_point_result(_save(point)).status == "failed"


@pytest.mark.parametrize(
    "field",
    [
        "writer",
        "solver_action",
        "baseline_manifest_sha256",
        "implementation_sha256",
        "specification_before",
        "specification_after",
        "target_temperature_C",
        "actual_temperature_C",
    ],
)
def test_incomplete_runtime_observations_cannot_claim_success(
    point: SimpleNamespace, field: str
) -> None:
    point.raw.pop(field)
    assert evidence.read_point_result(_save(point)).status == "failed"


@pytest.mark.parametrize(
    "field", ["writer", "solver_action", "specification_before", "specification_after"]
)
def test_contradictory_runtime_observations_fail(point: SimpleNamespace, field: str) -> None:
    if field.startswith("specification"):
        point.raw[field]["active_goal_C"] = 999.0
    else:
        point.raw[field] = "unsupported operation"
    assert evidence.read_point_result(_save(point)).status == "failed"


@pytest.mark.parametrize(
    "mutation", ["status", "checks", "unknown", "missing", "boolean_target", "nan", "duplicate"]
)
def test_report_tampering_is_rejected(point: SimpleNamespace, mutation: str) -> None:
    path = _save(point)
    raw = json.loads(path.read_text())
    if mutation == "status":
        raw["status"] = "failed"
    elif mutation == "checks":
        raw["checks"]["target_response"]["within_tolerance"] = 1
    elif mutation == "unknown":
        raw["extra"] = 1
    elif mutation == "missing":
        raw.pop("errors")
    elif mutation == "boolean_target":
        raw["target_c"] = True
    else:
        text = path.read_text()
        text = text.replace(
            '"target_c": 156.9',
            '"target_c": NaN' if mutation == "nan" else '"target_c": 156.9, "target_c": 156.9',
        )
        path.write_text(text)
    if mutation not in ("nan", "duplicate"):
        _write_json(path, raw)
    with pytest.raises((ValueError, TypeError)):
        evidence.read_point_result(path)


@pytest.mark.parametrize("name", ["B_changed.json", "candidate.hsc", "variables.json"])
def test_post_report_file_changes_fail_integrity(point: SimpleNamespace, name: str) -> None:
    path = _save(point)
    with (point.directory / name).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="hash differs"):
        evidence.read_point_result(path)


def test_deleting_B_and_its_hash_cannot_keep_success(point: SimpleNamespace) -> None:
    path = _save(point)
    (point.directory / "B_changed.json").unlink()
    raw = json.loads(path.read_text())
    raw["files"].pop("B_changed.json")
    _write_json(path, raw)
    with pytest.raises(ValueError, match="status differs"):
        evidence.read_point_result(path)


def test_unknown_file_path_and_catalog_identity_are_rejected(point: SimpleNamespace) -> None:
    path = _save(point)
    raw = json.loads(path.read_text())
    raw["files"]["../outside.hsc"] = point.digest
    _write_json(path, raw)
    with pytest.raises(ValueError, match="filename"):
        evidence.read_point_result(path)
    raw["files"].pop("../outside.hsc")
    catalog_path = point.directory / "variables.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["case_id"] = "different-case"
    _write_json(catalog_path, catalog)
    raw["files"]["variables.json"] = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    _write_json(path, raw)
    with pytest.raises(ValueError, match="qualify"):
        evidence.read_point_result(path)


def test_hard_links_are_rejected(point: SimpleNamespace) -> None:
    path = _save(point)
    target = point.directory / "candidate.hsc"
    os.link(target, point.directory / "linked.hsc")
    with pytest.raises(ValueError, match="without links"):
        evidence.read_point_result(path)


def test_existing_report_is_not_overwritten(point: SimpleNamespace) -> None:
    path = _save(point)
    previous = path.read_bytes()
    with pytest.raises(FileExistsError):
        _save(point)
    assert path.read_bytes() == previous


@pytest.mark.parametrize("target", [True, None, "156.9", float("nan"), float("inf"), 10**1000])
def test_invalid_targets_fail_before_report_write(point: SimpleNamespace, target: Any) -> None:
    with pytest.raises(ValueError):
        _save(point, target)
    assert not (point.directory / "report.json").exists()
