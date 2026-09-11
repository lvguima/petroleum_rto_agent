"""Immutable HYSYS observations and their strict, observation-only JSON boundary."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

QUANTITY_UNITS: Mapping[str, str] = MappingProxyType(
    {
        "temperature": "C",
        "pressure": "kPa",
        "mass_flow": "kg/h",
        "heat_flow": "kJ/h",
        "temperature_difference": "C",
        "pressure_difference": "kPa",
        "percent": "%",
        "ratio": "",
    }
)

_DECLARATIONS: Mapping[str, object] = MappingProxyType(
    {
        "schema_id": "hysys-operating-snapshot",
        "schema_version": "1.0.0",
        "source_kind": "existing_case_memory",
        "memory_state_bound_to_disk_hash": False,
        "atomic_snapshot_proven": False,
        "eligible_for_optimization": False,
    }
)


def _text(value: object, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be nonempty text")


def _boolean(value: object, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be Boolean")


def _integer(value: object, label: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer, not a Boolean")


def _number(value: object, label: str) -> float:
    if type(value) is not int and type(value) is not float:
        raise TypeError(f"{label} must be numeric, not a Boolean")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _quantity(quantity: object, unit: object) -> None:
    if type(quantity) is not str or quantity not in QUANTITY_UNITS:
        raise ValueError("unsupported quantity_type")
    if type(unit) is not str or unit != QUANTITY_UNITS[quantity]:
        raise ValueError(f"unsupported unit for {quantity}")


def _record(
    value: object, model: type[Any], *, declarations: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """Validate JSON shape; constructors own the value invariants."""
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"{model.__name__} must be a JSON object")
    names = {field.name for field in fields(model)}
    fixed = {} if declarations is None else declarations
    if set(value) != names | set(fixed):
        raise ValueError(f"{model.__name__} has missing or unknown fields")
    for key, expected in fixed.items():
        actual = value[key]
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(f"unsupported snapshot declaration: {key}")
    return {key: value[key] for key in names}


def _array(value: object, label: str) -> list[Any]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a JSON array")
    return value


@dataclass(frozen=True, slots=True)
class VariableReading:
    variable_id: str
    role: Literal["mv", "cv"]
    row: int
    object_name: str
    property_name: str
    quantity_type: str
    unit: str
    value: float
    internal_value: float
    state: int
    can_modify: bool

    def __post_init__(self) -> None:
        for name in ("variable_id", "object_name", "property_name"):
            _text(getattr(self, name), name)
        if type(self.role) is not str or self.role not in ("mv", "cv"):
            raise ValueError("role must be mv or cv")
        _integer(self.row, "row")
        expected_rows = range(2, 26) if self.role == "mv" else range(28, 64)
        if self.row not in expected_rows:
            raise ValueError("row does not belong to its Table role")
        _quantity(self.quantity_type, self.unit)
        for name in ("value", "internal_value"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        _integer(self.state, "state")
        if self.state not in (0, 1, 2, 4, 5):
            raise ValueError("unsupported variable state")
        _boolean(self.can_modify, "can_modify")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> VariableReading:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class StageReading:
    name: str
    pressure_kPa: float
    temperature_C: float
    liquid_kg_h: float
    vapor_kg_h: float

    def __post_init__(self) -> None:
        _text(self.name, "stage name")
        for name in ("pressure_kPa", "temperature_C", "liquid_kg_h", "vapor_kg_h"):
            object.__setattr__(self, name, _number(getattr(self, name), name))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> StageReading:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class SpecificationReading:
    name: str
    specification_type: str
    active: bool
    used_as_estimate: bool
    quantity_type: str
    unit: str
    goal: float | None
    current: float | None

    def __post_init__(self) -> None:
        _text(self.name, "specification name")
        _text(self.specification_type, "specification_type")
        _boolean(self.active, "active")
        _boolean(self.used_as_estimate, "used_as_estimate")
        _quantity(self.quantity_type, self.unit)
        for name in ("goal", "current"):
            value = getattr(self, name)
            if value is None:
                if self.active:
                    raise ValueError(f"active specification requires {name}")
            else:
                object.__setattr__(self, name, _number(value, name))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> SpecificationReading:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class SolverState:
    can_solve: bool
    is_solving: bool
    is_valid: bool
    column_converged: bool

    def __post_init__(self) -> None:
        for field in fields(self):
            _boolean(getattr(self, field.name), field.name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> SolverState:
        return cls(**_record(value, cls))


@dataclass(frozen=True, slots=True)
class OperatingSnapshot:
    case_id: str
    observed_at_utc: str
    source_case_path: str
    source_disk_sha256: str
    hysys_version: str
    memory_is_dirty: bool
    solver: SolverState
    degrees_of_freedom: int
    variables: tuple[VariableReading, ...]
    stages: tuple[StageReading, ...]
    specifications: tuple[SpecificationReading, ...]
    consistent_observation: bool

    def __post_init__(self) -> None:
        for name in ("case_id", "source_case_path", "hysys_version", "observed_at_utc"):
            _text(getattr(self, name), name)
        observed = datetime.fromisoformat(self.observed_at_utc)
        if observed.utcoffset() != timedelta(0):
            raise ValueError("observed_at_utc must include an explicit UTC offset")
        if (
            type(self.source_disk_sha256) is not str
            or re.fullmatch(r"[0-9a-fA-F]{64}", self.source_disk_sha256) is None
        ):
            raise ValueError("source_disk_sha256 must contain 64 hexadecimal characters")
        _boolean(self.memory_is_dirty, "memory_is_dirty")
        _boolean(self.consistent_observation, "consistent_observation")
        _integer(self.degrees_of_freedom, "degrees_of_freedom")
        if type(self.solver) is not SolverState:
            raise TypeError("solver must be a SolverState")
        for name, item_type in (
            ("variables", VariableReading),
            ("stages", StageReading),
            ("specifications", SpecificationReading),
        ):
            sequence = getattr(self, name)
            if type(sequence) is not tuple or any(type(item) is not item_type for item in sequence):
                raise TypeError(f"{name} must be a tuple of {item_type.__name__}")
        if len({item.variable_id for item in self.variables}) != len(self.variables):
            raise ValueError("duplicate variable_id")
        if len({item.row for item in self.variables}) != len(self.variables):
            raise ValueError("duplicate Table row")
        if (
            sum(item.role == "mv" for item in self.variables) != 24
            or sum(item.role == "cv" for item in self.variables) != 36
        ):
            raise ValueError("snapshot requires 24 mv and 36 cv readings")
        if len(self.stages) != 73:
            raise ValueError("snapshot requires 73 stage readings")
        if len({item.name for item in self.stages}) != len(self.stages):
            raise ValueError("duplicate stage name")
        if len({item.name for item in self.specifications}) != len(self.specifications):
            raise ValueError("duplicate specification name")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for name in ("variables", "stages", "specifications"):
            value[name] = list(value[name])
        return {**_DECLARATIONS, **value}

    @classmethod
    def from_dict(cls, value: object) -> OperatingSnapshot:
        raw = _record(value, cls, declarations=_DECLARATIONS)
        raw["solver"] = SolverState.from_dict(raw["solver"])
        raw["variables"] = tuple(
            VariableReading.from_dict(item) for item in _array(raw["variables"], "variables")
        )
        raw["stages"] = tuple(
            StageReading.from_dict(item) for item in _array(raw["stages"], "stages")
        )
        raw["specifications"] = tuple(
            SpecificationReading.from_dict(item)
            for item in _array(raw["specifications"], "specifications")
        )
        return cls(**raw)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def read_snapshot(path: Path) -> OperatingSnapshot:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    return OperatingSnapshot.from_dict(value)


def write_snapshot(path: Path, snapshot: OperatingSnapshot) -> None:
    if type(snapshot) is not OperatingSnapshot:
        raise TypeError("snapshot must be an OperatingSnapshot")
    payload = json.dumps(
        snapshot.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
    )
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")
