from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.control import (
    ClosedLoopScenarioConfig,
    SetpointEvent,
    load_closed_loop_scenario,
    load_control_config,
    validate_closed_loop_scenario_compatibility,
)
from petroleum_rto.cdu.core.config import ConfigurationError


def test_repository_closed_loop_scenarios_are_strict_and_versioned(
    repo_root: Path,
) -> None:
    baseline = load_closed_loop_scenario(
        repo_root / "configs/cdu/scenarios/closed_loop_baseline_v0.1.0.json"
    )
    step = load_closed_loop_scenario(
        repo_root / "configs/cdu/scenarios/closed_loop_feed_step_v0.1.0.json"
    )
    control = load_control_config(
        repo_root / "configs/cdu/controllers/cdu_pi_v0.1.0.json"
    )

    assert baseline.duration_s == 14_400.0
    assert baseline.events == ()
    assert step.duration_s == 7_200.0
    assert step.events == (SetpointEvent(600.0, "feed_flow", 1.05),)
    validate_closed_loop_scenario_compatibility(control, baseline)
    validate_closed_loop_scenario_compatibility(control, step)


def test_closed_loop_scenario_rejects_direct_mv_target() -> None:
    with pytest.raises(ConfigurationError, match="setpoint_ratio"):
        ClosedLoopScenarioConfig.from_mapping(
            {
                "schema_version": "1.0.0",
                "scenario_version": "invalid-v0.1.0",
                "control_version": "cdu-pi-control-0.1.0",
                "config_version": "cdu-mini-config-0.1.0",
                "case_version": "case-20260604-v0.1.0",
                "model_version": "cdu-reduced-0.1.0",
                "parameter_set_version": "cdu-parameters-0.1.0",
                "name": "invalid direct command",
                "duration": {"value": 10.0, "unit": "s"},
                "time_step": {"value": 1.0, "unit": "s"},
                "events": [
                    {
                        "time": {"value": 1.0, "unit": "s"},
                        "target": "fresh_feed_flow_kg_s",
                        "value": 120.0,
                    }
                ],
                "metadata": {"synthetic": "true", "purpose": "negative test"},
            }
        )


def test_closed_loop_preflight_rejects_unknown_loop_and_duplicate_event(
    repo_root: Path,
) -> None:
    control = load_control_config(
        repo_root / "configs/cdu/controllers/cdu_pi_v0.1.0.json"
    )
    scenario = load_closed_loop_scenario(
        repo_root / "configs/cdu/scenarios/closed_loop_feed_step_v0.1.0.json"
    )
    unknown = replace(
        scenario,
        events=(SetpointEvent(10.0, "unknown_loop", 1.05),),
    )
    with pytest.raises(ValueError, match="unknown loops"):
        validate_closed_loop_scenario_compatibility(control, unknown)

    duplicate = replace(
        scenario,
        events=(
            SetpointEvent(10.0, "feed_flow", 1.02),
            SetpointEvent(10.0, "feed_flow", 1.05),
        ),
    )
    with pytest.raises(ValueError, match="repeats loop"):
        validate_closed_loop_scenario_compatibility(control, duplicate)

    near_duplicate = replace(
        scenario,
        events=(
            SetpointEvent(0.3, "feed_flow", 1.02),
            SetpointEvent(0.30000000000000004, "feed_flow", 1.05),
        ),
    )
    with pytest.raises(ValueError, match="repeats loop"):
        validate_closed_loop_scenario_compatibility(control, near_duplicate)


def test_closed_loop_scenario_version_mismatch_is_rejected(
    repo_root: Path,
) -> None:
    control = load_control_config(
        repo_root / "configs/cdu/controllers/cdu_pi_v0.1.0.json"
    )
    scenario = load_closed_loop_scenario(
        repo_root / "configs/cdu/scenarios/closed_loop_feed_step_v0.1.0.json"
    )
    with pytest.raises(ValueError, match="control_version"):
        validate_closed_loop_scenario_compatibility(
            control,
            replace(scenario, control_version="other-control-v0.1.0"),
        )
