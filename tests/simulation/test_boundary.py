"""Real boundary contracts with small read-only COM substitutes; no HYSYS calls."""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
import weakref
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_snapshot import snapshot  # noqa: F401 - shared immutable observation

from petroleum_rto.simulation import boundary
from petroleum_rto.simulation.hysys import DEFAULT_CATALOG, HysysReadError
from petroleum_rto.simulation.models import OperatingSnapshot, VariableReading


class Collection:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    @property
    def Count(self) -> int:
        return len(self.items)

    @property
    def Names(self) -> tuple[str, ...]:
        return tuple(x.Name for x in self.items)

    def Item(self, key: str | int) -> Any:
        return self.items[key] if type(key) is int else next(x for x in self.items if x.Name == key)

    def Open(self, _: str) -> None:
        raise AssertionError("Never open a case")


class Quantity:
    def __init__(self, code: int, unit: str, value: float) -> None:
        self.UnitConversionType = code
        self.IsKnown = True
        self.unit, self.value = unit, value
        self.requests: list[str] = []

    def GetValue(self, unit: str) -> float:
        assert self.IsKnown, "Do not read an unknown quantity"
        assert unit == self.unit, "Require explicit qualified units"
        self.requests.append(unit)
        return self.value

    @property
    def Value(self) -> float:
        raise AssertionError("Do not use internal values or implicit display units")


class Case:
    def __init__(self, source: Path, flow: Any) -> None:
        self._oleobj_ = object()
        self.FullName = str(source)
        self.Flowsheet = flow

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "CanSolve":
            raise AssertionError("Never change source solver")
        super().__setattr__(name, value)

    def Close(self, *_: Any) -> None:
        raise AssertionError("Never close source")

    def Save(self) -> None:
        raise AssertionError("Never save source")

    def Activate(self) -> None:
        raise AssertionError("Never activate source")


class Application:
    def __init__(self, case: Case) -> None:
        self.SimulationCases = Collection([case])

    @property
    def Visible(self) -> bool:
        raise AssertionError("Never access application visibility")


def _connections(values: tuple[boundary.OperationConnection, ...]) -> Collection:
    return Collection([SimpleNamespace(Name=x.name, TypeName=x.type_name) for x in values])


def _application(source: Path, definition: boundary.BoundaryDefinition) -> Application:
    components = Collection(
        [
            SimpleNamespace(
                Name=f"component-{i}", IsHypothetical=i >= 13, Formula="" if i >= 13 else "H2O   "
            )
            for i in range(44)
        ]
    )
    package = SimpleNamespace(
        Name="Basis-1", PropertyPackageName="Peng-Robinson", Components=components
    )

    def stream(binding: boundary.StreamBinding, material: bool) -> Any:
        item = SimpleNamespace(
            Name=binding.name,
            UpstreamOpers=_connections(binding.upstream),
            DownstreamOpers=_connections(binding.downstream),
        )
        if material:
            item.FluidPackage = package
            item.ComponentMassFractionValue = (0.0,) * 7 + (1.0,) + (0.0,) * 36
            for field, (attr, code, unit) in boundary._MATERIAL_PROPERTIES.items():
                value = (
                    0.4
                    if field == "vapour_fraction_molar"
                    else -1200.0
                    if field in ("mass_enthalpy_kJ_kg", "heat_flow_kJ_h")
                    else 100.0
                )
                setattr(item, attr, Quantity(code, unit, value))
        else:
            item.HeatFlow = Quantity(6, "kJ/h", 1000.0)
        return item

    column = SimpleNamespace(
        Operations=_connections(definition.column_operations),
        Flowsheets=Collection([]),
        EnergyStreams=Collection(
            [stream(x, False) for x in definition.energy if x.scope == "column"]
        ),
    )
    operations = _connections(definition.main_operations)
    operations.Item(definition.column_name).ColumnFlowsheet = column
    flow = SimpleNamespace(
        Operations=operations,
        FluidPackage=package,
        MaterialStreams=Collection([stream(x, True) for x in definition.materials]),
        EnergyStreams=Collection(
            [stream(x, False) for x in definition.energy if x.scope == "main"]
        ),
    )
    return Application(Case(source, flow))


@pytest.fixture
def environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: OperatingSnapshot,  # noqa: F811
) -> SimpleNamespace:
    source = tmp_path / "source.hsc"
    source.write_bytes(b"untouched source")
    definition = boundary.BoundaryDefinition.from_dict(
        json.loads(boundary.DEFAULT_BOUNDARY.read_text())
    )
    core = replace(
        snapshot,
        case_id="mjh_atm",
        source_case_path=str(source),
        source_disk_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        consistent_observation=True,
        variables=tuple(
            VariableReading(
                **{
                    key: item[key]
                    for key in (
                        "variable_id",
                        "role",
                        "row",
                        "object_name",
                        "property_name",
                        "quantity_type",
                        "unit",
                    )
                },
                value=100.0,
                internal_value=100.0,
                state=1 if item["role"] == "mv" else 0,
                can_modify=item["role"] == "mv",
            )
            for item in json.loads(DEFAULT_CATALOG.read_text(encoding="utf-8"))["variables"]
        ),
    )
    app = _application(source, definition)
    state = SimpleNamespace(
        source=source,
        definition=definition,
        core=core,
        app=app,
        events=[],
        core_reads=0,
        on_core=None,
    )

    def read_core(*_: Any) -> OperatingSnapshot:
        state.core_reads += 1
        if state.on_core is not None:
            state.on_core(state.core_reads)
        return cast(OperatingSnapshot, state.core)

    monkeypatch.setattr(boundary, "_read_open_case", read_core)
    client = SimpleNamespace(GetActiveObject=lambda _: state.app)
    pythoncom = SimpleNamespace(
        CoInitialize=lambda: state.events.append(("initialize", threading.get_ident())),
        CoUninitialize=lambda: state.events.append(("uninitialize", threading.get_ident())),
    )
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: pythoncom if name == "pythoncom" else client,
    )
    return state


def _read(env: SimpleNamespace) -> boundary.BoundarySnapshot:
    return boundary.read_current_boundary(env.source)


def _material(env: SimpleNamespace) -> Any:
    return env.app.SimulationCases.Item(0).Flowsheet.MaterialStreams.Item("Crude_Oil")


def _column(env: SimpleNamespace) -> Any:
    return env.app.SimulationCases.Item(0).Flowsheet.Operations.Item("C-1102").ColumnFlowsheet


def test_complete_units_composition_bridge_and_source_protection(
    environment: SimpleNamespace,
) -> None:
    result = _read(environment)
    assert result.consistent_observation
    assert len(result.first.components) == 44
    assert [len(result.first.materials), len(result.first.energy)] == [9, 11]
    assert sum(x.direction == "in" for x in result.first.materials) == 4
    assert sum(x.direction == "in" for x in result.first.energy) == 8
    assert sum(x.name == "TopStagePA_Q-Cooler_1" for x in result.first.energy) == 1
    assert result.first.components[0].formula == "H2O   "
    assert result.first.components[13].is_hypothetical and result.first.components[13].formula == ""
    for attr, _, unit in boundary._MATERIAL_PROPERTIES.values():
        assert getattr(_material(environment), attr).requests == [unit, unit]
    assert environment.source.read_bytes() == b"untouched source"
    assert environment.core_reads == 2
    assert environment.events == [
        ("initialize", threading.get_ident()),
        ("uninitialize", threading.get_ident()),
    ]
    with pytest.raises(FrozenInstanceError):
        result.first.materials[0].mass_flow_kg_h = 5  # type: ignore[misc]


def test_roundtrip_saved_definition_independent_of_live_files(
    environment: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _read(environment)
    target = tmp_path / "boundary.json"
    boundary.write_boundary_snapshot(target, result)
    monkeypatch.setattr(boundary, "DEFAULT_BOUNDARY", tmp_path / "missing.json")
    environment.source.unlink()
    assert boundary.read_boundary_snapshot(target) == result
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        boundary.write_boundary_snapshot(target, result)
    assert target.read_bytes() == before


@pytest.mark.parametrize("quantity", list(boundary._MATERIAL_PROPERTIES.values()))
def test_quantity_drift_rejected(
    environment: SimpleNamespace, quantity: tuple[str, int, str]
) -> None:
    attr, code, _ = quantity
    getattr(_material(environment), attr).UnitConversionType = code + 100
    with pytest.raises(HysysReadError, match="quantity type"):
        _read(environment)
    assert environment.events[-1][0] == "uninitialize"


@pytest.mark.parametrize("value", [True, None, float("nan"), float("inf")])
def test_invalid_scalar_rejected(environment: SimpleNamespace, value: Any) -> None:
    _material(environment).Temperature.value = value
    with pytest.raises(HysysReadError):
        _read(environment)


def test_unknown_scalar_not_read_and_large_negative_enthalpy_kept(
    environment: SimpleNamespace,
) -> None:
    quantity = _material(environment).MassEnthalpy
    quantity.value = -1e30
    assert _read(environment).first.materials[0].mass_enthalpy_kJ_kg == -1e30
    quantity.IsKnown = False
    with pytest.raises(HysysReadError, match="Undefined"):
        _read(environment)
    assert len(quantity.requests) == 2


@pytest.mark.parametrize(
    "change", ["length", "negative", "above_one", "sum", "bool", "nan", "names", "duplicate_names"]
)
def test_composition_boundary(environment: SimpleNamespace, change: str) -> None:
    item = _material(environment)
    values = list(item.ComponentMassFractionValue)
    if change == "length":
        values.pop()
    elif change == "negative":
        values[0] = -1e-15
    elif change == "above_one":
        values[7] = 1.000001
    elif change == "sum":
        values[7] = 0.9
    elif change == "bool":
        values[0] = False
    elif change == "nan":
        values[0] = float("nan")
    elif change == "names":
        item.FluidPackage = SimpleNamespace(
            Name="Basis-1",
            PropertyPackageName="Peng-Robinson",
            Components=Collection(list(reversed(item.FluidPackage.Components.items))),
        )
    else:
        item.FluidPackage.Components.items[0].Name = item.FluidPackage.Components.items[1].Name
    item.ComponentMassFractionValue = tuple(values)
    with pytest.raises(HysysReadError):
        _read(environment)


@pytest.mark.parametrize(
    "change",
    [
        "extra_material",
        "missing_material",
        "extra_main_energy",
        "extra_column_energy",
        "connection",
        "main_operation",
        "column_operation",
        "child_flowsheet",
        "bridge",
    ],
)
def test_complete_topology_and_bridge(environment: SimpleNamespace, change: str) -> None:
    flow = environment.app.SimulationCases.Item(0).Flowsheet
    column = _column(environment)
    if change == "extra_material":
        flow.MaterialStreams.items.append(SimpleNamespace(Name="new-outlet"))
    elif change == "missing_material":
        flow.MaterialStreams.items.pop()
    elif change == "extra_main_energy":
        flow.EnergyStreams.items.append(SimpleNamespace(Name="new-duty"))
    elif change == "extra_column_energy":
        column.EnergyStreams.items.append(SimpleNamespace(Name="new-duty"))
    elif change == "connection":
        _material(environment).DownstreamOpers.items[0].Name = "different-heater"
    elif change == "main_operation":
        flow.Operations.items.append(SimpleNamespace(Name="new-child", TypeName="subflowsheetop"))
    elif change == "column_operation":
        column.Operations.items[0].TypeName = "different"
    elif change == "child_flowsheet":
        column.Flowsheets.items.append(SimpleNamespace(Name="nested"))
    else:
        flow.EnergyStreams.Item("TopStagePA_Q-Cooler_1").HeatFlow.value += 1
    with pytest.raises(HysysReadError):
        _read(environment)


def test_boundary_sample_change_is_saved_as_inconsistent(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = boundary._sample_boundary
    calls = 0

    def changed(*args: Any) -> boundary.BoundaryReadings:
        nonlocal calls
        calls += 1
        if calls == 2:
            _material(environment).MassEnthalpy.value += 1
        return sample(*args)

    monkeypatch.setattr(boundary, "_sample_boundary", changed)
    result = _read(environment)
    assert not result.consistent_observation
    assert boundary.BoundarySnapshot.from_dict(result.to_dict()) == result
    raw = result.to_dict()
    raw["consistent_observation"] = True
    with pytest.raises(ValueError, match="consistency"):
        boundary.BoundarySnapshot.from_dict(raw)


@pytest.mark.parametrize("change", ["core", "disk", "identity", "path", "extra_case"])
def test_source_drift_rejected(environment: SimpleNamespace, change: str) -> None:
    def mutate(count: int) -> None:
        if count != 2:
            return
        if change == "core":
            environment.core = replace(
                environment.core, memory_is_dirty=not environment.core.memory_is_dirty
            )
        elif change == "disk":
            environment.source.write_bytes(b"external change")
        elif change == "identity":
            environment.app.SimulationCases.items[0] = _application(
                environment.source, environment.definition
            ).SimulationCases.Item(0)
        elif change == "path":
            environment.app.SimulationCases.Item(0).FullName += ".other"
        else:
            environment.app.SimulationCases.items.append(
                _application(
                    environment.source.with_name("other.hsc"), environment.definition
                ).SimulationCases.Item(0)
            )

    environment.on_core = mutate
    with pytest.raises(HysysReadError):
        _read(environment)
    assert environment.events[-1][0] == "uninitialize"


@pytest.mark.parametrize("change", ["missing", "wrong_path", "duplicate"])
def test_case_matching_never_opens(environment: SimpleNamespace, change: str) -> None:
    cases = environment.app.SimulationCases
    if change == "missing":
        cases.items.clear()
    elif change == "wrong_path":
        cases.items[0].FullName += ".other"
    else:
        cases.items.append(cases.items[0])
    with pytest.raises(HysysReadError):
        _read(environment)
    assert environment.core_reads == 0


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_com_references_and_exception_frames_release_before_uninitialize(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    refs: list[Any] = []
    threads = []
    sample = boundary._sample_boundary

    def get_active(_: str) -> Application:
        app = _application(environment.source, environment.definition)
        refs.extend([weakref.ref(app), weakref.ref(app.SimulationCases.Item(0))])
        return app

    def fail_sample(*args: Any) -> boundary.BoundaryReadings:
        if failure:
            error = failure("COM boundary failure")
            error.hresult = -2147352567
            error.excepinfo = (0, "HYSYS", "detail", None, 0, -2147024891)
            raise error
        return sample(*args)

    def uninitialize() -> None:
        assert refs and all(ref() is None for ref in refs)
        threads.append(threading.get_ident())

    pythoncom = SimpleNamespace(
        CoInitialize=lambda: threads.append(threading.get_ident()), CoUninitialize=uninitialize
    )
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            pythoncom if name == "pythoncom" else SimpleNamespace(GetActiveObject=get_active)
        ),
    )
    monkeypatch.setattr(boundary, "_sample_boundary", fail_sample)
    if failure:
        with pytest.raises(HysysReadError) as captured:
            _read(environment)
        assert captured.value.hresult == -2147352567 and captured.value.scode == -2147024891
        assert captured.value.phase == "read-boundary"
        assert captured.value.code == (
            "interrupted" if failure is KeyboardInterrupt else "boundary-read-failed"
        )
    else:
        assert _read(environment).consistent_observation
    assert threads == [threading.get_ident()] * 2


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "missing",
        "declaration",
        "units",
        "hash",
        "material_name",
        "energy_direction",
        "duplicate_component",
        "component_bool",
        "fraction_bool",
        "missing_energy",
        "source_hash",
        "source_case_id",
        "core_unstable",
        "definition_extra",
    ],
)
def test_strict_snapshot_json(environment: SimpleNamespace, change: str) -> None:
    raw = _read(environment).to_dict()
    if change == "unknown":
        raw["extra"] = 0
    elif change == "missing":
        del raw["first"]
    elif change == "declaration":
        raw["eligible_for_optimization"] = True
    elif change == "units":
        raw["units"]["heat_flow_kJ_h"] = "kW"
    elif change == "hash":
        raw["definition_sha256"] = "0" * 64
    elif change == "material_name":
        raw["first"]["materials"][0]["name"] = "unknown"
    elif change == "energy_direction":
        raw["first"]["energy"][0]["direction"] = "out"
    elif change == "duplicate_component":
        raw["first"]["components"][1]["name"] = raw["first"]["components"][0]["name"]
    elif change == "component_bool":
        raw["first"]["components"][0]["is_hypothetical"] = 0
    elif change == "fraction_bool":
        raw["first"]["materials"][0]["component_mass_fractions"][0] = False
    elif change == "missing_energy":
        raw["first"]["energy"].pop()
    elif change == "source_hash":
        raw["core_after"]["source_disk_sha256"] = "0" * 64
    elif change == "source_case_id":
        raw["core_before"]["case_id"] = "different"
    elif change == "core_unstable":
        raw["core_before"]["consistent_observation"] = False
    else:
        raw["definition"]["unconsumed_field"] = 42
    with pytest.raises((ValueError, TypeError)):
        boundary.BoundarySnapshot.from_dict(raw)


@pytest.mark.parametrize(
    "payload", ['{"schema_id":1,"schema_id":2}', '{"value":NaN}', '{"value":Infinity}']
)
def test_json_duplicates_and_constants_rejected(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        boundary.read_boundary_snapshot(path)


@pytest.mark.parametrize(
    "stream,field",
    [
        (name, "mass_flow_kg_h")
        for name in (
            "Crude_Oil",
            "Water1",
            "Water2",
            "Water3",
            "Risedue",
            "Naptha",
            "Kerosene",
            "Diesel",
            "AGO",
        )
    ]
    + [("Crude_Oil", "temperature_C"), ("Crude_Oil", "pressure_kPa")],
)
def test_two_identical_boundary_samples_cannot_contradict_core(
    environment: SimpleNamespace, stream: str, field: str
) -> None:
    raw = _read(environment).to_dict()
    for key in ("first", "second"):
        next(x for x in raw[key]["materials"] if x["name"] == stream)[field] += 1.0
    with pytest.raises(ValueError, match="Core and boundary"):
        boundary.BoundarySnapshot.from_dict(raw)


def test_product_tbp_is_not_a_duplicate_of_outlet_temperature(environment: SimpleNamespace) -> None:
    material_names = {x.name for x in environment.definition.materials if x.direction != "internal"}
    selected = next(
        x
        for x in environment.core.variables
        if x.role == "cv" and x.object_name not in material_names
    )
    environment.core = replace(
        environment.core,
        variables=tuple(
            replace(
                x,
                object_name="Naptha",
                property_name="TBP 95%",
                quantity_type="temperature",
                unit="C",
                value=999.0,
            )
            if x == selected
            else x
            for x in environment.core.variables
        ),
    )
    assert _read(environment).consistent_observation


def test_config_change_during_observation_rejected(
    environment: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(environment.definition.to_dict()), encoding="utf-8")
    monkeypatch.setattr(boundary, "DEFAULT_BOUNDARY", path)

    def mutate(count: int) -> None:
        if count == 2:
            path.write_text("{}", encoding="utf-8")

    environment.on_core = mutate
    with pytest.raises(HysysReadError, match="Configuration changed"):
        _read(environment)


@pytest.mark.parametrize(
    "change",
    ["unknown", "missing_stream", "duplicate", "role", "operation", "missing_operation", "bridge"],
)
def test_invalid_config_before_com(
    environment: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    raw = environment.definition.to_dict()
    if change == "unknown":
        raw["future_option"] = True
    elif change == "missing_stream":
        raw["materials"].pop()
    elif change == "duplicate":
        raw["materials"][1] = raw["materials"][0]
    elif change == "role":
        raw["materials"][0]["direction"] = "out"
    elif change == "operation":
        raw["materials"][0]["downstream"][0]["name"] = "unknown"
    elif change == "missing_operation":
        raw["column_operations"].pop()
    else:
        next(x for x in raw["energy"] if x["direction"] == "bridge")["scope"] = "column"
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(boundary, "DEFAULT_BOUNDARY", path)
    with pytest.raises((ValueError, TypeError)):
        _read(environment)
    assert not environment.events
