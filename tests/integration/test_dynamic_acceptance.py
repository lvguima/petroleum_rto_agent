from __future__ import annotations

import math
from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import (
    load_case_config,
    load_component_catalog,
    load_model_config,
)
from petroleum_rto.cdu.dynamics.equations import OpenLoopDynamicModel
from petroleum_rto.cdu.dynamics.initialization import initialize_dynamic_model
from petroleum_rto.cdu.dynamics.schedule import CommandEvent, CommandSchedule
from petroleum_rto.cdu.dynamics.simulation import DynamicSimulationResult, simulate_dynamic
from petroleum_rto.cdu.dynamics.state import ACTUATOR_STATE_NAMES, DynamicState
from petroleum_rto.cdu.flowsheet.recycle import solve_recycle

_STEP_SCENARIO_VERSION = "m3-all-actuator-step-acceptance-v0.1.0"
_TIME_STEP_SCENARIO_VERSION = "m3-time-step-acceptance-v0.1.0"


@pytest.fixture(scope="module")
def acceptance_model(repo_root: Path) -> OpenLoopDynamicModel:
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")
    catalog = load_component_catalog(repo_root / model.component_catalog_path)
    return initialize_dynamic_model(model, case, catalog, solve_recycle(model, case, catalog))


def _run_step(
    model: OpenLoopDynamicModel,
    actuator_name: str,
    *,
    multiplier: float,
    duration_s: float = 600.0,
    dt_s: float = 1.0,
) -> DynamicSimulationResult:
    schedule = CommandSchedule(
        model.baseline_commands,
        (
            CommandEvent(
                0.0,
                actuator_name,
                multiplier * model.baseline_commands[actuator_name],
            ),
        ),
    )
    return simulate_dynamic(
        model,
        schedule,
        duration_s,
        dt_s,
        fingerprint=model.input_fingerprint,
        versions=model.versions,
        metadata={
            "scenario_name": f"{actuator_name}_{multiplier:.2f}",
            "scenario_version": _STEP_SCENARIO_VERSION,
            "purpose": "M3 real-model bidirectional actuator acceptance",
        },
    ).require_success()


def _inventory_kg(state: DynamicState, name: str) -> float:
    return state.liquid_inventories[name].total_mass_kg


def _key_values(
    model: OpenLoopDynamicModel,
    state: DynamicState,
) -> tuple[float, ...]:
    return (
        _inventory_kg(state, "flash_drum"),
        _inventory_kg(state, "reflux_drum"),
        _inventory_kg(state, "tower_bottom"),
        model.top_pressure_pa(state),
        state.thermal_states["furnace_outlet_temperature_k"],
        state.thermal_states["tower_top_temperature_k"],
        state.thermal_states["kerosene_temperature_k"],
        state.thermal_states["light_diesel_temperature_k"],
        state.thermal_states["heavy_diesel_temperature_k"],
    )


def _response_value(
    model: OpenLoopDynamicModel,
    state: DynamicState,
    actuator_name: str,
) -> float:
    if actuator_name in {"fresh_feed_flow_kg_s", "flash_liquid_outflow_kg_s"}:
        return _inventory_kg(state, "flash_drum")
    if actuator_name in {"gasoline_draw_kg_s", "reflux_flow_kg_s"}:
        return _inventory_kg(state, "reflux_drum")
    if actuator_name == "residue_draw_kg_s":
        return _inventory_kg(state, "tower_bottom")
    if actuator_name in {"top_gas_vent_kg_s", "condenser_cooling_duty_w"}:
        return model.top_pressure_pa(state)
    if actuator_name == "furnace_fuel_duty_w":
        return state.thermal_states["furnace_outlet_temperature_k"]
    if actuator_name == "pump_around_1_duty_w":
        return state.thermal_states["tower_top_temperature_k"]
    if actuator_name == "pump_around_2_duty_w":
        return state.thermal_states["light_diesel_temperature_k"]
    if actuator_name == "pump_around_3_duty_w":
        return state.thermal_states["heavy_diesel_temperature_k"]
    raise AssertionError(f"missing response metric for {actuator_name!r}")


def _assert_successful_conservative_step(result: DynamicSimulationResult) -> None:
    assert result.status == "success"
    assert result.balance.passed(mass_atol_kg=1e-5, salt_atol_kg=1e-8)
    assert result.diagnostics["max_instantaneous_component_residual_kg_s"] <= 1e-8
    assert all(
        math.isfinite(value) and value >= 0.0
        for sample in result.samples
        for value in sample.state.to_vector()
    )


def test_real_command_steps_are_continuous_conservative_and_directional(
    acceptance_model: OpenLoopDynamicModel,
) -> None:
    initial = acceptance_model.initial_state
    positive_command_response_direction = {
        "fresh_feed_flow_kg_s": 1.0,
        "flash_liquid_outflow_kg_s": -1.0,
        "gasoline_draw_kg_s": -1.0,
        "reflux_flow_kg_s": -1.0,
        "residue_draw_kg_s": -1.0,
        "top_gas_vent_kg_s": -1.0,
        "furnace_fuel_duty_w": 1.0,
        "condenser_cooling_duty_w": -1.0,
        "pump_around_1_duty_w": -1.0,
        "pump_around_2_duty_w": -1.0,
        "pump_around_3_duty_w": -1.0,
    }
    assert set(positive_command_response_direction) == set(ACTUATOR_STATE_NAMES)
    results = {
        (name, multiplier): _run_step(
            acceptance_model,
            name,
            multiplier=multiplier,
        )
        for name in ACTUATOR_STATE_NAMES
        for multiplier in (0.95, 1.05)
    }
    for (actuator_name, multiplier), result in results.items():
        _assert_successful_conservative_step(result)
        baseline = acceptance_model.baseline_commands[actuator_name]
        first_actual = result.samples[1].state.actuator_states[actuator_name]
        command = multiplier * baseline
        response_fraction = (first_actual - baseline) / (command - baseline)
        assert 0.0 < response_fraction < 1.0
        assert "stream_mass_flows_kg_s" in result.samples[-1].evaluation
        assert "product_component_flows_kg_s" in result.samples[-1].evaluation
        assert "product_quality_proxies" in result.samples[-1].evaluation
        initial_response = _response_value(
            acceptance_model,
            initial,
            actuator_name,
        )
        for response_sample in (result.samples[60], result.samples[-1]):
            response = _response_value(
                acceptance_model,
                response_sample.state,
                actuator_name,
            )
            assert (
                (response - initial_response)
                * positive_command_response_direction[actuator_name]
                * (multiplier - 1.0)
                > 0.0
            )


def test_real_model_time_step_halving_meets_common_time_and_terminal_limits(
    acceptance_model: OpenLoopDynamicModel,
) -> None:
    command_name = "furnace_fuel_duty_w"
    command_value = 1.05 * acceptance_model.baseline_commands[command_name]
    schedule = CommandSchedule(
        acceptance_model.baseline_commands,
        (CommandEvent(0.0, command_name, command_value),),
    )
    coarse = simulate_dynamic(
        acceptance_model,
        schedule,
        600.0,
        1.0,
        fingerprint=acceptance_model.input_fingerprint,
        versions=acceptance_model.versions,
        metadata={
            "scenario_name": "furnace_fuel_time_step_coarse",
            "scenario_version": _TIME_STEP_SCENARIO_VERSION,
            "purpose": "M3 real-model time-step sensitivity acceptance",
        },
    ).require_success()
    fine = simulate_dynamic(
        acceptance_model,
        schedule,
        600.0,
        0.5,
        fingerprint=acceptance_model.input_fingerprint,
        versions=acceptance_model.versions,
        metadata={
            "scenario_name": "furnace_fuel_time_step_fine",
            "scenario_version": _TIME_STEP_SCENARIO_VERSION,
            "purpose": "M3 real-model time-step sensitivity acceptance",
        },
    ).require_success()

    nominal = _key_values(acceptance_model, acceptance_model.initial_state)
    maximum_common_time_difference = 0.0
    for coarse_sample, fine_sample in zip(coarse.samples, fine.samples[::2], strict=True):
        coarse_values = _key_values(acceptance_model, coarse_sample.state)
        fine_values = _key_values(acceptance_model, fine_sample.state)
        maximum_common_time_difference = max(
            maximum_common_time_difference,
            *(
                abs(coarse_value - fine_value) / max(abs(scale), 1.0)
                for coarse_value, fine_value, scale in zip(
                    coarse_values,
                    fine_values,
                    nominal,
                    strict=True,
                )
            ),
        )
    terminal_difference = max(
        abs(coarse_value - fine_value) / max(abs(scale), 1.0)
        for coarse_value, fine_value, scale in zip(
            _key_values(acceptance_model, coarse.samples[-1].state),
            _key_values(acceptance_model, fine.samples[-1].state),
            nominal,
            strict=True,
        )
    )

    assert maximum_common_time_difference <= 0.005
    assert terminal_difference <= 0.002
