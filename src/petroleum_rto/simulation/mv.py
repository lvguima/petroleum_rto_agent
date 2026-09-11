"""Explicit, unit-aware MV changes for owned HYSYS work cases."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any

from .comparison import compare_operating_points
from .hysys import QUANTITY_CODES, VariableCatalog
from .models import OperatingSnapshot

WRITER = "qualified_MV_SetValue_or_specification_GoalValue"


@dataclass(frozen=True)
class MVChange:
    variable_id: str
    value: float
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def internal_value(value: float, quantity: str) -> float:
    # Qualified HYSYS units: temperatures/differences in C, pressures in kPa,
    # percent in percent, ratios dimensionless; mass flow internally kg/s.
    return value / 3600 if quantity == "mass_flow" else value


def close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8)


def parse_changes(
    value: Any, snapshot: OperatingSnapshot, catalog: VariableCatalog
) -> tuple[MVChange, ...]:
    if type(value) is not list or not 1 <= len(value) <= 24:
        raise ValueError("Provide between 1 and 24 MV changes")
    bindings = {b.variable_id: b for b in catalog.variables if b.role == "mv"}
    readings = {r.variable_id: r for r in snapshot.variables}
    specs = {s.name: s for s in snapshot.specifications}
    changes: dict[str, MVChange] = {}
    for item in value:
        if type(item) is not dict or set(item) != {"variable_id", "value", "unit"}:
            raise ValueError("Each MV change requires exactly variable_id, value and unit")
        identifier = item["variable_id"]
        if type(identifier) is not str or identifier not in bindings or identifier in changes:
            raise ValueError("Unknown, duplicate or non-MV variable")
        binding = bindings[identifier]
        reading = readings.get(identifier)
        if (
            reading is None
            or not reading.can_modify
            or reading.state != 1
            or any(
                getattr(binding, name) != getattr(reading, name)
                for name in ("row", "role", "object_name", "property_name", "quantity_type", "unit")
            )
            or item["unit"] != binding.unit
        ):
            raise ValueError("MV binding, unit or current writability differs")
        target = item["value"]
        if type(target) not in (float, int):
            raise ValueError("MV target must be a finite number, not a Boolean")
        try:
            target = float(target)
        except OverflowError as exc:
            raise ValueError("MV target must be finite") from exc
        if not math.isfinite(target):
            raise ValueError("MV target must be finite")
        quantity = binding.quantity_type
        if (
            (quantity in {"mass_flow", "ratio", "pressure"} and target < 0)
            or (quantity == "percent" and not 0 <= target <= 100)
            or (quantity == "temperature" and target <= -273.15)
        ):
            raise ValueError("MV target violates its physical quantity definition")
        if not close(reading.internal_value, internal_value(reading.value, quantity)):
            raise ValueError("MV internal unit conversion is not qualified")
        if binding.column_specification is not None:
            spec = specs.get(binding.column_specification)
            if (
                spec is None
                or not spec.active
                or spec.unit != binding.unit
                or spec.quantity_type != quantity
                or spec.goal != reading.value
            ):
                raise ValueError("MV does not match its active column specification")
        changes[identifier] = MVChange(identifier, target, binding.unit)
    return tuple(changes[name] for name in sorted(changes))


def check_changes(
    before: OperatingSnapshot,
    after: OperatingSnapshot,
    changes: tuple[MVChange, ...],
    catalog: VariableCatalog,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Allow numerical readback rounding only on explicitly selected setpoints."""
    requested = {c.variable_id: c for c in changes}
    bindings = {b.variable_id: b for b in catalog.variables}
    actual = {r.variable_id: r for r in after.variables}
    goals = {bindings[c.variable_id].column_specification: c for c in changes}
    expected_readings = []
    responses = []
    for old in before.variables:
        change = requested.get(old.variable_id)
        if change is None:
            expected_readings.append(old)
            continue
        new = actual.get(old.variable_id)
        converted = internal_value(change.value, old.quantity_type)
        matched = (
            new is not None
            and close(new.value, change.value)
            and close(new.internal_value, converted)
        )
        expected_readings.append(
            replace(
                old,
                value=new.value if matched and new is not None else change.value,
                internal_value=new.internal_value if matched and new is not None else converted,
            )
        )
        spec_name = bindings[old.variable_id].column_specification
        spec = next((s for s in after.specifications if s.name == spec_name), None)
        # HYSYS convergence is the check for general specifications; report the
        # actual process residual without inventing process acceptance limits.
        current = spec.current if spec is not None else (new.value if new is not None else None)
        temperature_tracked = (
            spec is None
            or old.quantity_type != "temperature"
            or (current is not None and abs(current - change.value) <= 0.01)
        )
        responses.append(
            {
                "variable_id": change.variable_id,
                "unit": change.unit,
                "target": change.value,
                "readback": new.value if new is not None else None,
                "actual": current,
                "setpoint_matches": matched,
                "tracking_passed": temperature_tracked,
            }
        )
    actual_specs = {s.name: s for s in after.specifications}
    expected_specs = []
    for old_spec in before.specifications:
        if old_spec.name not in goals:
            expected_specs.append(old_spec)
            continue
        target = goals[old_spec.name].value
        new_spec = actual_specs.get(old_spec.name)
        goal = new_spec.goal if new_spec is not None else None
        expected_specs.append(
            replace(old_spec, goal=goal if goal is not None and close(goal, target) else target)
        )
    expected = replace(
        before, variables=tuple(expected_readings), specifications=tuple(expected_specs)
    )
    return compare_operating_points(expected, after), sorted(
        responses, key=lambda r: r["variable_id"]
    )


def write_changes(
    case: Any, catalog: VariableCatalog, start: OperatingSnapshot, changes: tuple[MVChange, ...]
) -> None:
    """Preflight every selected binding before any write; discard failed work cases."""
    parse_changes([c.to_dict() for c in changes], start, catalog)
    table = case.Flowsheet.Operations.Item(catalog.table_name)
    cfs = case.Flowsheet.Operations.Item(catalog.column_name).ColumnFlowsheet
    readings = {r.variable_id: r for r in start.variables}
    bindings = {b.variable_id: b for b in catalog.variables}
    pending = []
    for change in changes:
        binding, initial = bindings[change.variable_id], readings[change.variable_id]
        cell = table.Cell(f"C{binding.row}")
        variable = cell.ImportedVariable
        if (
            cell.AttachedObjectName != binding.object_name
            or cell.VariableName != binding.property_name
            or QUANTITY_CODES.get(variable.UnitConversionType) != binding.quantity_type
            or variable.State != 1
            or not variable.CanModify
            or not variable.IsKnown
            or not close(variable.GetValue(binding.unit), initial.value)
            or not close(variable.Value, initial.internal_value)
        ):
            raise ValueError("Live MV binding or value changed before writing")
        spec = None
        if binding.column_specification is not None:
            spec = cfs.Specifications.Item(binding.column_specification)
            if (
                not spec.IsActive
                or QUANTITY_CODES.get(spec.Goal.UnitConversionType) != binding.quantity_type
                or not close(spec.Goal.GetValue(binding.unit), initial.value)
                or not close(spec.GoalValue, initial.internal_value)
            ):
                raise ValueError("Live active MV specification changed")
        pending.append((change, binding, variable, spec))
    case.Solver.CanSolve = False
    for change, binding, variable, spec in pending:
        if spec is None:
            variable.SetValue(change.value, change.unit)
        else:
            spec.GoalValue = internal_value(change.value, binding.quantity_type)
    # Check all values after the batch, catching aliases that overwrite an earlier write.
    for change, binding, variable, spec in pending:
        if (
            not close(variable.GetValue(change.unit), change.value)
            or not close(variable.Value, internal_value(change.value, binding.quantity_type))
            or (spec is not None and not close(spec.Goal.GetValue(change.unit), change.value))
        ):
            raise ValueError("MV batch did not read back all requested targets")
    cfs.Reset()
    case.Solver.CanSolve = True
    cfs.Run()
