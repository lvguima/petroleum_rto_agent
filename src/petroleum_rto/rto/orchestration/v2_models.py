"""Strict RTO V2 offline workflow, anchor, event, and manifest contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

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
from ..contracts.models import CLAIM_SCOPE, ContractRef, OperatingContextV1
from ..contracts.multiobjective import (
    RTO_V2_SCHEMA_VERSION,
    OptimizationProblemV2,
    ResolvedOptimizationIntentV2,
)
from ..contracts.results_v2 import CandidateEvaluationV2, CandidateProposalV2

OfflineRunStatusV2 = Literal["completed_draft", "completed_without_strategy", "failed"]


def _schema_claim(schema_version: str, claim_scope: str) -> None:
    if schema_version != RTO_V2_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V2 contract")
    if claim_scope != CLAIM_SCOPE:
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


@dataclass(frozen=True)
class OfflineRtoRequestV2:
    schema_version: str
    request_version: str
    resolved_intent_ref: ContractRef
    context_ref: ContractRef
    decision_catalog_ref: ContractRef
    kpi_catalog_ref: ContractRef
    constraint_profile_ref: ContractRef
    policy_ref: ContractRef
    objective_catalog_ref: ContractRef
    preference_catalog_ref: ContractRef
    publishability_catalog_ref: ContractRef
    provider_id: str
    coverage_policy: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "request_version", identifier(self.request_version, context="request_version")
        )
        for name in (
            "resolved_intent_ref",
            "context_ref",
            "decision_catalog_ref",
            "kpi_catalog_ref",
            "constraint_profile_ref",
            "policy_ref",
            "objective_catalog_ref",
            "preference_catalog_ref",
            "publishability_catalog_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        object.__setattr__(
            self,
            "coverage_policy",
            identifier(self.coverage_policy, context="coverage_policy"),
        )
        if self.coverage_policy not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported V2 coverage policy")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "resolved_intent_ref": self.resolved_intent_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "decision_catalog_ref": self.decision_catalog_ref.as_dict(),
            "kpi_catalog_ref": self.kpi_catalog_ref.as_dict(),
            "constraint_profile_ref": self.constraint_profile_ref.as_dict(),
            "policy_ref": self.policy_ref.as_dict(),
            "objective_catalog_ref": self.objective_catalog_ref.as_dict(),
            "preference_catalog_ref": self.preference_catalog_ref.as_dict(),
            "publishability_catalog_ref": self.publishability_catalog_ref.as_dict(),
            "provider_id": self.provider_id,
            "coverage_policy": self.coverage_policy,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def workflow_id(self) -> str:
        return f"offline-rto-v2-{self.fingerprint[:16]}"

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
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoRequestV2:
        refs = {
            "resolved_intent_ref",
            "context_ref",
            "decision_catalog_ref",
            "kpi_catalog_ref",
            "constraint_profile_ref",
            "policy_ref",
            "objective_catalog_ref",
            "preference_catalog_ref",
            "publishability_catalog_ref",
        }
        strict_keys(
            value,
            required={
                "schema_version",
                "request_version",
                *refs,
                "provider_id",
                "coverage_policy",
                "claim_scope",
            },
            optional={"workflow_id", "request_fingerprint"},
            context="offline RTO request V2",
        )
        parsed_refs = {
            name: ContractRef.from_mapping(as_mapping(value[name], context=name)) for name in refs
        }
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            request_version=identifier(value["request_version"], context="request_version"),
            resolved_intent_ref=parsed_refs["resolved_intent_ref"],
            context_ref=parsed_refs["context_ref"],
            decision_catalog_ref=parsed_refs["decision_catalog_ref"],
            kpi_catalog_ref=parsed_refs["kpi_catalog_ref"],
            constraint_profile_ref=parsed_refs["constraint_profile_ref"],
            policy_ref=parsed_refs["policy_ref"],
            objective_catalog_ref=parsed_refs["objective_catalog_ref"],
            preference_catalog_ref=parsed_refs["preference_catalog_ref"],
            publishability_catalog_ref=parsed_refs["publishability_catalog_ref"],
            provider_id=identifier(value["provider_id"], context="provider_id"),
            coverage_policy=identifier(value["coverage_policy"], context="coverage_policy"),
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
class AnchorAttemptV2:
    ratio: float
    context: OperatingContextV1
    resolved_intent: ResolvedOptimizationIntentV2
    problem: OptimizationProblemV2
    proposal: CandidateProposalV2
    static_evaluation: CandidateEvaluationV2
    dynamic_evaluation: CandidateEvaluationV2 | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ratio", finite(self.ratio, context="ratio"))
        if self.ratio <= 0.0:
            raise ValueError("anchor ratio must be positive")
        if self.resolved_intent.operating_context_ref != self.context.ref:
            raise ValueError("anchor intent references another context")
        if (
            self.problem.context_ref != self.context.ref
            or self.problem.intent_ref != self.resolved_intent.ref
        ):
            raise ValueError("anchor problem references another context or intent")
        if (
            self.proposal.problem_ref != self.problem.ref
            or self.proposal.context_ref != self.context.ref
        ):
            raise ValueError("anchor proposal references another problem or context")
        if (
            self.static_evaluation.stage != "M2"
            or self.static_evaluation.proposal_ref != self.proposal.ref
        ):
            raise ValueError("anchor static evaluation differs from proposal")
        if self.dynamic_evaluation is not None and (
            self.dynamic_evaluation.stage != "M4"
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
            "context": self.context.fingerprint_payload(),
            "resolved_intent": self.resolved_intent.as_dict(),
            "problem": self.problem.as_dict(),
            "proposal": self.proposal.as_dict(),
            "static_evaluation": self.static_evaluation.as_dict(),
            "dynamic_evaluation": (
                None if self.dynamic_evaluation is None else self.dynamic_evaluation.as_dict()
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AnchorAttemptV2:
        strict_keys(
            value,
            required={
                "ratio",
                "passed",
                "context",
                "resolved_intent",
                "problem",
                "proposal",
                "static_evaluation",
                "dynamic_evaluation",
            },
            context="anchor attempt V2",
        )
        dynamic = value["dynamic_evaluation"]
        result = cls(
            ratio=finite(value["ratio"], context="ratio"),
            context=OperatingContextV1.from_mapping(
                as_mapping(value["context"], context="context")
            ),
            resolved_intent=ResolvedOptimizationIntentV2.from_mapping(
                as_mapping(value["resolved_intent"], context="resolved_intent")
            ),
            problem=OptimizationProblemV2.from_mapping(
                as_mapping(value["problem"], context="problem")
            ),
            proposal=CandidateProposalV2.from_mapping(
                as_mapping(value["proposal"], context="proposal")
            ),
            static_evaluation=CandidateEvaluationV2.from_mapping(
                as_mapping(value["static_evaluation"], context="static_evaluation")
            ),
            dynamic_evaluation=(
                None
                if dynamic is None
                else CandidateEvaluationV2.from_mapping(
                    as_mapping(dynamic, context="dynamic_evaluation")
                )
            ),
        )
        if boolean(value["passed"], context="passed") != result.passed:
            raise ValueError("anchor passed flag differs from evaluations")
        return result


@dataclass(frozen=True)
class AnchorValidationResultV2:
    schema_version: str
    validation_version: str
    selected_action: Mapping[str, float]
    attempts: tuple[AnchorAttemptV2, ...]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "validation_version",
            identifier(self.validation_version, context="validation_version"),
        )
        action = numeric_mapping(self.selected_action, context="selected_action")
        if set(action) != {
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        }:
            raise ValueError("selected action differs from the V2 decision vector")
        object.__setattr__(self, "selected_action", action)
        attempts = tuple(self.attempts)
        ratios = tuple(item.ratio for item in attempts)
        if not attempts or ratios != tuple(sorted(set(ratios))):
            raise ValueError("anchor attempts must be non-empty, unique and sorted")
        if any(dict(item.proposal.decision_values) != dict(action) for item in attempts):
            raise ValueError("anchor attempts must evaluate the selected action")
        object.__setattr__(self, "attempts", attempts)

    @property
    def passed_attempts(self) -> tuple[AnchorAttemptV2, ...]:
        return tuple(item for item in self.attempts if item.passed)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "validation_version": self.validation_version,
            "selected_action": dict(self.selected_action),
            "attempts": [item.as_dict() for item in self.attempts],
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"anchor-validation-v2-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "validation_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AnchorValidationResultV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "validation_version",
                "selected_action",
                "attempts",
                "claim_scope",
            },
            optional={"validation_ref"},
            context="anchor validation V2",
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            validation_version=identifier(
                value["validation_version"], context="validation_version"
            ),
            selected_action=numeric_mapping(value["selected_action"], context="selected_action"),
            attempts=tuple(
                AnchorAttemptV2.from_mapping(as_mapping(item, context="anchor attempt"))
                for item in as_sequence(value["attempts"], context="attempts")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("validation_ref")
        if (
            supplied is not None
            and ContractRef.from_mapping(as_mapping(supplied, context="validation_ref"))
            != result.ref
        ):
            raise ValueError("validation_ref differs from validation content")
        return result


@dataclass(frozen=True)
class OfflineRtoResultV2:
    schema_version: str
    result_version: str
    status: OfflineRunStatusV2
    request_ref: ContractRef
    problem_ref: ContractRef
    pareto_search_ref: ContractRef
    preference_selection_ref: ContractRef
    optimization_result_ref: ContractRef
    anchor_validation_ref: ContractRef | None
    strategy_ref: ContractRef | None
    requested_anchor_count: int
    passed_anchor_count: int
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "result_version", identifier(self.result_version, context="result_version")
        )
        if self.status not in {"completed_draft", "completed_without_strategy", "failed"}:
            raise ValueError("unsupported offline V2 status")
        for name in (
            "request_ref",
            "problem_ref",
            "pareto_search_ref",
            "preference_selection_ref",
            "optimization_result_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        for name in ("anchor_validation_ref", "strategy_ref"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        for name in ("requested_anchor_count", "passed_anchor_count"):
            object.__setattr__(self, name, integer(getattr(self, name), context=name, minimum=0))
        if self.passed_anchor_count > self.requested_anchor_count:
            raise ValueError("passed anchor count exceeds requested anchor count")
        if self.status == "completed_draft" and self.strategy_ref is None:
            raise ValueError("completed draft requires a strategy ref")
        if self.status != "completed_draft" and self.strategy_ref is not None:
            raise ValueError("non-draft result cannot contain a strategy ref")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result_version": self.result_version,
            "status": self.status,
            "request_ref": self.request_ref.as_dict(),
            "problem_ref": self.problem_ref.as_dict(),
            "pareto_search_ref": self.pareto_search_ref.as_dict(),
            "preference_selection_ref": self.preference_selection_ref.as_dict(),
            "optimization_result_ref": self.optimization_result_ref.as_dict(),
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
        return ContractRef(f"offline-result-v2-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "result_ref": self.ref.as_dict(),
            "result_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoResultV2:
        required = {
            "schema_version",
            "result_version",
            "status",
            "request_ref",
            "problem_ref",
            "pareto_search_ref",
            "preference_selection_ref",
            "optimization_result_ref",
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
            context="offline result V2",
        )
        status = value["status"]
        if status not in {"completed_draft", "completed_without_strategy", "failed"}:
            raise ValueError("unsupported offline V2 status")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            result_version=identifier(value["result_version"], context="result_version"),
            status=status,
            request_ref=ContractRef.from_mapping(
                as_mapping(value["request_ref"], context="request_ref")
            ),
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            pareto_search_ref=ContractRef.from_mapping(
                as_mapping(value["pareto_search_ref"], context="pareto_search_ref")
            ),
            preference_selection_ref=ContractRef.from_mapping(
                as_mapping(value["preference_selection_ref"], context="preference_selection_ref")
            ),
            optimization_result_ref=ContractRef.from_mapping(
                as_mapping(value["optimization_result_ref"], context="optimization_result_ref")
            ),
            anchor_validation_ref=_optional_ref(
                value["anchor_validation_ref"], context="anchor_validation_ref"
            ),
            strategy_ref=_optional_ref(value["strategy_ref"], context="strategy_ref"),
            requested_anchor_count=integer(
                value["requested_anchor_count"], context="requested_anchor_count", minimum=0
            ),
            passed_anchor_count=integer(
                value["passed_anchor_count"], context="passed_anchor_count", minimum=0
            ),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied_ref = value.get("result_ref")
        if (
            supplied_ref is not None
            and ContractRef.from_mapping(as_mapping(supplied_ref, context="result_ref"))
            != result.ref
        ):
            raise ValueError("result_ref differs from result content")
        supplied = value.get("result_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="result_fingerprint") != result.fingerprint
        ):
            raise ValueError("result_fingerprint differs from result content")
        return result


@dataclass(frozen=True)
class WorkflowEventV2:
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
        _schema_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "event_version", identifier(self.event_version, context="event_version")
        )
        object.__setattr__(self, "sequence", integer(self.sequence, context="sequence"))
        object.__setattr__(self, "stage", identifier(self.stage, context="stage"))
        if not isinstance(self.workflow_ref, ContractRef) or not isinstance(
            self.object_ref, ContractRef
        ):
            raise TypeError("workflow event refs must be ContractRef values")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, context="occurred_at"))
        if self.previous_event_fingerprint is not None:
            object.__setattr__(
                self,
                "previous_event_fingerprint",
                digest(
                    self.previous_event_fingerprint,
                    context="previous_event_fingerprint",
                ),
            )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
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
    def from_mapping(cls, value: Mapping[str, object]) -> WorkflowEventV2:
        strict_keys(
            value,
            required={
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
            context="workflow event V2",
        )
        previous = value["previous_event_fingerprint"]
        result = cls(
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
class OfflineRtoManifestV2:
    schema_version: str
    manifest_version: str
    workflow_ref: ContractRef
    result_ref: ContractRef
    files: Mapping[str, str]
    software_versions: Mapping[str, str]
    created_at: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self, "manifest_version", identifier(self.manifest_version, context="manifest_version")
        )
        if not isinstance(self.workflow_ref, ContractRef) or not isinstance(
            self.result_ref, ContractRef
        ):
            raise TypeError("manifest refs must be ContractRef values")
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
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoManifestV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "manifest_version",
                "workflow_ref",
                "result_ref",
                "files",
                "software_versions",
                "created_at",
                "claim_scope",
            },
            optional={"manifest_fingerprint"},
            context="offline manifest V2",
        )
        result = cls(
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
        supplied = value.get("manifest_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="manifest_fingerprint") != result.fingerprint
        ):
            raise ValueError("manifest_fingerprint differs from manifest content")
        return result
