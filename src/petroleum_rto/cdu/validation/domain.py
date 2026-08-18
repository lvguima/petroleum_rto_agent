"""Deterministic engineering applicability-domain assessment for M6.

The domain is deliberately an engineering claim envelope, not a statistical
confidence region.  Assessment is pure and happens before any model evaluator
is called so unsupported, unknown, or non-finite inputs cannot leak into a
simulation run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

type DomainRepresentation = Literal["direct", "proxy", "unsupported"]
type DomainStatus = Literal["passed", "limited", "rejected"]
type DomainInputLayer = Literal[
    "M2_steady",
    "M3_open_loop",
    "M4_closed_loop",
    "M6_supervision",
    "structural_rejection",
    "M2_M3_shared",
    "M2_M3_M4_shared",
]
type DomainConfidence = Literal[
    "low_engineering",
    "low_case_observation",
    "low_single_case_aligned",
    "low_proxy",
    "synthetic_logic_only",
    "not_applicable",
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPRESENTATIONS = frozenset({"direct", "proxy", "unsupported"})
_STATUSES = frozenset({"passed", "limited", "rejected"})
_INPUT_LAYERS = frozenset(
    {
        "M2_steady",
        "M3_open_loop",
        "M4_closed_loop",
        "M6_supervision",
        "structural_rejection",
        "M2_M3_shared",
        "M2_M3_M4_shared",
    }
)
_CONFIDENCE_LEVELS = frozenset(
    {
        "low_engineering",
        "low_case_observation",
        "low_single_case_aligned",
        "low_proxy",
        "synthetic_logic_only",
        "not_applicable",
    }
)


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def _identifier_tuple(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise TypeError(f"{context} must be a sequence of identifiers")
    copied = tuple(
        _identifier(item, context=f"{context}[{index}]")
        for index, item in enumerate(value)
    )
    if not copied:
        raise ValueError(f"{context} cannot be empty")
    if len(set(copied)) != len(copied):
        raise ValueError(f"{context} cannot contain duplicates")
    return tuple(sorted(copied))


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _canonical_fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _invalid_value_label(value: object) -> str:
    if isinstance(value, bool):
        return f"bool:{str(value).lower()}"
    if isinstance(value, float):
        if math.isnan(value):
            return "nonfinite:nan"
        if value == math.inf:
            return "nonfinite:+inf"
        if value == -math.inf:
            return "nonfinite:-inf"
    return f"type:{type(value).__name__}"


@dataclass(frozen=True)
class DomainDimension:
    """One scalar input dimension with nested normal and limited envelopes."""

    dimension_id: str
    unit: str
    representation: DomainRepresentation
    reference_value: float
    normal_min: float
    normal_max: float
    limited_min: float
    limited_max: float
    source: str
    input_layer: DomainInputLayer = "M2_steady"
    confidence: DomainConfidence = "low_engineering"
    assumptions: tuple[str, ...] = ("synthetic_engineering_envelope",)

    def __post_init__(self) -> None:
        _identifier(self.dimension_id, context="domain dimension id")
        _text(self.unit, context=f"{self.dimension_id}.unit")
        if self.representation not in _REPRESENTATIONS:
            raise ValueError(
                f"{self.dimension_id}.representation must be direct, proxy or unsupported"
            )
        if (
            not isinstance(self.input_layer, str)
            or self.input_layer not in _INPUT_LAYERS
        ):
            raise ValueError(f"{self.dimension_id}.input_layer is unsupported")
        if (
            not isinstance(self.confidence, str)
            or self.confidence not in _CONFIDENCE_LEVELS
        ):
            raise ValueError(f"{self.dimension_id}.confidence is unsupported")
        assumptions = _identifier_tuple(
            self.assumptions,
            context=f"{self.dimension_id}.assumptions",
        )
        reference = _finite_number(
            self.reference_value,
            context=f"{self.dimension_id}.reference_value",
        )
        normal_min = _finite_number(
            self.normal_min,
            context=f"{self.dimension_id}.normal_min",
        )
        normal_max = _finite_number(
            self.normal_max,
            context=f"{self.dimension_id}.normal_max",
        )
        limited_min = _finite_number(
            self.limited_min,
            context=f"{self.dimension_id}.limited_min",
        )
        limited_max = _finite_number(
            self.limited_max,
            context=f"{self.dimension_id}.limited_max",
        )
        if not limited_min < normal_min < reference < normal_max < limited_max:
            raise ValueError(
                f"{self.dimension_id} bounds must satisfy "
                "limited_min < normal_min < reference_value < normal_max < limited_max"
            )
        _text(self.source, context=f"{self.dimension_id}.source")
        object.__setattr__(self, "reference_value", reference)
        object.__setattr__(self, "normal_min", normal_min)
        object.__setattr__(self, "normal_max", normal_max)
        object.__setattr__(self, "limited_min", limited_min)
        object.__setattr__(self, "limited_max", limited_max)
        object.__setattr__(self, "assumptions", assumptions)

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension_id": self.dimension_id,
            "unit": self.unit,
            "representation": self.representation,
            "input_layer": self.input_layer,
            "confidence": self.confidence,
            "assumptions": list(self.assumptions),
            "reference_value": self.reference_value,
            "normal_min": self.normal_min,
            "normal_max": self.normal_max,
            "limited_min": self.limited_min,
            "limited_max": self.limited_max,
            "source": self.source,
        }


@dataclass(frozen=True)
class DimensionAssessment:
    """Resolved value, distance, status, and reasons for one domain dimension."""

    dimension_id: str
    unit: str
    representation: DomainRepresentation
    requested: bool
    reference_value: float
    value: float | None
    invalid_value: str | None
    reference_distance: float | None
    excess_distance: float | None
    status: DomainStatus
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.dimension_id, context="dimension assessment id")
        _text(self.unit, context=f"{self.dimension_id}.unit")
        if self.representation not in _REPRESENTATIONS:
            raise ValueError("invalid dimension-assessment representation")
        if not isinstance(self.requested, bool):
            raise TypeError("dimension-assessment requested flag must be boolean")
        _finite_number(
            self.reference_value,
            context=f"{self.dimension_id}.reference_value",
        )
        if self.status not in _STATUSES:
            raise ValueError("invalid dimension-assessment status")
        if (self.value is None) == (self.invalid_value is None):
            raise ValueError(
                "a dimension assessment requires exactly one of value or invalid_value"
            )
        if self.value is not None:
            _finite_number(self.value, context=f"{self.dimension_id}.value")
            for name, distance in (
                ("reference_distance", self.reference_distance),
                ("excess_distance", self.excess_distance),
            ):
                if distance is None:
                    raise ValueError(f"valid dimension value requires {name}")
                number = _finite_number(distance, context=f"{self.dimension_id}.{name}")
                if number < 0.0:
                    raise ValueError(f"{self.dimension_id}.{name} must be non-negative")
        elif self.reference_distance is not None or self.excess_distance is not None:
            raise ValueError("invalid dimension values cannot have numeric distances")
        if self.invalid_value is not None:
            _text(self.invalid_value, context=f"{self.dimension_id}.invalid_value")
        if any(not isinstance(reason, str) or not reason for reason in self.reasons):
            raise ValueError("dimension-assessment reasons must be non-empty strings")
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension_id": self.dimension_id,
            "unit": self.unit,
            "representation": self.representation,
            "requested": self.requested,
            "reference_value": self.reference_value,
            "value": self.value,
            "invalid_value": self.invalid_value,
            "reference_distance": self.reference_distance,
            "excess_distance": self.excess_distance,
            "status": self.status,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ApplicabilityAssessment:
    """Aggregate applicability decision suitable for pre-solver gating."""

    status: DomainStatus
    abnormal_verification: bool
    dimensions: tuple[DimensionAssessment, ...]
    resolved_inputs: Mapping[str, float | str]
    unknown_inputs: tuple[str, ...]
    maximum_reference_distance: float | None
    maximum_excess_distance: float | None
    reasons: tuple[str, ...]
    input_fingerprint: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError("invalid applicability status")
        if not isinstance(self.abnormal_verification, bool):
            raise TypeError("abnormal_verification must be boolean")
        dimensions = tuple(self.dimensions)
        if not dimensions or any(
            not isinstance(item, DimensionAssessment) for item in dimensions
        ):
            raise TypeError("applicability dimensions must be non-empty assessments")
        dimension_ids = tuple(item.dimension_id for item in dimensions)
        if len(set(dimension_ids)) != len(dimension_ids):
            raise ValueError("applicability dimension ids must be unique")
        copied_inputs = dict(self.resolved_inputs)
        if set(copied_inputs) != set(dimension_ids):
            raise ValueError("resolved inputs must exactly match assessed dimensions")
        for name, value in copied_inputs.items():
            if not isinstance(name, str) or not isinstance(value, (float, str)):
                raise TypeError("resolved inputs must map strings to floats or labels")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("resolved input floats must be finite")
        unknown = tuple(self.unknown_inputs)
        if tuple(sorted(set(unknown))) != unknown:
            raise ValueError("unknown inputs must be sorted and unique")
        for name, distance in (
            ("maximum_reference_distance", self.maximum_reference_distance),
            ("maximum_excess_distance", self.maximum_excess_distance),
        ):
            if distance is not None:
                number = _finite_number(distance, context=name)
                if number < 0.0:
                    raise ValueError(f"{name} must be non-negative")
        if any(not isinstance(reason, str) or not reason for reason in self.reasons):
            raise ValueError("applicability reasons must be non-empty strings")
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_fingerprint):
            raise ValueError("input_fingerprint must be a lowercase SHA-256 digest")
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "resolved_inputs", MappingProxyType(copied_inputs))
        object.__setattr__(self, "unknown_inputs", unknown)
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def solver_allowed(self) -> bool:
        """Return whether downstream evaluation may run."""

        return self.status != "rejected"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "solver_allowed": self.solver_allowed,
            "abnormal_verification": self.abnormal_verification,
            "dimensions": [item.as_dict() for item in self.dimensions],
            "resolved_inputs": dict(self.resolved_inputs),
            "unknown_inputs": list(self.unknown_inputs),
            "maximum_reference_distance": self.maximum_reference_distance,
            "maximum_excess_distance": self.maximum_excess_distance,
            "reasons": list(self.reasons),
            "input_fingerprint": self.input_fingerprint,
        }


def _assess_dimension(
    dimension: DomainDimension,
    raw_value: object,
    *,
    requested: bool,
) -> DimensionAssessment:
    try:
        value = _finite_number(raw_value, context=f"input {dimension.dimension_id}")
    except (TypeError, ValueError):
        reason = f"{dimension.dimension_id}:invalid_or_nonfinite_input"
        return DimensionAssessment(
            dimension_id=dimension.dimension_id,
            unit=dimension.unit,
            representation=dimension.representation,
            requested=requested,
            reference_value=dimension.reference_value,
            value=None,
            invalid_value=_invalid_value_label(raw_value),
            reference_distance=None,
            excess_distance=None,
            status="rejected",
            reasons=(reason,),
        )

    if value >= dimension.reference_value:
        reference_distance = (value - dimension.reference_value) / (
            dimension.normal_max - dimension.reference_value
        )
    else:
        reference_distance = (dimension.reference_value - value) / (
            dimension.reference_value - dimension.normal_min
        )

    if dimension.normal_min <= value <= dimension.normal_max:
        excess_distance = 0.0
    elif value > dimension.normal_max:
        excess_distance = (value - dimension.normal_max) / (
            dimension.limited_max - dimension.normal_max
        )
    else:
        excess_distance = (dimension.normal_min - value) / (
            dimension.normal_min - dimension.limited_min
        )

    reasons: list[str] = []
    if not requested:
        status: DomainStatus = "passed"
    elif dimension.representation == "unsupported":
        status = "rejected"
        reasons.append(f"{dimension.dimension_id}:unsupported_model_input")
    elif value < dimension.limited_min or value > dimension.limited_max:
        status = "rejected"
        reasons.append(f"{dimension.dimension_id}:outside_limited_domain")
    elif dimension.representation == "proxy":
        status = "limited"
        reasons.append(f"{dimension.dimension_id}:proxy_representation")
        if not dimension.normal_min <= value <= dimension.normal_max:
            reasons.append(f"{dimension.dimension_id}:outside_normal_domain")
    elif not dimension.normal_min <= value <= dimension.normal_max:
        status = "limited"
        reasons.append(f"{dimension.dimension_id}:outside_normal_domain")
    else:
        status = "passed"

    return DimensionAssessment(
        dimension_id=dimension.dimension_id,
        unit=dimension.unit,
        representation=dimension.representation,
        requested=requested,
        reference_value=dimension.reference_value,
        value=value,
        invalid_value=None,
        reference_distance=reference_distance,
        excess_distance=excess_distance,
        status=status,
        reasons=tuple(reasons),
    )


def assess_applicability(
    dimensions: Sequence[DomainDimension],
    overrides: Mapping[str, object] | None = None,
    *,
    abnormal_verification: bool = False,
) -> ApplicabilityAssessment:
    """Merge partial overrides over references and return a pre-solver decision."""

    if not isinstance(abnormal_verification, bool):
        raise TypeError("abnormal_verification must be boolean")
    ordered = tuple(sorted(dimensions, key=lambda item: item.dimension_id))
    if not ordered or any(not isinstance(item, DomainDimension) for item in ordered):
        raise TypeError("dimensions must be a non-empty sequence of DomainDimension values")
    identifiers = tuple(item.dimension_id for item in ordered)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("domain dimension ids must be unique")

    supplied = {} if overrides is None else dict(overrides)
    non_string_keys = tuple(
        sorted(
            f"<non-string:{type(key).__name__}>"
            for key in supplied
            if not isinstance(key, str)
        )
    )
    known_ids = set(identifiers)
    unknown = tuple(
        sorted(
            {
                *(str(key) for key in supplied if isinstance(key, str) and key not in known_ids),
                *non_string_keys,
            }
        )
    )
    assessments: list[DimensionAssessment] = []
    resolved_inputs: dict[str, float | str] = {}
    for dimension in ordered:
        requested = dimension.dimension_id in supplied
        raw_value = supplied.get(dimension.dimension_id, dimension.reference_value)
        assessment = _assess_dimension(
            dimension,
            raw_value,
            requested=requested,
        )
        assessments.append(assessment)
        resolved_inputs[dimension.dimension_id] = (
            assessment.value
            if assessment.value is not None
            else assessment.invalid_value or "invalid"
        )

    reasons = [f"unknown_input:{name}" for name in unknown]
    reasons.extend(reason for item in assessments for reason in item.reasons)
    statuses = {item.status for item in assessments}
    if unknown or "rejected" in statuses:
        status: DomainStatus = "rejected"
    elif "limited" in statuses:
        status = "limited"
    else:
        status = "passed"
    if abnormal_verification and status != "rejected":
        status = "limited"
        reasons.append("abnormal_verification_mode")

    reference_distances = tuple(
        item.reference_distance
        for item in assessments
        if item.reference_distance is not None
    )
    excess_distances = tuple(
        item.excess_distance for item in assessments if item.excess_distance is not None
    )
    maximum_reference_distance = (
        max(reference_distances) if reference_distances else None
    )
    maximum_excess_distance = max(excess_distances) if excess_distances else None

    fingerprint_payload: dict[str, object] = {
        "domain_dimensions": [item.as_dict() for item in ordered],
        "resolved_inputs": resolved_inputs,
        "unknown_inputs": list(unknown),
        "abnormal_verification": abnormal_verification,
        "status": status,
        "reasons": reasons,
    }
    return ApplicabilityAssessment(
        status=status,
        abnormal_verification=abnormal_verification,
        dimensions=tuple(assessments),
        resolved_inputs=resolved_inputs,
        unknown_inputs=unknown,
        maximum_reference_distance=maximum_reference_distance,
        maximum_excess_distance=maximum_excess_distance,
        reasons=tuple(reasons),
        input_fingerprint=_canonical_fingerprint(fingerprint_payload),
    )
