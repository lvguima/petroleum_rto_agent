"""Strict R6 offline workflow request, result, event and manifest contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    ContractRef,
    OperatingContextV1,
    OptimizationProblemV1,
)
from ..contracts.common import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    identifier,
    integer,
    numeric_mapping,
    strict_keys,
    string_mapping,
    text,
)

OfflineRunStatus = Literal["completed_draft", "completed_without_strategy", "failed"]


def _schema(value: str) -> None:
    if value != RTO_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V1 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _timestamp(value: object, *, context: str) -> str:
    raw = text(value, context=context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include an explicit timezone")
    return raw


def _optional_ref(value: object, *, context: str) -> ContractRef | None:
    return None if value is None else ContractRef.from_mapping(as_mapping(value, context=context))


@dataclass(frozen=True)
class AnchorAttemptV1:
    ratio: float
    context: OperatingContextV1
    problem: OptimizationProblemV1
    proposal: CandidateProposalV1
    static_evaluation: CandidateEvaluationV1
    dynamic_evaluation: CandidateEvaluationV1 | None

    def __post_init__(self) -> None:
        from ..contracts.common import finite

        object.__setattr__(self, "ratio", finite(self.ratio, context="anchor ratio"))
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

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "ratio": self.ratio,
            "context_ref": self.context.ref.as_dict(),
            "problem_ref": self.problem.ref.as_dict(),
            "proposal_ref": self.proposal.ref.as_dict(),
            "static_evaluation_ref": self.static_evaluation.ref.as_dict(),
            "dynamic_evaluation_ref": (
                None if self.dynamic_evaluation is None else self.dynamic_evaluation.ref.as_dict()
            ),
            "passed": self.passed,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "context": self.context.fingerprint_payload(),
            "problem": self.problem.as_dict(),
            "proposal": self.proposal.as_dict(),
            "static_evaluation": self.static_evaluation.as_dict(),
            "dynamic_evaluation": (
                None if self.dynamic_evaluation is None else self.dynamic_evaluation.as_dict()
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AnchorAttemptV1:
        from ..contracts.common import finite

        required = {
            "ratio",
            "context_ref",
            "problem_ref",
            "proposal_ref",
            "static_evaluation_ref",
            "dynamic_evaluation_ref",
            "passed",
            "context",
            "problem",
            "proposal",
            "static_evaluation",
            "dynamic_evaluation",
        }
        strict_keys(value, required=required, context="anchor attempt")
        dynamic_raw = value["dynamic_evaluation"]
        result = cls(
            ratio=finite(value["ratio"], context="ratio"),
            context=OperatingContextV1.from_mapping(
                as_mapping(value["context"], context="context")
            ),
            problem=OptimizationProblemV1.from_mapping(
                as_mapping(value["problem"], context="problem")
            ),
            proposal=CandidateProposalV1.from_mapping(
                as_mapping(value["proposal"], context="proposal")
            ),
            static_evaluation=CandidateEvaluationV1.from_mapping(
                as_mapping(value["static_evaluation"], context="static_evaluation")
            ),
            dynamic_evaluation=(
                None
                if dynamic_raw is None
                else CandidateEvaluationV1.from_mapping(
                    as_mapping(dynamic_raw, context="dynamic_evaluation")
                )
            ),
        )
        refs = {
            "context_ref": result.context.ref,
            "problem_ref": result.problem.ref,
            "proposal_ref": result.proposal.ref,
            "static_evaluation_ref": result.static_evaluation.ref,
            "dynamic_evaluation_ref": (
                None if result.dynamic_evaluation is None else result.dynamic_evaluation.ref
            ),
        }
        for field, expected in refs.items():
            supplied = _optional_ref(value[field], context=field)
            if supplied != expected:
                raise ValueError(f"{field} differs from embedded anchor object")
        from ..contracts.common import boolean

        if boolean(value["passed"], context="passed") != result.passed:
            raise ValueError("anchor passed flag differs from evaluations")
        return result


@dataclass(frozen=True)
class AnchorValidationResultV1:
    schema_version: str
    validation_version: str
    selected_action: Mapping[str, float]
    attempts: tuple[AnchorAttemptV1, ...]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
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
            raise ValueError("selected_action must contain exactly two V1 variables")
        object.__setattr__(self, "selected_action", action)
        attempts = tuple(self.attempts)
        ratios = tuple(item.ratio for item in attempts)
        if not attempts or ratios != tuple(sorted(set(ratios))):
            raise ValueError("anchor attempts must be non-empty, unique and sorted")
        if any(dict(item.proposal.decision_values) != dict(action) for item in attempts):
            raise ValueError("anchor attempts must evaluate the selected action")
        object.__setattr__(self, "attempts", attempts)

    @property
    def passed_attempts(self) -> tuple[AnchorAttemptV1, ...]:
        return tuple(item for item in self.attempts if item.passed)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "validation_version": self.validation_version,
            "selected_action": dict(self.selected_action),
            "attempts": [item.fingerprint_payload() for item in self.attempts],
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"anchor-validation-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "validation_ref": self.ref.as_dict(),
            "attempts": [item.as_dict() for item in self.attempts],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AnchorValidationResultV1:
        required = {
            "schema_version",
            "validation_version",
            "selected_action",
            "attempts",
            "claim_scope",
        }
        strict_keys(
            value, required=required, optional={"validation_ref"}, context="anchor validation"
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            validation_version=identifier(
                value["validation_version"], context="validation_version"
            ),
            selected_action=numeric_mapping(value["selected_action"], context="selected_action"),
            attempts=tuple(
                AnchorAttemptV1.from_mapping(as_mapping(item, context="anchor attempt"))
                for item in as_sequence(value["attempts"], context="attempts")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("validation_ref") is not None:
            supplied = ContractRef.from_mapping(
                as_mapping(value["validation_ref"], context="validation_ref")
            )
            if supplied != result.ref:
                raise ValueError("validation_ref differs from anchor validation")
        return result


@dataclass(frozen=True)
class OfflineRtoRequestV1:
    schema_version: str
    request_version: str
    intent_ref: ContractRef
    context_ref: ContractRef
    decision_catalog_ref: ContractRef
    kpi_catalog_ref: ContractRef
    constraint_profile_ref: ContractRef
    policy_ref: ContractRef
    provider_id: str
    coverage_policy: str
    claim_scope: str
    external_request_ref: ContractRef | None = None

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "request_version", identifier(self.request_version, context="request_version")
        )
        for name in (
            "intent_ref",
            "context_ref",
            "decision_catalog_ref",
            "kpi_catalog_ref",
            "constraint_profile_ref",
            "policy_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        if self.external_request_ref is not None and not isinstance(
            self.external_request_ref, ContractRef
        ):
            raise TypeError("external_request_ref must be a ContractRef or None")
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        object.__setattr__(
            self,
            "coverage_policy",
            identifier(self.coverage_policy, context="coverage_policy"),
        )
        if self.coverage_policy not in {"point", "sampled-anchors"}:
            raise ValueError("unsupported offline coverage_policy")

    def fingerprint_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "intent_ref": self.intent_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "decision_catalog_ref": self.decision_catalog_ref.as_dict(),
            "kpi_catalog_ref": self.kpi_catalog_ref.as_dict(),
            "constraint_profile_ref": self.constraint_profile_ref.as_dict(),
            "policy_ref": self.policy_ref.as_dict(),
            "provider_id": self.provider_id,
            "coverage_policy": self.coverage_policy,
            "claim_scope": self.claim_scope,
        }
        if self.external_request_ref is not None:
            payload["external_request_ref"] = self.external_request_ref.as_dict()
        return payload

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
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoRequestV1:
        required = {
            "schema_version",
            "request_version",
            "intent_ref",
            "context_ref",
            "decision_catalog_ref",
            "kpi_catalog_ref",
            "constraint_profile_ref",
            "policy_ref",
            "provider_id",
            "coverage_policy",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"workflow_id", "request_fingerprint", "external_request_ref"},
            context="offline RTO request",
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            request_version=identifier(value["request_version"], context="request_version"),
            intent_ref=ContractRef.from_mapping(
                as_mapping(value["intent_ref"], context="intent_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            decision_catalog_ref=ContractRef.from_mapping(
                as_mapping(value["decision_catalog_ref"], context="decision_catalog_ref")
            ),
            kpi_catalog_ref=ContractRef.from_mapping(
                as_mapping(value["kpi_catalog_ref"], context="kpi_catalog_ref")
            ),
            constraint_profile_ref=ContractRef.from_mapping(
                as_mapping(value["constraint_profile_ref"], context="constraint_profile_ref")
            ),
            policy_ref=ContractRef.from_mapping(
                as_mapping(value["policy_ref"], context="policy_ref")
            ),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            coverage_policy=identifier(value["coverage_policy"], context="coverage_policy"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            external_request_ref=(
                None
                if value.get("external_request_ref") is None
                else ContractRef.from_mapping(
                    as_mapping(value["external_request_ref"], context="external_request_ref")
                )
            ),
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
class OfflineRtoResultV1:
    schema_version: str
    result_version: str
    status: OfflineRunStatus
    request_ref: ContractRef
    problem_ref: ContractRef
    static_search_ref: ContractRef
    optimization_result_ref: ContractRef
    strategy_ref: ContractRef | None
    requested_anchor_count: int
    passed_anchor_count: int
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "result_version", identifier(self.result_version, context="result_version")
        )
        if self.status not in {"completed_draft", "completed_without_strategy", "failed"}:
            raise ValueError("unsupported offline RTO result status")
        for name in ("request_ref", "problem_ref", "static_search_ref", "optimization_result_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        if self.strategy_ref is not None and not isinstance(self.strategy_ref, ContractRef):
            raise TypeError("strategy_ref must be a ContractRef")
        for name in ("requested_anchor_count", "passed_anchor_count"):
            object.__setattr__(self, name, integer(getattr(self, name), context=name))
        if self.passed_anchor_count > self.requested_anchor_count:
            raise ValueError("passed_anchor_count exceeds requested_anchor_count")
        if (self.status == "completed_draft") != (self.strategy_ref is not None):
            raise ValueError("only completed_draft may contain a strategy_ref")
        if self.status == "completed_draft" and self.passed_anchor_count < 1:
            raise ValueError("completed draft requires at least one passing anchor")
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
            "static_search_ref": self.static_search_ref.as_dict(),
            "optimization_result_ref": self.optimization_result_ref.as_dict(),
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
        return {**self.fingerprint_payload(), "result_ref": self.ref.as_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoResultV1:
        required = {
            "schema_version",
            "result_version",
            "status",
            "request_ref",
            "problem_ref",
            "static_search_ref",
            "optimization_result_ref",
            "strategy_ref",
            "requested_anchor_count",
            "passed_anchor_count",
            "termination_reason",
            "claim_scope",
        }
        strict_keys(value, required=required, optional={"result_ref"}, context="offline RTO result")
        status = value["status"]
        if status not in {"completed_draft", "completed_without_strategy", "failed"}:
            raise ValueError("unsupported offline RTO result status")
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
            static_search_ref=ContractRef.from_mapping(
                as_mapping(value["static_search_ref"], context="static_search_ref")
            ),
            optimization_result_ref=ContractRef.from_mapping(
                as_mapping(value["optimization_result_ref"], context="optimization_result_ref")
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
        if value.get("result_ref") is not None:
            supplied = ContractRef.from_mapping(
                as_mapping(value["result_ref"], context="result_ref")
            )
            if supplied != result.ref:
                raise ValueError("result_ref differs from offline result content")
        return result


@dataclass(frozen=True)
class WorkflowEventV1:
    schema_version: str
    event_version: str
    workflow_ref: ContractRef
    sequence: int
    stage: str
    object_ref: ContractRef | None
    occurred_at: str
    previous_event_fingerprint: str | None

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(
            self, "event_version", identifier(self.event_version, context="event_version")
        )
        if not isinstance(self.workflow_ref, ContractRef):
            raise TypeError("workflow_ref must be a ContractRef")
        object.__setattr__(self, "sequence", integer(self.sequence, context="sequence"))
        object.__setattr__(self, "stage", identifier(self.stage, context="stage"))
        if self.object_ref is not None and not isinstance(self.object_ref, ContractRef):
            raise TypeError("object_ref must be a ContractRef")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, context="occurred_at"))
        if self.previous_event_fingerprint is not None:
            object.__setattr__(
                self,
                "previous_event_fingerprint",
                digest(self.previous_event_fingerprint, context="previous_event_fingerprint"),
            )
        if (self.sequence == 0) != (self.previous_event_fingerprint is None):
            raise ValueError("only first workflow event may omit previous fingerprint")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_version": self.event_version,
            "workflow_ref": self.workflow_ref.as_dict(),
            "sequence": self.sequence,
            "stage": self.stage,
            "object_ref": None if self.object_ref is None else self.object_ref.as_dict(),
            "occurred_at": self.occurred_at,
            "previous_event_fingerprint": self.previous_event_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "event_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WorkflowEventV1:
        required = {
            "schema_version",
            "event_version",
            "workflow_ref",
            "sequence",
            "stage",
            "object_ref",
            "occurred_at",
            "previous_event_fingerprint",
        }
        strict_keys(
            value, required=required, optional={"event_fingerprint"}, context="workflow event"
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            event_version=identifier(value["event_version"], context="event_version"),
            workflow_ref=ContractRef.from_mapping(
                as_mapping(value["workflow_ref"], context="workflow_ref")
            ),
            sequence=integer(value["sequence"], context="sequence"),
            stage=identifier(value["stage"], context="stage"),
            object_ref=_optional_ref(value["object_ref"], context="object_ref"),
            occurred_at=_timestamp(value["occurred_at"], context="occurred_at"),
            previous_event_fingerprint=(
                None
                if value["previous_event_fingerprint"] is None
                else digest(
                    value["previous_event_fingerprint"], context="previous_event_fingerprint"
                )
            ),
        )
        supplied = value.get("event_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="event_fingerprint") != result.fingerprint
        ):
            raise ValueError("event_fingerprint differs from workflow event")
        return result


@dataclass(frozen=True)
class OfflineRtoManifestV1:
    schema_version: str
    manifest_version: str
    workflow_ref: ContractRef
    result_ref: ContractRef
    files: Mapping[str, str]
    software_versions: Mapping[str, str]
    created_at: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self,
            "manifest_version",
            identifier(self.manifest_version, context="manifest_version"),
        )
        if not isinstance(self.workflow_ref, ContractRef) or not isinstance(
            self.result_ref, ContractRef
        ):
            raise TypeError("manifest refs must be ContractRef values")
        raw_files = as_mapping(self.files, context="manifest files")
        files: dict[str, str] = {}
        for raw_path, raw_digest in raw_files.items():
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or str(path) != raw_path:
                raise ValueError("manifest file path must be a normalized relative POSIX path")
            files[raw_path] = digest(raw_digest, context=f"manifest files.{raw_path}")
        if not files or tuple(files) != tuple(sorted(files)):
            raise ValueError("manifest files must be non-empty and sorted")
        object.__setattr__(self, "files", MappingProxyType(files))
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
    def from_mapping(cls, value: Mapping[str, object]) -> OfflineRtoManifestV1:
        required = {
            "schema_version",
            "manifest_version",
            "workflow_ref",
            "result_ref",
            "files",
            "software_versions",
            "created_at",
            "claim_scope",
        }
        strict_keys(
            value, required=required, optional={"manifest_fingerprint"}, context="offline manifest"
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


def parse_workflow_events(value: object) -> tuple[WorkflowEventV1, ...]:
    return tuple(
        WorkflowEventV1.from_mapping(as_mapping(item, context="workflow event"))
        for item in as_sequence(value, context="workflow events")
    )
