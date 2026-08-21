from __future__ import annotations

from petroleum_rto.cdu import control


def test_m4_public_api_exports_complete_control_layer() -> None:
    expected = {
        "AUTOMATIC",
        "CONTROL_PAIRING_WHITELIST",
        "DIRECT",
        "MANUAL",
        "REQUIRED_CONTROL_LOOP_IDS",
        "REVERSE",
        "AssembledControlLoop",
        "ClosedLoopSample",
        "ClosedLoopScenarioConfig",
        "ClosedLoopSimulationResult",
        "ControlAcceptanceConfig",
        "ControlConfig",
        "ControlLoopAssembly",
        "ControlLoopConfig",
        "ControlLoopRecord",
        "ControlledVariableRef",
        "FurnaceFeedforward",
        "LoopInitializationDiagnostic",
        "LoopPerformance",
        "NormalizedPIController",
        "PIControllerSpec",
        "PIControllerState",
        "PIControllerUpdate",
        "SetpointEvent",
        "assemble_control_loops",
        "evaluate_closed_loop_acceptance",
        "load_closed_loop_scenario",
        "load_control_config",
        "run_closed_loop",
        "run_closed_loop_scenario",
        "simulate_closed_loop",
        "validate_closed_loop_scenario_compatibility",
        "validate_control_compatibility",
    }

    assert set(control.__all__) == expected
    assert all(hasattr(control, name) for name in expected)
