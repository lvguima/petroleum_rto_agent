"""Strict versioned configuration for the seven M4 primary PI loops."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

from ..core.config import (
    CaseConfig,
    ConfigurationError,
    ModelConfig,
    canonical_fingerprint,
    load_json,
    strict_keys,
)
from .controllers import (
    AUTOMATIC,
    DIRECT,
    REVERSE,
    ControlAction,
    ControlMode,
    PIControllerSpec,
)

ControlledVariableSource = Literal["actuator", "sensor"]
FeedforwardKind = Literal["none", "furnace_feed_flow"]

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTROLLED_VARIABLE_SOURCES = frozenset({"actuator", "sensor"})
_FEEDFORWARD_KINDS = frozenset({"none", "furnace_feed_flow"})
_METADATA_FIELDS = frozenset({"synthetic", "tuning_basis", "confidence", "purpose"})

REQUIRED_CONTROL_LOOP_IDS: Final[tuple[str, ...]] = (
    "feed_flow",
    "flash_inventory",
    "furnace_temperature",
    "top_pressure",
    "reflux_inventory",
    "bottom_inventory",
    "top_temperature",
)


@dataclass(frozen=True)
class ControlPairing:
    """One immutable allowed CV-MV pairing for the first M4 controller version."""

    controlled_variable_source: ControlledVariableSource
    controlled_variable_name: str
    manipulated_variable: str
    action: ControlAction
    feedforward: FeedforwardKind = "none"


CONTROL_PAIRING_WHITELIST: Final[Mapping[str, ControlPairing]] = MappingProxyType(
    {
        "feed_flow": ControlPairing(
            "actuator",
            "fresh_feed_flow_kg_s",
            "fresh_feed_flow_kg_s",
            DIRECT,
        ),
        "flash_inventory": ControlPairing(
            "sensor",
            "flash_drum_inventory_kg",
            "flash_liquid_outflow_kg_s",
            REVERSE,
        ),
        "furnace_temperature": ControlPairing(
            "sensor",
            "furnace_outlet_temperature_k",
            "furnace_fuel_duty_w",
            DIRECT,
            "furnace_feed_flow",
        ),
        "top_pressure": ControlPairing(
            "sensor",
            "tower_top_pressure_pa",
            "top_gas_vent_kg_s",
            REVERSE,
        ),
        "reflux_inventory": ControlPairing(
            "sensor",
            "reflux_drum_inventory_kg",
            "gasoline_draw_kg_s",
            REVERSE,
        ),
        "bottom_inventory": ControlPairing(
            "sensor",
            "tower_bottom_inventory_kg",
            "residue_draw_kg_s",
            REVERSE,
        ),
        "top_temperature": ControlPairing(
            "sensor",
            "tower_top_temperature_k",
            "pump_around_1_duty_w",
            REVERSE,
        ),
    }
)


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ConfigurationError(f"{context} must be a non-empty identifier")
    return value


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigurationError(f"{context} must be finite")
    return number


def _positive_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number <= 0.0:
        raise ConfigurationError(f"{context} must be positive")
    return number


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ControlledVariableRef:
    """Validated location of one measured controlled variable."""

    source: ControlledVariableSource
    name: str

    def __post_init__(self) -> None:
        if self.source not in _CONTROLLED_VARIABLE_SOURCES:
            raise ConfigurationError("controlled variable source must be actuator or sensor")
        object.__setattr__(
            self,
            "source",
            self.source,
        )
        object.__setattr__(
            self,
            "name",
            _identifier(self.name, context="controlled variable name"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ControlledVariableRef:
        strict_keys(
            value,
            required={"source", "name"},
            context="controlled variable",
        )
        source = value["source"]
        if source not in _CONTROLLED_VARIABLE_SOURCES:
            raise ConfigurationError("controlled variable source must be actuator or sensor")
        return cls(
            source=cast(ControlledVariableSource, source),
            name=_identifier(value["name"], context="controlled variable name"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "name": self.name}


@dataclass(frozen=True)
class ControlLoopConfig:
    """Frozen pairing, tuning, ramp and command-envelope settings for one loop."""

    loop_id: str
    controlled_variable: ControlledVariableRef
    manipulated_variable: str
    action: ControlAction
    proportional_gain: float
    integral_time_s: float
    anti_windup_time_s: float
    setpoint_rate_limit_fraction_per_s: float
    output_min_ratio: float
    output_max_ratio: float
    output_rate_limit_fraction_per_s: float
    initial_mode: ControlMode
    feedforward: FeedforwardKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "loop_id", _identifier(self.loop_id, context="loop_id"))
        if not isinstance(self.controlled_variable, ControlledVariableRef):
            raise TypeError("controlled_variable must be a ControlledVariableRef")
        object.__setattr__(
            self,
            "manipulated_variable",
            _identifier(self.manipulated_variable, context="manipulated_variable"),
        )
        if self.action not in (DIRECT, REVERSE):
            raise ConfigurationError("action must be direct or reverse")
        if self.initial_mode not in (AUTOMATIC, "manual"):
            raise ConfigurationError("initial_mode must be automatic or manual")
        if self.feedforward not in _FEEDFORWARD_KINDS:
            raise ConfigurationError("unsupported feedforward kind")
        object.__setattr__(self, "action", self.action)
        object.__setattr__(self, "initial_mode", self.initial_mode)
        object.__setattr__(
            self,
            "feedforward",
            self.feedforward,
        )
        for name in (
            "proportional_gain",
            "integral_time_s",
            "anti_windup_time_s",
            "setpoint_rate_limit_fraction_per_s",
            "output_rate_limit_fraction_per_s",
        ):
            object.__setattr__(
                self,
                name,
                _positive_number(getattr(self, name), context=f"{self.loop_id}.{name}"),
            )
        output_min = _finite_number(
            self.output_min_ratio,
            context=f"{self.loop_id}.output_min_ratio",
        )
        output_max = _finite_number(
            self.output_max_ratio,
            context=f"{self.loop_id}.output_max_ratio",
        )
        if output_min < 0.0 or output_min >= output_max:
            raise ConfigurationError("control-loop output ratios are invalid")
        if not output_min < 1.0 < output_max:
            raise ConfigurationError(
                "control-loop output ratios must strictly bracket nominal 1.0"
            )
        object.__setattr__(self, "output_min_ratio", output_min)
        object.__setattr__(self, "output_max_ratio", output_max)

    @classmethod
    def from_mapping(
        cls,
        loop_id: str,
        value: Mapping[str, object],
    ) -> ControlLoopConfig:
        required_fields = {
            "controlled_variable",
            "manipulated_variable",
            "action",
            "proportional_gain",
            "integral_time_s",
            "anti_windup_time_s",
            "setpoint_rate_limit_fraction_per_s",
            "output_min_ratio",
            "output_max_ratio",
            "output_rate_limit_fraction_per_s",
            "initial_mode",
            "feedforward",
        }
        strict_keys(value, required=required_fields, context=f"control loop {loop_id}")
        controlled_variable = ControlledVariableRef.from_mapping(
            _mapping(
                value["controlled_variable"],
                context=f"control loop {loop_id}.controlled_variable",
            )
        )
        action = value["action"]
        mode = value["initial_mode"]
        feedforward = value["feedforward"]
        return cls(
            loop_id=_identifier(loop_id, context="loop_id"),
            controlled_variable=controlled_variable,
            manipulated_variable=_identifier(
                value["manipulated_variable"],
                context=f"control loop {loop_id}.manipulated_variable",
            ),
            action=cast(ControlAction, action),
            proportional_gain=_positive_number(
                value["proportional_gain"],
                context=f"control loop {loop_id}.proportional_gain",
            ),
            integral_time_s=_positive_number(
                value["integral_time_s"],
                context=f"control loop {loop_id}.integral_time_s",
            ),
            anti_windup_time_s=_positive_number(
                value["anti_windup_time_s"],
                context=f"control loop {loop_id}.anti_windup_time_s",
            ),
            setpoint_rate_limit_fraction_per_s=_positive_number(
                value["setpoint_rate_limit_fraction_per_s"],
                context=(
                    f"control loop {loop_id}.setpoint_rate_limit_fraction_per_s"
                ),
            ),
            output_min_ratio=_finite_number(
                value["output_min_ratio"],
                context=f"control loop {loop_id}.output_min_ratio",
            ),
            output_max_ratio=_finite_number(
                value["output_max_ratio"],
                context=f"control loop {loop_id}.output_max_ratio",
            ),
            output_rate_limit_fraction_per_s=_positive_number(
                value["output_rate_limit_fraction_per_s"],
                context=(
                    f"control loop {loop_id}.output_rate_limit_fraction_per_s"
                ),
            ),
            initial_mode=cast(ControlMode, mode),
            feedforward=cast(FeedforwardKind, feedforward),
        )

    def controller_spec(self) -> PIControllerSpec:
        return PIControllerSpec(
            loop_id=self.loop_id,
            action=self.action,
            proportional_gain=self.proportional_gain,
            integral_time_s=self.integral_time_s,
            anti_windup_time_s=self.anti_windup_time_s,
            setpoint_rate_limit_fraction_per_s=(
                self.setpoint_rate_limit_fraction_per_s
            ),
            output_min_ratio=self.output_min_ratio,
            output_max_ratio=self.output_max_ratio,
            output_rate_limit_fraction_per_s=(
                self.output_rate_limit_fraction_per_s
            ),
            initial_mode=self.initial_mode,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "controlled_variable": self.controlled_variable.as_dict(),
            "manipulated_variable": self.manipulated_variable,
            "action": self.action,
            "proportional_gain": self.proportional_gain,
            "integral_time_s": self.integral_time_s,
            "anti_windup_time_s": self.anti_windup_time_s,
            "setpoint_rate_limit_fraction_per_s": (
                self.setpoint_rate_limit_fraction_per_s
            ),
            "output_min_ratio": self.output_min_ratio,
            "output_max_ratio": self.output_max_ratio,
            "output_rate_limit_fraction_per_s": (
                self.output_rate_limit_fraction_per_s
            ),
            "initial_mode": self.initial_mode,
            "feedforward": self.feedforward,
        }


@dataclass(frozen=True)
class ControlAcceptanceConfig:
    """Versioned quantitative gates for the first synthetic closed-loop scenarios."""

    baseline_tail_window_s: float
    baseline_error_fraction: float
    baseline_slope_fraction_per_s: float
    feed_band_fraction: float
    inventory_band_fraction: float
    temperature_pressure_band_fraction: float
    recovery_time_s: Mapping[str, float]
    settling_dwell_s: float
    inventory_true_min_ratio: float
    inventory_true_max_ratio: float
    tail_window_s: float
    tail_mean_abs_error_fraction: float
    tail_slope_fraction_per_s: float
    max_continuous_saturation_s: float
    tail_peak_to_peak_fraction: float

    def __post_init__(self) -> None:
        for name in (
            "baseline_tail_window_s",
            "baseline_error_fraction",
            "baseline_slope_fraction_per_s",
            "feed_band_fraction",
            "inventory_band_fraction",
            "temperature_pressure_band_fraction",
            "settling_dwell_s",
            "tail_window_s",
            "tail_mean_abs_error_fraction",
            "tail_slope_fraction_per_s",
            "max_continuous_saturation_s",
            "tail_peak_to_peak_fraction",
        ):
            object.__setattr__(
                self,
                name,
                _positive_number(getattr(self, name), context=f"acceptance.{name}"),
            )
        inventory_min = _finite_number(
            self.inventory_true_min_ratio,
            context="acceptance.inventory_true_min_ratio",
        )
        inventory_max = _finite_number(
            self.inventory_true_max_ratio,
            context="acceptance.inventory_true_max_ratio",
        )
        if inventory_min < 0.0 or inventory_min >= inventory_max:
            raise ConfigurationError("acceptance inventory true-state ratios are invalid")
        if not inventory_min < 1.0 < inventory_max:
            raise ConfigurationError(
                "acceptance inventory true-state ratios must bracket nominal 1.0"
            )
        object.__setattr__(self, "inventory_true_min_ratio", inventory_min)
        object.__setattr__(self, "inventory_true_max_ratio", inventory_max)
        if set(self.recovery_time_s) != set(REQUIRED_CONTROL_LOOP_IDS):
            raise ConfigurationError(
                "acceptance recovery_time_s must cover exactly the seven control loops"
            )
        recovery_times = {
            loop_id: _positive_number(
                self.recovery_time_s[loop_id],
                context=f"acceptance.recovery_time_s.{loop_id}",
            )
            for loop_id in REQUIRED_CONTROL_LOOP_IDS
        }
        object.__setattr__(self, "recovery_time_s", MappingProxyType(recovery_times))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ControlAcceptanceConfig:
        required_fields = {
            "baseline_tail_window_s",
            "baseline_error_fraction",
            "baseline_slope_fraction_per_s",
            "feed_band_fraction",
            "inventory_band_fraction",
            "temperature_pressure_band_fraction",
            "recovery_time_s",
            "settling_dwell_s",
            "inventory_true_min_ratio",
            "inventory_true_max_ratio",
            "tail_window_s",
            "tail_mean_abs_error_fraction",
            "tail_slope_fraction_per_s",
            "max_continuous_saturation_s",
            "tail_peak_to_peak_fraction",
        }
        strict_keys(value, required=required_fields, context="control acceptance")
        recovery_times = _mapping(
            value["recovery_time_s"],
            context="control acceptance recovery_time_s",
        )
        return cls(
            baseline_tail_window_s=_positive_number(
                value["baseline_tail_window_s"],
                context="acceptance.baseline_tail_window_s",
            ),
            baseline_error_fraction=_positive_number(
                value["baseline_error_fraction"],
                context="acceptance.baseline_error_fraction",
            ),
            baseline_slope_fraction_per_s=_positive_number(
                value["baseline_slope_fraction_per_s"],
                context="acceptance.baseline_slope_fraction_per_s",
            ),
            feed_band_fraction=_positive_number(
                value["feed_band_fraction"],
                context="acceptance.feed_band_fraction",
            ),
            inventory_band_fraction=_positive_number(
                value["inventory_band_fraction"],
                context="acceptance.inventory_band_fraction",
            ),
            temperature_pressure_band_fraction=_positive_number(
                value["temperature_pressure_band_fraction"],
                context="acceptance.temperature_pressure_band_fraction",
            ),
            recovery_time_s={
                loop_id: _positive_number(
                    recovery_time,
                    context=f"acceptance.recovery_time_s.{loop_id}",
                )
                for loop_id, recovery_time in recovery_times.items()
            },
            settling_dwell_s=_positive_number(
                value["settling_dwell_s"],
                context="acceptance.settling_dwell_s",
            ),
            inventory_true_min_ratio=_finite_number(
                value["inventory_true_min_ratio"],
                context="acceptance.inventory_true_min_ratio",
            ),
            inventory_true_max_ratio=_finite_number(
                value["inventory_true_max_ratio"],
                context="acceptance.inventory_true_max_ratio",
            ),
            tail_window_s=_positive_number(
                value["tail_window_s"],
                context="acceptance.tail_window_s",
            ),
            tail_mean_abs_error_fraction=_positive_number(
                value["tail_mean_abs_error_fraction"],
                context="acceptance.tail_mean_abs_error_fraction",
            ),
            tail_slope_fraction_per_s=_positive_number(
                value["tail_slope_fraction_per_s"],
                context="acceptance.tail_slope_fraction_per_s",
            ),
            max_continuous_saturation_s=_positive_number(
                value["max_continuous_saturation_s"],
                context="acceptance.max_continuous_saturation_s",
            ),
            tail_peak_to_peak_fraction=_positive_number(
                value["tail_peak_to_peak_fraction"],
                context="acceptance.tail_peak_to_peak_fraction",
            ),
        )

    def band_fraction(self, loop_id: str) -> float:
        if loop_id == "feed_flow":
            return self.feed_band_fraction
        if loop_id in {"flash_inventory", "reflux_inventory", "bottom_inventory"}:
            return self.inventory_band_fraction
        if loop_id not in REQUIRED_CONTROL_LOOP_IDS:
            raise KeyError(f"unknown control loop {loop_id!r}")
        return self.temperature_pressure_band_fraction

    def as_dict(self) -> dict[str, object]:
        return {
            "baseline_tail_window_s": self.baseline_tail_window_s,
            "baseline_error_fraction": self.baseline_error_fraction,
            "baseline_slope_fraction_per_s": self.baseline_slope_fraction_per_s,
            "feed_band_fraction": self.feed_band_fraction,
            "inventory_band_fraction": self.inventory_band_fraction,
            "temperature_pressure_band_fraction": (
                self.temperature_pressure_band_fraction
            ),
            "recovery_time_s": {
                loop_id: self.recovery_time_s[loop_id]
                for loop_id in REQUIRED_CONTROL_LOOP_IDS
            },
            "settling_dwell_s": self.settling_dwell_s,
            "inventory_true_min_ratio": self.inventory_true_min_ratio,
            "inventory_true_max_ratio": self.inventory_true_max_ratio,
            "tail_window_s": self.tail_window_s,
            "tail_mean_abs_error_fraction": self.tail_mean_abs_error_fraction,
            "tail_slope_fraction_per_s": self.tail_slope_fraction_per_s,
            "max_continuous_saturation_s": self.max_continuous_saturation_s,
            "tail_peak_to_peak_fraction": self.tail_peak_to_peak_fraction,
        }


@dataclass(frozen=True)
class ControlConfig:
    """Complete strict M4 control configuration and stable fingerprint source."""

    schema_version: str
    control_version: str
    model_version: str
    config_version: str
    parameter_set_version: str
    tuning_basis_case_version: str
    control_interval_s: float
    loops: Mapping[str, ControlLoopConfig]
    acceptance: ControlAcceptanceConfig
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in (
            "schema_version",
            "control_version",
            "model_version",
            "config_version",
            "parameter_set_version",
            "tuning_basis_case_version",
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), context=name),
            )
        object.__setattr__(
            self,
            "control_interval_s",
            _positive_number(self.control_interval_s, context="control_interval_s"),
        )
        if set(self.loops) != set(REQUIRED_CONTROL_LOOP_IDS):
            raise ConfigurationError(
                "control loop ids differ; "
                f"missing={sorted(set(REQUIRED_CONTROL_LOOP_IDS) - set(self.loops))}, "
                f"unknown={sorted(set(self.loops) - set(REQUIRED_CONTROL_LOOP_IDS))}"
            )
        frozen_loops: dict[str, ControlLoopConfig] = {}
        for loop_id in REQUIRED_CONTROL_LOOP_IDS:
            loop = self.loops[loop_id]
            if not isinstance(loop, ControlLoopConfig):
                raise TypeError("control loop values must be ControlLoopConfig instances")
            if loop.loop_id != loop_id:
                raise ConfigurationError("control loop key must equal loop_id")
            expected = CONTROL_PAIRING_WHITELIST[loop_id]
            actual = ControlPairing(
                loop.controlled_variable.source,
                loop.controlled_variable.name,
                loop.manipulated_variable,
                loop.action,
                loop.feedforward,
            )
            if actual != expected:
                raise ConfigurationError(f"control loop {loop_id!r} violates the M4 pairing whitelist")
            frozen_loops[loop_id] = loop
        manipulated_variables = [
            loop.manipulated_variable for loop in frozen_loops.values()
        ]
        if len(manipulated_variables) != len(set(manipulated_variables)):
            raise ConfigurationError("each manipulated variable must belong to one primary loop")

        if not isinstance(self.acceptance, ControlAcceptanceConfig):
            raise TypeError("acceptance must be a ControlAcceptanceConfig")

        if set(self.metadata) != _METADATA_FIELDS:
            raise ConfigurationError(
                "control metadata fields differ; "
                f"missing={sorted(_METADATA_FIELDS - set(self.metadata))}, "
                f"unknown={sorted(set(self.metadata) - _METADATA_FIELDS)}"
            )
        frozen_metadata = {
            name: _text(self.metadata[name], context=f"metadata.{name}")
            for name in sorted(_METADATA_FIELDS)
        }
        if frozen_metadata["synthetic"] != "true":
            raise ConfigurationError("control metadata.synthetic must be 'true'")
        if frozen_metadata["confidence"] != "low":
            raise ConfigurationError("initial M4 tuning confidence must be 'low'")
        object.__setattr__(self, "loops", MappingProxyType(frozen_loops))
        object.__setattr__(self, "metadata", MappingProxyType(frozen_metadata))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ControlConfig:
        required_fields = {
            "schema_version",
            "control_version",
            "model_version",
            "config_version",
            "parameter_set_version",
            "tuning_basis_case_version",
            "control_interval_s",
            "loops",
            "acceptance",
            "metadata",
        }
        strict_keys(value, required=required_fields, context="control configuration")
        raw_loops = _mapping(value["loops"], context="control loops")
        loops = {
            loop_id: ControlLoopConfig.from_mapping(
                loop_id,
                _mapping(raw_loop, context=f"control loop {loop_id}"),
            )
            for loop_id, raw_loop in raw_loops.items()
        }
        raw_metadata = _mapping(value["metadata"], context="control metadata")
        if any(not isinstance(item, str) for item in raw_metadata.values()):
            raise ConfigurationError("control metadata values must be strings")
        return cls(
            schema_version=_identifier(value["schema_version"], context="schema_version"),
            control_version=_identifier(
                value["control_version"],
                context="control_version",
            ),
            model_version=_identifier(value["model_version"], context="model_version"),
            config_version=_identifier(value["config_version"], context="config_version"),
            parameter_set_version=_identifier(
                value["parameter_set_version"],
                context="parameter_set_version",
            ),
            tuning_basis_case_version=_identifier(
                value["tuning_basis_case_version"],
                context="tuning_basis_case_version",
            ),
            control_interval_s=_positive_number(
                value["control_interval_s"],
                context="control_interval_s",
            ),
            loops=loops,
            acceptance=ControlAcceptanceConfig.from_mapping(
                _mapping(value["acceptance"], context="control acceptance")
            ),
            metadata=cast(Mapping[str, str], raw_metadata),
        )

    def loop(self, loop_id: str) -> ControlLoopConfig:
        try:
            return self.loops[loop_id]
        except KeyError as exc:
            raise KeyError(f"unknown control loop {loop_id!r}") from exc

    @property
    def input_fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "control_version": self.control_version,
            "model_version": self.model_version,
            "config_version": self.config_version,
            "parameter_set_version": self.parameter_set_version,
            "tuning_basis_case_version": self.tuning_basis_case_version,
            "control_interval_s": self.control_interval_s,
            "loops": {
                loop_id: self.loops[loop_id].as_dict()
                for loop_id in REQUIRED_CONTROL_LOOP_IDS
            },
            "acceptance": self.acceptance.as_dict(),
            "metadata": dict(self.metadata),
        }


def load_control_config(path: Path) -> ControlConfig:
    """Load and strictly validate one UTF-8 M4 control configuration."""

    try:
        return ControlConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid control configuration {path}: {exc}") from exc


def validate_control_compatibility(
    control: ControlConfig,
    model: ModelConfig,
    case: CaseConfig,
) -> None:
    """Reject a tuning file that does not describe the current M3 operating basis."""

    if not isinstance(control, ControlConfig):
        raise TypeError("control must be a ControlConfig")
    if not isinstance(model, ModelConfig) or not isinstance(case, CaseConfig):
        raise TypeError("model and case must be validated configuration objects")
    mismatches: list[str] = []
    checks = {
        "schema_version": (control.schema_version, model.schema_version),
        "model_version": (control.model_version, model.model_version),
        "config_version": (control.config_version, model.config_version),
        "parameter_set_version": (
            control.parameter_set_version,
            model.parameter_set_version,
        ),
        "tuning_basis_case_version": (
            control.tuning_basis_case_version,
            case.case_version,
        ),
    }
    mismatches.extend(name for name, pair in checks.items() if pair[0] != pair[1])
    if mismatches:
        raise ConfigurationError(
            "control configuration version mismatch: " + ", ".join(sorted(mismatches))
        )
