"""Strict, solver-neutral business intent contracts for RTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ._validation import (
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    identifier,
    integer,
    strict_keys,
)

OPTIMIZATION_INTENT_SCHEMA_ID = "optimization-intent"
OPTIMIZATION_INTENT_SCHEMA_VERSION = "1.0.0"

type ObjectiveSense = Literal["minimize", "maximize"]


def _identifiers(value: object, *, context: str, require_non_empty: bool) -> tuple[str, ...]:
    values = tuple(
        identifier(item, context=f"{context}[{index}]")
        for index, item in enumerate(as_sequence(value, context=context))
    )
    if require_non_empty and not values:
        raise ValueError(f"{context} must be non-empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must be unique")
    return values


@dataclass(frozen=True)
class ObjectiveRequest:
    metric_id: str
    sense: ObjectiveSense
    priority: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", identifier(self.metric_id, context="metric_id"))
        if self.sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        object.__setattr__(
            self,
            "priority",
            integer(self.priority, context="priority", minimum=1),
        )

    @classmethod
    def from_mapping(cls, value: object) -> ObjectiveRequest:
        raw = as_mapping(value, context="objective")
        strict_keys(
            raw,
            required={"metric_id", "sense", "priority"},
            context="objective",
        )
        sense = raw["sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        return cls(
            metric_id=identifier(raw["metric_id"], context="metric_id"),
            sense=sense,
            priority=integer(raw["priority"], context="priority", minimum=1),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class PreferenceRequest:
    method: str
    objective_order: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", identifier(self.method, context="preference method"))
        object.__setattr__(
            self,
            "objective_order",
            _identifiers(
                self.objective_order,
                context="preference objective_order",
                require_non_empty=True,
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> PreferenceRequest:
        raw = as_mapping(value, context="preference")
        strict_keys(
            raw,
            required={"method", "objective_order"},
            context="preference",
        )
        return cls(
            method=identifier(raw["method"], context="preference method"),
            objective_order=_identifiers(
                raw["objective_order"],
                context="preference objective_order",
                require_non_empty=True,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {"method": self.method, "objective_order": list(self.objective_order)}


@dataclass(frozen=True)
class ResultRequest:
    output_kind: str
    include_alternatives: bool
    max_candidates: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "output_kind",
            identifier(self.output_kind, context="result output_kind"),
        )
        object.__setattr__(
            self,
            "include_alternatives",
            boolean(self.include_alternatives, context="include_alternatives"),
        )
        object.__setattr__(
            self,
            "max_candidates",
            integer(self.max_candidates, context="max_candidates", minimum=1),
        )
        if not self.include_alternatives and self.max_candidates != 1:
            raise ValueError("max_candidates must be 1 when alternatives are not requested")

    @classmethod
    def from_mapping(cls, value: object) -> ResultRequest:
        raw = as_mapping(value, context="result_request")
        strict_keys(
            raw,
            required={"output_kind", "include_alternatives", "max_candidates"},
            context="result_request",
        )
        return cls(
            output_kind=identifier(raw["output_kind"], context="result output_kind"),
            include_alternatives=boolean(
                raw["include_alternatives"], context="include_alternatives"
            ),
            max_candidates=integer(raw["max_candidates"], context="max_candidates", minimum=1),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "output_kind": self.output_kind,
            "include_alternatives": self.include_alternatives,
            "max_candidates": self.max_candidates,
        }


@dataclass(frozen=True)
class OptimizationIntent:
    """Business intent only; deliberately excludes context, profiles, and algorithms."""

    schema_id: str
    schema_version: str
    intent_id: str
    objectives: tuple[ObjectiveRequest, ...]
    decision_variables: tuple[str, ...]
    constraints: tuple[str, ...]
    preference: PreferenceRequest
    result_request: ResultRequest
    ambiguities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_id != OPTIMIZATION_INTENT_SCHEMA_ID:
            raise ValueError("schema_id differs from the optimization intent contract")
        if self.schema_version != OPTIMIZATION_INTENT_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the optimization intent contract")
        object.__setattr__(self, "intent_id", identifier(self.intent_id, context="intent_id"))

        objectives = tuple(self.objectives)
        if not objectives or any(not isinstance(item, ObjectiveRequest) for item in objectives):
            raise TypeError("objectives must contain at least one ObjectiveRequest")
        metric_ids = tuple(item.metric_id for item in objectives)
        priorities = tuple(item.priority for item in objectives)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("objective metric ids must be unique")
        if priorities != tuple(range(1, len(objectives) + 1)):
            raise ValueError("objective priorities must be contiguous and ordered")
        object.__setattr__(self, "objectives", objectives)

        object.__setattr__(
            self,
            "decision_variables",
            _identifiers(
                self.decision_variables,
                context="decision_variables",
                require_non_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "constraints",
            _identifiers(self.constraints, context="constraints", require_non_empty=False),
        )
        if not isinstance(self.preference, PreferenceRequest):
            raise TypeError("preference must be a PreferenceRequest")
        if self.preference.objective_order != metric_ids:
            raise ValueError("preference objective_order must follow objective priorities")
        if not isinstance(self.result_request, ResultRequest):
            raise TypeError("result_request must be a ResultRequest")
        object.__setattr__(
            self,
            "ambiguities",
            _identifiers(self.ambiguities, context="ambiguities", require_non_empty=False),
        )

    @classmethod
    def from_mapping(cls, value: object) -> OptimizationIntent:
        raw = as_mapping(value, context="optimization intent")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "intent_id",
                "objectives",
                "decision_variables",
                "constraints",
                "preference",
                "result_request",
                "ambiguities",
            },
            context="optimization intent",
        )
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            intent_id=identifier(raw["intent_id"], context="intent_id"),
            objectives=tuple(
                ObjectiveRequest.from_mapping(item)
                for item in as_sequence(raw["objectives"], context="objectives")
            ),
            decision_variables=_identifiers(
                raw["decision_variables"],
                context="decision_variables",
                require_non_empty=True,
            ),
            constraints=_identifiers(
                raw["constraints"], context="constraints", require_non_empty=False
            ),
            preference=PreferenceRequest.from_mapping(raw["preference"]),
            result_request=ResultRequest.from_mapping(raw["result_request"]),
            ambiguities=_identifiers(
                raw["ambiguities"], context="ambiguities", require_non_empty=False
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "objectives": [item.as_dict() for item in self.objectives],
            "decision_variables": list(self.decision_variables),
            "constraints": list(self.constraints),
            "preference": self.preference.as_dict(),
            "result_request": self.result_request.as_dict(),
            "ambiguities": list(self.ambiguities),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())
