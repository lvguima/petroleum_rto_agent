"""Public API for the M4 synthetic PI control layer."""

from .config import (
    CONTROL_PAIRING_WHITELIST,
    REQUIRED_CONTROL_LOOP_IDS,
    ControlAcceptanceConfig,
    ControlConfig,
    ControlledVariableRef,
    ControlLoopConfig,
    load_control_config,
    validate_control_compatibility,
)
from .controllers import (
    AUTOMATIC,
    DIRECT,
    MANUAL,
    REVERSE,
    NormalizedPIController,
    PIControllerSpec,
    PIControllerState,
    PIControllerUpdate,
)
from .loops import (
    AssembledControlLoop,
    ControlLoopAssembly,
    FurnaceFeedforward,
    LoopInitializationDiagnostic,
    assemble_control_loops,
)
from .metrics import evaluate_closed_loop_acceptance
from .results import (
    ClosedLoopSample,
    ClosedLoopSimulationResult,
    ControlLoopRecord,
    LoopPerformance,
)
from .runner import run_closed_loop, run_closed_loop_scenario
from .scenario import (
    ClosedLoopScenarioConfig,
    SetpointEvent,
    load_closed_loop_scenario,
)
from .simulation import (
    simulate_closed_loop,
    validate_closed_loop_scenario_compatibility,
)

__all__ = [
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
]
