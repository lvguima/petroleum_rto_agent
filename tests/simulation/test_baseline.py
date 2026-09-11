"""Baseline capture boundaries, with synthetic COM objects and temporary files only."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import weakref
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_hysys_reader import FakeApplication, FakeCase, FakeComError, FakeSolver, _catalog_document

from petroleum_rto.simulation import baseline
from petroleum_rto.simulation.hysys import HysysReadError
from petroleum_rto.simulation.models import read_snapshot


@pytest.fixture
def capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    source = tmp_path / "模型.hsc"
    source.write_bytes(b"synthetic disk source, different from dirty memory")
    catalog = tmp_path / "catalog.json"
    document = _catalog_document()
    catalog.write_text(json.dumps(document, indent=4) + "\n", encoding="utf-8")
    state = SimpleNamespace(
        source=source,
        catalog=catalog,
        document=document,
        directory=tmp_path / "new-baseline",
        configure=lambda case, app: None,
        after_save=lambda case, app: None,
        events=[],
        references=[],
        saved_samples=[],
        init_error=None,
        save_error=None,
        save_bytes=b"synthetic HYSYS copy of dirty memory",
        produce_file=True,
    )

    class CopyCase(FakeCase):
        def __init__(self, source: Path, bindings: list[dict[str, Any]]) -> None:
            super().__init__(source, bindings)
            self._oleobj_ = object()

        def SaveCopyAs(self, path: str, flag: bool) -> None:
            assert flag is False
            assert Path(path) == state.directory / "baseline.hsc"
            state.events.append("SaveCopyAs")
            state.saved_samples.append(self.table.sample_count)
            if state.produce_file:
                with Path(path).open("xb") as stream:
                    stream.write(state.save_bytes)
            if state.save_error:
                raise state.save_error("synthetic save failure")
            state.after_save(self, state.references[1]())

    def active_application(progid: str) -> FakeApplication:
        assert progid == "HYSYS.Application"
        state.events.append("GetActiveObject")
        assert state.events.count("GetActiveObject") == 1, "must bind to the same COM application"
        case = CopyCase(source, document["variables"])
        app = FakeApplication(case)
        state.references.extend(weakref.ref(item) for item in (case, app, case.table))
        state.configure(case, app)
        return app

    def initialize() -> None:
        state.events.append(("initialize", threading.get_ident()))
        if state.init_error:
            raise state.init_error

    def uninitialize() -> None:
        assert all(reference() is None for reference in state.references), "COM objects retained"
        state.events.append(("uninitialize", threading.get_ident()))

    def import_module(name: str) -> Any:
        state.events.append(name)
        if name == "pythoncom":
            return SimpleNamespace(CoInitialize=initialize, CoUninitialize=uninitialize)
        assert name == "win32com.client"
        return SimpleNamespace(GetActiveObject=active_application)

    monkeypatch.setattr(baseline, "importlib", SimpleNamespace(import_module=import_module))
    state.run = lambda: baseline.capture_baseline(source, state.directory, catalog)
    return state


def test_capture_preserves_source_and_catalog_bytes_and_strictly_reloads(
    capture: SimpleNamespace,
) -> None:
    original_source = capture.source.read_bytes()
    original_catalog = capture.catalog.read_bytes()
    path = capture.run()
    result = baseline.read_baseline(path)
    assert path == capture.directory / "manifest.json"
    assert {item.name for item in capture.directory.iterdir()} == {
        "baseline.hsc",
        "snapshot.json",
        "variables.json",
        "manifest.json",
    }
    assert capture.source.read_bytes() == original_source
    assert result.catalog_path.read_bytes() == original_catalog
    assert result.snapshot == read_snapshot(capture.directory / "snapshot.json")
    assert result.snapshot.memory_is_dirty is True
    assert result.snapshot.source_disk_sha256 == hashlib.sha256(original_source).hexdigest()
    assert result.baseline_sha256 == hashlib.sha256(capture.save_bytes).hexdigest()
    assert result.baseline_sha256 != result.snapshot.source_disk_sha256
    assert result.directory == capture.directory
    manifest = json.loads(path.read_text())
    assert manifest["source_unchanged"] is True
    assert manifest["reopen_verified"] is False
    assert manifest["eligible_for_optimization"] is False
    assert capture.saved_samples == [2]
    assert capture.events[-1] == ("uninitialize", threading.get_ident())
    with pytest.raises(FrozenInstanceError):
        result.directory = capture.source  # type: ignore[misc]


@pytest.mark.parametrize("existing", ["directory", "file"])
def test_existing_output_is_never_reused(capture: SimpleNamespace, existing: str) -> None:
    if existing == "directory":
        capture.directory.mkdir()
        (capture.directory / "keep.txt").write_bytes(b"keep")
    else:
        capture.directory.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        capture.run()
    assert capture.events == []
    assert (
        capture.directory / "keep.txt" if existing == "directory" else capture.directory
    ).read_bytes() == b"keep"


@pytest.mark.parametrize("invalid", ["catalog", "suffix", "missing_source"])
def test_inputs_are_validated_before_creating_output_or_connecting(
    capture: SimpleNamespace, invalid: str
) -> None:
    if invalid == "catalog":
        capture.catalog.write_text('{"schema_version":"invalid"}')
    elif invalid == "suffix":
        capture.source = capture.source.with_suffix(".txt")
        capture.source.write_bytes(b"not hsc")
        capture.run = lambda: baseline.capture_baseline(
            capture.source, capture.directory, capture.catalog
        )
    else:
        capture.source.unlink()
    with pytest.raises(HysysReadError):
        capture.run()
    assert capture.events == []
    assert not capture.directory.exists()


@pytest.mark.parametrize("condition", ["different_case", "no_case", "duplicate_case"])
def test_exact_unique_open_source_is_required(capture: SimpleNamespace, condition: str) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        if condition == "different_case":
            case.FullName = str(capture.source.with_name("other.hsc"))
        else:
            app.SimulationCases.items = [] if condition == "no_case" else [case, case]

    capture.configure = configure
    with pytest.raises(HysysReadError) as failure:
        capture.run()
    assert failure.value.code == "case-match"
    assert "SaveCopyAs" not in capture.events
    assert not (capture.directory / "manifest.json").exists()


@pytest.mark.parametrize("condition", ["changing", "solving", "invalid", "unconverged", "dof"])
def test_unstable_source_is_never_saved(capture: SimpleNamespace, condition: str) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        if condition == "changing":
            case.table.change_second_sample = True
        elif condition == "solving":
            case.Solver = FakeSolver(IsSolving=True)
        elif condition == "invalid":
            case.IsValid = False
        elif condition == "unconverged":
            case.cfs.CfsConverged = False
        else:
            case.cfs.DegreesOfFreedom = 1

    capture.configure = configure
    with pytest.raises(HysysReadError) as failure:
        capture.run()
    assert failure.value.code == "unstable-source"
    assert "SaveCopyAs" not in capture.events


@pytest.mark.parametrize(
    "condition",
    ["dirty", "value", "stage", "specification", "version", "path", "count", "disk", "solving"],
)
def test_any_source_drift_after_save_prevents_success_manifest(
    capture: SimpleNamespace, condition: str
) -> None:
    def after_save(case: FakeCase, app: FakeApplication) -> None:
        if condition == "dirty":
            case.IsDirty = False
        elif condition == "value":
            case.table.cells["C28"].ImportedVariable.value += 1
        elif condition == "stage":
            case.cfs.ColumnStages.Item(0).SeparationStage.Temperature.value += 1
        elif condition == "specification":
            case.cfs.Specifications.Item("Monitor").Current.value += 1
        elif condition == "version":
            app.Version = "different"
        elif condition == "path":
            case.FullName = str(capture.source.with_name("different.hsc"))
        elif condition == "count":
            app.SimulationCases.items.append(
                SimpleNamespace(FullName=str(capture.source.with_name("other.hsc")))
            )
        elif condition == "disk":
            capture.source.write_bytes(b"external change")
        else:
            case.Solver = FakeSolver(IsSolving=True)

    capture.after_save = after_save
    with pytest.raises(HysysReadError) as failure:
        capture.run()
    expected = {"path": "case-match", "count": "case-count-changed", "disk": "source-file-changed"}
    assert failure.value.code == expected.get(condition, "source-state-changed")
    assert (capture.directory / "baseline.hsc").read_bytes() == capture.save_bytes
    assert not (capture.directory / "manifest.json").exists()


def test_source_disk_change_before_save_is_rejected(capture: SimpleNamespace) -> None:
    capture.configure = lambda case, app: capture.source.write_bytes(b"external change")
    with pytest.raises(HysysReadError) as failure:
        capture.run()
    assert failure.value.code == "source-file-changed"
    assert "SaveCopyAs" not in capture.events


@pytest.mark.parametrize("phase", ["before_save", "after_save"])
@pytest.mark.parametrize("rename_original", [False, True])
def test_replacing_source_with_equal_observations_and_count_is_rejected(
    capture: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    rename_original: bool,
) -> None:
    def replace_source() -> None:
        original = capture.references[0]()
        app = capture.references[1]()
        replacement = type(original)(capture.source, capture.document["variables"])
        assert replacement._oleobj_ != original._oleobj_
        assert replacement.FullName == original.FullName
        if rename_original:
            original.FullName = str(capture.source.with_name("saved-as.hsc"))
        app.SimulationCases.items = [replacement]
        capture.references.append(weakref.ref(replacement))

    if phase == "before_save":
        read = baseline._read_open_case

        def read_then_replace(*args: Any) -> Any:
            snapshot = read(*args)
            replace_source()
            return snapshot

        monkeypatch.setattr(baseline, "_read_open_case", read_then_replace)
    else:
        capture.after_save = lambda case, app: replace_source()
    with pytest.raises(HysysReadError) as failure:
        capture.run()
    assert failure.value.code == "source-case-changed"
    assert ("SaveCopyAs" in capture.events) == (phase == "after_save")
    assert not (capture.directory / "manifest.json").exists()
    assert capture.events[-1] == ("uninitialize", threading.get_ident())


@pytest.mark.parametrize("error_type", [FakeComError, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["read", "save"])
def test_original_com_traceback_is_released_before_uninitialize(
    capture: SimpleNamespace, error_type: type[BaseException], phase: str
) -> None:
    if phase == "read":
        capture.configure = lambda case, app: setattr(case.table, "fail_read", error_type)
    else:
        capture.save_error = error_type
    with pytest.raises(HysysReadError) as failure:
        capture.run()
    error = failure.value
    assert error.__context__ is None
    assert error.phase == "capture-baseline"
    assert error.code == (
        "interrupted" if error_type is KeyboardInterrupt else "baseline-capture-failed"
    )
    if error_type is FakeComError:
        assert error.hresult == FakeComError.hresult
        assert error.scode == -2147024891
    assert capture.events[-1] == ("uninitialize", threading.get_ident())
    assert not (capture.directory / "manifest.json").exists()
    assert (capture.directory / "baseline.hsc").exists() == (phase == "save")


def test_initialization_failure_is_not_uninitialized(capture: SimpleNamespace) -> None:
    capture.init_error = FakeComError("initialization failed")
    with pytest.raises(FakeComError):
        capture.run()
    assert "GetActiveObject" not in capture.events
    assert not any(
        isinstance(event, tuple) and event[0] == "uninitialize" for event in capture.events
    )


@pytest.mark.parametrize("missing", [True, False])
def test_save_must_produce_a_nonempty_file(capture: SimpleNamespace, missing: bool) -> None:
    capture.produce_file = not missing
    capture.save_bytes = b""
    with pytest.raises(HysysReadError):
        capture.run()
    assert not (capture.directory / "manifest.json").exists()


def test_file_appearing_before_save_is_not_overwritten(capture: SimpleNamespace) -> None:
    capture.configure = lambda case, app: (capture.directory / "baseline.hsc").write_bytes(b"keep")
    with pytest.raises(HysysReadError):
        capture.run()
    assert "SaveCopyAs" not in capture.events
    assert (capture.directory / "baseline.hsc").read_bytes() == b"keep"


def _rewrite_manifest(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        "schema_id",
        "schema_version",
        "source_unchanged",
        "reopen_verified",
        "eligible_for_optimization",
        "boolean_integer",
        "extra",
        "missing",
        "filename",
        "missing_file",
        "digest",
        "files_array",
    ],
)
def test_strict_manifest_shape_and_declarations(capture: SimpleNamespace, mutation: str) -> None:
    path = capture.run()
    document = json.loads(path.read_text())
    if mutation in {"schema_id", "schema_version"}:
        document[mutation] = "unsupported"
    elif mutation in {"source_unchanged", "reopen_verified", "eligible_for_optimization"}:
        document[mutation] = not document[mutation]
    elif mutation == "boolean_integer":
        document["source_unchanged"] = 1
    elif mutation == "extra":
        document["unexpected"] = True
    elif mutation == "missing":
        document.pop("schema_id")
    elif mutation == "filename":
        document["files"]["../baseline.hsc"] = document["files"].pop("baseline.hsc")
    elif mutation == "missing_file":
        document["files"].pop("variables.json")
    elif mutation == "digest":
        document["files"]["baseline.hsc"] = "bad-hash"
    else:
        document["files"] = []
    _rewrite_manifest(path, document)
    with pytest.raises(ValueError):
        baseline.read_baseline(path)


@pytest.mark.parametrize(
    "payload", ['{"a":0,"a":1}', '{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}', "[]"]
)
def test_duplicate_and_nonfinite_json_are_rejected(capture: SimpleNamespace, payload: str) -> None:
    path = capture.run()
    path.write_text(payload)
    with pytest.raises(ValueError):
        baseline.read_baseline(path)


@pytest.mark.parametrize("name", ["baseline.hsc", "snapshot.json", "variables.json"])
@pytest.mark.parametrize("mutation", ["missing", "tampered", "hardlink"])
def test_missing_tampered_and_linked_files_cannot_be_reloaded(
    capture: SimpleNamespace, name: str, mutation: str
) -> None:
    path = capture.run()
    target = capture.directory / name
    if mutation == "missing":
        target.unlink()
    elif mutation == "tampered":
        target.write_bytes(b"changed")
    else:
        os.link(target, capture.directory.parent / "external-link")
    with pytest.raises((ValueError, OSError)):
        baseline.read_baseline(path)


@pytest.mark.parametrize("name", ["snapshot.json", "variables.json", "baseline.hsc"])
def test_rehashing_invalid_content_does_not_bypass_semantic_validation(
    capture: SimpleNamespace, name: str
) -> None:
    path = capture.run()
    target = capture.directory / name
    target.write_bytes(b"" if name == "baseline.hsc" else b"{}")
    document = json.loads(path.read_text())
    document["files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
    _rewrite_manifest(path, document)
    with pytest.raises((ValueError, TypeError)):
        baseline.read_baseline(path)


@pytest.mark.parametrize("field", ["case_id", "object_name"])
def test_catalog_must_match_snapshot_even_with_a_correct_hash(
    capture: SimpleNamespace, field: str
) -> None:
    path = capture.run()
    target = capture.directory / "variables.json"
    catalog = json.loads(target.read_text())
    (catalog if field == "case_id" else catalog["variables"][0])[field] = "different"
    target.write_text(json.dumps(catalog), encoding="utf-8")
    document = json.loads(path.read_text())
    document["files"]["variables.json"] = hashlib.sha256(target.read_bytes()).hexdigest()
    _rewrite_manifest(path, document)
    with pytest.raises(ValueError, match="differ"):
        baseline.read_baseline(path)


def test_manifest_filename_is_fixed(capture: SimpleNamespace) -> None:
    path = capture.run()
    other = path.with_name("other.json")
    path.rename(other)
    with pytest.raises(ValueError, match="manifest.json"):
        baseline.read_baseline(other)
