"""Deterministic, conservation-gated M5 two-cut-point calibration."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import CaseConfig, ModelConfig, canonical_fingerprint
from ..flowsheet.recycle import RecycleSolveResult, solve_recycle
from ..properties.components import ComponentCatalog
from .config import (
    CALIBRATION_PARAMETER_DEFINITIONS,
    CALIBRATION_PARAMETER_PATHS,
    CALIBRATION_TARGETS,
    CalibrationConfig,
    validate_calibration_compatibility,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENERGY_TOLERANCE_W = 1e-5

CalibrationSolver = Callable[[ModelConfig, CaseConfig, ComponentCatalog], RecycleSolveResult]


class CalibrationError(RuntimeError):
    """Raised when calibration cannot retain a converged, conserving M2 evaluation."""


def _finite_mapping(
    value: Mapping[str, float],
    *,
    expected_keys: Sequence[str],
    context: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> Mapping[str, float]:
    if tuple(value) != tuple(expected_keys):
        raise ValueError(f"{context} keys or order differ from the frozen contract")
    copied: dict[str, float] = {}
    for key, item in value.items():
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or (positive and item <= 0.0)
            or (nonnegative and item < 0.0)
        ):
            qualification = (
                "finite and positive"
                if positive
                else "finite and non-negative" if nonnegative else "finite"
            )
            raise ValueError(f"{context} values must be {qualification}")
        copied[key] = float(item)
    return MappingProxyType(copied)


@dataclass(frozen=True)
class ObjectiveEvaluation:
    """Complete prediction, residual and regularization evidence for one point."""

    parameters_k: Mapping[str, float]
    predictions_kg_s: Mapping[str, float]
    targets_kg_s: Mapping[str, float]
    target_scales_kg_s: Mapping[str, float]
    errors_kg_s: Mapping[str, float]
    normalized_residuals: Mapping[str, float]
    squared_regularization_terms: Mapping[str, float]
    regularization_weight: float
    data_misfit: float
    regularization_penalty: float
    total_objective: float
    m2_input_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters_k",
            _finite_mapping(
                self.parameters_k,
                expected_keys=CALIBRATION_PARAMETER_PATHS,
                context="evaluation parameters",
            ),
        )
        for name in (
            "predictions_kg_s",
            "targets_kg_s",
            "target_scales_kg_s",
            "errors_kg_s",
            "normalized_residuals",
        ):
            object.__setattr__(
                self,
                name,
                _finite_mapping(
                    cast(Mapping[str, float], getattr(self, name)),
                    expected_keys=CALIBRATION_TARGETS,
                    context=name,
                    positive=name in ("targets_kg_s", "target_scales_kg_s"),
                    nonnegative=name == "predictions_kg_s",
                ),
            )
        object.__setattr__(
            self,
            "squared_regularization_terms",
            _finite_mapping(
                self.squared_regularization_terms,
                expected_keys=CALIBRATION_PARAMETER_PATHS,
                context="squared regularization terms",
                nonnegative=True,
            ),
        )
        scalars = (
            self.regularization_weight,
            self.data_misfit,
            self.regularization_penalty,
            self.total_objective,
        )
        if any(not math.isfinite(value) or value < 0.0 for value in scalars):
            raise ValueError("objective terms must be finite and non-negative")
        expected_data_misfit = sum(value * value for value in self.normalized_residuals.values())
        if not math.isclose(self.data_misfit, expected_data_misfit, abs_tol=1e-12):
            raise ValueError("data misfit does not equal the squared normalized residuals")
        for name in CALIBRATION_TARGETS:
            if not math.isclose(
                self.errors_kg_s[name],
                self.predictions_kg_s[name] - self.targets_kg_s[name],
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("objective error does not match prediction minus target")
            if not math.isclose(
                self.normalized_residuals[name],
                self.errors_kg_s[name] / self.target_scales_kg_s[name],
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("normalized residual does not match error divided by scale")
        expected_regularization = self.regularization_weight * sum(
            self.squared_regularization_terms.values()
        )
        if not math.isclose(
            self.regularization_penalty,
            expected_regularization,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("regularization penalty does not match its weighted terms")
        if not math.isclose(
            self.total_objective,
            self.data_misfit + self.regularization_penalty,
            abs_tol=1e-12,
        ):
            raise ValueError("total objective does not equal its two components")
        if not _SHA256_PATTERN.fullmatch(self.m2_input_fingerprint):
            raise ValueError("M2 input fingerprint must be a lowercase SHA-256 digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "parameters_k": dict(self.parameters_k),
            "predictions_kg_s": dict(self.predictions_kg_s),
            "targets_kg_s": dict(self.targets_kg_s),
            "target_scales_kg_s": dict(self.target_scales_kg_s),
            "errors_kg_s": dict(self.errors_kg_s),
            "normalized_residuals": dict(self.normalized_residuals),
            "squared_regularization_terms": dict(self.squared_regularization_terms),
            "regularization_weight": self.regularization_weight,
            "data_misfit": self.data_misfit,
            "regularization_penalty": self.regularization_penalty,
            "total_objective": self.total_objective,
            "m2_input_fingerprint": self.m2_input_fingerprint,
        }


@dataclass(frozen=True)
class CalibratedParameter:
    """Final parameter value with an explicit bound-hit audit flag."""

    path: str
    initial_k: float
    calibrated_k: float
    lower_bound_k: float
    upper_bound_k: float
    prior_scale_k: float
    at_lower_bound: bool
    at_upper_bound: bool

    def __post_init__(self) -> None:
        if self.path not in CALIBRATION_PARAMETER_PATHS:
            raise ValueError("calibrated parameter path is outside the frozen whitelist")
        values = (
            self.initial_k,
            self.calibrated_k,
            self.lower_bound_k,
            self.upper_bound_k,
            self.prior_scale_k,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("calibrated parameter values must be finite")
        if not self.lower_bound_k <= self.calibrated_k <= self.upper_bound_k:
            raise ValueError("calibrated parameter is outside its bounds")
        if self.prior_scale_k <= 0.0:
            raise ValueError("calibrated parameter prior scale must be positive")
        if self.at_lower_bound != (self.calibrated_k == self.lower_bound_k):
            raise ValueError("lower-bound hit flag does not match the calibrated value")
        if self.at_upper_bound != (self.calibrated_k == self.upper_bound_k):
            raise ValueError("upper-bound hit flag does not match the calibrated value")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "initial_k": self.initial_k,
            "calibrated_k": self.calibrated_k,
            "lower_bound_k": self.lower_bound_k,
            "upper_bound_k": self.upper_bound_k,
            "prior_scale_k": self.prior_scale_k,
            "at_lower_bound": self.at_lower_bound,
            "at_upper_bound": self.at_upper_bound,
        }


def _matrix_singular_values_and_cosine(
    matrix: Sequence[Sequence[float]],
) -> tuple[tuple[float, float], float | None]:
    gram_00 = sum(row[0] * row[0] for row in matrix)
    gram_01 = sum(row[0] * row[1] for row in matrix)
    gram_11 = sum(row[1] * row[1] for row in matrix)
    discriminant = math.sqrt(
        max(0.0, (gram_00 - gram_11) ** 2 + 4.0 * gram_01 * gram_01)
    )
    eigenvalue_high = max(0.0, 0.5 * (gram_00 + gram_11 + discriminant))
    eigenvalue_low = max(0.0, 0.5 * (gram_00 + gram_11 - discriminant))
    singular_values = (math.sqrt(eigenvalue_high), math.sqrt(eigenvalue_low))
    column_norm_product = math.sqrt(gram_00 * gram_11)
    column_cosine = (
        max(-1.0, min(1.0, gram_01 / column_norm_product))
        if column_norm_product > 0.0
        else None
    )
    return singular_values, column_cosine


@dataclass(frozen=True)
class SensitivityAnalysis:
    """Three-output/two-parameter central-difference identifiability evidence."""

    requested_parameters_k: Mapping[str, float]
    reference_parameters_k: Mapping[str, float]
    reference_adjusted_for_bounds: bool
    central_step_k: float
    matrix_kg_s_per_k: tuple[tuple[float, float], ...]
    target_scales_kg_s: Mapping[str, float]
    parameter_scales_k: Mapping[str, float]
    normalized_matrix: tuple[tuple[float, float], ...]
    singular_values: tuple[float, float]
    numerical_rank: int
    rank_threshold: float
    condition_number: float | None
    column_cosine: float | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requested_parameters_k",
            _finite_mapping(
                self.requested_parameters_k,
                expected_keys=CALIBRATION_PARAMETER_PATHS,
                context="sensitivity requested parameters",
            ),
        )
        object.__setattr__(
            self,
            "reference_parameters_k",
            _finite_mapping(
                self.reference_parameters_k,
                expected_keys=CALIBRATION_PARAMETER_PATHS,
                context="sensitivity reference parameters",
            ),
        )
        if not isinstance(self.reference_adjusted_for_bounds, bool):
            raise TypeError("sensitivity bound-adjustment flag must be boolean")
        if self.reference_adjusted_for_bounds != (
            self.requested_parameters_k != self.reference_parameters_k
        ):
            raise ValueError(
                "sensitivity bound-adjustment flag does not match its reference point"
            )
        if not math.isfinite(self.central_step_k) or self.central_step_k <= 0.0:
            raise ValueError("sensitivity central step must be finite and positive")
        if len(self.matrix_kg_s_per_k) != 3 or any(
            len(row) != 2 or any(not math.isfinite(value) for value in row)
            for row in self.matrix_kg_s_per_k
        ):
            raise ValueError("sensitivity matrix must be a finite 3x2 matrix")
        object.__setattr__(
            self,
            "target_scales_kg_s",
            _finite_mapping(
                self.target_scales_kg_s,
                expected_keys=CALIBRATION_TARGETS,
                context="sensitivity target scales",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "parameter_scales_k",
            _finite_mapping(
                self.parameter_scales_k,
                expected_keys=CALIBRATION_PARAMETER_PATHS,
                context="sensitivity parameter scales",
                positive=True,
            ),
        )
        if len(self.normalized_matrix) != 3 or any(
            len(row) != 2 or any(not math.isfinite(value) for value in row)
            for row in self.normalized_matrix
        ):
            raise ValueError("normalized sensitivity matrix must be a finite 3x2 matrix")
        for row_index, target in enumerate(CALIBRATION_TARGETS):
            for column_index, path in enumerate(CALIBRATION_PARAMETER_PATHS):
                expected_normalized = (
                    self.matrix_kg_s_per_k[row_index][column_index]
                    * self.parameter_scales_k[path]
                    / self.target_scales_kg_s[target]
                )
                if not math.isclose(
                    self.normalized_matrix[row_index][column_index],
                    expected_normalized,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ):
                    raise ValueError(
                        "normalized sensitivity does not match its target/parameter scales"
                    )
        if (
            len(self.singular_values) != 2
            or self.singular_values[0] < self.singular_values[1]
            or any(not math.isfinite(value) or value < 0.0 for value in self.singular_values)
        ):
            raise ValueError("singular values must be finite, non-negative and descending")
        if self.numerical_rank not in (0, 1, 2):
            raise ValueError("sensitivity numerical rank must be zero, one or two")
        if not math.isfinite(self.rank_threshold) or self.rank_threshold < 0.0:
            raise ValueError("sensitivity rank threshold must be finite and non-negative")
        if self.condition_number is not None and (
            not math.isfinite(self.condition_number) or self.condition_number < 1.0
        ):
            raise ValueError("finite condition number must be at least one")
        if self.column_cosine is not None and (
            not math.isfinite(self.column_cosine) or abs(self.column_cosine) > 1.0
        ):
            raise ValueError("sensitivity column cosine must be within [-1, 1]")
        expected_singular_values, expected_cosine = _matrix_singular_values_and_cosine(
            self.matrix_kg_s_per_k
        )
        for actual, expected in zip(self.singular_values, expected_singular_values, strict=True):
            if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("reported singular values do not match the sensitivity matrix")
        expected_rank = sum(value > self.rank_threshold for value in self.singular_values)
        if self.numerical_rank != expected_rank:
            raise ValueError("reported numerical rank does not match its threshold")
        expected_condition = (
            self.singular_values[0] / self.singular_values[1]
            if expected_rank == 2 and self.singular_values[1] > 0.0
            else None
        )
        if (
            (self.condition_number is None) != (expected_condition is None)
            or self.condition_number is not None
            and expected_condition is not None
            and not math.isclose(
                self.condition_number,
                expected_condition,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("reported condition number does not match the singular values")
        if (
            (self.column_cosine is None) != (expected_cosine is None)
            or self.column_cosine is not None
            and expected_cosine is not None
            and not math.isclose(
                self.column_cosine,
                expected_cosine,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("reported column cosine does not match the sensitivity matrix")
        object.__setattr__(
            self,
            "matrix_kg_s_per_k",
            tuple(tuple(row) for row in self.matrix_kg_s_per_k),
        )
        object.__setattr__(
            self,
            "normalized_matrix",
            tuple(tuple(row) for row in self.normalized_matrix),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "output_names": list(CALIBRATION_TARGETS),
            "parameter_paths": list(CALIBRATION_PARAMETER_PATHS),
            "requested_parameters_k": dict(self.requested_parameters_k),
            "reference_parameters_k": dict(self.reference_parameters_k),
            "reference_adjusted_for_bounds": self.reference_adjusted_for_bounds,
            "central_step_k": self.central_step_k,
            "matrix_kg_s_per_k": [list(row) for row in self.matrix_kg_s_per_k],
            "target_scales_kg_s": dict(self.target_scales_kg_s),
            "parameter_scales_k": dict(self.parameter_scales_k),
            "normalized_matrix": [list(row) for row in self.normalized_matrix],
            "singular_values": list(self.singular_values),
            "numerical_rank": self.numerical_rank,
            "rank_threshold": self.rank_threshold,
            "condition_number": self.condition_number,
            "column_cosine": self.column_cosine,
        }


@dataclass(frozen=True)
class CalibrationResult:
    """Successful, reproducible M5 calibration result."""

    status: str
    initial: ObjectiveEvaluation
    calibrated: ObjectiveEvaluation
    parameters: tuple[CalibratedParameter, ...]
    initial_sensitivity: SensitivityAnalysis
    calibrated_sensitivity: SensitivityAnalysis
    optimizer_iterations: int
    model_evaluations: int
    final_step_k: float
    versions: Mapping[str, str]
    fingerprints: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.status != "success":
            raise ValueError("a returned calibration result must have success status")
        if tuple(item.path for item in self.parameters) != CALIBRATION_PARAMETER_PATHS:
            raise ValueError("calibrated parameters differ from the frozen whitelist")
        if self.initial.targets_kg_s != self.calibrated.targets_kg_s:
            raise ValueError("initial and calibrated evaluations must use identical targets")
        if self.initial.target_scales_kg_s != self.calibrated.target_scales_kg_s:
            raise ValueError("initial and calibrated evaluations must use identical scales")
        if self.initial.regularization_weight != self.calibrated.regularization_weight:
            raise ValueError(
                "initial and calibrated evaluations must use identical regularization weight"
            )
        if not self.calibrated.total_objective < self.initial.total_objective:
            raise ValueError("a successful calibration must strictly improve total objective")
        for item in self.parameters:
            if item.initial_k != self.initial.parameters_k[item.path]:
                raise ValueError("parameter initial value differs from the initial evaluation")
            if item.calibrated_k != self.calibrated.parameters_k[item.path]:
                raise ValueError(
                    "parameter calibrated value differs from the calibrated evaluation"
                )
            if self.initial.squared_regularization_terms[item.path] != 0.0:
                raise ValueError("initial evaluation must have zero regularization terms")
            expected_term = ((item.calibrated_k - item.initial_k) / item.prior_scale_k) ** 2
            if not math.isclose(
                self.calibrated.squared_regularization_terms[item.path],
                expected_term,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    "calibrated regularization term differs from the parameter displacement"
                )
        if self.initial_sensitivity.requested_parameters_k != self.initial.parameters_k:
            raise ValueError("initial sensitivity does not belong to the initial evaluation")
        if (
            self.calibrated_sensitivity.requested_parameters_k
            != self.calibrated.parameters_k
        ):
            raise ValueError(
                "calibrated sensitivity does not belong to the calibrated evaluation"
            )
        if (
            self.initial_sensitivity.numerical_rank != 2
            or self.calibrated_sensitivity.numerical_rank != 2
        ):
            raise ValueError("a successful calibration requires full-rank sensitivity")
        expected_parameter_scales = {
            item.path: item.prior_scale_k for item in self.parameters
        }
        for sensitivity in (self.initial_sensitivity, self.calibrated_sensitivity):
            if sensitivity.target_scales_kg_s != self.initial.target_scales_kg_s:
                raise ValueError("sensitivity target scales differ from the objective")
            if sensitivity.parameter_scales_k != expected_parameter_scales:
                raise ValueError("sensitivity parameter scales differ from the result")
        if self.optimizer_iterations < 1 or self.model_evaluations < 1:
            raise ValueError("calibration iteration and evaluation counts must be positive")
        if not math.isfinite(self.final_step_k) or self.final_step_k < 0.0:
            raise ValueError("calibration final step must be finite and non-negative")
        if any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in self.versions.items()
        ):
            raise ValueError("calibration versions must map names to non-empty strings")
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not _SHA256_PATTERN.fullmatch(value)
            for key, value in self.fingerprints.items()
        ):
            raise ValueError("calibration fingerprints must be lowercase SHA-256 digests")
        required_fingerprints = {
            "configuration",
            "base_model",
            "case",
            "component_catalog",
            "targets",
            "input_bundle",
            "calibrated_model",
        }
        if set(self.fingerprints) != required_fingerprints:
            raise ValueError("calibration result fingerprint set is incomplete")
        object.__setattr__(self, "parameters", tuple(self.parameters))
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))
        object.__setattr__(self, "fingerprints", MappingProxyType(dict(self.fingerprints)))

    @property
    def boundary_hits(self) -> tuple[str, ...]:
        return tuple(
            item.path for item in self.parameters if item.at_lower_bound or item.at_upper_bound
        )

    @property
    def sensitivity(self) -> SensitivityAnalysis:
        """Return the post-calibration sensitivity for concise downstream use."""

        return self.calibrated_sensitivity

    @property
    def result_fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "initial": self.initial.as_dict(),
            "calibrated": self.calibrated.as_dict(),
            "parameters": [item.as_dict() for item in self.parameters],
            "boundary_hits": list(self.boundary_hits),
            "initial_sensitivity": self.initial_sensitivity.as_dict(),
            "calibrated_sensitivity": self.calibrated_sensitivity.as_dict(),
            "optimizer_iterations": self.optimizer_iterations,
            "model_evaluations": self.model_evaluations,
            "final_step_k": self.final_step_k,
            "versions": dict(self.versions),
            "fingerprints": dict(self.fingerprints),
        }


def _validated_targets(targets_kg_s: Mapping[str, float]) -> Mapping[str, float]:
    if set(targets_kg_s) != set(CALIBRATION_TARGETS):
        missing = sorted(set(CALIBRATION_TARGETS) - set(targets_kg_s))
        unknown = sorted(set(targets_kg_s) - set(CALIBRATION_TARGETS))
        raise ValueError(
            f"calibration target keys differ; missing={missing}, unknown={unknown}"
        )
    ordered = {name: targets_kg_s[name] for name in CALIBRATION_TARGETS}
    return _finite_mapping(
        ordered,
        expected_keys=CALIBRATION_TARGETS,
        context="calibration targets",
        positive=True,
    )


def apply_calibration_parameters(
    model: ModelConfig,
    config: CalibrationConfig,
    parameter_values_k: Mapping[str, float],
) -> ModelConfig:
    """Clone a model and change only the two explicitly whitelisted cut points."""

    if set(parameter_values_k) != set(CALIBRATION_PARAMETER_PATHS):
        missing = sorted(set(CALIBRATION_PARAMETER_PATHS) - set(parameter_values_k))
        unknown = sorted(set(parameter_values_k) - set(CALIBRATION_PARAMETER_PATHS))
        raise ValueError(
            f"calibration parameter keys differ; missing={missing}, unknown={unknown}"
        )
    specs = {item.path: item for item in config.parameters}
    values: dict[str, float] = {}
    for path in CALIBRATION_PARAMETER_PATHS:
        raw_value = parameter_values_k[path]
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not math.isfinite(raw_value)
        ):
            raise ValueError(f"calibration parameter {path} must be finite")
        value = float(raw_value)
        spec = specs[path]
        if not spec.lower_bound_k <= value <= spec.upper_bound_k:
            raise ValueError(f"calibration parameter {path} is outside its bounds")
        values[path] = value

    model_data = model.as_dict()
    equipment = model_data["equipment"]
    if not isinstance(equipment, dict):
        raise TypeError("model equipment did not serialize as an object")
    column = equipment.get("column")
    if not isinstance(column, dict):
        raise TypeError("model column did not serialize as an object")
    cut_points = column.get("cut_points_k")
    if not isinstance(cut_points, list) or len(cut_points) != 4:
        raise TypeError("model cut points did not serialize as a four-item list")
    original_cut_points = tuple(cut_points)
    for path in CALIBRATION_PARAMETER_PATHS:
        index = CALIBRATION_PARAMETER_DEFINITIONS[path][0]
        cut_points[index] = values[path]

    calibrated_model = ModelConfig.from_mapping(model_data)
    if calibrated_model.dynamic != model.dynamic:
        raise CalibrationError("calibration unexpectedly changed the dynamic configuration")
    calibrated_cut_points = calibrated_model.equipment["column"]["cut_points_k"]
    if not isinstance(calibrated_cut_points, tuple):
        raise CalibrationError("calibrated cut points are not immutable")
    for index in (0, 1):
        if calibrated_cut_points[index] != original_cut_points[index]:
            raise CalibrationError("calibration changed a non-whitelisted cut point")
    return calibrated_model


def _require_conserving_success(
    result: RecycleSolveResult,
    model: ModelConfig,
) -> None:
    if not isinstance(result, RecycleSolveResult):
        raise CalibrationError("calibration solver returned the wrong result type")
    if result.status != "success" or result.flowsheet is None:
        detail = result.failure_reason or result.status
        raise CalibrationError(f"M2 calibration evaluation did not converge: {detail}")
    flowsheet = result.flowsheet
    def tolerance(name: str) -> float:
        raw_value = model.solver[name]
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not math.isfinite(raw_value)
            or raw_value < 0.0
        ):
            raise CalibrationError(f"model solver tolerance {name} is invalid")
        return float(raw_value)

    tolerances = {
        "mass_atol_kg_s": tolerance("mass_tolerance_kg_s"),
        "component_atol_kg_s": tolerance("component_tolerance_kg_s"),
        "salt_atol_kg_s": tolerance("salt_tolerance_kg_s"),
        "energy_atol_w": _ENERGY_TOLERANCE_W,
    }
    if not flowsheet.balance.passed(**tolerances):
        raise CalibrationError("M2 calibration evaluation failed overall conservation")
    failed_units = tuple(
        name
        for name, unit in flowsheet.unit_results.items()
        if unit.balance is None or not unit.balance.passed(**tolerances)
    )
    if failed_units:
        raise CalibrationError(
            "M2 calibration evaluation failed unit conservation: "
            + ", ".join(failed_units)
        )


@dataclass
class _ObjectiveEvaluator:
    config: CalibrationConfig
    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    targets_kg_s: Mapping[str, float]
    solver: CalibrationSolver

    def __post_init__(self) -> None:
        self._cache: dict[tuple[float, float], ObjectiveEvaluation] = {}

    @property
    def evaluation_count(self) -> int:
        return len(self._cache)

    def evaluate(self, values_k: tuple[float, float]) -> ObjectiveEvaluation:
        cached = self._cache.get(values_k)
        if cached is not None:
            return cached
        parameter_values = {
            path: values_k[index]
            for index, path in enumerate(CALIBRATION_PARAMETER_PATHS)
        }
        calibrated_model = apply_calibration_parameters(
            self.model,
            self.config,
            parameter_values,
        )
        result = self.solver(calibrated_model, self.case, self.catalog)
        _require_conserving_success(result, calibrated_model)
        flowsheet = result.require_converged()
        predictions = {
            name: flowsheet.products[name].mass_flow_kg_s for name in CALIBRATION_TARGETS
        }
        errors = {
            name: predictions[name] - self.targets_kg_s[name]
            for name in CALIBRATION_TARGETS
        }
        scales = {item.product: item.scale_kg_s for item in self.config.targets}
        normalized = {name: errors[name] / scales[name] for name in CALIBRATION_TARGETS}
        data_misfit = sum(value * value for value in normalized.values())
        specs = {item.path: item for item in self.config.parameters}
        regularization_terms = {
            path: ((parameter_values[path] - specs[path].prior_k) / specs[path].prior_scale_k)
            ** 2
            for path in CALIBRATION_PARAMETER_PATHS
        }
        regularization_penalty = self.config.regularization_weight * sum(
            regularization_terms.values()
        )
        evaluation = ObjectiveEvaluation(
            parameters_k=parameter_values,
            predictions_kg_s=predictions,
            targets_kg_s=self.targets_kg_s,
            target_scales_kg_s=scales,
            errors_kg_s=errors,
            normalized_residuals=normalized,
            squared_regularization_terms=regularization_terms,
            regularization_weight=self.config.regularization_weight,
            data_misfit=data_misfit,
            regularization_penalty=regularization_penalty,
            total_objective=data_misfit + regularization_penalty,
            m2_input_fingerprint=flowsheet.input_fingerprint,
        )
        self._cache[values_k] = evaluation
        return evaluation


def _central_sensitivity(
    evaluator: _ObjectiveEvaluator,
    requested_values: tuple[float, float],
) -> SensitivityAnalysis:
    specs = evaluator.config.parameters
    step = evaluator.config.sensitivity.central_step_k
    reference = cast(
        tuple[float, float],
        tuple(
            min(
                item.upper_bound_k - step,
                max(item.lower_bound_k + step, requested_values[index]),
            )
            for index, item in enumerate(specs)
        ),
    )
    columns: list[tuple[float, float, float]] = []
    for index in range(2):
        minus = list(reference)
        plus = list(reference)
        minus[index] -= step
        plus[index] += step
        low = evaluator.evaluate(cast(tuple[float, float], tuple(minus)))
        high = evaluator.evaluate(cast(tuple[float, float], tuple(plus)))
        columns.append(
            cast(
                tuple[float, float, float],
                tuple(
                (high.predictions_kg_s[name] - low.predictions_kg_s[name])
                / (2.0 * step)
                for name in CALIBRATION_TARGETS
                ),
            )
        )
    matrix = tuple((columns[0][row], columns[1][row]) for row in range(3))
    target_scales = {
        item.product: item.scale_kg_s for item in evaluator.config.targets
    }
    parameter_scales = {
        item.path: item.prior_scale_k for item in evaluator.config.parameters
    }
    normalized_matrix = tuple(
        tuple(
            matrix[row_index][column_index]
            * parameter_scales[path]
            / target_scales[target]
            for column_index, path in enumerate(CALIBRATION_PARAMETER_PATHS)
        )
        for row_index, target in enumerate(CALIBRATION_TARGETS)
    )
    singular_values, column_cosine = _matrix_singular_values_and_cosine(matrix)
    settings = evaluator.config.sensitivity
    rank_threshold = max(
        settings.absolute_rank_tolerance,
        settings.relative_rank_tolerance * singular_values[0],
    )
    numerical_rank = sum(value > rank_threshold for value in singular_values)
    condition_number = (
        singular_values[0] / singular_values[1]
        if numerical_rank == 2 and singular_values[1] > 0.0
        else None
    )
    return SensitivityAnalysis(
        requested_parameters_k={
            path: requested_values[index]
            for index, path in enumerate(CALIBRATION_PARAMETER_PATHS)
        },
        reference_parameters_k={
            path: reference[index]
            for index, path in enumerate(CALIBRATION_PARAMETER_PATHS)
        },
        reference_adjusted_for_bounds=reference != requested_values,
        central_step_k=step,
        matrix_kg_s_per_k=matrix,
        target_scales_kg_s=target_scales,
        parameter_scales_k=parameter_scales,
        normalized_matrix=cast(tuple[tuple[float, float], ...], normalized_matrix),
        singular_values=singular_values,
        numerical_rank=numerical_rank,
        rank_threshold=rank_threshold,
        condition_number=condition_number,
        column_cosine=column_cosine,
    )


def _bounded_pattern_search(
    evaluator: _ObjectiveEvaluator,
) -> tuple[ObjectiveEvaluation, ObjectiveEvaluation, int, float]:
    config = evaluator.config
    current_values = tuple(item.prior_k for item in config.parameters)
    current = evaluator.evaluate(cast(tuple[float, float], current_values))
    initial = current
    step = config.optimizer.initial_step_k
    iterations = 0
    converged = False
    while iterations < config.optimizer.maximum_iterations:
        iterations += 1
        improved_sweep = False
        for index, spec in enumerate(config.parameters):
            candidates: list[tuple[tuple[float, float], ObjectiveEvaluation]] = []
            for direction in (-1.0, 1.0):
                candidate = list(current_values)
                candidate[index] = min(
                    spec.upper_bound_k,
                    max(spec.lower_bound_k, current_values[index] + direction * step),
                )
                candidate_tuple = cast(tuple[float, float], tuple(candidate))
                if candidate_tuple == current_values:
                    continue
                candidates.append((candidate_tuple, evaluator.evaluate(candidate_tuple)))
            if not candidates:
                continue
            candidate_values, candidate_evaluation = min(
                candidates,
                key=lambda item: (item[1].total_objective, item[0]),
            )
            if (
                candidate_evaluation.total_objective
                < current.total_objective
                - config.optimizer.objective_improvement_tolerance
            ):
                current_values = candidate_values
                current = candidate_evaluation
                improved_sweep = True
        if not improved_sweep:
            step *= config.optimizer.step_reduction
            if step < config.optimizer.minimum_step_k:
                converged = True
                break
    if not converged:
        raise CalibrationError(
            "bounded pattern search reached maximum_iterations before minimum_step_k"
        )
    return initial, current, iterations, step


def run_calibration(
    config: CalibrationConfig,
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    targets_kg_s: Mapping[str, float],
    *,
    solver: CalibrationSolver = solve_recycle,
) -> CalibrationResult:
    """Fit the frozen M5 parameter subset to reconciled net product flows."""

    validate_calibration_compatibility(config, model, case, catalog)
    targets = _validated_targets(targets_kg_s)
    evaluator = _ObjectiveEvaluator(config, model, case, catalog, targets, solver)
    prior_values = cast(
        tuple[float, float],
        tuple(item.prior_k for item in config.parameters),
    )
    initial_sensitivity = _central_sensitivity(evaluator, prior_values)
    if initial_sensitivity.numerical_rank != 2:
        raise CalibrationError("initial 3x2 sensitivity matrix is not full rank")
    initial, calibrated, iterations, final_step = _bounded_pattern_search(evaluator)
    if not calibrated.total_objective < initial.total_objective:
        raise CalibrationError("bounded calibration did not strictly improve the objective")
    calibrated_values = cast(
        tuple[float, float],
        tuple(calibrated.parameters_k[path] for path in CALIBRATION_PARAMETER_PATHS),
    )
    calibrated_sensitivity = _central_sensitivity(evaluator, calibrated_values)
    if calibrated_sensitivity.numerical_rank != 2:
        raise CalibrationError("calibrated 3x2 sensitivity matrix is not full rank")
    final_model = apply_calibration_parameters(model, config, calibrated.parameters_k)

    base_model_fingerprint = canonical_fingerprint(model.as_dict())
    case_fingerprint = canonical_fingerprint(case.as_dict())
    catalog_fingerprint = canonical_fingerprint(catalog.as_dict())
    target_fingerprint = canonical_fingerprint(
        {"unit": "kg/s", "values": dict(targets)}
    )
    input_bundle_fingerprint = canonical_fingerprint(
        {
            "configuration_fingerprint": config.fingerprint,
            "base_model_fingerprint": base_model_fingerprint,
            "case_fingerprint": case_fingerprint,
            "component_catalog_fingerprint": catalog_fingerprint,
            "target_fingerprint": target_fingerprint,
        }
    )
    parameter_results = tuple(
        CalibratedParameter(
            path=item.path,
            initial_k=item.prior_k,
            calibrated_k=calibrated.parameters_k[item.path],
            lower_bound_k=item.lower_bound_k,
            upper_bound_k=item.upper_bound_k,
            prior_scale_k=item.prior_scale_k,
            at_lower_bound=calibrated.parameters_k[item.path] == item.lower_bound_k,
            at_upper_bound=calibrated.parameters_k[item.path] == item.upper_bound_k,
        )
        for item in config.parameters
    )
    return CalibrationResult(
        status="success",
        initial=initial,
        calibrated=calibrated,
        parameters=parameter_results,
        initial_sensitivity=initial_sensitivity,
        calibrated_sensitivity=calibrated_sensitivity,
        optimizer_iterations=iterations,
        model_evaluations=evaluator.evaluation_count,
        final_step_k=final_step,
        versions={
            "schema_version": config.schema_version,
            "simulation_stage": "M5",
            "software_version": SOFTWARE_VERSION,
            "calibration_version": config.calibration_version,
            "model_version": config.model_version,
            "model_config_version": model.config_version,
            "base_parameter_set_version": config.base_parameter_set_version,
            "calibrated_parameter_set_version": config.calibrated_parameter_set_version,
            "case_version": config.case_version,
            "reconciliation_version": config.data_reference.reconciliation_version,
        },
        fingerprints={
            "configuration": config.fingerprint,
            "base_model": base_model_fingerprint,
            "case": case_fingerprint,
            "component_catalog": catalog_fingerprint,
            "targets": target_fingerprint,
            "input_bundle": input_bundle_fingerprint,
            "calibrated_model": canonical_fingerprint(final_model.as_dict()),
        },
    )
