from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import (
    load_case_config,
    load_component_catalog,
    load_model_config,
    load_scenario_config,
)
from petroleum_rto.cdu.dynamics.runner import run_dynamic_scenario
from petroleum_rto.cdu.flowsheet.recycle import solve_recycle
from petroleum_rto.cdu.validation.metrics import (
    STEADY_OUTPUT_IDS,
    _response_t63_s,
    dynamic_output_metrics,
    evaluate_metric_directions,
    steady_output_metrics,
)


def test_steady_metrics_cover_flow_yield_energy_and_quality(repo_root: Path) -> None:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    catalog = load_component_catalog(
        repo_root / "configs/cdu/models/components_v0.1.0.json"
    )

    outputs = steady_output_metrics(solve_recycle(model, case, catalog))

    assert tuple(outputs) == STEADY_OUTPUT_IDS
    assert all(value > 0.0 for value in outputs.values())
    assert outputs == steady_output_metrics(solve_recycle(model, case, catalog))
    assert outputs["energy.potential_recovered_duty_w"] > 0.0
    assert outputs["energy.pump_around_removed_duty_w"] > 0.0


def test_dynamic_metrics_use_residue_product_and_include_trip_temperatures(
    repo_root: Path,
) -> None:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    catalog = load_component_catalog(
        repo_root / "configs/cdu/models/components_v0.1.0.json"
    )
    scenario = load_scenario_config(
        repo_root / "configs/cdu/scenarios/open_loop_baseline_v0.1.0.json"
    )
    short_scenario = replace(scenario, duration_s=2.0, time_step_s=1.0, events=())

    outputs = dynamic_output_metrics(
        run_dynamic_scenario(model, case, catalog, short_scenario)
    )

    assert outputs["final_product_flow_ratio.residue"] == pytest.approx(1.0)
    assert outputs["final.tower_top_temperature_k"] > 0.0
    assert outputs["final.kerosene_temperature_k"] > 0.0
    assert outputs["tracking_iae.actuator.fresh_feed_flow_kg_s"] == 0.0
    assert outputs["tracking_iae.sensor.flash_drum_inventory_kg"] == 0.0
    assert outputs["tracking_iae.sensor.tower_top_pressure_pa"] == pytest.approx(
        0.0,
        abs=1e-6,
    )
    assert outputs["response_t63_s.actuator.fresh_feed_flow_kg_s"] == 0.0
    assert outputs["response_t63_s.sensor.flash_drum_inventory_kg"] == 0.0


def test_t63_uses_the_command_event_origin_and_interpolates_crossing() -> None:
    times = (0.0, 1.0, 2.0, 3.0)
    command = (1.0, 2.0, 2.0, 2.0)
    response = (0.0, 0.0, 0.5, 1.0)

    value = _response_t63_s(times, response, command)

    target = 1.0 - math.exp(-1.0)
    assert value == pytest.approx(1.0 + (target - 0.5) / 0.5)
    assert _response_t63_s(times, response, (1.0, 1.0, 1.0, 1.0)) == 0.0


def test_direction_checks_are_strict_and_deterministic() -> None:
    baseline = {"up": 1.0, "down": 1.0, "flat": 1.0}
    candidate = {"up": 2.0, "down": 0.5, "flat": 1.0}

    checks = evaluate_metric_directions(
        baseline,
        candidate,
        {"up": 1, "down": -1, "flat": 0},
    )

    assert dict(checks) == {"down": True, "flat": True, "up": True}
    with pytest.raises(ValueError, match="-1, 0 or 1"):
        evaluate_metric_directions(baseline, candidate, {"up": 2})
    with pytest.raises(ValueError, match="missing"):
        evaluate_metric_directions(baseline, candidate, {"unknown": 1})
