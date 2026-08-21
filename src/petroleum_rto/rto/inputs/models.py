"""Strict external JSON contract for human and future domain-model requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..contracts import CLAIM_SCOPE, RTO_SCHEMA_VERSION, ContractRef
from ..contracts.common import (
    as_mapping,
    canonical_fingerprint,
    finite,
    identifier,
    strict_keys,
    text,
)

ExternalSourceType = Literal["human", "domain-model"]
CoveragePolicy = Literal["point", "sampled-anchors"]


def _timestamp(value: object, *, context: str) -> str:
    raw = text(value, context=context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include a UTC offset")
    return raw


@dataclass(frozen=True)
class ExternalOperatingContextInputV1:
    """Allowed operating-context values supplied outside the RTO package."""

    base_context_ref: ContractRef
    context_id: str
    feed_mass_flow_t_h: float
    data_timestamp: str
    data_quality: str

    def __post_init__(self) -> None:
        if not isinstance(self.base_context_ref, ContractRef):
            raise TypeError("base_context_ref must be a ContractRef")
        object.__setattr__(self, "context_id", identifier(self.context_id, context="context_id"))
        object.__setattr__(
            self,
            "feed_mass_flow_t_h",
            finite(self.feed_mass_flow_t_h, context="feed_mass_flow_t_h"),
        )
        if self.feed_mass_flow_t_h <= 0.0:
            raise ValueError("feed_mass_flow_t_h must be positive")
        object.__setattr__(
            self,
            "data_timestamp",
            _timestamp(self.data_timestamp, context="data_timestamp"),
        )
        object.__setattr__(
            self,
            "data_quality",
            identifier(self.data_quality, context="data_quality"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> ExternalOperatingContextInputV1:
        raw = as_mapping(value, context="external operating_context")
        strict_keys(
            raw,
            required={
                "base_context_ref",
                "context_id",
                "feed_mass_flow_t_h",
                "data_timestamp",
                "data_quality",
            },
            context="external operating_context",
        )
        return cls(
            base_context_ref=ContractRef.from_mapping(
                as_mapping(raw["base_context_ref"], context="base_context_ref")
            ),
            context_id=identifier(raw["context_id"], context="context_id"),
            feed_mass_flow_t_h=finite(raw["feed_mass_flow_t_h"], context="feed_mass_flow_t_h"),
            data_timestamp=_timestamp(raw["data_timestamp"], context="data_timestamp"),
            data_quality=identifier(raw["data_quality"], context="data_quality"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "base_context_ref": self.base_context_ref.as_dict(),
            "context_id": self.context_id,
            "feed_mass_flow_t_h": self.feed_mass_flow_t_h,
            "data_timestamp": self.data_timestamp,
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True)
class ExternalOptimizationIntentInputV1:
    """Business intent emitted by a human form or a future domain model."""

    intent_id: str
    source_type: ExternalSourceType
    source_ref: str
    original_text: str
    objective_profile_id: str
    priority_profile_id: str
    decision_profile_id: str
    constraint_profile_id: str
    requested_output: str
    context_policy: str

    def __post_init__(self) -> None:
        for name in (
            "intent_id",
            "source_ref",
            "objective_profile_id",
            "priority_profile_id",
            "decision_profile_id",
            "constraint_profile_id",
            "requested_output",
            "context_policy",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.source_type not in {"human", "domain-model"}:
            raise ValueError("source_type must be human or domain-model")
        object.__setattr__(self, "original_text", text(self.original_text, context="original_text"))

    @classmethod
    def from_mapping(cls, value: object) -> ExternalOptimizationIntentInputV1:
        raw = as_mapping(value, context="external optimization_intent")
        strict_keys(
            raw,
            required={
                "intent_id",
                "source_type",
                "source_ref",
                "original_text",
                "objective_profile_id",
                "priority_profile_id",
                "decision_profile_id",
                "constraint_profile_id",
                "requested_output",
                "context_policy",
            },
            context="external optimization_intent",
        )
        source_type = raw["source_type"]
        if source_type not in {"human", "domain-model"}:
            raise ValueError("source_type must be human or domain-model")
        return cls(
            intent_id=identifier(raw["intent_id"], context="intent_id"),
            source_type=source_type,
            source_ref=identifier(raw["source_ref"], context="source_ref"),
            original_text=text(raw["original_text"], context="original_text"),
            objective_profile_id=identifier(
                raw["objective_profile_id"], context="objective_profile_id"
            ),
            priority_profile_id=identifier(
                raw["priority_profile_id"], context="priority_profile_id"
            ),
            decision_profile_id=identifier(
                raw["decision_profile_id"], context="decision_profile_id"
            ),
            constraint_profile_id=identifier(
                raw["constraint_profile_id"], context="constraint_profile_id"
            ),
            requested_output=identifier(raw["requested_output"], context="requested_output"),
            context_policy=identifier(raw["context_policy"], context="context_policy"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "original_text": self.original_text,
            "objective_profile_id": self.objective_profile_id,
            "priority_profile_id": self.priority_profile_id,
            "decision_profile_id": self.decision_profile_id,
            "constraint_profile_id": self.constraint_profile_id,
            "requested_output": self.requested_output,
            "context_policy": self.context_policy,
        }


@dataclass(frozen=True)
class ExternalOptimizationRequestV1:
    """One complete external request with no model-internal fields."""

    schema_version: str
    request_version: str
    request_id: str
    operating_context: ExternalOperatingContextInputV1
    optimization_intent: ExternalOptimizationIntentInputV1
    coverage_policy: CoveragePolicy
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RTO_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the RTO V1 contract")
        if self.request_version != "external-optimization-request-v1":
            raise ValueError("unsupported external request_version")
        object.__setattr__(self, "request_id", identifier(self.request_id, context="request_id"))
        if not isinstance(self.operating_context, ExternalOperatingContextInputV1):
            raise TypeError("operating_context must be ExternalOperatingContextInputV1")
        if not isinstance(self.optimization_intent, ExternalOptimizationIntentInputV1):
            raise TypeError("optimization_intent must be ExternalOptimizationIntentInputV1")
        if self.coverage_policy not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported coverage_policy")
        if self.claim_scope != CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")

    @classmethod
    def from_mapping(cls, value: object) -> ExternalOptimizationRequestV1:
        raw = as_mapping(value, context="external optimization request")
        strict_keys(
            raw,
            required={
                "schema_version",
                "request_version",
                "request_id",
                "operating_context",
                "optimization_intent",
                "coverage_policy",
                "claim_scope",
            },
            context="external optimization request",
        )
        coverage = raw["coverage_policy"]
        if coverage not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported coverage_policy")
        return cls(
            schema_version=text(raw["schema_version"], context="schema_version"),
            request_version=identifier(raw["request_version"], context="request_version"),
            request_id=identifier(raw["request_id"], context="request_id"),
            operating_context=ExternalOperatingContextInputV1.from_mapping(
                raw["operating_context"]
            ),
            optimization_intent=ExternalOptimizationIntentInputV1.from_mapping(
                raw["optimization_intent"]
            ),
            coverage_policy=coverage,
            claim_scope=text(raw["claim_scope"], context="claim_scope"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "request_id": self.request_id,
            "operating_context": self.operating_context.as_dict(),
            "optimization_intent": self.optimization_intent.as_dict(),
            "coverage_policy": self.coverage_policy,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.request_id, self.fingerprint)
