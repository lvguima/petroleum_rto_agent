"""Strict RTO V2 multi-objective policy and problem contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from .common import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    finite,
    identifier,
    integer,
    strict_keys,
    text,
)
from .models import (
    CLAIM_SCOPE,
    ConstraintRuleV1,
    ContractRef,
    DecisionDomainV1,
    EvaluationPlanV1,
)

RTO_V2_SCHEMA_VERSION: Final[str] = "2.0.0"
ObjectiveSenseV2 = Literal["minimize", "maximize"]
RelativeImprovementPolicyV2 = Literal["zero-baseline-null", "directional-relative"]


def _schema(value: str) -> None:
    if value != RTO_V2_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V2 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _unique_identifiers(value: object, *, context: str) -> tuple[str, ...]:
    result = tuple(
        identifier(item, context=f"{context} item") for item in as_sequence(value, context=context)
    )
    if not result or len(result) != len(set(result)):
        raise ValueError(f"{context} must be non-empty and unique")
    return result


@dataclass(frozen=True)
class ObjectiveDefinitionV2:
    metric_id: str
    sense: ObjectiveSenseV2
    stage: str
    unit: str
    kpi_formula_id: str
    normalization_scale: float
    relative_improvement_policy: RelativeImprovementPolicyV2

    def __post_init__(self) -> None:
        for name in ("metric_id", "stage", "kpi_formula_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        if self.sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        if self.stage != "M2":
            raise ValueError("RTO V2 objectives must be evaluated at M2")
        object.__setattr__(
            self,
            "normalization_scale",
            finite(self.normalization_scale, context="normalization_scale"),
        )
        if self.normalization_scale <= 0.0:
            raise ValueError("objective normalization_scale must be positive")
        if self.relative_improvement_policy not in {
            "zero-baseline-null",
            "directional-relative",
        }:
            raise ValueError("unsupported relative improvement policy")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveDefinitionV2:
        strict_keys(
            value,
            required={
                "metric_id",
                "sense",
                "stage",
                "unit",
                "kpi_formula_id",
                "normalization_scale",
                "relative_improvement_policy",
            },
            context="objective definition",
        )
        sense = value["sense"]
        relative = value["relative_improvement_policy"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        if relative not in {"zero-baseline-null", "directional-relative"}:
            raise ValueError("unsupported relative improvement policy")
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            sense=sense,
            stage=identifier(value["stage"], context="stage"),
            unit=text(value["unit"], context="unit"),
            kpi_formula_id=identifier(value["kpi_formula_id"], context="kpi_formula_id"),
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
            relative_improvement_policy=relative,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "stage": self.stage,
            "unit": self.unit,
            "kpi_formula_id": self.kpi_formula_id,
            "normalization_scale": self.normalization_scale,
            "relative_improvement_policy": self.relative_improvement_policy,
        }


@dataclass(frozen=True)
class ObjectiveProfileV2:
    profile_id: str
    objective_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", identifier(self.profile_id, context="profile_id"))
        values = tuple(identifier(item, context="objective_id") for item in self.objective_ids)
        if not values or len(values) != len(set(values)):
            raise ValueError("objective profile ids must be non-empty and unique")
        object.__setattr__(self, "objective_ids", values)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveProfileV2:
        strict_keys(
            value,
            required={"profile_id", "objective_ids"},
            context="objective profile",
        )
        return cls(
            profile_id=identifier(value["profile_id"], context="profile_id"),
            objective_ids=_unique_identifiers(value["objective_ids"], context="objective_ids"),
        )

    def as_dict(self) -> dict[str, object]:
        return {"profile_id": self.profile_id, "objective_ids": list(self.objective_ids)}


@dataclass(frozen=True)
class ObjectiveCatalogV2:
    schema_version: str
    catalog_version: str
    catalog_id: str
    maximum_objectives: int
    claim_scope: str
    profiles: tuple[ObjectiveProfileV2, ...]
    objectives: tuple[ObjectiveDefinitionV2, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("catalog_version", "catalog_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(
            self,
            "maximum_objectives",
            integer(self.maximum_objectives, context="maximum_objectives", minimum=1),
        )
        objectives = tuple(self.objectives)
        objective_ids = tuple(item.metric_id for item in objectives)
        if not objectives or len(objective_ids) != len(set(objective_ids)):
            raise ValueError("objective definitions must be non-empty and unique")
        profiles = tuple(self.profiles)
        profile_ids = tuple(item.profile_id for item in profiles)
        if not profiles or len(profile_ids) != len(set(profile_ids)):
            raise ValueError("objective profiles must be non-empty and unique")
        for profile in profiles:
            if len(profile.objective_ids) > self.maximum_objectives:
                raise ValueError("objective profile exceeds maximum_objectives")
            if not set(profile.objective_ids).issubset(objective_ids):
                raise ValueError("objective profile references an unknown objective")
        object.__setattr__(self, "objectives", objectives)
        object.__setattr__(self, "profiles", profiles)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveCatalogV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "catalog_version",
                "catalog_id",
                "maximum_objectives",
                "claim_scope",
                "profiles",
                "objectives",
            },
            context="objective catalog",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            catalog_version=identifier(value["catalog_version"], context="catalog_version"),
            catalog_id=identifier(value["catalog_id"], context="catalog_id"),
            maximum_objectives=integer(
                value["maximum_objectives"], context="maximum_objectives", minimum=1
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            profiles=tuple(
                ObjectiveProfileV2.from_mapping(as_mapping(item, context="objective profile"))
                for item in as_sequence(value["profiles"], context="objective profiles")
            ),
            objectives=tuple(
                ObjectiveDefinitionV2.from_mapping(as_mapping(item, context="objective definition"))
                for item in as_sequence(value["objectives"], context="objectives")
            ),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "maximum_objectives": self.maximum_objectives,
            "claim_scope": self.claim_scope,
            "profiles": [item.as_dict() for item in self.profiles],
            "objectives": [item.as_dict() for item in self.objectives],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.catalog_id, self.fingerprint)

    def objective_by_id(self, metric_id: str) -> ObjectiveDefinitionV2:
        for item in self.objectives:
            if item.metric_id == metric_id:
                return item
        raise KeyError(f"unknown objective {metric_id!r}")

    def profile_by_id(self, profile_id: str) -> ObjectiveProfileV2:
        for item in self.profiles:
            if item.profile_id == profile_id:
                return item
        raise KeyError(f"unknown objective profile {profile_id!r}")


@dataclass(frozen=True)
class PreferenceProfileV2:
    profile_id: str
    method: str
    objective_order: tuple[str, ...]
    tie_breaks: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", identifier(self.profile_id, context="profile_id"))
        object.__setattr__(self, "method", identifier(self.method, context="method"))
        if self.method != "lexicographic":
            raise ValueError("RTO V2 only supports lexicographic preference")
        objective_order = tuple(
            identifier(item, context="objective_order") for item in self.objective_order
        )
        tie_breaks = tuple(identifier(item, context="tie_break") for item in self.tie_breaks)
        if not objective_order or len(objective_order) != len(set(objective_order)):
            raise ValueError("preference objective_order must be non-empty and unique")
        if not tie_breaks or len(tie_breaks) != len(set(tie_breaks)):
            raise ValueError("preference tie_breaks must be non-empty and unique")
        object.__setattr__(self, "objective_order", objective_order)
        object.__setattr__(self, "tie_breaks", tie_breaks)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PreferenceProfileV2:
        strict_keys(
            value,
            required={"profile_id", "method", "objective_order", "tie_breaks"},
            context="preference profile",
        )
        return cls(
            profile_id=identifier(value["profile_id"], context="profile_id"),
            method=identifier(value["method"], context="method"),
            objective_order=_unique_identifiers(
                value["objective_order"], context="objective_order"
            ),
            tie_breaks=_unique_identifiers(value["tie_breaks"], context="tie_breaks"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "method": self.method,
            "objective_order": list(self.objective_order),
            "tie_breaks": list(self.tie_breaks),
        }


@dataclass(frozen=True)
class PreferenceCatalogV2:
    schema_version: str
    catalog_version: str
    catalog_id: str
    claim_scope: str
    profiles: tuple[PreferenceProfileV2, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("catalog_version", "catalog_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        profiles = tuple(self.profiles)
        ids = tuple(item.profile_id for item in profiles)
        if not profiles or len(ids) != len(set(ids)):
            raise ValueError("preference profiles must be non-empty and unique")
        object.__setattr__(self, "profiles", profiles)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PreferenceCatalogV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "catalog_version",
                "catalog_id",
                "claim_scope",
                "profiles",
            },
            context="preference catalog",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            catalog_version=identifier(value["catalog_version"], context="catalog_version"),
            catalog_id=identifier(value["catalog_id"], context="catalog_id"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            profiles=tuple(
                PreferenceProfileV2.from_mapping(as_mapping(item, context="preference profile"))
                for item in as_sequence(value["profiles"], context="preference profiles")
            ),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "claim_scope": self.claim_scope,
            "profiles": [item.as_dict() for item in self.profiles],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.catalog_id, self.fingerprint)

    def profile_by_id(self, profile_id: str) -> PreferenceProfileV2:
        for item in self.profiles:
            if item.profile_id == profile_id:
                return item
        raise KeyError(f"unknown preference profile {profile_id!r}")


@dataclass(frozen=True)
class PublishabilityProfileV2:
    profile_id: str
    metric_id: str
    comparison: str
    limit: float
    failure_status: str

    def __post_init__(self) -> None:
        for name in ("profile_id", "metric_id", "comparison", "failure_status"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.comparison != "relative-directional-improvement-ge":
            raise ValueError("unsupported publishability comparison")
        if self.failure_status != "feasible_not_publishable":
            raise ValueError("unsupported publishability failure status")
        object.__setattr__(self, "limit", finite(self.limit, context="limit"))
        if self.limit < 0.0:
            raise ValueError("publishability limit must be non-negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PublishabilityProfileV2:
        strict_keys(
            value,
            required={"profile_id", "metric_id", "comparison", "limit", "failure_status"},
            context="publishability profile",
        )
        return cls(
            profile_id=identifier(value["profile_id"], context="profile_id"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            comparison=identifier(value["comparison"], context="comparison"),
            limit=finite(value["limit"], context="limit"),
            failure_status=identifier(value["failure_status"], context="failure_status"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "metric_id": self.metric_id,
            "comparison": self.comparison,
            "limit": self.limit,
            "failure_status": self.failure_status,
        }


@dataclass(frozen=True)
class PublishabilityCatalogV2:
    schema_version: str
    catalog_version: str
    catalog_id: str
    claim_scope: str
    profiles: tuple[PublishabilityProfileV2, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("catalog_version", "catalog_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        profiles = tuple(self.profiles)
        ids = tuple(item.profile_id for item in profiles)
        if not profiles or len(ids) != len(set(ids)):
            raise ValueError("publishability profiles must be non-empty and unique")
        object.__setattr__(self, "profiles", profiles)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PublishabilityCatalogV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "catalog_version",
                "catalog_id",
                "claim_scope",
                "profiles",
            },
            context="publishability catalog",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            catalog_version=identifier(value["catalog_version"], context="catalog_version"),
            catalog_id=identifier(value["catalog_id"], context="catalog_id"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            profiles=tuple(
                PublishabilityProfileV2.from_mapping(
                    as_mapping(item, context="publishability profile")
                )
                for item in as_sequence(value["profiles"], context="publishability profiles")
            ),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "claim_scope": self.claim_scope,
            "profiles": [item.as_dict() for item in self.profiles],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.catalog_id, self.fingerprint)

    def profile_by_id(self, profile_id: str) -> PublishabilityProfileV2:
        for item in self.profiles:
            if item.profile_id == profile_id:
                return item
        raise KeyError(f"unknown publishability profile {profile_id!r}")


@dataclass(frozen=True)
class MultiObjectiveSearchPlanV2:
    algorithm_id: str
    algorithm_version: str
    grid_step_source: str
    points_per_dimension: int
    maximum_m2_candidates: int
    dominance_policy: str
    equivalence_policy: str
    cache_policy: str
    randomness: str

    def __post_init__(self) -> None:
        for name in (
            "algorithm_id",
            "algorithm_version",
            "grid_step_source",
            "dominance_policy",
            "equivalence_policy",
            "cache_policy",
            "randomness",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(
            self,
            "points_per_dimension",
            integer(self.points_per_dimension, context="points_per_dimension", minimum=2),
        )
        object.__setattr__(
            self,
            "maximum_m2_candidates",
            integer(self.maximum_m2_candidates, context="maximum_m2_candidates", minimum=1),
        )
        if self.algorithm_id != "deterministic-full-grid" or self.randomness != "none":
            raise ValueError("RTO V2 requires a deterministic full grid")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MultiObjectiveSearchPlanV2:
        required = {
            "algorithm_id",
            "algorithm_version",
            "grid_step_source",
            "points_per_dimension",
            "maximum_m2_candidates",
            "dominance_policy",
            "equivalence_policy",
            "cache_policy",
            "randomness",
        }
        strict_keys(value, required=required, context="multi-objective search plan")
        return cls(
            algorithm_id=identifier(value["algorithm_id"], context="algorithm_id"),
            algorithm_version=identifier(value["algorithm_version"], context="algorithm_version"),
            grid_step_source=identifier(value["grid_step_source"], context="grid_step_source"),
            points_per_dimension=integer(
                value["points_per_dimension"], context="points_per_dimension", minimum=2
            ),
            maximum_m2_candidates=integer(
                value["maximum_m2_candidates"],
                context="maximum_m2_candidates",
                minimum=1,
            ),
            dominance_policy=identifier(value["dominance_policy"], context="dominance_policy"),
            equivalence_policy=identifier(
                value["equivalence_policy"], context="equivalence_policy"
            ),
            cache_policy=identifier(value["cache_policy"], context="cache_policy"),
            randomness=identifier(value["randomness"], context="randomness"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "grid_step_source": self.grid_step_source,
            "points_per_dimension": self.points_per_dimension,
            "maximum_m2_candidates": self.maximum_m2_candidates,
            "dominance_policy": self.dominance_policy,
            "equivalence_policy": self.equivalence_policy,
            "cache_policy": self.cache_policy,
            "randomness": self.randomness,
        }


@dataclass(frozen=True)
class MultiObjectivePolicyV2:
    schema_version: str
    policy_version: str
    policy_id: str
    objective_catalog_id: str
    objective_profile_id: str
    preference_catalog_id: str
    selection_profile_id: str
    decision_profile_id: str
    business_constraint_profile_id: str
    constraint_profile_id: str
    publishability_profile_id: str
    requested_output: str
    context_policy: str
    allowed_assumptions: tuple[str, ...]
    evaluation: EvaluationPlanV1
    search: MultiObjectiveSearchPlanV2
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in (
            "policy_version",
            "policy_id",
            "objective_catalog_id",
            "objective_profile_id",
            "preference_catalog_id",
            "selection_profile_id",
            "decision_profile_id",
            "business_constraint_profile_id",
            "constraint_profile_id",
            "publishability_profile_id",
            "requested_output",
            "context_policy",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        assumptions = tuple(
            identifier(item, context="allowed_assumption") for item in self.allowed_assumptions
        )
        if not assumptions or len(assumptions) != len(set(assumptions)):
            raise ValueError("allowed_assumptions must be non-empty and unique")
        object.__setattr__(self, "allowed_assumptions", assumptions)
        if not isinstance(self.evaluation, EvaluationPlanV1):
            raise TypeError("multi-objective policy requires an EvaluationPlanV1")
        if not isinstance(self.search, MultiObjectiveSearchPlanV2):
            raise TypeError("multi-objective policy requires MultiObjectiveSearchPlanV2")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MultiObjectivePolicyV2:
        required = {
            "schema_version",
            "policy_version",
            "policy_id",
            "objective_catalog_id",
            "objective_profile_id",
            "preference_catalog_id",
            "selection_profile_id",
            "decision_profile_id",
            "business_constraint_profile_id",
            "constraint_profile_id",
            "publishability_profile_id",
            "requested_output",
            "context_policy",
            "allowed_assumptions",
            "evaluation",
            "search",
            "claim_scope",
        }
        strict_keys(value, required=required, context="multi-objective policy")
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            policy_version=identifier(value["policy_version"], context="policy_version"),
            policy_id=identifier(value["policy_id"], context="policy_id"),
            objective_catalog_id=identifier(
                value["objective_catalog_id"], context="objective_catalog_id"
            ),
            objective_profile_id=identifier(
                value["objective_profile_id"], context="objective_profile_id"
            ),
            preference_catalog_id=identifier(
                value["preference_catalog_id"], context="preference_catalog_id"
            ),
            selection_profile_id=identifier(
                value["selection_profile_id"], context="selection_profile_id"
            ),
            decision_profile_id=identifier(
                value["decision_profile_id"], context="decision_profile_id"
            ),
            business_constraint_profile_id=identifier(
                value["business_constraint_profile_id"],
                context="business_constraint_profile_id",
            ),
            constraint_profile_id=identifier(
                value["constraint_profile_id"], context="constraint_profile_id"
            ),
            publishability_profile_id=identifier(
                value["publishability_profile_id"], context="publishability_profile_id"
            ),
            requested_output=identifier(value["requested_output"], context="requested_output"),
            context_policy=identifier(value["context_policy"], context="context_policy"),
            allowed_assumptions=_unique_identifiers(
                value["allowed_assumptions"], context="allowed_assumptions"
            ),
            evaluation=EvaluationPlanV1.from_mapping(
                as_mapping(value["evaluation"], context="evaluation")
            ),
            search=MultiObjectiveSearchPlanV2.from_mapping(
                as_mapping(value["search"], context="search")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "policy_id": self.policy_id,
            "objective_catalog_id": self.objective_catalog_id,
            "objective_profile_id": self.objective_profile_id,
            "preference_catalog_id": self.preference_catalog_id,
            "selection_profile_id": self.selection_profile_id,
            "decision_profile_id": self.decision_profile_id,
            "business_constraint_profile_id": self.business_constraint_profile_id,
            "constraint_profile_id": self.constraint_profile_id,
            "publishability_profile_id": self.publishability_profile_id,
            "requested_output": self.requested_output,
            "context_policy": self.context_policy,
            "allowed_assumptions": list(self.allowed_assumptions),
            "evaluation": self.evaluation.as_dict(),
            "search": self.search.as_dict(),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.policy_id, self.fingerprint)


@dataclass(frozen=True)
class ObjectiveSpecV2:
    metric_id: str
    sense: ObjectiveSenseV2
    priority_tier: int
    unit: str
    kpi_formula_id: str
    normalization_scale: float
    relative_improvement_policy: RelativeImprovementPolicyV2

    def __post_init__(self) -> None:
        definition = ObjectiveDefinitionV2(
            metric_id=self.metric_id,
            sense=self.sense,
            stage="M2",
            unit=self.unit,
            kpi_formula_id=self.kpi_formula_id,
            normalization_scale=self.normalization_scale,
            relative_improvement_policy=self.relative_improvement_policy,
        )
        object.__setattr__(self, "metric_id", definition.metric_id)
        object.__setattr__(self, "unit", definition.unit)
        object.__setattr__(self, "kpi_formula_id", definition.kpi_formula_id)
        object.__setattr__(self, "normalization_scale", definition.normalization_scale)
        object.__setattr__(
            self,
            "priority_tier",
            integer(self.priority_tier, context="priority_tier", minimum=1),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveSpecV2:
        strict_keys(
            value,
            required={
                "metric_id",
                "sense",
                "priority_tier",
                "unit",
                "kpi_formula_id",
                "normalization_scale",
                "relative_improvement_policy",
            },
            context="objective spec",
        )
        sense = value["sense"]
        relative = value["relative_improvement_policy"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        if relative not in {"zero-baseline-null", "directional-relative"}:
            raise ValueError("unsupported relative improvement policy")
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            sense=sense,
            priority_tier=integer(value["priority_tier"], context="priority_tier", minimum=1),
            unit=text(value["unit"], context="unit"),
            kpi_formula_id=identifier(value["kpi_formula_id"], context="kpi_formula_id"),
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
            relative_improvement_policy=relative,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "sense": self.sense,
            "priority_tier": self.priority_tier,
            "unit": self.unit,
            "kpi_formula_id": self.kpi_formula_id,
            "normalization_scale": self.normalization_scale,
            "relative_improvement_policy": self.relative_improvement_policy,
        }


@dataclass(frozen=True)
class ResolvedOptimizationIntentV2:
    schema_version: str
    intent_version: str
    intent_id: str
    operating_context_ref: ContractRef
    audit_fingerprint: str
    semantic_fingerprint: str
    objective_profile_id: str
    objectives: tuple[ObjectiveSpecV2, ...]
    selection_profile_id: str
    return_pareto_front: bool
    max_returned_candidates: int
    decision_profile_id: str
    constraint_profile_id: str
    publishability_profile_id: str
    requested_output: str
    context_policy: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in (
            "intent_version",
            "intent_id",
            "objective_profile_id",
            "selection_profile_id",
            "decision_profile_id",
            "constraint_profile_id",
            "publishability_profile_id",
            "requested_output",
            "context_policy",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if not isinstance(self.operating_context_ref, ContractRef):
            raise TypeError("operating_context_ref must be a ContractRef")
        object.__setattr__(
            self, "audit_fingerprint", digest(self.audit_fingerprint, context="audit_fingerprint")
        )
        object.__setattr__(
            self,
            "semantic_fingerprint",
            digest(self.semantic_fingerprint, context="semantic_fingerprint"),
        )
        objectives = tuple(self.objectives)
        ids = tuple(item.metric_id for item in objectives)
        tiers = tuple(item.priority_tier for item in objectives)
        if not objectives or len(ids) != len(set(ids)) or tiers != tuple(range(1, len(tiers) + 1)):
            raise ValueError("resolved objectives must be unique with contiguous priority tiers")
        object.__setattr__(self, "objectives", objectives)
        if not isinstance(self.return_pareto_front, bool):
            raise TypeError("return_pareto_front must be boolean")
        object.__setattr__(
            self,
            "max_returned_candidates",
            integer(
                self.max_returned_candidates,
                context="max_returned_candidates",
                minimum=1,
            ),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_version": self.intent_version,
            "intent_id": self.intent_id,
            "operating_context_ref": self.operating_context_ref.as_dict(),
            "semantic_fingerprint": self.semantic_fingerprint,
            "objective_profile_id": self.objective_profile_id,
            "objectives": [item.as_dict() for item in self.objectives],
            "selection_profile_id": self.selection_profile_id,
            "return_pareto_front": self.return_pareto_front,
            "max_returned_candidates": self.max_returned_candidates,
            "decision_profile_id": self.decision_profile_id,
            "constraint_profile_id": self.constraint_profile_id,
            "publishability_profile_id": self.publishability_profile_id,
            "requested_output": self.requested_output,
            "context_policy": self.context_policy,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.intent_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "audit_fingerprint": self.audit_fingerprint,
            "intent_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResolvedOptimizationIntentV2:
        required = {
            "schema_version",
            "intent_version",
            "intent_id",
            "operating_context_ref",
            "audit_fingerprint",
            "semantic_fingerprint",
            "objective_profile_id",
            "objectives",
            "selection_profile_id",
            "return_pareto_front",
            "max_returned_candidates",
            "decision_profile_id",
            "constraint_profile_id",
            "publishability_profile_id",
            "requested_output",
            "context_policy",
            "claim_scope",
        }
        strict_keys(value, required=required, optional={"intent_fingerprint"}, context="intent")
        from .common import boolean

        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            intent_version=identifier(value["intent_version"], context="intent_version"),
            intent_id=identifier(value["intent_id"], context="intent_id"),
            operating_context_ref=ContractRef.from_mapping(
                as_mapping(value["operating_context_ref"], context="operating_context_ref")
            ),
            audit_fingerprint=digest(value["audit_fingerprint"], context="audit_fingerprint"),
            semantic_fingerprint=digest(
                value["semantic_fingerprint"], context="semantic_fingerprint"
            ),
            objective_profile_id=identifier(
                value["objective_profile_id"], context="objective_profile_id"
            ),
            objectives=tuple(
                ObjectiveSpecV2.from_mapping(as_mapping(item, context="objective spec"))
                for item in as_sequence(value["objectives"], context="objectives")
            ),
            selection_profile_id=identifier(
                value["selection_profile_id"], context="selection_profile_id"
            ),
            return_pareto_front=boolean(
                value["return_pareto_front"], context="return_pareto_front"
            ),
            max_returned_candidates=integer(
                value["max_returned_candidates"],
                context="max_returned_candidates",
                minimum=1,
            ),
            decision_profile_id=identifier(
                value["decision_profile_id"], context="decision_profile_id"
            ),
            constraint_profile_id=identifier(
                value["constraint_profile_id"], context="constraint_profile_id"
            ),
            publishability_profile_id=identifier(
                value["publishability_profile_id"], context="publishability_profile_id"
            ),
            requested_output=identifier(value["requested_output"], context="requested_output"),
            context_policy=identifier(value["context_policy"], context="context_policy"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("intent_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="intent_fingerprint") != result.fingerprint
        ):
            raise ValueError("intent_fingerprint differs from intent content")
        return result


@dataclass(frozen=True)
class OptimizationProblemV2:
    schema_version: str
    problem_version: str
    intent_ref: ContractRef
    context_ref: ContractRef
    decision_catalog_ref: ContractRef
    kpi_catalog_ref: ContractRef
    constraint_profile_ref: ContractRef
    policy_ref: ContractRef
    objective_catalog_ref: ContractRef
    preference_catalog_ref: ContractRef
    preference_profile_id: str
    publishability_catalog_ref: ContractRef
    publishability_profile_id: str
    decision_domains: tuple[DecisionDomainV1, ...]
    objectives: tuple[ObjectiveSpecV2, ...]
    constraints: tuple[ConstraintRuleV1, ...]
    evaluation_plan: EvaluationPlanV1
    search_plan: MultiObjectiveSearchPlanV2
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "problem_version", identifier(self.problem_version, context="problem_version")
        )
        for name in (
            "intent_ref",
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
        for name in ("preference_profile_id", "publishability_profile_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        domains = tuple(self.decision_domains)
        if not domains or tuple(item.variable_id for item in domains) != tuple(
            sorted(item.variable_id for item in domains)
        ):
            raise ValueError("decision domains must be non-empty and sorted")
        object.__setattr__(self, "decision_domains", domains)
        objectives = tuple(self.objectives)
        ids = tuple(item.metric_id for item in objectives)
        tiers = tuple(item.priority_tier for item in objectives)
        if not objectives or len(ids) != len(set(ids)) or tiers != tuple(range(1, len(tiers) + 1)):
            raise ValueError("problem objectives must be unique and priority ordered")
        object.__setattr__(self, "objectives", objectives)
        object.__setattr__(self, "constraints", tuple(self.constraints))
        if not isinstance(self.evaluation_plan, EvaluationPlanV1):
            raise TypeError("problem evaluation_plan must be EvaluationPlanV1")
        if not isinstance(self.search_plan, MultiObjectiveSearchPlanV2):
            raise TypeError("problem search_plan must be MultiObjectiveSearchPlanV2")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "problem_version": self.problem_version,
            "intent_ref": self.intent_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "decision_catalog_ref": self.decision_catalog_ref.as_dict(),
            "kpi_catalog_ref": self.kpi_catalog_ref.as_dict(),
            "constraint_profile_ref": self.constraint_profile_ref.as_dict(),
            "policy_ref": self.policy_ref.as_dict(),
            "objective_catalog_ref": self.objective_catalog_ref.as_dict(),
            "preference_catalog_ref": self.preference_catalog_ref.as_dict(),
            "preference_profile_id": self.preference_profile_id,
            "publishability_catalog_ref": self.publishability_catalog_ref.as_dict(),
            "publishability_profile_id": self.publishability_profile_id,
            "decision_domains": [item.as_dict() for item in self.decision_domains],
            "objectives": [item.as_dict() for item in self.objectives],
            "constraints": [item.as_dict() for item in self.constraints],
            "evaluation_plan": self.evaluation_plan.as_dict(),
            "search_plan": self.search_plan.as_dict(),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def problem_id(self) -> str:
        return f"problem-v2-{self.fingerprint[:16]}"

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
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationProblemV2:
        required = {
            "schema_version",
            "problem_version",
            "intent_ref",
            "context_ref",
            "decision_catalog_ref",
            "kpi_catalog_ref",
            "constraint_profile_ref",
            "policy_ref",
            "objective_catalog_ref",
            "preference_catalog_ref",
            "preference_profile_id",
            "publishability_catalog_ref",
            "publishability_profile_id",
            "decision_domains",
            "objectives",
            "constraints",
            "evaluation_plan",
            "search_plan",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"problem_id", "problem_fingerprint"},
            context="optimization problem V2",
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            problem_version=identifier(value["problem_version"], context="problem_version"),
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
            objective_catalog_ref=ContractRef.from_mapping(
                as_mapping(value["objective_catalog_ref"], context="objective_catalog_ref")
            ),
            preference_catalog_ref=ContractRef.from_mapping(
                as_mapping(value["preference_catalog_ref"], context="preference_catalog_ref")
            ),
            preference_profile_id=identifier(
                value["preference_profile_id"], context="preference_profile_id"
            ),
            publishability_catalog_ref=ContractRef.from_mapping(
                as_mapping(
                    value["publishability_catalog_ref"],
                    context="publishability_catalog_ref",
                )
            ),
            publishability_profile_id=identifier(
                value["publishability_profile_id"], context="publishability_profile_id"
            ),
            decision_domains=tuple(
                DecisionDomainV1.from_mapping(as_mapping(item, context="decision domain"))
                for item in as_sequence(value["decision_domains"], context="decision domains")
            ),
            objectives=tuple(
                ObjectiveSpecV2.from_mapping(as_mapping(item, context="objective spec"))
                for item in as_sequence(value["objectives"], context="objectives")
            ),
            constraints=tuple(
                ConstraintRuleV1.from_mapping(as_mapping(item, context="constraint rule"))
                for item in as_sequence(value["constraints"], context="constraints")
            ),
            evaluation_plan=EvaluationPlanV1.from_mapping(
                as_mapping(value["evaluation_plan"], context="evaluation_plan")
            ),
            search_plan=MultiObjectiveSearchPlanV2.from_mapping(
                as_mapping(value["search_plan"], context="search_plan")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("problem_id") not in {None, result.problem_id}:
            raise ValueError("problem_id differs from problem content")
        supplied = value.get("problem_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="problem_fingerprint") != result.fingerprint
        ):
            raise ValueError("problem_fingerprint differs from problem content")
        return result
