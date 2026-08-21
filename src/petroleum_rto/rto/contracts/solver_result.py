"""Common solver output for ordered scalar and layered Pareto solutions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from .candidate import CandidateEvaluation, CandidateProposal
from .common import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    identifier,
    integer,
    strict_keys,
    text,
)
from .problem import ENGINEERING_CLAIM_SCOPE
from .reference import ContractRef

SOLVER_RESULT_SCHEMA_VERSION: Final[str] = "2.0.0"
SolverResultStatus = Literal[
    "success",
    "no_static_feasible",
    "evaluation_error",
    "unsupported_problem",
]
SolutionRepresentation = Literal["ordered", "layered"]


@dataclass(frozen=True)
class SolutionGroup:
    """One ordered rank or non-dominated layer of evaluation references."""

    rank: int
    evaluation_refs: tuple[ContractRef, ...]

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("solution rank must be a positive integer")
        refs = tuple(self.evaluation_refs)
        if not refs or any(not isinstance(item, ContractRef) for item in refs):
            raise TypeError("solution group requires ContractRef values")
        if len(refs) != len(set(refs)):
            raise ValueError("solution group references must be unique")
        object.__setattr__(self, "evaluation_refs", refs)

    def as_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "evaluation_refs": [item.as_dict() for item in self.evaluation_refs],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SolutionGroup:
        strict_keys(
            value,
            required={"rank", "evaluation_refs"},
            context="solution group",
        )
        return cls(
            rank=integer(value["rank"], context="rank", minimum=1),
            evaluation_refs=tuple(
                ContractRef.from_mapping(as_mapping(item, context="evaluation ref"))
                for item in as_sequence(value["evaluation_refs"], context="evaluation_refs")
            ),
        )


@dataclass(frozen=True)
class SolverResult:
    """Objective-count-neutral in-memory and serializable result from a solver plugin."""

    schema_version: str
    result_version: str
    status: SolverResultStatus
    problem_ref: ContractRef
    solver_ref: ContractRef
    proposals: tuple[CandidateProposal, ...]
    evaluations: tuple[CandidateEvaluation, ...]
    solution_representation: SolutionRepresentation
    solution_groups: tuple[SolutionGroup, ...]
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != SOLVER_RESULT_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the solver result contract")
        if self.claim_scope != ENGINEERING_CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        object.__setattr__(
            self, "result_version", identifier(self.result_version, context="result_version")
        )
        if self.status not in {
            "success",
            "no_static_feasible",
            "evaluation_error",
            "unsupported_problem",
        }:
            raise ValueError("unsupported solver result status")
        if self.solution_representation not in {"ordered", "layered"}:
            raise ValueError("unsupported solution representation")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.solver_ref, ContractRef
        ):
            raise TypeError("solver result refs must be ContractRef")
        proposals = tuple(self.proposals)
        evaluations = tuple(self.evaluations)
        groups = tuple(self.solution_groups)
        if any(not isinstance(item, CandidateProposal) for item in proposals):
            raise TypeError("proposals must contain CandidateProposal")
        if any(not isinstance(item, CandidateEvaluation) for item in evaluations):
            raise TypeError("evaluations must contain CandidateEvaluation")
        if any(not isinstance(item, SolutionGroup) for item in groups):
            raise TypeError("solution_groups must contain SolutionGroup")
        if len(proposals) != len(evaluations):
            raise ValueError("each solver proposal requires one evaluation")
        proposal_refs = tuple(item.ref for item in proposals)
        if len(proposal_refs) != len(set(proposal_refs)):
            raise ValueError("solver proposals must be unique")
        if any(
            evaluation.proposal_ref != proposal.ref
            for proposal, evaluation in zip(proposals, evaluations, strict=True)
        ):
            raise ValueError("solver evaluations must align with proposals")
        if any(proposal.problem_ref != self.problem_ref for proposal in proposals):
            raise ValueError("solver proposals must reference the solver problem")
        if len({proposal.context_ref for proposal in proposals}) > 1:
            raise ValueError("solver proposals must share one operating context")
        if any(
            evaluation.problem_ref != self.problem_ref
            or evaluation.context_ref != proposal.context_ref
            for proposal, evaluation in zip(proposals, evaluations, strict=True)
        ):
            raise ValueError("solver evaluations must reference the proposal problem and context")
        if any(evaluation.stage != "M2" for evaluation in evaluations):
            raise ValueError("solver results may contain only M2 evaluations")
        evaluation_refs = {item.ref for item in evaluations}
        group_refs = tuple(ref for group in groups for ref in group.evaluation_refs)
        if not set(group_refs).issubset(evaluation_refs):
            raise ValueError("solution groups reference unknown evaluations")
        if len(group_refs) != len(set(group_refs)):
            raise ValueError("an evaluation cannot appear in multiple solution groups")
        evaluation_by_ref = {item.ref: item for item in evaluations}
        if any(evaluation_by_ref[ref].status != "feasible" for ref in group_refs):
            raise ValueError("solution groups may contain only feasible evaluations")
        if tuple(group.rank for group in groups) != tuple(range(1, len(groups) + 1)):
            raise ValueError("solution group ranks must be contiguous")
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "evaluations", evaluations)
        object.__setattr__(self, "solution_groups", groups)
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )
        if self.status == "success" and not groups:
            raise ValueError("successful solver result requires solution groups")
        if self.status != "success" and groups:
            raise ValueError("unsuccessful solver result cannot expose solution groups")
        statuses = {item.status for item in evaluations}
        system_failures = {"invalid_request", "evaluation_error", "not_evaluated"}
        if self.status == "success" and statuses & system_failures:
            raise ValueError("successful solver result cannot contain evaluation failures")
        if self.status == "no_static_feasible" and (
            "feasible" in statuses or statuses & system_failures
        ):
            raise ValueError("no_static_feasible requires only process-infeasible evaluations")
        if self.status == "evaluation_error" and not statuses & system_failures:
            raise ValueError("evaluation_error requires an evaluation failure")
        if self.status == "unsupported_problem" and (proposals or evaluations):
            raise ValueError("unsupported_problem cannot contain solver artifacts")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result_version": self.result_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "solver_ref": self.solver_ref.as_dict(),
            "proposal_refs": [item.ref.as_dict() for item in self.proposals],
            "evaluation_refs": [item.ref.as_dict() for item in self.evaluations],
            "solution_representation": self.solution_representation,
            "solution_groups": [item.as_dict() for item in self.solution_groups],
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "proposals": [item.as_dict() for item in self.proposals],
            "evaluations": [item.as_dict() for item in self.evaluations],
            "solver_result_id": self.ref.object_id,
            "solver_result_fingerprint": self.fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"solver-result-{self.fingerprint[:16]}", self.fingerprint)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SolverResult:
        strict_keys(
            value,
            required={
                "schema_version",
                "result_version",
                "status",
                "problem_ref",
                "solver_ref",
                "proposal_refs",
                "evaluation_refs",
                "proposals",
                "evaluations",
                "solution_representation",
                "solution_groups",
                "termination_reason",
                "claim_scope",
            },
            optional={"solver_result_id", "solver_result_fingerprint"},
            context="solver result",
        )
        status = value["status"]
        if status not in {
            "success",
            "no_static_feasible",
            "evaluation_error",
            "unsupported_problem",
        }:
            raise ValueError("unsupported solver result status")
        representation = value["solution_representation"]
        if representation not in {"ordered", "layered"}:
            raise ValueError("unsupported solution representation")
        proposals = tuple(
            CandidateProposal.from_mapping(as_mapping(item, context="candidate proposal"))
            for item in as_sequence(value["proposals"], context="proposals")
        )
        evaluations = tuple(
            CandidateEvaluation.from_mapping(as_mapping(item, context="candidate evaluation"))
            for item in as_sequence(value["evaluations"], context="evaluations")
        )
        proposal_refs = tuple(
            ContractRef.from_mapping(as_mapping(item, context="proposal ref"))
            for item in as_sequence(value["proposal_refs"], context="proposal_refs")
        )
        evaluation_refs = tuple(
            ContractRef.from_mapping(as_mapping(item, context="evaluation ref"))
            for item in as_sequence(value["evaluation_refs"], context="evaluation_refs")
        )
        if proposal_refs != tuple(item.ref for item in proposals):
            raise ValueError("proposal_refs differ from nested proposal content")
        if evaluation_refs != tuple(item.ref for item in evaluations):
            raise ValueError("evaluation_refs differ from nested evaluation content")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            result_version=identifier(value["result_version"], context="result_version"),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            solver_ref=ContractRef.from_mapping(
                as_mapping(value["solver_ref"], context="solver_ref")
            ),
            proposals=proposals,
            evaluations=evaluations,
            solution_representation=representation,
            solution_groups=tuple(
                SolutionGroup.from_mapping(as_mapping(item, context="solution group"))
                for item in as_sequence(value["solution_groups"], context="solution_groups")
            ),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("solver_result_id") not in {None, result.ref.object_id}:
            raise ValueError("solver_result_id differs from solver result content")
        supplied = value.get("solver_result_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="solver_result_fingerprint") != result.fingerprint
        ):
            raise ValueError("solver_result_fingerprint differs from solver result content")
        return result
