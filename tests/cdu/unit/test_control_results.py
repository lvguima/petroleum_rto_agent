from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from petroleum_rto.cdu.control.config import REQUIRED_CONTROL_LOOP_IDS
from petroleum_rto.cdu.control.results import (
    ClosedLoopSample,
    ClosedLoopSimulationResult,
    ControlLoopRecord,
    LoopPerformance,
)
from petroleum_rto.cdu.dynamics.simulation import (
    DynamicConservationTolerances,
    DynamicCumulativeBalance,
    DynamicSample,
)
from petroleum_rto.cdu.dynamics.state import (
    ACTUATOR_STATE_NAMES,
    LIQUID_INVENTORY_NAMES,
    SENSOR_STATE_NAMES,
    THERMAL_STATE_NAMES,
    DynamicState,
    InventoryState,
)
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS

_MASS_RESIDUAL = 1e-9
_SALT_RESIDUAL = 1e-11


def _state(
    *,
    component_loss_kg: float = 0.0,
    salt_loss_kg: float = 0.0,
) -> DynamicState:
    inventories: dict[str, InventoryState] = {}
    for name in LIQUID_INVENTORY_NAMES:
        component_masses = {component: 10.0 for component in ALL_COMPONENTS}
        salt_mass_kg = 1.0
        if name == "flash_drum":
            component_masses["naphtha"] -= component_loss_kg
            salt_mass_kg -= salt_loss_kg
        inventories[name] = InventoryState(
            name,
            component_masses,
            salt_mass_kg=salt_mass_kg,
        )
    return DynamicState(
        liquid_inventories=inventories,
        top_gas_component_masses_kg={component: 1.0 for component in ALL_COMPONENTS},
        thermal_states={name: 500.0 for name in THERMAL_STATE_NAMES},
        actuator_states={name: 1.0 for name in ACTUATOR_STATE_NAMES},
        sensor_states={name: 1.0 for name in SENSOR_STATE_NAMES},
    )


def _control_record(*, mode: str = "automatic") -> ControlLoopRecord:
    return ControlLoopRecord(
        target_setpoint=1.0,
        ramped_setpoint=1.0,
        process_value=1.0,
        decision_process_value=1.0,
        error_normalized=0.0,
        decision_error_normalized=0.0,
        proportional_term_normalized=0.0,
        integral_term_normalized=0.0,
        feedforward_normalized=0.0,
        unconstrained_output_normalized=1.0,
        magnitude_limited_output_normalized=1.0,
        output_normalized=1.0,
        output=1.0,
        mode=mode,
    )


def _controls() -> dict[str, ControlLoopRecord]:
    return {loop_id: _control_record() for loop_id in REQUIRED_CONTROL_LOOP_IDS}


def _plant_sample(
    time_s: float,
    *,
    commands: Mapping[str, float] | None = None,
) -> DynamicSample:
    cumulative = {component: time_s for component in ALL_COMPONENTS}
    residuals = {component: 0.0 for component in ALL_COMPONENTS}
    cumulative_mass_residual = 0.0 if time_s == 0.0 else _MASS_RESIDUAL
    cumulative_salt_residual = 0.0 if time_s == 0.0 else _SALT_RESIDUAL
    residuals["naphtha"] = cumulative_mass_residual
    return DynamicSample(
        time_s=time_s,
        state=_state(
            component_loss_kg=cumulative_mass_residual,
            salt_loss_kg=cumulative_salt_residual,
        ),
        commands=(
            {name: 1.0 for name in ACTUATOR_STATE_NAMES}
            if commands is None
            else commands
        ),
        evaluation={},
        cumulative_component_in_kg=cumulative,
        cumulative_component_out_kg=cumulative,
        component_balance_residuals_kg=residuals,
        cumulative_salt_in_kg=time_s,
        cumulative_salt_out_kg=time_s,
        mass_balance_residual_kg=cumulative_mass_residual,
        salt_balance_residual_kg=cumulative_salt_residual,
        instantaneous_mass_residual_kg_s=_MASS_RESIDUAL,
        instantaneous_max_component_residual_kg_s=_MASS_RESIDUAL,
        instantaneous_salt_residual_kg_s=_SALT_RESIDUAL,
    )


def _sample(time_s: float) -> ClosedLoopSample:
    return ClosedLoopSample(plant=_plant_sample(time_s), controls=_controls())


def _passing_performance() -> LoopPerformance:
    return LoopPerformance(
        normalized_iae_s=0.0,
        overshoot_fraction_of_pv_scale=0.0,
        settling_time_s=0.0,
        final_error_fraction=0.0,
        tail_mean_absolute_error_fraction=0.0,
        tail_slope_fraction_per_s=0.0,
        tail_peak_to_peak_fraction=0.0,
        saturation_time_s=0.0,
        longest_continuous_saturation_s=0.0,
        passed=True,
    )


def _inventory_components(state: DynamicState) -> dict[str, float]:
    return {
        component: (
            sum(
                inventory.component_masses_kg[component]
                for inventory in state.liquid_inventories.values()
            )
            + state.top_gas_component_masses_kg[component]
        )
        for component in ALL_COMPONENTS
    }


def _inventory_salt(state: DynamicState) -> float:
    return sum(
        inventory.salt_mass_kg for inventory in state.liquid_inventories.values()
    )


def _balance() -> DynamicCumulativeBalance:
    initial_state = _state()
    final_state = _state(
        component_loss_kg=_MASS_RESIDUAL,
        salt_loss_kg=_SALT_RESIDUAL,
    )
    cumulative = {component: 1.0 for component in ALL_COMPONENTS}
    return DynamicCumulativeBalance(
        initial_component_inventory_kg=_inventory_components(initial_state),
        final_component_inventory_kg=_inventory_components(final_state),
        cumulative_component_in_kg=cumulative,
        cumulative_component_out_kg=cumulative,
        initial_inventory_salt_kg=_inventory_salt(initial_state),
        final_inventory_salt_kg=_inventory_salt(final_state),
        cumulative_salt_in_kg=1.0,
        cumulative_salt_out_kg=1.0,
    )


def _result() -> ClosedLoopSimulationResult:
    return ClosedLoopSimulationResult(
        status="success",
        samples=(_sample(0.0), _sample(1.0)),
        balance=_balance(),
        conservation_tolerances=DynamicConservationTolerances(),
        loop_performance={
            loop_id: _passing_performance()
            for loop_id in REQUIRED_CONTROL_LOOP_IDS
        },
        acceptance_checks={
            "plant_execution": True,
            "plant_conservation": True,
            "automatic_initialization_no_bump": True,
            "baseline_hold": True,
            "loop_performance": True,
            "true_inventory_safety": True,
        },
        diagnostics={"maximum_residual": _MASS_RESIDUAL},
        versions={
            "software_version": "test-software-v1",
            "model_version": "test-model-v1",
            "parameter_set_version": "test-parameters-v1",
            "config_version": "test-config-v1",
            "case_version": "test-case-v1",
            "scenario_version": "test-scenario-v1",
            "control_version": "test-control-v1",
            "simulation_stage": "M4",
        },
        metadata={
            "scenario_name": "test_scenario",
            "scenario_version": "test-scenario-v1",
            "purpose": "closed-loop result contract test",
            "synthetic": "true",
            "data_origin": "M4_closed_loop_simulation",
        },
        source_fingerprint="a" * 64,
        control_fingerprint="b" * 64,
        input_fingerprint="c" * 64,
        requested_duration_s=1.0,
        time_step_s=1.0,
        control_interval_s=1.0,
    )


def test_closed_loop_sample_requires_exact_m3_command_names() -> None:
    sample = _sample(0.0)
    missing = dict(sample.plant.commands)
    del missing[ACTUATOR_STATE_NAMES[-1]]
    extra = dict(sample.plant.commands)
    extra["unknown_actuator"] = 1.0

    for commands in (missing, extra):
        forged_plant = replace(sample.plant, commands=commands)
        with pytest.raises(ValueError, match="exactly the 11 M3 actuator names"):
            replace(sample, plant=forged_plant)


def test_closed_loop_sample_rejects_record_output_detached_from_plant_command() -> None:
    sample = _sample(0.0)
    controls = dict(sample.controls)
    controls["feed_flow"] = replace(controls["feed_flow"], output=1.01)

    with pytest.raises(ValueError, match="output.*must match its plant command"):
        replace(sample, controls=controls)


def test_closed_loop_sample_rejects_current_pv_detached_from_plant_state() -> None:
    sample = _sample(0.0)
    controls = dict(sample.controls)
    controls["top_pressure"] = replace(
        controls["top_pressure"],
        process_value=1.01,
    )

    with pytest.raises(ValueError, match="process_value.*current plant PV"):
        replace(sample, controls=controls)


def test_decision_pv_can_retain_the_previous_control_sample() -> None:
    sample = _sample(0.0)
    controls = dict(sample.controls)
    controls["top_pressure"] = replace(
        controls["top_pressure"],
        decision_process_value=1.01,
    )

    held_sample = replace(sample, controls=controls)

    assert held_sample.controls["top_pressure"].decision_process_value == 1.01


def test_automatic_record_requires_the_raw_pi_identity_but_manual_does_not() -> None:
    record = _control_record()

    with pytest.raises(ValueError, match="unconstrained output must equal"):
        replace(record, proportional_term_normalized=0.01)

    manual_saturated = replace(
        record,
        mode="manual",
        unconstrained_output_normalized=1.2,
        magnitude_limited_output_normalized=1.1,
        output_normalized=1.0,
        limited_by_magnitude=True,
        limited_by_rate=True,
    )

    assert manual_saturated.saturated


def test_limiter_flags_exactly_match_their_normalized_output_changes() -> None:
    record = _control_record()

    with pytest.raises(ValueError, match="magnitude limiter flag"):
        replace(
            record,
            proportional_term_normalized=0.1,
            unconstrained_output_normalized=1.1,
            magnitude_limited_output_normalized=1.0,
        )
    with pytest.raises(ValueError, match="rate limiter flag"):
        replace(
            record,
            proportional_term_normalized=0.1,
            unconstrained_output_normalized=1.1,
            magnitude_limited_output_normalized=1.1,
            output_normalized=1.0,
        )

    tiny_change = math.nextafter(1.0, math.inf)
    exact_tiny_limit = replace(
        record,
        integral_term_normalized=tiny_change - 1.0,
        unconstrained_output_normalized=tiny_change,
        magnitude_limited_output_normalized=1.0,
        limited_by_magnitude=True,
    )
    assert exact_tiny_limit.limited_by_magnitude


def test_success_requires_every_loop_to_remain_automatic() -> None:
    result = _result()
    controls = dict(result.samples[-1].controls)
    controls["feed_flow"] = replace(controls["feed_flow"], mode="manual")
    forged_sample = replace(result.samples[-1], controls=controls)

    with pytest.raises(ValueError, match="remain automatic at every sample"):
        replace(result, samples=(*result.samples[:-1], forged_sample))


def test_success_requires_unowned_actuator_commands_to_remain_constant() -> None:
    result = _result()
    commands = dict(result.samples[-1].plant.commands)
    commands["reflux_flow_kg_s"] = 1.01
    forged_sample = replace(
        result.samples[-1],
        plant=replace(result.samples[-1].plant, commands=commands),
    )

    with pytest.raises(ValueError, match="unowned actuator commands"):
        replace(result, samples=(*result.samples[:-1], forged_sample))


def test_success_recomputes_current_and_decision_errors_from_the_initial_pv() -> None:
    result = _result()
    current_controls = dict(result.samples[-1].controls)
    current_controls["feed_flow"] = replace(
        current_controls["feed_flow"],
        error_normalized=0.01,
        decision_error_normalized=0.01,
    )
    forged_current = replace(result.samples[-1], controls=current_controls)
    with pytest.raises(ValueError, match="current error"):
        replace(result, samples=(*result.samples[:-1], forged_current))

    mid_sample = _sample(0.5)
    decision_controls = dict(mid_sample.controls)
    decision_controls["feed_flow"] = replace(
        decision_controls["feed_flow"],
        decision_process_value=0.99,
        decision_error_normalized=0.0,
    )
    forged_decision = replace(mid_sample, controls=decision_controls)
    with pytest.raises(ValueError, match="decision error"):
        replace(
            result,
            samples=(result.samples[0], forged_decision, result.samples[-1]),
        )


def test_success_anchors_pv_and_output_scales_to_the_first_sample() -> None:
    result = _result()
    initial_controls = dict(result.samples[0].controls)
    initial_controls["feed_flow"] = replace(
        initial_controls["feed_flow"],
        ramped_setpoint=1.01,
        error_normalized=0.01,
        decision_error_normalized=0.01,
    )
    forged_initial_pv = replace(result.samples[0], controls=initial_controls)
    with pytest.raises(ValueError, match="ramped setpoints.*current process values"):
        replace(result, samples=(forged_initial_pv, result.samples[-1]))

    normalized_controls = dict(result.samples[0].controls)
    normalized_controls["feed_flow"] = replace(
        normalized_controls["feed_flow"],
        integral_term_normalized=0.01,
        unconstrained_output_normalized=1.01,
        magnitude_limited_output_normalized=1.01,
        output_normalized=1.01,
    )
    forged_initial_output = replace(
        result.samples[0],
        controls=normalized_controls,
    )
    with pytest.raises(ValueError, match="unit normalized outputs"):
        replace(result, samples=(forged_initial_output, result.samples[-1]))

    commands = dict(result.samples[-1].plant.commands)
    commands["fresh_feed_flow_kg_s"] = 1.01
    output_controls = dict(result.samples[-1].controls)
    output_controls["feed_flow"] = replace(
        output_controls["feed_flow"],
        output=1.01,
    )
    forged_output_scale = replace(
        result.samples[-1],
        plant=replace(result.samples[-1].plant, commands=commands),
        controls=output_controls,
    )
    with pytest.raises(ValueError, match="normalized output"):
        replace(result, samples=(*result.samples[:-1], forged_output_scale))


def test_success_allows_feedforward_only_on_the_furnace_loop() -> None:
    result = _result()
    controls = dict(result.samples[-1].controls)
    controls["feed_flow"] = replace(
        controls["feed_flow"],
        feedforward_normalized=0.01,
        integral_term_normalized=-0.01,
    )
    forged_sample = replace(result.samples[-1], controls=controls)

    with pytest.raises(ValueError, match="only the furnace_temperature loop"):
        replace(result, samples=(*result.samples[:-1], forged_sample))


def test_result_requires_complete_output_and_control_grids() -> None:
    result = _result()

    with pytest.raises(ValueError, match="integer multiple"):
        replace(result, time_step_s=0.3)
    with pytest.raises(ValueError, match="regular output endpoint"):
        replace(result, time_step_s=0.5, control_interval_s=0.5)


def test_regular_control_ticks_require_current_decision_values() -> None:
    result = _result()
    controls = dict(result.samples[-1].controls)
    controls["feed_flow"] = replace(
        controls["feed_flow"],
        decision_process_value=0.99,
        decision_error_normalized=0.01,
    )
    forged_sample = replace(result.samples[-1], controls=controls)

    with pytest.raises(ValueError, match="every complete control tick"):
        replace(result, samples=(*result.samples[:-1], forged_sample))


def test_success_rechecks_instantaneous_conservation() -> None:
    result = _result()
    forged_plant = replace(
        result.samples[-1].plant,
        instantaneous_mass_residual_kg_s=(
            2.0 * result.conservation_tolerances.instantaneous_mass_atol_kg_s
        ),
    )
    forged_sample = replace(result.samples[-1], plant=forged_plant)

    with pytest.raises(ValueError, match="instantaneous conservation"):
        replace(result, samples=(*result.samples[:-1], forged_sample))


def test_success_rechecks_m3_cumulative_relative_conservation() -> None:
    result = _result()
    residuals = dict(result.samples[-1].plant.component_balance_residuals_kg)
    residuals["naphtha"] = 2e-6
    forged_plant = replace(
        result.samples[-1].plant,
        component_balance_residuals_kg=residuals,
        mass_balance_residual_kg=2e-6,
    )
    forged_sample = replace(result.samples[-1], plant=forged_plant)

    with pytest.raises(ValueError, match="cumulative conservation"):
        replace(result, samples=(*result.samples[:-1], forged_sample))


def test_success_rechecks_final_balance_absolute_tolerances() -> None:
    result = _result()
    final_sample = result.samples[-1]
    inventories = dict(final_sample.plant.state.liquid_inventories)
    flash_drum = inventories["flash_drum"]
    component_masses = dict(flash_drum.component_masses_kg)
    component_masses["naphtha"] -= 2e-6
    inventories["flash_drum"] = replace(
        flash_drum,
        component_masses_kg=component_masses,
    )
    forged_state = replace(
        final_sample.plant.state,
        liquid_inventories=inventories,
    )
    forged_sample = replace(
        final_sample,
        plant=replace(final_sample.plant, state=forged_state),
    )
    forged_balance = replace(
        result.balance,
        final_component_inventory_kg=_inventory_components(forged_state),
    )

    with pytest.raises(ValueError, match="final balance"):
        replace(
            result,
            samples=(*result.samples[:-1], forged_sample),
            balance=forged_balance,
        )


def test_success_rejects_self_consistent_balance_detached_from_samples() -> None:
    result = _result()
    zero_components = {component: 0.0 for component in ALL_COMPONENTS}
    forged_balance = DynamicCumulativeBalance(
        initial_component_inventory_kg=result.balance.initial_component_inventory_kg,
        final_component_inventory_kg=result.balance.initial_component_inventory_kg,
        cumulative_component_in_kg=zero_components,
        cumulative_component_out_kg=zero_components,
        initial_inventory_salt_kg=result.balance.initial_inventory_salt_kg,
        final_inventory_salt_kg=result.balance.initial_inventory_salt_kg,
        cumulative_salt_in_kg=0.0,
        cumulative_salt_out_kg=0.0,
    )

    with pytest.raises(ValueError, match="agree with the initial and final plant samples"):
        replace(result, balance=forged_balance)


def test_success_rejects_zero_tolerance_forgery() -> None:
    result = _result()
    zero_tolerances = replace(
        result.conservation_tolerances,
        instantaneous_mass_atol_kg_s=0.0,
        instantaneous_component_atol_kg_s=0.0,
        instantaneous_salt_atol_kg_s=0.0,
        cumulative_relative_atol=0.0,
    )

    with pytest.raises(ValueError, match="conservation"):
        replace(result, conservation_tolerances=zero_tolerances)


def test_success_rejects_terminal_saturation_and_forged_direct_metrics() -> None:
    result = _result()
    controls = dict(result.samples[-1].controls)
    controls["feed_flow"] = replace(
        controls["feed_flow"],
        proportional_term_normalized=0.1,
        unconstrained_output_normalized=1.1,
        magnitude_limited_output_normalized=1.0,
        limited_by_magnitude=True,
    )
    saturated_final = replace(result.samples[-1], controls=controls)
    with pytest.raises(ValueError, match="cannot end with a saturated loop"):
        replace(result, samples=(*result.samples[:-1], saturated_final))

    metric = result.loop_performance["feed_flow"]
    forged_metrics = (
        replace(metric, normalized_iae_s=1.0),
        replace(metric, final_error_fraction=0.1),
        replace(
            metric,
            saturation_time_s=1.0,
            longest_continuous_saturation_s=1.0,
        ),
    )
    for forged_metric in forged_metrics:
        performance = dict(result.loop_performance)
        performance["feed_flow"] = forged_metric
        with pytest.raises(ValueError, match="must agree with the sample series"):
            replace(result, loop_performance=performance)


def test_failed_result_cannot_predate_or_misreport_plant_completion() -> None:
    result = _result()
    checks = dict(result.acceptance_checks)
    checks["baseline_hold"] = False

    with pytest.raises(ValueError, match="cannot precede the last valid"):
        replace(
            result,
            status="failed",
            acceptance_checks=checks,
            failure_reason="forged failure",
            failure_stage="test",
            failure_time_s=0.0,
        )

    checks["plant_execution"] = False
    with pytest.raises(ValueError, match="plant_execution must agree"):
        replace(
            result,
            status="failed",
            acceptance_checks=checks,
            failure_reason="forged plant failure",
            failure_stage="test",
            failure_time_s=1.0,
        )


def test_failed_loop_gate_requires_exactly_seven_consistent_metrics() -> None:
    result = _result()
    checks = dict(result.acceptance_checks)
    checks["baseline_hold"] = False

    with pytest.raises(ValueError, match="loop_performance gate must agree"):
        replace(
            result,
            status="failed",
            loop_performance={},
            acceptance_checks=checks,
            failure_reason="forged metric gate",
            failure_stage="test",
            failure_time_s=1.0,
        )


def test_failed_result_rechecks_carried_balance_and_true_conservation_gate() -> None:
    result = _result()
    checks = dict(result.acceptance_checks)
    checks["baseline_hold"] = False
    zero_components = {component: 0.0 for component in ALL_COMPONENTS}
    detached_balance = DynamicCumulativeBalance(
        initial_component_inventory_kg=result.balance.initial_component_inventory_kg,
        final_component_inventory_kg=result.balance.initial_component_inventory_kg,
        cumulative_component_in_kg=zero_components,
        cumulative_component_out_kg=zero_components,
        initial_inventory_salt_kg=result.balance.initial_inventory_salt_kg,
        final_inventory_salt_kg=result.balance.initial_inventory_salt_kg,
        cumulative_salt_in_kg=0.0,
        cumulative_salt_out_kg=0.0,
    )
    with pytest.raises(ValueError, match="balance must agree"):
        replace(
            result,
            status="failed",
            balance=detached_balance,
            acceptance_checks=checks,
            failure_reason="forged balance",
            failure_stage="test",
            failure_time_s=1.0,
        )

    zero_tolerances = replace(
        result.conservation_tolerances,
        instantaneous_mass_atol_kg_s=0.0,
        instantaneous_component_atol_kg_s=0.0,
        instantaneous_salt_atol_kg_s=0.0,
        cumulative_relative_atol=0.0,
    )
    with pytest.raises(ValueError, match="conservation"):
        replace(
            result,
            status="failed",
            conservation_tolerances=zero_tolerances,
            acceptance_checks=checks,
            failure_reason="forged conservation gate",
            failure_stage="test",
            failure_time_s=1.0,
        )


def test_diagnostic_names_must_be_strings() -> None:
    result = _result()

    with pytest.raises(TypeError, match="diagnostic names must be strings"):
        replace(result, diagnostics={cast(str, 1): 1.0})
