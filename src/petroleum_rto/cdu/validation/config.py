"""Strict versioned configuration for the M6 engineering-validation suite."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

from ..control.config import REQUIRED_CONTROL_LOOP_IDS
from ..core.config import (
    ConfigurationError,
    canonical_fingerprint,
    load_json,
    strict_keys,
)
from ..dynamics.state import ACTUATOR_STATE_NAMES
from .domain import DomainDimension, assess_applicability
from .protection import ProtectionAction, ProtectionRule
from .uncertainty import (
    EngineeringInputInterval,
    InputSensitivitySpec,
    OutputSensitivitySpec,
)

type ScenarioClass = Literal["normal", "limited", "structural_rejection"]
type ScenarioExecutionLayer = Literal[
    "M2_steady",
    "M3_open_loop",
    "M4_closed_loop",
    "M6_supervision",
    "structural_rejection",
]
type ExpectedScenarioStatus = Literal["passed", "limited", "rejected"]
type UncertaintyExecutionLayer = Literal["M2_steady", "M3_open_loop"]

M6_SCHEMA_VERSION: Final[str] = "1.0.0"
M6_VALIDATION_VERSION: Final[str] = "m6-validation-v0.1.0"
M6_ANALYSIS_BASIS_VERSION: Final[str] = "m6-basis-v0.1.0"
M6_CLAIM_SCOPE: Final[str] = "engineering_validation_only"
M6_DATA_ORIGIN: Final[str] = "M6_synthetic_validation"

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_SCENARIO_CLASSES: Final[frozenset[str]] = frozenset(
    {"normal", "limited", "structural_rejection"}
)
_SCENARIO_LAYERS: Final[frozenset[str]] = frozenset(
    {
        "M2_steady",
        "M3_open_loop",
        "M4_closed_loop",
        "M6_supervision",
        "structural_rejection",
    }
)
_EXPECTED_STATUSES: Final[frozenset[str]] = frozenset(
    {"passed", "limited", "rejected"}
)
_UNCERTAINTY_LAYERS: Final[frozenset[str]] = frozenset(
    {"M2_steady", "M3_open_loop"}
)
_EXPECTED_STATUS_BY_CLASS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "normal": "passed",
        "limited": "limited",
        "structural_rejection": "rejected",
    }
)
_EXPECTED_LINEAGE_VERSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "model_version": "cdu-reduced-0.1.0",
        "model_config_version": "cdu-mini-config-0.1.0",
        "base_parameter_set_version": "cdu-parameters-0.1.0",
        "derived_parameter_set_version": (
            "cdu-parameters-m5-case20260604-v0.1.0"
        ),
        "base_case_version": "case-20260604-v0.1.0",
        "derived_case_version": "case-20260604-m5-aligned-v0.1.0",
        "control_version": "cdu-pi-control-0.1.0",
    }
)
_METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {"synthetic", "data_origin", "claim_scope", "confidence", "purpose"}
)


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{context} must be a non-empty identifier")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be non-empty text")
    return value


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ConfigurationError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ConfigurationError(f"{context} must be a sequence")
    return cast(Sequence[object], value)


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigurationError(f"{context} must be finite")
    return number


def _nonnegative_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number < 0.0:
        raise ConfigurationError(f"{context} must be non-negative")
    return number


def _positive_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number <= 0.0:
        raise ConfigurationError(f"{context} must be positive")
    return number


def _boolean(value: object, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{context} must be a boolean")
    return value


def _nonnegative_integer(value: object, *, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"{context} must be an integer")
    if value < 0:
        raise ConfigurationError(f"{context} must be non-negative")
    return value


def _string_mapping(value: object, *, context: str) -> Mapping[str, str]:
    raw = _mapping(value, context=context)
    copied: dict[str, str] = {}
    for name in sorted(raw):
        copied[_identifier(name, context=f"{context} key")] = _text(
            raw[name],
            context=f"{context}.{name}",
        )
    return MappingProxyType(copied)


def _float_mapping(value: object, *, context: str) -> Mapping[str, float]:
    raw = _mapping(value, context=context)
    return MappingProxyType(
        {
            _identifier(name, context=f"{context} key"): _finite_number(
                raw[name],
                context=f"{context}.{name}",
            )
            for name in sorted(raw)
        }
    )


def _direction_mapping(value: object, *, context: str) -> Mapping[str, int]:
    raw = _mapping(value, context=context)
    copied: dict[str, int] = {}
    for name in sorted(raw):
        direction = raw[name]
        if (
            not isinstance(direction, int)
            or isinstance(direction, bool)
            or direction not in {-1, 0, 1}
        ):
            raise ConfigurationError(
                f"{context}.{name} must be one of -1, 0, or 1"
            )
        copied[_identifier(name, context=f"{context} key")] = direction
    return MappingProxyType(copied)


def _identifier_tuple(
    value: object,
    *,
    context: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    raw = _sequence(value, context=context)
    copied = tuple(
        _identifier(item, context=f"{context}[{index}]")
        for index, item in enumerate(raw)
    )
    if not copied and not allow_empty:
        raise ConfigurationError(f"{context} cannot be empty")
    if len(set(copied)) != len(copied):
        raise ConfigurationError(f"{context} must contain unique identifiers")
    return tuple(sorted(copied))


def _scenario_class(value: object) -> ScenarioClass:
    if not isinstance(value, str) or value not in _SCENARIO_CLASSES:
        raise ConfigurationError(
            "scenario_class must be normal, limited, or structural_rejection"
        )
    return cast(ScenarioClass, value)


def _scenario_layer(value: object) -> ScenarioExecutionLayer:
    if not isinstance(value, str) or value not in _SCENARIO_LAYERS:
        raise ConfigurationError("scenario execution_layer is unsupported")
    return cast(ScenarioExecutionLayer, value)


def _expected_status(value: object) -> ExpectedScenarioStatus:
    if not isinstance(value, str) or value not in _EXPECTED_STATUSES:
        raise ConfigurationError("scenario expected_status is unsupported")
    return cast(ExpectedScenarioStatus, value)


def _uncertainty_layer(value: object) -> UncertaintyExecutionLayer:
    if not isinstance(value, str) or value not in _UNCERTAINTY_LAYERS:
        raise ConfigurationError("uncertainty execution_layer is unsupported")
    return cast(UncertaintyExecutionLayer, value)


@dataclass(frozen=True)
class ValidationScenarioSpec:
    """One versioned M6 scenario declaration and its expected status."""

    scenario_id: str
    scenario_version: str
    scenario_class: ScenarioClass
    execution_layer: ScenarioExecutionLayer
    execution_reference: str
    inputs: Mapping[str, float]
    expected_status: ExpectedScenarioStatus
    abnormal_verification: bool
    expected_directions: Mapping[str, int]
    claim_ids: tuple[str, ...]
    purpose: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scenario_id",
            _identifier(self.scenario_id, context="scenario_id"),
        )
        object.__setattr__(
            self,
            "scenario_version",
            _identifier(self.scenario_version, context="scenario_version"),
        )
        scenario_class = _scenario_class(self.scenario_class)
        layer = _scenario_layer(self.execution_layer)
        status = _expected_status(self.expected_status)
        expected = _EXPECTED_STATUS_BY_CLASS[scenario_class]
        if status != expected:
            raise ConfigurationError(
                f"scenario class {scenario_class!r} requires expected_status {expected!r}"
            )
        if (scenario_class == "structural_rejection") != (
            layer == "structural_rejection"
        ):
            raise ConfigurationError(
                "structural_rejection class and execution layer must be paired"
            )
        if layer == "M6_supervision" and scenario_class != "limited":
            raise ConfigurationError("M6 supervision scenarios must be limited")
        if scenario_class == "normal" and self.abnormal_verification:
            raise ConfigurationError("normal scenarios cannot be abnormal verification")
        if not isinstance(self.abnormal_verification, bool):
            raise ConfigurationError("abnormal_verification must be a boolean")
        object.__setattr__(
            self,
            "execution_reference",
            _text(self.execution_reference, context="execution_reference"),
        )
        object.__setattr__(
            self,
            "inputs",
            _float_mapping(self.inputs, context=f"scenario {self.scenario_id} inputs"),
        )
        expected_directions = _direction_mapping(
            self.expected_directions,
            context=f"scenario {self.scenario_id} expected_directions",
        )
        if (
            layer in {"M2_steady", "M3_open_loop", "M4_closed_loop"}
            and not expected_directions
        ):
            raise ConfigurationError(
                f"model-executed scenario {self.scenario_id!r} "
                "expected_directions cannot be empty"
            )
        object.__setattr__(self, "expected_directions", expected_directions)
        object.__setattr__(
            self,
            "claim_ids",
            _identifier_tuple(
                self.claim_ids,
                context=f"scenario {self.scenario_id} claim_ids",
            ),
        )
        object.__setattr__(self, "scenario_class", scenario_class)
        object.__setattr__(self, "execution_layer", layer)
        object.__setattr__(self, "expected_status", status)
        object.__setattr__(self, "purpose", _text(self.purpose, context="purpose"))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> ValidationScenarioSpec:
        strict_keys(
            value,
            required={
                "scenario_id",
                "scenario_version",
                "scenario_class",
                "execution_layer",
                "execution_reference",
                "inputs",
                "expected_status",
                "abnormal_verification",
                "expected_directions",
                "claim_ids",
                "purpose",
            },
            context="M6 scenario",
        )
        return cls(
            scenario_id=_identifier(value["scenario_id"], context="scenario_id"),
            scenario_version=_identifier(
                value["scenario_version"], context="scenario_version"
            ),
            scenario_class=_scenario_class(value["scenario_class"]),
            execution_layer=_scenario_layer(value["execution_layer"]),
            execution_reference=_text(
                value["execution_reference"], context="execution_reference"
            ),
            inputs=_float_mapping(value["inputs"], context="scenario inputs"),
            expected_status=_expected_status(value["expected_status"]),
            abnormal_verification=_boolean(
                value["abnormal_verification"],
                context="abnormal_verification",
            ),
            expected_directions=_direction_mapping(
                value["expected_directions"],
                context="scenario expected_directions",
            ),
            claim_ids=_identifier_tuple(value["claim_ids"], context="claim_ids"),
            purpose=_text(value["purpose"], context="purpose"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_class": self.scenario_class,
            "execution_layer": self.execution_layer,
            "execution_reference": self.execution_reference,
            "inputs": dict(self.inputs),
            "expected_status": self.expected_status,
            "abnormal_verification": self.abnormal_verification,
            "expected_directions": dict(self.expected_directions),
            "claim_ids": list(self.claim_ids),
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class UncertaintyPlan:
    """Fixed-order sensitivity and engineering-envelope configuration."""

    plan_id: str
    execution_layer: UncertaintyExecutionLayer
    inputs: tuple[InputSensitivitySpec, ...]
    outputs: tuple[OutputSensitivitySpec, ...]
    intervals: tuple[EngineeringInputInterval, ...]
    unquantified_sources: tuple[str, ...]

    def __post_init__(self) -> None:
        plan_id = _identifier(self.plan_id, context="uncertainty plan_id")
        layer = _uncertainty_layer(self.execution_layer)
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        intervals = tuple(self.intervals)
        if not inputs or any(not isinstance(item, InputSensitivitySpec) for item in inputs):
            raise ConfigurationError(
                "uncertainty inputs must contain InputSensitivitySpec values"
            )
        if not outputs or any(
            not isinstance(item, OutputSensitivitySpec) for item in outputs
        ):
            raise ConfigurationError(
                "uncertainty outputs must contain OutputSensitivitySpec values"
            )
        if not intervals or any(
            not isinstance(item, EngineeringInputInterval) for item in intervals
        ):
            raise ConfigurationError(
                "uncertainty intervals must contain EngineeringInputInterval values"
            )
        input_ids = tuple(item.input_id for item in inputs)
        output_ids = tuple(item.output_id for item in outputs)
        interval_ids = tuple(item.input_id for item in intervals)
        if len(set(input_ids)) != len(input_ids):
            raise ConfigurationError("uncertainty input ids must be unique")
        if len(set(output_ids)) != len(output_ids):
            raise ConfigurationError("uncertainty output ids must be unique")
        if input_ids != interval_ids:
            raise ConfigurationError(
                "uncertainty interval ids must exactly match the input ids"
            )
        by_interval = {item.input_id: item for item in intervals}
        for item in inputs:
            by_interval[item.input_id].radius_about(item.reference_value)
        sources = _identifier_tuple(
            self.unquantified_sources,
            context=f"uncertainty plan {plan_id} unquantified_sources",
        )
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "execution_layer", layer)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "intervals", intervals)
        object.__setattr__(self, "unquantified_sources", sources)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UncertaintyPlan:
        strict_keys(
            value,
            required={
                "plan_id",
                "execution_layer",
                "inputs",
                "outputs",
                "intervals",
                "unquantified_sources",
            },
            context="M6 uncertainty plan",
        )
        input_values = _sequence(value["inputs"], context="uncertainty inputs")
        output_values = _sequence(value["outputs"], context="uncertainty outputs")
        interval_values = _sequence(
            value["intervals"], context="uncertainty intervals"
        )
        inputs = tuple(_input_spec_from_mapping(item) for item in input_values)
        outputs = tuple(_output_spec_from_mapping(item) for item in output_values)
        intervals = tuple(
            _input_interval_from_mapping(item) for item in interval_values
        )
        return cls(
            plan_id=_identifier(value["plan_id"], context="uncertainty plan_id"),
            execution_layer=_uncertainty_layer(value["execution_layer"]),
            inputs=inputs,
            outputs=outputs,
            intervals=intervals,
            unquantified_sources=_identifier_tuple(
                value["unquantified_sources"],
                context="unquantified_sources",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "execution_layer": self.execution_layer,
            "inputs": [item.as_dict() for item in self.inputs],
            "outputs": [item.as_dict() for item in self.outputs],
            "intervals": [
                {
                    "input_id": item.input_id,
                    "lower": item.lower,
                    "upper": item.upper,
                    "confidence_multiplier": item.confidence_multiplier,
                    "confidence_label": item.confidence_label,
                }
                for item in self.intervals
            ],
            "unquantified_sources": list(self.unquantified_sources),
        }


@dataclass(frozen=True)
class ReportAcceptanceThresholds:
    """Numerical and coverage gates used by the final M6 report."""

    minimum_scenario_coverage_fraction: float
    maximum_failed_scenarios: int
    maximum_mass_residual_kg_s: float
    maximum_component_residual_kg_s: float
    maximum_salt_residual_kg_s: float
    direction_absolute_tolerance: float
    protection_timing_tolerance_s: float
    controller_tracking_relative_tolerance: float
    uncertainty_width_tolerance: float
    require_exact_reproduction: bool

    def __post_init__(self) -> None:
        coverage = _positive_number(
            self.minimum_scenario_coverage_fraction,
            context="minimum_scenario_coverage_fraction",
        )
        if coverage > 1.0:
            raise ConfigurationError(
                "minimum_scenario_coverage_fraction cannot exceed one"
            )
        object.__setattr__(self, "minimum_scenario_coverage_fraction", coverage)
        object.__setattr__(
            self,
            "maximum_failed_scenarios",
            _nonnegative_integer(
                self.maximum_failed_scenarios,
                context="maximum_failed_scenarios",
            ),
        )
        for name in (
            "maximum_mass_residual_kg_s",
            "maximum_component_residual_kg_s",
            "maximum_salt_residual_kg_s",
            "direction_absolute_tolerance",
            "protection_timing_tolerance_s",
            "controller_tracking_relative_tolerance",
            "uncertainty_width_tolerance",
        ):
            object.__setattr__(
                self,
                name,
                _nonnegative_number(getattr(self, name), context=name),
            )
        if not isinstance(self.require_exact_reproduction, bool):
            raise ConfigurationError("require_exact_reproduction must be a boolean")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> ReportAcceptanceThresholds:
        fields = {
            "minimum_scenario_coverage_fraction",
            "maximum_failed_scenarios",
            "maximum_mass_residual_kg_s",
            "maximum_component_residual_kg_s",
            "maximum_salt_residual_kg_s",
            "direction_absolute_tolerance",
            "protection_timing_tolerance_s",
            "controller_tracking_relative_tolerance",
            "uncertainty_width_tolerance",
            "require_exact_reproduction",
        }
        strict_keys(value, required=fields, context="M6 report acceptance")
        return cls(
            minimum_scenario_coverage_fraction=_positive_number(
                value["minimum_scenario_coverage_fraction"],
                context="minimum_scenario_coverage_fraction",
            ),
            maximum_failed_scenarios=_nonnegative_integer(
                value["maximum_failed_scenarios"],
                context="maximum_failed_scenarios",
            ),
            maximum_mass_residual_kg_s=_nonnegative_number(
                value["maximum_mass_residual_kg_s"],
                context="maximum_mass_residual_kg_s",
            ),
            maximum_component_residual_kg_s=_nonnegative_number(
                value["maximum_component_residual_kg_s"],
                context="maximum_component_residual_kg_s",
            ),
            maximum_salt_residual_kg_s=_nonnegative_number(
                value["maximum_salt_residual_kg_s"],
                context="maximum_salt_residual_kg_s",
            ),
            direction_absolute_tolerance=_nonnegative_number(
                value["direction_absolute_tolerance"],
                context="direction_absolute_tolerance",
            ),
            protection_timing_tolerance_s=_nonnegative_number(
                value["protection_timing_tolerance_s"],
                context="protection_timing_tolerance_s",
            ),
            controller_tracking_relative_tolerance=_nonnegative_number(
                value["controller_tracking_relative_tolerance"],
                context="controller_tracking_relative_tolerance",
            ),
            uncertainty_width_tolerance=_nonnegative_number(
                value["uncertainty_width_tolerance"],
                context="uncertainty_width_tolerance",
            ),
            require_exact_reproduction=_boolean(
                value["require_exact_reproduction"],
                context="require_exact_reproduction",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "minimum_scenario_coverage_fraction": (
                self.minimum_scenario_coverage_fraction
            ),
            "maximum_failed_scenarios": self.maximum_failed_scenarios,
            "maximum_mass_residual_kg_s": self.maximum_mass_residual_kg_s,
            "maximum_component_residual_kg_s": (
                self.maximum_component_residual_kg_s
            ),
            "maximum_salt_residual_kg_s": self.maximum_salt_residual_kg_s,
            "direction_absolute_tolerance": self.direction_absolute_tolerance,
            "protection_timing_tolerance_s": self.protection_timing_tolerance_s,
            "controller_tracking_relative_tolerance": (
                self.controller_tracking_relative_tolerance
            ),
            "uncertainty_width_tolerance": self.uncertainty_width_tolerance,
            "require_exact_reproduction": self.require_exact_reproduction,
        }


@dataclass(frozen=True)
class M6ValidationConfig:
    """Top-level immutable M6 validation, protection, and uncertainty contract."""

    schema_version: str
    validation_version: str
    analysis_basis_version: str
    model_version: str
    model_config_version: str
    base_parameter_set_version: str
    derived_parameter_set_version: str
    base_case_version: str
    derived_case_version: str
    control_version: str
    claim_scope: str
    domain_dimensions: tuple[DomainDimension, ...]
    scenarios: tuple[ValidationScenarioSpec, ...]
    protection_rules: tuple[ProtectionRule, ...]
    steady_uncertainty: UncertaintyPlan
    dynamic_uncertainty: UncertaintyPlan
    report_acceptance: ReportAcceptanceThresholds
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        versions = {
            "schema_version": self.schema_version,
            "validation_version": self.validation_version,
            "analysis_basis_version": self.analysis_basis_version,
            "model_version": self.model_version,
            "model_config_version": self.model_config_version,
            "base_parameter_set_version": self.base_parameter_set_version,
            "derived_parameter_set_version": self.derived_parameter_set_version,
            "base_case_version": self.base_case_version,
            "derived_case_version": self.derived_case_version,
            "control_version": self.control_version,
            "claim_scope": self.claim_scope,
        }
        for name, value in versions.items():
            _identifier(value, context=name)
        if self.schema_version != M6_SCHEMA_VERSION:
            raise ConfigurationError("unsupported M6 schema_version")
        if self.validation_version != M6_VALIDATION_VERSION:
            raise ConfigurationError("unsupported M6 validation_version")
        if self.analysis_basis_version != M6_ANALYSIS_BASIS_VERSION:
            raise ConfigurationError("unsupported M6 analysis_basis_version")
        for name, expected in _EXPECTED_LINEAGE_VERSIONS.items():
            if versions[name] != expected:
                raise ConfigurationError(
                    f"M6 {name} must be {expected!r} for this validation version"
                )
        if self.claim_scope != M6_CLAIM_SCOPE:
            raise ConfigurationError("M6 claim_scope must be engineering_validation_only")
        if self.base_parameter_set_version == self.derived_parameter_set_version:
            raise ConfigurationError("base and derived parameter versions must differ")
        if self.base_case_version == self.derived_case_version:
            raise ConfigurationError("base and derived case versions must differ")

        raw_dimensions = tuple(self.domain_dimensions)
        raw_scenarios = tuple(self.scenarios)
        raw_rules = tuple(self.protection_rules)
        if not raw_dimensions or any(
            not isinstance(item, DomainDimension) for item in raw_dimensions
        ):
            raise ConfigurationError("domain_dimensions must contain dimensions")
        if not raw_scenarios or any(
            not isinstance(item, ValidationScenarioSpec) for item in raw_scenarios
        ):
            raise ConfigurationError("scenarios must contain scenario specs")
        if not raw_rules or any(
            not isinstance(item, ProtectionRule) for item in raw_rules
        ):
            raise ConfigurationError("protection_rules must contain protection rules")
        dimensions = tuple(
            sorted(raw_dimensions, key=lambda item: item.dimension_id)
        )
        scenarios = tuple(
            sorted(raw_scenarios, key=lambda item: item.scenario_id)
        )
        rules = tuple(
            sorted(raw_rules, key=lambda item: (item.priority, item.rule_id))
        )
        _require_unique_ids(
            tuple(item.dimension_id for item in dimensions),
            context="domain dimension",
        )
        _require_unique_ids(
            tuple(item.scenario_id for item in scenarios),
            context="scenario",
        )
        _require_unique_ids(
            tuple(item.scenario_version for item in scenarios),
            context="scenario version",
        )
        _require_unique_ids(
            tuple(item.rule_id for item in rules),
            context="protection rule",
        )

        domain_ids = {item.dimension_id for item in dimensions}
        for scenario in scenarios:
            assessment = assess_applicability(
                dimensions,
                scenario.inputs,
                abnormal_verification=scenario.abnormal_verification,
            )
            if assessment.status != scenario.expected_status:
                raise ConfigurationError(
                    f"scenario {scenario.scenario_id!r} expects "
                    f"{scenario.expected_status!r} but its applicability is "
                    f"{assessment.status!r}"
                )
        classes = {item.scenario_class for item in scenarios}
        if classes != _SCENARIO_CLASSES:
            raise ConfigurationError(
                "scenario suite must contain normal, limited, and structural rejection"
            )

        if not isinstance(self.steady_uncertainty, UncertaintyPlan):
            raise TypeError("steady_uncertainty must be an UncertaintyPlan")
        if not isinstance(self.dynamic_uncertainty, UncertaintyPlan):
            raise TypeError("dynamic_uncertainty must be an UncertaintyPlan")
        if self.steady_uncertainty.execution_layer != "M2_steady":
            raise ConfigurationError("steady uncertainty must execute on M2_steady")
        if self.dynamic_uncertainty.execution_layer != "M3_open_loop":
            raise ConfigurationError("dynamic uncertainty must execute on M3_open_loop")
        if self.steady_uncertainty.plan_id == self.dynamic_uncertainty.plan_id:
            raise ConfigurationError("uncertainty plan ids must differ")
        for plan in (self.steady_uncertainty, self.dynamic_uncertainty):
            unknown_inputs = sorted(
                {item.input_id for item in plan.inputs} - domain_ids
            )
            if unknown_inputs:
                raise ConfigurationError(
                    f"uncertainty plan {plan.plan_id!r} has unknown domain inputs: "
                    + ", ".join(unknown_inputs)
                )
            dimensions_by_id = {item.dimension_id: item for item in dimensions}
            intervals_by_id = {item.input_id: item for item in plan.intervals}
            for spec in plan.inputs:
                dimension = dimensions_by_id[spec.input_id]
                if dimension.representation == "unsupported":
                    raise ConfigurationError(
                        f"uncertainty input {spec.input_id!r} cannot be unsupported"
                    )
                if not (
                    dimension.limited_min
                    <= spec.reference_value - spec.central_step
                    <= spec.reference_value + spec.central_step
                    <= dimension.limited_max
                ):
                    raise ConfigurationError(
                        f"uncertainty step for {spec.input_id!r} leaves its limited domain"
                    )
                interval = intervals_by_id[spec.input_id]
                if (
                    interval.lower < dimension.limited_min
                    or interval.upper > dimension.limited_max
                ):
                    raise ConfigurationError(
                        f"uncertainty interval for {spec.input_id!r} leaves its limited domain"
                    )

        allowed_commands = set(ACTUATOR_STATE_NAMES)
        allowed_loops = set(REQUIRED_CONTROL_LOOP_IDS)
        for rule in rules:
            unknown_commands = sorted(
                set(rule.action.command_ratio_overrides) - allowed_commands
            )
            unknown_loops = sorted(
                set(rule.action.manual_tracking_loop_ids) - allowed_loops
            )
            if unknown_commands or unknown_loops:
                raise ConfigurationError(
                    f"protection rule {rule.rule_id!r} has unknown actions; "
                    f"commands={unknown_commands}, loops={unknown_loops}"
                )

        if not isinstance(self.report_acceptance, ReportAcceptanceThresholds):
            raise TypeError(
                "report_acceptance must be ReportAcceptanceThresholds"
            )
        metadata = _string_mapping(self.metadata, context="M6 metadata")
        if set(metadata) != _METADATA_FIELDS:
            raise ConfigurationError(
                "M6 metadata must contain exactly the fixed provenance fields"
            )
        if metadata["synthetic"] != "true":
            raise ConfigurationError("M6 metadata.synthetic must be true")
        if metadata["data_origin"] != M6_DATA_ORIGIN:
            raise ConfigurationError("M6 metadata.data_origin is invalid")
        if metadata["claim_scope"] != self.claim_scope:
            raise ConfigurationError("M6 metadata claim_scope differs from the config")

        object.__setattr__(self, "domain_dimensions", dimensions)
        object.__setattr__(self, "scenarios", scenarios)
        object.__setattr__(self, "protection_rules", rules)
        object.__setattr__(self, "metadata", metadata)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> M6ValidationConfig:
        strict_keys(
            value,
            required={
                "schema_version",
                "validation_version",
                "analysis_basis_version",
                "model_version",
                "model_config_version",
                "base_parameter_set_version",
                "derived_parameter_set_version",
                "base_case_version",
                "derived_case_version",
                "control_version",
                "claim_scope",
                "domain_dimensions",
                "scenarios",
                "protection_rules",
                "steady_uncertainty",
                "dynamic_uncertainty",
                "report_acceptance",
                "metadata",
            },
            context="M6 validation configuration",
        )
        dimensions = tuple(
            _domain_dimension_from_mapping(item)
            for item in _sequence(
                value["domain_dimensions"], context="domain_dimensions"
            )
        )
        scenarios = tuple(
            ValidationScenarioSpec.from_mapping(
                _mapping(item, context="scenario")
            )
            for item in _sequence(value["scenarios"], context="scenarios")
        )
        rules = tuple(
            _protection_rule_from_mapping(item)
            for item in _sequence(
                value["protection_rules"], context="protection_rules"
            )
        )
        return cls(
            schema_version=_identifier(
                value["schema_version"], context="schema_version"
            ),
            validation_version=_identifier(
                value["validation_version"], context="validation_version"
            ),
            analysis_basis_version=_identifier(
                value["analysis_basis_version"], context="analysis_basis_version"
            ),
            model_version=_identifier(value["model_version"], context="model_version"),
            model_config_version=_identifier(
                value["model_config_version"], context="model_config_version"
            ),
            base_parameter_set_version=_identifier(
                value["base_parameter_set_version"],
                context="base_parameter_set_version",
            ),
            derived_parameter_set_version=_identifier(
                value["derived_parameter_set_version"],
                context="derived_parameter_set_version",
            ),
            base_case_version=_identifier(
                value["base_case_version"], context="base_case_version"
            ),
            derived_case_version=_identifier(
                value["derived_case_version"], context="derived_case_version"
            ),
            control_version=_identifier(
                value["control_version"], context="control_version"
            ),
            claim_scope=_identifier(value["claim_scope"], context="claim_scope"),
            domain_dimensions=dimensions,
            scenarios=scenarios,
            protection_rules=rules,
            steady_uncertainty=UncertaintyPlan.from_mapping(
                _mapping(value["steady_uncertainty"], context="steady_uncertainty")
            ),
            dynamic_uncertainty=UncertaintyPlan.from_mapping(
                _mapping(value["dynamic_uncertainty"], context="dynamic_uncertainty")
            ),
            report_acceptance=ReportAcceptanceThresholds.from_mapping(
                _mapping(value["report_acceptance"], context="report_acceptance")
            ),
            metadata=_string_mapping(value["metadata"], context="metadata"),
        )

    @property
    def input_fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def versions(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "validation_version": self.validation_version,
                "analysis_basis_version": self.analysis_basis_version,
                "model_version": self.model_version,
                "model_config_version": self.model_config_version,
                "base_parameter_set_version": self.base_parameter_set_version,
                "derived_parameter_set_version": self.derived_parameter_set_version,
                "base_case_version": self.base_case_version,
                "derived_case_version": self.derived_case_version,
                "control_version": self.control_version,
            }
        )

    def domain_dimension(self, dimension_id: str) -> DomainDimension:
        selected = _identifier(dimension_id, context="dimension_id")
        for dimension in self.domain_dimensions:
            if dimension.dimension_id == selected:
                return dimension
        raise KeyError(selected)

    def scenario(self, scenario_id: str) -> ValidationScenarioSpec:
        selected = _identifier(scenario_id, context="scenario_id")
        for scenario in self.scenarios:
            if scenario.scenario_id == selected:
                return scenario
        raise KeyError(selected)

    def protection_rule(self, rule_id: str) -> ProtectionRule:
        selected = _identifier(rule_id, context="rule_id")
        for rule in self.protection_rules:
            if rule.rule_id == selected:
                return rule
        raise KeyError(selected)

    def as_dict(self) -> dict[str, object]:
        protection_rules: list[dict[str, object]] = []
        for item in self.protection_rules:
            serialized = item.as_dict()
            if item.condition == "invalid":
                del serialized["trip_threshold"]
                del serialized["clear_threshold"]
            protection_rules.append(serialized)
        return {
            "schema_version": self.schema_version,
            "validation_version": self.validation_version,
            "analysis_basis_version": self.analysis_basis_version,
            "model_version": self.model_version,
            "model_config_version": self.model_config_version,
            "base_parameter_set_version": self.base_parameter_set_version,
            "derived_parameter_set_version": self.derived_parameter_set_version,
            "base_case_version": self.base_case_version,
            "derived_case_version": self.derived_case_version,
            "control_version": self.control_version,
            "claim_scope": self.claim_scope,
            "domain_dimensions": [
                item.as_dict() for item in self.domain_dimensions
            ],
            "scenarios": [item.as_dict() for item in self.scenarios],
            "protection_rules": protection_rules,
            "steady_uncertainty": self.steady_uncertainty.as_dict(),
            "dynamic_uncertainty": self.dynamic_uncertainty.as_dict(),
            "report_acceptance": self.report_acceptance.as_dict(),
            "metadata": dict(self.metadata),
        }


def _require_unique_ids(values: tuple[str, ...], *, context: str) -> None:
    if len(set(values)) != len(values):
        raise ConfigurationError(f"{context} ids must be unique")


def _domain_dimension_from_mapping(value: object) -> DomainDimension:
    raw = _mapping(value, context="domain dimension")
    strict_keys(
        raw,
        required={
            "dimension_id",
            "unit",
            "representation",
            "input_layer",
            "confidence",
            "assumptions",
            "reference_value",
            "normal_min",
            "normal_max",
            "limited_min",
            "limited_max",
            "source",
        },
        context="domain dimension",
    )
    try:
        return DomainDimension(
            dimension_id=_identifier(
                raw["dimension_id"], context="domain dimension_id"
            ),
            unit=_text(raw["unit"], context="domain unit"),
            representation=cast(
                Literal["direct", "proxy", "unsupported"],
                raw["representation"],
            ),
            input_layer=cast(
                Literal[
                    "M2_steady",
                    "M3_open_loop",
                    "M4_closed_loop",
                    "M6_supervision",
                    "structural_rejection",
                    "M2_M3_shared",
                    "M2_M3_M4_shared",
                ],
                raw["input_layer"],
            ),
            confidence=cast(
                Literal[
                    "low_engineering",
                    "low_case_observation",
                    "low_single_case_aligned",
                    "low_proxy",
                    "synthetic_logic_only",
                    "not_applicable",
                ],
                raw["confidence"],
            ),
            assumptions=_identifier_tuple(
                raw["assumptions"],
                context="domain assumptions",
            ),
            reference_value=_finite_number(
                raw["reference_value"], context="domain reference_value"
            ),
            normal_min=_finite_number(raw["normal_min"], context="domain normal_min"),
            normal_max=_finite_number(raw["normal_max"], context="domain normal_max"),
            limited_min=_finite_number(
                raw["limited_min"], context="domain limited_min"
            ),
            limited_max=_finite_number(
                raw["limited_max"], context="domain limited_max"
            ),
            source=_text(raw["source"], context="domain source"),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid domain dimension: {exc}") from exc


def _protection_action_from_mapping(value: object) -> ProtectionAction:
    raw = _mapping(value, context="protection action")
    strict_keys(
        raw,
        required={"command_ratio_overrides", "manual_tracking_loop_ids"},
        context="protection action",
    )
    try:
        return ProtectionAction(
            command_ratio_overrides=_float_mapping(
                raw["command_ratio_overrides"],
                context="protection command ratios",
            ),
            manual_tracking_loop_ids=_identifier_tuple(
                raw["manual_tracking_loop_ids"],
                context="manual_tracking_loop_ids",
                allow_empty=True,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid protection action: {exc}") from exc


def _protection_rule_from_mapping(value: object) -> ProtectionRule:
    raw = _mapping(value, context="protection rule")
    strict_keys(
        raw,
        required={
            "rule_id",
            "priority",
            "condition",
            "signal_name",
            "trigger_delay_s",
            "clear_delay_s",
            "latching",
            "action",
        },
        optional={"trip_threshold", "clear_threshold"},
        context="protection rule",
    )
    priority = _nonnegative_integer(raw["priority"], context="protection priority")
    try:
        return ProtectionRule(
            rule_id=_identifier(raw["rule_id"], context="protection rule_id"),
            priority=priority,
            condition=cast(Literal["high", "low", "invalid"], raw["condition"]),
            signal_name=_identifier(
                raw["signal_name"], context="protection signal_name"
            ),
            trigger_delay_s=_nonnegative_number(
                raw["trigger_delay_s"], context="trigger_delay_s"
            ),
            clear_delay_s=_nonnegative_number(
                raw["clear_delay_s"], context="clear_delay_s"
            ),
            latching=_boolean(raw["latching"], context="latching"),
            action=_protection_action_from_mapping(raw["action"]),
            trip_threshold=(
                None
                if "trip_threshold" not in raw
                else _finite_number(raw["trip_threshold"], context="trip_threshold")
            ),
            clear_threshold=(
                None
                if "clear_threshold" not in raw
                else _finite_number(raw["clear_threshold"], context="clear_threshold")
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid protection rule: {exc}") from exc


def _input_spec_from_mapping(value: object) -> InputSensitivitySpec:
    raw = _mapping(value, context="uncertainty input")
    strict_keys(
        raw,
        required={
            "input_id",
            "reference_value",
            "central_step",
            "normalization_scale",
            "unit",
        },
        context="uncertainty input",
    )
    try:
        return InputSensitivitySpec(
            input_id=_identifier(raw["input_id"], context="input_id"),
            reference_value=_finite_number(
                raw["reference_value"], context="reference_value"
            ),
            central_step=_positive_number(raw["central_step"], context="central_step"),
            normalization_scale=_positive_number(
                raw["normalization_scale"], context="normalization_scale"
            ),
            unit=_text(raw["unit"], context="input unit"),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid uncertainty input: {exc}") from exc


def _output_spec_from_mapping(value: object) -> OutputSensitivitySpec:
    raw = _mapping(value, context="uncertainty output")
    strict_keys(
        raw,
        required={"output_id", "normalization_scale", "unit", "numerical_margin"},
        context="uncertainty output",
    )
    try:
        return OutputSensitivitySpec(
            output_id=_identifier(raw["output_id"], context="output_id"),
            normalization_scale=_positive_number(
                raw["normalization_scale"], context="normalization_scale"
            ),
            unit=_text(raw["unit"], context="output unit"),
            numerical_margin=_nonnegative_number(
                raw["numerical_margin"], context="numerical_margin"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid uncertainty output: {exc}") from exc


def _input_interval_from_mapping(value: object) -> EngineeringInputInterval:
    raw = _mapping(value, context="uncertainty interval")
    strict_keys(
        raw,
        required={
            "input_id",
            "lower",
            "upper",
            "confidence_multiplier",
            "confidence_label",
        },
        context="uncertainty interval",
    )
    try:
        return EngineeringInputInterval(
            input_id=_identifier(raw["input_id"], context="interval input_id"),
            lower=_finite_number(raw["lower"], context="interval lower"),
            upper=_finite_number(raw["upper"], context="interval upper"),
            confidence_multiplier=_finite_number(
                raw["confidence_multiplier"], context="confidence_multiplier"
            ),
            confidence_label=_identifier(
                raw["confidence_label"], context="confidence_label"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid uncertainty interval: {exc}") from exc


def load_m6_validation_config(path: Path) -> M6ValidationConfig:
    """Load one UTF-8 M6 validation configuration with strict nested fields."""

    try:
        return M6ValidationConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid M6 validation configuration {path}: {exc}") from exc


__all__ = [
    "M6_ANALYSIS_BASIS_VERSION",
    "M6_CLAIM_SCOPE",
    "M6_DATA_ORIGIN",
    "M6_SCHEMA_VERSION",
    "M6_VALIDATION_VERSION",
    "ExpectedScenarioStatus",
    "M6ValidationConfig",
    "ReportAcceptanceThresholds",
    "ScenarioClass",
    "ScenarioExecutionLayer",
    "UncertaintyExecutionLayer",
    "UncertaintyPlan",
    "ValidationScenarioSpec",
    "load_m6_validation_config",
]
