"""Strict RTO V2 preference, dynamic verification, and final result contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .common import (
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    digest,
    identifier,
    strict_keys,
    text,
)
from .models import CLAIM_SCOPE, ContractRef
from .multiobjective import RTO_V2_SCHEMA_VERSION
from .results_v2 import CandidateEvaluationV2, ObjectiveOutcomeV2

PreferenceSelectionStatusV2 = Literal["success", "no_static_feasible", "evaluation_error"]
DynamicVerificationStatusV2 = Literal[
    "success", "pareto_shortlist_dynamic_failed", "evaluation_error"
]
OptimizationResultStatusV2 = Literal[
    "success",
    "no_static_feasible",
    "pareto_shortlist_dynamic_failed",
    "feasible_not_publishable",
    "evaluation_error",
]


def _schema(value: str) -> None:
    if value != RTO_V2_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V2 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _refs(value: object, *, context: str) -> tuple[ContractRef, ...]:
    return tuple(
        ContractRef.from_mapping(as_mapping(item, context=f"{context} item"))
        for item in as_sequence(value, context=context)
    )


def _optional_ref(value: object, *, context: str) -> ContractRef | None:
    return None if value is None else ContractRef.from_mapping(as_mapping(value, context=context))


@dataclass(frozen=True)
class PreferenceSelectionV2:
    schema_version: str
    selection_version: str
    status: PreferenceSelectionStatusV2
    problem_ref: ContractRef
    context_ref: ContractRef
    pareto_search_ref: ContractRef
    preference_profile_ref: ContractRef
    ranked_pareto_refs: tuple[ContractRef, ...]
    shortlist_refs: tuple[ContractRef, ...]
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self,
            "selection_version",
            identifier(self.selection_version, context="selection_version"),
        )
        if self.status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported preference selection status")
        for name in (
            "problem_ref",
            "context_ref",
            "pareto_search_ref",
            "preference_profile_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        ranking = tuple(self.ranked_pareto_refs)
        shortlist = tuple(self.shortlist_refs)
        if any(not isinstance(item, ContractRef) for item in ranking + shortlist):
            raise TypeError("selection rankings must contain ContractRef values")
        if len(ranking) != len(set(ranking)) or len(shortlist) != len(set(shortlist)):
            raise ValueError("selection rankings must contain unique refs")
        if shortlist != ranking[: len(shortlist)]:
            raise ValueError("dynamic shortlist must be a prefix of the Pareto ranking")
        if len(shortlist) > 5:
            raise ValueError("RTO V2 dynamic shortlist cannot exceed five candidates")
        if self.status == "success" and (not ranking or not shortlist):
            raise ValueError("successful preference selection requires a shortlist")
        if self.status != "success" and (ranking or shortlist):
            raise ValueError("unsuccessful preference selection cannot expose a ranking")
        object.__setattr__(self, "ranked_pareto_refs", ranking)
        object.__setattr__(self, "shortlist_refs", shortlist)
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "selection_version": self.selection_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "pareto_search_ref": self.pareto_search_ref.as_dict(),
            "preference_profile_ref": self.preference_profile_ref.as_dict(),
            "ranked_pareto_refs": [item.as_dict() for item in self.ranked_pareto_refs],
            "shortlist_refs": [item.as_dict() for item in self.shortlist_refs],
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"preference-selection-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "selection_id": self.ref.object_id,
            "selection_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PreferenceSelectionV2:
        required = {
            "schema_version",
            "selection_version",
            "status",
            "problem_ref",
            "context_ref",
            "pareto_search_ref",
            "preference_profile_ref",
            "ranked_pareto_refs",
            "shortlist_refs",
            "termination_reason",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"selection_id", "selection_fingerprint"},
            context="preference selection V2",
        )
        status = value["status"]
        if status not in {"success", "no_static_feasible", "evaluation_error"}:
            raise ValueError("unsupported preference selection status")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            selection_version=identifier(value["selection_version"], context="selection_version"),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            pareto_search_ref=ContractRef.from_mapping(
                as_mapping(value["pareto_search_ref"], context="pareto_search_ref")
            ),
            preference_profile_ref=ContractRef.from_mapping(
                as_mapping(value["preference_profile_ref"], context="preference_profile_ref")
            ),
            ranked_pareto_refs=_refs(value["ranked_pareto_refs"], context="ranked_pareto_refs"),
            shortlist_refs=_refs(value["shortlist_refs"], context="shortlist_refs"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("selection_id") not in {None, result.ref.object_id}:
            raise ValueError("selection_id differs from selection content")
        supplied = value.get("selection_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="selection_fingerprint") != result.fingerprint
        ):
            raise ValueError("selection_fingerprint differs from selection content")
        return result


@dataclass(frozen=True)
class DynamicVerificationV2:
    schema_version: str
    verification_version: str
    status: DynamicVerificationStatusV2
    problem_ref: ContractRef
    context_ref: ContractRef
    selection_ref: ContractRef
    shortlist_refs: tuple[ContractRef, ...]
    evaluations: tuple[CandidateEvaluationV2, ...]
    selected_static_evaluation_ref: ContractRef | None
    selected_dynamic_evaluation_ref: ContractRef | None
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self,
            "verification_version",
            identifier(self.verification_version, context="verification_version"),
        )
        if self.status not in {
            "success",
            "pareto_shortlist_dynamic_failed",
            "evaluation_error",
        }:
            raise ValueError("unsupported dynamic verification status")
        for name in ("problem_ref", "context_ref", "selection_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        shortlist = tuple(self.shortlist_refs)
        evaluations = tuple(self.evaluations)
        if any(not isinstance(item, ContractRef) for item in shortlist):
            raise TypeError("shortlist_refs must contain ContractRef values")
        if any(not isinstance(item, CandidateEvaluationV2) for item in evaluations):
            raise TypeError("evaluations must contain CandidateEvaluationV2 values")
        if any(item.stage != "M4" for item in evaluations):
            raise ValueError("dynamic verification accepts only M4 evaluations")
        if tuple(item.proposal_ref for item in evaluations) != shortlist:
            raise ValueError("dynamic evaluations must align with the full shortlist")
        object.__setattr__(self, "shortlist_refs", shortlist)
        object.__setattr__(self, "evaluations", evaluations)
        selected = (
            self.selected_static_evaluation_ref,
            self.selected_dynamic_evaluation_ref,
        )
        if any(item is not None and not isinstance(item, ContractRef) for item in selected):
            raise TypeError("selected evaluation refs must be ContractRef values")
        if self.status == "success":
            if any(item is None for item in selected):
                raise ValueError("successful dynamic verification requires selected refs")
            if not any(item.status == "feasible" for item in evaluations):
                raise ValueError("successful verification requires a feasible M4 candidate")
        elif any(item is not None for item in selected):
            raise ValueError("unsuccessful verification cannot contain selected refs")
        if self.status == "pareto_shortlist_dynamic_failed" and any(
            item.status != "process_infeasible" for item in evaluations
        ):
            raise ValueError("dynamic-failed status requires only process failures")
        if self.status == "evaluation_error" and not any(
            item.status in {"invalid_request", "evaluation_error"} for item in evaluations
        ):
            raise ValueError("evaluation_error requires a system error evaluation")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "verification_version": self.verification_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "selection_ref": self.selection_ref.as_dict(),
            "shortlist_refs": [item.as_dict() for item in self.shortlist_refs],
            "evaluation_refs": [item.ref.as_dict() for item in self.evaluations],
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
        return {
            **self.fingerprint_payload(),
            "verification_id": self.ref.object_id,
            "verification_fingerprint": self.fingerprint,
            "evaluations": [item.as_dict() for item in self.evaluations],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DynamicVerificationV2:
        required = {
            "schema_version",
            "verification_version",
            "status",
            "problem_ref",
            "context_ref",
            "selection_ref",
            "shortlist_refs",
            "evaluation_refs",
            "selected_static_evaluation_ref",
            "selected_dynamic_evaluation_ref",
            "termination_reason",
            "claim_scope",
            "evaluations",
        }
        strict_keys(
            value,
            required=required,
            optional={"verification_id", "verification_fingerprint"},
            context="dynamic verification V2",
        )
        status = value["status"]
        if status not in {
            "success",
            "pareto_shortlist_dynamic_failed",
            "evaluation_error",
        }:
            raise ValueError("unsupported dynamic verification status")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            verification_version=identifier(
                value["verification_version"], context="verification_version"
            ),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            selection_ref=ContractRef.from_mapping(
                as_mapping(value["selection_ref"], context="selection_ref")
            ),
            shortlist_refs=_refs(value["shortlist_refs"], context="shortlist_refs"),
            evaluations=tuple(
                CandidateEvaluationV2.from_mapping(as_mapping(item, context="dynamic evaluation"))
                for item in as_sequence(value["evaluations"], context="evaluations")
            ),
            selected_static_evaluation_ref=_optional_ref(
                value["selected_static_evaluation_ref"],
                context="selected_static_evaluation_ref",
            ),
            selected_dynamic_evaluation_ref=_optional_ref(
                value["selected_dynamic_evaluation_ref"],
                context="selected_dynamic_evaluation_ref",
            ),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if _refs(value["evaluation_refs"], context="evaluation_refs") != tuple(
            item.ref for item in result.evaluations
        ):
            raise ValueError("evaluation_refs differ from embedded evaluations")
        if value.get("verification_id") not in {None, result.ref.object_id}:
            raise ValueError("verification_id differs from verification content")
        supplied = value.get("verification_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="verification_fingerprint") != result.fingerprint
        ):
            raise ValueError("verification_fingerprint differs from verification content")
        return result


@dataclass(frozen=True)
class OptimizationResultV2:
    schema_version: str
    result_version: str
    status: OptimizationResultStatusV2
    problem_ref: ContractRef
    context_ref: ContractRef
    pareto_search_ref: ContractRef
    preference_selection_ref: ContractRef
    dynamic_verification_ref: ContractRef | None
    pareto_refs: tuple[ContractRef, ...]
    selected_proposal_ref: ContractRef | None
    selected_static_evaluation_ref: ContractRef | None
    selected_dynamic_evaluation_ref: ContractRef | None
    selected_objectives: tuple[ObjectiveOutcomeV2, ...]
    publishability_profile_ref: ContractRef
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
            "pareto_shortlist_dynamic_failed",
            "feasible_not_publishable",
            "evaluation_error",
        }:
            raise ValueError("unsupported optimization result status")
        for name in (
            "problem_ref",
            "context_ref",
            "pareto_search_ref",
            "preference_selection_ref",
            "publishability_profile_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        if self.dynamic_verification_ref is not None and not isinstance(
            self.dynamic_verification_ref, ContractRef
        ):
            raise TypeError("dynamic_verification_ref must be a ContractRef")
        pareto = tuple(self.pareto_refs)
        if any(not isinstance(item, ContractRef) for item in pareto):
            raise TypeError("pareto_refs must contain ContractRef values")
        object.__setattr__(self, "pareto_refs", pareto)
        selected_refs = (
            self.selected_proposal_ref,
            self.selected_static_evaluation_ref,
            self.selected_dynamic_evaluation_ref,
        )
        if any(item is not None and not isinstance(item, ContractRef) for item in selected_refs):
            raise TypeError("selected refs must be ContractRef values")
        objectives = tuple(self.selected_objectives)
        if any(not isinstance(item, ObjectiveOutcomeV2) for item in objectives):
            raise TypeError("selected_objectives must contain ObjectiveOutcomeV2 values")
        object.__setattr__(self, "selected_objectives", objectives)
        if not isinstance(self.publishable, bool):
            raise TypeError("publishable must be boolean")
        selected_status = self.status in {"success", "feasible_not_publishable"}
        if selected_status:
            if any(item is None for item in selected_refs) or not objectives:
                raise ValueError("selected result requires refs and objective summaries")
            if self.dynamic_verification_ref is None:
                raise ValueError("selected result requires dynamic verification")
        elif any(item is not None for item in selected_refs) or objectives:
            raise ValueError("unselected result cannot contain selected values")
        if self.publishable != (self.status == "success"):
            raise ValueError("publishable must be true only for success")
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
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "pareto_search_ref": self.pareto_search_ref.as_dict(),
            "preference_selection_ref": self.preference_selection_ref.as_dict(),
            "dynamic_verification_ref": (
                None
                if self.dynamic_verification_ref is None
                else self.dynamic_verification_ref.as_dict()
            ),
            "pareto_refs": [item.as_dict() for item in self.pareto_refs],
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
            "selected_objectives": [item.as_dict() for item in self.selected_objectives],
            "publishability_profile_ref": self.publishability_profile_ref.as_dict(),
            "publishable": self.publishable,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"optimization-result-v2-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "result_id": self.ref.object_id,
            "result_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationResultV2:
        required = {
            "schema_version",
            "result_version",
            "status",
            "problem_ref",
            "context_ref",
            "pareto_search_ref",
            "preference_selection_ref",
            "dynamic_verification_ref",
            "pareto_refs",
            "selected_proposal_ref",
            "selected_static_evaluation_ref",
            "selected_dynamic_evaluation_ref",
            "selected_objectives",
            "publishability_profile_ref",
            "publishable",
            "termination_reason",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"result_id", "result_fingerprint"},
            context="optimization result V2",
        )
        status = value["status"]
        if status not in {
            "success",
            "no_static_feasible",
            "pareto_shortlist_dynamic_failed",
            "feasible_not_publishable",
            "evaluation_error",
        }:
            raise ValueError("unsupported optimization result status")
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
            pareto_search_ref=ContractRef.from_mapping(
                as_mapping(value["pareto_search_ref"], context="pareto_search_ref")
            ),
            preference_selection_ref=ContractRef.from_mapping(
                as_mapping(
                    value["preference_selection_ref"],
                    context="preference_selection_ref",
                )
            ),
            dynamic_verification_ref=_optional_ref(
                value["dynamic_verification_ref"], context="dynamic_verification_ref"
            ),
            pareto_refs=_refs(value["pareto_refs"], context="pareto_refs"),
            selected_proposal_ref=_optional_ref(
                value["selected_proposal_ref"], context="selected_proposal_ref"
            ),
            selected_static_evaluation_ref=_optional_ref(
                value["selected_static_evaluation_ref"],
                context="selected_static_evaluation_ref",
            ),
            selected_dynamic_evaluation_ref=_optional_ref(
                value["selected_dynamic_evaluation_ref"],
                context="selected_dynamic_evaluation_ref",
            ),
            selected_objectives=tuple(
                ObjectiveOutcomeV2.from_mapping(as_mapping(item, context="selected objective"))
                for item in as_sequence(value["selected_objectives"], context="selected_objectives")
            ),
            publishability_profile_ref=ContractRef.from_mapping(
                as_mapping(
                    value["publishability_profile_ref"],
                    context="publishability_profile_ref",
                )
            ),
            publishable=boolean(value["publishable"], context="publishable"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("result_id") not in {None, result.ref.object_id}:
            raise ValueError("result_id differs from result content")
        supplied = value.get("result_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="result_fingerprint") != result.fingerprint
        ):
            raise ValueError("result_fingerprint differs from result content")
        return result
