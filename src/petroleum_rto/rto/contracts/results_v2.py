"""Strict RTO V2 proposal, vector-evaluation, and Pareto result contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .common import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    finite,
    identifier,
    integer,
    numeric_mapping,
    strict_keys,
    text,
)
from .evaluation import ConstraintOutcomeV1, RunEvidenceRefV1
from .models import CLAIM_SCOPE, ContractRef, EvaluationStage
from .multiobjective import RTO_V2_SCHEMA_VERSION, ObjectiveSenseV2

EvaluationStatusV2 = Literal[
    "feasible",
    "process_infeasible",
    "invalid_request",
    "evaluation_error",
]
ParetoSearchStatusV2 = Literal["success", "no_static_feasible", "evaluation_error"]


def _schema(value: str) -> None:
    if value != RTO_V2_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V2 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _optional_finite(value: object, *, context: str) -> float | None:
    return None if value is None else finite(value, context=context)


def _refs(value: object, *, context: str) -> tuple[ContractRef, ...]:
    return tuple(
        ContractRef.from_mapping(as_mapping(item, context=f"{context} item"))
        for item in as_sequence(value, context=context)
    )


@dataclass(frozen=True)
class CandidateProposalV2:
    schema_version: str
    proposal_version: str
    candidate_id: str
    sequence: int
    origin: str
    problem_ref: ContractRef
    context_ref: ContractRef
    decision_values: Mapping[str, float]
    output_kind: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("proposal_version", "candidate_id", "origin", "output_kind"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "sequence", integer(self.sequence, context="sequence"))
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.context_ref, ContractRef
        ):
            raise TypeError("proposal refs must be ContractRef values")
        values = numeric_mapping(self.decision_values, context="decision_values")
        if not values:
            raise ValueError("proposal decision_values cannot be empty")
        object.__setattr__(self, "decision_values", values)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proposal_version": self.proposal_version,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "decision_values": dict(self.decision_values),
            "output_kind": self.output_kind,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"proposal-v2-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "candidate_id": self.candidate_id,
            "sequence": self.sequence,
            "origin": self.origin,
            "proposal_id": self.ref.object_id,
            "proposal_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CandidateProposalV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "proposal_version",
                "candidate_id",
                "sequence",
                "origin",
                "problem_ref",
                "context_ref",
                "decision_values",
                "output_kind",
                "claim_scope",
            },
            optional={"proposal_id", "proposal_fingerprint"},
            context="candidate proposal V2",
        )
        proposal = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            proposal_version=identifier(value["proposal_version"], context="proposal_version"),
            candidate_id=identifier(value["candidate_id"], context="candidate_id"),
            sequence=integer(value["sequence"], context="sequence"),
            origin=identifier(value["origin"], context="origin"),
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            decision_values=numeric_mapping(value["decision_values"], context="decision_values"),
            output_kind=identifier(value["output_kind"], context="output_kind"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("proposal_id") not in {None, proposal.ref.object_id}:
            raise ValueError("proposal_id differs from proposal content")
        supplied = value.get("proposal_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="proposal_fingerprint") != proposal.fingerprint
        ):
            raise ValueError("proposal_fingerprint differs from proposal content")
        return proposal


@dataclass(frozen=True)
class ObjectiveOutcomeV2:
    metric_id: str
    sense: ObjectiveSenseV2
    unit: str
    kpi_formula_id: str
    baseline_value: float
    candidate_value: float
    directional_absolute_improvement: float
    relative_directional_improvement: float | None
    relative_unavailable_reason: str | None
    normalized_directional_improvement: float

    def __post_init__(self) -> None:
        for name in ("metric_id", "kpi_formula_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        for name in (
            "baseline_value",
            "candidate_value",
            "directional_absolute_improvement",
            "normalized_directional_improvement",
        ):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if self.relative_directional_improvement is not None:
            object.__setattr__(
                self,
                "relative_directional_improvement",
                finite(
                    self.relative_directional_improvement,
                    context="relative_directional_improvement",
                ),
            )
        if self.relative_unavailable_reason is not None:
            object.__setattr__(
                self,
                "relative_unavailable_reason",
                identifier(
                    self.relative_unavailable_reason,
                    context="relative_unavailable_reason",
                ),
            )
        if (self.relative_directional_improvement is None) == (
            self.relative_unavailable_reason is None
        ):
            raise ValueError(
                "objective outcome requires either relative improvement or an unavailable reason"
            )
        expected = (
            self.baseline_value - self.candidate_value
            if self.sense == "minimize"
            else self.candidate_value - self.baseline_value
        )
        if abs(expected - self.directional_absolute_improvement) > 1e-12:
            raise ValueError("directional absolute improvement differs from objective values")

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "unit": self.unit,
            "kpi_formula_id": self.kpi_formula_id,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "directional_absolute_improvement": self.directional_absolute_improvement,
            "relative_directional_improvement": self.relative_directional_improvement,
            "relative_unavailable_reason": self.relative_unavailable_reason,
            "normalized_directional_improvement": self.normalized_directional_improvement,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveOutcomeV2:
        strict_keys(
            value,
            required={
                "metric_id",
                "sense",
                "unit",
                "kpi_formula_id",
                "baseline_value",
                "candidate_value",
                "directional_absolute_improvement",
                "relative_directional_improvement",
                "relative_unavailable_reason",
                "normalized_directional_improvement",
            },
            context="objective outcome V2",
        )
        sense = value["sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        reason = value["relative_unavailable_reason"]
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            sense=sense,
            unit=text(value["unit"], context="unit"),
            kpi_formula_id=identifier(value["kpi_formula_id"], context="kpi_formula_id"),
            baseline_value=finite(value["baseline_value"], context="baseline_value"),
            candidate_value=finite(value["candidate_value"], context="candidate_value"),
            directional_absolute_improvement=finite(
                value["directional_absolute_improvement"],
                context="directional_absolute_improvement",
            ),
            relative_directional_improvement=_optional_finite(
                value["relative_directional_improvement"],
                context="relative_directional_improvement",
            ),
            relative_unavailable_reason=(
                None
                if reason is None
                else identifier(reason, context="relative_unavailable_reason")
            ),
            normalized_directional_improvement=finite(
                value["normalized_directional_improvement"],
                context="normalized_directional_improvement",
            ),
        )


@dataclass(frozen=True)
class CandidateEvaluationV2:
    schema_version: str
    evaluation_version: str
    stage: EvaluationStage
    status: EvaluationStatusV2
    problem_ref: ContractRef
    context_ref: ContractRef
    proposal_ref: ContractRef
    pair_id: str
    objective_outcomes: tuple[ObjectiveOutcomeV2, ...]
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
        outcomes = tuple(self.objective_outcomes)
        ids = tuple(item.metric_id for item in outcomes)
        if any(not isinstance(item, ObjectiveOutcomeV2) for item in outcomes):
            raise TypeError("objective_outcomes must contain ObjectiveOutcomeV2 values")
        if len(ids) != len(set(ids)):
            raise ValueError("objective outcome ids must be unique")
        object.__setattr__(self, "objective_outcomes", outcomes)
        object.__setattr__(self, "metrics", numeric_mapping(self.metrics, context="metrics"))
        constraints = tuple(self.constraints)
        if any(not isinstance(item, ConstraintOutcomeV1) for item in constraints):
            raise TypeError("constraints must contain ConstraintOutcomeV1 values")
        object.__setattr__(self, "constraints", constraints)
        if self.minimum_normalized_margin is not None:
            object.__setattr__(
                self,
                "minimum_normalized_margin",
                finite(self.minimum_normalized_margin, context="minimum_normalized_margin"),
            )
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
            evidence = getattr(self, name)
            if evidence is not None and not isinstance(evidence, RunEvidenceRefV1):
                raise TypeError(f"{name} must be a RunEvidenceRefV1")
        if self.status == "feasible":
            if not constraints or not all(item.passed for item in constraints):
                raise ValueError("feasible evaluation requires passing constraints")
            if self.baseline_evidence is None or self.candidate_evidence is None:
                raise ValueError("feasible evaluation requires paired evidence")
            if reasons:
                raise ValueError("feasible evaluation cannot contain reason codes")
            if self.stage == "M2" and not outcomes:
                raise ValueError("feasible M2 evaluation requires objective outcomes")
        elif not reasons:
            raise ValueError("non-feasible evaluation requires a reason code")
        if self.stage == "M4" and outcomes:
            raise ValueError("M4 evaluation cannot recompute M2 objective outcomes")

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
            "objective_outcomes": [item.as_dict() for item in self.objective_outcomes],
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
        return ContractRef(f"evaluation-v2-{self.fingerprint[:16]}", self.fingerprint)

    def outcome_by_id(self, metric_id: str) -> ObjectiveOutcomeV2:
        for item in self.objective_outcomes:
            if item.metric_id == metric_id:
                return item
        raise KeyError(f"unknown objective outcome {metric_id!r}")

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
    def from_mapping(cls, value: Mapping[str, object]) -> CandidateEvaluationV2:
        required = {
            "schema_version",
            "evaluation_version",
            "stage",
            "status",
            "problem_ref",
            "context_ref",
            "proposal_ref",
            "pair_id",
            "objective_outcomes",
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
            context="candidate evaluation V2",
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
            objective_outcomes=tuple(
                ObjectiveOutcomeV2.from_mapping(as_mapping(item, context="objective outcome"))
                for item in as_sequence(value["objective_outcomes"], context="objective_outcomes")
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
class ParetoLayerV2:
    rank: int
    evaluation_refs: tuple[ContractRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rank", integer(self.rank, context="rank", minimum=1))
        refs = tuple(self.evaluation_refs)
        if not refs or len(refs) != len(set(refs)):
            raise ValueError("Pareto layer refs must be non-empty and unique")
        if any(not isinstance(item, ContractRef) for item in refs):
            raise TypeError("Pareto layer refs must be ContractRef values")
        object.__setattr__(self, "evaluation_refs", refs)

    def as_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "evaluation_refs": [item.as_dict() for item in self.evaluation_refs],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ParetoLayerV2:
        strict_keys(value, required={"rank", "evaluation_refs"}, context="Pareto layer")
        return cls(
            rank=integer(value["rank"], context="rank", minimum=1),
            evaluation_refs=_refs(value["evaluation_refs"], context="evaluation_refs"),
        )


@dataclass(frozen=True)
class ObjectiveEquivalenceGroupV2:
    representative_ref: ContractRef
    member_refs: tuple[ContractRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.representative_ref, ContractRef):
            raise TypeError("representative_ref must be a ContractRef")
        members = tuple(self.member_refs)
        if len(members) < 2 or len(members) != len(set(members)):
            raise ValueError("equivalence group must contain at least two unique members")
        if self.representative_ref not in members:
            raise ValueError("equivalence representative must be a member")
        object.__setattr__(self, "member_refs", members)

    def as_dict(self) -> dict[str, object]:
        return {
            "representative_ref": self.representative_ref.as_dict(),
            "member_refs": [item.as_dict() for item in self.member_refs],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveEquivalenceGroupV2:
        strict_keys(
            value,
            required={"representative_ref", "member_refs"},
            context="objective equivalence group",
        )
        return cls(
            representative_ref=ContractRef.from_mapping(
                as_mapping(value["representative_ref"], context="representative_ref")
            ),
            member_refs=_refs(value["member_refs"], context="member_refs"),
        )


@dataclass(frozen=True)
class ParetoSearchResultV2:
    schema_version: str
    search_version: str
    status: ParetoSearchStatusV2
    problem_ref: ContractRef
    context_ref: ContractRef
    proposals: tuple[CandidateProposalV2, ...]
    evaluations: tuple[CandidateEvaluationV2, ...]
    pareto_layers: tuple[ParetoLayerV2, ...]
    pareto_refs: tuple[ContractRef, ...]
    equivalence_groups: tuple[ObjectiveEquivalenceGroupV2, ...]
    grid_count: int
    feasible_count: int
    process_infeasible_count: int
    error_count: int
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "search_version", identifier(self.search_version, context="search_version")
        )
        if self.status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported Pareto search status")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.context_ref, ContractRef
        ):
            raise TypeError("search refs must be ContractRef values")
        proposals = tuple(self.proposals)
        evaluations = tuple(self.evaluations)
        if any(not isinstance(item, CandidateProposalV2) for item in proposals):
            raise TypeError("proposals must contain CandidateProposalV2 values")
        if any(not isinstance(item, CandidateEvaluationV2) for item in evaluations):
            raise TypeError("evaluations must contain CandidateEvaluationV2 values")
        if len({item.ref for item in proposals}) != len(proposals):
            raise ValueError("search proposals must have unique semantic refs")
        if tuple(item.proposal_ref for item in evaluations) != tuple(
            item.ref for item in proposals
        ):
            raise ValueError("search evaluations must align with proposals")
        if any(item.stage != "M2" for item in evaluations):
            raise ValueError("Pareto search may contain only M2 evaluations")
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "evaluations", evaluations)
        feasible_refs = {item.ref for item in evaluations if item.status == "feasible"}
        groups = tuple(self.equivalence_groups)
        grouped_refs: set[ContractRef] = set()
        nonrepresentative_refs: set[ContractRef] = set()
        for group in groups:
            if not isinstance(group, ObjectiveEquivalenceGroupV2):
                raise TypeError(
                    "equivalence_groups must contain ObjectiveEquivalenceGroupV2 values"
                )
            if not set(group.member_refs).issubset(feasible_refs):
                raise ValueError("equivalence group references a non-feasible evaluation")
            if grouped_refs.intersection(group.member_refs):
                raise ValueError("equivalence groups must not overlap")
            grouped_refs.update(group.member_refs)
            nonrepresentative_refs.update(
                ref for ref in group.member_refs if ref != group.representative_ref
            )
        object.__setattr__(self, "equivalence_groups", groups)
        layers = tuple(self.pareto_layers)
        if any(not isinstance(item, ParetoLayerV2) for item in layers):
            raise TypeError("pareto_layers must contain ParetoLayerV2 values")
        if tuple(item.rank for item in layers) != tuple(range(1, len(layers) + 1)):
            raise ValueError("Pareto ranks must be contiguous")
        flattened = tuple(ref for layer in layers for ref in layer.evaluation_refs)
        expected_ranked_refs = (
            set() if self.status == "evaluation_error" else feasible_refs - nonrepresentative_refs
        )
        if len(flattened) != len(set(flattened)) or set(flattened) != expected_ranked_refs:
            raise ValueError("Pareto layers must partition feasible equivalence representatives")
        object.__setattr__(self, "pareto_layers", layers)
        pareto_refs = tuple(self.pareto_refs)
        expected_front = () if not layers else layers[0].evaluation_refs
        if pareto_refs != expected_front:
            raise ValueError("pareto_refs must equal the first Pareto layer")
        object.__setattr__(self, "pareto_refs", pareto_refs)
        if self.status == "evaluation_error" and (groups or layers or pareto_refs):
            raise ValueError("evaluation_error cannot expose an untrusted Pareto ranking")
        for name in (
            "grid_count",
            "feasible_count",
            "process_infeasible_count",
            "error_count",
        ):
            object.__setattr__(self, name, integer(getattr(self, name), context=name, minimum=0))
        if self.grid_count != len(evaluations) or self.grid_count != len(proposals):
            raise ValueError("grid_count differs from embedded candidates")
        expected_feasible = sum(item.status == "feasible" for item in evaluations)
        expected_infeasible = sum(item.status == "process_infeasible" for item in evaluations)
        expected_errors = sum(
            item.status in {"invalid_request", "evaluation_error"} for item in evaluations
        )
        if (
            self.feasible_count != expected_feasible
            or self.process_infeasible_count != expected_infeasible
            or self.error_count != expected_errors
        ):
            raise ValueError("search counts differ from evaluation statuses")
        if self.status == "success" and (not pareto_refs or expected_errors):
            raise ValueError("successful Pareto search requires a trusted non-empty front")
        if self.status == "no_static_feasible" and (pareto_refs or expected_feasible):
            raise ValueError("no_static_feasible cannot contain feasible evaluations")
        if self.status == "evaluation_error" and not expected_errors:
            raise ValueError("evaluation_error search requires an error evaluation")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "search_version": self.search_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "proposal_refs": [item.ref.as_dict() for item in self.proposals],
            "evaluation_refs": [item.ref.as_dict() for item in self.evaluations],
            "pareto_layers": [item.as_dict() for item in self.pareto_layers],
            "pareto_refs": [item.as_dict() for item in self.pareto_refs],
            "equivalence_groups": [item.as_dict() for item in self.equivalence_groups],
            "grid_count": self.grid_count,
            "feasible_count": self.feasible_count,
            "process_infeasible_count": self.process_infeasible_count,
            "error_count": self.error_count,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"pareto-search-{self.fingerprint[:16]}", self.fingerprint)

    def evaluation_by_ref(self, ref: ContractRef) -> CandidateEvaluationV2:
        for item in self.evaluations:
            if item.ref == ref:
                return item
        raise KeyError(f"unknown evaluation ref {ref.object_id!r}")

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "search_id": self.ref.object_id,
            "search_fingerprint": self.fingerprint,
            "proposals": [item.as_dict() for item in self.proposals],
            "evaluations": [item.as_dict() for item in self.evaluations],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ParetoSearchResultV2:
        required = {
            "schema_version",
            "search_version",
            "status",
            "problem_ref",
            "context_ref",
            "proposal_refs",
            "evaluation_refs",
            "pareto_layers",
            "pareto_refs",
            "equivalence_groups",
            "grid_count",
            "feasible_count",
            "process_infeasible_count",
            "error_count",
            "termination_reason",
            "claim_scope",
            "proposals",
            "evaluations",
        }
        strict_keys(
            value,
            required=required,
            optional={"search_id", "search_fingerprint"},
            context="Pareto search result V2",
        )
        status = value["status"]
        if status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported Pareto search status")
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
            proposals=tuple(
                CandidateProposalV2.from_mapping(as_mapping(item, context="candidate proposal"))
                for item in as_sequence(value["proposals"], context="proposals")
            ),
            evaluations=tuple(
                CandidateEvaluationV2.from_mapping(as_mapping(item, context="candidate evaluation"))
                for item in as_sequence(value["evaluations"], context="evaluations")
            ),
            pareto_layers=tuple(
                ParetoLayerV2.from_mapping(as_mapping(item, context="Pareto layer"))
                for item in as_sequence(value["pareto_layers"], context="pareto_layers")
            ),
            pareto_refs=_refs(value["pareto_refs"], context="pareto_refs"),
            equivalence_groups=tuple(
                ObjectiveEquivalenceGroupV2.from_mapping(
                    as_mapping(item, context="equivalence group")
                )
                for item in as_sequence(value["equivalence_groups"], context="equivalence_groups")
            ),
            grid_count=integer(value["grid_count"], context="grid_count", minimum=0),
            feasible_count=integer(value["feasible_count"], context="feasible_count", minimum=0),
            process_infeasible_count=integer(
                value["process_infeasible_count"],
                context="process_infeasible_count",
                minimum=0,
            ),
            error_count=integer(value["error_count"], context="error_count", minimum=0),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if _refs(value["proposal_refs"], context="proposal_refs") != tuple(
            item.ref for item in result.proposals
        ):
            raise ValueError("proposal_refs differ from embedded proposals")
        if _refs(value["evaluation_refs"], context="evaluation_refs") != tuple(
            item.ref for item in result.evaluations
        ):
            raise ValueError("evaluation_refs differ from embedded evaluations")
        if value.get("search_id") not in {None, result.ref.object_id}:
            raise ValueError("search_id differs from search content")
        supplied = value.get("search_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="search_fingerprint") != result.fingerprint
        ):
            raise ValueError("search_fingerprint differs from search content")
        return result


def immutable_numeric_mapping(value: Mapping[str, float]) -> Mapping[str, float]:
    """Expose an immutable numeric mapping for downstream summary contracts."""

    return MappingProxyType(dict(numeric_mapping(value, context="numeric mapping")))
