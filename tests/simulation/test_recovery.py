"""Recovery ownership and failure boundaries; all application objects are synthetic."""

from __future__ import annotations

import hashlib
import json
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_hysys_reader import (
    FakeApplication,
    FakeCase,
    FakeCollection,
    FakeComError,
    _catalog_document,
)

from petroleum_rto.simulation import recovery
from petroleum_rto.simulation.hysys import _read_open_case, load_catalog
from petroleum_rto.simulation.models import read_snapshot, write_snapshot


@pytest.fixture
def attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    source = tmp_path / "source.hsc"
    source.write_bytes(b"source disk is not the dirty memory baseline")
    frozen = tmp_path / "baseline"
    frozen.mkdir()
    document = _catalog_document()
    catalog = frozen / "variables.json"
    catalog.write_text(json.dumps(document), encoding="utf-8")
    original = FakeCase(source, document["variables"])
    app = FakeApplication(original)
    snapshot = _read_open_case(
        SimpleNamespace(GetActiveObject=lambda name: app),
        source,
        load_catalog(catalog),
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    write_snapshot(frozen / "snapshot.json", snapshot)
    (frozen / "baseline.hsc").write_bytes(b"frozen in-memory baseline")
    manifest = frozen / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_id": "hysys-baseline",
                "schema_version": "1.0.0",
                "source_unchanged": True,
                "reopen_verified": False,
                "eligible_for_optimization": False,
                "files": {
                    name: hashlib.sha256((frozen / name).read_bytes()).hexdigest()
                    for name in ("baseline.hsc", "snapshot.json", "variables.json")
                },
            }
        ),
        encoding="utf-8",
    )
    state = SimpleNamespace(
        source=source,
        frozen=frozen,
        manifest=manifest,
        directory=tmp_path / "recovery",
        events=[],
        references=[],
        configure=lambda source, app: None,
        after_open=lambda work, source, app: None,
        after_close=lambda work, app: None,
        open_error=None,
        close_error=None,
        init_error=None,
        create_work=True,
        return_source=False,
    )

    class Case(FakeCase):
        def __init__(self, path: Path) -> None:
            super().__init__(path, document["variables"])
            self._oleobj_ = object()

        def Close(self, save: bool = True) -> None:
            assert save is False
            assert Path(self.FullName) == state.directory / "work.hsc", "source Close forbidden"
            state.events.append("Close(False)")
            active = state.references[0]()
            if state.close_error:
                raise state.close_error("synthetic close failure")
            active.SimulationCases.items.remove(self)
            state.after_close(self, active)

    class Cases(FakeCollection):
        def Open(self, path: str) -> Any:
            assert Path(path) == state.directory / "work.hsc"
            state.events.append("Open")
            active = state.references[0]()
            source_case = self.items[0]
            work = Case(Path(path))
            work.IsDirty = False
            state.references.append(weakref.ref(work))
            if state.create_work:
                self.items.append(work)
            state.after_open(work, source_case, active)
            if state.open_error:
                raise state.open_error("synthetic Open failure")
            return source_case if state.return_source else work

    def connect(progid: str) -> FakeApplication:
        assert progid == "HYSYS.Application"
        state.events.append("GetActiveObject")
        assert state.events.count("GetActiveObject") == 1
        source_case = Case(source)
        active = FakeApplication(source_case)
        other = Case(tmp_path / "other-user-case.hsc")
        active.SimulationCases = Cases([source_case, other])
        state.references.extend(weakref.ref(item) for item in (active, source_case, other))
        state.configure(source_case, active)
        return active

    def initialize() -> None:
        state.events.append(("initialize", threading.get_ident()))
        if state.init_error:
            raise state.init_error("synthetic initialize failure")

    def uninitialize() -> None:
        assert all(reference() is None for reference in state.references), "COM frame leaked"
        state.events.append(("uninitialize", threading.get_ident()))

    def import_module(name: str) -> Any:
        if name == "pythoncom":
            return SimpleNamespace(CoInitialize=initialize, CoUninitialize=uninitialize)
        assert name == "win32com.client"
        return SimpleNamespace(GetActiveObject=connect)

    monkeypatch.setattr(recovery, "importlib", SimpleNamespace(import_module=import_module))
    state.run = lambda: recovery.verify_baseline(manifest, state.directory)
    state.report = lambda: json.loads(state.run().read_text(encoding="utf-8"))
    return state


def test_owned_copy_is_recovered_and_closed_without_changing_source_or_manifest(
    attempt: SimpleNamespace,
) -> None:
    manifest = attempt.manifest.read_bytes()
    result = attempt.report()
    assert result["status"] == "verified"
    assert result["eligible_for_optimization"] is False
    assert result["comparison"]["equivalent"] is True
    assert result["work_case"] == {
        "path": str(attempt.directory / "work.hsc"),
        "ownership_verified": True,
        "close_requested": True,
        "closed": True,
    }
    assert result["source_protection"] == {
        "original_cases_restored": True,
        "observation_unchanged": True,
        "disk_unchanged": True,
        "changed_fields": [],
    }
    assert len(result["documents_before"]) == 2
    assert len(result["documents_with_work"]) == 3
    assert result["documents_before"] == result["documents_after"]
    assert all(check["unchanged"] for check in result["file_checks"])
    assert len(result["file_checks"]) == 6
    assert attempt.manifest.read_bytes() == manifest
    assert read_snapshot(attempt.directory / "observed.json").memory_is_dirty is False
    assert (
        result["observed_snapshot"]["sha256"]
        == hashlib.sha256((attempt.directory / "observed.json").read_bytes()).hexdigest()
    )
    assert attempt.events[-2:] == ["Close(False)", ("uninitialize", threading.get_ident())]


def test_existing_output_is_never_overwritten(attempt: SimpleNamespace) -> None:
    attempt.directory.mkdir()
    marker = attempt.directory / "keep.txt"
    marker.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        attempt.run()
    assert marker.read_bytes() == b"keep"
    assert attempt.events == []


def test_bad_baseline_fails_before_output_creation_or_com(attempt: SimpleNamespace) -> None:
    (attempt.frozen / "baseline.hsc").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        attempt.run()
    assert not attempt.directory.exists()
    assert attempt.events == []


@pytest.mark.parametrize("create_work", [True, False])
@pytest.mark.parametrize("error_type", [FakeComError, KeyboardInterrupt])
def test_open_failure_cleanup_releases_frames_and_only_closes_a_new_exact_path(
    attempt: SimpleNamespace, create_work: bool, error_type: type[BaseException]
) -> None:
    attempt.create_work = create_work
    attempt.open_error = error_type
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["errors"][0]["phase"] == "open-working-copy"
    assert result["errors"][0]["code"] == (
        "interrupted" if error_type is KeyboardInterrupt else "recovery-failed"
    )
    if error_type is FakeComError:
        assert result["errors"][0]["scode"] == -2147024891
    assert ("Close(False)" in attempt.events) == create_work
    assert result["source_protection"]["original_cases_restored"] is True
    assert result["source_protection"]["observation_unchanged"] is True
    assert result["com_lifecycle"]["uninitialized"] is True


def test_wrong_returned_object_is_rejected_but_proven_new_work_is_closed(
    attempt: SimpleNamespace,
) -> None:
    attempt.return_source = True
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "work-ownership"
    assert result["work_case"]["close_requested"] is True
    assert result["source_protection"]["original_cases_restored"] is True


def test_original_object_under_work_path_is_never_closed(attempt: SimpleNamespace) -> None:
    def after_open(work: Any, source: Any, app: Any) -> None:
        app.SimulationCases.items.remove(work)
        source.FullName = work.FullName

    attempt.after_open = after_open
    result = attempt.report()
    assert result["status"] == "failed"
    assert "Close(False)" not in attempt.events
    assert result["source_protection"]["original_cases_restored"] is False
    assert any(error["code"] == "work-ownership" for error in result["errors"])


@pytest.mark.parametrize("condition", ["extra", "removed", "replacement"])
def test_other_case_changes_fail_without_closing_unowned_cases(
    attempt: SimpleNamespace, condition: str
) -> None:
    def after_open(work: Any, source: Any, app: Any) -> None:
        if condition == "extra":
            app.SimulationCases.items.append(
                SimpleNamespace(
                    FullName=str(attempt.source.with_name("new-user.hsc")), _oleobj_=object()
                )
            )
        elif condition == "removed":
            app.SimulationCases.items.pop(1)
        else:
            old = app.SimulationCases.items[1]
            app.SimulationCases.items[1] = SimpleNamespace(FullName=old.FullName, _oleobj_=object())

    attempt.after_open = after_open
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["work_case"]["closed"] is True
    assert result["source_protection"]["original_cases_restored"] is False
    assert attempt.events.count("Close(False)") == 1


def test_recovery_difference_is_recorded_and_copy_still_closed(attempt: SimpleNamespace) -> None:
    attempt.after_open = lambda work, source, app: setattr(
        work.table.cells["C28"].ImportedVariable, "value", 43.0
    )
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["comparison"]["equivalent"] is False
    assert len(result["comparison"]["output_differences"]) == 1
    assert result["work_case"]["closed"] is True
    assert result["source_protection"]["observation_unchanged"] is True


@pytest.mark.parametrize("field", ["dirty", "value"])
def test_source_observation_change_is_not_hidden_by_successful_recovery(
    attempt: SimpleNamespace, field: str
) -> None:
    def after_open(work: Any, source: Any, app: Any) -> None:
        if field == "dirty":
            source.IsDirty = False
        else:
            source.table.cells["C28"].ImportedVariable.value = 43.0

    attempt.after_open = after_open
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["comparison"]["equivalent"] is True
    assert result["source_protection"]["observation_unchanged"] is False
    assert result["source_protection"]["changed_fields"] == (
        ["memory_is_dirty"] if field == "dirty" else ["variables"]
    )


@pytest.mark.parametrize("target", ["source", "work", "baseline", "catalog", "manifest"])
def test_file_changes_cannot_pass_recovery(attempt: SimpleNamespace, target: str) -> None:
    def after_close(work: Any, app: Any) -> None:
        path = {
            "source": attempt.source,
            "work": attempt.directory / "work.hsc",
            "baseline": attempt.frozen / "baseline.hsc",
            "catalog": attempt.frozen / "variables.json",
            "manifest": attempt.manifest,
        }[target]
        path.write_bytes(b"external change")

    attempt.after_close = after_close
    result = attempt.report()
    assert result["status"] == "failed"
    assert any(error["code"] == "file-changed" for error in result["errors"])
    assert sum(not check["unchanged"] for check in result["file_checks"]) == 1
    if target == "source":
        assert result["source_protection"]["disk_unchanged"] is False


@pytest.mark.parametrize("error_type", [FakeComError, KeyboardInterrupt])
def test_read_errors_release_com_frames_and_close_owned_copy(
    attempt: SimpleNamespace, error_type: type[BaseException]
) -> None:
    attempt.after_open = lambda work, source, app: setattr(work.table, "fail_read", error_type)
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["work_case"]["closed"] is True
    assert result["errors"][0]["phase"] == "read-working-copy"
    assert result["com_lifecycle"]["uninitialized"] is True


def test_close_failure_is_retained_and_source_still_checked(attempt: SimpleNamespace) -> None:
    attempt.close_error = FakeComError
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["work_case"]["closed"] is False
    assert result["source_protection"]["observation_unchanged"] is True
    assert result["source_protection"]["original_cases_restored"] is False
    assert any(error["phase"] == "close-working-copy" for error in result["errors"])
    assert result["com_lifecycle"]["uninitialized"] is True


def test_initialize_failure_gets_a_report_without_uninitialize(attempt: SimpleNamespace) -> None:
    attempt.init_error = FakeComError
    result = attempt.report()
    assert result["status"] == "failed"
    assert result["com_lifecycle"] == {"initialized": False, "uninitialized": False}
    assert "GetActiveObject" not in attempt.events
