"""Exact comparisons of qualified HYSYS observations, independent of COM."""

from __future__ import annotations

import math
from typing import Any

from .models import OperatingSnapshot


def _difference(
    differences: list[dict[str, Any]],
    path: str,
    expected: Any,
    actual: Any,
    *,
    quantity: str | None = None,
    unit: str | None = None,
) -> None:
    if expected == actual:
        return
    absolute = None
    if type(expected) in (int, float) and type(actual) in (int, float):
        delta = abs(actual - expected)
        if type(delta) is int or math.isfinite(delta):
            absolute = delta
    differences.append(
        {
            "path": path,
            "expected": expected,
            "actual": actual,
            "quantity": quantity,
            "unit": unit,
            "abs_difference": absolute,
        }
    )


def compare_operating_points(
    expected: OperatingSnapshot, actual: OperatingSnapshot
) -> dict[str, Any]:
    """Compare complete, already validated observations with zero numerical tolerance.

    The caller must independently verify frozen model/catalog hashes and source-case
    protection. Time, path, disk hash and IsDirty are deliberately excluded here;
    equality of visible readings cannot establish identity of hidden model state.
    Both observations must be consistent, idle, valid, converged and have zero DOF.
    CanSolve must match, including when both observations are paused.

    Differences use named members rather than collection positions. The unit is
    None for raw internal values because the snapshot does not name their units.
    abs_difference is None for nonnumeric/missing values or an overflowing delta.
    This result does not change either snapshot's optimization eligibility.
    """
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []

    for field in ("case_id", "hysys_version"):
        _difference(inputs, field, getattr(expected, field), getattr(actual, field))

    before_variables = {item.variable_id: item for item in expected.variables}
    after_variables = {item.variable_id: item for item in actual.variables}
    for name in sorted(before_variables.keys() | after_variables.keys()):
        before_variable = before_variables.get(name)
        after_variable = after_variables.get(name)
        path = f"variables[{name!r}]"
        if before_variable is None or after_variable is None:
            _difference(
                inputs,
                path,
                None if before_variable is None else before_variable.to_dict(),
                None if after_variable is None else after_variable.to_dict(),
            )
            continue
        for field in (
            "role",
            "row",
            "object_name",
            "property_name",
            "quantity_type",
            "unit",
            "state",
            "can_modify",
        ):
            _difference(
                inputs,
                f"{path}.{field}",
                getattr(before_variable, field),
                getattr(after_variable, field),
            )
        readings = inputs if before_variable.role == "mv" else outputs
        for field in ("value", "internal_value"):
            _difference(
                readings,
                f"{path}.{field}",
                getattr(before_variable, field),
                getattr(after_variable, field),
                quantity=before_variable.quantity_type,
                unit=before_variable.unit if field == "value" else None,
            )

    before_specs = {item.name: item for item in expected.specifications}
    after_specs = {item.name: item for item in actual.specifications}
    for name in sorted(before_specs.keys() | after_specs.keys()):
        before_spec = before_specs.get(name)
        after_spec = after_specs.get(name)
        path = f"specifications[{name!r}]"
        if before_spec is None or after_spec is None:
            _difference(
                inputs,
                path,
                None if before_spec is None else before_spec.to_dict(),
                None if after_spec is None else after_spec.to_dict(),
            )
            continue
        for field in ("specification_type", "active", "used_as_estimate", "quantity_type", "unit"):
            _difference(
                inputs,
                f"{path}.{field}",
                getattr(before_spec, field),
                getattr(after_spec, field),
            )
        for field, differences in (("goal", inputs), ("current", outputs)):
            _difference(
                differences,
                f"{path}.{field}",
                getattr(before_spec, field),
                getattr(after_spec, field),
                quantity=before_spec.quantity_type,
                unit=before_spec.unit,
            )

    before_stages = {item.name: item for item in expected.stages}
    after_stages = {item.name: item for item in actual.stages}
    for name in sorted(before_stages.keys() | after_stages.keys()):
        before_stage = before_stages.get(name)
        after_stage = after_stages.get(name)
        path = f"stages[{name!r}]"
        if before_stage is None or after_stage is None:
            _difference(
                inputs,
                path,
                None if before_stage is None else before_stage.to_dict(),
                None if after_stage is None else after_stage.to_dict(),
            )
            continue
        for field, quantity, unit in (
            ("pressure_kPa", "pressure", "kPa"),
            ("temperature_C", "temperature", "C"),
            ("liquid_kg_h", "mass_flow", "kg/h"),
            ("vapor_kg_h", "mass_flow", "kg/h"),
        ):
            _difference(
                outputs,
                f"{path}.{field}",
                getattr(before_stage, field),
                getattr(after_stage, field),
                quantity=quantity,
                unit=unit,
            )

    _difference(states, "solver.can_solve", expected.solver.can_solve, actual.solver.can_solve)
    for side, snapshot in (("expected", expected), ("actual", actual)):
        for field, required, observed in (
            ("consistent_observation", True, snapshot.consistent_observation),
            ("degrees_of_freedom", 0, snapshot.degrees_of_freedom),
            ("solver.is_solving", False, snapshot.solver.is_solving),
            ("solver.is_valid", True, snapshot.solver.is_valid),
            ("solver.column_converged", True, snapshot.solver.column_converged),
        ):
            _difference(states, f"{side}.{field}", required, observed)

    return {
        "equivalent": not (inputs or outputs or states),
        "input_differences": inputs,
        "output_differences": outputs,
        "state_differences": states,
    }
