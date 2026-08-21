"""Strict V2 domain-intent and external request boundary models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..contracts import CLAIM_SCOPE, RTO_V2_SCHEMA_VERSION, ContractRef
from ..contracts.common import (
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    digest,
    identifier,
    integer,
    strict_keys,
    text,
)
from .models import CoveragePolicy, ExternalOperatingContextInputV1

DomainSourceTypeV2 = Literal["human", "domain-model", "preset"]
ObjectiveSenseInputV2 = Literal["minimize", "maximize"]


@dataclass(frozen=True)
class DomainIntentSourceV2:
    source_type: DomainSourceTypeV2
    producer_id: str
    producer_version: str
    correlation_id: str

    def __post_init__(self) -> None:
        if self.source_type not in {"human", "domain-model", "preset"}:
            raise ValueError("unsupported domain intent source_type")
        for name in ("producer_id", "producer_version", "correlation_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))

    @classmethod
    def from_mapping(cls, value: object) -> DomainIntentSourceV2:
        raw = as_mapping(value, context="domain intent source")
        strict_keys(
            raw,
            required={"source_type", "producer_id", "producer_version", "correlation_id"},
            context="domain intent source",
        )
        source_type = raw["source_type"]
        if source_type not in {"human", "domain-model", "preset"}:
            raise ValueError("unsupported domain intent source_type")
        return cls(
            source_type=source_type,
            producer_id=identifier(raw["producer_id"], context="producer_id"),
            producer_version=identifier(raw["producer_version"], context="producer_version"),
            correlation_id=identifier(raw["correlation_id"], context="correlation_id"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source_type": self.source_type,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class DomainObjectiveRequestV2:
    metric_id: str
    sense: ObjectiveSenseInputV2
    priority_tier: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", identifier(self.metric_id, context="metric_id"))
        if self.sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        object.__setattr__(
            self,
            "priority_tier",
            integer(self.priority_tier, context="priority_tier", minimum=1),
        )

    @classmethod
    def from_mapping(cls, value: object) -> DomainObjectiveRequestV2:
        raw = as_mapping(value, context="domain objective")
        strict_keys(
            raw,
            required={"metric_id", "sense", "priority_tier"},
            context="domain objective",
        )
        sense = raw["sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        return cls(
            metric_id=identifier(raw["metric_id"], context="metric_id"),
            sense=sense,
            priority_tier=integer(raw["priority_tier"], context="priority_tier", minimum=1),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "priority_tier": self.priority_tier,
        }


@dataclass(frozen=True)
class DomainSelectionRequestV2:
    selection_profile_id: str
    return_pareto_front: bool
    max_returned_candidates: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selection_profile_id",
            identifier(self.selection_profile_id, context="selection_profile_id"),
        )
        if not isinstance(self.return_pareto_front, bool):
            raise TypeError("return_pareto_front must be boolean")
        object.__setattr__(
            self,
            "max_returned_candidates",
            integer(
                self.max_returned_candidates,
                context="max_returned_candidates",
                minimum=1,
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> DomainSelectionRequestV2:
        raw = as_mapping(value, context="domain selection")
        strict_keys(
            raw,
            required={
                "selection_profile_id",
                "return_pareto_front",
                "max_returned_candidates",
            },
            context="domain selection",
        )
        return cls(
            selection_profile_id=identifier(
                raw["selection_profile_id"], context="selection_profile_id"
            ),
            return_pareto_front=boolean(raw["return_pareto_front"], context="return_pareto_front"),
            max_returned_candidates=integer(
                raw["max_returned_candidates"],
                context="max_returned_candidates",
                minimum=1,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "selection_profile_id": self.selection_profile_id,
            "return_pareto_front": self.return_pareto_front,
            "max_returned_candidates": self.max_returned_candidates,
        }


@dataclass(frozen=True)
class DomainOptimizationIntentV2:
    schema_version: str
    intent_version: str
    intent_id: str
    source: DomainIntentSourceV2
    original_text: str
    objective_profile_id: str
    objectives: tuple[DomainObjectiveRequestV2, ...]
    selection: DomainSelectionRequestV2
    decision_profile_id: str
    business_constraint_profile_id: str
    requested_output: str
    context_policy: str
    assumptions: tuple[str, ...]
    ambiguities: tuple[str, ...]
    rationale_summary: str
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RTO_V2_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the RTO V2 contract")
        if self.intent_version != "domain-optimization-intent-v2":
            raise ValueError("unsupported domain intent_version")
        if self.claim_scope != CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        for name in (
            "intent_id",
            "objective_profile_id",
            "decision_profile_id",
            "business_constraint_profile_id",
            "requested_output",
            "context_policy",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if not isinstance(self.source, DomainIntentSourceV2):
            raise TypeError("source must be DomainIntentSourceV2")
        object.__setattr__(self, "original_text", text(self.original_text, context="original_text"))
        object.__setattr__(
            self, "rationale_summary", text(self.rationale_summary, context="rationale_summary")
        )
        objectives = tuple(self.objectives)
        ids = tuple(item.metric_id for item in objectives)
        tiers = tuple(item.priority_tier for item in objectives)
        if not objectives or len(ids) != len(set(ids)):
            raise ValueError("domain objectives must be non-empty and unique")
        if tiers != tuple(range(1, len(tiers) + 1)):
            raise ValueError("domain objective priority_tier values must be contiguous")
        object.__setattr__(self, "objectives", objectives)
        if not isinstance(self.selection, DomainSelectionRequestV2):
            raise TypeError("selection must be DomainSelectionRequestV2")
        assumptions = tuple(identifier(item, context="assumption") for item in self.assumptions)
        ambiguities = tuple(identifier(item, context="ambiguity") for item in self.ambiguities)
        if len(assumptions) != len(set(assumptions)):
            raise ValueError("assumptions must be unique")
        if len(ambiguities) != len(set(ambiguities)):
            raise ValueError("ambiguities must be unique")
        object.__setattr__(self, "assumptions", assumptions)
        object.__setattr__(self, "ambiguities", ambiguities)

    @classmethod
    def from_mapping(cls, value: object) -> DomainOptimizationIntentV2:
        raw = as_mapping(value, context="domain optimization intent")
        required = {
            "schema_version",
            "intent_version",
            "intent_id",
            "source",
            "original_text",
            "objective_profile_id",
            "objectives",
            "selection",
            "decision_profile_id",
            "business_constraint_profile_id",
            "requested_output",
            "context_policy",
            "assumptions",
            "ambiguities",
            "rationale_summary",
            "claim_scope",
        }
        strict_keys(raw, required=required, context="domain optimization intent")
        return cls(
            schema_version=text(raw["schema_version"], context="schema_version"),
            intent_version=identifier(raw["intent_version"], context="intent_version"),
            intent_id=identifier(raw["intent_id"], context="intent_id"),
            source=DomainIntentSourceV2.from_mapping(raw["source"]),
            original_text=text(raw["original_text"], context="original_text"),
            objective_profile_id=identifier(
                raw["objective_profile_id"], context="objective_profile_id"
            ),
            objectives=tuple(
                DomainObjectiveRequestV2.from_mapping(item)
                for item in as_sequence(raw["objectives"], context="objectives")
            ),
            selection=DomainSelectionRequestV2.from_mapping(raw["selection"]),
            decision_profile_id=identifier(
                raw["decision_profile_id"], context="decision_profile_id"
            ),
            business_constraint_profile_id=identifier(
                raw["business_constraint_profile_id"],
                context="business_constraint_profile_id",
            ),
            requested_output=identifier(raw["requested_output"], context="requested_output"),
            context_policy=identifier(raw["context_policy"], context="context_policy"),
            assumptions=tuple(
                identifier(item, context="assumption")
                for item in as_sequence(raw["assumptions"], context="assumptions")
            ),
            ambiguities=tuple(
                identifier(item, context="ambiguity")
                for item in as_sequence(raw["ambiguities"], context="ambiguities")
            ),
            rationale_summary=text(raw["rationale_summary"], context="rationale_summary"),
            claim_scope=text(raw["claim_scope"], context="claim_scope"),
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_version": self.intent_version,
            "objective_profile_id": self.objective_profile_id,
            "objectives": [item.as_dict() for item in self.objectives],
            "selection": self.selection.as_dict(),
            "decision_profile_id": self.decision_profile_id,
            "business_constraint_profile_id": self.business_constraint_profile_id,
            "requested_output": self.requested_output,
            "context_policy": self.context_policy,
            "claim_scope": self.claim_scope,
        }

    @property
    def semantic_fingerprint(self) -> str:
        return canonical_fingerprint(self.semantic_payload())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_version": self.intent_version,
            "intent_id": self.intent_id,
            "source": self.source.as_dict(),
            "original_text": self.original_text,
            "objective_profile_id": self.objective_profile_id,
            "objectives": [item.as_dict() for item in self.objectives],
            "selection": self.selection.as_dict(),
            "decision_profile_id": self.decision_profile_id,
            "business_constraint_profile_id": self.business_constraint_profile_id,
            "requested_output": self.requested_output,
            "context_policy": self.context_policy,
            "assumptions": list(self.assumptions),
            "ambiguities": list(self.ambiguities),
            "rationale_summary": self.rationale_summary,
            "claim_scope": self.claim_scope,
        }

    @property
    def audit_fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class ExternalOptimizationRequestV2:
    schema_version: str
    request_version: str
    request_id: str
    operating_context: ExternalOperatingContextInputV1
    optimization_intent: DomainOptimizationIntentV2
    coverage_policy: CoveragePolicy
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RTO_V2_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the RTO V2 contract")
        if self.request_version != "external-optimization-request-v2":
            raise ValueError("unsupported external request_version")
        object.__setattr__(self, "request_id", identifier(self.request_id, context="request_id"))
        if not isinstance(self.operating_context, ExternalOperatingContextInputV1):
            raise TypeError("operating_context must be ExternalOperatingContextInputV1")
        if not isinstance(self.optimization_intent, DomainOptimizationIntentV2):
            raise TypeError("optimization_intent must be DomainOptimizationIntentV2")
        if self.coverage_policy not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported coverage_policy")
        if self.claim_scope != CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")

    @classmethod
    def from_mapping(cls, value: object) -> ExternalOptimizationRequestV2:
        raw = as_mapping(value, context="external optimization request V2")
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
            context="external optimization request V2",
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
            optimization_intent=DomainOptimizationIntentV2.from_mapping(raw["optimization_intent"]),
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


@dataclass(frozen=True)
class IntentValidationIssueV2:
    code: str
    json_pointer: str
    message: str
    supported_values: tuple[str, ...]
    retryable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", identifier(self.code, context="validation code"))
        if not isinstance(self.json_pointer, str) or not self.json_pointer.startswith("/"):
            raise ValueError("json_pointer must start with /")
        object.__setattr__(self, "message", text(self.message, context="validation message"))
        supported = tuple(
            identifier(item, context="supported value") for item in self.supported_values
        )
        if len(supported) != len(set(supported)):
            raise ValueError("supported_values must be unique")
        object.__setattr__(self, "supported_values", supported)
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "json_pointer": self.json_pointer,
            "message": self.message,
            "supported_values": list(self.supported_values),
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class IntentValidationResultV2:
    valid: bool
    status: Literal["valid", "invalid", "needs_clarification"]
    audit_fingerprint: str | None
    semantic_fingerprint: str | None
    issues: tuple[IntentValidationIssueV2, ...]
    solver_called: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.valid, bool):
            raise TypeError("valid must be boolean")
        if self.status not in {"valid", "invalid", "needs_clarification"}:
            raise ValueError("unsupported validation status")
        for name in ("audit_fingerprint", "semantic_fingerprint"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, digest(value, context=name))
        issues = tuple(self.issues)
        if any(not isinstance(item, IntentValidationIssueV2) for item in issues):
            raise TypeError("issues must contain IntentValidationIssueV2 values")
        object.__setattr__(self, "issues", issues)
        if self.valid != (self.status == "valid"):
            raise ValueError("valid flag differs from status")
        if self.valid and issues:
            raise ValueError("valid result must not contain issues")
        if not self.valid and not issues:
            raise ValueError("invalid result requires at least one issue")
        if not isinstance(self.solver_called, bool) or self.solver_called:
            raise ValueError("intent validation must not call a solver")

    def as_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "status": self.status,
            "audit_fingerprint": self.audit_fingerprint,
            "semantic_fingerprint": self.semantic_fingerprint,
            "issues": [item.as_dict() for item in self.issues],
            "solver_called": self.solver_called,
        }


@dataclass(frozen=True)
class RtoCapabilityManifestV2:
    schema_version: str
    manifest_version: str
    objective_catalog_ref: ContractRef
    preference_catalog_ref: ContractRef
    publishability_catalog_ref: ContractRef
    supported_request_versions: tuple[str, ...]
    objective_profiles: tuple[str, ...]
    objectives: tuple[Mapping[str, str], ...]
    selection_profiles: tuple[str, ...]
    decision_profiles: tuple[str, ...]
    business_constraint_profiles: tuple[str, ...]
    requested_outputs: tuple[str, ...]
    context_policies: tuple[str, ...]
    allowed_assumptions: tuple[str, ...]
    maximum_objectives: int
    maximum_returned_candidates: int
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RTO_V2_SCHEMA_VERSION:
            raise ValueError("capability schema_version differs from V2")
        if self.manifest_version != "rto-capability-manifest-v2":
            raise ValueError("unsupported capability manifest_version")
        if self.claim_scope != CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        for name in (
            "objective_catalog_ref",
            "preference_catalog_ref",
            "publishability_catalog_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        sequence_fields = (
            "supported_request_versions",
            "objective_profiles",
            "selection_profiles",
            "decision_profiles",
            "business_constraint_profiles",
            "requested_outputs",
            "context_policies",
            "allowed_assumptions",
        )
        for name in sequence_fields:
            values = tuple(identifier(item, context=name) for item in getattr(self, name))
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{name} must be non-empty and unique")
            object.__setattr__(self, name, values)
        objective_rows = tuple(self.objectives)
        for row in objective_rows:
            if set(row) != {"metric_id", "sense", "unit"}:
                raise ValueError("capability objective fields differ")
            identifier(row["metric_id"], context="capability metric_id")
            identifier(row["sense"], context="capability sense")
            text(row["unit"], context="capability unit")
        object.__setattr__(self, "objectives", objective_rows)
        object.__setattr__(
            self,
            "maximum_objectives",
            integer(self.maximum_objectives, context="maximum_objectives", minimum=1),
        )
        object.__setattr__(
            self,
            "maximum_returned_candidates",
            integer(
                self.maximum_returned_candidates,
                context="maximum_returned_candidates",
                minimum=1,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_version": self.manifest_version,
            "objective_catalog_ref": self.objective_catalog_ref.as_dict(),
            "preference_catalog_ref": self.preference_catalog_ref.as_dict(),
            "publishability_catalog_ref": self.publishability_catalog_ref.as_dict(),
            "supported_request_versions": list(self.supported_request_versions),
            "objective_profiles": list(self.objective_profiles),
            "objectives": [dict(item) for item in self.objectives],
            "selection_profiles": list(self.selection_profiles),
            "decision_profiles": list(self.decision_profiles),
            "business_constraint_profiles": list(self.business_constraint_profiles),
            "requested_outputs": list(self.requested_outputs),
            "context_policies": list(self.context_policies),
            "allowed_assumptions": list(self.allowed_assumptions),
            "maximum_objectives": self.maximum_objectives,
            "maximum_returned_candidates": self.maximum_returned_candidates,
            "claim_scope": self.claim_scope,
            "solver_called": False,
        }
