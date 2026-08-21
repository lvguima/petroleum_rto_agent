from __future__ import annotations

import json
from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

import petroleum_rto.cdu.dynamics.runner as runner_module
from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ModelConfig,
    ScenarioConfig,
    load_case_config,
    load_component_catalog,
    load_model_config,
    load_scenario_config,
)
from petroleum_rto.cdu.core.math_utils import ConvergenceError
from petroleum_rto.cdu.dynamics.runner import run_dynamic_scenario
from petroleum_rto.cdu.dynamics.state import DynamicState
from petroleum_rto.cdu.flowsheet.recycle import RecycleSettings
from petroleum_rto.cdu.properties.components import ComponentCatalog
from petroleum_rto.cdu.repository import resolve_cdu_repository_path


@dataclass(frozen=True)
class RunnerInputs:
    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    baseline: ScenarioConfig
    feed_step: ScenarioConfig


@pytest.fixture(scope="module")
def runner_inputs(repo_root: Path) -> RunnerInputs:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    catalog = load_component_catalog(
        resolve_cdu_repository_path(repo_root, model.component_catalog_path)
    )
    baseline = load_scenario_config(
        repo_root / "configs/cdu/scenarios/open_loop_baseline_v0.1.0.json"
    )
    feed_step = load_scenario_config(
        repo_root / "configs/cdu/scenarios/open_loop_feed_step_v0.1.0.json"
    )
    return RunnerInputs(model, case, catalog, baseline, feed_step)


def _total_inventory_kg(state: DynamicState) -> float:
    return sum(inventory.total_mass_kg for inventory in state.liquid_inventories.values()) + sum(
        state.top_gas_component_masses_kg.values()
    )


def test_versioned_baseline_runs_through_the_public_entry_point(
    runner_inputs: RunnerInputs,
) -> None:
    scenario = replace(
        runner_inputs.baseline,
        duration_s=20.0,
        time_step_s=1.0,
    )

    first = run_dynamic_scenario(
        runner_inputs.model,
        runner_inputs.case,
        runner_inputs.catalog,
        scenario,
    ).require_success()
    second = run_dynamic_scenario(
        runner_inputs.model,
        runner_inputs.case,
        runner_inputs.catalog,
        scenario,
    ).require_success()

    assert len(first.samples) == 21
    assert first.as_dict() == second.as_dict()
    assert first.input_fingerprint == second.input_fingerprint
    assert first.samples[-1].state.to_vector() == second.samples[-1].state.to_vector()
    assert first.versions["simulation_stage"] == "M3"
    assert first.versions["scenario_version"] == scenario.scenario_version
    assert first.metadata["synthetic"] == "true"
    assert first.metadata["data_origin"] == "M3_open_loop_simulation"
    assert first.metadata["scenario_name"] == scenario.name
    assert first.metadata["scenario_version"] == scenario.scenario_version
    assert first.metadata["purpose"] == scenario.metadata["purpose"]
    assert json.dumps(first.as_dict(), allow_nan=False)
    frozen_metadata = cast(MutableMapping[str, str], first.metadata)
    with pytest.raises(TypeError):
        frozen_metadata["synthetic"] = "false"


def test_feed_step_scenario_accumulates_inventory_without_hidden_control(
    runner_inputs: RunnerInputs,
) -> None:
    scenario = replace(
        runner_inputs.feed_step,
        duration_s=1200.0,
        time_step_s=1.0,
    )

    result = run_dynamic_scenario(
        runner_inputs.model,
        runner_inputs.case,
        runner_inputs.catalog,
        scenario,
    ).require_success()

    initial_inventory = _total_inventory_kg(result.samples[0].state)
    event_inventory = _total_inventory_kg(result.samples[600].state)
    final_inventory = _total_inventory_kg(result.samples[-1].state)
    assert final_inventory > event_inventory >= initial_inventory
    assert (
        result.samples[-1].state.actuator_states["fresh_feed_flow_kg_s"]
        > runner_inputs.case.feed.mass_flow_kg_s
    )
    assert result.balance.passed(mass_atol_kg=1e-5, salt_atol_kg=1e-8)
    assert (
        result.balance.maximum_absolute_component_residual_kg
        / max(result.balance.cumulative_mass_in_kg, 1.0)
        <= 1e-6
    )


def test_public_entry_point_rejects_a_failed_m2_prerequisite(
    runner_inputs: RunnerInputs,
) -> None:
    settings = replace(
        RecycleSettings.from_model(runner_inputs.model),
        maximum_iterations=1,
    )
    scenario = replace(runner_inputs.baseline, duration_s=1.0, time_step_s=1.0)

    with pytest.raises(ConvergenceError, match="M3 prerequisite failed at convergence"):
        run_dynamic_scenario(
            runner_inputs.model,
            runner_inputs.case,
            runner_inputs.catalog,
            scenario,
            recycle_settings=settings,
        )


def test_bypassed_invalid_scenarios_fail_before_m2_without_partial_trajectory(
    runner_inputs: RunnerInputs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solve_calls: list[object] = []
    simulation_calls: list[object] = []

    def forbidden_solve(*args: object, **kwargs: object) -> object:
        solve_calls.append((args, kwargs))
        raise AssertionError("solve_recycle must not run for an invalid scenario")

    def forbidden_simulation(*args: object, **kwargs: object) -> object:
        simulation_calls.append((args, kwargs))
        raise AssertionError("simulate_dynamic must not produce a partial trajectory")

    monkeypatch.setattr(runner_module, "solve_recycle", forbidden_solve)
    monkeypatch.setattr(runner_module, "simulate_dynamic", forbidden_simulation)

    baseline = runner_inputs.baseline
    invalid_scenarios = (
        replace(
            baseline,
            events=(
                {
                    "time_s": 0.0,
                    "target": "unknown_actuator",
                    "value": 1.0,
                },
            ),
        ),
        replace(
            baseline,
            events=(
                {
                    "time_s": 0.0,
                    "target": "fresh_feed_flow_kg_s",
                    "value": -1.0,
                },
            ),
        ),
        replace(
            baseline,
            metadata={"synthetic": "false", "purpose": "invalid test"},
        ),
        replace(
            baseline,
            metadata={"synthetic": "true", "purpose": "   "},
        ),
        replace(baseline, name="   "),
        replace(baseline, scenario_version=""),
    )

    for scenario in invalid_scenarios:
        with pytest.raises((TypeError, ValueError)):
            run_dynamic_scenario(
                runner_inputs.model,
                runner_inputs.case,
                runner_inputs.catalog,
                scenario,
            )

    assert solve_calls == []
    assert simulation_calls == []
