from __future__ import annotations

from pathlib import Path

import pytest

import petroleum_rto.cdu.validation.artifacts as artifacts_module
from petroleum_rto.cdu.validation.artifacts import (
    verify_m6_artifacts,
    write_m6_artifacts,
)
from petroleum_rto.cdu.validation.config import load_m6_validation_config
from petroleum_rto.cdu.validation.results import M6ValidationResult
from petroleum_rto.cdu.validation.runner import (
    M6ValidationExecutionError,
    run_m6_validation,
)


@pytest.fixture(scope="module")
def m6_result(repo_root: Path) -> M6ValidationResult:
    return run_m6_validation(repo_root)


def test_complete_m6_matrix_passes_with_expected_limited_and_rejected_evidence(
    m6_result: M6ValidationResult,
) -> None:
    assert m6_result.status == "success"
    assert m6_result.completion_passed
    assert all(m6_result.completion_checks.values())
    assert len(m6_result.scenarios) == 22
    by_id = {item.scenario_id: item for item in m6_result.scenarios}

    assert by_id["normal_m4_feed_setpoint_plus_5"].metrics[
        "loop.feed_flow.final_output_ratio"
    ] == pytest.approx(1.05)
    assert by_id["normal_m4_feed_setpoint_minus_5"].metrics[
        "loop.feed_flow.final_output_ratio"
    ] == pytest.approx(0.95)
    assert by_id["limited_feed_drop_30"].scenario_status == "limited"
    feed_drop = by_id["limited_feed_drop_30"]
    pump_trip = by_id["limited_pump_around_1_trip"]
    assert pump_trip.protection_trace is not None
    for scenario in (feed_drop, pump_trip):
        assert scenario.protection_trace is not None
        triggered = tuple(
            event
            for event in scenario.protection_trace.events
            if event.event_kind == "triggered"
        )
        assert triggered
        assert triggered[0].time_s >= 60.0
        assert scenario.metrics["protection_first_trigger_time_s"] >= 60.0

    sensor_bias = by_id["limited_furnace_temperature_sensor_bias"]
    assert sensor_bias.metrics["applied_sensor_bias_k"] == 5.0
    assert sensor_bias.metrics["false_trip_absent"] == 1.0
    assert sensor_bias.protection_trace is not None
    assert not any(
        event.event_kind == "triggered"
        for event in sensor_bias.protection_trace.events
    )

    valve_stuck = by_id["limited_residue_draw_valve_stuck"]
    assert valve_stuck.metrics["applied_valve_mobility_ratio"] == 0.0
    assert valve_stuck.metrics["fault_constraint_applied"] == 1.0

    for scenario_id in (
        "rejected_dynamic_feed_water_pulse",
        "rejected_stripping_steam_request",
    ):
        scenario = by_id[scenario_id]
        assert scenario.scenario_status == "rejected"
        assert scenario.verification_outcome == "passed"
        assert not scenario.solver_called
        assert not scenario.metrics

    tracking = by_id["limited_furnace_temperature_sensor_freeze"]
    assert tracking.metrics["tracking_no_bump"] == 1.0
    assert tracking.conservation_checks["tracking_no_bump"]
    assert set(m6_result.sensitivity_analyses) == set(
        m6_result.uncertainty_results
    )
    assert len(m6_result.protection_traces) == 11
    assert all(
        any(event.event_kind == "triggered" for event in trace.events)
        for trace in m6_result.protection_traces.values()
    )
    assert m6_result.controller_tracking
    assert all(item.passed for item in m6_result.controller_tracking.values())
    held = m6_result.controller_tracking[
        "furnace_temperature_measurement_invalid.furnace_temperature"
    ]
    assert held.protected_output == held.initial_output
    dynamic_plan = m6_result.sensitivity_analyses[
        "m6_dynamic_lag_envelope_v0.1.0"
    ]
    assert {
        "maximum.furnace_outlet_temperature_k",
        "maximum.tower_top_pressure_pa",
        "maximum_abs_inventory_deviation.flash_drum",
        "maximum_abs_inventory_deviation.reflux_drum",
        "maximum_abs_inventory_deviation.tower_bottom",
        "final_inventory_ratio.flash_drum",
        "final_inventory_ratio.reflux_drum",
        "final_inventory_ratio.tower_bottom",
        "response_t63_s.actuator.fresh_feed_flow_kg_s",
        "response_t63_s.sensor.flash_drum_inventory_kg",
    } <= {item.output_id for item in dynamic_plan.output_specs}


def test_m6_artifact_bundle_is_transactional_and_verifiable(
    m6_result: M6ValidationResult,
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_m6_validation_config(
        repo_root / "configs/validation/m6_validation_v0.1.0.json"
    )
    monkeypatch.setattr(
        artifacts_module,
        "load_m6_validation_config",
        lambda path: config,
    )
    monkeypatch.setattr(
        artifacts_module,
        "load_m6_basis",
        lambda root: m6_result.basis,
    )
    written = write_m6_artifacts(m6_result, tmp_path)
    verified = verify_m6_artifacts(
        tmp_path,
        expected_result_fingerprint=m6_result.result_fingerprint,
    )

    assert written.as_dict() == verified.as_dict()


def test_m6_preflight_failure_preserves_explicit_failure_evidence(
    repo_root: Path,
) -> None:
    with pytest.raises(M6ValidationExecutionError) as caught:
        run_m6_validation(
            repo_root,
            config_path=repo_root / "configs/validation/missing-m6-config.json",
        )

    evidence = caught.value.as_dict()
    assert evidence["status"] == "failed"
    assert evidence["failure_time_s"] == 0.0
    assert evidence["last_valid_scenario_ids"] == []
