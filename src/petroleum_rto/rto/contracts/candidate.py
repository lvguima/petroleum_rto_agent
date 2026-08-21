"""Candidate proposal and vector-evaluation contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

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
    text,
)
from .evidence import RunEvidenceRef
from .problem import ENGINEERING_CLAIM_SCOPE, ObjectiveSense
from .reference import ContractRef

CANDIDATE_SCHEMA_VERSION: Final[str] = "2.0.0"
EvaluationStatus = Literal[
    "feasible",
    "process_infeasible",
    "invalid_request",
    "evaluation_error",
    "not_evaluated",
]


@dataclass(frozen=True)
class CandidateProposal:
    """One solver query expressed as an absolute canonical decision vector."""

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
        if self.schema_version != CANDIDATE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the candidate contract")
        if self.claim_scope != ENGINEERING_CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        for name in ("proposal_version", "candidate_id", "origin", "output_kind"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.context_ref, ContractRef
        ):
            raise TypeError("proposal refs must be ContractRef")
        values = numeric_mapping(self.decision_values, context="decision_values")
        if not values:
            raise ValueError("decision_values must be non-empty")
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
        return ContractRef(f"proposal-{self.fingerprint[:16]}", self.fingerprint)

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
    def from_mapping(cls, value: Mapping[str, object]) -> CandidateProposal:
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
            context="candidate proposal",
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
class ObjectiveOutcome:
    """One paired objective value in a candidate's objective vector."""

    metric_id: str
    sense: ObjectiveSense
    unit: str
    formula_id: str
    baseline_value: float
    candidate_value: float
    directional_absolute_improvement: float
    relative_directional_improvement: float | None
    normalized_directional_improvement: float

    def __post_init__(self) -> None:
        for name in ("metric_id", "formula_id"):
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
        expected = (
            self.baseline_value - self.candidate_value
            if self.sense == "minimize"
            else self.candidate_value - self.baseline_value
        )
        if abs(expected - self.directional_absolute_improvement) > 1e-12:
            raise ValueError("objective directional improvement differs from its values")

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "unit": self.unit,
            "formula_id": self.formula_id,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "directional_absolute_improvement": self.directional_absolute_improvement,
            "relative_directional_improvement": self.relative_directional_improvement,
            "normalized_directional_improvement": self.normalized_directional_improvement,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveOutcome:
        strict_keys(
            value,
            required={
                "metric_id",
                "sense",
                "unit",
                "formula_id",
                "baseline_value",
                "candidate_value",
                "directional_absolute_improvement",
                "relative_directional_improvement",
                "normalized_directional_improvement",
            },
            context="objective outcome",
        )
        sense = value["sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        relative = value["relative_directional_improvement"]
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            sense=sense,
            unit=text(value["unit"], context="unit"),
            formula_id=identifier(value["formula_id"], context="formula_id"),
            baseline_value=finite(value["baseline_value"], context="baseline_value"),
            candidate_value=finite(value["candidate_value"], context="candidate_value"),
            directional_absolute_improvement=finite(
                value["directional_absolute_improvement"],
                context="directional_absolute_improvement",
            ),
            relative_directional_improvement=(
                None
                if relative is None
                else finite(relative, context="relative_directional_improvement")
            ),
            normalized_directional_improvement=finite(
                value["normalized_directional_improvement"],
                context="normalized_directional_improvement",
            ),
        )


@dataclass(frozen=True)
class ConstraintOutcome:
    """One evaluated hard constraint with a normalized feasibility margin."""

    constraint_id: str
    metric_id: str
    raw_value: float
    limit: float
    normalized_margin: float
    passed: bool

    def __post_init__(self) -> None:
        for name in ("constraint_id", "metric_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        for name in ("raw_value", "limit", "normalized_margin"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "metric_id": self.metric_id,
            "raw_value": self.raw_value,
            "limit": self.limit,
            "normalized_margin": self.normalized_margin,
            "passed": self.passed,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ConstraintOutcome:
        strict_keys(
            value,
            required={
                "constraint_id",
                "metric_id",
                "raw_value",
                "limit",
                "normalized_margin",
                "passed",
            },
            context="constraint outcome",
        )
        return cls(
            constraint_id=identifier(value["constraint_id"], context="constraint_id"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            raw_value=finite(value["raw_value"], context="raw_value"),
            limit=finite(value["limit"], context="limit"),
            normalized_margin=finite(value["normalized_margin"], context="normalized_margin"),
            passed=boolean(value["passed"], context="passed"),
        )


@dataclass(frozen=True)
class CandidateEvaluation:
    """Provider-neutral vector evaluation consumed by every solver plugin."""

    schema_version: str
    evaluation_version: str
    stage: str
    status: EvaluationStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    proposal_ref: ContractRef
    pair_id: str
    objective_outcomes: tuple[ObjectiveOutcome, ...]
    metrics: Mapping[str, float]
    constraints: tuple[ConstraintOutcome, ...]
    minimum_normalized_margin: float | None
    normalized_action_l1: float
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[RunEvidenceRef, ...]
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the candidate contract")
        if self.claim_scope != ENGINEERING_CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        for name in ("evaluation_version", "stage", "pair_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.stage not in {"M2", "M4"}:
            raise ValueError("candidate evaluation stage must be M2 or M4")
        if self.status not in {
            "feasible",
            "process_infeasible",
            "invalid_request",
            "evaluation_error",
            "not_evaluated",
        }:
            raise ValueError("unsupported evaluation status")
        for name in ("problem_ref", "context_ref", "proposal_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        outcomes = tuple(self.objective_outcomes)
        outcome_ids = tuple(item.metric_id for item in outcomes)
        if len(outcome_ids) != len(set(outcome_ids)):
            raise ValueError("objective outcomes must be unique")
        constraints = tuple(self.constraints)
        constraint_ids = tuple(item.constraint_id for item in constraints)
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraint outcomes must be unique")
        metrics = numeric_mapping(self.metrics, context="metrics")
        reasons = tuple(identifier(item, context="reason_code") for item in self.reason_codes)
        if len(reasons) != len(set(reasons)) or reasons != tuple(sorted(reasons)):
            raise ValueError("reason_codes must be unique and sorted")
        evidence = tuple(self.evidence_refs)
        if any(not isinstance(item, RunEvidenceRef) for item in evidence):
            raise TypeError("evidence_refs must contain RunEvidenceRef")
        evidence_roles = tuple(item.pair_role for item in evidence)
        if evidence_roles not in {(), ("baseline", "candidate")}:
            raise ValueError("evidence_refs must be empty or an ordered baseline/candidate pair")
        object.__setattr__(self, "objective_outcomes", outcomes)
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(
            self,
            "normalized_action_l1",
            finite(self.normalized_action_l1, context="normalized_action_l1"),
        )
        if self.normalized_action_l1 < 0.0:
            raise ValueError("normalized_action_l1 must be non-negative")
        if self.minimum_normalized_margin is not None:
            object.__setattr__(
                self,
                "minimum_normalized_margin",
                finite(
                    self.minimum_normalized_margin,
                    context="minimum_normalized_margin",
                ),
            )
        expected_margin = (
            None if not constraints else min(item.normalized_margin for item in constraints)
        )
        if expected_margin is None:
            if self.minimum_normalized_margin is not None:
                raise ValueError("minimum_normalized_margin requires constraint outcomes")
        elif self.minimum_normalized_margin is None or not math.isclose(
            self.minimum_normalized_margin,
            expected_margin,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("minimum_normalized_margin differs from constraint outcomes")
        if self.status == "feasible":
            if (
                self.minimum_normalized_margin is None
                or reasons
                or evidence_roles != ("baseline", "candidate")
            ):
                raise ValueError(
                    "feasible evaluation requires a margin, paired evidence, and no reasons"
                )
            if any(not item.passed for item in constraints):
                raise ValueError("feasible evaluation cannot contain a failed constraint")
        elif not reasons:
            raise ValueError("non-feasible evaluation requires at least one reason code")
        if outcomes and evidence_roles != ("baseline", "candidate"):
            raise ValueError("evaluated objective outcomes require paired evidence")
        if self.stage == "M2" and self.status == "feasible" and not outcomes:
            raise ValueError("feasible M2 evaluation requires objective outcomes")
        if self.stage == "M4" and outcomes:
            raise ValueError("M4 evaluation must not recompute M2 objective outcomes")
        if self.stage == "M4" and self.status == "feasible" and not constraints:
            raise ValueError("feasible M4 evaluation requires dynamic constraint outcomes")
        if self.status in {"invalid_request", "evaluation_error", "not_evaluated"} and outcomes:
            raise ValueError("error or unevaluated result cannot contain objective outcomes")

    @property
    def objective_values(self) -> Mapping[str, float]:
        return MappingProxyType(
            {item.metric_id: item.candidate_value for item in self.objective_outcomes}
        )

    def outcome_by_id(self, metric_id: str) -> ObjectiveOutcome:
        for item in self.objective_outcomes:
            if item.metric_id == metric_id:
                return item
        raise KeyError(f"objective outcome is missing: {metric_id}")

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
            "evidence_refs": [item.semantic_payload() for item in self.evidence_refs],
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
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "evaluation_id": self.ref.object_id,
            "evaluation_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CandidateEvaluation:
        strict_keys(
            value,
            required={
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
                "evidence_refs",
                "claim_scope",
            },
            optional={"evaluation_id", "evaluation_fingerprint"},
            context="candidate evaluation",
        )
        status = value["status"]
        if status not in {
            "feasible",
            "process_infeasible",
            "invalid_request",
            "evaluation_error",
            "not_evaluated",
        }:
            raise ValueError("unsupported evaluation status")
        margin = value["minimum_normalized_margin"]
        evaluation = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            evaluation_version=identifier(
                value["evaluation_version"], context="evaluation_version"
            ),
            stage=identifier(value["stage"], context="stage"),
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
                ObjectiveOutcome.from_mapping(as_mapping(item, context="objective outcome"))
                for item in as_sequence(value["objective_outcomes"], context="objective_outcomes")
            ),
            metrics=numeric_mapping(value["metrics"], context="metrics"),
            constraints=tuple(
                ConstraintOutcome.from_mapping(as_mapping(item, context="constraint outcome"))
                for item in as_sequence(value["constraints"], context="constraints")
            ),
            minimum_normalized_margin=(
                None if margin is None else finite(margin, context="minimum_normalized_margin")
            ),
            normalized_action_l1=finite(
                value["normalized_action_l1"], context="normalized_action_l1"
            ),
            reason_codes=tuple(
                identifier(item, context="reason_code")
                for item in as_sequence(value["reason_codes"], context="reason_codes")
            ),
            evidence_refs=tuple(
                RunEvidenceRef.from_mapping(as_mapping(item, context="run evidence ref"))
                for item in as_sequence(value["evidence_refs"], context="evidence_refs")
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
