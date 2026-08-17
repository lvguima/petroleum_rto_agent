"""Core data contracts, configuration and numerical helpers."""

from .config import (
    CaseConfig,
    ModelConfig,
    ScenarioConfig,
    input_bundle_fingerprint,
    validate_config_compatibility,
)
from .conservation import material_balance
from .types import (
    BalanceReport,
    ControlSignals,
    EquipmentState,
    MaterialStream,
    SimulationResult,
    UnitResult,
    merge_streams,
    stream_from_component_flows,
)
from .versions import VersionBundle

__all__ = [
    "BalanceReport",
    "CaseConfig",
    "ControlSignals",
    "EquipmentState",
    "MaterialStream",
    "ModelConfig",
    "ScenarioConfig",
    "SimulationResult",
    "UnitResult",
    "VersionBundle",
    "input_bundle_fingerprint",
    "material_balance",
    "merge_streams",
    "stream_from_component_flows",
    "validate_config_compatibility",
]
