"""Neutral static-selection, publishability, and finalization contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
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
    strict_keys,
    text,
)
from .problem import ENGINEERING_CLAIM_SCOPE, ResultMode
from .reference import ContractRef

FINALIZATION_SCHEMA_VERSION: Final[str] = "1.0.0"

StaticSelectionStatus = Literal[
    "ready",
    "no_feasible",
    "invalid_request",
    "evaluation_error",
    "unsupported_problem",
]
PublishabilityStatus = Literal["publishable", "not_publishable"]
FinalizationStatus = Literal[
    "success",
    "feasible_not_publishable",
    "no_feasible",
    "no_verified_candidate",
    "invalid_request",
    "evaluation_error",
    "unsupported_problem",
]
GuardrailOperator = Literal["eq", "le", "ge"]


def _require_schema_and_claim(schema_version: str, claim_scope: str) -> None:
    if schema_version != FINALIZATION_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the finalization contract")
    if claim_scope != ENGINEERING_CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _refs(value: object, *, context: str) -> tuple[ContractRef, ...]:
    return tuple(
        ContractRef.from_mapping(as_mapping(item, context=f"{context} item"))
        for item in as_sequence(value, context=context)
    )


def _optional_ref(value: object, *, context: str) -> ContractRef | None:
    if value is None:
        return None
    return ContractRef.from_mapping(as_mapping(value, context=context))


def _validate_supplied_identity(
    value: Mapping[str, object],
    *,
    object_field: str,
    fingerprint_field: str,
    ref: ContractRef,
) -> None:
    if value.get(object_field) not in {None, ref.object_id}:
        raise ValueError(f"{object_field} differs from contract content")
    supplied = value.get(fingerprint_field)
    if supplied is not None and digest(supplied, context=fingerprint_field) != ref.fingerprint:
        raise ValueError(f"{fingerprint_field} differs from contract content")


@dataclass(frozen=True)
class StaticPreferenceSelection:
    """Deterministic static ranking and its dynamic-verification budget prefix."""

    schema_version: str
    selection_version: str
    status: StaticSelectionStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    solver_result_ref: ContractRef
    shortlist_limit: int
    ranked_proposal_refs: tuple[ContractRef, ...]
    shortlist_proposal_refs: tuple[ContractRef, ...]
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _require_schema_and_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "selection_version",
            identifier(self.selection_version, context="selection_version"),
        )
        if self.status not in {
            "ready",
            "no_feasible",
            "invalid_request",
            "evaluation_error",
            "unsupported_problem",
        }:
            raise ValueError("unsupported static selection status")
        for name in ("problem_ref", "context_ref", "solver_result_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        object.__setattr__(
            self,
            "shortlist_limit",
            integer(self.shortlist_limit, context="shortlist_limit", minimum=1),
        )
        ranking = tuple(self.ranked_proposal_refs)
        shortlist = tuple(self.shortlist_proposal_refs)
        if any(not isinstance(item, ContractRef) for item in (*ranking, *shortlist)):
            raise TypeError("static rankings must contain ContractRef values")
        if len(ranking) != len(set(ranking)) or len(shortlist) != len(set(shortlist)):
            raise ValueError("static rankings must contain unique proposal refs")
        if self.status == "ready":
            if not ranking:
                raise ValueError("ready static selection requires a ranking")
            expected = ranking[: min(self.shortlist_limit, len(ranking))]
            if shortlist != expected:
                raise ValueError("static shortlist must be the configured ranking prefix")
        elif ranking or shortlist:
            raise ValueError("non-ready static selection cannot expose candidate rankings")
        object.__setattr__(self, "ranked_proposal_refs", ranking)
        object.__setattr__(self, "shortlist_proposal_refs", shortlist)
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
            "solver_result_ref": self.solver_result_ref.as_dict(),
            "shortlist_limit": self.shortlist_limit,
            "ranked_proposal_refs": [item.as_dict() for item in self.ranked_proposal_refs],
            "shortlist_proposal_refs": [item.as_dict() for item in self.shortlist_proposal_refs],
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"static-selection-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "selection_id": self.ref.object_id,
            "selection_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StaticPreferenceSelection:
        strict_keys(
            value,
            required={
                "schema_version",
                "selection_version",
                "status",
                "problem_ref",
                "context_ref",
                "solver_result_ref",
                "shortlist_limit",
                "ranked_proposal_refs",
                "shortlist_proposal_refs",
                "termination_reason",
                "claim_scope",
            },
            optional={"selection_id", "selection_fingerprint"},
            context="static preference selection",
        )
        status = value["status"]
        if status not in {
            "ready",
            "no_feasible",
            "invalid_request",
            "evaluation_error",
            "unsupported_problem",
        }:
            raise ValueError("unsupported static selection status")
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
            solver_result_ref=ContractRef.from_mapping(
                as_mapping(value["solver_result_ref"], context="solver_result_ref")
            ),
            shortlist_limit=integer(value["shortlist_limit"], context="shortlist_limit", minimum=1),
            ranked_proposal_refs=_refs(
                value["ranked_proposal_refs"], context="ranked_proposal_refs"
            ),
            shortlist_proposal_refs=_refs(
                value["shortlist_proposal_refs"], context="shortlist_proposal_refs"
            ),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        _validate_supplied_identity(
            value,
            object_field="selection_id",
            fingerprint_field="selection_fingerprint",
            ref=result.ref,
        )
        return result


@dataclass(frozen=True)
class PublishabilityOutcome:
    """One post-selection rule result, separate from process feasibility."""

    guardrail_id: str
    priority: int
    metric_id: str
    operator: GuardrailOperator
    limit: float
    observed_value: float | None
    passed: bool
    reason_code: str

    def __post_init__(self) -> None:
        for name in ("guardrail_id", "metric_id", "reason_code"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "priority", integer(self.priority, context="priority"))
        if self.operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported publishability operator")
        object.__setattr__(self, "limit", finite(self.limit, context="limit"))
        if self.observed_value is not None:
            object.__setattr__(
                self,
                "observed_value",
                finite(self.observed_value, context="observed_value"),
            )
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean")
        if self.observed_value is None:
            if self.passed:
                raise ValueError("missing publishability evidence cannot pass")
            return
        expected = (
            self.observed_value <= self.limit
            if self.operator == "le"
            else self.observed_value >= self.limit
            if self.operator == "ge"
            else math.isclose(
                self.observed_value,
                self.limit,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        if self.passed != expected:
            raise ValueError("publishability passed flag differs from its explicit rule")

    def as_dict(self) -> dict[str, object]:
        return {
            "guardrail_id": self.guardrail_id,
            "priority": self.priority,
            "metric_id": self.metric_id,
            "operator": self.operator,
            "limit": self.limit,
            "observed_value": self.observed_value,
            "passed": self.passed,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PublishabilityOutcome:
        strict_keys(
            value,
            required={
                "guardrail_id",
                "priority",
                "metric_id",
                "operator",
                "limit",
                "observed_value",
                "passed",
                "reason_code",
            },
            context="publishability outcome",
        )
        operator = value["operator"]
        if operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported publishability operator")
        observed_raw = value["observed_value"]
        return cls(
            guardrail_id=identifier(value["guardrail_id"], context="guardrail_id"),
            priority=integer(value["priority"], context="priority"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            operator=operator,
            limit=finite(value["limit"], context="limit"),
            observed_value=(
                None if observed_raw is None else finite(observed_raw, context="observed_value")
            ),
            passed=boolean(value["passed"], context="passed"),
            reason_code=identifier(value["reason_code"], context="reason_code"),
        )


@dataclass(frozen=True)
class PublishabilityAssessment:
    """Post-selection policy assessment that cannot alter feasibility."""

    schema_version: str
    assessment_version: str
    status: PublishabilityStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    capability_catalog_ref: ContractRef
    system_policy_ref: ContractRef
    selected_proposal_ref: ContractRef
    selected_static_evaluation_ref: ContractRef
    outcomes: tuple[PublishabilityOutcome, ...]
    publishable: bool
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _require_schema_and_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "assessment_version",
            identifier(self.assessment_version, context="assessment_version"),
        )
        if self.status not in {"publishable", "not_publishable"}:
            raise ValueError("unsupported publishability status")
        for name in (
            "problem_ref",
            "context_ref",
            "capability_catalog_ref",
            "system_policy_ref",
            "selected_proposal_ref",
            "selected_static_evaluation_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        outcomes = tuple(self.outcomes)
        if not outcomes or any(not isinstance(item, PublishabilityOutcome) for item in outcomes):
            raise TypeError("publishability assessment requires outcomes")
        ids = tuple(item.guardrail_id for item in outcomes)
        priorities = tuple(item.priority for item in outcomes)
        if len(ids) != len(set(ids)) or len(priorities) != len(set(priorities)):
            raise ValueError("publishability outcomes require unique ids and priorities")
        if priorities != tuple(sorted(priorities)):
            raise ValueError("publishability outcomes must be priority ordered")
        if not isinstance(self.publishable, bool):
            raise TypeError("publishable must be boolean")
        expected = all(item.passed for item in outcomes)
        if self.publishable != expected or self.publishable != (self.status == "publishable"):
            raise ValueError("publishability status differs from its rule outcomes")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "assessment_version": self.assessment_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "capability_catalog_ref": self.capability_catalog_ref.as_dict(),
            "system_policy_ref": self.system_policy_ref.as_dict(),
            "selected_proposal_ref": self.selected_proposal_ref.as_dict(),
            "selected_static_evaluation_ref": self.selected_static_evaluation_ref.as_dict(),
            "outcomes": [item.as_dict() for item in self.outcomes],
            "publishable": self.publishable,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"publishability-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "assessment_id": self.ref.object_id,
            "assessment_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PublishabilityAssessment:
        strict_keys(
            value,
            required={
                "schema_version",
                "assessment_version",
                "status",
                "problem_ref",
                "context_ref",
                "capability_catalog_ref",
                "system_policy_ref",
                "selected_proposal_ref",
                "selected_static_evaluation_ref",
                "outcomes",
                "publishable",
                "termination_reason",
                "claim_scope",
            },
            optional={"assessment_id", "assessment_fingerprint"},
            context="publishability assessment",
        )
        status = value["status"]
        if status not in {"publishable", "not_publishable"}:
            raise ValueError("unsupported publishability status")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            assessment_version=identifier(
                value["assessment_version"], context="assessment_version"
            ),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            capability_catalog_ref=ContractRef.from_mapping(
                as_mapping(value["capability_catalog_ref"], context="capability_catalog_ref")
            ),
            system_policy_ref=ContractRef.from_mapping(
                as_mapping(value["system_policy_ref"], context="system_policy_ref")
            ),
            selected_proposal_ref=ContractRef.from_mapping(
                as_mapping(value["selected_proposal_ref"], context="selected_proposal_ref")
            ),
            selected_static_evaluation_ref=ContractRef.from_mapping(
                as_mapping(
                    value["selected_static_evaluation_ref"],
                    context="selected_static_evaluation_ref",
                )
            ),
            outcomes=tuple(
                PublishabilityOutcome.from_mapping(
                    as_mapping(item, context="publishability outcome")
                )
                for item in as_sequence(value["outcomes"], context="outcomes")
            ),
            publishable=boolean(value["publishable"], context="publishable"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        _validate_supplied_identity(
            value,
            object_field="assessment_id",
            fingerprint_field="assessment_fingerprint",
            ref=result.ref,
        )
        return result


@dataclass(frozen=True)
class FinalizationResult:
    """One objective-count-neutral final selection and its evidence references."""

    schema_version: str
    result_version: str
    status: FinalizationStatus
    problem_ref: ContractRef
    context_ref: ContractRef
    solver_result_ref: ContractRef
    static_selection_ref: ContractRef
    result_mode: ResultMode
    maximum_returned_candidates: int
    ranked_proposal_refs: tuple[ContractRef, ...]
    returned_proposal_refs: tuple[ContractRef, ...]
    shortlist_proposal_refs: tuple[ContractRef, ...]
    dynamic_proposal_refs: tuple[ContractRef, ...]
    dynamic_evaluation_refs: tuple[ContractRef, ...]
    selected_proposal_ref: ContractRef | None
    selected_static_evaluation_ref: ContractRef | None
    selected_dynamic_evaluation_ref: ContractRef | None
    publishability_assessment_ref: ContractRef | None
    publishable: bool
    termination_reason: str
    claim_scope: str

    def __post_init__(self) -> None:
        _require_schema_and_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "result_version",
            identifier(self.result_version, context="result_version"),
        )
        if self.status not in {
            "success",
            "feasible_not_publishable",
            "no_feasible",
            "no_verified_candidate",
            "invalid_request",
            "evaluation_error",
            "unsupported_problem",
        }:
            raise ValueError("unsupported finalization status")
        for name in (
            "problem_ref",
            "context_ref",
            "solver_result_ref",
            "static_selection_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        if self.result_mode not in {
            "selected",
            "ranked-and-selected",
            "pareto-and-selected",
        }:
            raise ValueError("unsupported finalization result mode")
        object.__setattr__(
            self,
            "maximum_returned_candidates",
            integer(
                self.maximum_returned_candidates,
                context="maximum_returned_candidates",
                minimum=1,
            ),
        )
        ranking = tuple(self.ranked_proposal_refs)
        returned = tuple(self.returned_proposal_refs)
        shortlist = tuple(self.shortlist_proposal_refs)
        dynamic_proposals = tuple(self.dynamic_proposal_refs)
        dynamic_evaluations = tuple(self.dynamic_evaluation_refs)
        all_refs = (
            *ranking,
            *returned,
            *shortlist,
            *dynamic_proposals,
            *dynamic_evaluations,
        )
        if any(not isinstance(item, ContractRef) for item in all_refs):
            raise TypeError("finalization rankings and evaluations must contain ContractRef")
        for name, values in (
            ("ranking", ranking),
            ("returned proposals", returned),
            ("shortlist", shortlist),
            ("dynamic proposals", dynamic_proposals),
            ("dynamic evaluations", dynamic_evaluations),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique refs")
        if shortlist != ranking[: len(shortlist)]:
            raise ValueError("finalization shortlist must be a ranking prefix")
        if len(returned) > self.maximum_returned_candidates:
            raise ValueError("returned proposals exceed the requested output limit")
        if self.result_mode != "selected" and returned != ranking[: len(returned)]:
            raise ValueError("alternative-returning modes must use a ranking prefix")
        if dynamic_proposals != shortlist[: len(dynamic_proposals)]:
            raise ValueError("dynamic proposals must be a shortlist prefix")
        if len(dynamic_proposals) != len(dynamic_evaluations):
            raise ValueError("dynamic proposal and evaluation refs must align")
        object.__setattr__(self, "ranked_proposal_refs", ranking)
        object.__setattr__(self, "returned_proposal_refs", returned)
        object.__setattr__(self, "shortlist_proposal_refs", shortlist)
        object.__setattr__(self, "dynamic_proposal_refs", dynamic_proposals)
        object.__setattr__(self, "dynamic_evaluation_refs", dynamic_evaluations)

        selected = (
            self.selected_proposal_ref,
            self.selected_static_evaluation_ref,
            self.selected_dynamic_evaluation_ref,
        )
        if any(item is not None and not isinstance(item, ContractRef) for item in selected):
            raise TypeError("selected refs must be ContractRef values")
        if self.publishability_assessment_ref is not None and not isinstance(
            self.publishability_assessment_ref, ContractRef
        ):
            raise TypeError("publishability_assessment_ref must be ContractRef")
        selected_status = self.status in {"success", "feasible_not_publishable"}
        if selected_status:
            if any(item is None for item in selected):
                raise ValueError("selected finalization requires all selected refs")
            if self.publishability_assessment_ref is None:
                raise ValueError("selected finalization requires publishability assessment")
            if self.selected_proposal_ref not in dynamic_proposals:
                raise ValueError("selected proposal must have dynamic verification")
            selected_index = dynamic_proposals.index(self.selected_proposal_ref)
            if self.selected_dynamic_evaluation_ref != dynamic_evaluations[selected_index]:
                raise ValueError(
                    "selected dynamic evaluation must align with the selected proposal"
                )
        elif any(item is not None for item in selected):
            raise ValueError("unselected finalization cannot contain selected refs")
        elif self.publishability_assessment_ref is not None:
            raise ValueError("unselected finalization cannot contain publishability assessment")
        expected_returned: tuple[ContractRef, ...]
        if self.result_mode == "selected":
            expected_returned = (
                () if self.selected_proposal_ref is None else (self.selected_proposal_ref,)
            )
            if returned != expected_returned:
                raise ValueError("selected mode must return exactly the selected proposal")
        else:
            expected_returned = ranking[: min(self.maximum_returned_candidates, len(ranking))]
            if returned != expected_returned:
                raise ValueError("alternative-returning mode must apply the output limit")
        if self.status == "no_verified_candidate" and (
            not ranking or not shortlist or not dynamic_evaluations
        ):
            raise ValueError("no_verified_candidate requires attempted dynamic verification")
        if self.status in {"no_feasible", "unsupported_problem"} and (
            ranking or shortlist or dynamic_evaluations
        ):
            raise ValueError(f"{self.status} cannot expose candidate rankings")
        if not isinstance(self.publishable, bool):
            raise TypeError("publishable must be boolean")
        if self.publishable != (self.status == "success"):
            raise ValueError("publishable must be true only for success")
        object.__setattr__(
            self,
            "termination_reason",
            identifier(self.termination_reason, context="termination_reason"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        def optional(value: ContractRef | None) -> dict[str, str] | None:
            return None if value is None else value.as_dict()

        return {
            "schema_version": self.schema_version,
            "result_version": self.result_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "solver_result_ref": self.solver_result_ref.as_dict(),
            "static_selection_ref": self.static_selection_ref.as_dict(),
            "result_mode": self.result_mode,
            "maximum_returned_candidates": self.maximum_returned_candidates,
            "ranked_proposal_refs": [item.as_dict() for item in self.ranked_proposal_refs],
            "returned_proposal_refs": [item.as_dict() for item in self.returned_proposal_refs],
            "shortlist_proposal_refs": [item.as_dict() for item in self.shortlist_proposal_refs],
            "dynamic_proposal_refs": [item.as_dict() for item in self.dynamic_proposal_refs],
            "dynamic_evaluation_refs": [item.as_dict() for item in self.dynamic_evaluation_refs],
            "selected_proposal_ref": optional(self.selected_proposal_ref),
            "selected_static_evaluation_ref": optional(self.selected_static_evaluation_ref),
            "selected_dynamic_evaluation_ref": optional(self.selected_dynamic_evaluation_ref),
            "publishability_assessment_ref": optional(self.publishability_assessment_ref),
            "publishable": self.publishable,
            "termination_reason": self.termination_reason,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"finalization-{self.fingerprint[:16]}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "result_id": self.ref.object_id,
            "result_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FinalizationResult:
        strict_keys(
            value,
            required={
                "schema_version",
                "result_version",
                "status",
                "problem_ref",
                "context_ref",
                "solver_result_ref",
                "static_selection_ref",
                "result_mode",
                "maximum_returned_candidates",
                "ranked_proposal_refs",
                "returned_proposal_refs",
                "shortlist_proposal_refs",
                "dynamic_proposal_refs",
                "dynamic_evaluation_refs",
                "selected_proposal_ref",
                "selected_static_evaluation_ref",
                "selected_dynamic_evaluation_ref",
                "publishability_assessment_ref",
                "publishable",
                "termination_reason",
                "claim_scope",
            },
            optional={"result_id", "result_fingerprint"},
            context="finalization result",
        )
        status = value["status"]
        if status not in {
            "success",
            "feasible_not_publishable",
            "no_feasible",
            "no_verified_candidate",
            "invalid_request",
            "evaluation_error",
            "unsupported_problem",
        }:
            raise ValueError("unsupported finalization status")
        result_mode = value["result_mode"]
        if result_mode not in {
            "selected",
            "ranked-and-selected",
            "pareto-and-selected",
        }:
            raise ValueError("unsupported finalization result mode")
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
            solver_result_ref=ContractRef.from_mapping(
                as_mapping(value["solver_result_ref"], context="solver_result_ref")
            ),
            static_selection_ref=ContractRef.from_mapping(
                as_mapping(value["static_selection_ref"], context="static_selection_ref")
            ),
            result_mode=result_mode,
            maximum_returned_candidates=integer(
                value["maximum_returned_candidates"],
                context="maximum_returned_candidates",
                minimum=1,
            ),
            ranked_proposal_refs=_refs(
                value["ranked_proposal_refs"], context="ranked_proposal_refs"
            ),
            returned_proposal_refs=_refs(
                value["returned_proposal_refs"], context="returned_proposal_refs"
            ),
            shortlist_proposal_refs=_refs(
                value["shortlist_proposal_refs"], context="shortlist_proposal_refs"
            ),
            dynamic_proposal_refs=_refs(
                value["dynamic_proposal_refs"], context="dynamic_proposal_refs"
            ),
            dynamic_evaluation_refs=_refs(
                value["dynamic_evaluation_refs"], context="dynamic_evaluation_refs"
            ),
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
            publishability_assessment_ref=_optional_ref(
                value["publishability_assessment_ref"],
                context="publishability_assessment_ref",
            ),
            publishable=boolean(value["publishable"], context="publishable"),
            termination_reason=identifier(
                value["termination_reason"], context="termination_reason"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        _validate_supplied_identity(
            value,
            object_field="result_id",
            fingerprint_field="result_fingerprint",
            ref=result.ref,
        )
        return result


__all__ = [
    "FINALIZATION_SCHEMA_VERSION",
    "FinalizationResult",
    "FinalizationStatus",
    "PublishabilityAssessment",
    "PublishabilityOutcome",
    "PublishabilityStatus",
    "StaticPreferenceSelection",
    "StaticSelectionStatus",
]
