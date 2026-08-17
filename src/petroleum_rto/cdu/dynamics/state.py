"""Strict, deterministic state contracts for the reduced dynamic CDU model."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ..properties.components import ALL_COMPONENTS

LIQUID_INVENTORY_NAMES: tuple[str, ...] = (
    "flash_drum",
    "reflux_drum",
    "tower_bottom",
)
THERMAL_STATE_NAMES: tuple[str, ...] = (
    "furnace_outlet_temperature_k",
    "tower_top_temperature_k",
    "kerosene_temperature_k",
    "light_diesel_temperature_k",
    "heavy_diesel_temperature_k",
    "preheater_duty_w",
)
ACTUATOR_STATE_NAMES: tuple[str, ...] = (
    "fresh_feed_flow_kg_s",
    "flash_liquid_outflow_kg_s",
    "gasoline_draw_kg_s",
    "reflux_flow_kg_s",
    "residue_draw_kg_s",
    "top_gas_vent_kg_s",
    "furnace_fuel_duty_w",
    "condenser_cooling_duty_w",
    "pump_around_1_duty_w",
    "pump_around_2_duty_w",
    "pump_around_3_duty_w",
)
SENSOR_STATE_NAMES: tuple[str, ...] = (
    "furnace_outlet_temperature_k",
    "tower_top_pressure_pa",
    "tower_top_temperature_k",
    "flash_drum_inventory_kg",
    "reflux_drum_inventory_kg",
    "tower_bottom_inventory_kg",
)


def _finite_number(value: object, *, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{context} must be a finite number")
    return float(value)


def _exact_numeric_mapping(
    values: Mapping[str, float],
    expected_names: tuple[str, ...],
    *,
    context: str,
    strictly_positive: bool,
) -> Mapping[str, float]:
    if any(not isinstance(name, str) for name in values):
        raise TypeError(f"{context} keys must be strings")
    actual = set(values)
    expected = set(expected_names)
    if actual != expected:
        raise ValueError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    copied: dict[str, float] = {}
    for name in expected_names:
        number = _finite_number(values[name], context=f"{context}.{name}")
        if strictly_positive:
            if number <= 0.0:
                raise ValueError(f"{context}.{name} must be positive")
        elif number < 0.0:
            raise ValueError(f"{context}.{name} must be non-negative")
        copied[name] = number
    return MappingProxyType(copied)


@dataclass(frozen=True)
class InventoryState:
    """One liquid bulk inventory plus an independently conserved salt mass."""

    name: str
    component_masses_kg: Mapping[str, float]
    salt_mass_kg: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("inventory name must be a non-empty string")
        component_masses = _exact_numeric_mapping(
            self.component_masses_kg,
            ALL_COMPONENTS,
            context=f"inventory {self.name!r} component masses",
            strictly_positive=False,
        )
        if sum(component_masses.values()) <= 0.0:
            raise ValueError("inventory bulk component mass must be positive")
        salt_mass = _finite_number(
            self.salt_mass_kg,
            context=f"inventory {self.name!r} salt mass",
        )
        if salt_mass < 0.0:
            raise ValueError("inventory salt mass must be non-negative")
        object.__setattr__(self, "component_masses_kg", component_masses)
        object.__setattr__(self, "salt_mass_kg", salt_mass)

    @property
    def total_mass_kg(self) -> float:
        """Return bulk liquid mass; the trace salt mass is reported separately."""

        return sum(self.component_masses_kg[name] for name in ALL_COMPONENTS)

    @property
    def mass_fractions(self) -> Mapping[str, float]:
        total = self.total_mass_kg
        return MappingProxyType(
            {
                name: self.component_masses_kg[name] / total
                for name in ALL_COMPONENTS
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "component_masses_kg": dict(self.component_masses_kg),
            "salt_mass_kg": self.salt_mass_kg,
            "total_mass_kg": self.total_mass_kg,
            "mass_fractions": dict(self.mass_fractions),
        }


@dataclass(frozen=True)
class DynamicState:
    """Complete numeric state with a stable 54-element vector representation."""

    liquid_inventories: Mapping[str, InventoryState]
    top_gas_component_masses_kg: Mapping[str, float]
    thermal_states: Mapping[str, float]
    actuator_states: Mapping[str, float]
    sensor_states: Mapping[str, float]

    def __post_init__(self) -> None:
        if any(not isinstance(name, str) for name in self.liquid_inventories):
            raise TypeError("liquid inventory keys must be strings")
        actual_inventory_names = set(self.liquid_inventories)
        expected_inventory_names = set(LIQUID_INVENTORY_NAMES)
        if actual_inventory_names != expected_inventory_names:
            raise ValueError(
                "liquid inventory keys differ; "
                f"missing={sorted(expected_inventory_names - actual_inventory_names)}, "
                f"unknown={sorted(actual_inventory_names - expected_inventory_names)}"
            )
        inventories: dict[str, InventoryState] = {}
        for name in LIQUID_INVENTORY_NAMES:
            inventory = self.liquid_inventories[name]
            if not isinstance(inventory, InventoryState):
                raise TypeError("liquid inventory values must be InventoryState instances")
            if inventory.name != name:
                raise ValueError("liquid inventory key must match InventoryState.name")
            inventories[name] = inventory

        top_gas = _exact_numeric_mapping(
            self.top_gas_component_masses_kg,
            ALL_COMPONENTS,
            context="top gas component masses",
            strictly_positive=False,
        )
        if sum(top_gas.values()) <= 0.0:
            raise ValueError("top gas component mass must be positive")

        thermal = _exact_numeric_mapping(
            self.thermal_states,
            THERMAL_STATE_NAMES,
            context="thermal states",
            strictly_positive=False,
        )
        for name in THERMAL_STATE_NAMES[:-1]:
            if thermal[name] <= 0.0:
                raise ValueError(f"thermal states.{name} must be positive")

        actuators = _exact_numeric_mapping(
            self.actuator_states,
            ACTUATOR_STATE_NAMES,
            context="actuator states",
            strictly_positive=False,
        )
        sensors = _exact_numeric_mapping(
            self.sensor_states,
            SENSOR_STATE_NAMES,
            context="sensor states",
            strictly_positive=True,
        )

        object.__setattr__(
            self,
            "liquid_inventories",
            MappingProxyType(inventories),
        )
        object.__setattr__(self, "top_gas_component_masses_kg", top_gas)
        object.__setattr__(self, "thermal_states", thermal)
        object.__setattr__(self, "actuator_states", actuators)
        object.__setattr__(self, "sensor_states", sensors)

    @staticmethod
    def vector_names() -> tuple[str, ...]:
        names: list[str] = []
        for inventory_name in LIQUID_INVENTORY_NAMES:
            names.extend(
                f"liquid_inventories.{inventory_name}.component_masses_kg.{component}"
                for component in ALL_COMPONENTS
            )
            names.append(f"liquid_inventories.{inventory_name}.salt_mass_kg")
        names.extend(
            f"top_gas_component_masses_kg.{component}"
            for component in ALL_COMPONENTS
        )
        names.extend(f"thermal_states.{name}" for name in THERMAL_STATE_NAMES)
        names.extend(f"actuator_states.{name}" for name in ACTUATOR_STATE_NAMES)
        names.extend(f"sensor_states.{name}" for name in SENSOR_STATE_NAMES)
        return tuple(names)

    def to_vector(self) -> tuple[float, ...]:
        values: list[float] = []
        for inventory_name in LIQUID_INVENTORY_NAMES:
            inventory = self.liquid_inventories[inventory_name]
            values.extend(
                inventory.component_masses_kg[component]
                for component in ALL_COMPONENTS
            )
            values.append(inventory.salt_mass_kg)
        values.extend(
            self.top_gas_component_masses_kg[component]
            for component in ALL_COMPONENTS
        )
        values.extend(self.thermal_states[name] for name in THERMAL_STATE_NAMES)
        values.extend(self.actuator_states[name] for name in ACTUATOR_STATE_NAMES)
        values.extend(self.sensor_states[name] for name in SENSOR_STATE_NAMES)
        return tuple(values)

    @classmethod
    def from_vector(cls, values: Sequence[float]) -> DynamicState:
        vector = tuple(
            _finite_number(value, context=f"state vector element {index}")
            for index, value in enumerate(values)
        )
        expected_length = len(cls.vector_names())
        if len(vector) != expected_length:
            raise ValueError(
                f"state vector must contain {expected_length} values, got {len(vector)}"
            )

        index = 0
        inventories: dict[str, InventoryState] = {}
        for inventory_name in LIQUID_INVENTORY_NAMES:
            component_masses = {
                component: vector[index + offset]
                for offset, component in enumerate(ALL_COMPONENTS)
            }
            index += len(ALL_COMPONENTS)
            salt_mass = vector[index]
            index += 1
            inventories[inventory_name] = InventoryState(
                inventory_name,
                component_masses,
                salt_mass,
            )

        top_gas = {
            component: vector[index + offset]
            for offset, component in enumerate(ALL_COMPONENTS)
        }
        index += len(ALL_COMPONENTS)
        thermal = {
            name: vector[index + offset]
            for offset, name in enumerate(THERMAL_STATE_NAMES)
        }
        index += len(THERMAL_STATE_NAMES)
        actuators = {
            name: vector[index + offset]
            for offset, name in enumerate(ACTUATOR_STATE_NAMES)
        }
        index += len(ACTUATOR_STATE_NAMES)
        sensors = {
            name: vector[index + offset]
            for offset, name in enumerate(SENSOR_STATE_NAMES)
        }
        return cls(inventories, top_gas, thermal, actuators, sensors)

    def as_dict(self) -> dict[str, object]:
        return {
            "liquid_inventories": {
                name: self.liquid_inventories[name].as_dict()
                for name in LIQUID_INVENTORY_NAMES
            },
            "top_gas_component_masses_kg": dict(
                self.top_gas_component_masses_kg
            ),
            "thermal_states": dict(self.thermal_states),
            "actuator_states": dict(self.actuator_states),
            "sensor_states": dict(self.sensor_states),
        }
