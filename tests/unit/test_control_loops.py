from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.control.config import (
    REQUIRED_CONTROL_LOOP_IDS,
    ControlConfig,
    load_control_config,
)
from petroleum_rto.cdu.control.controllers import PIControllerUpdate
from petroleum_rto.cdu.control.loops import (
    ControlLoopAssembly,
    FurnaceFeedforward,
    assemble_control_loops,
)
from petroleum_rto.cdu.dynamics.state import (
    ACTUATOR_STATE_NAMES,
    SENSOR_STATE_NAMES,
    THERMAL_STATE_NAMES,
    DynamicState,
    InventoryState,
)
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS

_FURNACE_EFFICIENCY = 0.85
_FURNACE_HEAT_LOSS_W = 1_000_000.0


def _component_masses(total_mass_kg: float) -> dict[str, float]:
    return {
        component: total_mass_kg if index == 0 else 0.0
        for index, component in enumerate(ALL_COMPONENTS)
    }


def _nominal_state() -> DynamicState:
    baseline_commands = _baseline_commands()
    inventories = {
        "flash_drum": InventoryState(
            "flash_drum",
            _component_masses(6_000.0),
        ),
        "reflux_drum": InventoryState(
            "reflux_drum",
            _component_masses(9_000.0),
        ),
        "tower_bottom": InventoryState(
            "tower_bottom",
            _component_masses(12_000.0),
        ),
    }
    thermal_states = {
        "furnace_outlet_temperature_k": 630.0,
        "tower_top_temperature_k": 386.0,
        "kerosene_temperature_k": 440.0,
        "light_diesel_temperature_k": 530.0,
        "heavy_diesel_temperature_k": 575.0,
        "preheater_duty_w": 11_000_000.0,
    }
    assert set(thermal_states) == set(THERMAL_STATE_NAMES)
    sensor_states = {
        "furnace_outlet_temperature_k": 630.0,
        "tower_top_pressure_pa": 152_325.0,
        "tower_top_temperature_k": 386.0,
        "flash_drum_inventory_kg": 6_000.0,
        "reflux_drum_inventory_kg": 9_000.0,
        "tower_bottom_inventory_kg": 12_000.0,
    }
    assert set(sensor_states) == set(SENSOR_STATE_NAMES)
    return DynamicState(
        liquid_inventories=inventories,
        top_gas_component_masses_kg=_component_masses(100.0),
        thermal_states=thermal_states,
        actuator_states=baseline_commands,
        sensor_states=sensor_states,
    )


def _baseline_commands() -> dict[str, float]:
    commands = {
        "fresh_feed_flow_kg_s": 100.0,
        "flash_liquid_outflow_kg_s": 90.0,
        "gasoline_draw_kg_s": 15.0,
        "reflux_flow_kg_s": 7.0,
        "residue_draw_kg_s": 50.0,
        "top_gas_vent_kg_s": 1.0,
        "furnace_fuel_duty_w": 20_000_000.0,
        "condenser_cooling_duty_w": 12_000_000.0,
        "pump_around_1_duty_w": 8_000_000.0,
        "pump_around_2_duty_w": 10_000_000.0,
        "pump_around_3_duty_w": 8_000_000.0,
    }
    assert set(commands) == set(ACTUATOR_STATE_NAMES)
    return commands


def _state_with_value(state: DynamicState, vector_name: str, value: float) -> DynamicState:
    vector = list(state.to_vector())
    vector[DynamicState.vector_names().index(vector_name)] = value
    return DynamicState.from_vector(vector)


@pytest.fixture
def control_config(repo_root: Path) -> ControlConfig:
    return load_control_config(repo_root / "configs/controllers/cdu_pi_v0.1.0.json")


@pytest.fixture
def assembly(control_config: ControlConfig) -> ControlLoopAssembly:
    return assemble_control_loops(
        control_config,
        _nominal_state(),
        _baseline_commands(),
        furnace_efficiency=_FURNACE_EFFICIENCY,
        furnace_heat_loss_w=_FURNACE_HEAT_LOSS_W,
    )


def _nominal_updates(
    assembly: ControlLoopAssembly,
    state: DynamicState,
) -> dict[str, PIControllerUpdate]:
    ratios = {loop_id: 1.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS}
    process_values = assembly.process_values(state)
    setpoints = assembly.target_setpoints(ratios)
    feedforwards = assembly.feedforward_outputs(state)
    return {
        loop_id: loop.controller.update(
            loop.initial_state,
            process_value=process_values[loop_id],
            target_setpoint=setpoints[loop_id],
            dt_s=0.0,
            feedforward_output=feedforwards[loop_id],
        )
        for loop_id, loop in assembly.loops.items()
    }


def test_assembly_builds_seven_scaled_bumpless_controllers(
    assembly: ControlLoopAssembly,
) -> None:
    assert tuple(assembly.loops) == REQUIRED_CONTROL_LOOP_IDS
    assert len(assembly.manipulated_variable_owners) == len(REQUIRED_CONTROL_LOOP_IDS)
    assert assembly.initial_target_setpoint_ratios == {
        loop_id: 1.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS
    }
    assert set(assembly.manipulated_variable_owners) == {
        "fresh_feed_flow_kg_s",
        "flash_liquid_outflow_kg_s",
        "furnace_fuel_duty_w",
        "top_gas_vent_kg_s",
        "gasoline_draw_kg_s",
        "residue_draw_kg_s",
        "pump_around_1_duty_w",
    }
    for loop_id, loop in assembly.loops.items():
        assert loop.controller.pv_scale == loop.nominal_process_value
        assert loop.controller.output_scale == loop.nominal_output
        assert loop.initial_state.output_normalized == pytest.approx(1.0)
        assert loop.initial_diagnostic.target_setpoint_ratio == 1.0
        assert loop.initial_diagnostic.initial_command_delta == pytest.approx(0.0)
        assert loop.initial_diagnostic.output == assembly.baseline_commands[
            loop.manipulated_variable
        ]
        assert assembly.initial_controller_states[loop_id] is loop.initial_state
        assert assembly.initial_diagnostics[loop_id] is loop.initial_diagnostic


def test_process_values_use_actual_feed_and_six_sensor_states(
    assembly: ControlLoopAssembly,
) -> None:
    state = _nominal_state()
    values = assembly.process_values(state)
    assert values == {
        "feed_flow": state.actuator_states["fresh_feed_flow_kg_s"],
        "flash_inventory": state.sensor_states["flash_drum_inventory_kg"],
        "furnace_temperature": state.sensor_states["furnace_outlet_temperature_k"],
        "top_pressure": state.sensor_states["tower_top_pressure_pa"],
        "reflux_inventory": state.sensor_states["reflux_drum_inventory_kg"],
        "bottom_inventory": state.sensor_states["tower_bottom_inventory_kg"],
        "top_temperature": state.sensor_states["tower_top_temperature_k"],
    }


def test_target_ratios_are_converted_with_each_nominal_pv_scale(
    assembly: ControlLoopAssembly,
) -> None:
    ratios = {loop_id: 1.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS}
    ratios["feed_flow"] = 1.05
    setpoints = assembly.target_setpoints(ratios)
    assert setpoints["feed_flow"] == pytest.approx(105.0)
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        expected_ratio = 1.05 if loop_id == "feed_flow" else 1.0
        assert setpoints[loop_id] == pytest.approx(
            expected_ratio * assembly.loops[loop_id].nominal_process_value
        )


def test_furnace_feedforward_uses_actual_flash_outflow_and_is_nominally_unbiased(
    assembly: ControlLoopAssembly,
) -> None:
    nominal = assembly.feedforward_outputs(_nominal_state())
    assert nominal == {
        loop_id: 0.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS
    }

    state = _state_with_value(
        _nominal_state(),
        "actuator_states.flash_liquid_outflow_kg_s",
        1.05 * 90.0,
    )
    outputs = assembly.feedforward_outputs(state)
    expected_fuel = (
        _FURNACE_HEAT_LOSS_W
        + (
            _FURNACE_EFFICIENCY * 20_000_000.0
            - _FURNACE_HEAT_LOSS_W
        )
        * 1.05
    ) / _FURNACE_EFFICIENCY
    assert outputs["furnace_temperature"] == pytest.approx(
        expected_fuel - 20_000_000.0
    )
    assert all(
        output == 0.0
        for loop_id, output in outputs.items()
        if loop_id != "furnace_temperature"
    )


def test_true_inventory_ratios_ignore_lagged_inventory_measurements(
    assembly: ControlLoopAssembly,
) -> None:
    state = _state_with_value(
        _nominal_state(),
        "sensor_states.flash_drum_inventory_kg",
        7_500.0,
    )
    assert assembly.process_values(state)["flash_inventory"] == 7_500.0
    assert assembly.true_inventory_ratios(state) == {
        "flash_inventory": 1.0,
        "reflux_inventory": 1.0,
        "bottom_inventory": 1.0,
    }


def test_commands_replace_each_owned_mv_once_and_keep_independent_commands(
    assembly: ControlLoopAssembly,
) -> None:
    state = _nominal_state()
    updates = _nominal_updates(assembly, state)
    feed_loop = assembly.loops["feed_flow"]
    first = feed_loop.controller.update(
        feed_loop.initial_state,
        process_value=100.0,
        target_setpoint=105.0,
        dt_s=0.0,
    )
    second = feed_loop.controller.update(
        first.state,
        process_value=100.0,
        target_setpoint=105.0,
        dt_s=1.0,
    )
    updates["feed_flow"] = second

    commands = assembly.commands_from_updates(updates)
    assert commands["fresh_feed_flow_kg_s"] > 100.0
    for command_name in (
        "reflux_flow_kg_s",
        "condenser_cooling_duty_w",
        "pump_around_2_duty_w",
        "pump_around_3_duty_w",
    ):
        assert commands[command_name] == assembly.baseline_commands[command_name]
    for loop_id, loop in assembly.loops.items():
        assert commands[loop.manipulated_variable] == updates[loop_id].output


def test_builder_rejects_incomplete_or_nonpositive_nominal_scales(
    control_config: ControlConfig,
) -> None:
    incomplete = _baseline_commands()
    incomplete.pop("pump_around_3_duty_w")
    with pytest.raises(ValueError, match="baseline_commands keys differ"):
        assemble_control_loops(
            control_config,
            _nominal_state(),
            incomplete,
            furnace_efficiency=_FURNACE_EFFICIENCY,
            furnace_heat_loss_w=_FURNACE_HEAT_LOSS_W,
        )

    baseline = _baseline_commands()
    baseline["pump_around_1_duty_w"] = 0.0
    state = _state_with_value(
        _nominal_state(),
        "actuator_states.pump_around_1_duty_w",
        0.0,
    )
    with pytest.raises(ValueError, match="pump_around_1_duty_w must be positive"):
        assemble_control_loops(
            control_config,
            state,
            baseline,
            furnace_efficiency=_FURNACE_EFFICIENCY,
            furnace_heat_loss_w=_FURNACE_HEAT_LOSS_W,
        )


def test_config_requires_nominal_output_strictly_inside_envelope(
    control_config: ControlConfig,
) -> None:
    with pytest.raises(ValueError, match="strictly bracket nominal"):
        replace(
            control_config.loop("feed_flow"),
            output_min_ratio=1.0,
        )


def test_builder_rejects_initial_actuator_command_mismatch(
    control_config: ControlConfig,
) -> None:
    state = _state_with_value(
        _nominal_state(),
        "actuator_states.gasoline_draw_kg_s",
        16.0,
    )
    with pytest.raises(ValueError, match="must equal its baseline command"):
        assemble_control_loops(
            control_config,
            state,
            _baseline_commands(),
            furnace_efficiency=_FURNACE_EFFICIENCY,
            furnace_heat_loss_w=_FURNACE_HEAT_LOSS_W,
        )


@pytest.mark.parametrize(
    ("efficiency", "heat_loss_w", "message"),
    [
        (0.0, 0.0, "efficiency must be positive"),
        (1.1, 0.0, "efficiency cannot exceed one"),
        (0.85, -1.0, "heat_loss_w must be non-negative"),
        (0.85, 18_000_000.0, "below the furnace heat-loss threshold"),
    ],
)
def test_furnace_feedforward_rejects_invalid_nominal_physics(
    efficiency: float,
    heat_loss_w: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        FurnaceFeedforward(
            nominal_feed_flow_kg_s=90.0,
            nominal_fuel_duty_w=20_000_000.0,
            efficiency=efficiency,
            heat_loss_w=heat_loss_w,
        )


def test_complete_ratio_and_update_mappings_are_required(
    assembly: ControlLoopAssembly,
) -> None:
    incomplete_ratios: Mapping[str, float] = {
        loop_id: 1.0
        for loop_id in REQUIRED_CONTROL_LOOP_IDS
        if loop_id != "top_temperature"
    }
    with pytest.raises(ValueError, match="target_setpoint_ratios keys differ"):
        assembly.target_setpoints(incomplete_ratios)

    updates = _nominal_updates(assembly, _nominal_state())
    updates.pop("top_temperature")
    with pytest.raises(ValueError, match="exactly the seven M4 loop ids"):
        assembly.commands_from_updates(updates)
