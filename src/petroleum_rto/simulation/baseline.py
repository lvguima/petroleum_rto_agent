"""Capture a new HYSYS case copy; reopening and optimization remain unverified."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .hysys import DEFAULT_CATALOG, HysysReadError, VariableCatalog, _read_open_case, load_catalog
from .models import OperatingSnapshot, write_snapshot

_DECLARATIONS: dict[str, object] = {
    "schema_id": "hysys-baseline",
    "schema_version": "1.0.0",
    "source_unchanged": True,
    "reopen_verified": False,
    "eligible_for_optimization": False,
}
_FILES = {"baseline.hsc", "snapshot.json", "variables.json"}


@dataclass(frozen=True, slots=True)
class Baseline:
    directory: Path
    baseline_sha256: str
    snapshot: OperatingSnapshot
    catalog_path: Path


def _plain_directory(path: Path) -> None:
    for directory in (path, *path.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Expected a directory without links or reparse points: {directory}")


def _file_bytes(path: Path) -> bytes:
    _plain_directory(path.parent)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_nlink != 1
    ):
        raise ValueError(f"Expected a regular file without links: {path}")
    return path.read_bytes()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable(snapshot: OperatingSnapshot) -> bool:
    return (
        snapshot.consistent_observation
        and not snapshot.solver.is_solving
        and snapshot.solver.is_valid
        and snapshot.solver.column_converged
        and snapshot.degrees_of_freedom == 0
    )


def _save_copy(
    client: Any, source: Path, target: Path, catalog: VariableCatalog, digest: str
) -> OperatingSnapshot:
    app = client.GetActiveObject("HYSYS.Application")
    count = app.SimulationCases.Count
    if type(count) is not int or count < 1:
        raise HysysReadError("case-match", "Expected an open source case")
    matches = [
        case
        for index in range(count)
        for case in [app.SimulationCases.Item(index)]
        if os.path.normcase(os.path.abspath(case.FullName)) == os.path.normcase(str(source))
    ]
    if len(matches) != 1:
        raise HysysReadError("case-match", "The source case must already be open exactly once")
    case = matches[0]

    def verify_source_identity() -> None:
        current = [
            item
            for index in range(app.SimulationCases.Count)
            for item in [app.SimulationCases.Item(index)]
            if os.path.normcase(os.path.abspath(item.FullName)) == os.path.normcase(str(source))
        ]
        if (
            len(current) != 1
            or current[0]._oleobj_ != case._oleobj_
            or os.path.normcase(os.path.abspath(case.FullName)) != os.path.normcase(str(source))
        ):
            raise HysysReadError("source-case-changed", "The original source case was replaced")

    # Both complete observations must use this application, even if the ROT changes.
    bound_client = SimpleNamespace(GetActiveObject=lambda progid: app)
    before = _read_open_case(bound_client, source, catalog, digest)
    if not _stable(before):
        raise HysysReadError(
            "unstable-source", "The source observation must be stable before saving"
        )
    if _sha256(_file_bytes(source)) != digest:
        raise HysysReadError("source-file-changed", "Disk source changed before saving")
    if app.SimulationCases.Count != count:
        raise HysysReadError("case-count-changed", "Open case count changed before saving")
    if os.path.lexists(target):
        raise FileExistsError(target)
    verify_source_identity()
    case.SaveCopyAs(str(target), False)
    after = _read_open_case(bound_client, source, catalog, digest)
    verify_source_identity()
    if not _stable(after) or replace(after, observed_at_utc=before.observed_at_utc) != before:
        raise HysysReadError("source-state-changed", "The source observation changed while saving")
    if app.SimulationCases.Count != count:
        raise HysysReadError("case-count-changed", "Open case count changed while saving")
    if _sha256(_file_bytes(source)) != digest:
        raise HysysReadError("source-file-changed", "Disk source changed while saving")
    if not _file_bytes(target):
        raise HysysReadError("empty-baseline", "SaveCopyAs produced an empty case file")
    return before


def _capture_copy(
    client: Any, source: Path, target: Path, catalog: VariableCatalog, digest: str
) -> tuple[OperatingSnapshot | None, HysysReadError | None]:
    try:
        return _save_copy(client, source, target, catalog, digest), None
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - external COM boundary
        # A fresh error releases the original traceback and its COM objects before uninitializing.
        error = HysysReadError(
            exc.code
            if isinstance(exc, HysysReadError)
            else (
                "interrupted" if isinstance(exc, KeyboardInterrupt) else "baseline-capture-failed"
            ),
            str(exc),
            phase="capture-baseline",
        )
        error.hresult = getattr(exc, "hresult", None)
        details = getattr(exc, "excepinfo", None)
        if isinstance(details, tuple) and len(details) == 6:
            error.scode = details[5]
        return None, error


def capture_baseline(
    case_path: Path, output_dir: Path, catalog_path: Path = DEFAULT_CATALOG
) -> Path:
    """SaveCopyAs into a new directory without opening, closing, or modifying the source."""
    catalog_bytes = _file_bytes(catalog_path.expanduser().absolute())
    catalog = load_catalog(catalog_path.expanduser().absolute())
    if _file_bytes(catalog_path.expanduser().absolute()) != catalog_bytes:
        raise HysysReadError("catalog-changed", "The catalog changed during validation")
    source = case_path.expanduser().absolute()
    if source.suffix.lower() != ".hsc" or not source.is_file():
        raise HysysReadError("invalid-case-path", "An existing .hsc file must be specified")
    digest = _sha256(_file_bytes(source))
    directory = output_dir.expanduser().absolute()
    _plain_directory(directory.parent)
    directory.mkdir(exist_ok=False)
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    pythoncom.CoInitialize()
    try:
        snapshot, error = _capture_copy(client, source, directory / "baseline.hsc", catalog, digest)
    finally:
        # _capture_copy no longer owns COM references or the original exception traceback.
        pythoncom.CoUninitialize()
    if error is not None:
        raise error
    assert snapshot is not None
    if _sha256(_file_bytes(source)) != digest:
        raise HysysReadError("source-file-changed", "Disk source changed during capture")
    write_snapshot(directory / "snapshot.json", snapshot)
    with (directory / "variables.json").open("xb") as stream:
        stream.write(catalog_bytes)
    manifest = {
        **_DECLARATIONS,
        "files": {name: _sha256(_file_bytes(directory / name)) for name in sorted(_FILES)},
    }
    path = directory / "manifest.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return path


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"Nonfinite JSON value: {value}")


def _json(payload: bytes) -> Any:
    return json.loads(
        payload.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_reject_constant
    )


def read_baseline(manifest_path: Path) -> Baseline:
    """Strictly reload the local capture; this does not verify that HYSYS can reopen it."""
    path = manifest_path.expanduser().absolute()
    if path.name != "manifest.json":
        raise ValueError("The baseline manifest must be named manifest.json")
    raw = _json(_file_bytes(path))
    if type(raw) is not dict or set(raw) != {*_DECLARATIONS, "files"}:
        raise ValueError("Baseline manifest has missing or unknown fields")
    for key, expected in _DECLARATIONS.items():
        if type(raw[key]) is not type(expected) or raw[key] != expected:
            raise ValueError(f"Unsupported baseline declaration: {key}")
    hashes = raw["files"]
    if type(hashes) is not dict or set(hashes) != _FILES:
        raise ValueError("Baseline manifest must name exactly the three fixed files")
    contents = {}
    for name, digest in hashes.items():
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"Invalid SHA256 for {name}")
        contents[name] = _file_bytes(path.parent / name)
        if _sha256(contents[name]) != digest:
            raise ValueError(f"Baseline file hash differs: {name}")
    if not contents["baseline.hsc"]:
        raise ValueError("The baseline case must not be empty")
    snapshot = OperatingSnapshot.from_dict(_json(contents["snapshot.json"]))
    if not _stable(snapshot):
        raise ValueError("The baseline snapshot must describe a stable source observation")
    catalog_path = path.parent / "variables.json"
    catalog = load_catalog(catalog_path)
    if _file_bytes(catalog_path) != contents["variables.json"]:
        raise ValueError("The baseline catalog changed during validation")
    if catalog.case_id != snapshot.case_id:
        raise ValueError("The baseline catalog and snapshot case IDs differ")
    for binding, reading in zip(catalog.variables, sorted(snapshot.variables, key=lambda x: x.row)):
        if any(
            getattr(binding, field) != getattr(reading, field)
            for field in (
                "variable_id",
                "row",
                "role",
                "object_name",
                "property_name",
                "quantity_type",
                "unit",
            )
        ):
            raise ValueError("The baseline catalog and snapshot bindings differ")
    return Baseline(path.parent, hashes["baseline.hsc"], snapshot, catalog_path)
