"""High-level M2-to-M4 preparation and closed-loop scenario execution."""

from __future__ import annotations

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import CaseConfig, ModelConfig, validate_config_compatibility
from ..core.math_utils import ConvergenceError
from ..core.types import MaterialStream
from ..dynamics.initialization import initialize_open_loop_dynamic_model
from ..flowsheet.recycle import RecycleSettings, solve_recycle
from ..properties.components import ComponentCatalog
from .config import ControlConfig, validate_control_compatibility
from .results import ClosedLoopSimulationResult
from .scenario import ClosedLoopScenarioConfig
from .simulation import (
    simulate_closed_loop,
    validate_closed_loop_scenario_compatibility,
)


def run_closed_loop_scenario(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    control: ControlConfig,
    scenario: ClosedLoopScenarioConfig,
    *,
    recycle_settings: RecycleSettings | None = None,
    initial_reflux: MaterialStream | None = None,
    software_version: str = SOFTWARE_VERSION,
) -> ClosedLoopSimulationResult:
    """Validate all M4 inputs, solve M2, initialize M3, and run feedback."""

    versions = validate_config_compatibility(
        model,
        case,
        software_version=software_version,
        catalog=catalog,
    )
    validate_control_compatibility(control, model, case)
    validate_closed_loop_scenario_compatibility(control, scenario)
    recycle = solve_recycle(
        model,
        case,
        catalog,
        settings=recycle_settings,
        initial_reflux=initial_reflux,
        software_version=software_version,
    )
    if not recycle.converged:
        stage = recycle.failure_stage or "unknown"
        reason = recycle.failure_reason or "M2 prerequisite did not converge"
        raise ConvergenceError(f"M4 prerequisite failed at {stage}: {reason}")
    dynamic_model = initialize_open_loop_dynamic_model(
        model,
        case,
        catalog,
        recycle,
    )
    version_mapping = {
        name: value for name, value in versions.as_dict().items() if value is not None
    }
    return simulate_closed_loop(
        dynamic_model,
        control,
        scenario,
        versions=version_mapping,
    )


def run_closed_loop(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    control: ControlConfig,
    scenario: ClosedLoopScenarioConfig,
    *,
    recycle_settings: RecycleSettings | None = None,
    initial_reflux: MaterialStream | None = None,
    software_version: str = SOFTWARE_VERSION,
) -> ClosedLoopSimulationResult:
    """Short alias for :func:`run_closed_loop_scenario`."""

    return run_closed_loop_scenario(
        model,
        case,
        catalog,
        control,
        scenario,
        recycle_settings=recycle_settings,
        initial_reflux=initial_reflux,
        software_version=software_version,
    )
