from __future__ import annotations

import json
import math
from collections.abc import Mapping

import pytest

from petroleum_rto.cdu.dynamics.state import (
    ACTUATOR_STATE_NAMES,
    LIQUID_INVENTORY_NAMES,
    SENSOR_STATE_NAMES,
    THERMAL_STATE_NAMES,
    DynamicState,
    InventoryState,
)
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS


def component_masses(scale: float = 1.0) -> dict[str, float]:
    return {
        component: scale * float(index + 1)
        for index, component in enumerate(ALL_COMPONENTS)
    }


def make_inventory(name: str, scale: float = 1.0) -> InventoryState:
    return InventoryState(name, component_masses(scale), salt_mass_kg=0.25 * scale)


def make_state(
    *,
    liquid_inventories: Mapping[str, InventoryState] | None = None,
    top_gas_component_masses_kg: Mapping[str, float] | None = None,
    thermal_states: Mapping[str, float] | None = None,
    actuator_states: Mapping[str, float] | None = None,
    sensor_states: Mapping[str, float] | None = None,
) -> DynamicState:
    return DynamicState(
        liquid_inventories=(
            {
                name: make_inventory(name, float(index + 1))
                for index, name in enumerate(LIQUID_INVENTORY_NAMES)
            }
            if liquid_inventories is None
            else liquid_inventories
        ),
        top_gas_component_masses_kg=(
            component_masses(0.01)
            if top_gas_component_masses_kg is None
            else top_gas_component_masses_kg
        ),
        thermal_states=(
            {
                "furnace_outlet_temperature_k": 628.35,
                "tower_top_temperature_k": 386.65,
                "kerosene_temperature_k": 438.15,
                "light_diesel_temperature_k": 533.15,
                "heavy_diesel_temperature_k": 573.15,
                "preheater_duty_w": 18_000_000.0,
            }
            if thermal_states is None
            else thermal_states
        ),
        actuator_states=(
            {name: float(index + 1) for index, name in enumerate(ACTUATOR_STATE_NAMES)}
            if actuator_states is None
            else actuator_states
        ),
        sensor_states=(
            {
                "furnace_outlet_temperature_k": 628.35,
                "tower_top_pressure_pa": 152_325.0,
                "tower_top_temperature_k": 386.65,
                "flash_drum_inventory_kg": 10_000.0,
                "reflux_drum_inventory_kg": 8_000.0,
                "tower_bottom_inventory_kg": 20_000.0,
            }
            if sensor_states is None
            else sensor_states
        ),
    )


def test_inventory_derivations_are_strict_immutable_and_serializable() -> None:
    inventory = make_inventory("flash_drum")

    assert inventory.total_mass_kg == pytest.approx(28.0)
    assert set(inventory.mass_fractions) == set(ALL_COMPONENTS)
    assert sum(inventory.mass_fractions.values()) == pytest.approx(1.0)
    assert json.dumps(inventory.as_dict(), allow_nan=False)
    with pytest.raises(TypeError):
        inventory.component_masses_kg["naphtha"] = 1.0  # type: ignore[index]
    with pytest.raises(TypeError):
        inventory.mass_fractions["naphtha"] = 1.0  # type: ignore[index]


def test_inventory_rejects_incomplete_nonfinite_negative_and_empty_mass() -> None:
    missing = component_masses()
    del missing["water"]
    with pytest.raises(ValueError, match="keys differ"):
        InventoryState("flash_drum", missing)

    unknown = component_masses()
    unknown["unknown"] = 1.0
    with pytest.raises(ValueError, match="keys differ"):
        InventoryState("flash_drum", unknown)

    for invalid in (-1.0, math.nan, math.inf):
        masses = component_masses()
        masses["naphtha"] = invalid
        with pytest.raises(ValueError):
            InventoryState("flash_drum", masses)

    with pytest.raises(ValueError, match="bulk component mass must be positive"):
        InventoryState("flash_drum", {name: 0.0 for name in ALL_COMPONENTS})
    with pytest.raises(ValueError, match="salt mass"):
        InventoryState("flash_drum", component_masses(), salt_mass_kg=-1.0)
    with pytest.raises(ValueError, match="name"):
        InventoryState(" ", component_masses())


def test_dynamic_state_requires_exact_keys_and_matching_inventory_names() -> None:
    missing_inventory = {
        name: make_inventory(name)
        for name in LIQUID_INVENTORY_NAMES
        if name != "tower_bottom"
    }
    with pytest.raises(ValueError, match="liquid inventory keys differ"):
        make_state(liquid_inventories=missing_inventory)

    mismatched = {
        name: make_inventory(name) for name in LIQUID_INVENTORY_NAMES
    }
    mismatched["flash_drum"] = make_inventory("wrong_name")
    with pytest.raises(ValueError, match="key must match"):
        make_state(liquid_inventories=mismatched)

    missing_thermal = {
        name: 1.0
        for name in THERMAL_STATE_NAMES
        if name != "preheater_duty_w"
    }
    with pytest.raises(ValueError, match="thermal states keys differ"):
        make_state(thermal_states=missing_thermal)

    extra_actuator = {name: 1.0 for name in ACTUATOR_STATE_NAMES}
    extra_actuator["unexpected"] = 1.0
    with pytest.raises(ValueError, match="actuator states keys differ"):
        make_state(actuator_states=extra_actuator)

    missing_sensor = {
        name: 1.0
        for name in SENSOR_STATE_NAMES
        if name != "tower_top_pressure_pa"
    }
    with pytest.raises(ValueError, match="sensor states keys differ"):
        make_state(sensor_states=missing_sensor)


def test_dynamic_state_deep_freezes_every_mapping() -> None:
    state = make_state()

    with pytest.raises(TypeError):
        state.liquid_inventories["flash_drum"] = make_inventory(  # type: ignore[index]
            "flash_drum"
        )
    with pytest.raises(TypeError):
        state.top_gas_component_masses_kg["naphtha"] = 2.0  # type: ignore[index]
    with pytest.raises(TypeError):
        state.thermal_states["preheater_duty_w"] = 2.0  # type: ignore[index]
    with pytest.raises(TypeError):
        state.actuator_states["fresh_feed_flow_kg_s"] = 2.0  # type: ignore[index]
    with pytest.raises(TypeError):
        state.sensor_states["tower_top_pressure_pa"] = 2.0  # type: ignore[index]
    assert json.dumps(state.as_dict(), allow_nan=False)


def test_vector_order_dimension_and_round_trip_are_deterministic() -> None:
    state = make_state()
    names = DynamicState.vector_names()
    vector = state.to_vector()

    assert len(names) == len(vector) == 54
    assert names[0] == (
        "liquid_inventories.flash_drum.component_masses_kg.light_ends"
    )
    assert names[7] == "liquid_inventories.flash_drum.salt_mass_kg"
    assert names[-1] == "sensor_states.tower_bottom_inventory_kg"
    assert DynamicState.vector_names() == names

    restored = DynamicState.from_vector(vector)
    assert restored.to_vector() == vector
    assert restored.as_dict() == state.as_dict()


def test_from_vector_rejects_wrong_dimension_nonfinite_and_invalid_bounds() -> None:
    state = make_state()
    vector = list(state.to_vector())
    with pytest.raises(ValueError, match="must contain 54"):
        DynamicState.from_vector(vector[:-1])

    nonfinite = vector.copy()
    nonfinite[0] = math.nan
    with pytest.raises(ValueError, match="finite"):
        DynamicState.from_vector(nonfinite)

    negative_inventory = vector.copy()
    negative_inventory[0] = -1.0
    with pytest.raises(ValueError, match="non-negative"):
        DynamicState.from_vector(negative_inventory)

    zero_sensor = vector.copy()
    zero_sensor[-1] = 0.0
    with pytest.raises(ValueError, match="must be positive"):
        DynamicState.from_vector(zero_sensor)


def test_dynamic_state_enforces_domain_specific_bounds() -> None:
    zero_top_gas = {name: 0.0 for name in ALL_COMPONENTS}
    with pytest.raises(ValueError, match="top gas component mass must be positive"):
        make_state(top_gas_component_masses_kg=zero_top_gas)

    invalid_thermal = {name: 1.0 for name in THERMAL_STATE_NAMES}
    invalid_thermal["tower_top_temperature_k"] = 0.0
    with pytest.raises(ValueError, match="must be positive"):
        make_state(thermal_states=invalid_thermal)
    invalid_thermal["tower_top_temperature_k"] = 300.0
    invalid_thermal["preheater_duty_w"] = -1.0
    with pytest.raises(ValueError, match="non-negative"):
        make_state(thermal_states=invalid_thermal)

    invalid_actuator = {name: 1.0 for name in ACTUATOR_STATE_NAMES}
    invalid_actuator["residue_draw_kg_s"] = -1.0
    with pytest.raises(ValueError, match="non-negative"):
        make_state(actuator_states=invalid_actuator)

    invalid_sensor = {name: 1.0 for name in SENSOR_STATE_NAMES}
    invalid_sensor["tower_top_pressure_pa"] = 0.0
    with pytest.raises(ValueError, match="must be positive"):
        make_state(sensor_states=invalid_sensor)
