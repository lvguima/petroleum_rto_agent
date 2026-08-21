from __future__ import annotations

from petroleum_rto.cdu import dynamics


def test_dynamic_package_exposes_the_supported_m3_api() -> None:
    expected = {
        "ActuatorSpec",
        "CommandEvent",
        "CommandSchedule",
        "DynamicConservationError",
        "DynamicConservationTolerances",
        "DynamicCumulativeBalance",
        "DynamicEvaluation",
        "DynamicSample",
        "DynamicSimulationResult",
        "DynamicState",
        "InventoryState",
        "OpenLoopDynamicModel",
        "SensorSpec",
        "initialize_dynamic_model",
        "initialize_open_loop_dynamic_model",
        "run_dynamic",
        "run_dynamic_scenario",
        "schedule_from_scenario",
        "simulate_dynamic",
    }

    assert expected <= set(dynamics.__all__)
    assert all(getattr(dynamics, name) is not None for name in expected)
