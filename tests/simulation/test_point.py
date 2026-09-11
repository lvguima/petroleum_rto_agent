"""Trusted T-39 point execution, using only synthetic COM objects and temporary files."""

from __future__ import annotations

import hashlib
import json
import threading
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from point_boundary_helpers import boundary_for
from test_hysys_reader import (
    FakeApplication,
    FakeCase,
    FakeCollection,
    FakeComError,
    FakeQuantity,
    FakeSpecification,
)

from petroleum_rto.simulation import point
from petroleum_rto.simulation.hysys import DEFAULT_CATALOG, _read_open_case, load_catalog
from petroleum_rto.simulation.models import read_snapshot, write_snapshot
from petroleum_rto.simulation.point_evidence import read_point_result

_WAIT_FOR_COLUMN = point._wait_for_column


@pytest.fixture
def diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    source = tmp_path / "source.hsc"
    source.write_bytes(b"synthetic source disk")
    directory = tmp_path / "baseline"
    directory.mkdir()
    payload = b"synthetic frozen current memory"
    (directory / "baseline.hsc").write_bytes(payload)
    document = json.loads(DEFAULT_CATALOG.read_text(encoding="utf-8"))
    for entry in document["variables"]:
        entry["column_specification"] = None
    binding = next(item for item in document["variables"] if item["row"] == 23)
    binding.update(
        variable_id=point.VARIABLE_ID,
        object_name="C-1102",
        property_name="规定值 (T-39)",
        quantity_type="temperature",
        unit="C",
        column_specification="T-39",
    )
    catalog_path = directory / "variables.json"
    catalog_path.write_text(json.dumps(document), encoding="utf-8")
    catalog = load_catalog(catalog_path)
    state = SimpleNamespace(
        source=source,
        directory=directory,
        output=tmp_path / "diagnostic",
        target=156.9,
        catalog=catalog,
        events=[],
        references=[],
        set_error=None,
        reset_error=None,
        run_error=None,
        close_error=None,
        close_keeps_open=False,
        duplicate_open=False,
        readback_error=False,
        response_offset=-5e-7,
        stage_response=0.02,
        source_drift=False,
        source_after_error=False,
        replace_source_after_restore=False,
        after_run=lambda: None,
        before_close=lambda case: None,
        configure_open=lambda case: None,
        configure_application=lambda app: None,
        initialize_error=None,
        open_error=None,
        boundary_error=None,
        boundary_mutation=lambda value: value,
    )

    class WritableTemperature(FakeQuantity):
        def __init__(self, name: str) -> None:
            super().__init__("temperature", 156.8)
            self.Value = 156.8
            self.name = name

        def SetValue(self, value: float, unit: str) -> None:
            raise AssertionError("The diagnostic must write through the specification owner")

    class TemperatureSpecification(FakeSpecification):
        def __init__(self, case_name: str) -> None:
            super().__init__("T-39", active=True)
            self.case_name = case_name
            self.Goal = WritableTemperature(case_name)
            self.Current = FakeQuantity("temperature", 156.8 - 5e-7)
            self.goal_value = 156.8
            info = SimpleNamespace(GetDocumentation=lambda member: ("ColumnTemperatureSpec",))
            self._oleobj_ = SimpleNamespace(GetTypeInfo=lambda: info)

        @property
        def GoalValue(self) -> float:
            return self.goal_value

        @GoalValue.setter
        def GoalValue(self, value: float) -> None:
            state.events.append(("GoalValue", self.case_name, value))
            if state.set_error:
                raise state.set_error("synthetic write failure")
            self.goal_value = value
            if not state.readback_error:
                self.Goal.value = self.Goal.Value = value

    class MutableSolver:
        IsSolving = False

        def __init__(self, name: str) -> None:
            self.name = name
            self.enabled = True

        @property
        def CanSolve(self) -> bool:
            return self.enabled

        @CanSolve.setter
        def CanSolve(self, enabled: bool) -> None:
            state.events.append(("CanSolve", self.name, enabled))
            self.enabled = enabled

    class DiagnosticCase(FakeCase):
        def __init__(self, path: Path) -> None:
            super().__init__(path, document["variables"])
            self._oleobj_ = object()
            self.Solver = MutableSolver(path.name)  # type: ignore[assignment]
            self.owner: weakref.ReferenceType[Any] | None = None
            spec = TemperatureSpecification(path.name)
            self.cfs.Specifications.items.append(spec)
            self.cfs.ActiveSpecifications.items.append(spec)
            self.table.cells["C23"].ImportedVariable = spec.Goal
            case_ref = weakref.ref(self)

            def reset() -> None:
                case = case_ref()
                assert case is not None
                assert Path(case.FullName).name == "candidate.hsc", (
                    "Reset is only for the candidate"
                )
                assert case.Solver.CanSolve is False, "Reset must occur while the solver is paused"
                state.events.append(("Reset", "candidate.hsc"))
                if state.reset_error:
                    raise state.reset_error("synthetic Reset failure")

            def run() -> None:
                case = case_ref()
                assert case is not None
                state.events.append(("Run", Path(case.FullName).name))
                if state.run_error:
                    raise state.run_error("synthetic run failure")
                current_spec = case.cfs.Specifications.Item("T-39")
                # Supply planned test observations, without emulating HYSYS solve/notification rules.
                current_spec.Current.value = current_spec.Goal.value + state.response_offset
                case.cfs.ColumnStages.Item(
                    0
                ).SeparationStage.Temperature.value += state.stage_response
                if state.source_drift and case.owner is not None:
                    owner = case.owner()
                    assert owner is not None
                    owner.items[0].IsDirty = False
                state.after_run()

            self.cfs.Reset = reset
            self.cfs.Run = run

        def Close(self, save: bool = False) -> None:
            assert Path(self.FullName) != source, "must never close the source"
            assert save is False
            name = Path(self.FullName).name
            state.events.append(("Close", name, save))
            assert self.owner is not None
            owner = self.owner()
            assert owner is not None
            state.before_close(self)
            if name == "candidate.hsc" and state.close_keeps_open:
                raise FakeComError("close failed and document is still open")
            owner.items.remove(self)
            if name == "restored.hsc" and state.replace_source_after_restore:
                replacement = DiagnosticCase(source)
                replacement.owner = weakref.ref(owner)
                owner.items[0] = replacement
                state.references.append(weakref.ref(replacement))
            if name == "restored.hsc" and state.source_after_error:
                owner.items[0].table.fail_read = FakeComError
            if state.close_error and name == "candidate.hsc":
                raise state.close_error("close returned an error after removing the case")

    class Cases(FakeCollection):
        def Open(self, path: str) -> Any:
            state.events.append(("Open", Path(path).name))
            assert Path(path).read_bytes() == payload
            case = DiagnosticCase(Path(path))
            case.owner = weakref.ref(self)
            state.configure_open(case)
            self.items.append(case)
            state.references.append(weakref.ref(case))
            if state.duplicate_open:
                duplicate = DiagnosticCase(Path(path))
                duplicate.owner = weakref.ref(self)
                self.items.append(duplicate)
                state.references.append(weakref.ref(duplicate))
            if state.open_error:
                raise state.open_error("Open returned an error after creating the case")
            return case

    def make_application() -> FakeApplication:
        case = DiagnosticCase(source)
        app = FakeApplication(case)
        app.SimulationCases = Cases([case])
        case.owner = weakref.ref(app.SimulationCases)
        return app

    # Generate the strict baseline with the same qualified reader used by point execution.
    app = make_application()
    start = _read_open_case(
        SimpleNamespace(GetActiveObject=lambda _: app),
        source,
        catalog,
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    state.case = DiagnosticCase(tmp_path / "candidate.hsc")
    state.start = start
    write_snapshot(directory / "snapshot.json", start)
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
    state.manifest = manifest

    def active_application(progid: str) -> FakeApplication:
        assert progid == "HYSYS.Application"
        state.events.append("GetActiveObject")
        actual_app = make_application()
        state.references.extend(
            weakref.ref(item) for item in (actual_app, actual_app.SimulationCases.Item(0))
        )
        state.configure_application(actual_app)
        return actual_app

    def uninitialize() -> None:
        assert all(ref() is None for ref in state.references), (
            "COM objects retained during teardown"
        )
        state.events.append(("uninitialize", threading.get_ident()))

    def import_module(name: str) -> Any:
        if name == "pythoncom":

            def initialize() -> None:
                state.events.append(("initialize", threading.get_ident()))
                if state.initialize_error:
                    raise state.initialize_error("synthetic initialization failure")

            return SimpleNamespace(
                CoInitialize=initialize,
                CoUninitialize=uninitialize,
            )
        assert name == "win32com.client"
        return SimpleNamespace(GetActiveObject=active_application)

    monkeypatch.setattr(point, "importlib", SimpleNamespace(import_module=import_module))
    monkeypatch.setattr(point, "_wait_for_column", lambda *args: state.events.append("wait"))

    def read_boundary(client: Any, path: Path, definition: Any, catalog: Any, digest: str) -> Any:
        assert path.name == "candidate.hsc"
        assert client.GetActiveObject("HYSYS.Application").SimulationCases.Count == 2
        state.events.append(("boundary", path.name))
        if state.boundary_error:
            raise state.boundary_error("synthetic boundary failure")
        return state.boundary_mutation(boundary_for(_read_open_case(client, path, catalog, digest)))

    monkeypatch.setattr(point, "_read_open_boundary", read_boundary)
    write_result = point.write_point_result

    def capture_raw_report(
        directory: Path, target: float, digest: str, report: dict[str, Any]
    ) -> Path:
        state.raw_report = report
        return write_result(directory, target, digest, report)

    monkeypatch.setattr(point, "write_point_result", capture_raw_report)

    def run() -> dict[str, Any]:
        state.report_path = point.run_t39_point(manifest, state.target, state.output)
        state.result = read_point_result(state.report_path)
        assert state.result.status == state.raw_report["status"]
        return state.raw_report

    state.run = run
    return state


def test_single_change_writes_owner_then_resets_paused_candidate_before_run(
    diagnostic: SimpleNamespace,
) -> None:
    target = point._change(diagnostic.case, diagnostic.catalog, diagnostic.start, 156.9)
    assert target == 156.8 + 0.1
    assert diagnostic.events == [
        ("CanSolve", "candidate.hsc", False),
        ("GoalValue", "candidate.hsc", target),
        ("Reset", "candidate.hsc"),
        ("CanSolve", "candidate.hsc", True),
        ("Run", "candidate.hsc"),
    ]


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_boundary_failure_still_closes_candidate_restores_and_protects_source(
    diagnostic: SimpleNamespace, error: type[BaseException]
) -> None:
    diagnostic.boundary_error = error
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["source_unchanged"] is True and report["restore_error"] is None
    assert ("Close", "candidate.hsc", False) in diagnostic.events
    assert ("Close", "restored.hsc", False) in diagnostic.events


def test_boundary_is_read_before_closing_changed_working_case(diagnostic: SimpleNamespace) -> None:
    diagnostic.run()
    assert diagnostic.result.boundary is not None
    assert diagnostic.events.index(("boundary", "candidate.hsc")) < diagnostic.events.index(
        ("Close", "candidate.hsc", False)
    )


@pytest.mark.parametrize("condition", ["owner_drift", "kelvin_internal"])
def test_owner_value_and_internal_celsius_qualification_precede_any_write(
    diagnostic: SimpleNamespace,
    condition: str,
) -> None:
    spec = diagnostic.case.cfs.Specifications.Item("T-39")
    start = diagnostic.start
    if condition == "owner_drift":
        spec.goal_value = 157.0
    else:
        spec.goal_value = spec.Goal.Value = 156.8 + 273.15
        start = replace(
            start,
            variables=tuple(
                replace(item, internal_value=spec.goal_value)
                if item.variable_id == point.VARIABLE_ID
                else item
                for item in start.variables
            ),
        )
    with pytest.raises(ValueError):
        point._change(diagnostic.case, diagnostic.catalog, start, 156.9)
    assert diagnostic.events == []


def test_specification_diagnostics_preserve_distinct_active_collection_values(
    diagnostic: SimpleNamespace,
) -> None:
    active = SimpleNamespace(
        Name="T-39",
        GoalValue=155.0,
        Goal=FakeQuantity("temperature", 155.0),
        Current=FakeQuantity("temperature", 154.0),
    )
    diagnostic.case.cfs.ActiveSpecifications = FakeCollection([active])
    values = point._specification_values(diagnostic.case, diagnostic.catalog)
    assert values["goal_value_internal"] == values["goal_C"] == 156.8
    assert values["active_goal_value_internal"] == values["active_goal_C"] == 155.0
    assert values["active_current_C"] == 154.0
    assert values["current_C"] == 156.8 - 5e-7


@pytest.mark.parametrize("count", [0, 2])
def test_specification_diagnostics_require_one_active_t39(
    diagnostic: SimpleNamespace,
    count: int,
) -> None:
    spec = diagnostic.case.cfs.Specifications.Item("T-39")
    diagnostic.case.cfs.ActiveSpecifications = FakeCollection([spec] * count)
    with pytest.raises(ValueError):
        point._specification_values(diagnostic.case, diagnostic.catalog)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_name", "other"),
        ("property_name", "other"),
        ("row", 3),
        ("role", "cv"),
        ("quantity_type", "temperature_difference"),
        ("unit", "K"),
        ("column_specification", None),
    ],
)
def test_unqualified_fixed_diagnostic_binding_never_writes(
    diagnostic: SimpleNamespace,
    field: str,
    value: Any,
) -> None:
    catalog = diagnostic.catalog
    bindings = tuple(
        replace(x, **{field: value}) if x.variable_id == point.VARIABLE_ID else x
        for x in catalog.variables
    )
    with pytest.raises(ValueError):
        point._change(
            diagnostic.case, replace(catalog, variables=bindings), diagnostic.start, 156.9
        )
    assert diagnostic.events == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("UnitConversionType", 24),
        ("State", 0),
        ("CanModify", False),
        ("IsKnown", False),
        ("value", 155.0),
    ],
)
def test_live_variable_drift_never_writes(
    diagnostic: SimpleNamespace, field: str, value: Any
) -> None:
    setattr(diagnostic.case.table.cells["C23"].ImportedVariable, field, value)
    with pytest.raises(ValueError):
        point._change(diagnostic.case, diagnostic.catalog, diagnostic.start, 156.9)
    assert diagnostic.events == []


@pytest.mark.parametrize("field", ["AttachedObjectName", "VariableName", "active"])
def test_live_binding_or_active_specification_drift_prevents_write(
    diagnostic: SimpleNamespace,
    field: str,
) -> None:
    if field == "active":
        diagnostic.case.cfs.Specifications.Item("T-39").IsActive = False
    else:
        setattr(diagnostic.case.table.cells["C23"], field, "changed")
    with pytest.raises(ValueError):
        point._change(diagnostic.case, diagnostic.catalog, diagnostic.start, 156.9)
    assert diagnostic.events == []


@pytest.mark.parametrize("condition", ["ready", "busy"])
def test_column_polling_waits_for_stability_or_stops_at_deadline(
    diagnostic: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    ticks = iter([0.0, 0.0, 0.5, 1.0] if condition == "ready" else [0.0, 0.0, 120.0])
    sleeps: list[float] = []
    monkeypatch.setattr(
        point, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append)
    )
    pythoncom = SimpleNamespace(PumpWaitingMessages=lambda: None)
    if condition == "busy":
        diagnostic.case.Solver.CanSolve = False
        with pytest.raises(TimeoutError):
            _WAIT_FOR_COLUMN(diagnostic.case, diagnostic.catalog, pythoncom)
    else:
        _WAIT_FOR_COLUMN(diagnostic.case, diagnostic.catalog, pythoncom)
    assert sleeps and all(0 < wait <= 0.25 for wait in sleeps)


@pytest.mark.parametrize("failure", ["write", "readback"])
def test_write_failure_never_enables_solver_or_calls_run(
    diagnostic: SimpleNamespace, failure: str
) -> None:
    diagnostic.set_error = FakeComError if failure == "write" else None
    diagnostic.readback_error = failure == "readback"
    with pytest.raises((FakeComError, ValueError)):
        point._change(diagnostic.case, diagnostic.catalog, diagnostic.start, 156.9)
    assert len(diagnostic.events) == 2
    assert diagnostic.case.Solver.CanSolve is False


def test_expected_change_allows_only_the_single_mv_and_matching_goal(
    diagnostic: SimpleNamespace,
) -> None:
    before = diagnostic.start
    expected = point._expected_changed(before, 156.9)
    assert [a.variable_id for a, b in zip(before.variables, expected.variables) if a != b] == [
        point.VARIABLE_ID
    ]
    assert [a.name for a, b in zip(before.specifications, expected.specifications) if a != b] == [
        "T-39"
    ]
    variable = next(x for x in expected.variables if x.variable_id == point.VARIABLE_ID)
    assert variable.value == variable.internal_value == 156.9
    assert expected.stages == before.stages
    for old, new in zip(before.specifications, expected.specifications):
        assert new == replace(old, goal=156.9) if old.name == "T-39" else new == old
    assert before == diagnostic.start


@pytest.mark.parametrize("offset", [-0.1, -0.2, -0.02, 0.02])
def test_unrelated_output_change_cannot_substitute_for_t39_response(
    diagnostic: SimpleNamespace, offset: float
) -> None:
    diagnostic.response_offset = offset
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["change_error"]
    assert report["change_comparison"]["output_differences"]
    assert report["restoration_comparison"]["equivalent"]
    assert report["source_unchanged"]


def test_full_point_protects_source_restores_baseline_and_preserves_files(
    diagnostic: SimpleNamespace,
) -> None:
    source_bytes = diagnostic.source.read_bytes()
    report = diagnostic.run()
    assert report["status"] == "passed"
    assert report["source_unchanged"] and report["files_unchanged"]
    assert report["restoration_comparison"]["equivalent"]
    assert report["temperature_response"]["observed_change_C"] > 0
    assert report["writer"] == "ColumnTemperatureSpec.GoalValue"
    assert report["solver_action"] == "Reset_then_Run_in_working_case"
    assert [
        event for event in diagnostic.events if isinstance(event, tuple) and event[0] == "Reset"
    ] == [("Reset", "candidate.hsc")]
    for key in ("goal_value_internal", "goal_C", "active_goal_value_internal", "active_goal_C"):
        assert report["specification_before"][key] == 156.8
        assert report["specification_after"][key] == 156.9
    assert report["eligible_for_optimization"] is False
    assert diagnostic.source.read_bytes() == source_bytes
    assert [x for x in diagnostic.events if isinstance(x, tuple) and x[0] in {"Open", "Close"}] == [
        ("Open", "candidate.hsc"),
        ("Close", "candidate.hsc", False),
        ("Open", "restored.hsc"),
        ("Close", "restored.hsc", False),
    ]
    assert diagnostic.events[-1] == ("uninitialize", threading.get_ident())
    restored = read_snapshot(diagnostic.output / "A_restored.json")
    assert restored.variables == diagnostic.start.variables


@pytest.mark.parametrize(
    "failure",
    ["write", "reset", "reset_interrupt", "run", "interrupt", "close_interrupt", "source_read"],
)
def test_b_failures_still_attempt_restoration_and_preserve_source_evidence(
    diagnostic: SimpleNamespace, failure: str
) -> None:
    if failure == "write":
        diagnostic.set_error = FakeComError
    elif failure in {"reset", "reset_interrupt"}:
        diagnostic.reset_error = FakeComError if failure == "reset" else KeyboardInterrupt
    elif failure in {"run", "interrupt"}:
        diagnostic.run_error = FakeComError if failure == "run" else KeyboardInterrupt
    elif failure == "close_interrupt":
        diagnostic.close_error = KeyboardInterrupt
    else:
        diagnostic.run_error = FakeComError
        diagnostic.source_after_error = True
    report = diagnostic.run()
    assert report["status"] == "failed"
    if failure in {"reset", "reset_interrupt"}:
        assert ("Reset", "candidate.hsc") in diagnostic.events
        assert ("Run", "candidate.hsc") not in diagnostic.events
        assert ("CanSolve", "candidate.hsc", True) not in diagnostic.events
    if failure == "close_interrupt":
        assert ("Open", "restored.hsc") not in diagnostic.events
        assert report["restore_error"]
    else:
        assert ("Open", "restored.hsc") in diagnostic.events
        assert report["restoration_comparison"]["equivalent"]
    if failure != "close_interrupt":
        assert report["change_error"]
    if failure == "source_read":
        assert report["source_protection_error"]
    else:
        assert report["source_unchanged"]
    assert report["files_unchanged"]


def test_source_state_drift_is_never_success(diagnostic: SimpleNamespace) -> None:
    diagnostic.source_drift = True
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["source_unchanged"] is False


def test_same_path_same_state_replacement_source_is_not_the_original(
    diagnostic: SimpleNamespace,
) -> None:
    diagnostic.replace_source_after_restore = True
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["restoration_comparison"]["equivalent"]
    assert report["source_unchanged"] is False
    assert report["source_protection_error"]


def test_close_failure_with_work_still_open_prevents_another_open(
    diagnostic: SimpleNamespace,
) -> None:
    diagnostic.close_keeps_open = True
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["cleanup_errors"] and report["restore_error"]
    assert ("Open", "restored.hsc") not in diagnostic.events
    assert report["source_unchanged"] is False
    assert report["source_protection_error"]


def test_skipped_restoration_never_closes_an_unattempted_same_path_document(
    diagnostic: SimpleNamespace,
) -> None:
    foreign = SimpleNamespace(
        FullName=str(diagnostic.output / "restored.hsc"),
        _oleobj_=object(),
        Close=lambda save: diagnostic.events.append("foreign-close"),
    )
    diagnostic.close_keeps_open = True
    diagnostic.before_close = lambda case: case.owner().items.append(foreign)
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert ("Open", "restored.hsc") not in diagnostic.events
    assert "foreign-close" not in diagnostic.events
    assert report["restore_error"]


def test_source_change_during_open_prevents_any_write(diagnostic: SimpleNamespace) -> None:
    def configure(case: Any) -> None:
        case.owner().items[0].IsDirty = False

    diagnostic.configure_open = configure
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert not any(
        isinstance(event, tuple) and event[0] == "GoalValue" for event in diagnostic.events
    )
    assert report["source_unchanged"] is False


def test_ambiguous_work_document_ownership_never_closes_or_writes(
    diagnostic: SimpleNamespace,
) -> None:
    diagnostic.duplicate_open = True
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert not any(
        isinstance(event, tuple) and event[0] in {"Close", "GoalValue"}
        for event in diagnostic.events
    )
    assert ("Open", "restored.hsc") not in diagnostic.events
    assert report["cleanup_errors"]


@pytest.mark.parametrize(
    "file", ["baseline.hsc", "snapshot.json", "variables.json", "manifest.json"]
)
def test_invalid_baseline_precedes_output_creation_and_com(
    diagnostic: SimpleNamespace, file: str
) -> None:
    (diagnostic.directory / file).write_bytes(b"changed")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        diagnostic.run()
    assert diagnostic.events == []
    assert not diagnostic.output.exists()


@pytest.mark.parametrize("file", ["baseline.hsc", "candidate.hsc", "restored.hsc"])
def test_file_drift_after_execution_is_reported(diagnostic: SimpleNamespace, file: str) -> None:
    path = diagnostic.directory / file if file == "baseline.hsc" else diagnostic.output / file
    diagnostic.after_run = lambda: path.write_bytes(b"changed by external actor")
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["integrity_error"]["phase"] == "verify-file-integrity"


def test_output_directory_is_never_reused(diagnostic: SimpleNamespace) -> None:
    diagnostic.output.mkdir()
    marker = diagnostic.output / "keep.txt"
    marker.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        diagnostic.run()
    assert diagnostic.events == []
    assert marker.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "target", [True, False, "156.9", None, float("nan"), float("inf"), -float("inf"), 10**400]
)
def test_invalid_target_is_rejected_before_files_or_com(
    diagnostic: SimpleNamespace, target: Any
) -> None:
    diagnostic.target = target
    with pytest.raises((TypeError, ValueError)):
        diagnostic.run()
    assert diagnostic.events == []
    assert not diagnostic.output.exists()


@pytest.mark.parametrize("target", [156.8, 156.7, 157])
def test_requested_target_uses_same_protocol_without_a_direction_requirement(
    diagnostic: SimpleNamespace, target: float
) -> None:
    diagnostic.target = target
    diagnostic.stage_response = 0.0
    report = diagnostic.run()
    assert report["status"] == "passed"
    assert report["target_temperature_C"] == target
    assert ("GoalValue", "candidate.hsc", target) in diagnostic.events
    assert ("Reset", "candidate.hsc") in diagnostic.events
    assert ("Run", "candidate.hsc") in diagnostic.events
    assert abs(report["temperature_response"]["target_residual_C"]) <= 0.01
    if target == 156.8:
        assert report["change_comparison"]["output_differences"] == []
        assert report["temperature_response"]["observed_change_C"] == 0.0
    elif target < 156.8:
        assert report["temperature_response"]["observed_change_C"] < 0.0
    assert read_snapshot(diagnostic.output / "baseline_snapshot.json") == diagnostic.start
    assert (diagnostic.output / "variables.json").read_bytes() == (
        diagnostic.directory / "variables.json"
    ).read_bytes()


@pytest.mark.parametrize("phase", ["before_write", "after_run"])
def test_replaced_candidate_is_not_written_or_closed(
    diagnostic: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    def replace_candidate() -> None:
        app = diagnostic.references[0]()
        candidate = app.SimulationCases.items[1]
        replacement = type(candidate)(Path(candidate.FullName))
        replacement.owner = weakref.ref(app.SimulationCases)
        app.SimulationCases.items[1] = replacement
        diagnostic.references.append(weakref.ref(replacement))

    if phase == "before_write":
        original_write = point.write_snapshot

        def replace_after_snapshot(path: Path, snapshot: Any) -> None:
            original_write(path, snapshot)
            if path.name == "A_before.json":
                replace_candidate()

        monkeypatch.setattr(point, "write_snapshot", replace_after_snapshot)
    else:
        diagnostic.after_run = replace_candidate
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["change_error"] and report["cleanup_errors"]
    assert ("Close", "candidate.hsc", False) not in diagnostic.events
    assert ("Open", "restored.hsc") not in diagnostic.events
    if phase == "before_write":
        assert not any(
            isinstance(event, tuple) and event[0] == "GoalValue" for event in diagnostic.events
        )


def test_no_open_attempt_never_closes_any_case(diagnostic: SimpleNamespace) -> None:
    diagnostic.configure_application = lambda app: setattr(
        app.SimulationCases.items[0], "FullName", "other.hsc"
    )
    report = diagnostic.run()
    assert report["status"] == "failed" and report["error"]
    assert not any(
        isinstance(event, tuple) and event[0] in {"Open", "Close"} for event in diagnostic.events
    )


def test_failed_open_can_clean_only_its_unique_new_document(diagnostic: SimpleNamespace) -> None:
    diagnostic.open_error = FakeComError
    report = diagnostic.run()
    assert report["status"] == "failed"
    assert report["change_error"] and report["restore_error"]
    assert ("Close", "candidate.hsc", False) in diagnostic.events
    assert ("Close", "restored.hsc", False) in diagnostic.events
    assert report["source_unchanged"]


def test_failed_initialization_never_uninitializes_or_opens(diagnostic: SimpleNamespace) -> None:
    diagnostic.initialize_error = FakeComError
    with pytest.raises(FakeComError):
        diagnostic.run()
    assert "GetActiveObject" not in diagnostic.events
    assert not any(
        isinstance(event, tuple) and event[0] == "uninitialize" for event in diagnostic.events
    )


@pytest.mark.parametrize("condition", ["paused", "wrong_type", "readonly", "kelvin"])
def test_unqualified_baseline_is_rejected_before_output_and_com(
    diagnostic: SimpleNamespace, condition: str
) -> None:
    path = diagnostic.directory / "snapshot.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    reading = next(
        item for item in snapshot["variables"] if item["variable_id"] == point.VARIABLE_ID
    )
    spec = next(item for item in snapshot["specifications"] if item["name"] == point.SPECIFICATION)
    if condition == "paused":
        snapshot["solver"]["can_solve"] = False
    elif condition == "wrong_type":
        spec["specification_type"] = "ColumnFlowSpec"
    elif condition == "readonly":
        reading["can_modify"] = False
    else:
        reading["internal_value"] += 273.15
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    manifest = json.loads(diagnostic.manifest.read_text(encoding="utf-8"))
    manifest["files"]["snapshot.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    diagnostic.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="qualify"):
        diagnostic.run()
    assert diagnostic.events == []
    assert not diagnostic.output.exists()


def test_catalog_copy_must_match_the_revalidated_frozen_bytes(
    diagnostic: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = diagnostic.directory / "variables.json"
    original_read = point._file_bytes
    temporary_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    temporary_catalog["table_name"] = "temporary-other-table"
    temporary_bytes = json.dumps(temporary_catalog).encode("utf-8")
    calls = 0

    def transient_read(path: Path) -> bytes:
        nonlocal calls
        if path == catalog_path:
            calls += 1
            if calls == 1:
                return temporary_bytes
        return original_read(path)

    monkeypatch.setattr(point, "_file_bytes", transient_read)
    with pytest.raises(ValueError, match="catalog changed"):
        diagnostic.run()
    assert diagnostic.events == []
    assert not diagnostic.output.exists()
