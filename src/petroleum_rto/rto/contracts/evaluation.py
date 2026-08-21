"""Strict R3/R4 evaluation, search, and final-result contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .common import (
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
from .models import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateProposalV1,
    ContractRef,
    EvaluationStage,
    SimulationRunBundleV1,
)

EvaluationStatus = Literal[
    "feasible",
    "process_infeasible",
    "invalid_request",
    "evaluation_error",
]
StaticSearchStatus = Literal["success", "no_static_feasible", "evaluation_error"]
OptimizationResultStatus = Literal[
    "success",
    "no_static_feasible",
    "shortlist_dynamic_failed",
    "feasible_not_publishable",
    "evaluation_error",
]


def _schema(value: str) -> None:
    if value != RTO_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V1 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _optional_finite(value: object, *, context: str) -> float | None:
    return None if value is None else finite(value, context=context)


def _optional_ref(value: object, *, context: str) -> ContractRef | None:
    return None if value is None else ContractRef.from_mapping(as_mapping(value, context=context))


@dataclass(frozen=True)
class RunEvidenceRefV1:
    """Compact reference to one strict simulator artifact without copying trajectories."""

    provider_id: str
    run_ref: str
    provider_request_fingerprint: str
    request_fingerprint: str
    effective_input_fingerprint: str
    result_fingerprint: str
    manifest_fingerprint: str
    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        object.__setattr__(self, "run_ref", text(self.run_ref, context="run_ref"))
        for name in (
            "provider_request_fingerprint",
            "request_fingerprint",
            "effective_input_fingerprint",
            "result_fingerprint",
            "manifest_fingerprint",
        ):
            object.__setattr__(self, name, digest(getattr(self, name), context=name))
        object.__setattr__(self, "versions", string_mapping(self.versions, context="versions"))
        sources = as_mapping(self.source_fingerprints, context="source_fingerprints")
        object.__setattr__(
            self,
            "source_fingerprints",
            MappingProxyType(
                {
                    text(key, context="source key"): digest(
                        item, context=f"source_fingerprints.{key}"
                    )
                    for key, item in sources.items()
                }
            ),
        )

    @classmethod
    def from_bundle(cls, bundle: SimulationRunBundleV1) -> RunEvidenceRefV1:
        if not isinstance(bundle, SimulationRunBundleV1):
            raise TypeError("run evidence requires a SimulationRunBundleV1")
        return cls(
            provider_id=bundle.provider_id,
            run_ref=bundle.run_ref,
            provider_request_fingerprint=bundle.provider_request_fingerprint,
            request_fingerprint=bundle.request_fingerprint,
            effective_input_fingerprint=bundle.effective_input_fingerprint,
            result_fingerprint=bundle.result_fingerprint,
            manifest_fingerprint=bundle.manifest_fingerprint,
            versions=bundle.versions,
            source_fingerprints=bundle.source_fingerprints,
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_request_fingerprint": self.provider_request_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.semantic_payload(),
            "run_ref": self.run_ref,
            "request_fingerprint": self.request_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RunEvidenceRefV1:
        strict_keys(
            value,
            required={
                "provider_id",
                "run_ref",
                "provider_request_fingerprint",
                "request_fingerprint",
                "effective_input_fingerprint",
                "result_fingerprint",
                "manifest_fingerprint",
                "versions",
                "source_fingerprints",
            },
            context="run evidence ref",
        )
        return cls(
            provider_id=identifier(value["provider_id"], context="provider_id"),
            run_ref=text(value["run_ref"], context="run_ref"),
            provider_request_fingerprint=digest(
                value["provider_request_fingerprint"], context="provider_request_fingerprint"
            ),
            request_fingerprint=digest(value["request_fingerprint"], context="request_fingerprint"),
            effective_input_fingerprint=digest(
                value["effective_input_fingerprint"], context="effective_input_fingerprint"
            ),
            result_fingerprint=digest(value["result_fingerprint"], context="result_fingerprint"),
            manifest_fingerprint=digest(
                value["manifest_fingerprint"], context="manifest_fingerprint"
            ),
            versions=string_mapping(value["versions"], context="versions"),
            source_fingerprints=string_mapping(
                value["source_fingerprints"], context="source_fingerprints"
            ),
        )


@dataclass(frozen=True)
class ConstraintOutcomeV1:
    constraint_id: str
    stage: str
    metric_id: str
    operator: str
    limit: float
    candidate_value: float
    baseline_value: float | None
    normalized_margin: float
    passed: bool

    def __post_init__(self) -> None:
        for name in ("constraint_id", "stage", "metric_id", "operator"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported constraint operator")
        for name in ("limit", "candidate_value", "normalized_margin"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if self.baseline_value is not None:
            object.__setattr__(
                self,
                "baseline_value",
                finite(self.baseline_value, context="baseline_value"),
            )
        if not isinstance(self.passed, bool):
            raise TypeError("constraint passed must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "stage": self.stage,
            "metric_id": self.metric_id,
            "operator": self.operator,
            "limit": self.limit,
            "candidate_value": self.candidate_value,
            "baseline_value": self.baseline_value,
            "normalized_margin": self.normalized_margin,
            "passed": self.passed,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ConstraintOutcomeV1:
        strict_keys(
            value,
            required={
                "constraint_id",
                "stage",
                "metric_id",
                "operator",
                "limit",
                "candidate_value",
                "baseline_value",
                "normalized_margin",
                "passed",
            },
            context="constraint outcome",
        )
        return cls(
            constraint_id=identifier(value["constraint_id"], context="constraint_id"),
            stage=identifier(value["stage"], context="stage"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            operator=identifier(value["operator"], context="operator"),
            limit=finite(value["limit"], context="limit"),
            candidate_value=finite(value["candidate_value"], context="candidate_value"),
            baseline_value=_optional_finite(value["baseline_value"], context="baseline_value"),
            normalized_margin=finite(value["normalized_margin"], context="normalized_margin"),
            passed=boolean(value["passed"], context="passed"),
        )


@dataclass(frozen=True)
class CandidateEvaluationV1:
    schema_version: str
    evaluation_version: str
    stage: EvaluationStage
    status: EvaluationStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    proposal_ref: ContractRef
    pair_id: str
    objective_metric_id: str | None
    baseline_objective: float | None
    candidate_objective: float | None
    objective_delta: float | None
    relative_improvement: float | None
    metrics: Mapping[str, float]
    constraints: tuple[ConstraintOutcomeV1, ...]
    minimum_normalized_margin: float | None
    normalized_action_l1: float
    reason_codes: tuple[str, ...]
    baseline_evidence: RunEvidenceRefV1 | None
    candidate_evidence: RunEvidenceRefV1 | None
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self,
            "evaluation_version",
            identifier(self.evaluation_version, context="evaluation_version"),
        )
        if self.stage not in {"M2", "M4"}:
            raise ValueError("unsupported evaluation stage")
        if self.status not in {
            "feasible",
            "process_infeasible",
            "invalid_request",
            "evaluation_error",
        }:
            raise ValueError("unsupported candidate evaluation status")
        for name in ("problem_ref", "context_ref", "proposal_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        object.__setattr__(self, "pair_id", identifier(self.pair_id, context="pair_id"))
        if self.objective_metric_id is not None:
            object.__setattr__(
                self,
                "objective_metric_id",
                identifier(self.objective_metric_id, context="objective_metric_id"),
            )
        for name in (
            "baseline_objective",
            "candidate_objective",
            "objective_delta",
            "relative_improvement",
            "minimum_normalized_margin",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, finite(value, context=name))
        object.__setattr__(self, "metrics", numeric_mapping(self.metrics, context="metrics"))
        constraints = tuple(self.constraints)
        if any(not isinstance(item, ConstraintOutcomeV1) for item in constraints):
            raise TypeError("constraints must contain ConstraintOutcomeV1 values")
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(
            self,
            "normalized_action_l1",
            finite(self.normalized_action_l1, context="normalized_action_l1"),
        )
        if self.normalized_action_l1 < 0.0:
            raise ValueError("normalized_action_l1 must be non-negative")
        reasons = tuple(identifier(item, context="reason_code") for item in self.reason_codes)
        if len(reasons) != len(set(reasons)):
            raise ValueError("reason_codes must be unique")
        object.__setattr__(self, "reason_codes", reasons)
        for name in ("baseline_evidence", "candidate_evidence"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, RunEvidenceRefV1):
                raise TypeError(f"{name} must be a RunEvidenceRefV1")
        if self.status == "feasible":
            if not constraints or not all(item.passed for item in constraints):
                raise ValueError("feasible evaluation requires passing constraints")
            if self.baseline_evidence is None or self.candidate_evidence is None:
                raise ValueError("feasible evaluation requires paired evidence")
            if reasons:
                raise ValueError("feasible evaluation cannot contain reason codes")
        elif not reasons:
            raise ValueError("non-feasible evaluation requires a reason code")
        objective_values = (
            self.objective_metric_id,
            self.baseline_objective,
            self.candidate_objective,
            self.objective_delta,
            self.relative_improvement,
        )
        if (
            self.stage == "M2"
            and self.status == "feasible"
            and any(value is None for value in objective_values)
        ):
            raise ValueError("feasible M2 candidate requires complete objective values")
        if any(value is not None for value in objective_values) and any(
            value is None for value in objective_values
        ):
            raise ValueError("objective values must be entirely present or absent")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluation_version": self.evaluation_version,
            "stage": self.stage,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "proposal_ref": self.proposal_ref.as_dict(),
            "pair_id": self.pair_id,
            "objective_metric_id": self.objective_metric_id,
            "baseline_objective": self.baseline_objective,
            "candidate_objective": self.candidate_objective,
            "objective_delta": self.objective_delta,
            "relative_improvement": self.relative_improvement,
            "metrics": dict(self.metrics),
            "constraints": [item.as_dict() for item in self.constraints],
            "minimum_normalized_margin": self.minimum_normalized_margin,
            "normalized_action_l1": self.normalized_action_l1,
            "reason_codes": list(self.reason_codes),
            "baseline_evidence": (
                None
                if self.baseline_evidence is None
                else self.baseline_evidence.semantic_payload()
            ),
            "candidate_evidence": (
                None
                if self.candidate_evidence is None
                else self.candidate_evidence.semantic_payload()
            ),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"evaluation-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "evaluation_id": self.ref.object_id,
            "evaluation_fingerprint": self.fingerprint,
            "baseline_evidence": (
                None if self.baseline_evidence is None else self.baseline_evidence.as_dict()
            ),
            "candidate_evidence": (
                None if self.candidate_evidence is None else self.candidate_evidence.as_dict()
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CandidateEvaluationV1:
        required = {
            "schema_version",
            "evaluation_version",
            "stage",
            "status",
            "problem_ref",
            "context_ref",
            "proposal_ref",
            "pair_id",
            "objective_metric_id",
            "baseline_objective",
            "candidate_objective",
            "objective_delta",
            "relative_improvement",
            "metrics",
            "constraints",
            "minimum_normalized_margin",
            "normalized_action_l1",
            "reason_codes",
            "baseline_evidence",
            "candidate_evidence",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"evaluation_id", "evaluation_fingerprint"},
            context="candidate evaluation",
        )
        stage = value["stage"]
        status = value["status"]
        if stage not in {"M2", "M4"}:
            raise ValueError("unsupported evaluation stage")
        if status not in {
            "feasible",
            "process_infeasible",
            "invalid_request",
            "evaluation_error",
        }:
            raise ValueError("unsupported candidate evaluation status")
        objective_id = value["objective_metric_id"]
        evaluation = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            evaluation_version=identifier(
                value["evaluation_version"], context="evaluation_version"
            ),
            stage=stage,
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            proposal_ref=ContractRef.from_mapping(
                as_mapping(value["proposal_ref"], context="proposal_ref")
            ),
            pair_id=identifier(value["pair_id"], context="pair_id"),
            objective_metric_id=(
                None
                if objective_id is None
                else identifier(objective_id, context="objective_metric_id")
            ),
            baseline_objective=_optional_finite(
                value["baseline_objective"], context="baseline_objective"
            ),
            candidate_objective=_optional_finite(
                value["candidate_objective"], context="candidate_objective"
            ),
            objective_delta=_optional_finite(value["objective_delta"], context="objective_delta"),
            relative_improvement=_optional_finite(
                value["relative_improvement"], context="relative_improvement"
            ),
            metrics=numeric_mapping(value["metrics"], context="metrics"),
            constraints=tuple(
                ConstraintOutcomeV1.from_mapping(as_mapping(item, context="constraint outcome"))
                for item in as_sequence(value["constraints"], context="constraints")
            ),
            minimum_normalized_margin=_optional_finite(
                value["minimum_normalized_margin"],
                context="minimum_normalized_margin",
            ),
            normalized_action_l1=finite(
                value["normalized_action_l1"], context="normalized_action_l1"
            ),
            reason_codes=tuple(
                identifier(item, context="reason_code")
                for item in as_sequence(value["reason_codes"], context="reason_codes")
            ),
            baseline_evidence=(
                None
                if value["baseline_evidence"] is None
                else RunEvidenceRefV1.from_mapping(
                    as_mapping(value["baseline_evidence"], context="baseline_evidence")
                )
            ),
            candidate_evidence=(
                None
                if value["candidate_evidence"] is None
                else RunEvidenceRefV1.from_mapping(
                    as_mapping(value["candidate_evidence"], context="candidate_evidence")
                )
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("evaluation_id") not in {None, evaluation.ref.object_id}:
            raise ValueError("evaluation_id differs from evaluation content")
        supplied = value.get("evaluation_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="evaluation_fingerprint") != evaluation.fingerprint
        ):
            raise ValueError("evaluation_fingerprint differs from evaluation content")
        return evaluation


@dataclass(frozen=True)
class StaticSearchResultV1:
    schema_version: str
    search_version: str
    status: StaticSearchStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    proposals: tuple[CandidateProposalV1, ...]
    evaluations: tuple[CandidateEvaluationV1, ...]
    ranked_feasible: tuple[CandidateEvaluationV1, ...]
    coarse_count: int
    refinement_count: int
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "search_version", identifier(self.search_version, context="search_version")
        )
        if self.status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported static search status")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.context_ref, ContractRef
        ):
            raise TypeError("search refs must be ContractRef values")
        proposals = tuple(self.proposals)
        if any(not isinstance(item, CandidateProposalV1) for item in proposals):
            raise TypeError("proposals must contain CandidateProposalV1 values")
        if len({item.ref for item in proposals}) != len(proposals):
            raise ValueError("search proposals must have unique semantic refs")
        object.__setattr__(self, "proposals", proposals)
        evaluations = tuple(self.evaluations)
        ranked = tuple(self.ranked_feasible)
        if any(item.stage != "M2" for item in evaluations + ranked):
            raise ValueError("static search may contain only M2 evaluations")
        evaluation_refs = {item.ref for item in evaluations}
        if tuple(item.proposal_ref for item in evaluations) != tuple(
            item.ref for item in proposals
        ):
            raise ValueError("search evaluations must align with generated proposals")
        if any(item.status != "feasible" or item.ref not in evaluation_refs for item in ranked):
            raise ValueError("ranked_feasible must be a feasible subset of evaluations")
        object.__setattr__(self, "evaluations", evaluations)
        object.__setattr__(self, "ranked_feasible", ranked)
        object.__setattr__(self, "coarse_count", integer(self.coarse_count, context="coarse_count"))
        object.__setattr__(
            self,
            "refinement_count",
            integer(self.refinement_count, context="refinement_count"),
        )
        if self.coarse_count != 25 or self.refinement_count > 8:
            raise ValueError("RTO V1 search must use 25 coarse and at most 8 refinement points")
        if len(evaluations) != self.coarse_count + self.refinement_count:
            raise ValueError("search evaluation count differs from generated candidates")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )
        if (self.status == "success") != bool(ranked):
            raise ValueError("static search success must agree with feasible ranking")
        if self.status == "no_static_feasible" and ranked:
            raise ValueError("no_static_feasible cannot contain ranked candidates")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "search_version": self.search_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "proposal_refs": [item.ref.as_dict() for item in self.proposals],
            "evaluation_refs": [item.ref.as_dict() for item in self.evaluations],
            "ranked_feasible_refs": [item.ref.as_dict() for item in self.ranked_feasible],
            "coarse_count": self.coarse_count,
            "refinement_count": self.refinement_count,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"static-search-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "search_id": self.ref.object_id,
            "search_fingerprint": self.fingerprint,
            "proposals": [item.as_dict() for item in self.proposals],
            "evaluations": [item.as_dict() for item in self.evaluations],
            "ranked_feasible": [item.as_dict() for item in self.ranked_feasible],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StaticSearchResultV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "search_version",
                "status",
                "problem_ref",
                "context_ref",
                "proposal_refs",
                "evaluation_refs",
                "ranked_feasible_refs",
                "coarse_count",
                "refinement_count",
                "termination_reason",
                "claim_scope",
                "proposals",
                "evaluations",
                "ranked_feasible",
            },
            optional={"search_id", "search_fingerprint"},
            context="static search result",
        )
        status = value["status"]
        if status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported static search status")
        proposals = tuple(
            CandidateProposalV1.from_mapping(as_mapping(item, context="candidate proposal"))
            for item in as_sequence(value["proposals"], context="proposals")
        )
        evaluations = parse_evaluations(value["evaluations"], context="evaluations")
        ranked = parse_evaluations(value["ranked_feasible"], context="ranked_feasible")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            search_version=identifier(value["search_version"], context="search_version"),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            proposals=proposals,
            evaluations=evaluations,
            ranked_feasible=ranked,
            coarse_count=integer(value["coarse_count"], context="coarse_count"),
            refinement_count=integer(value["refinement_count"], context="refinement_count"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if parse_refs(value["evaluation_refs"], context="evaluation_refs") != tuple(
            item.ref for item in result.evaluations
        ):
            raise ValueError("evaluation_refs differ from embedded evaluations")
        if parse_refs(value["proposal_refs"], context="proposal_refs") != tuple(
            item.ref for item in result.proposals
        ):
            raise ValueError("proposal_refs differ from embedded proposals")
        if parse_refs(value["ranked_feasible_refs"], context="ranked_feasible_refs") != tuple(
            item.ref for item in result.ranked_feasible
        ):
            raise ValueError("ranked_feasible_refs differ from embedded ranking")
        if value.get("search_id") is not None:
            require_derived_id(value["search_id"], result.ref.object_id, context="search_id")
        if value.get("search_fingerprint") is not None:
            require_derived_digest(
                value["search_fingerprint"],
                result.fingerprint,
                context="search_fingerprint",
            )
        return result


@dataclass(frozen=True)
class OptimizationResultV1:
    schema_version: str
    result_version: str
    status: OptimizationResultStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    static_search_ref: ContractRef
    static_ranking: tuple[ContractRef, ...]
    dynamic_evaluations: tuple[CandidateEvaluationV1, ...]
    selected_proposal_ref: ContractRef | None
    selected_static_evaluation_ref: ContractRef | None
    selected_dynamic_evaluation_ref: ContractRef | None
    publishable: bool
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "result_version", identifier(self.result_version, context="result_version")
        )
        if self.status not in {
            "success",
            "no_static_feasible",
            "shortlist_dynamic_failed",
            "feasible_not_publishable",
            "evaluation_error",
        }:
            raise ValueError("unsupported optimization result status")
        for name in ("problem_ref", "context_ref", "static_search_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        ranking = tuple(self.static_ranking)
        if any(not isinstance(item, ContractRef) for item in ranking):
            raise TypeError("static_ranking must contain ContractRef values")
        object.__setattr__(self, "static_ranking", ranking)
        dynamic = tuple(self.dynamic_evaluations)
        if any(item.stage != "M4" for item in dynamic):
            raise ValueError("dynamic evaluations must be M4 evaluations")
        object.__setattr__(self, "dynamic_evaluations", dynamic)
        for name in (
            "selected_proposal_ref",
            "selected_static_evaluation_ref",
            "selected_dynamic_evaluation_ref",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        if not isinstance(self.publishable, bool):
            raise TypeError("publishable must be boolean")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )
        selected = (
            self.selected_proposal_ref,
            self.selected_static_evaluation_ref,
            self.selected_dynamic_evaluation_ref,
        )
        if self.status in {"success", "feasible_not_publishable"}:
            if any(item is None for item in selected):
                raise ValueError("selected result requires all selected refs")
        elif any(item is not None for item in selected):
            raise ValueError("unselected result cannot contain selected refs")
        if self.publishable != (self.status == "success"):
            raise ValueError("publishable must be true only for success")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result_version": self.result_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "static_search_ref": self.static_search_ref.as_dict(),
            "static_ranking": [item.as_dict() for item in self.static_ranking],
            "dynamic_evaluation_refs": [item.ref.as_dict() for item in self.dynamic_evaluations],
            "selected_proposal_ref": (
                None if self.selected_proposal_ref is None else self.selected_proposal_ref.as_dict()
            ),
            "selected_static_evaluation_ref": (
                None
                if self.selected_static_evaluation_ref is None
                else self.selected_static_evaluation_ref.as_dict()
            ),
            "selected_dynamic_evaluation_ref": (
                None
                if self.selected_dynamic_evaluation_ref is None
                else self.selected_dynamic_evaluation_ref.as_dict()
            ),
            "publishable": self.publishable,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def result_id(self) -> str:
        return f"optimization-result-{self.fingerprint[:16]}"

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "result_id": self.result_id,
            "result_fingerprint": self.fingerprint,
            "dynamic_evaluations": [item.as_dict() for item in self.dynamic_evaluations],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationResultV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "result_version",
                "status",
                "problem_ref",
                "context_ref",
                "static_search_ref",
                "static_ranking",
                "dynamic_evaluation_refs",
                "selected_proposal_ref",
                "selected_static_evaluation_ref",
                "selected_dynamic_evaluation_ref",
                "publishable",
                "termination_reason",
                "claim_scope",
                "dynamic_evaluations",
            },
            optional={"result_id", "result_fingerprint"},
            context="optimization result",
        )
        status = value["status"]
        if status not in {
            "success",
            "no_static_feasible",
            "shortlist_dynamic_failed",
            "feasible_not_publishable",
            "evaluation_error",
        }:
            raise ValueError("unsupported optimization result status")
        selected = optional_refs_from_mapping(
            value,
            (
                "selected_proposal_ref",
                "selected_static_evaluation_ref",
                "selected_dynamic_evaluation_ref",
            ),
        )
        dynamic = parse_evaluations(value["dynamic_evaluations"], context="dynamic_evaluations")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            result_version=identifier(value["result_version"], context="result_version"),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            static_search_ref=ContractRef.from_mapping(
                as_mapping(value["static_search_ref"], context="static_search_ref")
            ),
            static_ranking=parse_refs(value["static_ranking"], context="static_ranking"),
            dynamic_evaluations=dynamic,
            selected_proposal_ref=selected["selected_proposal_ref"],
            selected_static_evaluation_ref=selected["selected_static_evaluation_ref"],
            selected_dynamic_evaluation_ref=selected["selected_dynamic_evaluation_ref"],
            publishable=boolean(value["publishable"], context="publishable"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if parse_refs(value["dynamic_evaluation_refs"], context="dynamic_evaluation_refs") != tuple(
            item.ref for item in result.dynamic_evaluations
        ):
            raise ValueError("dynamic_evaluation_refs differ from embedded evaluations")
        if value.get("result_id") is not None:
            require_derived_id(value["result_id"], result.result_id, context="result_id")
        if value.get("result_fingerprint") is not None:
            require_derived_digest(
                value["result_fingerprint"],
                result.fingerprint,
                context="result_fingerprint",
            )
        return result


def parse_evaluations(value: object, *, context: str) -> tuple[CandidateEvaluationV1, ...]:
    """Strict shared parser for embedded evaluation sequences."""

    return tuple(
        CandidateEvaluationV1.from_mapping(as_mapping(item, context="candidate evaluation"))
        for item in as_sequence(value, context=context)
    )


def parse_refs(value: object, *, context: str) -> tuple[ContractRef, ...]:
    return tuple(
        ContractRef.from_mapping(as_mapping(item, context="contract ref"))
        for item in as_sequence(value, context=context)
    )


def require_derived_digest(value: object, expected: str, *, context: str) -> None:
    supplied = digest(value, context=context)
    if supplied != expected:
        raise ValueError(f"{context} differs from contract content")


def require_derived_id(value: object, expected: str, *, context: str) -> None:
    supplied = identifier(value, context=context)
    if supplied != expected:
        raise ValueError(f"{context} differs from contract content")


def optional_refs_from_mapping(
    value: Mapping[str, object], names: Sequence[str]
) -> dict[str, ContractRef | None]:
    return {name: _optional_ref(value[name], context=name) for name in names}
