"""Observe a specified HYSYS case without writing its variables or solver controls.

This is a Windows integration probe, not the production simulation contract. The
default opens a unique disk copy. --attach-existing reads only an exact FullName
match in the running application. Neither mode activates, saves, closes, or quits
a case/application. Opening a copy can itself make HYSYS calculate; this probe
does not request a solve. COM calls may block; no Python timeout is claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MV_ROWS = range(2, 26)
CV_ROWS = range(28, 64)
EXPECTED_STAGES = 73


class ProbeValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")


def error_record(error: BaseException, phase: str) -> dict[str, Any]:
    hresult = getattr(error, "hresult", None)
    if not isinstance(hresult, int) or isinstance(hresult, bool):
        hresult = None
    code = getattr(error, "code", None)
    excepinfo = getattr(error, "excepinfo", None)
    com_exception = None
    if isinstance(excepinfo, (tuple, list)) and len(excepinfo) == 6:
        # pywin32 EXCEPINFO: wCode, source, description, helpFile, helpContext, scode.
        source, description, scode = excepinfo[1], excepinfo[2], excepinfo[5]
        com_exception = {
            "scode": scode if isinstance(scode, int) and not isinstance(scode, bool) else None,
            "source": source if isinstance(source, str) else None,
            "description": description if isinstance(description, str) else None,
        }
    return {
        "phase": phase,
        "code": code or ("com_error" if hresult is not None else "runtime_error"),
        "exception_type": type(error).__name__,
        "hresult": hresult,
        "excepinfo": com_exception,
        "message": str(error),
    }


def finite_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeValidationError("invalid_number", f"{label}: expected a numeric value")
    if not math.isfinite(value) or value <= -1e20:
        raise ProbeValidationError("undefined_number", f"{label}: non-finite or undefined value")
    return value


def required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeValidationError("missing_text", f"{label}: expected nonempty text")
    return value


def boolean_value(value: Any, label: str) -> bool:
    # VARIANT_BOOL may be exposed as bool or its 0/-1 integer representation.
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (-1, 0, 1):
        return bool(value)
    raise ProbeValidationError("invalid_status", f"{label}: expected a Boolean")


def read_solver_status(case: Any, column: Any) -> dict[str, bool]:
    solver = case.Solver
    return {
        "can_solve": boolean_value(solver.CanSolve, "Solver.CanSolve"),
        "is_solving": boolean_value(solver.IsSolving, "Solver.IsSolving"),
        "is_valid": boolean_value(case.IsValid, "case.IsValid"),
        "column_converged": boolean_value(
            column.ColumnFlowsheet.CfsConverged, "column.CfsConverged"
        ),
    }


def read_table(table: Any) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"mv": [], "cv": []}
    keys: set[tuple[str, str]] = set()
    for section, rows in (("mv", MV_ROWS), ("cv", CV_ROWS)):
        for row in rows:
            obj = required_text(table.Cell(f"A{row}").CellText, f"A{row}")
            prop = required_text(table.Cell(f"B{row}").CellText, f"B{row}")
            unit = required_text(table.Cell(f"D{row}").CellText, f"D{row}")
            key = (obj.strip(), prop.strip())
            if key in keys:
                raise ProbeValidationError("duplicate_mapping", f"Table row {row}: duplicate {key}")
            keys.add(key)
            result[section].append(
                {
                    "row": row,
                    "object_name": obj,
                    "property_name": prop,
                    "value_cell": f"C{row}",
                    "raw_cell_value": finite_number(table.Cell(f"C{row}").CellValue, f"C{row}"),
                    "unit_label": unit,
                }
            )
    return result


def read_stages(column: Any) -> list[dict[str, Any]]:
    stages = column.ColumnFlowsheet.ColumnStages
    count = stages.Count
    if isinstance(count, bool) or not isinstance(count, int) or count != EXPECTED_STAGES:
        raise ProbeValidationError("stage_count", f"Expected 73 stages; observed {count!r}")
    result = []
    names: set[str] = set()
    for index in range(count):
        stage = stages.Item(index)
        name = required_text(stage.Name, f"stage[{index}].Name")
        if name.strip() in names:
            raise ProbeValidationError("duplicate_stage", f"Duplicate stage name: {name}")
        names.add(name.strip())
        sep = stage.SeparationStage
        result.append(
            {
                "name": name,
                "pressure_kPa": finite_number(sep.Pressure.GetValue("kPa"), name),
                "temperature_C": finite_number(sep.Temperature.GetValue("C"), name),
                "liquid_kg_h": finite_number(sep.MassLiquidFlow.GetValue("kg/h"), name),
                "vapor_kg_h": finite_number(sep.MassVapourFlow.GetValue("kg/h"), name),
            }
        )
    return result


def read_sample(case: Any, table: Any, column: Any) -> dict[str, Any]:
    before = read_solver_status(case, column)
    values = read_table(table)
    stages = read_stages(column)
    after = read_solver_status(case, column)
    return {"solver_before": before, **values, "stage": stages, "solver_after": after}


def assess_samples(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    statuses = [
        sample[key] for sample in (first, second) for key in ("solver_before", "solver_after")
    ]
    if any(status != statuses[0] for status in statuses[1:]):
        reasons.append("solver_status_changed")
    if any(status["is_solving"] for status in statuses):
        reasons.append("solver_was_solving")
    if any(not status["is_valid"] for status in statuses):
        reasons.append("case_not_valid")
    if any(not status["column_converged"] for status in statuses):
        reasons.append("column_not_converged")
    for section in ("mv", "cv", "stage"):
        if first[section] != second[section]:
            reasons.append(f"{section}_changed")
    return {
        "consistent_observation": not reasons,
        "reasons": reasons,
        "method": "two_complete_samples_exact_equality_and_four_solver_status_reads",
        "atomic_snapshot_proven": False,
        "eligible_for_prepare": False,
        "prepare_blockers": [
            "probe_only",
            "units_not_qualified",
            "rebuildable_baseline_not_frozen",
        ],
    }


def _load_com() -> tuple[Any, Any]:
    # Delayed so --help and offline tests work without Windows/pywin32.
    return importlib.import_module("pythoncom"), importlib.import_module("win32com.client")


def normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def read_application_metadata(app: Any) -> dict[str, Any]:
    """Read Version only if the server's type information advertises it."""
    try:
        info = app._oleobj_.GetTypeInfo()
        members = {
            info.GetDocumentation(info.GetFuncDesc(index).memid)[0]
            for index in range(info.GetTypeAttr().cFuncs)
        }
        result: dict[str, Any] = {"type_name": info.GetDocumentation(-1)[0]}
        if "Version" in members:
            version = app.Version
            if not isinstance(version, (str, int, float)) or isinstance(version, bool):
                raise ProbeValidationError("invalid_version", "Application.Version is not scalar")
            if isinstance(version, (int, float)):
                finite_number(version, "Application.Version")
            result["version"] = version
        else:
            result["version"] = None
            result["version_status"] = "not_advertised_by_type_information"
        return result
    except Exception as error:  # noqa: BLE001 - optional external COM metadata boundary
        return {"version": None, "error": error_record(error, "application_metadata")}


def _read_in_com_session(
    client: Any, target: Path, attach_existing: bool, report: dict[str, Any]
) -> dict[str, Any] | None:
    app = cases = case = table = column = None
    try:
        report["phase"] = "connect_application"
        if attach_existing:
            app = client.GetActiveObject("HYSYS.Application")
        else:
            app = client.DispatchEx("HYSYS.Application")
        report["application"] = read_application_metadata(app)
        report["phase"] = "select_case"
        cases = app.SimulationCases
        if attach_existing:
            matches = []
            for index in range(cases.Count):
                candidate = cases.Item(index)
                if normalized_path(candidate.FullName) == normalized_path(target):
                    matches.append(candidate)
            if len(matches) != 1:
                raise ProbeValidationError(
                    "case_match_count",
                    f"Expected one open case matching {target}; found {len(matches)}",
                )
            case = matches[0]
        else:
            report["phase"] = "open_copy"
            case = cases.Open(str(target))
            report["ownership"]["probe_opened_case"] = True
        report["phase"] = "verify_case_identity"
        actual_name = required_text(case.FullName, "case.FullName")
        report["opened_case_full_name"] = actual_name
        if normalized_path(actual_name) != normalized_path(target):
            raise ProbeValidationError("wrong_case", "HYSYS returned a different case FullName")
        report["phase"] = "bind_objects"
        table = case.Flowsheet.Operations.Item("Table")
        column = case.Flowsheet.Operations.Item("C-1102")
        report["phase"] = "read_first_sample"
        first = read_sample(case, table, column)
        report["phase"] = "read_second_sample"
        second = read_sample(case, table, column)
        report["phase"] = "verify_case_identity_after_read"
        if normalized_path(case.FullName) != normalized_path(target):
            raise ProbeValidationError(
                "case_identity_changed", "case.FullName changed during reading"
            )
        return {
            "schema": "hysys-read-probe-snapshot/1",
            "observed_at_utc": utc_now(),
            "source": dict(report["source"]),
            "opened_case_full_name": actual_name,
            "table_value_semantics": "raw_COM_CellValue_with_unverified_spreadsheet_unit_labels",
            "stage_value_semantics": "COM_GetValue_in_explicit_kPa_C_kg_per_h_units",
            "counts": {
                "mv": len(first["mv"]),
                "cv": len(first["cv"]),
                "stage": len(first["stage"]),
            },
            "assessment": assess_samples(first, second),
            "first_sample": first,
            "second_sample": second,
            "claim_scope": "synthetic_engineering_simulation_observation_only",
        }
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - serialize COM boundary failure
        report["errors"].append(error_record(error, report["phase"]))
        return None
    finally:
        # No Close/Quit: DispatchEx does not prove a separate HYSYS process.
        # Function return also releases candidate/matches and exception frames
        # before the caller uninitializes COM on this same thread.
        app = cases = case = table = column = None


def run_probe(
    case_path: Path,
    output_root: Path = ROOT / "runs" / "simulation",
    *,
    attach_existing: bool = False,
) -> tuple[Path, dict[str, Any]]:
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="hysys-read-", dir=output_root))
    source = case_path.expanduser().resolve()
    report: dict[str, Any] = {
        "schema": "hysys-read-probe-report/1",
        "started_at_utc": utc_now(),
        "run_directory": str(run_dir),
        "phase": "validate_source",
        "status": "failed",
        "source": {
            "kind": "existing_case_memory" if attach_existing else "disk_case_copy",
            "requested_case": str(source),
            "memory_state_bound_to_disk_hash": False,
        },
        "ownership": {
            "application_connection": "GetActiveObject" if attach_existing else "DispatchEx",
            "application_exclusive_ownership_proven": False,
            "probe_opened_case": False,
            "close_or_quit_requested": False,
            "case_retained_in_application_if_opened": True,
        },
        "com_lifecycle": {"initialized": False, "uninitialized": False},
        "errors": [],
        "snapshot": None,
    }
    snapshot = None
    original_hash = None
    try:
        if source.suffix.lower() != ".hsc" or not source.is_file():
            raise ProbeValidationError(
                "invalid_case_file", f"Expected an existing .hsc file: {source}"
            )
        original_hash = file_hash(source)
        report["source"]["disk_sha256_before"] = original_hash
        target = source
        if not attach_existing:
            report["phase"] = "copy_source"
            target = run_dir / source.name
            shutil.copy2(source, target)
            if file_hash(target) != original_hash:
                raise ProbeValidationError("copy_hash_mismatch", "Copied case differs from source")
            report["source"]["working_copy"] = str(target)
        report["phase"] = "load_com"
        pythoncom, client = _load_com()
        report["phase"] = "initialize_com"
        pythoncom.CoInitialize()
        report["com_lifecycle"]["initialized"] = True
        try:
            snapshot = _read_in_com_session(client, target, attach_existing, report)
        finally:
            report["phase"] = "uninitialize_com"
            pythoncom.CoUninitialize()
            report["com_lifecycle"]["uninitialized"] = True
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - persist failed probe diagnostics
        report["errors"].append(error_record(error, report["phase"]))
    finally:
        if original_hash is not None:
            try:
                after_hash = file_hash(source)
                report["source"]["disk_sha256_after"] = after_hash
                report["source"]["disk_unchanged"] = after_hash == original_hash
                if after_hash != original_hash:
                    raise ProbeValidationError(
                        "source_changed", "Source case file changed during probe"
                    )
            except (OSError, ProbeValidationError) as error:
                report["source"]["disk_unchanged"] = False
                report["errors"].append(error_record(error, "verify_source_after_read"))
    if snapshot is not None:
        snapshot["source"] = dict(report["source"])
        if report["errors"]:
            snapshot["assessment"]["consistent_observation"] = False
            snapshot["assessment"]["reasons"].append("probe_error")
        report["phase"] = "write_snapshot"
        try:
            snapshot_path = run_dir / "snapshot.json"
            write_new_json(snapshot_path, snapshot)
            report["snapshot"] = {"path": str(snapshot_path), "sha256": file_hash(snapshot_path)}
            report["assessment"] = snapshot["assessment"]
            report["counts"] = snapshot["counts"]
            if not report["errors"]:
                report["status"] = (
                    "observed_stable"
                    if snapshot["assessment"]["consistent_observation"]
                    else "observed_unstable"
                )
        except (OSError, TypeError, ValueError) as error:
            report["errors"].append(error_record(error, report["phase"]))
    report["phase"] = "finished"
    report["finished_at_utc"] = utc_now()
    write_new_json(run_dir / "report.json", report)
    return run_dir, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True, help="Explicit source .hsc path")
    parser.add_argument(
        "--attach-existing",
        action="store_true",
        help="Read an exact FullName match in the running application; never open a missing case",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "runs" / "simulation",
        help="Parent of a newly created unique run directory (existing outputs are never overwritten)",
    )
    args = parser.parse_args(argv)
    run_dir, report = run_probe(args.case, args.output_root, attach_existing=args.attach_existing)
    print(
        json.dumps(
            {"status": report["status"], "report": str(run_dir / "report.json")}, ensure_ascii=False
        )
    )
    return {"observed_stable": 0, "observed_unstable": 2, "failed": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
