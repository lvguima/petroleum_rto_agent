"""Read-only mjh_atm boundary observations, with self-contained strict evidence.

Component arrays follow FluidPackage.Components.Names. VapourFraction is molar
(HYSYS V12 Key_HYSYS_Objects and VaporFractionFlashCalcs help). Enthalpy flow
and connected energy streams remain signed observations, not optimization metrics.
"""

from __future__ import annotations

import importlib
import json
import math
import traceback
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .baseline import _file_bytes, _json, _plain_directory, _sha256, _stable
from .hysys import (
    DEFAULT_CATALOG,
    HysysReadError,
    VariableCatalog,
    _read_open_case,
    load_catalog,
)
from .hysys import (
    _boolean as _com_boolean,
)
from .models import OperatingSnapshot, _array, _boolean, _number, _record, _text
from .recovery import _documents, _path, _same_documents

DEFAULT_BOUNDARY = DEFAULT_CATALOG.with_name("mjh_atm_boundary.json")
_DEFINITION = {"schema_id": "hysys-boundary-definition", "schema_version": "1.0.0"}
_DECLARATIONS = {
    "schema_id": "hysys-boundary-snapshot",
    "schema_version": "1.0.0",
    "source_kind": "existing_case_memory",
    "memory_state_bound_to_disk_hash": False,
    "atomic_snapshot_proven": False,
    "eligible_for_optimization": False,
}
# Codes are qualified against this installation; field names fix persisted units.
_MATERIAL_PROPERTIES = {
    "temperature_C": ("Temperature", 1, "C"),
    "pressure_kPa": ("Pressure", 2, "kPa"),
    "mass_flow_kg_h": ("MassFlow", 4, "kg/h"),
    "mass_enthalpy_kJ_kg": ("MassEnthalpy", 30, "kJ/kg"),
    "heat_flow_kJ_h": ("HeatFlow", 6, "kJ/h"),
    "vapour_fraction_molar": ("VapourFraction", 0, ""),
}
_UNITS = {name: item[2] for name, item in _MATERIAL_PROPERTIES.items()}
_UNITS["component_mass_fractions"] = "kg/kg"


def _tuple(value: Any, kind: type[Any], label: str) -> None:
    if type(value) is not tuple or any(type(item) is not kind for item in value):
        raise TypeError(f"{label} must be a tuple of {kind.__name__}")


def _unique(values: tuple[Any, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {label}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class OperationConnection:
    name: str
    type_name: str

    def __post_init__(self) -> None:
        _text(self.name, "operation name")
        _text(self.type_name, "operation type")

    @classmethod
    def from_dict(cls, value: Any) -> OperationConnection:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class StreamBinding:
    scope: str
    name: str
    direction: str
    upstream: tuple[OperationConnection, ...]
    downstream: tuple[OperationConnection, ...]

    def __post_init__(self) -> None:
        _text(self.name, "stream name")
        if type(self.scope) is not str or self.scope not in ("main", "column"):
            raise ValueError("Unknown flowsheet scope")
        if type(self.direction) is not str or self.direction not in (
            "in",
            "out",
            "internal",
            "bridge",
        ):
            raise ValueError("Unknown boundary direction")
        for side in (self.upstream, self.downstream):
            _tuple(side, OperationConnection, "connections")
            if len(side) > 1:
                raise ValueError("This boundary requires at most one connection on each side")

    @classmethod
    def from_dict(cls, value: Any) -> StreamBinding:
        raw = _record(value, cls)
        for key in ("upstream", "downstream"):
            raw[key] = tuple(OperationConnection.from_dict(x) for x in _array(raw[key], key))
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class BoundaryDefinition:
    case_id: str
    column_name: str
    materials: tuple[StreamBinding, ...]
    energy: tuple[StreamBinding, ...]
    main_operations: tuple[OperationConnection, ...]
    column_operations: tuple[OperationConnection, ...]

    def __post_init__(self) -> None:
        if self.case_id != "mjh_atm" or self.column_name != "C-1102":
            raise ValueError("Only the qualified mjh_atm column is supported")
        _tuple(self.materials, StreamBinding, "material bindings")
        _tuple(self.energy, StreamBinding, "energy bindings")
        for key, count in (("main_operations", 10), ("column_operations", 8)):
            operations = getattr(self, key)
            _tuple(operations, OperationConnection, key)
            if len(operations) != count:
                raise ValueError(f"Expected {count} {key}")
            _unique(tuple(x.name for x in operations), key)
        for binding in (*self.materials, *self.energy):
            operations = self.main_operations if binding.scope == "main" else self.column_operations
            if any(
                x.type_name != "feederblock" and x not in operations
                for x in (*binding.upstream, *binding.downstream)
            ):
                raise ValueError("Stream connection is absent from its operation inventory")
        for bindings in (self.materials, self.energy):
            _unique(tuple((item.scope, item.name) for item in bindings), "stream binding")
        if len(self.materials) != 18:
            raise ValueError("The full material topology requires 18 streams")
        for item in self.materials:
            if item.scope != "main" or len(item.upstream) != 1 or len(item.downstream) != 1:
                raise ValueError("Material connections differ from the supported main flowsheet")
            incoming = item.upstream[0].type_name == "feederblock"
            outgoing = item.downstream[0].type_name == "feederblock"
            direction = "in" if incoming else "out" if outgoing else "internal"
            if (incoming and outgoing) or item.direction != direction:
                raise ValueError("Material direction contradicts its feederblock connections")
        if [sum(x.direction == d for x in self.materials) for d in ("in", "out", "internal")] != [
            4,
            5,
            9,
        ]:
            raise ValueError("Expected four material inlets and five outlets")
        if len(self.energy) != 12 or sum(x.scope == "main" for x in self.energy) != 8:
            raise ValueError("Energy inventory requires eight main and four column streams")
        for item in self.energy:
            if item.direction == "in":
                valid = not item.upstream and len(item.downstream) == 1
            else:
                valid = (
                    item.direction in ("out", "bridge")
                    and len(item.upstream) == 1
                    and not item.downstream
                )
            if not valid:
                raise ValueError("Energy direction contradicts its operation connections")
        if [sum(x.direction == d for x in self.energy) for d in ("in", "out", "bridge")] != [
            8,
            3,
            1,
        ]:
            raise ValueError("Expected eleven physical energy flows and one bridge")
        bridge = next(x for x in self.energy if x.direction == "bridge")
        if (
            bridge.scope != "main"
            or bridge.upstream != (OperationConnection(self.column_name, "columnop"),)
            or not any(
                x.scope == "column" and x.name == bridge.name and x.direction == "out"
                for x in self.energy
            )
        ):
            raise ValueError("The bridge must identify the column's exported cooler duty")

    def to_dict(self) -> dict[str, Any]:
        # JSON conversion normalizes all immutable tuple collections to arrays.
        return {**_DEFINITION, **json.loads(_canonical(asdict(self)))}

    @classmethod
    def from_dict(cls, value: Any) -> BoundaryDefinition:
        raw = _record(value, cls, declarations=_DEFINITION)
        for key in ("materials", "energy"):
            raw[key] = tuple(StreamBinding.from_dict(x) for x in _array(raw[key], key))
        for key in ("main_operations", "column_operations"):
            raw[key] = tuple(OperationConnection.from_dict(x) for x in _array(raw[key], key))
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    is_hypothetical: bool
    formula: str

    def __post_init__(self) -> None:
        _text(self.name, "component name")
        _boolean(self.is_hypothetical, "is_hypothetical")
        if type(self.formula) is not str:
            raise TypeError("Component formula must be text, including an empty unknown formula")

    @classmethod
    def from_dict(cls, value: Any) -> Component:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class MaterialReading:
    name: str
    direction: str
    temperature_C: float
    pressure_kPa: float
    mass_flow_kg_h: float
    mass_enthalpy_kJ_kg: float
    heat_flow_kJ_h: float
    vapour_fraction_molar: float
    component_mass_fractions: tuple[float, ...]

    def __post_init__(self) -> None:
        _text(self.name, "material name")
        if self.direction not in ("in", "out"):
            raise ValueError("Only external material readings belong to the boundary")
        for key in _MATERIAL_PROPERTIES:
            object.__setattr__(self, key, _number(getattr(self, key), key))
        if (
            self.mass_flow_kg_h < 0
            or self.pressure_kPa < 0
            or not 0 <= self.vapour_fraction_molar <= 1
        ):
            raise ValueError("Invalid flow, pressure or molar vapour fraction")
        if (
            type(self.component_mass_fractions) is not tuple
            or len(self.component_mass_fractions) != 44
        ):
            raise ValueError("Expected 44 component mass fractions")
        fractions = tuple(
            _number(x, "component mass fraction") for x in self.component_mass_fractions
        )
        if any(not 0 <= x <= 1 for x in fractions) or not math.isclose(
            math.fsum(fractions), 1.0, rel_tol=0, abs_tol=1e-12
        ):
            raise ValueError("Component mass fractions must lie in [0,1] and sum to one")
        object.__setattr__(self, "component_mass_fractions", fractions)

    @classmethod
    def from_dict(cls, value: Any) -> MaterialReading:
        raw = _record(value, cls)
        raw["component_mass_fractions"] = tuple(
            _array(raw["component_mass_fractions"], "fractions")
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class EnergyReading:
    scope: str
    name: str
    direction: str
    heat_flow_kJ_h: float

    def __post_init__(self) -> None:
        _text(self.name, "energy name")
        if self.scope not in ("main", "column") or self.direction not in ("in", "out"):
            raise ValueError("Invalid physical energy flow identity")
        object.__setattr__(self, "heat_flow_kJ_h", _number(self.heat_flow_kJ_h, "energy flow"))

    @classmethod
    def from_dict(cls, value: Any) -> EnergyReading:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class BoundaryReadings:
    fluid_package_name: str
    property_package_name: str
    components: tuple[Component, ...]
    materials: tuple[MaterialReading, ...]
    energy: tuple[EnergyReading, ...]
    bridge_heat_flow_kJ_h: float

    def __post_init__(self) -> None:
        _text(self.fluid_package_name, "fluid package")
        _text(self.property_package_name, "property package")
        for key, kind, count in (
            ("components", Component, 44),
            ("materials", MaterialReading, 9),
            ("energy", EnergyReading, 11),
        ):
            seq = getattr(self, key)
            _tuple(seq, kind, key)
            if len(seq) != count:
                raise ValueError(f"Expected {count} {key}")
            _unique(tuple((x.scope, x.name) if kind is EnergyReading else x.name for x in seq), key)
        object.__setattr__(
            self, "bridge_heat_flow_kJ_h", _number(self.bridge_heat_flow_kJ_h, "bridge heat flow")
        )

    @classmethod
    def from_dict(cls, value: Any) -> BoundaryReadings:
        raw = _record(value, cls)
        for key, kind in (
            ("components", Component),
            ("materials", MaterialReading),
            ("energy", EnergyReading),
        ):
            raw[key] = tuple(kind.from_dict(x) for x in _array(raw[key], key))
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class BoundarySnapshot:
    definition: BoundaryDefinition
    core_before: OperatingSnapshot
    core_after: OperatingSnapshot
    first: BoundaryReadings
    second: BoundaryReadings

    def __post_init__(self) -> None:
        for key, kind in (
            ("definition", BoundaryDefinition),
            ("core_before", OperatingSnapshot),
            ("core_after", OperatingSnapshot),
            ("first", BoundaryReadings),
            ("second", BoundaryReadings),
        ):
            if type(getattr(self, key)) is not kind:
                raise TypeError(f"{key} must be {kind.__name__}")
        before, after = self.core_before, self.core_after
        if (
            before.case_id != self.definition.case_id
            or replace(after, observed_at_utc=before.observed_at_utc) != before
            or not _stable(before)
        ):
            raise ValueError("Source core observation changed or is not stable")
        for sample in (self.first, self.second):
            # Only these exact Table properties duplicate boundary observations.
            # Product TBP temperatures are different physical quantities.
            for material in sample.materials:
                checks = [("相-质量流量 (总体)", "mass_flow", "kg/h", material.mass_flow_kg_h)]
                if material.name == "Crude_Oil":
                    checks.extend(
                        [
                            ("相-温度 (总体)", "temperature", "C", material.temperature_C),
                            ("相-压力 (总体)", "pressure", "kPa", material.pressure_kPa),
                        ]
                    )
                for property_name, quantity, unit, value in checks:
                    matches = [
                        x
                        for x in before.variables
                        if (x.object_name, x.property_name, x.quantity_type, x.unit)
                        == (material.name, property_name, quantity, unit)
                    ]
                    if len(matches) != 1 or matches[0].value != value:
                        raise ValueError(
                            f"Core and boundary observations disagree: {material.name}.{property_name}"
                        )
            if {(x.name, x.direction) for x in sample.materials} != {
                (x.name, x.direction)
                for x in self.definition.materials
                if x.direction != "internal"
            }:
                raise ValueError("Material boundary differs from the saved definition")
            if {(x.scope, x.name, x.direction) for x in sample.energy} != {
                (x.scope, x.name, x.direction)
                for x in self.definition.energy
                if x.direction != "bridge"
            }:
                raise ValueError("Energy boundary differs from the saved definition")
            bridge = next(x for x in self.definition.energy if x.direction == "bridge")
            exported = next(
                x for x in sample.energy if x.scope == "column" and x.name == bridge.name
            )
            if exported.heat_flow_kJ_h != sample.bridge_heat_flow_kJ_h:
                raise ValueError("Main and column bridge duty readings differ")

    @property
    def consistent_observation(self) -> bool:
        return self.first == self.second

    def to_dict(self) -> dict[str, Any]:
        return {
            **_DECLARATIONS,
            "units": dict(_UNITS),
            "definition": self.definition.to_dict(),
            "definition_sha256": _sha256(_canonical(self.definition.to_dict())),
            "core_before": self.core_before.to_dict(),
            "core_after": self.core_after.to_dict(),
            "first": json.loads(_canonical(asdict(self.first))),
            "second": json.loads(_canonical(asdict(self.second))),
            "consistent_observation": self.consistent_observation,
        }

    @classmethod
    def from_dict(cls, value: Any) -> BoundarySnapshot:
        if type(value) is not dict:
            raise TypeError("Boundary snapshot must be an object")
        raw = dict(value)
        consistency = raw.pop("consistent_observation", None)
        _boolean(consistency, "consistent_observation")
        units = raw.pop("units", None)
        if type(units) is not dict or units != _UNITS:
            raise ValueError("Boundary units differ from the supported units")
        digest = raw.pop("definition_sha256", None)
        raw = _record(raw, cls, declarations=_DECLARATIONS)
        raw["definition"] = BoundaryDefinition.from_dict(raw["definition"])
        if digest != _sha256(_canonical(raw["definition"].to_dict())):
            raise ValueError("Boundary definition hash differs")
        for key in ("core_before", "core_after"):
            raw[key] = OperatingSnapshot.from_dict(raw[key])
        for key in ("first", "second"):
            raw[key] = BoundaryReadings.from_dict(raw[key])
        result = cls(**raw)
        if result.consistent_observation != consistency:
            raise ValueError("Declared consistency differs from the two saved observations")
        return result


def read_boundary_snapshot(path: Path) -> BoundarySnapshot:
    """Read saved evidence without current configuration, COM, or source file access.

    Historical COM identity checks are runtime observations, not independently
    proved by offline loading. The definition hash is integrity, not authentication.
    """
    return BoundarySnapshot.from_dict(_json(_file_bytes(path.expanduser().absolute())))


def write_boundary_snapshot(path: Path, snapshot: BoundarySnapshot) -> None:
    """Write one new file in an existing plain directory; never overwrite evidence."""
    if type(snapshot) is not BoundarySnapshot:
        raise TypeError("snapshot must be a BoundarySnapshot")
    path = path.expanduser().absolute()
    _plain_directory(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )


def _items(collection: Any, expected: int | None = None) -> tuple[Any, ...]:
    count = collection.Count
    if (
        type(count) is not int
        or not 0 <= count <= 1000
        or (expected is not None and count != expected)
    ):
        raise HysysReadError("boundary-inventory", "Unexpected collection count")
    return tuple(collection.Item(i) for i in range(count))


def _connections(collection: Any) -> tuple[OperationConnection, ...]:
    return tuple(OperationConnection(x.Name, x.TypeName) for x in _items(collection))


def _streams(collection: Any, bindings: tuple[StreamBinding, ...]) -> dict[str, Any]:
    items = _items(collection, len(bindings))
    result = {item.Name: item for item in items}
    if len(result) != len(items) or result.keys() != {item.name for item in bindings}:
        raise HysysReadError(
            "boundary-topology", "Stream names differ from the complete boundary inventory"
        )
    for binding in bindings:
        item = result[binding.name]
        if (
            _connections(item.UpstreamOpers) != binding.upstream
            or _connections(item.DownstreamOpers) != binding.downstream
        ):
            raise HysysReadError("boundary-topology", f"Connections changed: {binding.name}")
    return result


def _quantity(item: Any, attr: str, code: int, unit: str) -> float:
    variable = getattr(item, attr)
    if type(variable.UnitConversionType) is not int or variable.UnitConversionType != code:
        raise HysysReadError("boundary-unit", f"Unexpected quantity type: {item.Name}.{attr}")
    if not _com_boolean(variable.IsKnown, "IsKnown"):
        raise HysysReadError("boundary-unknown", f"Undefined quantity: {item.Name}.{attr}")
    return _number(variable.GetValue(unit), attr)


def _sample_boundary(case: Any, definition: BoundaryDefinition) -> BoundaryReadings:
    flow = case.Flowsheet
    column = flow.Operations.Item(definition.column_name).ColumnFlowsheet
    for operations, expected in (
        (flow.Operations, definition.main_operations),
        (column.Operations, definition.column_operations),
    ):
        actual = _connections(operations)
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise HysysReadError(
                "boundary-topology", "Operation inventory differs from the qualified flowsheet"
            )
    if _items(column.Flowsheets):
        raise HysysReadError("boundary-topology", "Nested column flowsheets are not qualified")
    materials = _streams(flow.MaterialStreams, definition.materials)
    energy = {
        scope: _streams(
            sheet.EnergyStreams, tuple(x for x in definition.energy if x.scope == scope)
        )
        for scope, sheet in (("main", flow), ("column", column))
    }
    package = flow.FluidPackage
    components = tuple(
        Component(x.Name, _com_boolean(x.IsHypothetical, "IsHypothetical"), x.Formula)
        for x in _items(package.Components, 44)
    )
    names = tuple(x.name for x in components)
    if tuple(package.Components.Names) != names:
        raise HysysReadError("boundary-components", "Component Names and indexed metadata differ")
    readings = []
    for binding in definition.materials:
        if binding.direction == "internal":
            continue
        item = materials[binding.name]
        if (
            tuple(item.FluidPackage.Components.Names) != names
            or item.FluidPackage.Name != package.Name
            or item.FluidPackage.PropertyPackageName != package.PropertyPackageName
        ):
            raise HysysReadError(
                "boundary-components", "Material stream component order or fluid package differs"
            )
        readings.append(
            MaterialReading(
                binding.name,
                binding.direction,
                **{key: _quantity(item, *args) for key, args in _MATERIAL_PROPERTIES.items()},
                component_mass_fractions=tuple(item.ComponentMassFractionValue),
            )
        )
    duties = []
    bridge_value = None
    for binding in definition.energy:
        value = _quantity(energy[binding.scope][binding.name], "HeatFlow", 6, "kJ/h")
        if binding.direction == "bridge":
            bridge_value = value
        else:
            duties.append(EnergyReading(binding.scope, binding.name, binding.direction, value))
    assert bridge_value is not None
    return BoundaryReadings(
        package.Name,
        package.PropertyPackageName,
        components,
        tuple(readings),
        tuple(duties),
        bridge_value,
    )


def _read_open_boundary(
    client: Any, source: Path, definition: BoundaryDefinition, catalog: VariableCatalog, digest: str
) -> BoundarySnapshot:
    app = client.GetActiveObject("HYSYS.Application")
    original = _documents(app)
    if _path(source) not in original:
        raise HysysReadError("case-match", "The specified case must already be open")
    case = original[_path(source)]
    bound = SimpleNamespace(GetActiveObject=lambda _: app)

    def protect() -> None:
        if not _same_documents(original, _documents(app)) or _sha256(_file_bytes(source)) != digest:
            raise HysysReadError(
                "source-changed", "Source case identity, path or disk file changed"
            )

    before = _read_open_case(bound, source, catalog, digest)
    protect()
    if not _stable(before):
        raise HysysReadError("source-unstable", "Source core observation must be stable")
    first = _sample_boundary(case, definition)
    protect()
    second = _sample_boundary(case, definition)
    protect()
    after = _read_open_case(bound, source, catalog, digest)
    protect()
    return BoundarySnapshot(definition, before, after, first, second)


def _observe(
    client: Any, source: Path, definition: BoundaryDefinition, catalog: VariableCatalog, digest: str
) -> BoundarySnapshot:
    app = client.GetActiveObject("HYSYS.Application")
    original = _documents(app)
    if len(original) != 1 or _path(source) not in original:
        raise HysysReadError("case-match", "Only the specified source case must already be open")
    snapshot = _read_open_boundary(
        SimpleNamespace(GetActiveObject=lambda _: app), source, definition, catalog, digest
    )
    if not _same_documents(original, _documents(app)):
        raise HysysReadError("source-changed", "Source case identity or document set changed")
    return snapshot


def _capture(*args: Any) -> tuple[BoundarySnapshot | None, HysysReadError | None]:
    try:
        return _observe(*args), None
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - release COM tracebacks before uninitialize
        error = HysysReadError(
            getattr(
                exc,
                "code",
                "interrupted" if isinstance(exc, KeyboardInterrupt) else "boundary-read-failed",
            ),
            str(exc),
            phase="read-boundary",
        )
        code = getattr(exc, "hresult", None)
        error.hresult = code if type(code) is int else None
        details = getattr(exc, "excepinfo", None)
        scode = (
            details[5]
            if isinstance(details, tuple) and len(details) == 6
            else getattr(exc, "scode", None)
        )
        error.scode = scode if type(scode) is int else None
        # COM wrappers can retain a local exception that points back at its own
        # traceback. Clear those finished frames rather than waiting for cyclic GC.
        traceback.clear_frames(exc.__traceback__)
        return None, error


def read_current_boundary(case_path: Path) -> BoundarySnapshot:
    """Attach read-only to one source. Never open, activate, save, solve or close it."""
    payload = _file_bytes(DEFAULT_BOUNDARY)
    definition = BoundaryDefinition.from_dict(_json(payload))
    catalog_payload = _file_bytes(DEFAULT_CATALOG)
    catalog = load_catalog(DEFAULT_CATALOG)
    if (
        _file_bytes(DEFAULT_CATALOG) != catalog_payload
        or catalog.case_id != definition.case_id
        or catalog.column_name != definition.column_name
    ):
        raise HysysReadError(
            "boundary-config", "Core catalog changed or identifies a different boundary"
        )
    source = case_path.expanduser().absolute()
    if source.suffix.lower() != ".hsc":
        raise HysysReadError("invalid-case-path", "An existing .hsc file is required")
    digest = _sha256(_file_bytes(source))
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    pythoncom.CoInitialize()
    try:
        snapshot, error = _capture(client, source, definition, catalog, digest)
    finally:
        pythoncom.CoUninitialize()
    if _sha256(_file_bytes(source)) != digest:
        raise HysysReadError(
            "source-file-changed", "Source disk file changed during boundary reading"
        )
    if _file_bytes(DEFAULT_BOUNDARY) != payload or _file_bytes(DEFAULT_CATALOG) != catalog_payload:
        raise HysysReadError("boundary-config", "Configuration changed during boundary reading")
    if error is not None:
        raise error
    assert snapshot is not None
    return snapshot
