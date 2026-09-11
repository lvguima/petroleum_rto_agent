"""Verify a frozen case through an owned copy in the existing HYSYS application."""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .baseline import Baseline, _file_bytes, _plain_directory, _sha256, _stable, read_baseline
from .comparison import compare_operating_points
from .hysys import HysysReadError, VariableCatalog, _read_open_case, load_catalog
from .models import OperatingSnapshot, read_snapshot, write_snapshot


def _path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(value))


def _error(report: dict[str, Any], error: BaseException, phase: str) -> None:
    details = getattr(error, "excepinfo", None)
    scode = getattr(error, "scode", None)
    if isinstance(details, tuple) and len(details) == 6:
        scode = details[5]
    hresult = getattr(error, "hresult", None)
    report["errors"].append(
        {
            "phase": phase,
            "code": getattr(
                error,
                "code",
                "interrupted" if isinstance(error, KeyboardInterrupt) else "recovery-failed",
            ),
            "exception_type": type(error).__name__,
            "message": str(error),
            "hresult": hresult if type(hresult) is int else None,
            "scode": scode if type(scode) is int else None,
        }
    )


def _documents(app: Any) -> dict[str, Any]:
    cases = app.SimulationCases
    count = cases.Count
    if type(count) is not int or count < 0:
        raise HysysReadError("case-inventory", "Invalid open case count")
    documents = {}
    for index in range(count):
        case = cases.Item(index)
        name = case.FullName
        if not isinstance(name, str) or not name.strip() or _path(name) in documents:
            raise HysysReadError("case-inventory", "Open cases require unique complete names")
        documents[_path(name)] = case
    return documents


def _same_documents(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return expected.keys() == actual.keys() and all(
        case._oleobj_ == actual[name]._oleobj_ for name, case in expected.items()
    )


def _close_copy(app: Any, original: dict[str, Any], work: Path, report: dict[str, Any]) -> None:
    documents = _documents(app)
    case = documents.get(_path(work))
    expected = original if case is None else {**original, _path(work): case}
    if not _same_documents(expected, documents):
        _error(
            report,
            HysysReadError("case-inventory-changed", "Other cases changed before cleanup"),
            "inventory-before-close",
        )
    if case is not None:
        if any(case._oleobj_ == other._oleobj_ for other in original.values()):
            raise HysysReadError("work-ownership", "Working path belongs to a pre-existing case")
        # The path was absent before Open, is unique now, and belongs to a new object.
        report["work_case"]["close_requested"] = True
        case.Close(False)
    remaining = _documents(app)
    report["work_case"]["closed"] = _path(work) not in remaining and (
        case is None or all(case._oleobj_ != other._oleobj_ for other in remaining.values())
    )
    if not report["work_case"]["closed"]:
        raise HysysReadError("work-not-closed", "The working case remains open")


def _session(
    client: Any,
    baseline: Baseline,
    source: Path,
    source_digest: str,
    work: Path,
    catalog: VariableCatalog,
    report: dict[str, Any],
) -> None:
    app = None
    original: dict[str, Any] | None = None
    before: OperatingSnapshot | None = None
    attempted_open = False
    phase = "connect-application"
    try:
        app = client.GetActiveObject("HYSYS.Application")
        phase = "inventory-source"
        original = _documents(app)
        report["documents_before"] = sorted(original)
        if _path(source) not in original or _path(work) in original:
            raise HysysReadError("case-match", "Source must be open and working path absent")
        bound_client = SimpleNamespace(GetActiveObject=lambda progid: app)
        phase = "read-source-before"
        before = _read_open_case(bound_client, source, catalog, source_digest)
        if not _stable(before):
            raise HysysReadError("unstable-source", "Source must be stable before opening a copy")
        if not _same_documents(original, _documents(app)):
            raise HysysReadError("case-inventory-changed", "Cases changed before Open")
        phase = "open-working-copy"
        attempted_open = True
        work_case = app.SimulationCases.Open(str(work))
        phase = "verify-working-ownership"
        documents = _documents(app)
        if (
            _path(work_case.FullName) != _path(work)
            or any(work_case._oleobj_ == case._oleobj_ for case in original.values())
            or _path(work) not in documents
            or documents[_path(work)]._oleobj_ != work_case._oleobj_
        ):
            raise HysysReadError(
                "work-ownership", "Open did not return the unique new working case"
            )
        report["work_case"]["ownership_verified"] = True
        report["documents_with_work"] = sorted(documents)
        if not _same_documents({**original, _path(work): work_case}, documents):
            raise HysysReadError("case-inventory-changed", "Open changed other cases")
        phase = "read-working-copy"
        observed = _read_open_case(bound_client, work, catalog, baseline.baseline_sha256)
        observed_path = work.parent / "observed.json"
        write_snapshot(observed_path, observed)
        if read_snapshot(observed_path) != observed:
            raise HysysReadError("snapshot-roundtrip", "Working observation did not reload exactly")
        report["observed_snapshot"] = {
            "path": str(observed_path),
            "sha256": _sha256(_file_bytes(observed_path)),
        }
        report["comparison"] = compare_operating_points(baseline.snapshot, observed)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - external COM boundary
        _error(report, error, phase)
    finally:
        if app is not None and original is not None:
            if attempted_open:
                try:
                    _close_copy(app, original, work, report)
                except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - owned cleanup
                    _error(report, error, "close-working-copy")
            try:
                remaining = _documents(app)
                report["documents_after"] = sorted(remaining)
                restored = _same_documents(original, remaining)
                report["source_protection"]["original_cases_restored"] = restored
                if not restored:
                    raise HysysReadError(
                        "case-inventory-changed", "Original cases were not restored"
                    )
            except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - source protection
                _error(report, error, "verify-original-cases")
            if before is not None:
                try:
                    bound_client = SimpleNamespace(GetActiveObject=lambda progid: app)
                    after = _read_open_case(bound_client, source, catalog, source_digest)
                    after = replace(after, observed_at_utc=before.observed_at_utc)
                    unchanged = _stable(after) and after == before
                    report["source_protection"]["observation_unchanged"] = unchanged
                    report["source_protection"]["changed_fields"] = [
                        name
                        for name, value in before.to_dict().items()
                        if after.to_dict()[name] != value
                    ]
                    if not unchanged:
                        raise HysysReadError("source-state-changed", "Source observation changed")
                except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - source protection
                    _error(report, error, "read-source-after")
    # Only plain values entered report. This frame and caught exception traces end before CoUninitialize.


def verify_baseline(manifest_path: Path, output_dir: Path) -> Path:
    """Open, observe and close an owned copy; never write MV, solve or quit HYSYS.

    Invalid baseline inputs and existing output paths fail before COM and create no
    report. Once the new output directory exists, failures are recorded there.
    Synchronous COM calls can block; this function does not claim a hard timeout.
    """
    manifest = manifest_path.expanduser().absolute()
    manifest_bytes = _file_bytes(manifest)
    baseline = read_baseline(manifest)
    if _file_bytes(manifest) != manifest_bytes:
        raise ValueError("Baseline manifest changed during validation")
    directory = output_dir.expanduser().absolute()
    _plain_directory(directory.parent)
    directory.mkdir(exist_ok=False)
    work = directory / "work.hsc"
    report: dict[str, Any] = {
        "schema_id": "hysys-baseline-recovery",
        "schema_version": "1.0.0",
        "status": "failed",
        "eligible_for_optimization": False,
        "baseline_manifest": {"path": str(manifest), "sha256": _sha256(manifest_bytes)},
        "work_case": {
            "path": str(work),
            "ownership_verified": False,
            "close_requested": False,
            "closed": False,
        },
        "source_protection": {
            "original_cases_restored": False,
            "observation_unchanged": False,
            "disk_unchanged": False,
        },
        "com_lifecycle": {"initialized": False, "uninitialized": False},
        "observed_snapshot": None,
        "comparison": None,
        "file_checks": [],
        "errors": [],
    }
    protected: dict[Path, str] = {manifest: _sha256(manifest_bytes)}
    source = Path(baseline.snapshot.source_case_path).expanduser().absolute()
    phase = "prepare-working-copy"
    try:
        for name in ("baseline.hsc", "snapshot.json", "variables.json"):
            path = baseline.directory / name
            protected[path] = _sha256(_file_bytes(path))
        if protected[baseline.directory / "baseline.hsc"] != baseline.baseline_sha256:
            raise ValueError("Frozen baseline changed before copying")
        # Revalidate all frozen content before loading it into the application.
        if read_baseline(manifest) != baseline:
            raise ValueError("Baseline changed before recovery")
        payload = _file_bytes(baseline.directory / "baseline.hsc")
        if _sha256(payload) != baseline.baseline_sha256:
            raise ValueError("Frozen baseline changed while copying")
        with work.open("xb") as stream:
            stream.write(payload)
        protected[work] = baseline.baseline_sha256
        if _sha256(_file_bytes(work)) != baseline.baseline_sha256:
            raise ValueError("Working copy hash differs")
        protected[source] = _sha256(_file_bytes(source))
        catalog = load_catalog(baseline.catalog_path)
        if _sha256(_file_bytes(baseline.catalog_path)) != protected[baseline.catalog_path]:
            raise ValueError("Frozen catalog changed during loading")
        phase = "initialize-com"
        pythoncom = importlib.import_module("pythoncom")
        client = importlib.import_module("win32com.client")
        pythoncom.CoInitialize()
        report["com_lifecycle"]["initialized"] = True
        try:
            _session(client, baseline, source, protected[source], work, catalog, report)
        finally:
            phase = "uninitialize-com"
            pythoncom.CoUninitialize()
            report["com_lifecycle"]["uninitialized"] = True
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - persist the failed attempt
        _error(report, error, phase)
    finally:
        for path, expected in protected.items():
            check: dict[str, Any] = {
                "path": str(path),
                "expected_sha256": expected,
                "unchanged": False,
            }
            try:
                actual = _sha256(_file_bytes(path))
                check.update(sha256=actual, unchanged=actual == expected)
                if path == source:
                    report["source_protection"]["disk_unchanged"] = actual == expected
                if actual != expected:
                    raise HysysReadError("file-changed", f"Protected file changed: {path}")
            except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - independent file checks
                _error(report, error, "verify-files-after")
            report["file_checks"].append(check)
    if (
        not report["errors"]
        and report["comparison"] is not None
        and report["comparison"]["equivalent"]
        and report["work_case"]["closed"]
        and all(
            report["source_protection"][name]
            for name in ("original_cases_restored", "observation_unchanged", "disk_unchanged")
        )
    ):
        report["status"] = "verified"
    path = directory / "report.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return path
