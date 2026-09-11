"""Evaluate one trusted T-39 target in an owned case and verify baseline recovery."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .baseline import Baseline, _file_bytes, _json, _plain_directory, read_baseline
from .boundary import (
    DEFAULT_BOUNDARY,
    BoundaryDefinition,
    _read_open_boundary,
    write_boundary_snapshot,
)
from .comparison import compare_operating_points
from .hysys import VariableCatalog, _read_open_case, _solver, load_catalog
from .models import OperatingSnapshot, write_snapshot
from .mv import WRITER as MV_WRITER
from .mv import MVChange, check_changes, parse_changes, write_changes
from .point_evidence import (
    SOLVER_ACTION,
    SPECIFICATION,
    TRACKING_TOLERANCE_C,
    VARIABLE_ID,
    WRITER,
    _bindings_match,
    _expected_changed,
    _qualified,
    write_point_result,
)
from .recovery import _documents, _path, _same_documents


def _hash(path: Path) -> str:
    return hashlib.sha256(_file_bytes(path)).hexdigest()


def _error(exc: BaseException, phase: str) -> dict[str, Any]:
    details = getattr(exc, "excepinfo", None)
    hresult = getattr(exc, "hresult", None)
    scode = (
        details[5]
        if isinstance(details, tuple) and len(details) == 6
        else getattr(exc, "scode", None)
    )
    return {
        "phase": phase,
        "message": str(exc),
        "hresult": hresult if type(hresult) is int else None,
        "scode": scode if type(scode) is int else None,
    }


def _event(directory: Path, phase: str) -> None:
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"phase": phase, "monotonic": time.monotonic()}) + "\n")


def _change(case: Any, catalog: VariableCatalog, start: OperatingSnapshot, target: float) -> float:
    """Write only the qualified active T-39 goal, including an unchanged requested target."""
    binding = next(item for item in catalog.variables if item.variable_id == VARIABLE_ID)
    initial = next(item for item in start.variables if item.variable_id == VARIABLE_ID)
    if not _qualified(catalog, start):
        raise ValueError("The T-39 temperature binding is not qualified in this baseline")
    cfs = case.Flowsheet.Operations.Item(catalog.column_name).ColumnFlowsheet
    spec = cfs.Specifications.Item(SPECIFICATION)
    cell = case.Flowsheet.Operations.Item(catalog.table_name).Cell(f"C{binding.row}")
    variable = cell.ImportedVariable
    if (
        cell.AttachedObjectName != binding.object_name
        or cell.VariableName != binding.property_name
        or variable.UnitConversionType != 1
        or variable.State != 1
        or not variable.CanModify
        or not variable.IsKnown
        or not spec.IsActive
        or spec.Goal.UnitConversionType != 1
        or variable.GetValue("C") != initial.value
        or spec.Goal.GetValue("C") != initial.value
        or spec.GoalValue != initial.internal_value
        or initial.internal_value != initial.value
    ):
        raise ValueError("The live T-39 binding or goal changed before writing")
    case.Solver.CanSolve = False
    # This qualified temperature goal uses C internally. Both owner and imported
    # setters read back correctly, but warm Run did not follow the live +0.1 C target.
    spec.GoalValue = target
    if (
        variable.GetValue("C") != target
        or spec.Goal.GetValue("C") != target
        or spec.GoalValue != target
    ):
        raise ValueError("Specification owner write did not read back as the requested T-39 target")
    # Reset only this owned working column, discarding its old solution/estimates.
    # No native solver tolerance is changed, even when the requested goal is unchanged.
    cfs.Reset()
    case.Solver.CanSolve = True
    cfs.Run()
    return target


def _wait_for_column(case: Any, catalog: VariableCatalog, pythoncom: Any) -> None:
    cfs = case.Flowsheet.Operations.Item(catalog.column_name).ColumnFlowsheet
    deadline = time.monotonic() + 120
    stable_since = None
    while True:
        pythoncom.PumpWaitingMessages()
        state = _solver(case, cfs)
        now = time.monotonic()
        ready = (
            state.can_solve and not state.is_solving and state.is_valid and state.column_converged
        )
        stable_since = (now if stable_since is None else stable_since) if ready else None
        if stable_since is not None and now - stable_since >= 1:
            return
        if now >= deadline:
            raise TimeoutError(f"Column did not become stable within polling deadline: {state}")
        time.sleep(min(0.25, deadline - now))


def _specification_values(case: Any, catalog: VariableCatalog) -> dict[str, Any]:
    cfs = case.Flowsheet.Operations.Item(catalog.column_name).ColumnFlowsheet
    spec = cfs.Specifications.Item(SPECIFICATION)
    matches = [
        cfs.ActiveSpecifications.Item(index)
        for index in range(cfs.ActiveSpecifications.Count)
        if cfs.ActiveSpecifications.Item(index).Name == SPECIFICATION
    ]
    if len(matches) != 1:
        raise ValueError("Expected exactly one current active T-39 specification")
    active = matches[0]
    return {
        "goal_value_internal": spec.GoalValue,
        "goal_C": spec.Goal.GetValue("C"),
        "current_C": spec.Current.GetValue("C"),
        "active_goal_value_internal": active.GoalValue,
        "active_goal_C": active.Goal.GetValue("C"),
        "active_current_C": active.Current.GetValue("C"),
    }


def _response(
    start: OperatingSnapshot, changed: OperatingSnapshot, target: float
) -> dict[str, float]:
    previous = next(item.current for item in start.specifications if item.name == SPECIFICATION)
    current = next(item.current for item in changed.specifications if item.name == SPECIFICATION)
    assert (
        previous is not None and current is not None
    )  # Active specifications require known values.
    # This interface checks target tracking, not the sign or size of an output change.
    if abs(current - target) > TRACKING_TOLERANCE_C:
        raise ValueError(
            "T-39 current temperature did not follow the requested target within 0.01 C"
        )
    return {
        "before_current_C": previous,
        "after_current_C": current,
        "observed_change_C": current - previous,
        "target_residual_C": current - target,
        "diagnostic_tracking_tolerance_C": TRACKING_TOLERANCE_C,
    }


def _run_session(
    client: Any,
    pythoncom: Any,
    baseline: Baseline,
    directory: Path,
    target: float | tuple[MVChange, ...],
) -> dict[str, Any]:
    app = client.GetActiveObject("HYSYS.Application")
    original = _documents(app)
    source = Path(baseline.snapshot.source_case_path)
    if len(original) != 1 or _path(source) not in original:
        raise ValueError("A point evaluation requires only its source case to be open")
    source_case = original[_path(source)]
    bound = SimpleNamespace(GetActiveObject=lambda _: app)
    catalog = load_catalog(baseline.catalog_path)
    definition = BoundaryDefinition.from_dict(
        _json(_file_bytes(directory / "boundary_definition.json"))
    )
    before = _read_open_case(bound, source, catalog, _hash(source))
    if not before.consistent_observation or not before.solver.can_solve:
        raise ValueError("Source must be idle, valid and converged with its solver enabled")
    write_snapshot(directory / "source_before.json", before)
    report: dict[str, Any] = {"change_error": None, "restore_error": None, "cleanup_errors": []}
    attempted_opens: set[str] = set()
    opened_cases: dict[str, Any] = {}

    def observe(path: Path) -> OperatingSnapshot:
        return _read_open_case(bound, path, catalog, baseline.baseline_sha256)

    def verify_source(expected_count: int) -> None:
        documents = _documents(app)
        current_source = documents.get(_path(source))
        if len(documents) != expected_count:
            raise ValueError("Unexpected document set; refusing to continue the point evaluation")
        if current_source is None or current_source._oleobj_ != source_case._oleobj_:
            raise ValueError("The original source case identity or path changed")
        current = _read_open_case(bound, source, catalog, _hash(source))
        if (
            replace(current, observed_at_utc=before.observed_at_utc) != before
            or _hash(source) != before.source_disk_sha256
        ):
            raise ValueError("The source observation or disk file changed")

    def open_work(name: str) -> Any:
        path = directory / name
        verify_source(1)
        if _hash(path) != baseline.baseline_sha256:
            raise ValueError("Working file changed before Open")
        attempted_opens.add(name)
        case = app.SimulationCases.Open(str(path))
        opened_cases[name] = case
        if (
            case._oleobj_ == source_case._oleobj_
            or _path(case.FullName) != _path(path)
            or not _same_documents({**original, _path(path): case}, _documents(app))
        ):
            raise ValueError("Opened case is not the uniquely owned working document")
        verify_source(2)
        return case

    def verify_work(name: str) -> None:
        case = opened_cases[name]
        path = directory / name
        if not _same_documents({**original, _path(path): case}, _documents(app)):
            raise ValueError("The owned working case identity or document set changed")

    def close_work(name: str) -> None:
        if name not in attempted_opens:
            return
        path = directory / name
        documents = _documents(app)
        case = documents.get(_path(path))
        opened = opened_cases.get(name)
        if case is not None:
            if case._oleobj_ == source_case._oleobj_:
                raise ValueError("Refusing to close the source case")
            if opened is not None and case._oleobj_ != opened._oleobj_:
                raise ValueError(
                    "The working path belongs to a replacement case; refusing to close"
                )
            if opened is None and not _same_documents({**original, _path(path): case}, documents):
                raise ValueError("The failed Open did not leave a uniquely owned working case")
            case.Close(False)
        if not _same_documents(original, _documents(app)):
            raise ValueError("Working case did not close or the document set changed")

    phase = "open-baseline-before-change"
    candidate_closed = False
    try:
        _event(directory, phase)
        case = open_work("candidate.hsc")
        start = observe(directory / "candidate.hsc")
        write_snapshot(directory / "A_before.json", start)
        report["initial_comparison"] = compare_operating_points(baseline.snapshot, start)
        if not report["initial_comparison"]["equivalent"]:
            raise ValueError("Reopened A does not exactly match the frozen baseline")
        phase = (
            "write-MV-targets-and-run-column"
            if isinstance(target, tuple)
            else "write-T-39-target-and-run-column"
        )
        _event(directory, phase)
        verify_work("candidate.hsc")
        verify_source(2)
        if isinstance(target, tuple):
            write_changes(case, catalog, start, target)
        else:
            report["specification_before"] = _specification_values(case, catalog)
            _change(case, catalog, start, target)
        phase = "wait-and-read-B"
        _event(directory, phase)
        _wait_for_column(case, catalog, pythoncom)
        changed = observe(directory / "candidate.hsc")
        verify_work("candidate.hsc")
        write_snapshot(directory / "B_changed.json", changed)
        if isinstance(target, tuple):
            comparison, responses = check_changes(start, changed, target, catalog)
            report["change_comparison"] = comparison
            if not all(r["setpoint_matches"] and r["tracking_passed"] for r in responses):
                raise ValueError("Selected MV targets did not pass readback/tracking checks")
        else:
            report["specification_after"] = _specification_values(case, catalog)
            comparison = compare_operating_points(_expected_changed(start, target), changed)
            report["change_comparison"] = comparison
            report["target_temperature_C"] = target
            report["actual_temperature_C"] = next(
                item.current for item in changed.specifications if item.name == SPECIFICATION
            )
            report["temperature_response"] = _response(start, changed, target)
        if comparison["input_differences"] or comparison["state_differences"]:
            raise ValueError(
                "Changed point has unintended input/specification changes or invalid state"
            )
        phase = "read-computed-boundary"
        _event(directory, phase)
        verify_work("candidate.hsc")
        boundary = _read_open_boundary(
            bound, directory / "candidate.hsc", definition, catalog, baseline.baseline_sha256
        )
        verify_work("candidate.hsc")
        write_boundary_snapshot(directory / "B_boundary.json", boundary)
        if (
            not boundary.consistent_observation
            or replace(boundary.core_before, observed_at_utc=changed.observed_at_utc) != changed
        ):
            raise ValueError("Computed point changed during its boundary observation")
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - preserve error and attempt recovery
        report["change_error"] = _error(exc, phase)
        traceback.clear_frames(exc.__traceback__)
    finally:
        try:
            close_work("candidate.hsc")
            candidate_closed = True
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - preserve cleanup failure
            report["cleanup_errors"].append(_error(exc, "close-candidate"))

    phase = "reopen-frozen-A"
    try:
        if not candidate_closed:
            raise ValueError("Restoration skipped because candidate closure was not verified")
        _event(directory, phase)
        case = open_work("restored.hsc")
        restored = observe(directory / "restored.hsc")
        write_snapshot(directory / "A_restored.json", restored)
        report["restoration_comparison"] = compare_operating_points(baseline.snapshot, restored)
        if not report["restoration_comparison"]["equivalent"]:
            raise ValueError("Restored A does not exactly match the frozen baseline")
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - restoration is independent evidence
        report["restore_error"] = _error(exc, phase)
    finally:
        try:
            close_work("restored.hsc")
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - retain cleanup failure
            report["cleanup_errors"].append(_error(exc, "close-restored"))
    report["source_unchanged"] = False
    try:
        verify_source(1)
        after = _read_open_case(bound, source, catalog, _hash(source))
        write_snapshot(directory / "source_after.json", after)
        report["source_disk_unchanged"] = _hash(source) == before.source_disk_sha256
        report["source_unchanged"] = (
            replace(after, observed_at_utc=before.observed_at_utc) == before
            and report["source_disk_unchanged"]
        )
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - keep change and restoration evidence
        report["source_protection_error"] = _error(exc, "check-source-after")
    report["status"] = (
        "passed"
        if report["source_unchanged"]
        and report["change_error"] is None
        and report["restore_error"] is None
        and not report["cleanup_errors"]
        else "failed"
    )
    return report


def _capture(*args: Any) -> dict[str, Any]:
    try:
        return _run_session(*args)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - release COM stack before uninitialize
        return {"status": "failed", "error": _error(exc, "probe-boundary")}


def run_t39_point(baseline_manifest: Path, target_c: float, output_dir: Path) -> Path:
    """Evaluate a finite Celsius target from a trusted caller; no process range is implied.

    Only T-39 is writable. The same Reset/Run protocol applies to unchanged targets.
    Calls are serial in the existing application; a blocking COM call has no hard timeout.
    """
    if type(target_c) not in (int, float):
        raise TypeError("target_c must be numeric, not a Boolean")
    try:
        target = float(target_c)
    except OverflowError as exc:
        raise ValueError("target_c must be finite") from exc
    if not math.isfinite(target):
        raise ValueError("target_c must be finite")
    return _run_point(baseline_manifest, target, output_dir)


def run_mv_point(baseline_manifest: Path, changes: Any, output_dir: Path) -> Path:
    """Evaluate one explicitly selected MV batch using the shared owned-case lifecycle."""
    baseline = read_baseline(baseline_manifest)
    targets = parse_changes(changes, baseline.snapshot, load_catalog(baseline.catalog_path))
    return _run_point(baseline_manifest, targets, output_dir)


def _run_point(
    baseline_manifest: Path, target: float | tuple[MVChange, ...], output_dir: Path
) -> Path:
    manifest = baseline_manifest.expanduser().absolute()
    baseline = read_baseline(manifest)
    frozen_manifest_hash = _hash(manifest)
    payload = _file_bytes(baseline.directory / "baseline.hsc")
    if hashlib.sha256(payload).hexdigest() != baseline.baseline_sha256:
        raise ValueError("Frozen baseline changed before copying")
    catalog_bytes = _file_bytes(baseline.catalog_path)
    catalog = load_catalog(baseline.catalog_path)
    if not _bindings_match(catalog, baseline.snapshot):
        raise ValueError("Baseline and catalog bindings differ")
    if isinstance(target, tuple):
        parse_changes([c.to_dict() for c in target], baseline.snapshot, catalog)
    elif not _qualified(catalog, baseline.snapshot):
        raise ValueError("Baseline does not qualify the T-39 point input")
    if read_baseline(manifest) != baseline or _hash(manifest) != frozen_manifest_hash:
        raise ValueError("Frozen baseline changed during input validation")
    if _file_bytes(baseline.catalog_path) != catalog_bytes:
        raise ValueError("Frozen catalog changed during input validation")
    boundary_bytes = _file_bytes(DEFAULT_BOUNDARY)
    definition = BoundaryDefinition.from_dict(_json(boundary_bytes))
    if (
        definition.case_id != baseline.snapshot.case_id
        or definition.column_name != catalog.column_name
    ):
        raise ValueError("Boundary definition identifies a different baseline")
    output_dir = output_dir.expanduser().absolute()
    _plain_directory(output_dir.parent)
    output_dir.mkdir(exist_ok=False)
    write_snapshot(output_dir / "baseline_snapshot.json", baseline.snapshot)
    with (output_dir / "variables.json").open("xb") as stream:
        stream.write(catalog_bytes)
    with (output_dir / "boundary_definition.json").open("xb") as stream:
        stream.write(boundary_bytes)
    for name in ("candidate.hsc", "restored.hsc"):
        with (output_dir / name).open("xb") as stream:
            stream.write(payload)
        if _hash(output_dir / name) != baseline.baseline_sha256:
            raise ValueError("Working bytes differ from the frozen baseline")
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    pythoncom.CoInitialize()
    try:
        report = _capture(
            client,
            pythoncom,
            replace(baseline, catalog_path=output_dir / "variables.json"),
            output_dir,
            target,
        )
    finally:
        pythoncom.CoUninitialize()
    try:
        current = read_baseline(manifest)
        if current != baseline or _hash(manifest) != frozen_manifest_hash:
            raise ValueError("Frozen baseline changed")
        if any(
            _hash(output_dir / name) != baseline.baseline_sha256
            for name in ("candidate.hsc", "restored.hsc")
        ):
            raise ValueError("Working case files changed despite no-save execution")
        if _file_bytes(output_dir / "variables.json") != catalog_bytes:
            raise ValueError("Copied variable catalog changed during point evaluation")
        if _file_bytes(output_dir / "boundary_definition.json") != boundary_bytes:
            raise ValueError("Copied boundary definition changed during point evaluation")
        report["files_unchanged"] = True
    except Exception as exc:  # noqa: BLE001 - final evidence integrity boundary
        report.update(status="failed", integrity_error=_error(exc, "verify-file-integrity"))
    report.update(
        scope="selected_MV_targets" if isinstance(target, tuple) else "one_T-39_target_C_only",
        isolation="separate_cases_same_application",
        baseline_manifest_sha256=frozen_manifest_hash,
        baseline_sha256=baseline.baseline_sha256,
        implementation_sha256=_hash(Path(__file__).resolve()),
        writer=MV_WRITER if isinstance(target, tuple) else WRITER,
        solver_action=SOLVER_ACTION,
        eligible_for_optimization=False,
    )
    return write_point_result(output_dir, target, baseline.baseline_sha256, report)
