"""Fake-COM boundary checks for the production HYSYS observation reader."""

from __future__ import annotations

import hashlib
import json
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from petroleum_rto.simulation import hysys

_QUANTITIES = {
    "temperature": (1, "C"),
    "pressure": (2, "kPa"),
    "mass_flow": (4, "kg/h"),
    "heat_flow": (6, "kJ/h"),
    "temperature_difference": (24, "C"),
    "pressure_difference": (45, "kPa"),
    "percent": (64, "%"),
    "ratio": (65, ""),
}


class FakeQuantity:
    def __init__(self, quantity: str, value: float, *, known: bool = True) -> None:
        self.UnitConversionType, self.unit = _QUANTITIES[quantity]
        self.value = value
        self.Value = 7.0
        self.IsKnown = known
        self.State = 1
        self.CanModify = True
        self.units_read: list[str] = []

    def GetValue(self, unit: str) -> float:
        assert self.IsKnown, "must not call GetValue on an unknown variable"
        assert unit == self.unit, "must request the declared explicit unit"
        self.units_read.append(unit)
        return self.value


class FakeCell:
    def __init__(self, binding: dict[str, Any]) -> None:
        self.AttachedObjectName = binding["object_name"]
        self.VariableName = binding["property_name"]
        value = 0.5 if binding["quantity_type"] == "ratio" else 42.0
        self.ImportedVariable = FakeQuantity(binding["quantity_type"], value)
        self.ImportedVariable.State = 1 if binding["role"] == "mv" else 0
        self.ImportedVariable.CanModify = binding["role"] == "mv"

    @property
    def CellValue(self) -> float:
        raise AssertionError("use ImportedVariable.GetValue, never spreadsheet coefficients")


class FakeTable:
    def __init__(self, bindings: list[dict[str, Any]]) -> None:
        self.cells = {f"C{item['row']}": FakeCell(item) for item in bindings}
        self.sample_count = 0
        self.change_second_sample = False
        self.fail_read: type[BaseException] | None = None

    def Cell(self, address: str) -> FakeCell:
        if self.fail_read is not None:
            raise self.fail_read("read failure")
        if address == "C2":
            self.sample_count += 1
        if address == "C28" and self.sample_count == 2 and self.change_second_sample:
            self.cells[address].ImportedVariable.value = 43.0
        return self.cells[address]


class FakeCollection:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    @property
    def Count(self) -> int:
        return len(self.items)

    def Item(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self.items[key]
        return next(item for item in self.items if item.Name == key)

    def Open(self, path: str) -> None:
        raise AssertionError("read-only attachment must never Open a case")


class FakeSpecification:
    def __init__(self, name: str, *, active: bool) -> None:
        self.Name = name
        self.IsActive = active
        self.IsUsedAsEstimate = active
        self.Goal = FakeQuantity("mass_flow", 42.0, known=active)
        self.Current = FakeQuantity("mass_flow", 42.0)
        info = SimpleNamespace(GetDocumentation=lambda member: ("ColumnFlowSpec",))
        self._oleobj_ = SimpleNamespace(GetTypeInfo=lambda: info)


@dataclass(frozen=True)
class FakeSolver:
    CanSolve: bool = True
    IsSolving: bool = False


class FakeCase:
    def __init__(self, source: Path, bindings: list[dict[str, Any]]) -> None:
        self.FullName = str(source)
        self.IsDirty = True
        self.IsValid = True
        self.Solver = FakeSolver()
        self.table = FakeTable(bindings)
        specs = [
            FakeSpecification("Flow spec", active=True),
            FakeSpecification("Monitor", active=False),
        ]
        self.cfs = SimpleNamespace(
            CfsConverged=True,
            DegreesOfFreedom=0,
            Specifications=FakeCollection(specs),
            ActiveSpecifications=FakeCollection([specs[0]]),
            ColumnStages=FakeCollection(
                [
                    SimpleNamespace(
                        Name=f"stage-{index}",
                        SeparationStage=SimpleNamespace(
                            Pressure=FakeQuantity("pressure", 150.0),
                            Temperature=FakeQuantity("temperature", 100.0),
                            MassLiquidFlow=FakeQuantity("mass_flow", 2000.0),
                            MassVapourFlow=FakeQuantity("mass_flow", 3000.0),
                        ),
                    )
                    for index in range(73)
                ]
            ),
        )
        table = SimpleNamespace(Name="Table")
        column = SimpleNamespace(Name="C-1102", ColumnFlowsheet=self.cfs)
        table.Cell = self.table.Cell
        self.Flowsheet = SimpleNamespace(Operations=FakeCollection([table, column]))

    def Activate(self) -> None:
        raise AssertionError("must not activate the user's case")

    def Save(self) -> None:
        raise AssertionError("must not save the user's case")

    def Close(self) -> None:
        raise AssertionError("must not close the user's case")


class FakeApplication:
    def __init__(self, case: FakeCase) -> None:
        self.Version = "12.0"
        self.SimulationCases = FakeCollection([case])

    @property
    def Visible(self) -> bool:
        raise AssertionError("must not inspect or change application visibility")

    def Quit(self) -> None:
        raise AssertionError("must not quit the user's application")


def _catalog_document() -> dict[str, Any]:
    rows = [*range(2, 26), *range(28, 64)]
    quantities = {
        3: "temperature",
        4: "temperature_difference",
        5: "pressure",
        6: "pressure_difference",
        7: "percent",
        22: "ratio",
        28: "heat_flow",
    }
    return {
        "schema_version": "1.0.0",
        "case_id": "synthetic-case",
        "table_name": "Table",
        "column_name": "C-1102",
        "variables": [
            {
                "variable_id": f"variable-{row}",
                "row": row,
                "role": "mv" if row < 26 else "cv",
                "object_name": f"equipment-{row}",
                "property_name": f"property-{row}",
                "quantity_type": quantities.get(row, "mass_flow"),
                "unit": _QUANTITIES[quantities.get(row, "mass_flow")][1],
                "column_specification": "Flow spec" if row == 2 else None,
            }
            for row in rows
        ],
    }


@pytest.fixture
def reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    source = tmp_path / "模型.hsc"
    source.write_bytes(b"synthetic disk case")
    catalog = tmp_path / "variables.json"
    document = _catalog_document()
    catalog.write_text(json.dumps(document), encoding="utf-8")
    state = SimpleNamespace(
        source=source,
        catalog=catalog,
        document=document,
        configure=lambda case, app: None,
        events=[],
        references=[],
        init_error=None,
    )

    def active_application(progid: str) -> FakeApplication:
        assert progid == "HYSYS.Application"
        state.events.append("GetActiveObject")
        case = FakeCase(source, document["variables"])
        app = FakeApplication(case)
        state.configure(case, app)
        state.references.extend(
            weakref.ref(item)
            for item in (case, app, case.table, case.table.cells["C2"].ImportedVariable)
            if item is not None
        )
        return app

    def initialize() -> None:
        state.events.append(("initialize", threading.get_ident()))
        if state.init_error:
            raise state.init_error

    def uninitialize() -> None:
        assert all(reference() is None for reference in state.references), (
            "COM objects still retained"
        )
        state.events.append(("uninitialize", threading.get_ident()))

    def import_module(name: str) -> Any:
        state.events.append(name)
        if name == "pythoncom":
            return SimpleNamespace(CoInitialize=initialize, CoUninitialize=uninitialize)
        assert name == "win32com.client"
        return SimpleNamespace(GetActiveObject=active_application)

    monkeypatch.setattr(hysys, "importlib", SimpleNamespace(import_module=import_module))
    state.read = lambda: hysys.read_current_snapshot(source, catalog)
    return state


def test_qualified_values_use_explicit_units_and_preserve_raw_internal_values(
    reader: SimpleNamespace,
) -> None:
    before = reader.source.read_bytes()
    snapshot = reader.read()
    assert snapshot.consistent_observation
    assert snapshot.source_disk_sha256 == hashlib.sha256(before).hexdigest()
    assert reader.source.read_bytes() == before
    assert len(snapshot.variables) == 60 and len(snapshot.stages) == 73
    assert snapshot.variables[0].value == 42.0
    assert snapshot.variables[0].internal_value == 7.0
    by_row = {item.row: item for item in snapshot.variables}
    assert by_row[22].unit == "" and by_row[22].value == 0.5
    assert by_row[28].quantity_type == "heat_flow" and by_row[28].unit == "kJ/h"
    assert snapshot.stages[0].liquid_kg_h == 2000.0
    assert snapshot.stages[0].vapor_kg_h == 3000.0
    assert snapshot.specifications[1].goal is None
    assert snapshot.specifications[1].current == 42.0
    assert reader.events[2] == ("initialize", threading.get_ident())
    assert reader.events[-1] == ("uninitialize", threading.get_ident())


@pytest.mark.parametrize(
    "field", ["AttachedObjectName", "VariableName", "ImportedVariable", "quantity"]
)
def test_binding_and_quantity_drift_are_rejected(reader: SimpleNamespace, field: str) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        cell = case.table.cells["C2"]
        if field == "quantity":
            cell.ImportedVariable.UnitConversionType = 1
        else:
            setattr(cell, field, None if field == "ImportedVariable" else "changed-binding")

    reader.configure = configure
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "binding-drift"


@pytest.mark.parametrize("field", ["active", "quantity", "value"])
def test_linked_column_specification_drift_is_rejected(reader: SimpleNamespace, field: str) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        spec = case.cfs.Specifications.Item("Flow spec")
        if field == "active":
            spec.IsActive = False
        elif field == "quantity":
            spec.Goal.UnitConversionType = 2
        else:
            spec.Goal.value = 100.0

    reader.configure = configure
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "specification-drift"


def test_current_and_active_specification_lists_must_agree(reader: SimpleNamespace) -> None:
    reader.configure = lambda case, app: setattr(
        case.cfs, "ActiveSpecifications", FakeCollection([])
    )
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "alternate-specifications"


def test_specification_current_quantity_must_match_goal_even_with_the_same_unit(
    reader: SimpleNamespace,
) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        spec = case.cfs.Specifications.Item("Monitor")
        spec.Goal = FakeQuantity("temperature", 42.0)
        spec.Current = FakeQuantity("temperature_difference", 42.0)
        assert spec.Goal.unit == spec.Current.unit == "C"

    reader.configure = configure
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "specification-drift"


@pytest.mark.parametrize("match_count", [0, 2])
def test_case_mismatch_never_opens_a_disk_case(reader: SimpleNamespace, match_count: int) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        app.SimulationCases.items = [case] * match_count

    reader.configure = configure
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "case-match"
    assert reader.events.count("GetActiveObject") == 1


def test_a_different_open_case_is_not_used(reader: SimpleNamespace) -> None:
    reader.configure = lambda case, app: setattr(
        case, "FullName", str(reader.source.with_name("other.hsc"))
    )
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "case-match"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("IsKnown", False, "unknown-variable"),
        ("value", float("nan"), "invalid-value"),
        ("value", float("inf"), "invalid-value"),
        ("value", -1e30, "invalid-value"),
        ("value", True, "invalid-value"),
        ("Value", -1e30, "invalid-value"),
        ("UnitConversionType", 999, "unsupported-quantity"),
    ],
)
def test_unknown_undefined_and_invalid_variable_values_are_rejected(
    reader: SimpleNamespace, field: str, value: Any, code: str
) -> None:
    reader.configure = lambda case, app: setattr(
        case.table.cells["C28"].ImportedVariable, field, value
    )
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == code


def test_missing_stage_is_rejected(reader: SimpleNamespace) -> None:
    reader.configure = lambda case, app: case.cfs.ColumnStages.items.pop()
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "stage-drift"


def test_two_different_complete_samples_are_retained_as_unstable(reader: SimpleNamespace) -> None:
    reader.configure = lambda case, app: setattr(case.table, "change_second_sample", True)
    snapshot = reader.read()
    assert not snapshot.consistent_observation
    assert snapshot.to_dict()["eligible_for_optimization"] is False


@pytest.mark.parametrize("condition", ["solving", "invalid", "not_converged", "nonzero_dof"])
def test_ineligible_solver_conditions_are_observations_not_process_failures(
    reader: SimpleNamespace, condition: str
) -> None:
    def configure(case: FakeCase, app: FakeApplication) -> None:
        if condition == "solving":
            case.Solver = FakeSolver(IsSolving=True)
        elif condition == "invalid":
            case.IsValid = False
        elif condition == "not_converged":
            case.cfs.CfsConverged = False
        else:
            case.cfs.DegreesOfFreedom = -1

    reader.configure = configure
    assert reader.read().consistent_observation is False


class FakeComError(Exception):
    hresult = -2147352567
    excepinfo = (0, "HYSYS", "read failed", None, 0, -2147024891)


@pytest.mark.parametrize("error_type", [FakeComError, KeyboardInterrupt])
def test_com_error_or_interruption_releases_references_before_uninitialize(
    reader: SimpleNamespace, error_type: type[BaseException]
) -> None:
    # Create a fresh exception at the call boundary, as COM does.
    reader.configure = lambda case, app: setattr(case.table, "fail_read", error_type)
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    error = failure.value
    assert error.phase == "read-open-case"
    assert error.__context__ is None
    if error_type is FakeComError:
        assert error.code == "com-read-failed"
        assert error.hresult == FakeComError.hresult
        assert error.scode == -2147024891
    else:
        assert error.code == "interrupted"
    assert reader.events[-1] == ("uninitialize", threading.get_ident())


def test_failed_coinitialize_is_not_uninitialized(reader: SimpleNamespace) -> None:
    reader.init_error = FakeComError("initialization failed")
    with pytest.raises(FakeComError):
        reader.read()
    assert not any(
        isinstance(event, tuple) and event[0] == "uninitialize" for event in reader.events
    )
    assert "GetActiveObject" not in reader.events


def test_source_change_during_read_invalidates_the_snapshot(reader: SimpleNamespace) -> None:
    reader.configure = lambda case, app: reader.source.write_bytes(b"externally changed disk case")
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "source-file-changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "unknown",
        "missing",
        "duplicate_id",
        "duplicate_row",
        "duplicate_binding",
        "row_bool",
        "unit",
        "quantity",
        "incomplete",
    ],
)
def test_invalid_catalog_is_rejected_before_com(reader: SimpleNamespace, mutation: str) -> None:
    document = reader.document
    first, second = document["variables"][:2]
    if mutation == "schema":
        document["schema_version"] = "2.0.0"
    elif mutation == "unknown":
        first["algorithm"] = "untrusted"
    elif mutation == "missing":
        first.pop("column_specification")
    elif mutation == "duplicate_id":
        second["variable_id"] = first["variable_id"]
    elif mutation == "duplicate_row":
        second["row"] = first["row"]
    elif mutation == "duplicate_binding":
        second["object_name"], second["property_name"] = (
            first["object_name"],
            first["property_name"],
        )
    elif mutation == "row_bool":
        first["row"] = True
    elif mutation == "unit":
        first["unit"] = "kg/s"
    elif mutation == "quantity":
        first["quantity_type"] = "unknown"
    else:
        document["variables"].pop()
    reader.catalog.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(hysys.HysysReadError):
        reader.read()
    assert reader.events == []


@pytest.mark.parametrize("payload", ['{"case_id":"first","case_id":"second"}', '{"row":NaN}'])
def test_duplicate_json_keys_and_nonfinite_catalog_constants_precede_com(
    reader: SimpleNamespace, payload: str
) -> None:
    reader.catalog.write_text(payload, encoding="utf-8")
    with pytest.raises(hysys.HysysReadError) as failure:
        reader.read()
    assert failure.value.code == "invalid-catalog"
    assert reader.events == []
