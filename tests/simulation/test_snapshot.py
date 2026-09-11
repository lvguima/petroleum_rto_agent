"""Boundary checks for persisted HYSYS observations; no COM or solver is used."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from petroleum_rto.simulation.models import (
    QUANTITY_UNITS,
    OperatingSnapshot,
    SolverState,
    SpecificationReading,
    StageReading,
    VariableReading,
    read_snapshot,
    write_snapshot,
)


@pytest.fixture
def snapshot() -> OperatingSnapshot:
    return OperatingSnapshot(
        case_id="test-hysys-case",
        observed_at_utc="2026-09-11T08:30:00+00:00",
        source_case_path="D:/synthetic/模型.hsc",
        source_disk_sha256="a" * 64,
        hysys_version="12.0",
        memory_is_dirty=True,
        solver=SolverState(True, False, True, True),
        degrees_of_freedom=0,
        variables=tuple(
            VariableReading(
                variable_id=f"variable-{row}",
                role="mv" if row < 26 else "cv",
                row=row,
                object_name="synthetic-equipment",
                property_name=f"property-{row}",
                quantity_type="mass_flow",
                unit="kg/h",
                value=3600.0,
                internal_value=1.0,
                state=1 if row < 26 else 0,
                can_modify=row < 26,
            )
            for row in (*range(2, 26), *range(28, 64))
        ),
        stages=tuple(
            StageReading(f"stage-{index}", 150.0, 100.0, 1000.0, 2000.0) for index in range(73)
        ),
        specifications=(
            SpecificationReading("active", "temperature", True, True, "temperature", "C", 150, 150),
            SpecificationReading("inactive", "ratio", False, False, "ratio", "", None, 0.5),
        ),
        consistent_observation=True,
    )


def test_round_trip_preserves_values_and_observation_only_declarations(
    snapshot: OperatingSnapshot, tmp_path: Path
) -> None:
    document = snapshot.to_dict()
    assert document["schema_id"] == "hysys-operating-snapshot"
    assert document["schema_version"] == "1.0.0"
    assert document["source_kind"] == "existing_case_memory"
    assert document["memory_state_bound_to_disk_hash"] is False
    assert document["atomic_snapshot_proven"] is False
    assert document["eligible_for_optimization"] is False
    assert type(document["variables"]) is list
    assert OperatingSnapshot.from_dict(document) == snapshot
    target = tmp_path / "工况.json"
    write_snapshot(target, snapshot)
    assert read_snapshot(target) == snapshot
    assert snapshot.variables[0].value == 3600.0
    assert snapshot.variables[0].internal_value == 1.0
    assert snapshot.stages[0].liquid_kg_h != snapshot.stages[0].vapor_kg_h
    assert snapshot.specifications[1].goal is None
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        write_snapshot(target, replace(snapshot, consistent_observation=False))
    assert target.read_bytes() == before


def test_snapshot_and_nested_values_are_immutable(snapshot: OperatingSnapshot) -> None:
    for value, field in (
        (snapshot, "case_id"),
        (snapshot.variables[0], "value"),
        (snapshot.stages[0], "temperature_C"),
        (snapshot.specifications[0], "goal"),
        (snapshot.solver, "is_valid"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)
    detached = snapshot.to_dict()
    detached["variables"][0]["value"] = 12
    assert snapshot.variables[0].value == 3600


@pytest.mark.parametrize("location", ["root", "variable", "stage", "specification", "solver"])
@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_missing_and_unknown_fields_are_rejected(
    snapshot: OperatingSnapshot, location: str, mutation: str
) -> None:
    document = snapshot.to_dict()
    targets = {
        "root": document,
        "variable": document["variables"][0],
        "stage": document["stages"][0],
        "specification": document["specifications"][0],
        "solver": document["solver"],
    }
    target = targets[location]
    if mutation == "missing":
        target.pop(next(iter(target)))
    else:
        target["unexpected"] = "not accepted"
    with pytest.raises(ValueError, match="missing or unknown"):
        OperatingSnapshot.from_dict(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_id", "other-snapshot"),
        ("schema_version", "2.0.0"),
        ("source_kind", "disk_case_copy"),
        ("memory_state_bound_to_disk_hash", True),
        ("atomic_snapshot_proven", True),
        ("eligible_for_optimization", True),
        ("eligible_for_optimization", 0),
    ],
)
def test_unsupported_or_forged_declarations_are_rejected(
    snapshot: OperatingSnapshot, field: str, value: Any
) -> None:
    document = snapshot.to_dict()
    document[field] = value
    with pytest.raises(ValueError, match="declaration"):
        OperatingSnapshot.from_dict(document)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("variables", "value", True),
        ("variables", "internal_value", float("nan")),
        ("variables", "value", float("inf")),
        ("variables", "value", "123"),
        ("stages", "pressure_kPa", float("-inf")),
        ("stages", "vapor_kg_h", False),
        ("specifications", "goal", True),
        ("specifications", "current", float("nan")),
    ],
)
def test_numeric_fields_reject_nonfinite_values_and_coercion(
    snapshot: OperatingSnapshot, section: str, field: str, value: Any
) -> None:
    document = snapshot.to_dict()
    document[section][0][field] = value
    with pytest.raises((TypeError, ValueError)):
        OperatingSnapshot.from_dict(document)


@pytest.mark.parametrize("field", ["goal", "current"])
def test_only_inactive_specifications_can_have_missing_numbers(
    snapshot: OperatingSnapshot, field: str
) -> None:
    active = snapshot.specifications[0].to_dict()
    active[field] = None
    with pytest.raises(ValueError, match="active specification"):
        SpecificationReading.from_dict(active)
    active["active"] = False
    assert getattr(SpecificationReading.from_dict(active), field) is None


@pytest.mark.parametrize(
    ("quantity", "unit"),
    [
        ("temperature", "C"),
        ("pressure", "kPa"),
        ("mass_flow", "kg/h"),
        ("heat_flow", "kJ/h"),
        ("temperature_difference", "C"),
        ("pressure_difference", "kPa"),
        ("percent", "%"),
        ("ratio", ""),
    ],
)
def test_supported_unit_pairs_are_shared_by_variables_and_specifications(
    snapshot: OperatingSnapshot, quantity: str, unit: str
) -> None:
    assert QUANTITY_UNITS[quantity] == unit
    assert replace(snapshot.variables[0], quantity_type=quantity, unit=unit).unit == unit
    assert replace(snapshot.specifications[0], quantity_type=quantity, unit=unit).unit == unit
    with pytest.raises(ValueError, match="unit"):
        replace(snapshot.variables[0], quantity_type=quantity, unit="unsupported")
    with pytest.raises(ValueError, match="unit"):
        replace(snapshot.specifications[0], quantity_type=quantity, unit="unsupported")


def test_unknown_quantity_is_not_accepted_by_an_existing_unit(snapshot: OperatingSnapshot) -> None:
    with pytest.raises(ValueError, match="quantity"):
        replace(snapshot.variables[0], quantity_type="density", unit="kg/h")


@pytest.mark.parametrize("state", [-1, 3, 6, True, 1.0, "1"])
def test_unknown_or_coerced_variable_state_is_rejected(
    snapshot: OperatingSnapshot, state: Any
) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(snapshot.variables[0], state=state)


def test_supported_variable_states_preserve_the_raw_state(snapshot: OperatingSnapshot) -> None:
    assert [replace(snapshot.variables[0], state=state).state for state in (0, 1, 2, 4, 5)] == [
        0,
        1,
        2,
        4,
        5,
    ]


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("variables", "variable_id"),
        ("variables", "row"),
        ("stages", "name"),
        ("specifications", "name"),
    ],
)
def test_duplicate_identities_are_rejected(
    snapshot: OperatingSnapshot, section: str, field: str
) -> None:
    document = snapshot.to_dict()
    document[section][1][field] = document[section][0][field]
    with pytest.raises(ValueError, match="duplicate"):
        OperatingSnapshot.from_dict(document)


@pytest.mark.parametrize("section", ["variables", "stages"])
def test_incomplete_observation_is_rejected(snapshot: OperatingSnapshot, section: str) -> None:
    document = snapshot.to_dict()
    document[section].pop()
    with pytest.raises(ValueError, match="requires"):
        OperatingSnapshot.from_dict(document)


def test_table_roles_and_rows_cannot_be_relabelled(snapshot: OperatingSnapshot) -> None:
    with pytest.raises(ValueError, match="row"):
        replace(snapshot.variables[0], role="cv")
    with pytest.raises(ValueError, match="row"):
        replace(snapshot.variables[0], row=27)
    with pytest.raises(TypeError, match="integer"):
        replace(snapshot.variables[0], row=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_is_dirty", 1),
        ("consistent_observation", "true"),
        ("degrees_of_freedom", False),
        ("hysys_version", None),
        ("source_case_path", ""),
        ("case_id", " "),
        ("observed_at_utc", "2026-09-11T08:30:00"),
        ("observed_at_utc", "2026-09-11T08:30:00+08:00"),
        ("source_disk_sha256", "a" * 63),
        ("source_disk_sha256", "g" * 64),
    ],
)
def test_snapshot_metadata_is_strict(snapshot: OperatingSnapshot, field: str, value: Any) -> None:
    document = snapshot.to_dict()
    document[field] = value
    with pytest.raises((TypeError, ValueError)):
        OperatingSnapshot.from_dict(document)


def test_json_containers_and_internal_object_types_are_not_interchangeable(
    snapshot: OperatingSnapshot,
) -> None:
    with pytest.raises(TypeError, match="tuple"):
        replace(snapshot, variables=list(snapshot.variables))
    with pytest.raises(TypeError, match="SolverState"):
        replace(snapshot, solver=snapshot.solver.to_dict())
    document = snapshot.to_dict()
    document["variables"] = tuple(document["variables"])
    with pytest.raises(TypeError, match="array"):
        OperatingSnapshot.from_dict(document)


@pytest.mark.parametrize(
    "payload",
    [
        '{"case_id":"first","case_id":"second"}',
        '{"solver":{"can_solve":true,"can_solve":false}}',
        '{"value":NaN}',
        '{"value":Infinity}',
    ],
)
def test_json_duplicate_keys_and_nonfinite_constants_are_rejected(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key|non-finite JSON constant"):
        read_snapshot(path)


def test_exponent_overflow_is_rejected_after_json_parsing(
    snapshot: OperatingSnapshot, tmp_path: Path
) -> None:
    document = snapshot.to_dict()
    document["variables"][0]["value"] = "REPLACE_WITH_NUMBER"
    payload = json.dumps(document).replace('"REPLACE_WITH_NUMBER"', "1e400")
    path = tmp_path / "overflow.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        read_snapshot(path)
