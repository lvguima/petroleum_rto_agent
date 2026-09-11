"""Recovery comparison boundaries, using only immutable synthetic observations."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from petroleum_rto.simulation.comparison import compare_operating_points
from petroleum_rto.simulation.models import (
    OperatingSnapshot,
    SolverState,
    SpecificationReading,
    StageReading,
    VariableReading,
)


@pytest.fixture
def snapshot() -> OperatingSnapshot:
    return OperatingSnapshot(
        case_id="synthetic-case",
        observed_at_utc="2026-09-11T08:30:00+00:00",
        source_case_path="D:/synthetic/source.hsc",
        source_disk_sha256="a" * 64,
        hysys_version="12.0",
        memory_is_dirty=True,
        solver=SolverState(True, False, True, True),
        degrees_of_freedom=0,
        variables=tuple(
            VariableReading(
                f"variable-{row}",
                "mv" if row < 26 else "cv",
                row,
                "equipment",
                f"property-{row}",
                "mass_flow",
                "kg/h",
                3600.0,
                1.0,
                1 if row < 26 else 0,
                row < 26,
            )
            for row in (*range(2, 26), *range(28, 64))
        ),
        stages=tuple(
            StageReading(f"stage-{index}", 150.0, 100.0, 1000.0, 2000.0) for index in range(73)
        ),
        specifications=(
            SpecificationReading(
                "active", "ColumnTemperatureSpec", True, True, "temperature", "C", 150.0, 150.0
            ),
            SpecificationReading(
                "inactive", "ColumnReboilRatioSpec", False, True, "ratio", "", 0.5, 0.4
            ),
            SpecificationReading(
                "unknown", "ColumnReboilRatioSpec", False, False, "ratio", "", None, None
            ),
        ),
        consistent_observation=True,
    )


def test_equal_points_ignore_observation_metadata_and_collection_order(
    snapshot: OperatingSnapshot,
) -> None:
    actual = replace(
        snapshot,
        observed_at_utc="2026-09-12T10:00:00+00:00",
        source_case_path="D:/synthetic/working-copy.hsc",
        source_disk_sha256="b" * 64,
        memory_is_dirty=False,
        variables=tuple(reversed(snapshot.variables)),
        stages=tuple(reversed(snapshot.stages)),
        specifications=tuple(reversed(snapshot.specifications)),
    )
    assert compare_operating_points(snapshot, actual) == {
        "equivalent": True,
        "input_differences": [],
        "output_differences": [],
        "state_differences": [],
    }
    assert snapshot.to_dict()["eligible_for_optimization"] is False
    assert actual.to_dict()["eligible_for_optimization"] is False


@pytest.mark.parametrize("field", ["case_id", "hysys_version"])
def test_case_identity_is_strict(snapshot: OperatingSnapshot, field: str) -> None:
    change: dict[str, Any] = {field: "different"}
    result = compare_operating_points(snapshot, replace(snapshot, **change))
    assert not result["equivalent"]
    assert result["input_differences"][0]["path"] == field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_name", "other-equipment"),
        ("property_name", "other-property"),
        ("state", 2),
        ("can_modify", False),
        ("value", math.nextafter(3600.0, math.inf)),
        ("internal_value", math.nextafter(1.0, math.inf)),
    ],
)
def test_mv_identity_and_smallest_representable_value_changes_are_rejected(
    snapshot: OperatingSnapshot,
    field: str,
    value: Any,
) -> None:
    actual = replace(
        snapshot,
        variables=(replace(snapshot.variables[0], **{field: value}), *snapshot.variables[1:]),
    )
    result = compare_operating_points(snapshot, actual)
    assert not result["equivalent"]
    assert len(result["input_differences"]) == 1
    assert result["input_differences"][0]["path"] == f"variables['variable-2'].{field}"
    assert result["output_differences"] == []


def test_binding_row_exchange_and_unit_quantity_changes_are_not_hidden(
    snapshot: OperatingSnapshot,
) -> None:
    variables = list(snapshot.variables)
    variables[0] = replace(variables[0], row=3, quantity_type="temperature", unit="C")
    variables[1] = replace(variables[1], row=2)
    result = compare_operating_points(snapshot, replace(snapshot, variables=tuple(variables)))
    assert not result["equivalent"]
    assert {d["path"] for d in result["input_differences"]} == {
        "variables['variable-2'].row",
        "variables['variable-3'].row",
        "variables['variable-2'].quantity_type",
        "variables['variable-2'].unit",
    }


@pytest.mark.parametrize("collection", ["variables", "stages", "specifications"])
def test_named_member_replacement_is_an_identity_failure(
    snapshot: OperatingSnapshot,
    collection: str,
) -> None:
    items = getattr(snapshot, collection)
    key = "variable_id" if collection == "variables" else "name"
    change: dict[str, Any] = {collection: (replace(items[0], **{key: "renamed"}), *items[1:])}
    actual = replace(snapshot, **change)
    result = compare_operating_points(snapshot, actual)
    assert not result["equivalent"]
    assert len(result["input_differences"]) == 2
    assert all(d["expected"] is None or d["actual"] is None for d in result["input_differences"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("field", ["value", "internal_value"])
def test_cv_differences_are_outputs_with_units_only_for_qualified_values(
    snapshot: OperatingSnapshot,
    field: str,
) -> None:
    variables = list(snapshot.variables)
    variables[24] = replace(variables[24], **{field: getattr(variables[24], field) + 1.0})
    result = compare_operating_points(snapshot, replace(snapshot, variables=tuple(variables)))
    assert not result["equivalent"]
    assert result["input_differences"] == []
    assert result["output_differences"] == [
        {
            "path": f"variables['variable-28'].{field}",
            "expected": getattr(snapshot.variables[24], field),
            "actual": getattr(variables[24], field),
            "quantity": "mass_flow",
            "unit": "kg/h" if field == "value" else None,
            "abs_difference": 1.0,
        }
    ]


@pytest.mark.parametrize("field", ["pressure_kPa", "temperature_C", "liquid_kg_h", "vapor_kg_h"])
def test_every_stage_output_is_compared(snapshot: OperatingSnapshot, field: str) -> None:
    stage = snapshot.stages[0]
    actual = replace(
        snapshot,
        stages=(replace(stage, **{field: getattr(stage, field) + 1.0}), *snapshot.stages[1:]),
    )
    result = compare_operating_points(snapshot, actual)
    assert not result["equivalent"]
    assert result["output_differences"][0]["path"] == f"stages['stage-0'].{field}"
    assert result["output_differences"][0]["abs_difference"] == 1.0


@pytest.mark.parametrize(
    ("index", "field", "value", "category"),
    [
        (0, "active", False, "input_differences"),
        (0, "used_as_estimate", False, "input_differences"),
        (0, "specification_type", "DifferentSpec", "input_differences"),
        (0, "quantity_type", "temperature_difference", "input_differences"),
        (0, "goal", 151.0, "input_differences"),
        (1, "goal", 0.6, "input_differences"),
        (1, "goal", None, "input_differences"),
        (2, "goal", 0.0, "input_differences"),
        (0, "current", 150.001, "output_differences"),
        (1, "current", None, "output_differences"),
        (2, "current", 0.0, "output_differences"),
    ],
)
def test_specification_inputs_and_outputs_include_inactive_and_unknown_values(
    snapshot: OperatingSnapshot,
    index: int,
    field: str,
    value: Any,
    category: str,
) -> None:
    specs = list(snapshot.specifications)
    specs[index] = replace(specs[index], **{field: value})
    result = compare_operating_points(snapshot, replace(snapshot, specifications=tuple(specs)))
    assert not result["equivalent"]
    assert len(result[category]) == 1
    assert result[category][0]["path"] == f"specifications[{specs[index].name!r}].{field}"


@pytest.mark.parametrize(
    "condition", ["inconsistent", "dof", "solving", "invalid", "not_converged"]
)
@pytest.mark.parametrize("side", ["expected", "actual", "both"])
def test_equal_numbers_do_not_hide_an_unacceptable_solver_state(
    snapshot: OperatingSnapshot,
    condition: str,
    side: str,
) -> None:
    if condition == "inconsistent":
        bad = replace(snapshot, consistent_observation=False)
    elif condition == "dof":
        bad = replace(snapshot, degrees_of_freedom=1)
    else:
        field = {
            "solving": "is_solving",
            "invalid": "is_valid",
            "not_converged": "column_converged",
        }[condition]
        bad = replace(snapshot, solver=replace(snapshot.solver, **{field: condition == "solving"}))
    result = compare_operating_points(
        bad if side != "actual" else snapshot, bad if side != "expected" else snapshot
    )
    assert not result["equivalent"]
    assert result["input_differences"] == result["output_differences"] == []
    assert result["state_differences"]


def test_can_solve_must_match_but_paused_stable_observations_can_match(
    snapshot: OperatingSnapshot,
) -> None:
    paused = replace(snapshot, solver=replace(snapshot.solver, can_solve=False))
    result = compare_operating_points(snapshot, paused)
    assert not result["equivalent"]
    assert result["state_differences"][0]["path"] == "solver.can_solve"
    assert compare_operating_points(paused, paused)["equivalent"]


def test_overflowing_difference_remains_a_json_serializable_failure(
    snapshot: OperatingSnapshot,
) -> None:
    before = replace(
        snapshot, stages=(replace(snapshot.stages[0], temperature_C=-1e308), *snapshot.stages[1:])
    )
    after = replace(
        snapshot, stages=(replace(snapshot.stages[0], temperature_C=1e308), *snapshot.stages[1:])
    )
    result = compare_operating_points(before, after)
    assert not result["equivalent"]
    assert result["output_differences"][0]["abs_difference"] is None
    json.dumps(result, allow_nan=False)
