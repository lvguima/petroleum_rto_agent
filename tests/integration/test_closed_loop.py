from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.control import (
    REQUIRED_CONTROL_LOOP_IDS,
    ClosedLoopSample,
    ClosedLoopScenarioConfig,
    ClosedLoopSimulationResult,
    ControlConfig,
    SetpointEvent,
    load_closed_loop_scenario,
    load_control_config,
    run_closed_loop,
    simulate_closed_loop,
)
from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ModelConfig,
    load_case_config,
    load_component_catalog,
    load_model_config,
)
from petroleum_rto.cdu.dynamics import (
    DynamicConservationTolerances,
    OpenLoopDynamicModel,
    initialize_open_loop_dynamic_model,
)
from petroleum_rto.cdu.dynamics.state import (
    ACTUATOR_STATE_NAMES,
    LIQUID_INVENTORY_NAMES,
    SENSOR_STATE_NAMES,
    THERMAL_STATE_NAMES,
)
from petroleum_rto.cdu.flowsheet.recycle import solve_recycle
from petroleum_rto.cdu.properties.components import ComponentCatalog

M4Inputs = tuple[
    ModelConfig,
    CaseConfig,
    ComponentCatalog,
    ControlConfig,
    ClosedLoopScenarioConfig,
    ClosedLoopScenarioConfig,
]


@pytest.fixture(scope="module")
def m4_inputs(repo_root: Path) -> M4Inputs:
    return (
        load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json"),
        load_case_config(repo_root / "configs/cases/case_20260604.json"),
        load_component_catalog(repo_root / "configs/models/components_v0.1.0.json"),
        load_control_config(repo_root / "configs/controllers/cdu_pi_v0.1.0.json"),
        load_closed_loop_scenario(
            repo_root / "configs/scenarios/closed_loop_baseline_v0.1.0.json"
        ),
        load_closed_loop_scenario(
            repo_root / "configs/scenarios/closed_loop_feed_step_v0.1.0.json"
        ),
    )


@pytest.fixture(scope="module")
def dynamic_model(
    m4_inputs: M4Inputs,
) -> OpenLoopDynamicModel:
    model, case, catalog, _, _, _ = m4_inputs
    recycle = solve_recycle(model, case, catalog)
    return initialize_open_loop_dynamic_model(model, case, catalog, recycle)


@pytest.fixture(scope="module")
def feed_step_result(m4_inputs: M4Inputs) -> ClosedLoopSimulationResult:
    model, case, catalog, control, _, step = m4_inputs
    return run_closed_loop(model, case, catalog, control, step)


def _sample_at(
    result: ClosedLoopSimulationResult,
    time_s: float,
) -> ClosedLoopSample:
    return min(result.samples, key=lambda sample: abs(sample.time_s - time_s))


def _normalized_sample_difference(
    left: ClosedLoopSample,
    right: ClosedLoopSample,
    reference: ClosedLoopSample,
) -> float:
    expected_commands = set(ACTUATOR_STATE_NAMES)
    assert set(left.plant.commands) == expected_commands
    assert set(right.plant.commands) == expected_commands
    assert set(reference.plant.commands) == expected_commands
    differences: list[float] = []
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        pv_scale = reference.controls[loop_id].target_setpoint
        differences.extend(
            (
                abs(
                    left.controls[loop_id].process_value
                    - right.controls[loop_id].process_value
                )
                / pv_scale,
                abs(
                    left.controls[loop_id].output_normalized
                    - right.controls[loop_id].output_normalized
                ),
            )
        )
    for name in LIQUID_INVENTORY_NAMES:
        scale = reference.plant.state.liquid_inventories[name].total_mass_kg
        differences.append(
            abs(
                left.plant.state.liquid_inventories[name].total_mass_kg
                - right.plant.state.liquid_inventories[name].total_mass_kg
            )
            / scale
        )
    for names, left_values, right_values, reference_values in (
        (
            THERMAL_STATE_NAMES,
            left.plant.state.thermal_states,
            right.plant.state.thermal_states,
            reference.plant.state.thermal_states,
        ),
        (
            SENSOR_STATE_NAMES,
            left.plant.state.sensor_states,
            right.plant.state.sensor_states,
            reference.plant.state.sensor_states,
        ),
        (
            ACTUATOR_STATE_NAMES,
            left.plant.state.actuator_states,
            right.plant.state.actuator_states,
            reference.plant.state.actuator_states,
        ),
    ):
        differences.extend(
            abs(left_values[name] - right_values[name])
            / max(abs(reference_values[name]), 1.0)
            for name in names
        )
    differences.extend(
        abs(left.plant.commands[name] - right.plant.commands[name])
        / max(abs(reference.plant.commands[name]), 1.0)
        for name in reference.plant.commands
    )
    return max(differences)


def test_full_closed_loop_baseline_holds_for_four_hours(
    m4_inputs: M4Inputs,
) -> None:
    model, case, catalog, control, baseline, _ = m4_inputs
    result = run_closed_loop(model, case, catalog, control, baseline)

    assert result.status == "success"
    assert len(result.samples) == 14_401
    assert all(result.acceptance_checks.values())
    assert result.diagnostics["maximum_initial_command_delta"] == 0.0
    assert result.diagnostics["maximum_baseline_error_fraction"] <= 1e-6
    assert all(item.saturation_time_s == 0.0 for item in result.loop_performance.values())


def test_full_feed_setpoint_recovery_passes_all_m4_gates(
    feed_step_result: ClosedLoopSimulationResult,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, _, _ = m4_inputs
    result = feed_step_result

    assert result.status == "success"
    assert len(result.samples) == 7_201
    assert result.completed_time_s == 7_200.0
    assert all(result.acceptance_checks.values())
    assert result.versions["simulation_stage"] == "M4"
    assert result.metadata["synthetic"] == "true"
    assert result.metadata["data_origin"] == "M4_closed_loop_simulation"
    assert result.control_fingerprint == control.input_fingerprint
    assert set(result.loop_performance) == set(REQUIRED_CONTROL_LOOP_IDS)
    assert all(metric.passed for metric in result.loop_performance.values())
    feed_settling = result.loop_performance["feed_flow"].settling_time_s
    assert feed_settling is not None
    assert feed_settling <= 300.0
    for loop_id in ("flash_inventory", "reflux_inventory", "bottom_inventory"):
        settling = result.loop_performance[loop_id].settling_time_s
        assert settling is not None
        assert settling <= 3_600.0
    assert result.diagnostics["minimum_true_inventory_ratio"] >= 0.8
    assert result.diagnostics["maximum_true_inventory_ratio"] <= 1.2
    assert result.balance.maximum_absolute_component_residual_kg < 1e-5

    initial_commands = result.samples[0].plant.commands
    final_commands = result.samples[-1].plant.commands
    for fixed in (
        "reflux_flow_kg_s",
        "condenser_cooling_duty_w",
        "pump_around_2_duty_w",
        "pump_around_3_duty_w",
    ):
        assert final_commands[fixed] == initial_commands[fixed]


def test_closed_loop_time_step_halving_is_consistent(
    feed_step_result: ClosedLoopSimulationResult,
    m4_inputs: M4Inputs,
) -> None:
    model, case, catalog, control, _, step = m4_inputs
    fine = run_closed_loop(
        model,
        case,
        catalog,
        control,
        replace(step, time_step_s=0.5),
    )

    assert fine.status == "success"
    coarse_by_time = {sample.time_s: sample for sample in feed_step_result.samples}
    fine_by_time = {
        sample.time_s: sample
        for sample in fine.samples
        if sample.time_s in coarse_by_time
    }
    maximum_difference = 0.0
    reference = feed_step_result.samples[0]
    for time_s, coarse in coarse_by_time.items():
        fine_sample = fine_by_time[time_s]
        maximum_difference = max(
            maximum_difference,
            _normalized_sample_difference(coarse, fine_sample, reference),
        )
    final_difference = _normalized_sample_difference(
        feed_step_result.samples[-1],
        fine.samples[-1],
        reference,
    )
    assert maximum_difference <= 0.005
    assert final_difference <= 0.002
    non_control_sample = _sample_at(fine, 602.5)
    feed_record = non_control_sample.controls["feed_flow"]
    actual_feed = non_control_sample.plant.state.actuator_states[
        "fresh_feed_flow_kg_s"
    ]
    assert feed_record.process_value == actual_feed
    assert feed_record.process_value != feed_record.decision_process_value


def test_closed_loop_result_is_fully_repeatable_under_disturbance(
    feed_step_result: ClosedLoopSimulationResult,
    m4_inputs: M4Inputs,
) -> None:
    model, case, catalog, control, _, step = m4_inputs
    second = run_closed_loop(model, case, catalog, control, step)

    assert feed_step_result.status == second.status == "success"
    assert feed_step_result.as_dict() == second.as_dict()


def test_non_grid_setpoint_event_has_no_future_effect(
    m4_inputs: M4Inputs,
) -> None:
    model, case, catalog, control, baseline, _ = m4_inputs
    common = replace(
        baseline,
        scenario_version="closed-loop-event-causality-v0.1.0",
        name="closed_loop_event_causality",
        duration_s=12.0,
        time_step_s=0.2,
    )
    neutral_event = run_closed_loop(
        model,
        case,
        catalog,
        control,
        replace(common, events=(SetpointEvent(10.4, "feed_flow", 1.0),)),
    )
    with_event = run_closed_loop(
        model,
        case,
        catalog,
        control,
        replace(common, events=(SetpointEvent(10.4, "feed_flow", 1.05),)),
    )

    before = _sample_at(neutral_event, 10.4)
    event = _sample_at(with_event, 10.4)
    assert before.time_s == event.time_s == 10.4
    assert before.plant.state.to_vector() == event.plant.state.to_vector()
    assert before.plant.commands == event.plant.commands
    after = _sample_at(with_event, 11.0)
    assert after.controls["feed_flow"].ramped_setpoint > event.controls[
        "feed_flow"
    ].ramped_setpoint
    assert after.plant.commands["fresh_feed_flow_kg_s"] > event.plant.commands[
        "fresh_feed_flow_kg_s"
    ]


def test_weak_controller_cannot_turn_plant_success_into_m4_success(
    m4_inputs: M4Inputs,
) -> None:
    model, case, catalog, control, _, step = m4_inputs
    weak_feed = replace(control.loops["feed_flow"], proportional_gain=1e-12)
    weak_control = replace(
        control,
        loops={**control.loops, "feed_flow": weak_feed},
    )
    shortened = replace(
        step,
        scenario_version="closed-loop-weak-controller-v0.1.0",
        name="closed_loop_weak_controller",
        duration_s=1_200.0,
    )

    result = run_closed_loop(model, case, catalog, weak_control, shortened)

    assert result.status == "failed"
    assert result.failure_stage == "performance"
    assert result.acceptance_checks["plant_execution"]
    assert result.acceptance_checks["plant_conservation"]
    assert not result.acceptance_checks["loop_performance"]
    assert result.loop_performance["feed_flow"].final_error_fraction > 0.04


def test_true_inventory_gate_is_not_replaced_by_lagged_sensor(
    m4_inputs: M4Inputs,
) -> None:
    model, case, catalog, control, baseline, _ = m4_inputs
    tight_acceptance = replace(
        control.acceptance,
        baseline_tail_window_s=5.0,
        settling_dwell_s=30.0,
        tail_window_s=60.0,
        inventory_true_min_ratio=0.9999999,
        inventory_true_max_ratio=1.0000001,
        recovery_time_s={loop_id: 100.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS},
    )
    tight_control = replace(control, acceptance=tight_acceptance)
    scenario = replace(
        baseline,
        scenario_version="closed-loop-true-inventory-gate-v0.1.0",
        name="closed_loop_true_inventory_gate",
        duration_s=120.0,
        events=(SetpointEvent(10.0, "feed_flow", 1.05),),
    )

    result = run_closed_loop(model, case, catalog, tight_control, scenario)

    assert result.status == "failed"
    assert result.acceptance_checks["plant_execution"]
    assert not result.acceptance_checks["true_inventory_safety"]
    nominal_true = result.samples[0].plant.state.liquid_inventories[
        "flash_drum"
    ].total_mass_kg
    nominal_sensor = result.samples[0].controls[
        "flash_inventory"
    ].process_value
    assert any(
        sample.plant.state.liquid_inventories["flash_drum"].total_mass_kg
        / nominal_true
        > tight_acceptance.inventory_true_max_ratio
        and sample.controls["flash_inventory"].process_value / nominal_sensor
        <= tight_acceptance.inventory_true_max_ratio
        for sample in result.samples
    )


def test_m3_conservation_failure_propagates_to_m4(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, _, step = m4_inputs
    result = simulate_closed_loop(
        dynamic_model,
        control,
        replace(step, duration_s=10.0, events=()),
        versions=dynamic_model.versions,
        conservation_tolerances=DynamicConservationTolerances(
            instantaneous_mass_atol_kg_s=0.0,
            instantaneous_component_atol_kg_s=0.0,
            instantaneous_salt_atol_kg_s=0.0,
        ),
    )

    assert result.status == "failed"
    assert result.failure_stage == "conservation"
    assert result.failure_time_s == 0.0
    assert not result.acceptance_checks["plant_execution"]


def test_low_level_m4_requires_complete_source_versions(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    with pytest.raises(ValueError, match="missing required source versions"):
        simulate_closed_loop(
            dynamic_model,
            control,
            replace(baseline, duration_s=600.0),
            versions={},
        )


def test_low_level_m4_rejects_control_versions_that_disagree_with_m3(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    incompatible_control = replace(control, model_version="different-model-v9")
    incompatible_scenario = replace(
        baseline,
        model_version="different-model-v9",
    )

    with pytest.raises(ValueError, match="control versions disagree with M3"):
        simulate_closed_loop(
            dynamic_model,
            incompatible_control,
            incompatible_scenario,
            versions=dynamic_model.versions,
        )


def test_short_run_cannot_truncate_acceptance_windows_into_success(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    result = simulate_closed_loop(
        dynamic_model,
        control,
        replace(
            baseline,
            scenario_version="closed-loop-short-window-v0.1.0",
            name="closed_loop_short_window",
            duration_s=300.0,
        ),
        versions=dynamic_model.versions,
    )

    assert result.status == "failed"
    assert result.failure_stage == "performance_evaluation"
    assert result.completed_time_s == 300.0
    assert result.acceptance_checks["plant_execution"]
    assert result.acceptance_checks["plant_conservation"]
    assert not result.acceptance_checks["loop_performance"]
    assert result.failure_reason is not None
    assert "complete acceptance tail window" in result.failure_reason


def test_partial_final_interval_is_not_an_extra_control_tick(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    result = simulate_closed_loop(
        dynamic_model,
        control,
        replace(
            baseline,
            scenario_version="closed-loop-partial-control-v0.1.0",
            name="closed_loop_partial_control_interval",
            duration_s=1.5,
            time_step_s=0.5,
        ),
        versions=dynamic_model.versions,
    )

    assert result.completed_time_s == 1.5
    assert result.diagnostics["controller_decisions"] == 1.0


def test_floating_equivalent_event_and_output_times_are_merged(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    result = simulate_closed_loop(
        dynamic_model,
        control,
        replace(
            baseline,
            scenario_version="closed-loop-time-merge-v0.1.0",
            name="closed_loop_time_merge",
            duration_s=1.0,
            time_step_s=0.1,
            events=(SetpointEvent(0.3, "feed_flow", 1.01),),
        ),
        versions=dynamic_model.versions,
    )

    near_event = [
        sample
        for sample in result.samples
        if abs(sample.time_s - 0.3) <= 1e-12
    ]
    assert len(near_event) == 1
    assert len(result.samples) == 11
    assert result.diagnostics["requested_integration_substeps"] == 10.0


def test_chain_near_event_times_are_all_applied_exactly_once(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    result = simulate_closed_loop(
        dynamic_model,
        control,
        replace(
            baseline,
            scenario_version="closed-loop-chain-time-merge-v0.1.0",
            name="closed_loop_chain_time_merge",
            duration_s=1.0,
            time_step_s=0.1,
            events=(
                SetpointEvent(0.3, "feed_flow", 1.01),
                SetpointEvent(0.30000000000075, "flash_inventory", 1.02),
                SetpointEvent(0.3000000000015, "furnace_temperature", 1.03),
            ),
        ),
        versions=dynamic_model.versions,
    )

    first = result.samples[0]
    last = result.samples[-1]
    assert result.diagnostics["controller_decisions"] == 3.0
    for loop_id, ratio in (
        ("feed_flow", 1.01),
        ("flash_inventory", 1.02),
        ("furnace_temperature", 1.03),
    ):
        assert last.controls[loop_id].target_setpoint == pytest.approx(
            ratio * first.controls[loop_id].target_setpoint
        )


def test_near_zero_event_is_normalized_to_initial_time_and_applied_once(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    result = simulate_closed_loop(
        dynamic_model,
        control,
        replace(
            baseline,
            scenario_version="closed-loop-near-zero-event-v0.1.0",
            name="closed_loop_near_zero_event",
            duration_s=1.0,
            time_step_s=0.1,
            events=(SetpointEvent(5e-13, "feed_flow", 1.01),),
        ),
        versions=dynamic_model.versions,
    )

    initial_feed = result.samples[0].controls["feed_flow"]
    assert initial_feed.target_setpoint / initial_feed.process_value == pytest.approx(1.01)
    assert result.diagnostics["controller_decisions"] == 2.0
    assert len(result.samples) == 11
    assert all(sample.time_s == 0.0 or sample.time_s >= 0.1 for sample in result.samples)


def test_manual_loop_cannot_be_accepted_as_automatic_closed_loop(
    dynamic_model: OpenLoopDynamicModel,
    m4_inputs: M4Inputs,
) -> None:
    _, _, _, control, baseline, _ = m4_inputs
    manual_loop = replace(control.loops["top_temperature"], initial_mode="manual")
    manual_control = replace(
        control,
        loops={**control.loops, "top_temperature": manual_loop},
    )
    result = simulate_closed_loop(
        dynamic_model,
        manual_control,
        replace(
            baseline,
            scenario_version="closed-loop-manual-rejected-v0.1.0",
            name="closed_loop_manual_rejected",
            duration_s=703.5,
            time_step_s=3.5,
        ),
        versions=dynamic_model.versions,
    )

    assert result.status == "failed"
    assert result.failure_stage == "performance"
    assert not result.acceptance_checks["automatic_initialization_no_bump"]
    assert {
        sample.controls["top_temperature"].mode for sample in result.samples
    } == {"manual"}


def test_result_contract_rejects_forged_success_and_invalid_traceability(
    feed_step_result: ClosedLoopSimulationResult,
) -> None:
    result = feed_step_result
    with pytest.raises(ValueError, match="fixed M4 gates"):
        replace(result, acceptance_checks={"fake_gate": True})
    with pytest.raises(ValueError, match="seven passing loop metrics"):
        replace(result, loop_performance={})
    with pytest.raises(ValueError, match="required metadata"):
        replace(
            result,
            metadata={
                key: value
                for key, value in result.metadata.items()
                if key != "purpose"
            },
        )
    with pytest.raises(ValueError, match="failed gate"):
        replace(
            result,
            status="failed",
            failure_reason="forged failure",
            failure_stage="test",
            failure_time_s=result.requested_duration_s,
        )
    with pytest.raises(ValueError, match="failure_time_s"):
        replace(
            result,
            status="failed",
            acceptance_checks={**result.acceptance_checks, "loop_performance": False},
            failure_reason="missing failure time",
            failure_stage="test",
            failure_time_s=None,
        )
    with pytest.raises(ValueError, match="exactly the seven M4 loops"):
        replace(
            result.samples[0],
            controls={"feed_flow": result.samples[0].controls["feed_flow"]},
        )


def test_loop_performance_rejects_impossible_passing_metrics(
    feed_step_result: ClosedLoopSimulationResult,
) -> None:
    metric = feed_step_result.loop_performance["feed_flow"]
    with pytest.raises(ValueError, match="non-negative"):
        replace(metric, normalized_iae_s=-1.0)
    with pytest.raises(ValueError, match="settling_time_s"):
        replace(metric, settling_time_s=None)
    with pytest.raises(ValueError, match="cannot exceed total"):
        replace(
            metric,
            saturation_time_s=1.0,
            longest_continuous_saturation_s=2.0,
        )
