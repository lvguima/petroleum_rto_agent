"""Strict objective-count-neutral offline workflow artifact contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final, Literal

from ..capabilities.models import (
    CapabilityCatalog,
    ContextSchema,
    SystemPolicy,
    UnifiedCapabilityBundle,
)
from ..contracts.candidate import CandidateEvaluation, CandidateProposal
from ..contracts.common import (
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    digest,
    finite,
    identifier,
    integer,
    numeric_mapping,
    strict_keys,
    string_mapping,
    text,
)
from ..contracts.context import OperatingContext
from ..contracts.finalization import (
    FinalizationResult,
    PublishabilityAssessment,
)
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from ..contracts.reference import ContractRef
from ..contracts.solver_result import SolverResult
from ..solvers.models import SolverRoutingDecision

OFFLINE_WORKFLOW_SCHEMA_ID: Final[str] = "offline-rto-workflow"
OFFLINE_WORKFLOW_SCHEMA_VERSION: Final[str] = "1.0.0"
UNIFIED_MANIFEST_VERSION: Final[str] = "offline-rto-manifest-unified"
OfflineRunStatus = Literal["completed_draft", "completed_without_strategy", "failed"]
CoveragePolicy = Literal["point", "sampled-anchors"]


def _schema_claim(schema_id: str, schema_version: str, claim_scope: str) -> None:
    if schema_id != OFFLINE_WORKFLOW_SCHEMA_ID:
        raise ValueError("schema_id differs from the offline workflow contract")
    if schema_version != OFFLINE_WORKFLOW_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the offline workflow contract")
    if claim_scope != ENGINEERING_CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _timestamp(value: object, *, context: str) -> str:
    raw = text(value, context=context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include an explicit timezone")
    return raw


def _optional_ref(value: object, *, context: str) -> ContractRef | None:
    return None if value is None else ContractRef.from_mapping(as_mapping(value, context=context))


def _validate_identity(
    value: Mapping[str, object],
    *,
    ref: ContractRef,
    ref_field: str,
    fingerprint_field: str,
) -> None:
    supplied_ref = value.get(ref_field)
    if (
        supplied_ref is not None
        and ContractRef.from_mapping(as_mapping(supplied_ref, context=ref_field)) != ref
    ):
        raise ValueError(f"{ref_field} differs from artifact content")
    supplied_fingerprint = value.get(fingerprint_field)
    if (
        supplied_fingerprint is not None
        and digest(supplied_fingerprint, context=fingerprint_field) != ref.fingerprint
    ):
        raise ValueError(f"{fingerprint_field} differs from artifact content")


@dataclass(frozen=True)
class OfflineRtoRequest:
    """Trusted wrapper joining one business intent to one operating context."""

    schema_id: str
    schema_version: str
    request_version: str
    intent_ref: ContractRef
    context_ref: ContractRef
    capability_catalog_ref: ContractRef
    context_schema_ref: ContractRef
    system_policy_ref: ContractRef
    solver_policy_ref: ContractRef
    provider_id: str
    coverage_policy: CoveragePolicy
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "request_version", identifier(self.request_version, context="request_version")
        )
        for name in (
            "intent_ref",
            "context_ref",
            "capability_catalog_ref",
            "context_schema_ref",
            "system_policy_ref",
            "solver_policy_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        if self.coverage_policy not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported coverage policy")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "intent_ref": self.intent_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "capability_catalog_ref": self.capability_catalog_ref.as_dict(),
            "context_schema_ref": self.context_schema_ref.as_dict(),
            "system_policy_ref": self.system_policy_ref.as_dict(),
            "solver_policy_ref": self.solver_policy_ref.as_dict(),
            "provider_id": self.provider_id,
            "coverage_policy": self.coverage_policy,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def workflow_id(self) -> str:
        return f"offline-rto-{self.fingerprint[:16]}"

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.workflow_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "workflow_id": self.workflow_id,
            "request_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoRequest:
        ref_fields = {
            "intent_ref",
            "context_ref",
            "capability_catalog_ref",
            "context_schema_ref",
            "system_policy_ref",
            "solver_policy_ref",
        }
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "request_version",
                *ref_fields,
                "provider_id",
                "coverage_policy",
                "claim_scope",
            },
            optional={"workflow_id", "request_fingerprint"},
            context="offline RTO request",
        )
        coverage = value["coverage_policy"]
        if coverage not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported coverage policy")
        refs = {
            name: ContractRef.from_mapping(as_mapping(value[name], context=name))
            for name in ref_fields
        }
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            request_version=identifier(value["request_version"], context="request_version"),
            intent_ref=refs["intent_ref"],
            context_ref=refs["context_ref"],
            capability_catalog_ref=refs["capability_catalog_ref"],
            context_schema_ref=refs["context_schema_ref"],
            system_policy_ref=refs["system_policy_ref"],
            solver_policy_ref=refs["solver_policy_ref"],
            provider_id=identifier(value["provider_id"], context="provider_id"),
            coverage_policy=coverage,
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("workflow_id") not in {None, result.workflow_id}:
            raise ValueError("workflow_id differs from request content")
        supplied = value.get("request_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="request_fingerprint") != result.fingerprint
        ):
            raise ValueError("request_fingerprint differs from request content")
        return result


@dataclass(frozen=True)
class CapabilityBundleSnapshot:
    """Complete internal capability inputs required for portable strict replay."""

    schema_id: str
    schema_version: str
    snapshot_version: str
    bundle: UnifiedCapabilityBundle
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "snapshot_version",
            identifier(self.snapshot_version, context="snapshot_version"),
        )
        if not isinstance(self.bundle, UnifiedCapabilityBundle):
            raise TypeError("bundle must be UnifiedCapabilityBundle")
        if self.bundle.catalog.claim_scope != self.claim_scope:
            raise ValueError("capability bundle claim scope differs from its snapshot")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "snapshot_version": self.snapshot_version,
            "catalog": self.bundle.catalog.as_dict(),
            "context_schema": self.bundle.context_schema.as_dict(),
            "system_policy": self.bundle.system_policy.as_dict(),
            "bundle_fingerprint": self.bundle.fingerprint,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"capability-snapshot-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "snapshot_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CapabilityBundleSnapshot:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "snapshot_version",
                "catalog",
                "context_schema",
                "system_policy",
                "bundle_fingerprint",
                "claim_scope",
            },
            optional={"snapshot_ref"},
            context="capability bundle snapshot",
        )
        bundle = UnifiedCapabilityBundle(
            catalog=CapabilityCatalog.from_mapping(value["catalog"]),
            context_schema=ContextSchema.from_mapping(value["context_schema"]),
            system_policy=SystemPolicy.from_mapping(value["system_policy"]),
        )
        if digest(value["bundle_fingerprint"], context="bundle_fingerprint") != bundle.fingerprint:
            raise ValueError("bundle_fingerprint differs from nested capability objects")
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            snapshot_version=identifier(value["snapshot_version"], context="snapshot_version"),
            bundle=bundle,
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("snapshot_ref")
        if (
            supplied is not None
            and ContractRef.from_mapping(as_mapping(supplied, context="snapshot_ref")) != result.ref
        ):
            raise ValueError("snapshot_ref differs from capability content")
        return result


@dataclass(frozen=True)
class SolverExecutionArtifact:
    """Persist the solver result together with its referenced proposals and M2 evidence."""

    schema_id: str
    schema_version: str
    execution_version: str
    routing_fingerprint: str
    result: SolverResult
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "execution_version",
            identifier(self.execution_version, context="execution_version"),
        )
        object.__setattr__(
            self,
            "routing_fingerprint",
            digest(self.routing_fingerprint, context="routing_fingerprint"),
        )
        if not isinstance(self.result, SolverResult):
            raise TypeError("result must be SolverResult")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "execution_version": self.execution_version,
            "routing_fingerprint": self.routing_fingerprint,
            "solver_result": self.result.as_dict(),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"solver-execution-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "execution_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SolverExecutionArtifact:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "execution_version",
                "routing_fingerprint",
                "solver_result",
                "claim_scope",
            },
            optional={"execution_ref"},
            context="solver execution artifact",
        )
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            execution_version=identifier(value["execution_version"], context="execution_version"),
            routing_fingerprint=digest(value["routing_fingerprint"], context="routing_fingerprint"),
            result=SolverResult.from_mapping(
                as_mapping(value["solver_result"], context="solver_result")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("execution_ref")
        if (
            supplied is not None
            and ContractRef.from_mapping(as_mapping(supplied, context="execution_ref"))
            != result.ref
        ):
            raise ValueError("execution_ref differs from solver execution content")
        return result


@dataclass(frozen=True)
class DynamicVerificationArtifact:
    """Ordered M4 evaluations for exactly one static shortlist prefix."""

    schema_id: str
    schema_version: str
    verification_version: str
    problem_ref: ContractRef
    static_selection_ref: ContractRef
    evaluations: tuple[CandidateEvaluation, ...]
    applicable: bool
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "verification_version",
            identifier(self.verification_version, context="verification_version"),
        )
        for name in ("problem_ref", "static_selection_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        evaluations = tuple(self.evaluations)
        if any(not isinstance(item, CandidateEvaluation) for item in evaluations):
            raise TypeError("dynamic evaluations must contain CandidateEvaluation")
        if any(item.stage != "M4" or item.problem_ref != self.problem_ref for item in evaluations):
            raise ValueError("dynamic evaluations differ from the verification problem or stage")
        refs = tuple(item.proposal_ref for item in evaluations)
        if len(refs) != len(set(refs)):
            raise ValueError("dynamic evaluations must contain unique proposals")
        object.__setattr__(self, "evaluations", evaluations)
        object.__setattr__(self, "applicable", boolean(self.applicable, context="applicable"))
        if self.applicable != bool(evaluations):
            raise ValueError("applicable must equal whether M4 evaluations exist")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "verification_version": self.verification_version,
            "problem_ref": self.problem_ref.as_dict(),
            "static_selection_ref": self.static_selection_ref.as_dict(),
            "evaluations": [item.as_dict() for item in self.evaluations],
            "applicable": self.applicable,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"dynamic-verification-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "verification_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DynamicVerificationArtifact:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "verification_version",
                "problem_ref",
                "static_selection_ref",
                "evaluations",
                "applicable",
                "termination_reason",
                "claim_scope",
            },
            optional={"verification_ref"},
            context="dynamic verification artifact",
        )
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            verification_version=identifier(
                value["verification_version"], context="verification_version"
            ),
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            static_selection_ref=ContractRef.from_mapping(
                as_mapping(value["static_selection_ref"], context="static_selection_ref")
            ),
            evaluations=tuple(
                CandidateEvaluation.from_mapping(as_mapping(item, context="dynamic evaluation"))
                for item in as_sequence(value["evaluations"], context="evaluations")
            ),
            applicable=boolean(value["applicable"], context="applicable"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("verification_ref")
        if (
            supplied is not None
            and ContractRef.from_mapping(as_mapping(supplied, context="verification_ref"))
            != result.ref
        ):
            raise ValueError("verification_ref differs from dynamic verification content")
        return result


@dataclass(frozen=True)
class FinalizationArtifact:
    """Single-file commit of optional publishability and the terminal selector result."""

    schema_id: str
    schema_version: str
    artifact_version: str
    static_selection_ref: ContractRef
    publishability: PublishabilityAssessment | None
    result: FinalizationResult
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "artifact_version",
            identifier(self.artifact_version, context="artifact_version"),
        )
        if not isinstance(self.static_selection_ref, ContractRef):
            raise TypeError("static_selection_ref must be ContractRef")
        if self.publishability is not None and not isinstance(
            self.publishability, PublishabilityAssessment
        ):
            raise TypeError("publishability must be PublishabilityAssessment or None")
        if not isinstance(self.result, FinalizationResult):
            raise TypeError("result must be FinalizationResult")
        if self.result.static_selection_ref != self.static_selection_ref:
            raise ValueError("finalization result references another static selection")
        expected = None if self.publishability is None else self.publishability.ref
        if self.result.publishability_assessment_ref != expected:
            raise ValueError("finalization result references another publishability assessment")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "artifact_version": self.artifact_version,
            "static_selection_ref": self.static_selection_ref.as_dict(),
            "publishability": (
                None if self.publishability is None else self.publishability.as_dict()
            ),
            "result": self.result.as_dict(),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"finalization-artifact-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "artifact_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FinalizationArtifact:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "artifact_version",
                "static_selection_ref",
                "publishability",
                "result",
                "claim_scope",
            },
            optional={"artifact_ref"},
            context="finalization artifact",
        )
        publishability_raw = value["publishability"]
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            artifact_version=identifier(value["artifact_version"], context="artifact_version"),
            static_selection_ref=ContractRef.from_mapping(
                as_mapping(value["static_selection_ref"], context="static_selection_ref")
            ),
            publishability=(
                None
                if publishability_raw is None
                else PublishabilityAssessment.from_mapping(
                    as_mapping(publishability_raw, context="publishability")
                )
            ),
            result=FinalizationResult.from_mapping(as_mapping(value["result"], context="result")),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("artifact_ref")
        if (
            supplied is not None
            and ContractRef.from_mapping(as_mapping(supplied, context="artifact_ref")) != result.ref
        ):
            raise ValueError("artifact_ref differs from finalization content")
        return result


@dataclass(frozen=True)
class AnchorAttempt:
    """One selected absolute action evaluated at one trusted feed anchor."""

    ratio: float
    context: OperatingContext
    problem: OptimizationProblem
    proposal: CandidateProposal
    static_evaluation: CandidateEvaluation
    dynamic_evaluation: CandidateEvaluation | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ratio", finite(self.ratio, context="ratio"))
        if self.ratio <= 0.0:
            raise ValueError("anchor ratio must be positive")
        if self.problem.context_ref != self.context.ref:
            raise ValueError("anchor problem references another context")
        if (
            self.proposal.problem_ref != self.problem.ref
            or self.proposal.context_ref != self.context.ref
        ):
            raise ValueError("anchor proposal references another problem or context")
        if (
            self.static_evaluation.stage != self.problem.evaluation_plan.static_stage
            or self.static_evaluation.problem_ref != self.problem.ref
            or self.static_evaluation.context_ref != self.context.ref
            or self.static_evaluation.proposal_ref != self.proposal.ref
        ):
            raise ValueError("anchor static evaluation differs from proposal")
        if self.dynamic_evaluation is not None and (
            self.dynamic_evaluation.stage != self.problem.evaluation_plan.dynamic_stage
            or self.dynamic_evaluation.problem_ref != self.problem.ref
            or self.dynamic_evaluation.context_ref != self.context.ref
            or self.dynamic_evaluation.proposal_ref != self.proposal.ref
        ):
            raise ValueError("anchor dynamic evaluation differs from proposal")
        if self.static_evaluation.status != "feasible" and self.dynamic_evaluation is not None:
            raise ValueError("non-feasible M2 anchor must not execute M4")

    @property
    def passed(self) -> bool:
        return (
            self.static_evaluation.status == "feasible"
            and self.dynamic_evaluation is not None
            and self.dynamic_evaluation.status == "feasible"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ratio": self.ratio,
            "passed": self.passed,
            "context": self.context.as_dict(),
            "problem": self.problem.as_dict(),
            "proposal": self.proposal.as_dict(),
            "static_evaluation": self.static_evaluation.as_dict(),
            "dynamic_evaluation": (
                None if self.dynamic_evaluation is None else self.dynamic_evaluation.as_dict()
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AnchorAttempt:
        strict_keys(
            value,
            required={
                "ratio",
                "passed",
                "context",
                "problem",
                "proposal",
                "static_evaluation",
                "dynamic_evaluation",
            },
            context="anchor attempt",
        )
        dynamic_raw = value["dynamic_evaluation"]
        result = cls(
            ratio=finite(value["ratio"], context="ratio"),
            context=OperatingContext.from_mapping(as_mapping(value["context"], context="context")),
            problem=OptimizationProblem.from_mapping(
                as_mapping(value["problem"], context="problem")
            ),
            proposal=CandidateProposal.from_mapping(
                as_mapping(value["proposal"], context="proposal")
            ),
            static_evaluation=CandidateEvaluation.from_mapping(
                as_mapping(value["static_evaluation"], context="static_evaluation")
            ),
            dynamic_evaluation=(
                None
                if dynamic_raw is None
                else CandidateEvaluation.from_mapping(
                    as_mapping(dynamic_raw, context="dynamic_evaluation")
                )
            ),
        )
        if boolean(value["passed"], context="passed") != result.passed:
            raise ValueError("anchor passed flag differs from its evaluations")
        return result


@dataclass(frozen=True)
class AnchorValidationResult:
    """Discrete sampled coverage; it never claims a continuous operating interval."""

    schema_id: str
    schema_version: str
    validation_version: str
    central_problem_ref: ContractRef
    selected_action: Mapping[str, float]
    attempts: tuple[AnchorAttempt, ...]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "validation_version",
            identifier(self.validation_version, context="validation_version"),
        )
        if not isinstance(self.central_problem_ref, ContractRef):
            raise TypeError("central_problem_ref must be ContractRef")
        action = numeric_mapping(self.selected_action, context="selected_action")
        if not action:
            raise ValueError("selected_action must be non-empty")
        object.__setattr__(self, "selected_action", action)
        attempts = tuple(self.attempts)
        ratios = tuple(item.ratio for item in attempts)
        if not attempts or ratios != tuple(sorted(set(ratios))):
            raise ValueError("anchor attempts must be non-empty, unique and sorted")
        if any(dict(item.proposal.decision_values) != dict(action) for item in attempts):
            raise ValueError("anchor attempts must evaluate the selected action")
        if any(item.problem.intent_ref != attempts[0].problem.intent_ref for item in attempts):
            raise ValueError("anchor attempts must retain one business intent")
        object.__setattr__(self, "attempts", attempts)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.attempts)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "validation_version": self.validation_version,
            "central_problem_ref": self.central_problem_ref.as_dict(),
            "selected_action": dict(self.selected_action),
            "attempts": [item.as_dict() for item in self.attempts],
            "passed": self.passed,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"anchor-validation-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "validation_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AnchorValidationResult:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "validation_version",
                "central_problem_ref",
                "selected_action",
                "attempts",
                "passed",
                "claim_scope",
            },
            optional={"validation_ref"},
            context="anchor validation",
        )
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            validation_version=identifier(
                value["validation_version"], context="validation_version"
            ),
            central_problem_ref=ContractRef.from_mapping(
                as_mapping(value["central_problem_ref"], context="central_problem_ref")
            ),
            selected_action=numeric_mapping(value["selected_action"], context="selected_action"),
            attempts=tuple(
                AnchorAttempt.from_mapping(as_mapping(item, context="anchor attempt"))
                for item in as_sequence(value["attempts"], context="attempts")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if boolean(value["passed"], context="passed") != result.passed:
            raise ValueError("anchor validation passed flag differs from its attempts")
        supplied = value.get("validation_ref")
        if (
            supplied is not None
            and ContractRef.from_mapping(as_mapping(supplied, context="validation_ref"))
            != result.ref
        ):
            raise ValueError("validation_ref differs from anchor validation content")
        return result


@dataclass(frozen=True)
class OfflineRtoResult:
    """Terminal workflow summary; optimization outcomes remain in finalization.json."""

    schema_id: str
    schema_version: str
    result_version: str
    status: OfflineRunStatus
    request_ref: ContractRef
    problem_ref: ContractRef
    routing_ref: ContractRef
    solver_execution_ref: ContractRef
    static_selection_ref: ContractRef
    dynamic_verification_ref: ContractRef
    finalization_ref: ContractRef
    anchor_validation_ref: ContractRef | None
    strategy_ref: ContractRef | None
    requested_anchor_count: int
    passed_anchor_count: int
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "result_version", identifier(self.result_version, context="result_version")
        )
        if self.status not in {"completed_draft", "completed_without_strategy", "failed"}:
            raise ValueError("unsupported offline result status")
        for name in (
            "request_ref",
            "problem_ref",
            "routing_ref",
            "solver_execution_ref",
            "static_selection_ref",
            "dynamic_verification_ref",
            "finalization_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        for name in ("anchor_validation_ref", "strategy_ref"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef or None")
        object.__setattr__(
            self,
            "requested_anchor_count",
            integer(self.requested_anchor_count, context="requested_anchor_count"),
        )
        object.__setattr__(
            self,
            "passed_anchor_count",
            integer(self.passed_anchor_count, context="passed_anchor_count"),
        )
        if self.passed_anchor_count > self.requested_anchor_count:
            raise ValueError("passed anchor count exceeds requested anchor count")
        if self.status == "completed_draft" and self.strategy_ref is None:
            raise ValueError("completed_draft requires a strategy ref")
        if self.status != "completed_draft" and self.strategy_ref is not None:
            raise ValueError("non-draft result cannot contain a strategy ref")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "result_version": self.result_version,
            "status": self.status,
            "request_ref": self.request_ref.as_dict(),
            "problem_ref": self.problem_ref.as_dict(),
            "routing_ref": self.routing_ref.as_dict(),
            "solver_execution_ref": self.solver_execution_ref.as_dict(),
            "static_selection_ref": self.static_selection_ref.as_dict(),
            "dynamic_verification_ref": self.dynamic_verification_ref.as_dict(),
            "finalization_ref": self.finalization_ref.as_dict(),
            "anchor_validation_ref": (
                None if self.anchor_validation_ref is None else self.anchor_validation_ref.as_dict()
            ),
            "strategy_ref": None if self.strategy_ref is None else self.strategy_ref.as_dict(),
            "requested_anchor_count": self.requested_anchor_count,
            "passed_anchor_count": self.passed_anchor_count,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"offline-result-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "result_ref": self.ref.as_dict(),
            "result_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoResult:
        required = {
            "schema_id",
            "schema_version",
            "result_version",
            "status",
            "request_ref",
            "problem_ref",
            "routing_ref",
            "solver_execution_ref",
            "static_selection_ref",
            "dynamic_verification_ref",
            "finalization_ref",
            "anchor_validation_ref",
            "strategy_ref",
            "requested_anchor_count",
            "passed_anchor_count",
            "termination_reason",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"result_ref", "result_fingerprint"},
            context="offline RTO result",
        )
        status = value["status"]
        if status not in {"completed_draft", "completed_without_strategy", "failed"}:
            raise ValueError("unsupported offline result status")
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            result_version=identifier(value["result_version"], context="result_version"),
            status=status,
            request_ref=ContractRef.from_mapping(
                as_mapping(value["request_ref"], context="request_ref")
            ),
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            routing_ref=ContractRef.from_mapping(
                as_mapping(value["routing_ref"], context="routing_ref")
            ),
            solver_execution_ref=ContractRef.from_mapping(
                as_mapping(value["solver_execution_ref"], context="solver_execution_ref")
            ),
            static_selection_ref=ContractRef.from_mapping(
                as_mapping(value["static_selection_ref"], context="static_selection_ref")
            ),
            dynamic_verification_ref=ContractRef.from_mapping(
                as_mapping(value["dynamic_verification_ref"], context="dynamic_verification_ref")
            ),
            finalization_ref=ContractRef.from_mapping(
                as_mapping(value["finalization_ref"], context="finalization_ref")
            ),
            anchor_validation_ref=_optional_ref(
                value["anchor_validation_ref"], context="anchor_validation_ref"
            ),
            strategy_ref=_optional_ref(value["strategy_ref"], context="strategy_ref"),
            requested_anchor_count=integer(
                value["requested_anchor_count"], context="requested_anchor_count"
            ),
            passed_anchor_count=integer(
                value["passed_anchor_count"], context="passed_anchor_count"
            ),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        _validate_identity(
            value,
            ref=result.ref,
            ref_field="result_ref",
            fingerprint_field="result_fingerprint",
        )
        return result


@dataclass(frozen=True)
class WorkflowEvent:
    schema_id: str
    schema_version: str
    event_version: str
    workflow_ref: ContractRef
    sequence: int
    stage: str
    object_ref: ContractRef
    occurred_at: str
    previous_event_fingerprint: str | None
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "event_version", identifier(self.event_version, context="event_version")
        )
        object.__setattr__(self, "sequence", integer(self.sequence, context="sequence"))
        object.__setattr__(self, "stage", identifier(self.stage, context="stage"))
        if not isinstance(self.workflow_ref, ContractRef) or not isinstance(
            self.object_ref, ContractRef
        ):
            raise TypeError("workflow event refs must be ContractRef")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, context="occurred_at"))
        if self.previous_event_fingerprint is not None:
            object.__setattr__(
                self,
                "previous_event_fingerprint",
                digest(self.previous_event_fingerprint, context="previous_event_fingerprint"),
            )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "event_version": self.event_version,
            "workflow_ref": self.workflow_ref.as_dict(),
            "sequence": self.sequence,
            "stage": self.stage,
            "object_ref": self.object_ref.as_dict(),
            "occurred_at": self.occurred_at,
            "previous_event_fingerprint": self.previous_event_fingerprint,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "event_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WorkflowEvent:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "event_version",
                "workflow_ref",
                "sequence",
                "stage",
                "object_ref",
                "occurred_at",
                "previous_event_fingerprint",
                "claim_scope",
            },
            optional={"event_fingerprint"},
            context="workflow event",
        )
        previous = value["previous_event_fingerprint"]
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            event_version=identifier(value["event_version"], context="event_version"),
            workflow_ref=ContractRef.from_mapping(
                as_mapping(value["workflow_ref"], context="workflow_ref")
            ),
            sequence=integer(value["sequence"], context="sequence"),
            stage=identifier(value["stage"], context="stage"),
            object_ref=ContractRef.from_mapping(
                as_mapping(value["object_ref"], context="object_ref")
            ),
            occurred_at=_timestamp(value["occurred_at"], context="occurred_at"),
            previous_event_fingerprint=(
                None if previous is None else digest(previous, context="previous_event_fingerprint")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("event_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="event_fingerprint") != result.fingerprint
        ):
            raise ValueError("event_fingerprint differs from event content")
        return result


@dataclass(frozen=True)
class OfflineRtoManifest:
    schema_id: str
    schema_version: str
    manifest_version: str
    workflow_ref: ContractRef
    result_ref: ContractRef
    files: Mapping[str, str]
    software_versions: Mapping[str, str]
    created_at: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_id, self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "manifest_version",
            identifier(self.manifest_version, context="manifest_version"),
        )
        if self.manifest_version != UNIFIED_MANIFEST_VERSION:
            raise ValueError("manifest_version differs from the unified workflow contract")
        if not isinstance(self.workflow_ref, ContractRef) or not isinstance(
            self.result_ref, ContractRef
        ):
            raise TypeError("manifest refs must be ContractRef")
        raw_files = as_mapping(self.files, context="files")
        files: dict[str, str] = {}
        for relative, value in raw_files.items():
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
                raise ValueError("manifest files must be safe top-level paths")
            files[relative] = digest(value, context=f"files.{relative}")
        if not files:
            raise ValueError("manifest files cannot be empty")
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(files.items()))))
        object.__setattr__(
            self,
            "software_versions",
            string_mapping(self.software_versions, context="software_versions"),
        )
        object.__setattr__(self, "created_at", _timestamp(self.created_at, context="created_at"))

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "manifest_version": self.manifest_version,
            "workflow_ref": self.workflow_ref.as_dict(),
            "result_ref": self.result_ref.as_dict(),
            "files": dict(self.files),
            "software_versions": dict(self.software_versions),
            "created_at": self.created_at,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "manifest_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoManifest:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "manifest_version",
                "workflow_ref",
                "result_ref",
                "files",
                "software_versions",
                "created_at",
                "claim_scope",
                "manifest_fingerprint",
            },
            context="offline RTO manifest",
        )
        result = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            manifest_version=identifier(value["manifest_version"], context="manifest_version"),
            workflow_ref=ContractRef.from_mapping(
                as_mapping(value["workflow_ref"], context="workflow_ref")
            ),
            result_ref=ContractRef.from_mapping(
                as_mapping(value["result_ref"], context="result_ref")
            ),
            files=string_mapping(value["files"], context="files"),
            software_versions=string_mapping(
                value["software_versions"], context="software_versions"
            ),
            created_at=_timestamp(value["created_at"], context="created_at"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = digest(value["manifest_fingerprint"], context="manifest_fingerprint")
        if supplied != result.fingerprint:
            raise ValueError("manifest_fingerprint differs from manifest content")
        return result


def routing_ref(decision: SolverRoutingDecision) -> ContractRef:
    if not isinstance(decision, SolverRoutingDecision):
        raise TypeError("decision must be SolverRoutingDecision")
    return ContractRef(f"solver-routing-{decision.fingerprint[:16]}", decision.fingerprint)


def finalization_ref(result: FinalizationResult) -> ContractRef:
    if not isinstance(result, FinalizationResult):
        raise TypeError("result must be FinalizationResult")
    return result.ref


__all__ = [
    "OFFLINE_WORKFLOW_SCHEMA_ID",
    "OFFLINE_WORKFLOW_SCHEMA_VERSION",
    "AnchorAttempt",
    "AnchorValidationResult",
    "CapabilityBundleSnapshot",
    "CoveragePolicy",
    "DynamicVerificationArtifact",
    "FinalizationArtifact",
    "OfflineRtoManifest",
    "OfflineRtoRequest",
    "OfflineRtoResult",
    "OfflineRunStatus",
    "SolverExecutionArtifact",
    "WorkflowEvent",
    "finalization_ref",
    "routing_ref",
]
