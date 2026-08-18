"""Local M6 sensitivity and deterministic engineering interval propagation.

This module intentionally implements a fixed-reference, fixed-Jacobian local
contract.  It does not attach a probability level to an interval and it never
uses signed cancellation when propagating independent input envelopes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

type EvaluationStatus = Literal["success", "failed"]
type SensitivityStatus = Literal["success", "partial", "failed"]
type SensitivityEvaluator = Callable[[Mapping[str, float]], Mapping[str, float]]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _positive_number(value: object, *, context: str) -> float:
    result = _finite_number(value, context=context)
    if result <= 0.0:
        raise ValueError(f"{context} must be positive")
    return result


def _nonnegative_number(value: object, *, context: str) -> float:
    result = _finite_number(value, context=context)
    if result < 0.0:
        raise ValueError(f"{context} must be non-negative")
    return result


def _fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finite_mapping(
    values: Mapping[str, float],
    *,
    context: str,
    expected_keys: Sequence[str] | None = None,
) -> Mapping[str, float]:
    copied = {
        _identifier(name, context=f"{context} key"): _finite_number(
            value,
            context=f"{context}.{name}",
        )
        for name, value in values.items()
    }
    if expected_keys is not None and set(copied) != set(expected_keys):
        raise ValueError(
            f"{context} keys differ; missing={sorted(set(expected_keys) - set(copied))}, "
            f"unknown={sorted(set(copied) - set(expected_keys))}"
        )
    return MappingProxyType(copied)


@dataclass(frozen=True)
class InputSensitivitySpec:
    """One scalar input around a fixed central-difference reference."""

    input_id: str
    reference_value: float
    central_step: float
    normalization_scale: float
    unit: str = "1"

    def __post_init__(self) -> None:
        _identifier(self.input_id, context="sensitivity input id")
        object.__setattr__(
            self,
            "reference_value",
            _finite_number(
                self.reference_value,
                context=f"{self.input_id}.reference_value",
            ),
        )
        object.__setattr__(
            self,
            "central_step",
            _positive_number(self.central_step, context=f"{self.input_id}.central_step"),
        )
        object.__setattr__(
            self,
            "normalization_scale",
            _positive_number(
                self.normalization_scale,
                context=f"{self.input_id}.normalization_scale",
            ),
        )
        _text(self.unit, context=f"{self.input_id}.unit")

    def as_dict(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            "reference_value": self.reference_value,
            "central_step": self.central_step,
            "normalization_scale": self.normalization_scale,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class OutputSensitivitySpec:
    """One finite model output and its fixed normalization/numerical margin."""

    output_id: str
    normalization_scale: float
    unit: str = "1"
    numerical_margin: float = 0.0

    def __post_init__(self) -> None:
        _identifier(self.output_id, context="sensitivity output id")
        object.__setattr__(
            self,
            "normalization_scale",
            _positive_number(
                self.normalization_scale,
                context=f"{self.output_id}.normalization_scale",
            ),
        )
        _text(self.unit, context=f"{self.output_id}.unit")
        object.__setattr__(
            self,
            "numerical_margin",
            _nonnegative_number(
                self.numerical_margin,
                context=f"{self.output_id}.numerical_margin",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "output_id": self.output_id,
            "normalization_scale": self.normalization_scale,
            "unit": self.unit,
            "numerical_margin": self.numerical_margin,
        }


@dataclass(frozen=True)
class EvaluationRecord:
    """One isolated reference or perturbation evaluation."""

    label: str
    status: EvaluationStatus
    inputs: Mapping[str, float]
    outputs: Mapping[str, float]
    input_fingerprint: str
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.label, context="evaluation label")
        if self.status not in {"success", "failed"}:
            raise ValueError("evaluation status must be success or failed")
        inputs = _finite_mapping(self.inputs, context="evaluation inputs")
        if not inputs:
            raise ValueError("evaluation inputs cannot be empty")
        outputs = _finite_mapping(self.outputs, context="evaluation outputs")
        if self.status == "success":
            if not outputs:
                raise ValueError("a successful evaluation requires outputs")
            if self.failure_reason is not None:
                raise ValueError("a successful evaluation cannot have a failure reason")
        else:
            if outputs:
                raise ValueError("a failed evaluation cannot expose valid outputs")
            if self.failure_reason is None:
                raise ValueError("a failed evaluation requires a failure reason")
            _text(self.failure_reason, context="evaluation failure reason")
        if not _SHA256.fullmatch(self.input_fingerprint):
            raise ValueError("evaluation input_fingerprint must be a SHA-256 digest")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "status": self.status,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "input_fingerprint": self.input_fingerprint,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class LocalSensitivityAnalysis:
    """Fixed-reference local Jacobian with per-evaluation failure evidence."""

    status: SensitivityStatus
    basis_fingerprint: str
    input_specs: tuple[InputSensitivitySpec, ...]
    output_specs: tuple[OutputSensitivitySpec, ...]
    baseline_outputs: Mapping[str, float]
    evaluations: tuple[EvaluationRecord, ...]
    matrix: tuple[tuple[float | None, ...], ...]
    normalized_matrix: tuple[tuple[float | None, ...], ...]
    analysis_fingerprint: str

    def __post_init__(self) -> None:
        if self.status not in {"success", "partial", "failed"}:
            raise ValueError("invalid local-sensitivity status")
        if not _SHA256.fullmatch(self.basis_fingerprint):
            raise ValueError("basis_fingerprint must be a lowercase SHA-256 digest")
        inputs = tuple(self.input_specs)
        outputs = tuple(self.output_specs)
        if not inputs or any(not isinstance(item, InputSensitivitySpec) for item in inputs):
            raise TypeError("input_specs must contain InputSensitivitySpec values")
        if not outputs or any(
            not isinstance(item, OutputSensitivitySpec) for item in outputs
        ):
            raise TypeError("output_specs must contain OutputSensitivitySpec values")
        input_ids = tuple(item.input_id for item in inputs)
        output_ids = tuple(item.output_id for item in outputs)
        if len(set(input_ids)) != len(input_ids) or len(set(output_ids)) != len(output_ids):
            raise ValueError("sensitivity input and output ids must be unique")
        evaluations = tuple(self.evaluations)
        expected_labels = ("baseline",) + tuple(
            label
            for item in inputs
            for label in (f"{item.input_id}:minus", f"{item.input_id}:plus")
        )
        if tuple(item.label for item in evaluations) != expected_labels:
            raise ValueError("sensitivity evaluation labels or order differ")
        if any(not isinstance(item, EvaluationRecord) for item in evaluations):
            raise TypeError("evaluations must contain EvaluationRecord values")
        baseline = _finite_mapping(
            self.baseline_outputs,
            context="baseline outputs",
            expected_keys=output_ids if self.baseline_outputs else None,
        )
        baseline_success = evaluations[0].status == "success"
        if baseline_success != bool(baseline):
            raise ValueError("baseline outputs differ from baseline evaluation status")
        if baseline_success and dict(baseline) != dict(evaluations[0].outputs):
            raise ValueError("baseline outputs differ from the baseline evaluation")

        matrix = tuple(tuple(row) for row in self.matrix)
        normalized = tuple(tuple(row) for row in self.normalized_matrix)
        expected_shape = (len(outputs), len(inputs))
        for name, values in (("matrix", matrix), ("normalized_matrix", normalized)):
            if len(values) != expected_shape[0] or any(
                len(row) != expected_shape[1] for row in values
            ):
                raise ValueError(f"{name} has the wrong shape")
            for row in values:
                for value in row:
                    if value is not None:
                        _finite_number(value, context=name)
        for row_index, output in enumerate(outputs):
            for column_index, input_spec in enumerate(inputs):
                raw = matrix[row_index][column_index]
                scaled = normalized[row_index][column_index]
                if (raw is None) != (scaled is None):
                    raise ValueError("raw and normalized sensitivity missingness differs")
                if raw is not None and scaled is not None:
                    expected = (
                        raw
                        * input_spec.normalization_scale
                        / output.normalization_scale
                    )
                    if not math.isclose(scaled, expected, rel_tol=1e-12, abs_tol=1e-12):
                        raise ValueError("normalized sensitivity does not match its scales")
        complete_columns = sum(
            all(matrix[row][column] is not None for row in range(len(outputs)))
            for column in range(len(inputs))
        )
        all_evaluations_succeeded = all(item.status == "success" for item in evaluations)
        expected_status: SensitivityStatus
        if all_evaluations_succeeded:
            expected_status = "success"
        elif baseline_success and complete_columns > 0:
            expected_status = "partial"
        else:
            expected_status = "failed"
        if self.status != expected_status:
            raise ValueError("local-sensitivity status differs from evaluation evidence")
        if not _SHA256.fullmatch(self.analysis_fingerprint):
            raise ValueError("analysis_fingerprint must be a lowercase SHA-256 digest")
        payload = _analysis_payload(
            status=self.status,
            basis_fingerprint=self.basis_fingerprint,
            input_specs=inputs,
            output_specs=outputs,
            baseline_outputs=baseline,
            evaluations=evaluations,
            matrix=matrix,
            normalized_matrix=normalized,
        )
        if _fingerprint(payload) != self.analysis_fingerprint:
            raise ValueError("analysis_fingerprint differs from local-sensitivity content")
        object.__setattr__(self, "input_specs", inputs)
        object.__setattr__(self, "output_specs", outputs)
        object.__setattr__(self, "baseline_outputs", baseline)
        object.__setattr__(self, "evaluations", evaluations)
        object.__setattr__(self, "matrix", matrix)
        object.__setattr__(self, "normalized_matrix", normalized)

    @property
    def complete(self) -> bool:
        return self.status == "success"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "basis_fingerprint": self.basis_fingerprint,
            "input_specs": [item.as_dict() for item in self.input_specs],
            "output_specs": [item.as_dict() for item in self.output_specs],
            "baseline_outputs": dict(self.baseline_outputs),
            "evaluations": [item.as_dict() for item in self.evaluations],
            "matrix": [list(row) for row in self.matrix],
            "normalized_matrix": [list(row) for row in self.normalized_matrix],
            "analysis_fingerprint": self.analysis_fingerprint,
        }


def _analysis_payload(
    *,
    status: SensitivityStatus,
    basis_fingerprint: str,
    input_specs: Sequence[InputSensitivitySpec],
    output_specs: Sequence[OutputSensitivitySpec],
    baseline_outputs: Mapping[str, float],
    evaluations: Sequence[EvaluationRecord],
    matrix: Sequence[Sequence[float | None]],
    normalized_matrix: Sequence[Sequence[float | None]],
) -> dict[str, object]:
    return {
        "status": status,
        "basis_fingerprint": basis_fingerprint,
        "input_specs": [item.as_dict() for item in input_specs],
        "output_specs": [item.as_dict() for item in output_specs],
        "baseline_outputs": dict(baseline_outputs),
        "evaluations": [item.as_dict() for item in evaluations],
        "matrix": [list(row) for row in matrix],
        "normalized_matrix": [list(row) for row in normalized_matrix],
    }


def _evaluate(
    label: str,
    inputs: Mapping[str, float],
    output_ids: tuple[str, ...],
    evaluator: SensitivityEvaluator,
) -> EvaluationRecord:
    copied_inputs = dict(inputs)
    input_fingerprint = _fingerprint({"inputs": copied_inputs})
    try:
        raw_outputs = evaluator(MappingProxyType(copied_inputs))
        if not isinstance(raw_outputs, Mapping):
            raise TypeError("sensitivity evaluator must return a mapping")
        outputs = _finite_mapping(
            raw_outputs,
            context=f"evaluation {label} outputs",
            expected_keys=output_ids,
        )
        return EvaluationRecord(
            label=label,
            status="success",
            inputs=copied_inputs,
            outputs=outputs,
            input_fingerprint=input_fingerprint,
        )
    except Exception as exc:  # noqa: BLE001 - failure isolation is the contract here.
        detail = str(exc).strip()
        reason = type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
        return EvaluationRecord(
            label=label,
            status="failed",
            inputs=copied_inputs,
            outputs={},
            input_fingerprint=input_fingerprint,
            failure_reason=reason,
        )


def run_local_sensitivity(
    input_specs: Sequence[InputSensitivitySpec],
    output_specs: Sequence[OutputSensitivitySpec],
    evaluator: SensitivityEvaluator,
    *,
    basis_fingerprint: str,
) -> LocalSensitivityAnalysis:
    """Evaluate every central-difference pair and isolate individual failures."""

    inputs = tuple(input_specs)
    outputs = tuple(output_specs)
    if not inputs or any(not isinstance(item, InputSensitivitySpec) for item in inputs):
        raise TypeError("input_specs must be a non-empty sequence of input specs")
    if not outputs or any(not isinstance(item, OutputSensitivitySpec) for item in outputs):
        raise TypeError("output_specs must be a non-empty sequence of output specs")
    if len({item.input_id for item in inputs}) != len(inputs):
        raise ValueError("input sensitivity ids must be unique")
    if len({item.output_id for item in outputs}) != len(outputs):
        raise ValueError("output sensitivity ids must be unique")
    if not callable(evaluator):
        raise TypeError("evaluator must be callable")
    if not _SHA256.fullmatch(basis_fingerprint):
        raise ValueError("basis_fingerprint must be a lowercase SHA-256 digest")

    reference = {item.input_id: item.reference_value for item in inputs}
    output_ids = tuple(item.output_id for item in outputs)
    evaluations: list[EvaluationRecord] = [
        _evaluate("baseline", reference, output_ids, evaluator)
    ]
    pairs: dict[str, tuple[EvaluationRecord, EvaluationRecord]] = {}
    for item in inputs:
        minus_inputs = dict(reference)
        plus_inputs = dict(reference)
        minus_inputs[item.input_id] -= item.central_step
        plus_inputs[item.input_id] += item.central_step
        minus = _evaluate(f"{item.input_id}:minus", minus_inputs, output_ids, evaluator)
        plus = _evaluate(f"{item.input_id}:plus", plus_inputs, output_ids, evaluator)
        evaluations.extend((minus, plus))
        pairs[item.input_id] = (minus, plus)

    matrix_rows: list[tuple[float | None, ...]] = []
    normalized_rows: list[tuple[float | None, ...]] = []
    for output in outputs:
        row: list[float | None] = []
        normalized_row: list[float | None] = []
        for item in inputs:
            minus, plus = pairs[item.input_id]
            if minus.status == "failed" or plus.status == "failed":
                derivative = None
                normalized_derivative = None
            else:
                derivative = (
                    plus.outputs[output.output_id] - minus.outputs[output.output_id]
                ) / (2.0 * item.central_step)
                normalized_derivative = (
                    derivative
                    * item.normalization_scale
                    / output.normalization_scale
                )
            row.append(derivative)
            normalized_row.append(normalized_derivative)
        matrix_rows.append(tuple(row))
        normalized_rows.append(tuple(normalized_row))

    baseline = evaluations[0]
    complete_columns = sum(
        pairs[item.input_id][0].status == "success"
        and pairs[item.input_id][1].status == "success"
        for item in inputs
    )
    if all(item.status == "success" for item in evaluations):
        status: SensitivityStatus = "success"
    elif baseline.status == "success" and complete_columns > 0:
        status = "partial"
    else:
        status = "failed"
    baseline_outputs = dict(baseline.outputs) if baseline.status == "success" else {}
    matrix = tuple(matrix_rows)
    normalized_matrix = tuple(normalized_rows)
    payload = _analysis_payload(
        status=status,
        basis_fingerprint=basis_fingerprint,
        input_specs=inputs,
        output_specs=outputs,
        baseline_outputs=baseline_outputs,
        evaluations=evaluations,
        matrix=matrix,
        normalized_matrix=normalized_matrix,
    )
    return LocalSensitivityAnalysis(
        status=status,
        basis_fingerprint=basis_fingerprint,
        input_specs=inputs,
        output_specs=outputs,
        baseline_outputs=baseline_outputs,
        evaluations=tuple(evaluations),
        matrix=matrix,
        normalized_matrix=normalized_matrix,
        analysis_fingerprint=_fingerprint(payload),
    )


@dataclass(frozen=True)
class EngineeringInputInterval:
    """Non-probabilistic input envelope around the sensitivity reference."""

    input_id: str
    lower: float
    upper: float
    confidence_multiplier: float = 1.0
    confidence_label: str = "bounded"

    def __post_init__(self) -> None:
        _identifier(self.input_id, context="engineering input interval id")
        lower = _finite_number(self.lower, context=f"{self.input_id}.lower")
        upper = _finite_number(self.upper, context=f"{self.input_id}.upper")
        if lower > upper:
            raise ValueError(f"{self.input_id} interval lower bound exceeds upper bound")
        multiplier = _finite_number(
            self.confidence_multiplier,
            context=f"{self.input_id}.confidence_multiplier",
        )
        if multiplier < 1.0:
            raise ValueError("confidence_multiplier must be at least one")
        _identifier(self.confidence_label, context=f"{self.input_id}.confidence_label")
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)
        object.__setattr__(self, "confidence_multiplier", multiplier)

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def radius_about(self, reference_value: float) -> float:
        reference = _finite_number(reference_value, context="interval reference value")
        if not self.lower <= reference <= self.upper:
            raise ValueError(f"{self.input_id} interval must contain its reference value")
        return max(reference - self.lower, self.upper - reference) * self.confidence_multiplier

    def as_dict(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            "lower": self.lower,
            "upper": self.upper,
            "width": self.width,
            "confidence_multiplier": self.confidence_multiplier,
            "confidence_label": self.confidence_label,
        }


@dataclass(frozen=True)
class EngineeringOutputInterval:
    """One fixed-Jacobian output enclosure and its non-negative contributors."""

    output_id: str
    unit: str
    reference_value: float
    lower: float
    upper: float
    radius: float
    numerical_margin: float
    contributions: Mapping[str, float]

    def __post_init__(self) -> None:
        _identifier(self.output_id, context="engineering output interval id")
        _text(self.unit, context=f"{self.output_id}.unit")
        reference = _finite_number(
            self.reference_value,
            context=f"{self.output_id}.reference_value",
        )
        lower = _finite_number(self.lower, context=f"{self.output_id}.lower")
        upper = _finite_number(self.upper, context=f"{self.output_id}.upper")
        radius = _nonnegative_number(self.radius, context=f"{self.output_id}.radius")
        margin = _nonnegative_number(
            self.numerical_margin,
            context=f"{self.output_id}.numerical_margin",
        )
        if not math.isclose(lower, reference - radius, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("output interval lower bound differs from reference-radius")
        if not math.isclose(upper, reference + radius, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("output interval upper bound differs from reference+radius")
        copied = dict(self.contributions)
        if not copied:
            raise ValueError("output interval requires input contributions")
        for name, value in copied.items():
            _identifier(name, context="output contribution input id")
            copied[name] = _nonnegative_number(
                value,
                context=f"{self.output_id}.contributions.{name}",
            )
        expected_radius = margin + math.fsum(copied.values())
        if not math.isclose(radius, expected_radius, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("output interval radius differs from margin plus contributions")
        object.__setattr__(self, "reference_value", reference)
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)
        object.__setattr__(self, "radius", radius)
        object.__setattr__(self, "numerical_margin", margin)
        object.__setattr__(self, "contributions", MappingProxyType(copied))

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def as_dict(self) -> dict[str, object]:
        return {
            "output_id": self.output_id,
            "unit": self.unit,
            "reference_value": self.reference_value,
            "lower": self.lower,
            "upper": self.upper,
            "radius": self.radius,
            "width": self.width,
            "numerical_margin": self.numerical_margin,
            "contributions": dict(self.contributions),
        }


@dataclass(frozen=True)
class UncertaintyPropagationResult:
    """Complete fixed-Jacobian interval result with monotonicity comparison."""

    basis_fingerprint: str
    sensitivity_fingerprint: str
    input_intervals: tuple[EngineeringInputInterval, ...]
    input_radii: Mapping[str, float]
    output_intervals: tuple[EngineeringOutputInterval, ...]
    result_fingerprint: str
    interval_semantics: str = "deterministic_engineering_envelope"

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.basis_fingerprint):
            raise ValueError("basis_fingerprint must be a SHA-256 digest")
        if not _SHA256.fullmatch(self.sensitivity_fingerprint):
            raise ValueError("sensitivity_fingerprint must be a SHA-256 digest")
        inputs = tuple(self.input_intervals)
        outputs = tuple(self.output_intervals)
        if not inputs or any(not isinstance(item, EngineeringInputInterval) for item in inputs):
            raise TypeError("input_intervals must contain engineering intervals")
        if not outputs or any(
            not isinstance(item, EngineeringOutputInterval) for item in outputs
        ):
            raise TypeError("output_intervals must contain engineering intervals")
        input_ids = tuple(item.input_id for item in inputs)
        output_ids = tuple(item.output_id for item in outputs)
        if len(set(input_ids)) != len(input_ids) or len(set(output_ids)) != len(output_ids):
            raise ValueError("propagation input and output ids must be unique")
        radii = _finite_mapping(
            self.input_radii,
            context="propagation input radii",
            expected_keys=input_ids,
        )
        if any(value < 0.0 for value in radii.values()):
            raise ValueError("propagation input radii must be non-negative")
        _identifier(self.interval_semantics, context="interval semantics")
        if not _SHA256.fullmatch(self.result_fingerprint):
            raise ValueError("result_fingerprint must be a SHA-256 digest")
        payload = _propagation_payload(
            basis_fingerprint=self.basis_fingerprint,
            sensitivity_fingerprint=self.sensitivity_fingerprint,
            input_intervals=inputs,
            input_radii=radii,
            output_intervals=outputs,
            interval_semantics=self.interval_semantics,
        )
        if _fingerprint(payload) != self.result_fingerprint:
            raise ValueError("result_fingerprint differs from propagation content")
        object.__setattr__(self, "input_intervals", inputs)
        object.__setattr__(self, "input_radii", radii)
        object.__setattr__(self, "output_intervals", outputs)

    def assert_not_narrower_than(
        self,
        narrower: UncertaintyPropagationResult,
    ) -> None:
        """Assert this result comes from nested/no-higher-confidence inputs."""

        if not isinstance(narrower, UncertaintyPropagationResult):
            raise TypeError("narrower must be an UncertaintyPropagationResult")
        if self.basis_fingerprint != narrower.basis_fingerprint:
            raise ValueError("cannot compare uncertainty results from different bases")
        if self.sensitivity_fingerprint != narrower.sensitivity_fingerprint:
            raise ValueError("cannot compare uncertainty results from different Jacobians")
        wider_inputs = {item.input_id: item for item in self.input_intervals}
        narrower_inputs = {item.input_id: item for item in narrower.input_intervals}
        if set(wider_inputs) != set(narrower_inputs):
            raise ValueError("cannot compare different uncertainty input sets")
        for input_id, current in wider_inputs.items():
            prior = narrower_inputs[input_id]
            if current.lower > prior.lower or current.upper < prior.upper:
                raise ValueError(f"input interval {input_id} is not nested around the prior one")
            if current.confidence_multiplier < prior.confidence_multiplier:
                raise ValueError(f"input confidence multiplier {input_id} became smaller")
            if self.input_radii[input_id] < narrower.input_radii[input_id]:
                raise AssertionError(f"effective input radius {input_id} unexpectedly shrank")
        wider_outputs = {item.output_id: item for item in self.output_intervals}
        narrower_outputs = {item.output_id: item for item in narrower.output_intervals}
        if set(wider_outputs) != set(narrower_outputs):
            raise ValueError("cannot compare different uncertainty output sets")
        for output_id, output_current in wider_outputs.items():
            output_prior = narrower_outputs[output_id]
            tolerance = 1e-12 * max(
                1.0,
                abs(output_current.lower),
                abs(output_current.upper),
                abs(output_prior.lower),
                abs(output_prior.upper),
            )
            if output_current.lower > output_prior.lower + tolerance:
                raise AssertionError(f"output interval {output_id} lower bound increased")
            if output_current.upper < output_prior.upper - tolerance:
                raise AssertionError(f"output interval {output_id} upper bound decreased")
            if output_current.width < output_prior.width - tolerance:
                raise AssertionError(f"output interval {output_id} width decreased")

    def as_dict(self) -> dict[str, object]:
        return {
            "basis_fingerprint": self.basis_fingerprint,
            "sensitivity_fingerprint": self.sensitivity_fingerprint,
            "interval_semantics": self.interval_semantics,
            "input_intervals": [item.as_dict() for item in self.input_intervals],
            "input_radii": dict(self.input_radii),
            "output_intervals": [item.as_dict() for item in self.output_intervals],
            "result_fingerprint": self.result_fingerprint,
        }


def _propagation_payload(
    *,
    basis_fingerprint: str,
    sensitivity_fingerprint: str,
    input_intervals: Sequence[EngineeringInputInterval],
    input_radii: Mapping[str, float],
    output_intervals: Sequence[EngineeringOutputInterval],
    interval_semantics: str,
) -> dict[str, object]:
    return {
        "basis_fingerprint": basis_fingerprint,
        "sensitivity_fingerprint": sensitivity_fingerprint,
        "interval_semantics": interval_semantics,
        "input_intervals": [item.as_dict() for item in input_intervals],
        "input_radii": dict(input_radii),
        "output_intervals": [item.as_dict() for item in output_intervals],
    }


def propagate_uncertainty(
    analysis: LocalSensitivityAnalysis,
    intervals: Sequence[EngineeringInputInterval],
) -> UncertaintyPropagationResult:
    """Propagate intervals with ``margin + sum(abs(J) * effective_radius)``."""

    if not isinstance(analysis, LocalSensitivityAnalysis):
        raise TypeError("analysis must be a LocalSensitivityAnalysis")
    if not analysis.complete:
        raise ValueError("uncertainty propagation requires a complete sensitivity analysis")
    supplied = tuple(intervals)
    if any(not isinstance(item, EngineeringInputInterval) for item in supplied):
        raise TypeError("intervals must contain EngineeringInputInterval values")
    by_id = {item.input_id: item for item in supplied}
    expected_ids = tuple(item.input_id for item in analysis.input_specs)
    if len(by_id) != len(supplied) or set(by_id) != set(expected_ids):
        raise ValueError(
            "uncertainty interval ids differ from the sensitivity inputs; "
            f"missing={sorted(set(expected_ids) - set(by_id))}, "
            f"unknown={sorted(set(by_id) - set(expected_ids))}"
        )
    ordered_intervals = tuple(by_id[input_id] for input_id in expected_ids)
    input_radii = {
        spec.input_id: by_id[spec.input_id].radius_about(spec.reference_value)
        for spec in analysis.input_specs
    }
    output_intervals: list[EngineeringOutputInterval] = []
    for row_index, output in enumerate(analysis.output_specs):
        contributions: dict[str, float] = {}
        for column_index, input_spec in enumerate(analysis.input_specs):
            derivative = analysis.matrix[row_index][column_index]
            if derivative is None:  # pragma: no cover - complete analysis proves otherwise.
                raise ValueError("complete sensitivity analysis contains a missing derivative")
            contributions[input_spec.input_id] = (
                abs(derivative) * input_radii[input_spec.input_id]
            )
        radius = output.numerical_margin + math.fsum(contributions.values())
        reference = analysis.baseline_outputs[output.output_id]
        output_intervals.append(
            EngineeringOutputInterval(
                output_id=output.output_id,
                unit=output.unit,
                reference_value=reference,
                lower=reference - radius,
                upper=reference + radius,
                radius=radius,
                numerical_margin=output.numerical_margin,
                contributions=contributions,
            )
        )
    payload = _propagation_payload(
        basis_fingerprint=analysis.basis_fingerprint,
        sensitivity_fingerprint=analysis.analysis_fingerprint,
        input_intervals=ordered_intervals,
        input_radii=input_radii,
        output_intervals=output_intervals,
        interval_semantics="deterministic_engineering_envelope",
    )
    return UncertaintyPropagationResult(
        basis_fingerprint=analysis.basis_fingerprint,
        sensitivity_fingerprint=analysis.analysis_fingerprint,
        input_intervals=ordered_intervals,
        input_radii=input_radii,
        output_intervals=tuple(output_intervals),
        result_fingerprint=_fingerprint(payload),
    )


def assert_uncertainty_not_narrower(
    wider: UncertaintyPropagationResult,
    narrower: UncertaintyPropagationResult,
) -> None:
    """Functional form of the nested-range monotonicity assertion."""

    wider.assert_not_narrower_than(narrower)
