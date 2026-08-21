"""Strict immutable contracts for the RTO capability layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from ..contracts.common import (
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
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE
from ..contracts.reference import ContractRef

CAPABILITY_SCHEMA_VERSION: Final[str] = "2.0.0"

Availability = Literal["available", "conditional", "deferred", "unsupported"]
MetricDirection = Literal["equal", "minimize", "maximize"]
ObjectiveSense = Literal["minimize", "maximize"]
GuardrailOperator = Literal["eq", "le", "ge"]
SelectorMethod = Literal["single-objective", "lexicographic"]


def _schema_and_claim(schema_version: str, claim_scope: str) -> None:
    if schema_version != CAPABILITY_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the capability contract")
    if claim_scope != ENGINEERING_CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _optional_text(value: object, *, context: str) -> str | None:
    return None if value is None else text(value, context=context)


def _optional_identifier(value: object, *, context: str) -> str | None:
    return None if value is None else identifier(value, context=context)


def _identifiers(value: object, *, context: str, allow_empty: bool = False) -> tuple[str, ...]:
    values = tuple(
        identifier(item, context=f"{context} item") for item in as_sequence(value, context=context)
    )
    if (not values and not allow_empty) or len(values) != len(set(values)):
        raise ValueError(f"{context} must be {'unique' if allow_empty else 'non-empty and unique'}")
    return values


def _texts(value: object, *, context: str) -> tuple[str, ...]:
    values = tuple(
        text(item, context=f"{context} item") for item in as_sequence(value, context=context)
    )
    if not values or len(values) != len(set(values)):
        raise ValueError(f"{context} must be non-empty and unique")
    return values


def _availability(
    availability: object,
    availability_reason: object,
    *,
    context: str,
) -> tuple[Availability, str | None]:
    if availability not in {"available", "conditional", "deferred", "unsupported"}:
        raise ValueError(f"{context}.availability is unsupported")
    reason = _optional_text(availability_reason, context=f"{context}.availability_reason")
    if availability == "available" and reason is not None:
        raise ValueError(f"{context}.availability_reason must be null when available")
    if availability != "available" and reason is None:
        raise ValueError(f"{context}.availability_reason is required when not available")
    return availability, reason


@dataclass(frozen=True)
class MetricCapability:
    metric_id: str
    business_name: str
    stage: str
    unit: str
    direction: MetricDirection
    formula_ref: str
    source_paths: tuple[str, ...]
    proxy: bool
    availability: Availability
    availability_reason: str | None

    def __post_init__(self) -> None:
        for name in ("metric_id", "stage", "formula_ref"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "business_name", text(self.business_name, context="business_name"))
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        if self.direction not in {"equal", "minimize", "maximize"}:
            raise ValueError("unsupported metric direction")
        paths = tuple(text(item, context="source_path") for item in self.source_paths)
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("source_paths must be non-empty and unique")
        object.__setattr__(self, "source_paths", paths)
        if not isinstance(self.proxy, bool):
            raise TypeError("proxy must be boolean")
        availability, reason = _availability(
            self.availability, self.availability_reason, context="metric"
        )
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "availability_reason", reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MetricCapability:
        strict_keys(
            value,
            required={
                "metric_id",
                "business_name",
                "stage",
                "unit",
                "direction",
                "formula_ref",
                "source_paths",
                "proxy",
                "availability",
                "availability_reason",
            },
            context="metric capability",
        )
        direction = value["direction"]
        if direction not in {"equal", "minimize", "maximize"}:
            raise ValueError("unsupported metric direction")
        availability, reason = _availability(
            value["availability"], value["availability_reason"], context="metric capability"
        )
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            business_name=text(value["business_name"], context="business_name"),
            stage=identifier(value["stage"], context="stage"),
            unit=text(value["unit"], context="unit"),
            direction=direction,
            formula_ref=identifier(value["formula_ref"], context="formula_ref"),
            source_paths=_texts(value["source_paths"], context="source_paths"),
            proxy=boolean(value["proxy"], context="proxy"),
            availability=availability,
            availability_reason=reason,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "business_name": self.business_name,
            "stage": self.stage,
            "unit": self.unit,
            "direction": self.direction,
            "formula_ref": self.formula_ref,
            "source_paths": list(self.source_paths),
            "proxy": self.proxy,
            "availability": self.availability,
            "availability_reason": self.availability_reason,
        }


@dataclass(frozen=True)
class ObjectiveCapability:
    objective_id: str
    business_name: str
    metric_id: str
    sense: ObjectiveSense
    normalization_scale: float
    availability: Availability
    availability_reason: str | None

    def __post_init__(self) -> None:
        for name in ("objective_id", "metric_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "business_name", text(self.business_name, context="business_name"))
        if self.sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        scale = finite(self.normalization_scale, context="normalization_scale")
        if scale <= 0.0:
            raise ValueError("normalization_scale must be positive")
        object.__setattr__(self, "normalization_scale", scale)
        availability, reason = _availability(
            self.availability, self.availability_reason, context="objective"
        )
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "availability_reason", reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObjectiveCapability:
        strict_keys(
            value,
            required={
                "objective_id",
                "business_name",
                "metric_id",
                "sense",
                "normalization_scale",
                "availability",
                "availability_reason",
            },
            context="objective capability",
        )
        sense = value["sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        availability, reason = _availability(
            value["availability"], value["availability_reason"], context="objective capability"
        )
        return cls(
            objective_id=identifier(value["objective_id"], context="objective_id"),
            business_name=text(value["business_name"], context="business_name"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            sense=sense,
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
            availability=availability,
            availability_reason=reason,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id,
            "business_name": self.business_name,
            "metric_id": self.metric_id,
            "sense": self.sense,
            "normalization_scale": self.normalization_scale,
            "availability": self.availability,
            "availability_reason": self.availability_reason,
        }


@dataclass(frozen=True)
class DecisionCapability:
    decision_id: str
    business_name: str
    display_unit: str
    canonical_unit: str
    lower_bound: float
    upper_bound: float
    coarse_step: float
    refine_step: float
    m2_parameter_path: str | None
    m4_loop_id: str | None
    availability: Availability
    availability_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", identifier(self.decision_id, context="decision_id"))
        for name in (
            "business_name",
            "display_unit",
            "canonical_unit",
        ):
            object.__setattr__(self, name, text(getattr(self, name), context=name))
        for name in ("m2_parameter_path", "m4_loop_id"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), context=name))
        for name in ("lower_bound", "upper_bound", "coarse_step", "refine_step"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if self.lower_bound >= self.upper_bound:
            raise ValueError("decision lower_bound must be smaller than upper_bound")
        if self.coarse_step <= 0.0 or self.refine_step <= 0.0:
            raise ValueError("decision steps must be positive")
        availability, reason = _availability(
            self.availability, self.availability_reason, context="decision"
        )
        if availability == "available" and (
            self.m2_parameter_path is None or self.m4_loop_id is None
        ):
            raise ValueError("available decisions require both M2 and M4 bindings")
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "availability_reason", reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DecisionCapability:
        strict_keys(
            value,
            required={
                "decision_id",
                "business_name",
                "display_unit",
                "canonical_unit",
                "lower_bound",
                "upper_bound",
                "coarse_step",
                "refine_step",
                "m2_parameter_path",
                "m4_loop_id",
                "availability",
                "availability_reason",
            },
            context="decision capability",
        )
        availability, reason = _availability(
            value["availability"], value["availability_reason"], context="decision capability"
        )
        return cls(
            decision_id=identifier(value["decision_id"], context="decision_id"),
            business_name=text(value["business_name"], context="business_name"),
            display_unit=text(value["display_unit"], context="display_unit"),
            canonical_unit=text(value["canonical_unit"], context="canonical_unit"),
            lower_bound=finite(value["lower_bound"], context="lower_bound"),
            upper_bound=finite(value["upper_bound"], context="upper_bound"),
            coarse_step=finite(value["coarse_step"], context="coarse_step"),
            refine_step=finite(value["refine_step"], context="refine_step"),
            m2_parameter_path=_optional_text(
                value["m2_parameter_path"], context="m2_parameter_path"
            ),
            m4_loop_id=_optional_identifier(value["m4_loop_id"], context="m4_loop_id"),
            availability=availability,
            availability_reason=reason,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "business_name": self.business_name,
            "display_unit": self.display_unit,
            "canonical_unit": self.canonical_unit,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "coarse_step": self.coarse_step,
            "refine_step": self.refine_step,
            "m2_parameter_path": self.m2_parameter_path,
            "m4_loop_id": self.m4_loop_id,
            "availability": self.availability,
            "availability_reason": self.availability_reason,
        }


@dataclass(frozen=True)
class GuardrailCapability:
    guardrail_id: str
    business_name: str
    metric_id: str
    stage: str
    unit: str
    allowed_operators: tuple[GuardrailOperator, ...]
    availability: Availability
    availability_reason: str | None

    def __post_init__(self) -> None:
        for name in ("guardrail_id", "metric_id", "stage"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "business_name", text(self.business_name, context="business_name"))
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        operators = tuple(self.allowed_operators)
        if (
            not operators
            or len(operators) != len(set(operators))
            or any(item not in {"eq", "le", "ge"} for item in operators)
        ):
            raise ValueError("allowed_operators must be non-empty, unique and supported")
        object.__setattr__(self, "allowed_operators", operators)
        availability, reason = _availability(
            self.availability, self.availability_reason, context="guardrail"
        )
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "availability_reason", reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> GuardrailCapability:
        strict_keys(
            value,
            required={
                "guardrail_id",
                "business_name",
                "metric_id",
                "stage",
                "unit",
                "allowed_operators",
                "availability",
                "availability_reason",
            },
            context="guardrail capability",
        )
        raw_operators = as_sequence(value["allowed_operators"], context="allowed_operators")
        operators: list[GuardrailOperator] = []
        for item in raw_operators:
            if item not in {"eq", "le", "ge"}:
                raise ValueError("unsupported guardrail operator")
            operators.append(item)
        availability, reason = _availability(
            value["availability"], value["availability_reason"], context="guardrail capability"
        )
        return cls(
            guardrail_id=identifier(value["guardrail_id"], context="guardrail_id"),
            business_name=text(value["business_name"], context="business_name"),
            metric_id=identifier(value["metric_id"], context="metric_id"),
            stage=identifier(value["stage"], context="stage"),
            unit=text(value["unit"], context="unit"),
            allowed_operators=tuple(operators),
            availability=availability,
            availability_reason=reason,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "guardrail_id": self.guardrail_id,
            "business_name": self.business_name,
            "metric_id": self.metric_id,
            "stage": self.stage,
            "unit": self.unit,
            "allowed_operators": list(self.allowed_operators),
            "availability": self.availability,
            "availability_reason": self.availability_reason,
        }


@dataclass(frozen=True)
class SelectorCapability:
    selector_id: str
    business_name: str
    method: SelectorMethod
    minimum_objectives: int
    maximum_objectives: int
    availability: Availability
    availability_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "selector_id", identifier(self.selector_id, context="selector_id"))
        object.__setattr__(self, "business_name", text(self.business_name, context="business_name"))
        if self.method not in {"single-objective", "lexicographic"}:
            raise ValueError("unsupported selector method")
        minimum = integer(self.minimum_objectives, context="minimum_objectives", minimum=1)
        maximum = integer(self.maximum_objectives, context="maximum_objectives", minimum=1)
        if minimum > maximum:
            raise ValueError("selector objective range is inverted")
        object.__setattr__(self, "minimum_objectives", minimum)
        object.__setattr__(self, "maximum_objectives", maximum)
        availability, reason = _availability(
            self.availability, self.availability_reason, context="selector"
        )
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "availability_reason", reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SelectorCapability:
        strict_keys(
            value,
            required={
                "selector_id",
                "business_name",
                "method",
                "minimum_objectives",
                "maximum_objectives",
                "availability",
                "availability_reason",
            },
            context="selector capability",
        )
        method = value["method"]
        if method not in {"single-objective", "lexicographic"}:
            raise ValueError("unsupported selector method")
        availability, reason = _availability(
            value["availability"], value["availability_reason"], context="selector capability"
        )
        return cls(
            selector_id=identifier(value["selector_id"], context="selector_id"),
            business_name=text(value["business_name"], context="business_name"),
            method=method,
            minimum_objectives=integer(
                value["minimum_objectives"], context="minimum_objectives", minimum=1
            ),
            maximum_objectives=integer(
                value["maximum_objectives"], context="maximum_objectives", minimum=1
            ),
            availability=availability,
            availability_reason=reason,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "selector_id": self.selector_id,
            "business_name": self.business_name,
            "method": self.method,
            "minimum_objectives": self.minimum_objectives,
            "maximum_objectives": self.maximum_objectives,
            "availability": self.availability,
            "availability_reason": self.availability_reason,
        }


@dataclass(frozen=True)
class CapabilityCatalog:
    schema_version: str
    catalog_id: str
    catalog_version: str
    claim_scope: str
    metrics: tuple[MetricCapability, ...]
    objectives: tuple[ObjectiveCapability, ...]
    decisions: tuple[DecisionCapability, ...]
    guardrails: tuple[GuardrailCapability, ...]
    selectors: tuple[SelectorCapability, ...]

    def __post_init__(self) -> None:
        _schema_and_claim(self.schema_version, self.claim_scope)
        for name in ("catalog_id", "catalog_version"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        collections: tuple[tuple[object, ...], ...] = (
            tuple(self.metrics),
            tuple(self.objectives),
            tuple(self.decisions),
            tuple(self.guardrails),
            tuple(self.selectors),
        )
        if any(not values for values in collections):
            raise ValueError("every capability kind must contain at least one atom")
        ids_by_kind = (
            tuple(item.metric_id for item in self.metrics),
            tuple(item.objective_id for item in self.objectives),
            tuple(item.decision_id for item in self.decisions),
            tuple(item.guardrail_id for item in self.guardrails),
            tuple(item.selector_id for item in self.selectors),
        )
        for ids in ids_by_kind:
            if len(ids) != len(set(ids)) or ids != tuple(sorted(ids)):
                raise ValueError("capability ids must be unique and sorted within each kind")
        all_ids = tuple(item for ids in ids_by_kind for item in ids)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("capability ids must be globally unique")
        metric_by_id = {item.metric_id: item for item in self.metrics}
        for objective in self.objectives:
            metric = metric_by_id.get(objective.metric_id)
            if metric is None:
                raise ValueError("objective references an unknown metric")
            if metric.direction != objective.sense:
                raise ValueError("objective sense differs from its metric direction")
        for guardrail in self.guardrails:
            metric = metric_by_id.get(guardrail.metric_id)
            if metric is None:
                raise ValueError("guardrail references an unknown metric")
            if metric.stage != guardrail.stage or metric.unit != guardrail.unit:
                raise ValueError("guardrail stage or unit differs from its metric")
        object.__setattr__(self, "metrics", tuple(self.metrics))
        object.__setattr__(self, "objectives", tuple(self.objectives))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "guardrails", tuple(self.guardrails))
        object.__setattr__(self, "selectors", tuple(self.selectors))

    @classmethod
    def from_mapping(cls, value: object) -> CapabilityCatalog:
        raw = as_mapping(value, context="capability catalog")
        strict_keys(
            raw,
            required={
                "schema_version",
                "catalog_id",
                "catalog_version",
                "claim_scope",
                "metrics",
                "objectives",
                "decisions",
                "guardrails",
                "selectors",
            },
            context="capability catalog",
        )
        return cls(
            schema_version=text(raw["schema_version"], context="schema_version"),
            catalog_id=identifier(raw["catalog_id"], context="catalog_id"),
            catalog_version=identifier(raw["catalog_version"], context="catalog_version"),
            claim_scope=text(raw["claim_scope"], context="claim_scope"),
            metrics=tuple(
                MetricCapability.from_mapping(as_mapping(item, context="metric capability"))
                for item in as_sequence(raw["metrics"], context="metrics")
            ),
            objectives=tuple(
                ObjectiveCapability.from_mapping(as_mapping(item, context="objective capability"))
                for item in as_sequence(raw["objectives"], context="objectives")
            ),
            decisions=tuple(
                DecisionCapability.from_mapping(as_mapping(item, context="decision capability"))
                for item in as_sequence(raw["decisions"], context="decisions")
            ),
            guardrails=tuple(
                GuardrailCapability.from_mapping(as_mapping(item, context="guardrail capability"))
                for item in as_sequence(raw["guardrails"], context="guardrails")
            ),
            selectors=tuple(
                SelectorCapability.from_mapping(as_mapping(item, context="selector capability"))
                for item in as_sequence(raw["selectors"], context="selectors")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_id": self.catalog_id,
            "catalog_version": self.catalog_version,
            "claim_scope": self.claim_scope,
            "metrics": [item.as_dict() for item in self.metrics],
            "objectives": [item.as_dict() for item in self.objectives],
            "decisions": [item.as_dict() for item in self.decisions],
            "guardrails": [item.as_dict() for item in self.guardrails],
            "selectors": [item.as_dict() for item in self.selectors],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.catalog_id, self.fingerprint)


@dataclass(frozen=True)
class ExecutionRoute:
    route_id: str
    selector_id: str
    minimum_objectives: int
    maximum_objectives: int
    search_algorithm_id: str
    search_algorithm_version: str
    maximum_m2_candidates: int
    m2_preset_id: str
    m4_preset_id: str
    m4_event_time_s: float
    m4_duration_s: float
    m4_time_step_s: float
    top_k: int
    feed_anchor_ratios: tuple[float, ...]
    tie_breaks: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "route_id",
            "selector_id",
            "search_algorithm_id",
            "search_algorithm_version",
            "m2_preset_id",
            "m4_preset_id",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        minimum = integer(self.minimum_objectives, context="minimum_objectives", minimum=1)
        maximum = integer(self.maximum_objectives, context="maximum_objectives", minimum=1)
        if minimum > maximum:
            raise ValueError("execution route objective range is inverted")
        object.__setattr__(self, "minimum_objectives", minimum)
        object.__setattr__(self, "maximum_objectives", maximum)
        object.__setattr__(
            self,
            "maximum_m2_candidates",
            integer(self.maximum_m2_candidates, context="maximum_m2_candidates", minimum=1),
        )
        for name in ("m4_event_time_s", "m4_duration_s", "m4_time_step_s"):
            value = finite(getattr(self, name), context=name)
            if value <= 0.0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.m4_event_time_s >= self.m4_duration_s:
            raise ValueError("M4 event time must precede the route duration")
        object.__setattr__(self, "top_k", integer(self.top_k, context="top_k", minimum=1))
        anchors = tuple(
            finite(item, context="feed_anchor_ratio") for item in self.feed_anchor_ratios
        )
        if (
            not anchors
            or any(item <= 0.0 for item in anchors)
            or len(anchors) != len(set(anchors))
            or anchors != tuple(sorted(anchors))
        ):
            raise ValueError("feed_anchor_ratios must be positive, unique and sorted")
        object.__setattr__(self, "feed_anchor_ratios", anchors)
        tie_breaks = tuple(identifier(item, context="tie_break") for item in self.tie_breaks)
        if not tie_breaks or len(tie_breaks) != len(set(tie_breaks)):
            raise ValueError("tie_breaks must be non-empty and unique")
        object.__setattr__(self, "tie_breaks", tie_breaks)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ExecutionRoute:
        strict_keys(
            value,
            required={
                "route_id",
                "selector_id",
                "minimum_objectives",
                "maximum_objectives",
                "search_algorithm_id",
                "search_algorithm_version",
                "maximum_m2_candidates",
                "m2_preset_id",
                "m4_preset_id",
                "m4_event_time_s",
                "m4_duration_s",
                "m4_time_step_s",
                "top_k",
                "feed_anchor_ratios",
                "tie_breaks",
            },
            context="execution route",
        )
        return cls(
            route_id=identifier(value["route_id"], context="route_id"),
            selector_id=identifier(value["selector_id"], context="selector_id"),
            minimum_objectives=integer(
                value["minimum_objectives"], context="minimum_objectives", minimum=1
            ),
            maximum_objectives=integer(
                value["maximum_objectives"], context="maximum_objectives", minimum=1
            ),
            search_algorithm_id=identifier(
                value["search_algorithm_id"], context="search_algorithm_id"
            ),
            search_algorithm_version=identifier(
                value["search_algorithm_version"], context="search_algorithm_version"
            ),
            maximum_m2_candidates=integer(
                value["maximum_m2_candidates"], context="maximum_m2_candidates", minimum=1
            ),
            m2_preset_id=identifier(value["m2_preset_id"], context="m2_preset_id"),
            m4_preset_id=identifier(value["m4_preset_id"], context="m4_preset_id"),
            m4_event_time_s=finite(value["m4_event_time_s"], context="m4_event_time_s"),
            m4_duration_s=finite(value["m4_duration_s"], context="m4_duration_s"),
            m4_time_step_s=finite(value["m4_time_step_s"], context="m4_time_step_s"),
            top_k=integer(value["top_k"], context="top_k", minimum=1),
            feed_anchor_ratios=tuple(
                finite(item, context="feed_anchor_ratio")
                for item in as_sequence(value["feed_anchor_ratios"], context="feed_anchor_ratios")
            ),
            tie_breaks=_identifiers(value["tie_breaks"], context="tie_breaks"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "selector_id": self.selector_id,
            "minimum_objectives": self.minimum_objectives,
            "maximum_objectives": self.maximum_objectives,
            "search_algorithm_id": self.search_algorithm_id,
            "search_algorithm_version": self.search_algorithm_version,
            "maximum_m2_candidates": self.maximum_m2_candidates,
            "m2_preset_id": self.m2_preset_id,
            "m4_preset_id": self.m4_preset_id,
            "m4_event_time_s": self.m4_event_time_s,
            "m4_duration_s": self.m4_duration_s,
            "m4_time_step_s": self.m4_time_step_s,
            "top_k": self.top_k,
            "feed_anchor_ratios": list(self.feed_anchor_ratios),
            "tie_breaks": list(self.tie_breaks),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.route_id, self.fingerprint)


@dataclass(frozen=True)
class GuardrailBinding:
    guardrail_id: str
    priority: int
    operator: GuardrailOperator
    limit: float
    normalization_scale: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "guardrail_id", identifier(self.guardrail_id, context="guardrail_id")
        )
        object.__setattr__(self, "priority", integer(self.priority, context="priority", minimum=0))
        if self.operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported guardrail binding operator")
        object.__setattr__(self, "limit", finite(self.limit, context="limit"))
        scale = finite(self.normalization_scale, context="normalization_scale")
        if scale <= 0.0:
            raise ValueError("guardrail normalization_scale must be positive")
        object.__setattr__(self, "normalization_scale", scale)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> GuardrailBinding:
        strict_keys(
            value,
            required={"guardrail_id", "priority", "operator", "limit", "normalization_scale"},
            context="guardrail binding",
        )
        operator = value["operator"]
        if operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported guardrail binding operator")
        return cls(
            guardrail_id=identifier(value["guardrail_id"], context="guardrail_id"),
            priority=integer(value["priority"], context="priority", minimum=0),
            operator=operator,
            limit=finite(value["limit"], context="limit"),
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "guardrail_id": self.guardrail_id,
            "priority": self.priority,
            "operator": self.operator,
            "limit": self.limit,
            "normalization_scale": self.normalization_scale,
        }


@dataclass(frozen=True)
class SystemPolicy:
    schema_version: str
    policy_id: str
    policy_version: str
    capability_catalog_id: str
    claim_scope: str
    execution_routes: tuple[ExecutionRoute, ...]
    hard_guardrails: tuple[GuardrailBinding, ...]
    publishability_guardrails: tuple[GuardrailBinding, ...]

    def __post_init__(self) -> None:
        _schema_and_claim(self.schema_version, self.claim_scope)
        for name in (
            "policy_id",
            "policy_version",
            "capability_catalog_id",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        routes = tuple(self.execution_routes)
        hard = tuple(self.hard_guardrails)
        publishability = tuple(self.publishability_guardrails)
        for name, values, ids in (
            ("execution routes", routes, tuple(item.route_id for item in routes)),
            ("hard guardrails", hard, tuple(item.guardrail_id for item in hard)),
            (
                "publishability guardrails",
                publishability,
                tuple(item.guardrail_id for item in publishability),
            ),
        ):
            if not values or len(ids) != len(set(ids)):
                raise ValueError(f"{name} must be non-empty and have unique ids")
        hard_priorities = tuple(item.priority for item in hard)
        publishability_priorities = tuple(item.priority for item in publishability)
        if len(hard_priorities) != len(set(hard_priorities)):
            raise ValueError("hard guardrail priorities must be unique")
        if len(publishability_priorities) != len(set(publishability_priorities)):
            raise ValueError("publishability guardrail priorities must be unique")
        bound_guardrail_ids = tuple(item.guardrail_id for item in (*hard, *publishability))
        if len(bound_guardrail_ids) != len(set(bound_guardrail_ids)):
            raise ValueError("guardrail ids must not repeat across policy sections")
        bound_priorities = tuple(item.priority for item in (*hard, *publishability))
        if len(bound_priorities) != len(set(bound_priorities)):
            raise ValueError("guardrail priorities must not repeat across policy sections")
        object.__setattr__(self, "execution_routes", routes)
        object.__setattr__(self, "hard_guardrails", hard)
        object.__setattr__(self, "publishability_guardrails", publishability)

    @classmethod
    def from_mapping(cls, value: object) -> SystemPolicy:
        raw = as_mapping(value, context="system policy")
        strict_keys(
            raw,
            required={
                "schema_version",
                "policy_id",
                "policy_version",
                "capability_catalog_id",
                "claim_scope",
                "execution_routes",
                "hard_guardrails",
                "publishability_guardrails",
            },
            context="system policy",
        )
        return cls(
            schema_version=text(raw["schema_version"], context="schema_version"),
            policy_id=identifier(raw["policy_id"], context="policy_id"),
            policy_version=identifier(raw["policy_version"], context="policy_version"),
            capability_catalog_id=identifier(
                raw["capability_catalog_id"], context="capability_catalog_id"
            ),
            claim_scope=text(raw["claim_scope"], context="claim_scope"),
            execution_routes=tuple(
                ExecutionRoute.from_mapping(as_mapping(item, context="execution route"))
                for item in as_sequence(raw["execution_routes"], context="execution_routes")
            ),
            hard_guardrails=tuple(
                GuardrailBinding.from_mapping(as_mapping(item, context="hard guardrail"))
                for item in as_sequence(raw["hard_guardrails"], context="hard_guardrails")
            ),
            publishability_guardrails=tuple(
                GuardrailBinding.from_mapping(as_mapping(item, context="publishability guardrail"))
                for item in as_sequence(
                    raw["publishability_guardrails"], context="publishability_guardrails"
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "capability_catalog_id": self.capability_catalog_id,
            "claim_scope": self.claim_scope,
            "execution_routes": [item.as_dict() for item in self.execution_routes],
            "hard_guardrails": [item.as_dict() for item in self.hard_guardrails],
            "publishability_guardrails": [
                item.as_dict() for item in self.publishability_guardrails
            ],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.policy_id, self.fingerprint)


@dataclass(frozen=True)
class CapabilityBundle:
    catalog: CapabilityCatalog
    system_policy: SystemPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, CapabilityCatalog):
            raise TypeError("catalog must be CapabilityCatalog")
        if not isinstance(self.system_policy, SystemPolicy):
            raise TypeError("system_policy must be SystemPolicy")
        if self.system_policy.capability_catalog_id != self.catalog.catalog_id:
            raise ValueError("system policy references another capability catalog")
        if self.catalog.claim_scope != self.system_policy.claim_scope:
            raise ValueError("capability claim scopes differ")

        selector_by_id = {item.selector_id: item for item in self.catalog.selectors}
        covered_counts: set[int] = set()
        for route in self.system_policy.execution_routes:
            selector = selector_by_id.get(route.selector_id)
            if selector is None or selector.availability != "available":
                raise ValueError("execution route references an unavailable selector")
            if (
                route.minimum_objectives < selector.minimum_objectives
                or route.maximum_objectives > selector.maximum_objectives
            ):
                raise ValueError("execution route exceeds selector objective cardinality")
            counts = set(range(route.minimum_objectives, route.maximum_objectives + 1))
            if counts & covered_counts:
                raise ValueError("execution route objective cardinalities overlap")
            covered_counts.update(counts)

        guardrail_by_id = {item.guardrail_id: item for item in self.catalog.guardrails}
        for binding in (
            *self.system_policy.hard_guardrails,
            *self.system_policy.publishability_guardrails,
        ):
            guardrail = guardrail_by_id.get(binding.guardrail_id)
            if guardrail is None or guardrail.availability != "available":
                raise ValueError("system policy references an unavailable guardrail")
            if binding.operator not in guardrail.allowed_operators:
                raise ValueError("system guardrail operator is not published by the catalog")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "catalog_ref": self.catalog.ref.as_dict(),
                "system_policy_ref": self.system_policy.ref.as_dict(),
            }
        )
