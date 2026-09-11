"""Read a specified open HYSYS case through qualified spreadsheet bindings."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .models import (
    QUANTITY_UNITS,
    OperatingSnapshot,
    SolverState,
    SpecificationReading,
    StageReading,
    VariableReading,
)

DEFAULT_CATALOG = Path(__file__).resolve().parents[3] / "configs/simulation/mjh_atm_variables.json"
# UnitConversionType_enum values read from this installation's HYSYS type library.
QUANTITY_CODES = {
    1: "temperature",
    2: "pressure",
    4: "mass_flow",
    6: "heat_flow",
    24: "temperature_difference",
    45: "pressure_difference",
    64: "percent",
    65: "ratio",
}


class HysysReadError(ValueError):
    """A failed observation is not a process-infeasibility result."""

    def __init__(self, code: str, message: str, *, phase: str = "read") -> None:
        self.code = code
        self.phase = phase
        self.hresult: int | None = None
        self.scode: int | None = None
        super().__init__(message)


@dataclass(frozen=True)
class VariableBinding:
    variable_id: str
    row: int
    role: Literal["mv", "cv"]
    object_name: str
    property_name: str
    quantity_type: str
    unit: str
    column_specification: str | None


@dataclass(frozen=True)
class VariableCatalog:
    case_id: str
    table_name: str
    column_name: str
    variables: tuple[VariableBinding, ...]


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HysysReadError("invalid-catalog", f"Duplicate catalog key: {key}")
        result[key] = value
    return result


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HysysReadError("invalid-value", f"{label}: expected nonempty text")
    return value


def _number(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= -1e20:
        raise HysysReadError("invalid-value", f"{label}: invalid or undefined number")
    return float(value)


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise HysysReadError("invalid-value", f"{label}: expected integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (-1, 0, 1):
        return bool(value)
    raise HysysReadError("invalid-value", f"{label}: expected COM Boolean")


def load_catalog(path: Path = DEFAULT_CATALOG) -> VariableCatalog:
    def reject_constant(value: str) -> Any:
        raise HysysReadError("invalid-catalog", f"Nonfinite catalog value: {value}")

    raw = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object,
        parse_constant=reject_constant,
    )
    return catalog_from_dict(raw)


def catalog_from_dict(raw: Any) -> VariableCatalog:
    expected = {"schema_version", "case_id", "table_name", "column_name", "variables"}
    if type(raw) is not dict or set(raw) != expected or raw["schema_version"] != "1.0.0":
        raise HysysReadError("invalid-catalog", "Unsupported variable catalog")
    if type(raw["variables"]) is not list:
        raise HysysReadError("invalid-catalog", "Catalog variables must be an array")
    bindings = []
    for item in raw["variables"]:
        fields = set(VariableBinding.__dataclass_fields__)
        if type(item) is not dict or set(item) != fields:
            raise HysysReadError("invalid-catalog", "Variable binding fields differ")
        for key in fields - {"row", "unit", "column_specification"}:
            _text(item[key], key)
        row = _integer(item["row"], "row")
        if item["role"] not in {"mv", "cv"}:
            raise HysysReadError("invalid-catalog", "Unknown variable role")
        if row not in (range(2, 26) if item["role"] == "mv" else range(28, 64)):
            raise HysysReadError("invalid-catalog", "Variable row does not match its role")
        quantity = item["quantity_type"]
        if quantity not in QUANTITY_UNITS or item["unit"] != QUANTITY_UNITS[quantity]:
            raise HysysReadError("invalid-catalog", "Unsupported quantity or unit")
        if item["column_specification"] is not None:
            _text(item["column_specification"], "column_specification")
        bindings.append(VariableBinding(**item))
    if (
        len(bindings) != 60
        or len({item.variable_id for item in bindings}) != 60
        or {item.row for item in bindings} != {*range(2, 26), *range(28, 64)}
        or len({(item.object_name, item.property_name) for item in bindings}) != 60
    ):
        raise HysysReadError("invalid-catalog", "Expected 60 unique, complete variable bindings")
    return VariableCatalog(
        _text(raw["case_id"], "case_id"),
        _text(raw["table_name"], "table_name"),
        _text(raw["column_name"], "column_name"),
        tuple(sorted(bindings, key=lambda item: item.row)),
    )


def _quantity(variable: Any) -> str:
    code = _integer(variable.UnitConversionType, "UnitConversionType")
    if code not in QUANTITY_CODES:
        raise HysysReadError("unsupported-quantity", f"Unsupported HYSYS quantity code: {code}")
    return QUANTITY_CODES[code]


def _solver(case: Any, cfs: Any) -> SolverState:
    return SolverState(
        _boolean(case.Solver.CanSolve, "CanSolve"),
        _boolean(case.Solver.IsSolving, "IsSolving"),
        _boolean(case.IsValid, "IsValid"),
        _boolean(cfs.CfsConverged, "CfsConverged"),
    )


def _variables(table: Any, cfs: Any, catalog: VariableCatalog) -> tuple[VariableReading, ...]:
    result = []
    for binding in catalog.variables:
        cell = table.Cell(f"C{binding.row}")
        # Reading the actual imported object is essential: text labels alone are editable.
        variable = cell.ImportedVariable
        if variable is None or (
            cell.AttachedObjectName != binding.object_name
            or cell.VariableName != binding.property_name
            or _quantity(variable) != binding.quantity_type
        ):
            raise HysysReadError("binding-drift", f"Binding changed: {binding.variable_id}")
        if not _boolean(variable.IsKnown, "IsKnown"):
            raise HysysReadError("unknown-variable", f"Value unknown: {binding.variable_id}")
        value = _number(variable.GetValue(binding.unit), binding.variable_id)
        if binding.column_specification is not None:
            spec = cfs.Specifications.Item(binding.column_specification)
            if (
                not _boolean(spec.IsActive, "IsActive")
                or _quantity(spec.Goal) != binding.quantity_type
                or not math.isclose(
                    value,
                    _number(spec.Goal.GetValue(binding.unit), "specification goal"),
                    rel_tol=1e-12,
                    abs_tol=1e-10,
                )
            ):
                raise HysysReadError("specification-drift", binding.column_specification)
        result.append(
            VariableReading(
                binding.variable_id,
                binding.role,
                binding.row,
                binding.object_name,
                binding.property_name,
                binding.quantity_type,
                binding.unit,
                value,
                _number(variable.Value, "internal_value"),
                _integer(variable.State, "State"),
                _boolean(variable.CanModify, "CanModify"),
            )
        )
    return tuple(result)


def _stages(cfs: Any) -> tuple[StageReading, ...]:
    stages = cfs.ColumnStages
    if _integer(stages.Count, "ColumnStages.Count") != 73:
        raise HysysReadError("stage-drift", "Expected 73 stages")
    readings = []
    for index in range(stages.Count):
        stage = stages.Item(index)
        separation = stage.SeparationStage
        readings.append(
            StageReading(
                _text(stage.Name, "stage name"),
                _number(separation.Pressure.GetValue("kPa"), "stage pressure"),
                _number(separation.Temperature.GetValue("C"), "stage temperature"),
                _number(separation.MassLiquidFlow.GetValue("kg/h"), "stage liquid flow"),
                _number(separation.MassVapourFlow.GetValue("kg/h"), "stage vapor flow"),
            )
        )
    return tuple(readings)


def _specifications(cfs: Any) -> tuple[SpecificationReading, ...]:
    specs = cfs.Specifications
    count = _integer(specs.Count, "Specifications.Count")
    if not 1 <= count <= 1000:
        raise HysysReadError("invalid-specifications", "Unexpected specification count")
    result = []
    for index in range(count):
        spec = specs.Item(index)
        quantity = _quantity(spec.Goal)
        if _quantity(spec.Current) != quantity:
            raise HysysReadError(
                "specification-drift", "Specification goal and current quantity types differ"
            )
        unit = QUANTITY_UNITS[quantity]
        goal = (
            _number(spec.Goal.GetValue(unit), "goal")
            if _boolean(spec.Goal.IsKnown, "Goal.IsKnown")
            else None
        )
        current = (
            _number(spec.Current.GetValue(unit), "current")
            if _boolean(spec.Current.IsKnown, "Current.IsKnown")
            else None
        )
        result.append(
            SpecificationReading(
                _text(spec.Name, "specification name"),
                _text(spec._oleobj_.GetTypeInfo().GetDocumentation(-1)[0], "specification type"),
                _boolean(spec.IsActive, "IsActive"),
                _boolean(spec.IsUsedAsEstimate, "IsUsedAsEstimate"),
                quantity,
                unit,
                goal,
                current,
            )
        )
    current_names = {
        cfs.ActiveSpecifications.Item(index).Name
        for index in range(_integer(cfs.ActiveSpecifications.Count, "ActiveSpecifications.Count"))
    }
    if current_names != {spec.name for spec in result if spec.active}:
        raise HysysReadError("alternate-specifications", "Current and active specifications differ")
    return tuple(result)


def _sample(case: Any, catalog: VariableCatalog) -> tuple[Any, ...]:
    table = case.Flowsheet.Operations.Item(catalog.table_name)
    cfs = case.Flowsheet.Operations.Item(catalog.column_name).ColumnFlowsheet
    before = _solver(case, cfs)
    readings = (
        _boolean(case.IsDirty, "IsDirty"),
        _integer(cfs.DegreesOfFreedom, "DegreesOfFreedom"),
        _variables(table, cfs, catalog),
        _stages(cfs),
        _specifications(cfs),
    )
    return before, *readings, _solver(case, cfs)


def _read_open_case(
    client: Any, source: Path, catalog: VariableCatalog, digest: str
) -> OperatingSnapshot:
    app = client.GetActiveObject("HYSYS.Application")
    matches = [
        case
        for index in range(app.SimulationCases.Count)
        for case in [app.SimulationCases.Item(index)]
        if os.path.normcase(os.path.abspath(case.FullName)) == os.path.normcase(str(source))
    ]
    if len(matches) != 1:
        raise HysysReadError("case-match", "The specified case must already be open exactly once")
    case = matches[0]
    first, second = _sample(case, catalog), _sample(case, catalog)
    if os.path.normcase(os.path.abspath(case.FullName)) != os.path.normcase(str(source)):
        raise HysysReadError("case-drift", "The case path changed during observation")
    before, dirty, dof, variables, stages, specifications, after = first
    consistent = (
        first == second
        and before == after
        and not before.is_solving
        and before.is_valid
        and before.column_converged
        and dof == 0
    )
    return OperatingSnapshot(
        case_id=catalog.case_id,
        observed_at_utc=datetime.now(UTC).isoformat(),
        source_case_path=str(source),
        source_disk_sha256=digest,
        hysys_version=_text(app.Version, "HYSYS version"),
        memory_is_dirty=dirty,
        solver=after,
        degrees_of_freedom=dof,
        variables=variables,
        stages=stages,
        specifications=specifications,
        consistent_observation=consistent,
    )


def read_current_snapshot(
    case_path: Path, catalog_path: Path = DEFAULT_CATALOG
) -> OperatingSnapshot:
    """Attach and read only; never open, activate, save, solve, write, or close a case."""
    catalog = load_catalog(catalog_path)
    source = case_path.expanduser().resolve()
    if source.suffix.lower() != ".hsc" or not source.is_file():
        raise HysysReadError("invalid-case-path", "An existing .hsc file must be specified")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    pythoncom.CoInitialize()
    try:
        snapshot, error = _capture_observation(client, source, catalog, digest)
    finally:
        # The capture frame has released COM objects and exception tracebacks.
        pythoncom.CoUninitialize()
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise HysysReadError("source-file-changed", "Disk case changed during observation")
    if error is not None:
        raise error
    assert snapshot is not None
    return snapshot


def _capture_observation(
    client: Any, source: Path, catalog: VariableCatalog, digest: str
) -> tuple[OperatingSnapshot | None, HysysReadError | None]:
    try:
        return _read_open_case(client, source, catalog, digest), None
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - external COM boundary
        # Do not retain the original traceback, which owns COM references.
        error = HysysReadError(
            exc.code
            if isinstance(exc, HysysReadError)
            else ("interrupted" if isinstance(exc, KeyboardInterrupt) else "com-read-failed"),
            str(exc),
            phase="read-open-case",
        )
        error.hresult = getattr(exc, "hresult", None)
        details = getattr(exc, "excepinfo", None)
        if isinstance(details, tuple) and len(details) == 6:
            error.scode = details[5]
        return None, error
