"""Objective-count-neutral optimization problem contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from .common import (
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    finite,
    identifier,
    integer,
    strict_keys,
    text,
)
from .reference import ContractRef

OPTIMIZATION_PROBLEM_SCHEMA_ID: Final[str] = "optimization-problem"
OPTIMIZATION_PROBLEM_SCHEMA_VERSION: Final[str] = "2.0.0"
ENGINEERING_CLAIM_SCOPE: Final[str] = "engineering_simulation_only"

ObjectiveSense = Literal["minimize", "maximize"]
ConstraintOperator = Literal["eq", "le", "ge"]
ConstraintSource = Literal["system", "business"]
ResultMode = Literal[
    "selected",
    "ranked-and-selected",
    "pareto-and-selected",
]


def _require_problem_schema(schema_id: str, schema_version: str) -> None:
    if schema_id != OPTIMIZATION_PROBLEM_SCHEMA_ID:
        raise ValueError("schema_id differs from the optimization problem contract")
    if schema_version != OPTIMIZATION_PROBLEM_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the optimization problem contract")


def _require_claim_scope(value: str) -> None:
    if value != ENGINEERING_CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


@dataclass(frozen=True)
class DecisionDomain:
    """One bounded decision domain expressed in canonical physical units."""

    variable_id: str
    display_unit: str
    canonical_unit: str
    nominal_value: float
    lower_bound: float
    upper_bound: float
    coarse_step: float
    refine_step: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "variable_id", identifier(self.variable_id, context="variable_id"))
        object.__setattr__(self, "display_unit", text(self.display_unit, context="display_unit"))
        object.__setattr__(
            self, "canonical_unit", text(self.canonical_unit, context="canonical_unit")
        )
        for name in (
            "nominal_value",
            "lower_bound",
            "upper_bound",
            "coarse_step",
            "refine_step",
        ):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if not self.lower_bound <= self.nominal_value <= self.upper_bound:
            raise ValueError("decision nominal_value must lie inside its bounds")
        if self.coarse_step <= 0.0 or self.refine_step <= 0.0:
            raise ValueError("decision steps must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DecisionDomain:
        strict_keys(
            value,
            required={
                "variable_id",
                "display_unit",
                "canonical_unit",
                "nominal_value",
                "lower_bound",
                "upper_bound",
                "coarse_step",
                "refine_step",
            },
            context="decision domain",
        )
        return cls(
            variable_id=identifier(value["variable_id"], context="variable_id"),
            display_unit=text(value["display_unit"], context="display_unit"),
            canonical_unit=text(value["canonical_unit"], context="canonical_unit"),
            nominal_value=finite(value["nominal_value"], context="nominal_value"),
            lower_bound=finite(value["lower_bound"], context="lower_bound"),
            upper_bound=finite(value["upper_bound"], context="upper_bound"),
            coarse_step=finite(value["coarse_step"], context="coarse_step"),
            refine_step=finite(value["refine_step"], context="refine_step"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "variable_id": self.variable_id,
            "display_unit": self.display_unit,
            "canonical_unit": self.canonical_unit,
            "nominal_value": self.nominal_value,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "coarse_step": self.coarse_step,
            "refine_step": self.refine_step,
        }


@dataclass(frozen=True)
class ObjectiveSpec:
    """One objective selected from the capability catalog."""

    metric_id: str
    sense: ObjectiveSense
    unit: str
    evaluation_stage: str
    formula_id: str
    normalization_scale: float

    def __post_init__(self) -> None:
        for name in ("metric_id", "evaluation_stage", "formula_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        object.__setattr__(
            self,
            "normalization_scale",
            finite(self.normalization_scale, context="normalization_scale"),
        )
        if self.normalization_scale <= 0.0:
            raise ValueError("objective normalization_scale must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveSpec:
        strict_keys(
            value,
            required={
                "metric_id",
                "sense",
                "unit",
                "evaluation_stage",
                "formula_id",
                "normalization_scale",
            },
            context="objective spec",
        )
        sense = value["sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            sense=sense,
            unit=text(value["unit"], context="unit"),
            evaluation_stage=identifier(value["evaluation_stage"], context="evaluation_stage"),
            formula_id=identifier(value["formula_id"], context="formula_id"),
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "unit": self.unit,
            "evaluation_stage": self.evaluation_stage,
            "formula_id": self.formula_id,
            "normalization_scale": self.normalization_scale,
        }


@dataclass(frozen=True)
class ConstraintRule:
    """A deterministic feasibility or publishability rule expanded from trusted inputs."""

    constraint_id: str
    priority: int
    metric_id: str
    evaluation_stage: str
    operator: ConstraintOperator
    limit: float
    unit: str
    normalization_scale: float
    source: ConstraintSource

    def __post_init__(self) -> None:
        for name in ("constraint_id", "metric_id", "evaluation_stage"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "priority", integer(self.priority, context="priority"))
        if self.operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported constraint operator")
        if self.source not in {"system", "business"}:
            raise ValueError("unsupported constraint source")
        object.__setattr__(self, "limit", finite(self.limit, context="limit"))
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        object.__setattr__(
            self,
            "normalization_scale",
            finite(self.normalization_scale, context="normalization_scale"),
        )
        if self.normalization_scale <= 0.0:
            raise ValueError("constraint normalization_scale must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ConstraintRule:
        strict_keys(
            value,
            required={
                "constraint_id",
                "priority",
                "metric_id",
                "evaluation_stage",
                "operator",
                "limit",
                "unit",
                "normalization_scale",
                "source",
            },
            context="constraint rule",
        )
        operator = value["operator"]
        source = value["source"]
        if operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported constraint operator")
        if source not in {"system", "business"}:
            raise ValueError("unsupported constraint source")
        return cls(
            constraint_id=identifier(value["constraint_id"], context="constraint_id"),
            priority=integer(value["priority"], context="priority"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            evaluation_stage=identifier(value["evaluation_stage"], context="evaluation_stage"),
            operator=operator,
            limit=finite(value["limit"], context="limit"),
            unit=text(value["unit"], context="unit"),
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
            source=source,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "priority": self.priority,
            "metric_id": self.metric_id,
            "evaluation_stage": self.evaluation_stage,
            "operator": self.operator,
            "limit": self.limit,
            "unit": self.unit,
            "normalization_scale": self.normalization_scale,
            "source": self.source,
        }


@dataclass(frozen=True)
class SelectionPreference:
    """Explicit candidate-selection semantics, independent of the solver."""

    method: str
    objective_order: tuple[str, ...]
    tie_breaks: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", identifier(self.method, context="method"))
        objectives = tuple(
            identifier(item, context="objective_id") for item in self.objective_order
        )
        ties = tuple(identifier(item, context="tie_break") for item in self.tie_breaks)
        if not objectives or len(objectives) != len(set(objectives)):
            raise ValueError("preference objective_order must be non-empty and unique")
        if len(ties) != len(set(ties)):
            raise ValueError("preference tie_breaks must be unique")
        object.__setattr__(self, "objective_order", objectives)
        object.__setattr__(self, "tie_breaks", ties)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SelectionPreference:
        strict_keys(
            value,
            required={"method", "objective_order", "tie_breaks"},
            context="selection preference",
        )
        return cls(
            method=identifier(value["method"], context="method"),
            objective_order=tuple(
                identifier(item, context="objective_id")
                for item in as_sequence(value["objective_order"], context="objective_order")
            ),
            tie_breaks=tuple(
                identifier(item, context="tie_break")
                for item in as_sequence(value["tie_breaks"], context="tie_breaks")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "objective_order": list(self.objective_order),
            "tie_breaks": list(self.tie_breaks),
        }


@dataclass(frozen=True)
class ResultRequest:
    """Requested optimization artifact shape, not an algorithm choice."""

    mode: ResultMode
    maximum_returned_candidates: int

    def __post_init__(self) -> None:
        if self.mode not in {
            "selected",
            "ranked-and-selected",
            "pareto-and-selected",
        }:
            raise ValueError("unsupported result mode")
        maximum = integer(
            self.maximum_returned_candidates,
            context="maximum_returned_candidates",
            minimum=1,
        )
        if self.mode == "selected" and maximum != 1:
            raise ValueError("selected result mode requires exactly one returned candidate")
        object.__setattr__(self, "maximum_returned_candidates", maximum)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResultRequest:
        strict_keys(
            value,
            required={"mode", "maximum_returned_candidates"},
            context="result request",
        )
        mode = value["mode"]
        if mode not in {
            "selected",
            "ranked-and-selected",
            "pareto-and-selected",
        }:
            raise ValueError("unsupported result mode")
        return cls(
            mode=mode,
            maximum_returned_candidates=integer(
                value["maximum_returned_candidates"],
                context="maximum_returned_candidates",
                minimum=1,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "maximum_returned_candidates": self.maximum_returned_candidates,
        }


@dataclass(frozen=True)
class EvaluationPlan:
    """Provider-neutral evaluation stages and dynamic-verification budget."""

    static_stage: str
    dynamic_stage: str
    m2_preset_id: str
    m4_preset_id: str
    m4_event_time_s: float
    m4_duration_s: float
    m4_time_step_s: float
    dynamic_verification_required: bool
    dynamic_shortlist_size: int
    context_anchor_ratios: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "static_stage", identifier(self.static_stage, context="static_stage")
        )
        object.__setattr__(
            self, "dynamic_stage", identifier(self.dynamic_stage, context="dynamic_stage")
        )
        object.__setattr__(
            self, "m2_preset_id", identifier(self.m2_preset_id, context="m2_preset_id")
        )
        object.__setattr__(
            self, "m4_preset_id", identifier(self.m4_preset_id, context="m4_preset_id")
        )
        for name in ("m4_event_time_s", "m4_duration_s", "m4_time_step_s"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if (
            self.m4_event_time_s < 0.0
            or self.m4_event_time_s >= self.m4_duration_s
            or self.m4_time_step_s <= 0.0
        ):
            raise ValueError("M4 evaluation time settings are invalid")
        object.__setattr__(
            self,
            "dynamic_verification_required",
            boolean(
                self.dynamic_verification_required,
                context="dynamic_verification_required",
            ),
        )
        object.__setattr__(
            self,
            "dynamic_shortlist_size",
            integer(self.dynamic_shortlist_size, context="dynamic_shortlist_size", minimum=1),
        )
        ratios = tuple(
            finite(item, context="context_anchor_ratio") for item in self.context_anchor_ratios
        )
        if ratios != tuple(sorted(set(ratios))) or any(item <= 0.0 for item in ratios):
            raise ValueError("context anchor ratios must be positive, unique and sorted")
        object.__setattr__(self, "context_anchor_ratios", ratios)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EvaluationPlan:
        strict_keys(
            value,
            required={
                "static_stage",
                "dynamic_stage",
                "m2_preset_id",
                "m4_preset_id",
                "m4_event_time_s",
                "m4_duration_s",
                "m4_time_step_s",
                "dynamic_verification_required",
                "dynamic_shortlist_size",
                "context_anchor_ratios",
            },
            context="evaluation plan",
        )
        return cls(
            static_stage=identifier(value["static_stage"], context="static_stage"),
            dynamic_stage=identifier(value["dynamic_stage"], context="dynamic_stage"),
            m2_preset_id=identifier(value["m2_preset_id"], context="m2_preset_id"),
            m4_preset_id=identifier(value["m4_preset_id"], context="m4_preset_id"),
            m4_event_time_s=finite(value["m4_event_time_s"], context="m4_event_time_s"),
            m4_duration_s=finite(value["m4_duration_s"], context="m4_duration_s"),
            m4_time_step_s=finite(value["m4_time_step_s"], context="m4_time_step_s"),
            dynamic_verification_required=boolean(
                value["dynamic_verification_required"],
                context="dynamic_verification_required",
            ),
            dynamic_shortlist_size=integer(
                value["dynamic_shortlist_size"],
                context="dynamic_shortlist_size",
                minimum=1,
            ),
            context_anchor_ratios=tuple(
                finite(item, context="context_anchor_ratio")
                for item in as_sequence(
                    value["context_anchor_ratios"], context="context_anchor_ratios"
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "static_stage": self.static_stage,
            "dynamic_stage": self.dynamic_stage,
            "m2_preset_id": self.m2_preset_id,
            "m4_preset_id": self.m4_preset_id,
            "m4_event_time_s": self.m4_event_time_s,
            "m4_duration_s": self.m4_duration_s,
            "m4_time_step_s": self.m4_time_step_s,
            "dynamic_verification_required": self.dynamic_verification_required,
            "dynamic_shortlist_size": self.dynamic_shortlist_size,
            "context_anchor_ratios": list(self.context_anchor_ratios),
        }


@dataclass(frozen=True)
class SolveRequirements:
    """Neutral requirements used by the router to select a solver."""

    maximum_evaluations: int
    deterministic_required: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_evaluations",
            integer(self.maximum_evaluations, context="maximum_evaluations", minimum=1),
        )
        object.__setattr__(
            self,
            "deterministic_required",
            boolean(self.deterministic_required, context="deterministic_required"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SolveRequirements:
        strict_keys(
            value,
            required={
                "maximum_evaluations",
                "deterministic_required",
            },
            context="solve requirements",
        )
        return cls(
            maximum_evaluations=integer(
                value["maximum_evaluations"], context="maximum_evaluations", minimum=1
            ),
            deterministic_required=boolean(
                value["deterministic_required"], context="deterministic_required"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_evaluations": self.maximum_evaluations,
            "deterministic_required": self.deterministic_required,
        }


@dataclass(frozen=True)
class OptimizationProblem:
    """One immutable optimization problem supporting any non-empty objective vector."""

    schema_id: str
    schema_version: str
    problem_version: str
    intent_ref: ContractRef
    context_ref: ContractRef
    capability_catalog_ref: ContractRef
    system_policy_ref: ContractRef
    execution_route_ref: ContractRef
    decision_domains: tuple[DecisionDomain, ...]
    objectives: tuple[ObjectiveSpec, ...]
    hard_constraints: tuple[ConstraintRule, ...]
    publishability_constraints: tuple[ConstraintRule, ...]
    preference: SelectionPreference
    result_request: ResultRequest
    evaluation_plan: EvaluationPlan
    solve_requirements: SolveRequirements
    claim_scope: str

    def __post_init__(self) -> None:
        _require_problem_schema(self.schema_id, self.schema_version)
        _require_claim_scope(self.claim_scope)
        object.__setattr__(
            self, "problem_version", identifier(self.problem_version, context="problem_version")
        )
        for name in (
            "intent_ref",
            "context_ref",
            "capability_catalog_ref",
            "system_policy_ref",
            "execution_route_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        decisions = tuple(self.decision_domains)
        decision_ids = tuple(item.variable_id for item in decisions)
        if not decisions or len(decision_ids) != len(set(decision_ids)):
            raise ValueError("decision domains must be non-empty and unique")
        if decision_ids != tuple(sorted(decision_ids)):
            raise ValueError("decision domains must be sorted by variable_id")
        objectives = tuple(self.objectives)
        objective_ids = tuple(item.metric_id for item in objectives)
        if not objectives or len(objective_ids) != len(set(objective_ids)):
            raise ValueError("objectives must be non-empty and unique")
        constraints = tuple(self.hard_constraints)
        if any(not isinstance(item, ConstraintRule) for item in constraints):
            raise TypeError("hard_constraints must contain ConstraintRule values")
        constraint_ids = tuple(item.constraint_id for item in constraints)
        priorities = tuple(item.priority for item in constraints)
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("hard constraints must have unique ids")
        if len(priorities) != len(set(priorities)) or priorities != tuple(sorted(priorities)):
            raise ValueError("hard constraints must have unique, ordered priorities")
        if any(item.evaluation_stage == "post_selection" for item in constraints):
            raise ValueError("post-selection constraints must not be mixed into hard constraints")
        publishability = tuple(self.publishability_constraints)
        if any(not isinstance(item, ConstraintRule) for item in publishability):
            raise TypeError("publishability_constraints must contain ConstraintRule values")
        publishability_ids = tuple(item.constraint_id for item in publishability)
        publishability_priorities = tuple(item.priority for item in publishability)
        if not publishability or len(publishability_ids) != len(set(publishability_ids)):
            raise ValueError("publishability constraints must be non-empty and have unique ids")
        if len(publishability_priorities) != len(
            set(publishability_priorities)
        ) or publishability_priorities != tuple(sorted(publishability_priorities)):
            raise ValueError("publishability constraints must have unique, ordered priorities")
        if set(constraint_ids) & set(publishability_ids):
            raise ValueError("publishability constraint ids must not repeat hard constraint ids")
        if set(priorities) & set(publishability_priorities):
            raise ValueError("constraint priorities must be unique across problem sections")
        if any(
            item.source != "system" or item.evaluation_stage != "post_selection"
            for item in publishability
        ):
            raise ValueError(
                "publishability constraints must be system-sourced post-selection rules"
            )
        if tuple(self.preference.objective_order) != objective_ids:
            raise ValueError("preference objective order must equal the problem objectives")
        object.__setattr__(self, "decision_domains", decisions)
        object.__setattr__(self, "objectives", objectives)
        object.__setattr__(self, "hard_constraints", constraints)
        object.__setattr__(self, "publishability_constraints", publishability)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "problem_version": self.problem_version,
            "intent_ref": self.intent_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "capability_catalog_ref": self.capability_catalog_ref.as_dict(),
            "system_policy_ref": self.system_policy_ref.as_dict(),
            "execution_route_ref": self.execution_route_ref.as_dict(),
            "decision_domains": [item.as_dict() for item in self.decision_domains],
            "objectives": [item.as_dict() for item in self.objectives],
            "hard_constraints": [item.as_dict() for item in self.hard_constraints],
            "publishability_constraints": [
                item.as_dict() for item in self.publishability_constraints
            ],
            "preference": self.preference.as_dict(),
            "result_request": self.result_request.as_dict(),
            "evaluation_plan": self.evaluation_plan.as_dict(),
            "solve_requirements": self.solve_requirements.as_dict(),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def problem_id(self) -> str:
        return f"problem-{self.fingerprint[:16]}"

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.problem_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "problem_id": self.problem_id,
            "problem_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationProblem:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "problem_version",
                "intent_ref",
                "context_ref",
                "capability_catalog_ref",
                "system_policy_ref",
                "execution_route_ref",
                "decision_domains",
                "objectives",
                "hard_constraints",
                "publishability_constraints",
                "preference",
                "result_request",
                "evaluation_plan",
                "solve_requirements",
                "claim_scope",
            },
            optional={"problem_id", "problem_fingerprint"},
            context="optimization problem",
        )
        problem = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            problem_version=identifier(value["problem_version"], context="problem_version"),
            intent_ref=ContractRef.from_mapping(
                as_mapping(value["intent_ref"], context="intent_ref")
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
            execution_route_ref=ContractRef.from_mapping(
                as_mapping(value["execution_route_ref"], context="execution_route_ref")
            ),
            decision_domains=tuple(
                DecisionDomain.from_mapping(as_mapping(item, context="decision domain"))
                for item in as_sequence(value["decision_domains"], context="decision_domains")
            ),
            objectives=tuple(
                ObjectiveSpec.from_mapping(as_mapping(item, context="objective spec"))
                for item in as_sequence(value["objectives"], context="objectives")
            ),
            hard_constraints=tuple(
                ConstraintRule.from_mapping(as_mapping(item, context="constraint rule"))
                for item in as_sequence(value["hard_constraints"], context="hard_constraints")
            ),
            publishability_constraints=tuple(
                ConstraintRule.from_mapping(
                    as_mapping(item, context="publishability constraint rule")
                )
                for item in as_sequence(
                    value["publishability_constraints"],
                    context="publishability_constraints",
                )
            ),
            preference=SelectionPreference.from_mapping(
                as_mapping(value["preference"], context="preference")
            ),
            result_request=ResultRequest.from_mapping(
                as_mapping(value["result_request"], context="result_request")
            ),
            evaluation_plan=EvaluationPlan.from_mapping(
                as_mapping(value["evaluation_plan"], context="evaluation_plan")
            ),
            solve_requirements=SolveRequirements.from_mapping(
                as_mapping(value["solve_requirements"], context="solve_requirements")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("problem_id") not in {None, problem.problem_id}:
            raise ValueError("problem_id differs from problem content")
        supplied = value.get("problem_fingerprint")
        if supplied not in {None, problem.fingerprint}:
            raise ValueError("problem_fingerprint differs from problem content")
        return problem
