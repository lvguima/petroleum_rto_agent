"""Strict, offline evidence for one T-39 target and a subsequent baseline restore."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .baseline import _file_bytes, _json, _plain_directory, _sha256, _stable
from .boundary import BoundaryDefinition, BoundarySnapshot
from .comparison import compare_operating_points
from .hysys import VariableCatalog, load_catalog
from .models import OperatingSnapshot
from .mv import WRITER as MV_WRITER
from .mv import MVChange, check_changes, parse_changes

VARIABLE_ID = "C-1102.39_temperature_C"
SPECIFICATION = "T-39"
TRACKING_TOLERANCE_C = 0.01
WRITER = "ColumnTemperatureSpec.GoalValue"
SOLVER_ACTION = "Reset_then_Run_in_working_case"
_DECLARATIONS = {
    "schema_id": "hysys-t39-point",
    "schema_version": "2.0.0",
    "eligible_for_optimization": False,
    "historical_com_lifecycle_proven_offline": False,
    "isolation": "separate_cases_same_application",
}
_SNAPSHOTS = (
    "baseline_snapshot.json",
    "source_before.json",
    "A_before.json",
    "B_changed.json",
    "A_restored.json",
    "source_after.json",
)
_V1_FILES = {*_SNAPSHOTS, "variables.json", "candidate.hsc", "restored.hsc"}
_FILES = _V1_FILES | {"boundary_definition.json", "B_boundary.json"}
_ERROR_FIELDS = {
    "change_error": "change",
    "restore_error": "restore",
    "source_protection_error": "source",
    "integrity_error": "integrity",
    "error": "boundary",
}
_RUNTIME_FIELDS = {
    "status",
    "source_unchanged",
    "source_disk_unchanged",
    "files_unchanged",
    "baseline_manifest_sha256",
    "implementation_sha256",
    "writer",
    "solver_action",
    "specification_before",
    "specification_after",
    "target_temperature_C",
    "actual_temperature_C",
}
_RAW_FIELDS = (
    _RUNTIME_FIELDS
    | set(_ERROR_FIELDS)
    | {
        "cleanup_errors",
        "initial_comparison",
        "change_comparison",
        "restoration_comparison",
        "temperature_response",
        "baseline_sha256",
        "scope",
        "isolation",
        "eligible_for_optimization",
    }
)


@dataclass(frozen=True, slots=True)
class PointError:
    stage: str
    phase: str
    message: str
    hresult: int | None
    scode: int | None


@dataclass(frozen=True, slots=True)
class PointResult:
    status: Literal["passed", "failed"]
    target_c: float | None
    baseline_sha256: str
    baseline: OperatingSnapshot
    before: OperatingSnapshot | None
    changed: OperatingSnapshot | None
    restored: OperatingSnapshot | None
    errors: tuple[PointError, ...]
    boundary: BoundarySnapshot | None = None
    eligible_for_optimization: Literal[False] = False
    changes: tuple[MVChange, ...] = ()


def _record(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{label} has missing or unknown fields")
    return value


def _number(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("Expected a finite number, not a Boolean")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("Expected a finite number") from error
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def _digest(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Expected a lowercase SHA-256")
    return value


def _errors(value: Any) -> tuple[PointError, ...]:
    if type(value) is not list:
        raise ValueError("Errors must be an array")
    result = []
    for item in value:
        _record(item, {"stage", "phase", "message", "hresult", "scode"}, "Point error")
        if item["stage"] not in {*_ERROR_FIELDS.values(), "cleanup"}:
            raise ValueError("Unknown point error stage")
        for name in ("stage", "phase", "message"):
            if type(item[name]) is not str or (name != "message" and not item[name].strip()):
                raise ValueError("Error descriptions must be text")
        for name in ("hresult", "scode"):
            if item[name] is not None and type(item[name]) is not int:
                raise ValueError("COM error codes must be integers or null")
        result.append(PointError(**item))
    return tuple(result)


def _runtime(value: Any, *, multi: bool = False) -> dict[str, Any]:
    fields = (
        _RUNTIME_FIELDS
        - {
            "specification_before",
            "specification_after",
            "target_temperature_C",
            "actual_temperature_C",
        }
        if multi
        else _RUNTIME_FIELDS
    )
    item = _record(value, fields, "Runtime observations")
    if type(item["status"]) is not str or item["status"] not in ("passed", "failed"):
        raise ValueError("Invalid runtime status")
    for name in ("source_unchanged", "source_disk_unchanged", "files_unchanged"):
        if item[name] is not None and type(item[name]) is not bool:
            raise ValueError("Runtime protection observations must be Boolean or null")
    for name in ("baseline_manifest_sha256", "implementation_sha256"):
        if item[name] is not None:
            _digest(item[name])
    for name in ("writer", "solver_action"):
        if item[name] is not None and (type(item[name]) is not str or not item[name].strip()):
            raise ValueError("Runtime operation descriptions must be text or null")
    if multi:
        return item
    for name in ("target_temperature_C", "actual_temperature_C"):
        if item[name] is not None:
            _number(item[name])
    spec_fields = {
        "goal_value_internal",
        "goal_C",
        "current_C",
        "active_goal_value_internal",
        "active_goal_C",
        "active_current_C",
    }
    for name in ("specification_before", "specification_after"):
        if item[name] is not None:
            for number in _record(item[name], spec_fields, name).values():
                _number(number)
    return item


def _path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(value))


def _bindings_match(catalog: VariableCatalog, snapshot: OperatingSnapshot) -> bool:
    if catalog.case_id != snapshot.case_id:
        return False
    readings = {item.variable_id: item for item in snapshot.variables}
    specs = {item.name: item for item in snapshot.specifications}
    for binding in catalog.variables:
        reading = readings.get(binding.variable_id)
        if reading is None or any(
            getattr(binding, name) != getattr(reading, name)
            for name in ("row", "role", "object_name", "property_name", "quantity_type", "unit")
        ):
            return False
        if binding.column_specification is not None:
            spec = specs.get(binding.column_specification)
            if (
                spec is None
                or not spec.active
                or spec.quantity_type != binding.quantity_type
                or spec.unit != binding.unit
                or spec.goal != reading.value
            ):
                return False
    return True


def _qualified(catalog: VariableCatalog, baseline: OperatingSnapshot) -> bool:
    binding = next((item for item in catalog.variables if item.variable_id == VARIABLE_ID), None)
    reading = next((item for item in baseline.variables if item.variable_id == VARIABLE_ID), None)
    spec = next((item for item in baseline.specifications if item.name == SPECIFICATION), None)
    return bool(
        binding is not None
        and reading is not None
        and spec is not None
        and catalog.column_name == "C-1102"
        and catalog.table_name == "Table"
        and binding.row == 23
        and binding.object_name == "C-1102"
        and binding.property_name == "规定值 (T-39)"
        and binding.role == "mv"
        and binding.quantity_type == "temperature"
        and binding.unit == "C"
        and binding.column_specification == SPECIFICATION
        and reading.state == 1
        and reading.can_modify
        and reading.internal_value == reading.value
        and spec.active
        and spec.specification_type == "ColumnTemperatureSpec"
        and baseline.solver.can_solve
        and _stable(baseline)
    )


def _expected_changed(before: OperatingSnapshot, target: float) -> OperatingSnapshot:
    return replace(
        before,
        variables=tuple(
            replace(item, value=target, internal_value=target)
            if item.variable_id == VARIABLE_ID
            else item
            for item in before.variables
        ),
        specifications=tuple(
            replace(item, goal=target) if item.name == SPECIFICATION else item
            for item in before.specifications
        ),
    )


def _specification_values(snapshot: OperatingSnapshot | None) -> dict[str, float | None] | None:
    if snapshot is None:
        return None
    variable = next((item for item in snapshot.variables if item.variable_id == VARIABLE_ID), None)
    spec = next((item for item in snapshot.specifications if item.name == SPECIFICATION), None)
    if variable is None or spec is None:
        return None
    return {
        "goal_value_internal": variable.internal_value,
        "goal_C": spec.goal,
        "current_C": spec.current,
        "active_goal_value_internal": variable.internal_value,
        "active_goal_C": spec.goal,
        "active_current_C": spec.current,
    }


def _evaluate(
    directory: Path,
    files: dict[str, Any],
    target: float | tuple[MVChange, ...],
    digest: str,
    runtime: dict[str, Any],
    errors: tuple[PointError, ...],
    *,
    version: str = "2.0.0",
) -> tuple[dict[str, Any], dict[str, OperatingSnapshot], BoundarySnapshot | None, bool]:
    if type(files) is not dict or not {"baseline_snapshot.json", "variables.json"} <= files.keys():
        raise ValueError("Missing baseline snapshot or catalog")
    required_files = _V1_FILES if version == "1.0.0" else _FILES
    if set(files) - required_files:
        raise ValueError("Unexpected point evidence filename")
    if {name for name in _FILES if os.path.lexists(directory / name)} != files.keys():
        raise ValueError("Point evidence inventory differs from the manifest")
    payloads = {}
    for name, expected in files.items():
        payload = _file_bytes(directory / name)
        if _sha256(payload) != _digest(expected):
            raise ValueError(f"Point file hash differs: {name}")
        payloads[name] = payload
    snapshots = {
        name: OperatingSnapshot.from_dict(_json(payloads[name]))
        for name in _SNAPSHOTS
        if name in payloads
    }
    baseline = snapshots["baseline_snapshot.json"]
    catalog = load_catalog(directory / "variables.json")
    if _file_bytes(directory / "variables.json") != payloads["variables.json"]:
        raise ValueError("Point catalog changed while reading")
    if not _bindings_match(catalog, baseline):
        raise ValueError("Baseline and catalog do not qualify the point input")
    if isinstance(target, tuple):
        if parse_changes([c.to_dict() for c in target], baseline, catalog) != target:
            raise ValueError("MV targets are not canonical")
    elif not _qualified(catalog, baseline):
        raise ValueError("Baseline does not qualify the T-39 point input")
    before, changed, restored = (snapshots.get(name) for name in _SNAPSHOTS[2:5])
    source_before = snapshots.get("source_before.json")
    source_after = snapshots.get("source_after.json")
    identity = {}
    for name, snapshot in snapshots.items():
        expected_path = {
            "A_before.json": directory / "candidate.hsc",
            "B_changed.json": directory / "candidate.hsc",
            "A_restored.json": directory / "restored.hsc",
        }.get(name, Path(baseline.source_case_path))
        is_work = name in ("A_before.json", "B_changed.json", "A_restored.json")
        identity[name] = (
            _bindings_match(catalog, snapshot)
            and snapshot.hysys_version == baseline.hysys_version
            and _path(snapshot.source_case_path) == _path(expected_path)
            and (not is_work or snapshot.source_disk_sha256 == digest)
            and (not is_work or _path(expected_path) != _path(baseline.source_case_path))
        )
    initial = None if before is None else compare_operating_points(baseline, before)
    restoration = None if restored is None else compare_operating_points(baseline, restored)
    change = None
    response = None
    current = None
    if isinstance(target, tuple):
        if before is not None and changed is not None:
            change, responses = check_changes(before, changed, target, catalog)
            response = {
                "variables": responses,
                "within_tolerance": all(
                    r["setpoint_matches"] and r["tracking_passed"] for r in responses
                ),
            }
    else:
        if before is not None and changed is not None:
            change = compare_operating_points(_expected_changed(before, target), changed)
        if changed is not None:
            current = next(
                (x.current for x in changed.specifications if x.name == SPECIFICATION), None
            )
        residual = None if current is None else current - target
        if current is not None:
            response = {
                "current_c": current,
                "residual_c": residual
                if residual is not None and math.isfinite(residual)
                else None,
                "tolerance_c": TRACKING_TOLERANCE_C,
                "within_tolerance": residual is not None and abs(residual) <= TRACKING_TOLERANCE_C,
            }
    source_equal = (
        None
        if source_before is None or source_after is None
        else (
            _stable(source_before)
            and _stable(source_after)
            and replace(source_after, observed_at_utc=source_before.observed_at_utc)
            == source_before
        )
    )
    case_files = {
        name: None if name not in files else bool(payloads[name]) and files[name] == digest
        for name in ("candidate.hsc", "restored.hsc")
    }
    if isinstance(target, tuple):
        runtime_consistent = (
            runtime["writer"] == MV_WRITER
            and runtime["solver_action"] == SOLVER_ACTION
            and runtime["baseline_manifest_sha256"] is not None
            and runtime["implementation_sha256"] is not None
            and before is not None
            and changed is not None
        )
    else:
        runtime_consistent = (
            runtime["writer"] == WRITER
            and runtime["solver_action"] == SOLVER_ACTION
            and runtime["baseline_manifest_sha256"] is not None
            and runtime["implementation_sha256"] is not None
            and runtime["target_temperature_C"] == target
            and current is not None
            and runtime["actual_temperature_C"] == current
            and before is not None
            and runtime["specification_before"] == _specification_values(before)
            and changed is not None
            and runtime["specification_after"] == _specification_values(changed)
        )
    checks = {
        "missing_files": sorted(required_files - files.keys()),
        "snapshot_identity": identity,
        "case_files_match_baseline": case_files,
        "initial_comparison": initial,
        "change_comparison": change,
        "restoration_comparison": restoration,
        "target_response": response,
        "source_observation_unchanged": source_equal,
        "runtime_values_consistent": runtime_consistent,
    }
    boundary = None
    boundary_matches = False
    if version != "1.0.0":
        definition = (
            None
            if "boundary_definition.json" not in payloads
            else BoundaryDefinition.from_dict(_json(payloads["boundary_definition.json"]))
        )
        if definition is not None and (
            definition.case_id != baseline.case_id or definition.column_name != catalog.column_name
        ):
            raise ValueError("Boundary definition identifies a different baseline")
        if "B_boundary.json" in payloads:
            boundary = BoundarySnapshot.from_dict(_json(payloads["B_boundary.json"]))
            boundary_matches = bool(
                changed is not None
                and boundary.definition == definition
                and boundary.consistent_observation
                and replace(boundary.core_before, observed_at_utc=changed.observed_at_utc)
                == changed
            )
        checks["boundary_matches_changed_point"] = boundary_matches
    passed = bool(
        (version == "1.0.0" or boundary_matches)
        and not errors
        and not checks["missing_files"]
        and all(identity.values())
        and all(case_files.values())
        and initial is not None
        and initial["equivalent"]
        and change is not None
        and not change["input_differences"]
        and not change["state_differences"]
        and response is not None
        and response["within_tolerance"]
        and restoration is not None
        and restoration["equivalent"]
        and source_equal
        and runtime_consistent
        and runtime["status"] == "passed"
        and all(
            runtime[name] is True
            for name in ("source_unchanged", "source_disk_unchanged", "files_unchanged")
        )
    )
    return checks, snapshots, boundary, passed


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False)


def read_point_result(path: Path) -> PointResult:
    """Reload local evidence and recompute checks; never connect to or solve HYSYS.

    Historical object ownership, successful Close calls and original file checks
    remain recorded runtime observations, not facts independently proven offline.
    """
    path = path.expanduser().absolute()
    if path.name != "report.json":
        raise ValueError("Point evidence must be named report.json")
    payload = _json(_file_bytes(path))
    multi = type(payload) is dict and payload.get("schema_id") == "hysys-mv-point"
    raw = _record(
        payload,
        set(_DECLARATIONS)
        | {
            "status",
            "changes" if multi else "target_c",
            "baseline_sha256",
            "files",
            "runtime_observations",
            "errors",
            "checks",
        },
        "Point result",
    )
    version = raw["schema_version"]
    if type(version) is not str or version not in (("1.0.0",) if multi else ("1.0.0", "2.0.0")):
        raise ValueError("Unsupported point declaration: schema_version")
    declarations = {**_DECLARATIONS, "schema_version": version}
    if multi:
        declarations.update(schema_id="hysys-mv-point", schema_version="1.0.0")
    for name, expected in declarations.items():
        if type(raw[name]) is not type(expected) or raw[name] != expected:
            raise ValueError(f"Unsupported point declaration: {name}")
    target: float | tuple[MVChange, ...]
    if multi:
        baseline = OperatingSnapshot.from_dict(
            _json(_file_bytes(path.parent / "baseline_snapshot.json"))
        )
        target = parse_changes(
            raw["changes"], baseline, load_catalog(path.parent / "variables.json")
        )
        if raw["changes"] != [c.to_dict() for c in target]:
            raise ValueError("Noncanonical MV targets")
    else:
        target = _number(raw["target_c"])
    digest = _digest(raw["baseline_sha256"])
    runtime = _runtime(raw["runtime_observations"], multi=multi)
    errors = _errors(raw["errors"])
    checks, snapshots, boundary, passed = _evaluate(
        path.parent,
        raw["files"],
        target,
        digest,
        runtime,
        errors,
        version="3.0.0" if multi else version,
    )
    status: Literal["passed", "failed"] = "passed" if passed else "failed"
    if type(raw["status"]) is not str or raw["status"] != status:
        raise ValueError("Point status differs from independently recomputed evidence")
    if _canonical(raw["checks"]) != _canonical(checks):
        raise ValueError("Point checks differ from independently recomputed evidence")
    return PointResult(
        status,
        None if isinstance(target, tuple) else target,
        digest,
        snapshots["baseline_snapshot.json"],
        snapshots.get("A_before.json"),
        snapshots.get("B_changed.json"),
        snapshots.get("A_restored.json"),
        errors,
        boundary,
        changes=target if isinstance(target, tuple) else (),
    )


def write_point_result(
    directory: Path,
    target_c: float | tuple[MVChange, ...],
    baseline_sha256: str,
    raw_report: dict[str, Any],
) -> Path:
    """Write a new report, replacing untrusted summaries with recomputed checks."""
    directory = directory.expanduser().absolute()
    _plain_directory(directory)
    multi = isinstance(target_c, tuple)
    target = target_c if multi else _number(target_c)
    digest = _digest(baseline_sha256)
    if type(raw_report) is not dict or set(raw_report) - _RAW_FIELDS:
        raise ValueError("Unknown runtime report fields")
    for name, expected in {
        "baseline_sha256": digest,
        "eligible_for_optimization": False,
        "isolation": _DECLARATIONS["isolation"],
    }.items():
        if name in raw_report and (
            type(raw_report[name]) is not type(expected) or raw_report[name] != expected
        ):
            raise ValueError(f"Runtime report identity differs: {name}")
    runtime_fields = (
        _RUNTIME_FIELDS
        - {
            "specification_before",
            "specification_after",
            "target_temperature_C",
            "actual_temperature_C",
        }
        if multi
        else _RUNTIME_FIELDS
    )
    runtime = _runtime({name: raw_report.get(name) for name in runtime_fields}, multi=multi)
    errors = []
    for field, stage in _ERROR_FIELDS.items():
        error = raw_report.get(field)
        if error is not None:
            _record(error, {"phase", "message", "hresult", "scode"}, field)
            errors.append({"stage": stage, **error})
    cleanup = raw_report.get("cleanup_errors", [])
    if type(cleanup) is not list:
        raise ValueError("Cleanup errors must be an array")
    for error in cleanup:
        _record(error, {"phase", "message", "hresult", "scode"}, "Cleanup error")
        errors.append({"stage": "cleanup", **error})
    files = {
        name: _sha256(_file_bytes(directory / name))
        for name in sorted(_FILES)
        if os.path.lexists(directory / name)
    }
    checks, _, _, passed = _evaluate(
        directory,
        files,
        target,
        digest,
        runtime,
        _errors(errors),
        version="3.0.0" if multi else "2.0.0",
    )
    document = {
        **_DECLARATIONS,
        "status": "passed" if passed else "failed",
        **(
            {"changes": [c.to_dict() for c in target]}
            if isinstance(target, tuple)
            else {"target_c": target}
        ),
        "baseline_sha256": digest,
        "files": files,
        "runtime_observations": runtime,
        "errors": errors,
        "checks": checks,
    }
    if multi:
        document.update(schema_id="hysys-mv-point", schema_version="1.0.0")
    path = directory / "report.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    read_point_result(path)
    return path
