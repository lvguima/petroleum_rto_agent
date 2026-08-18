"""Strict M5 calibration configuration for the identifiable two-parameter subset."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from ..core.config import (
    CaseConfig,
    ConfigurationError,
    ModelConfig,
    canonical_fingerprint,
    load_json,
    strict_keys,
)
from ..properties.components import ComponentCatalog

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CALIBRATION_PARAMETER_PATHS: Final[tuple[str, str]] = (
    "column.cut_points_k[2]",
    "column.cut_points_k[3]",
)
CALIBRATION_TARGETS: Final[tuple[str, str, str]] = (
    "light_diesel",
    "heavy_diesel",
    "residue",
)

# path -> (column index, lower bound K, upper bound K, prior K, prior scale K)
CALIBRATION_PARAMETER_DEFINITIONS: Final[
    Mapping[str, tuple[int, float, float, float, float]]
] = MappingProxyType(
    {
        "column.cut_points_k[2]": (2, 568.15, 598.15, 583.15, 10.0),
        "column.cut_points_k[3]": (3, 623.15, 653.15, 638.15, 10.0),
    }
)


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ConfigurationError(f"{context} must be a non-empty identifier")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be a non-empty string")
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


def _nonnegative_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number < 0.0:
        raise ConfigurationError(f"{context} must be non-negative")
    return number


def _integer(value: object, *, context: str, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"{context} must be an integer")
    if value < minimum:
        raise ConfigurationError(f"{context} must be at least {minimum}")
    return value


def _relative_repo_path(value: object, *, context: str) -> str:
    text = _text(value, context=context)
    if "\\" in text:
        raise ConfigurationError(f"{context} must use forward slashes")
    parsed = PurePosixPath(text)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ConfigurationError(f"{context} must be a repository-relative path")
    return text


@dataclass(frozen=True)
class CalibrationParameterSpec:
    """One frozen adjustable parameter and its physical/regularization envelope."""

    path: str
    lower_bound_k: float
    upper_bound_k: float
    prior_k: float
    prior_scale_k: float

    def __post_init__(self) -> None:
        values = (
            self.lower_bound_k,
            self.upper_bound_k,
            self.prior_k,
            self.prior_scale_k,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise ConfigurationError("calibration parameter values must be finite numbers")
        if self.path not in CALIBRATION_PARAMETER_DEFINITIONS:
            raise ConfigurationError(f"parameter path is not calibratable: {self.path!r}")
        if self.lower_bound_k >= self.upper_bound_k:
            raise ConfigurationError("calibration parameter bounds must be increasing")
        if not self.lower_bound_k <= self.prior_k <= self.upper_bound_k:
            raise ConfigurationError("calibration parameter prior must be within its bounds")
        if self.prior_scale_k <= 0.0:
            raise ConfigurationError("calibration parameter prior scale must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CalibrationParameterSpec:
        strict_keys(
            value,
            required={
                "path",
                "lower_bound_k",
                "upper_bound_k",
                "prior_k",
                "prior_scale_k",
            },
            context="calibration parameter",
        )
        return cls(
            path=_text(value["path"], context="calibration parameter path"),
            lower_bound_k=_finite_number(
                value["lower_bound_k"], context="calibration parameter lower bound"
            ),
            upper_bound_k=_finite_number(
                value["upper_bound_k"], context="calibration parameter upper bound"
            ),
            prior_k=_finite_number(
                value["prior_k"], context="calibration parameter prior"
            ),
            prior_scale_k=_positive_number(
                value["prior_scale_k"], context="calibration parameter prior scale"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "lower_bound_k": self.lower_bound_k,
            "upper_bound_k": self.upper_bound_k,
            "prior_k": self.prior_k,
            "prior_scale_k": self.prior_scale_k,
        }


@dataclass(frozen=True)
class CalibrationTargetSpec:
    """One reconciled product-flow target and its residual scale."""

    product: str
    scale_kg_s: float

    def __post_init__(self) -> None:
        if self.product not in CALIBRATION_TARGETS:
            raise ConfigurationError(f"unsupported calibration target: {self.product!r}")
        if (
            isinstance(self.scale_kg_s, bool)
            or not isinstance(self.scale_kg_s, (int, float))
            or not math.isfinite(self.scale_kg_s)
            or self.scale_kg_s <= 0.0
        ):
            raise ConfigurationError("calibration target scale must be finite and positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CalibrationTargetSpec:
        strict_keys(
            value,
            required={"product", "scale_kg_s"},
            context="calibration target",
        )
        return cls(
            product=_text(value["product"], context="calibration target product"),
            scale_kg_s=_positive_number(
                value["scale_kg_s"], context="calibration target scale"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {"product": self.product, "scale_kg_s": self.scale_kg_s}


@dataclass(frozen=True)
class CalibrationDataReference:
    """Versioned reference to the upstream reconciled flow artifact."""

    reconciliation_version: str
    path: str
    target_basis: str
    target_unit: str

    def __post_init__(self) -> None:
        _identifier(self.reconciliation_version, context="reconciliation version")
        _relative_repo_path(self.path, context="reconciliation path")
        if self.target_basis != "reconciled_net_boundary_flows":
            raise ConfigurationError(
                "calibration target basis must be reconciled_net_boundary_flows"
            )
        if self.target_unit != "kg/s":
            raise ConfigurationError("calibration target unit must be kg/s")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CalibrationDataReference:
        strict_keys(
            value,
            required={"reconciliation_version", "path", "target_basis", "target_unit"},
            context="calibration data reference",
        )
        return cls(
            reconciliation_version=_identifier(
                value["reconciliation_version"], context="reconciliation version"
            ),
            path=_relative_repo_path(value["path"], context="reconciliation path"),
            target_basis=_text(value["target_basis"], context="calibration target basis"),
            target_unit=_text(value["target_unit"], context="calibration target unit"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "reconciliation_version": self.reconciliation_version,
            "path": self.path,
            "target_basis": self.target_basis,
            "target_unit": self.target_unit,
        }


@dataclass(frozen=True)
class PatternSearchSpec:
    """Deterministic bounded coordinate/pattern-search settings."""

    initial_step_k: float
    minimum_step_k: float
    step_reduction: float
    maximum_iterations: int
    objective_improvement_tolerance: float

    def __post_init__(self) -> None:
        if self.initial_step_k <= 0.0 or not math.isfinite(self.initial_step_k):
            raise ConfigurationError("optimizer initial step must be finite and positive")
        if self.minimum_step_k <= 0.0 or not math.isfinite(self.minimum_step_k):
            raise ConfigurationError("optimizer minimum step must be finite and positive")
        if self.minimum_step_k >= self.initial_step_k:
            raise ConfigurationError("optimizer minimum step must be below its initial step")
        if not 0.0 < self.step_reduction < 1.0 or not math.isfinite(
            self.step_reduction
        ):
            raise ConfigurationError("optimizer step reduction must be in (0, 1)")
        _integer(self.maximum_iterations, context="optimizer maximum iterations")
        if (
            not math.isfinite(self.objective_improvement_tolerance)
            or self.objective_improvement_tolerance < 0.0
        ):
            raise ConfigurationError(
                "optimizer objective improvement tolerance must be finite and non-negative"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PatternSearchSpec:
        strict_keys(
            value,
            required={
                "initial_step_k",
                "minimum_step_k",
                "step_reduction",
                "maximum_iterations",
                "objective_improvement_tolerance",
            },
            context="calibration optimizer",
        )
        return cls(
            initial_step_k=_positive_number(
                value["initial_step_k"], context="optimizer initial step"
            ),
            minimum_step_k=_positive_number(
                value["minimum_step_k"], context="optimizer minimum step"
            ),
            step_reduction=_positive_number(
                value["step_reduction"], context="optimizer step reduction"
            ),
            maximum_iterations=_integer(
                value["maximum_iterations"], context="optimizer maximum iterations"
            ),
            objective_improvement_tolerance=_nonnegative_number(
                value["objective_improvement_tolerance"],
                context="optimizer objective improvement tolerance",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "initial_step_k": self.initial_step_k,
            "minimum_step_k": self.minimum_step_k,
            "step_reduction": self.step_reduction,
            "maximum_iterations": self.maximum_iterations,
            "objective_improvement_tolerance": self.objective_improvement_tolerance,
        }


@dataclass(frozen=True)
class SensitivitySpec:
    """Central-difference and numerical-rank settings."""

    central_step_k: float
    absolute_rank_tolerance: float
    relative_rank_tolerance: float

    def __post_init__(self) -> None:
        if self.central_step_k <= 0.0 or not math.isfinite(self.central_step_k):
            raise ConfigurationError("sensitivity central step must be finite and positive")
        if self.absolute_rank_tolerance < 0.0 or not math.isfinite(
            self.absolute_rank_tolerance
        ):
            raise ConfigurationError(
                "sensitivity absolute rank tolerance must be finite and non-negative"
            )
        if not 0.0 < self.relative_rank_tolerance <= 1.0 or not math.isfinite(
            self.relative_rank_tolerance
        ):
            raise ConfigurationError("sensitivity relative rank tolerance must be in (0, 1]")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SensitivitySpec:
        strict_keys(
            value,
            required={
                "central_step_k",
                "absolute_rank_tolerance",
                "relative_rank_tolerance",
            },
            context="calibration sensitivity",
        )
        return cls(
            central_step_k=_positive_number(
                value["central_step_k"], context="sensitivity central step"
            ),
            absolute_rank_tolerance=_nonnegative_number(
                value["absolute_rank_tolerance"],
                context="sensitivity absolute rank tolerance",
            ),
            relative_rank_tolerance=_positive_number(
                value["relative_rank_tolerance"],
                context="sensitivity relative rank tolerance",
            ),
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "central_step_k": self.central_step_k,
            "absolute_rank_tolerance": self.absolute_rank_tolerance,
            "relative_rank_tolerance": self.relative_rank_tolerance,
        }


@dataclass(frozen=True)
class CalibrationConfig:
    """Complete reproducible M5 calibration contract."""

    schema_version: str
    calibration_version: str
    model_version: str
    base_parameter_set_version: str
    calibrated_parameter_set_version: str
    case_version: str
    name: str
    data_reference: CalibrationDataReference
    parameters: tuple[CalibrationParameterSpec, ...]
    targets: tuple[CalibrationTargetSpec, ...]
    regularization_weight: float
    optimizer: PatternSearchSpec
    sensitivity: SensitivitySpec
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        for context, value in (
            ("schema version", self.schema_version),
            ("calibration version", self.calibration_version),
            ("model version", self.model_version),
            ("base parameter-set version", self.base_parameter_set_version),
            ("calibrated parameter-set version", self.calibrated_parameter_set_version),
            ("case version", self.case_version),
        ):
            _identifier(value, context=context)
        _text(self.name, context="calibration name")
        if not isinstance(self.data_reference, CalibrationDataReference):
            raise TypeError("data_reference must be CalibrationDataReference")
        if tuple(item.path for item in self.parameters) != CALIBRATION_PARAMETER_PATHS:
            raise ConfigurationError(
                "calibration must contain exactly the two frozen column cut-point paths"
            )
        for item in self.parameters:
            expected = CALIBRATION_PARAMETER_DEFINITIONS[item.path][1:]
            actual = (
                item.lower_bound_k,
                item.upper_bound_k,
                item.prior_k,
                item.prior_scale_k,
            )
            if actual != expected:
                raise ConfigurationError(
                    f"calibration envelope differs from the frozen definition for {item.path}"
                )
        if tuple(item.product for item in self.targets) != CALIBRATION_TARGETS:
            raise ConfigurationError(
                "calibration targets must be exactly light_diesel, heavy_diesel and residue"
            )
        if (
            isinstance(self.regularization_weight, bool)
            or not isinstance(self.regularization_weight, (int, float))
            or not math.isfinite(self.regularization_weight)
            or self.regularization_weight < 0.0
        ):
            raise ConfigurationError("regularization weight must be finite and non-negative")
        if not isinstance(self.optimizer, PatternSearchSpec):
            raise TypeError("optimizer must be PatternSearchSpec")
        if not isinstance(self.sensitivity, SensitivitySpec):
            raise TypeError("sensitivity must be SensitivitySpec")
        for item in self.parameters:
            step = self.sensitivity.central_step_k
            if item.prior_k - step < item.lower_bound_k or item.prior_k + step > item.upper_bound_k:
                raise ConfigurationError(
                    "sensitivity central step must fit symmetrically around every prior"
                )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.metadata.items()
        ):
            raise ConfigurationError("calibration metadata must map strings to strings")
        for required in ("purpose", "confidence", "claim_scope"):
            if not self.metadata.get(required, "").strip():
                raise ConfigurationError(f"calibration metadata.{required} is required")
        object.__setattr__(self, "parameters", tuple(self.parameters))
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CalibrationConfig:
        strict_keys(
            value,
            required={
                "schema_version",
                "calibration_version",
                "model_version",
                "base_parameter_set_version",
                "calibrated_parameter_set_version",
                "case_version",
                "name",
                "data_reference",
                "parameters",
                "targets",
                "regularization_weight",
                "optimizer",
                "sensitivity",
                "metadata",
            },
            context="calibration configuration",
        )
        raw_parameters = value["parameters"]
        raw_targets = value["targets"]
        if not isinstance(raw_parameters, Sequence) or isinstance(
            raw_parameters, (str, bytes, bytearray)
        ):
            raise ConfigurationError("calibration parameters must be a sequence")
        if not isinstance(raw_targets, Sequence) or isinstance(
            raw_targets, (str, bytes, bytearray)
        ):
            raise ConfigurationError("calibration targets must be a sequence")
        parameters = tuple(
            CalibrationParameterSpec.from_mapping(
                _mapping(item, context=f"calibration parameter {index}")
            )
            for index, item in enumerate(raw_parameters)
        )
        targets = tuple(
            CalibrationTargetSpec.from_mapping(
                _mapping(item, context=f"calibration target {index}")
            )
            for index, item in enumerate(raw_targets)
        )
        raw_metadata = _mapping(value["metadata"], context="calibration metadata")
        if any(not isinstance(item, str) for item in raw_metadata.values()):
            raise ConfigurationError("calibration metadata values must be strings")
        return cls(
            schema_version=_identifier(value["schema_version"], context="schema version"),
            calibration_version=_identifier(
                value["calibration_version"], context="calibration version"
            ),
            model_version=_identifier(value["model_version"], context="model version"),
            base_parameter_set_version=_identifier(
                value["base_parameter_set_version"],
                context="base parameter-set version",
            ),
            calibrated_parameter_set_version=_identifier(
                value["calibrated_parameter_set_version"],
                context="calibrated parameter-set version",
            ),
            case_version=_identifier(value["case_version"], context="case version"),
            name=_text(value["name"], context="calibration name"),
            data_reference=CalibrationDataReference.from_mapping(
                _mapping(value["data_reference"], context="calibration data reference")
            ),
            parameters=parameters,
            targets=targets,
            regularization_weight=_nonnegative_number(
                value["regularization_weight"], context="regularization weight"
            ),
            optimizer=PatternSearchSpec.from_mapping(
                _mapping(value["optimizer"], context="calibration optimizer")
            ),
            sensitivity=SensitivitySpec.from_mapping(
                _mapping(value["sensitivity"], context="calibration sensitivity")
            ),
            metadata=MappingProxyType(
                {key: cast(str, item) for key, item in raw_metadata.items()}
            ),
        )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "calibration_version": self.calibration_version,
            "model_version": self.model_version,
            "base_parameter_set_version": self.base_parameter_set_version,
            "calibrated_parameter_set_version": self.calibrated_parameter_set_version,
            "case_version": self.case_version,
            "name": self.name,
            "data_reference": self.data_reference.as_dict(),
            "parameters": [item.as_dict() for item in self.parameters],
            "targets": [item.as_dict() for item in self.targets],
            "regularization_weight": self.regularization_weight,
            "optimizer": self.optimizer.as_dict(),
            "sensitivity": self.sensitivity.as_dict(),
            "metadata": dict(self.metadata),
        }


def load_calibration_config(path: Path) -> CalibrationConfig:
    """Load and strictly validate one M5 calibration configuration."""

    try:
        return CalibrationConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid calibration configuration {path}: {exc}") from exc


def validate_calibration_compatibility(
    config: CalibrationConfig,
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
) -> None:
    """Reject version drift and a base model that no longer matches the priors."""

    checks = {
        "schema_version": (config.schema_version, model.schema_version),
        "case_schema_version": (config.schema_version, case.schema_version),
        "catalog_schema_version": (config.schema_version, catalog.schema_version),
        "model_version": (config.model_version, model.model_version),
        "case_model_version": (config.model_version, case.model_version),
        "base_parameter_set_version": (
            config.base_parameter_set_version,
            model.parameter_set_version,
        ),
        "case_parameter_set_version": (
            config.base_parameter_set_version,
            case.parameter_set_version,
        ),
        "catalog_parameter_set_version": (
            config.base_parameter_set_version,
            catalog.parameter_set_version,
        ),
        "case_version": (config.case_version, case.case_version),
    }
    mismatches = sorted(name for name, pair in checks.items() if pair[0] != pair[1])
    if mismatches:
        raise ConfigurationError(
            f"calibration input version mismatch: {', '.join(mismatches)}"
        )
    column = model.equipment.get("column")
    if column is None:
        raise ConfigurationError("base model is missing the column section")
    raw_cut_points = column.get("cut_points_k")
    if not isinstance(raw_cut_points, Sequence) or isinstance(
        raw_cut_points, (str, bytes, bytearray)
    ) or len(raw_cut_points) != 4:
        raise ConfigurationError("base model column cut points are invalid")
    for item in config.parameters:
        index = CALIBRATION_PARAMETER_DEFINITIONS[item.path][0]
        raw_value = raw_cut_points[index]
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ConfigurationError("base model column cut points must be numeric")
        if float(raw_value) != item.prior_k:
            raise ConfigurationError(
                f"base model value no longer matches the calibration prior for {item.path}"
            )
