"""Immutable RTO V1 contracts used through the R2 simulator boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, cast

from .common import (
    JsonValue,
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    digest,
    finite,
    freeze_json_mapping,
    identifier,
    integer,
    numeric_mapping,
    strict_keys,
    string_mapping,
    text,
    thaw_json,
)
from .reference import ContractRef as ContractRef  # noqa: PLC0414

RTO_SCHEMA_VERSION: Final[str] = "1.0.0"
CLAIM_SCOPE: Final[str] = "engineering_simulation_only"

DecisionRole = Literal["decision", "context", "deferred"]
EvaluationStage = Literal["M2", "M4"]
PairRole = Literal["baseline", "candidate"]
ObjectiveSense = Literal["minimize", "maximize"]
ConstraintKind = Literal["hard", "publishability"]
ConstraintOperator = Literal["eq", "le", "ge"]

_SUPPORTED_UNITS: Final[frozenset[str]] = frozenset(
    {"1", "K", "MJ/t", "MPa(g)", "Pa(a)", "degC", "kg/s", "mass_fraction", "t/h"}
)


def _schema(value: str) -> None:
    if value != RTO_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V1 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _unit(value: object, *, context: str) -> str:
    result = text(value, context=context)
    if result not in _SUPPORTED_UNITS:
        raise ValueError(f"{context} is not supported by the RTO V1 unit registry")
    return result


def _timestamp(value: object, *, context: str) -> str:
    raw = text(value, context=context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include a UTC offset")
    return raw


@dataclass(frozen=True)
class DecisionVariableSpecV1:
    variable_id: str
    business_name: str
    role: DecisionRole
    enabled: bool
    display_unit: str
    canonical_unit: str
    nominal_value: float
    lower_bound: float
    upper_bound: float
    coarse_step: float
    refine_step: float
    m2_parameter: str | None
    m4_loop: str | None
    controller_owner: str
    compiler_rule_id: str
    confidence: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "variable_id", identifier(self.variable_id, context="variable_id"))
        object.__setattr__(self, "business_name", text(self.business_name, context="business_name"))
        if self.role not in {"decision", "context", "deferred"}:
            raise ValueError("unsupported decision variable role")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be boolean")
        for name in ("display_unit", "canonical_unit"):
            object.__setattr__(self, name, _unit(getattr(self, name), context=name))
        for name in ("controller_owner", "confidence"):
            object.__setattr__(self, name, text(getattr(self, name), context=name))
        for name in (
            "nominal_value",
            "lower_bound",
            "upper_bound",
            "coarse_step",
            "refine_step",
        ):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if self.lower_bound > self.nominal_value or self.nominal_value > self.upper_bound:
            raise ValueError("nominal value must lie inside local bounds")
        if self.coarse_step <= 0.0 or self.refine_step <= 0.0:
            raise ValueError("decision steps must be positive")
        for name in ("m2_parameter", "m4_loop"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, text(value, context=name))
        object.__setattr__(
            self,
            "compiler_rule_id",
            identifier(self.compiler_rule_id, context="compiler_rule_id"),
        )
        if (
            self.enabled
            and self.role == "decision"
            and (self.m2_parameter is None or self.m4_loop is None)
        ):
            raise ValueError("enabled decisions require both M2 and M4 mappings")
        if self.role != "decision" and self.enabled:
            raise ValueError("only decision-role variables may be enabled")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DecisionVariableSpecV1:
        required = {
            "variable_id",
            "business_name",
            "role",
            "enabled",
            "display_unit",
            "canonical_unit",
            "nominal_value",
            "lower_bound",
            "upper_bound",
            "coarse_step",
            "refine_step",
            "m2_parameter",
            "m4_loop",
            "controller_owner",
            "compiler_rule_id",
            "confidence",
        }
        strict_keys(value, required=required, context="decision variable")
        role = value["role"]
        if role not in {"decision", "context", "deferred"}:
            raise ValueError("unsupported decision variable role")
        return cls(
            variable_id=identifier(value["variable_id"], context="variable_id"),
            business_name=text(value["business_name"], context="business_name"),
            role=role,
            enabled=boolean(value["enabled"], context="enabled"),
            display_unit=_unit(value["display_unit"], context="display_unit"),
            canonical_unit=_unit(value["canonical_unit"], context="canonical_unit"),
            nominal_value=finite(value["nominal_value"], context="nominal_value"),
            lower_bound=finite(value["lower_bound"], context="lower_bound"),
            upper_bound=finite(value["upper_bound"], context="upper_bound"),
            coarse_step=finite(value["coarse_step"], context="coarse_step"),
            refine_step=finite(value["refine_step"], context="refine_step"),
            m2_parameter=(
                None
                if value["m2_parameter"] is None
                else text(value["m2_parameter"], context="m2_parameter")
            ),
            m4_loop=(
                None if value["m4_loop"] is None else text(value["m4_loop"], context="m4_loop")
            ),
            controller_owner=text(value["controller_owner"], context="controller_owner"),
            compiler_rule_id=identifier(value["compiler_rule_id"], context="compiler_rule_id"),
            confidence=text(value["confidence"], context="confidence"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "variable_id": self.variable_id,
            "business_name": self.business_name,
            "role": self.role,
            "enabled": self.enabled,
            "display_unit": self.display_unit,
            "canonical_unit": self.canonical_unit,
            "nominal_value": self.nominal_value,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "coarse_step": self.coarse_step,
            "refine_step": self.refine_step,
            "m2_parameter": self.m2_parameter,
            "m4_loop": self.m4_loop,
            "controller_owner": self.controller_owner,
            "compiler_rule_id": self.compiler_rule_id,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class DecisionVariableCatalogV1:
    schema_version: str
    catalog_version: str
    catalog_id: str
    claim_scope: str
    variables: tuple[DecisionVariableSpecV1, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "catalog_version", identifier(self.catalog_version, context="catalog_version")
        )
        object.__setattr__(self, "catalog_id", identifier(self.catalog_id, context="catalog_id"))
        variables = tuple(self.variables)
        if not variables or any(not isinstance(item, DecisionVariableSpecV1) for item in variables):
            raise TypeError("decision catalog requires DecisionVariableSpecV1 values")
        ids = tuple(item.variable_id for item in variables)
        if len(ids) != len(set(ids)) or ids != tuple(sorted(ids)):
            raise ValueError("decision variables must have unique, sorted ids")
        object.__setattr__(self, "variables", variables)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DecisionVariableCatalogV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "catalog_version",
                "catalog_id",
                "claim_scope",
                "variables",
            },
            context="decision variable catalog",
        )
        variables = tuple(
            DecisionVariableSpecV1.from_mapping(as_mapping(item, context="decision variable"))
            for item in as_sequence(value["variables"], context="decision variables")
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            catalog_version=identifier(value["catalog_version"], context="catalog_version"),
            catalog_id=identifier(value["catalog_id"], context="catalog_id"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            variables=variables,
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "claim_scope": self.claim_scope,
            "variables": [item.as_dict() for item in self.variables],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.catalog_id, self.fingerprint)

    def by_id(self, variable_id: str) -> DecisionVariableSpecV1:
        for item in self.variables:
            if item.variable_id == variable_id:
                return item
        raise KeyError(f"unknown decision variable {variable_id!r}")


@dataclass(frozen=True)
class KpiSpecV1:
    kpi_id: str
    stage: str
    unit: str
    direction: str
    formula_id: str
    source_paths: tuple[str, ...]
    proxy: bool

    def __post_init__(self) -> None:
        for name in ("kpi_id", "stage", "direction", "formula_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "unit", _unit(self.unit, context="unit"))
        paths = tuple(text(item, context="source_path") for item in self.source_paths)
        if not paths:
            raise ValueError("KPI requires at least one source path")
        object.__setattr__(self, "source_paths", paths)
        if not isinstance(self.proxy, bool):
            raise TypeError("proxy must be boolean")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> KpiSpecV1:
        strict_keys(
            value,
            required={
                "kpi_id",
                "stage",
                "unit",
                "direction",
                "formula_id",
                "source_paths",
                "proxy",
            },
            context="KPI spec",
        )
        return cls(
            kpi_id=identifier(value["kpi_id"], context="kpi_id"),
            stage=identifier(value["stage"], context="stage"),
            unit=_unit(value["unit"], context="unit"),
            direction=identifier(value["direction"], context="direction"),
            formula_id=identifier(value["formula_id"], context="formula_id"),
            source_paths=tuple(
                text(item, context="source_path")
                for item in as_sequence(value["source_paths"], context="source_paths")
            ),
            proxy=boolean(value["proxy"], context="proxy"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kpi_id": self.kpi_id,
            "stage": self.stage,
            "unit": self.unit,
            "direction": self.direction,
            "formula_id": self.formula_id,
            "source_paths": list(self.source_paths),
            "proxy": self.proxy,
        }


@dataclass(frozen=True)
class KpiCatalogV1:
    schema_version: str
    catalog_version: str
    catalog_id: str
    claim_scope: str
    kpis: tuple[KpiSpecV1, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "catalog_version", identifier(self.catalog_version, context="catalog_version")
        )
        object.__setattr__(self, "catalog_id", identifier(self.catalog_id, context="catalog_id"))
        kpis = tuple(self.kpis)
        ids = tuple(item.kpi_id for item in kpis)
        if not kpis or len(ids) != len(set(ids)) or ids != tuple(sorted(ids)):
            raise ValueError("KPI ids must be non-empty, unique and sorted")
        object.__setattr__(self, "kpis", kpis)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> KpiCatalogV1:
        strict_keys(
            value,
            required={"schema_version", "catalog_version", "catalog_id", "claim_scope", "kpis"},
            context="KPI catalog",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            catalog_version=identifier(value["catalog_version"], context="catalog_version"),
            catalog_id=identifier(value["catalog_id"], context="catalog_id"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            kpis=tuple(
                KpiSpecV1.from_mapping(as_mapping(item, context="KPI spec"))
                for item in as_sequence(value["kpis"], context="KPIs")
            ),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "claim_scope": self.claim_scope,
            "kpis": [item.as_dict() for item in self.kpis],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.catalog_id, self.fingerprint)

    def by_id(self, kpi_id: str) -> KpiSpecV1:
        for item in self.kpis:
            if item.kpi_id == kpi_id:
                return item
        raise KeyError(f"unknown KPI {kpi_id!r}")


@dataclass(frozen=True)
class ConstraintRuleV1:
    constraint_id: str
    priority: int
    stage: str
    kind: ConstraintKind
    metric_id: str
    operator: ConstraintOperator
    limit: float
    unit: str
    normalization_scale: float

    def __post_init__(self) -> None:
        for name in ("constraint_id", "stage", "metric_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "priority", integer(self.priority, context="priority"))
        if self.kind not in {"hard", "publishability"}:
            raise ValueError("unsupported constraint kind")
        if self.operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported constraint operator")
        object.__setattr__(self, "limit", finite(self.limit, context="limit"))
        object.__setattr__(self, "unit", _unit(self.unit, context="unit"))
        object.__setattr__(
            self,
            "normalization_scale",
            finite(self.normalization_scale, context="normalization_scale"),
        )
        if self.normalization_scale <= 0.0:
            raise ValueError("normalization_scale must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ConstraintRuleV1:
        strict_keys(
            value,
            required={
                "constraint_id",
                "priority",
                "stage",
                "kind",
                "metric_id",
                "operator",
                "limit",
                "unit",
                "normalization_scale",
            },
            context="constraint rule",
        )
        kind = value["kind"]
        operator = value["operator"]
        if kind not in {"hard", "publishability"} or operator not in {"eq", "le", "ge"}:
            raise ValueError("unsupported constraint kind or operator")
        return cls(
            constraint_id=identifier(value["constraint_id"], context="constraint_id"),
            priority=integer(value["priority"], context="priority"),
            stage=identifier(value["stage"], context="stage"),
            kind=kind,
            metric_id=identifier(value["metric_id"], context="metric_id"),
            operator=operator,
            limit=finite(value["limit"], context="limit"),
            unit=_unit(value["unit"], context="unit"),
            normalization_scale=finite(value["normalization_scale"], context="normalization_scale"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "priority": self.priority,
            "stage": self.stage,
            "kind": self.kind,
            "metric_id": self.metric_id,
            "operator": self.operator,
            "limit": self.limit,
            "unit": self.unit,
            "normalization_scale": self.normalization_scale,
        }


@dataclass(frozen=True)
class ConstraintProfileV1:
    schema_version: str
    profile_version: str
    profile_id: str
    claim_scope: str
    rules: tuple[ConstraintRuleV1, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        object.__setattr__(
            self, "profile_version", identifier(self.profile_version, context="profile_version")
        )
        object.__setattr__(self, "profile_id", identifier(self.profile_id, context="profile_id"))
        rules = tuple(self.rules)
        ids = tuple(item.constraint_id for item in rules)
        priorities = tuple(item.priority for item in rules)
        if not rules or len(ids) != len(set(ids)) or priorities != tuple(sorted(priorities)):
            raise ValueError("constraint rules must be unique and priority ordered")
        object.__setattr__(self, "rules", rules)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ConstraintProfileV1:
        strict_keys(
            value,
            required={"schema_version", "profile_version", "profile_id", "claim_scope", "rules"},
            context="constraint profile",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            profile_version=identifier(value["profile_version"], context="profile_version"),
            profile_id=identifier(value["profile_id"], context="profile_id"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            rules=tuple(
                ConstraintRuleV1.from_mapping(as_mapping(item, context="constraint rule"))
                for item in as_sequence(value["rules"], context="constraint rules")
            ),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_version": self.profile_version,
            "profile_id": self.profile_id,
            "claim_scope": self.claim_scope,
            "rules": [item.as_dict() for item in self.rules],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.profile_id, self.fingerprint)


@dataclass(frozen=True)
class OperatingContextV1:
    schema_version: str
    context_version: str
    context_id: str
    provider_id: str
    model_ref: ContractRef
    case_ref: ContractRef
    operating_mode: str
    feed_mass_flow_kg_s: float
    feed_composition: Mapping[str, float]
    current_setpoints: Mapping[str, float]
    initial_inventory_ratios: Mapping[str, float]
    data_timestamp: str
    data_quality: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in (
            "context_version",
            "context_id",
            "provider_id",
            "operating_mode",
            "data_quality",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if not isinstance(self.model_ref, ContractRef) or not isinstance(
            self.case_ref, ContractRef
        ):
            raise TypeError("context model_ref and case_ref must be ContractRef values")
        object.__setattr__(
            self,
            "feed_mass_flow_kg_s",
            finite(self.feed_mass_flow_kg_s, context="feed_mass_flow_kg_s"),
        )
        if self.feed_mass_flow_kg_s <= 0.0:
            raise ValueError("feed_mass_flow_kg_s must be positive")
        composition = numeric_mapping(self.feed_composition, context="feed_composition")
        if (
            any(value < 0.0 for value in composition.values())
            or abs(sum(composition.values()) - 1.0) > 1e-12
        ):
            raise ValueError("feed composition must be non-negative and sum to one")
        object.__setattr__(self, "feed_composition", composition)
        setpoints = numeric_mapping(self.current_setpoints, context="current_setpoints")
        if set(setpoints) != {"furnace_temperature_target_k", "tower_top_pressure_target_pa_a"}:
            raise ValueError("context must define exactly the two V1 high-level setpoints")
        object.__setattr__(self, "current_setpoints", setpoints)
        ratios = numeric_mapping(self.initial_inventory_ratios, context="initial_inventory_ratios")
        if set(ratios) != {"flash_drum", "reflux_drum", "tower_bottom"} or any(
            value <= 0.0 for value in ratios.values()
        ):
            raise ValueError("context must define three positive inventory ratios")
        object.__setattr__(self, "initial_inventory_ratios", ratios)
        object.__setattr__(
            self, "data_timestamp", _timestamp(self.data_timestamp, context="data_timestamp")
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OperatingContextV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "context_version",
                "context_id",
                "provider_id",
                "model_ref",
                "case_ref",
                "operating_mode",
                "feed_mass_flow_kg_s",
                "feed_composition",
                "current_setpoints",
                "initial_inventory_ratios",
                "data_timestamp",
                "data_quality",
                "claim_scope",
            },
            context="operating context",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            context_version=identifier(value["context_version"], context="context_version"),
            context_id=identifier(value["context_id"], context="context_id"),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            model_ref=ContractRef.from_mapping(as_mapping(value["model_ref"], context="model_ref")),
            case_ref=ContractRef.from_mapping(as_mapping(value["case_ref"], context="case_ref")),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            feed_mass_flow_kg_s=finite(value["feed_mass_flow_kg_s"], context="feed_mass_flow_kg_s"),
            feed_composition=numeric_mapping(value["feed_composition"], context="feed_composition"),
            current_setpoints=numeric_mapping(
                value["current_setpoints"], context="current_setpoints"
            ),
            initial_inventory_ratios=numeric_mapping(
                value["initial_inventory_ratios"], context="initial_inventory_ratios"
            ),
            data_timestamp=_timestamp(value["data_timestamp"], context="data_timestamp"),
            data_quality=identifier(value["data_quality"], context="data_quality"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_version": self.context_version,
            "context_id": self.context_id,
            "provider_id": self.provider_id,
            "model_ref": self.model_ref.as_dict(),
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "feed_mass_flow_kg_s": self.feed_mass_flow_kg_s,
            "feed_composition": dict(self.feed_composition),
            "current_setpoints": dict(self.current_setpoints),
            "initial_inventory_ratios": dict(self.initial_inventory_ratios),
            "data_timestamp": self.data_timestamp,
            "data_quality": self.data_quality,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.context_id, self.fingerprint)


@dataclass(frozen=True)
class OptimizationIntentV1:
    schema_version: str
    intent_version: str
    intent_id: str
    source_type: str
    source_ref: str
    original_text: str
    operating_context_ref: ContractRef
    objective_metric_id: str
    objective_sense: ObjectiveSense
    priority_profile_id: str
    decision_profile_id: str
    constraint_profile_id: str
    requested_output: str
    context_policy: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in (
            "intent_version",
            "intent_id",
            "source_type",
            "source_ref",
            "objective_metric_id",
            "priority_profile_id",
            "decision_profile_id",
            "constraint_profile_id",
            "requested_output",
            "context_policy",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "original_text", text(self.original_text, context="original_text"))
        if not isinstance(self.operating_context_ref, ContractRef):
            raise TypeError("operating_context_ref must be a ContractRef")
        if self.objective_sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationIntentV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "intent_version",
                "intent_id",
                "source_type",
                "source_ref",
                "original_text",
                "operating_context_ref",
                "objective_metric_id",
                "objective_sense",
                "priority_profile_id",
                "decision_profile_id",
                "constraint_profile_id",
                "requested_output",
                "context_policy",
                "claim_scope",
            },
            context="optimization intent",
        )
        sense = value["objective_sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            intent_version=identifier(value["intent_version"], context="intent_version"),
            intent_id=identifier(value["intent_id"], context="intent_id"),
            source_type=identifier(value["source_type"], context="source_type"),
            source_ref=identifier(value["source_ref"], context="source_ref"),
            original_text=text(value["original_text"], context="original_text"),
            operating_context_ref=ContractRef.from_mapping(
                as_mapping(value["operating_context_ref"], context="operating_context_ref")
            ),
            objective_metric_id=identifier(
                value["objective_metric_id"], context="objective_metric_id"
            ),
            objective_sense=sense,
            priority_profile_id=identifier(
                value["priority_profile_id"], context="priority_profile_id"
            ),
            decision_profile_id=identifier(
                value["decision_profile_id"], context="decision_profile_id"
            ),
            constraint_profile_id=identifier(
                value["constraint_profile_id"], context="constraint_profile_id"
            ),
            requested_output=identifier(value["requested_output"], context="requested_output"),
            context_policy=identifier(value["context_policy"], context="context_policy"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_version": self.intent_version,
            "intent_id": self.intent_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "operating_context_ref": self.operating_context_ref.as_dict(),
            "objective_metric_id": self.objective_metric_id,
            "objective_sense": self.objective_sense,
            "priority_profile_id": self.priority_profile_id,
            "decision_profile_id": self.decision_profile_id,
            "constraint_profile_id": self.constraint_profile_id,
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


@dataclass(frozen=True)
class EvaluationPlanV1:
    m2_preset_id: str
    m4_preset_id: str
    m4_event_time_s: float
    m4_duration_s: float
    m4_time_step_s: float
    top_k: int
    feed_anchor_ratios: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "m2_preset_id", identifier(self.m2_preset_id, context="m2_preset_id")
        )
        object.__setattr__(
            self, "m4_preset_id", identifier(self.m4_preset_id, context="m4_preset_id")
        )
        for name in ("m4_event_time_s", "m4_duration_s", "m4_time_step_s"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if not 0.0 <= self.m4_event_time_s < self.m4_duration_s or self.m4_time_step_s <= 0.0:
            raise ValueError("M4 evaluation time settings are invalid")
        object.__setattr__(self, "top_k", integer(self.top_k, context="top_k", minimum=1))
        ratios = tuple(
            finite(item, context="feed_anchor_ratio") for item in self.feed_anchor_ratios
        )
        if ratios != tuple(sorted(set(ratios))) or any(item <= 0.0 for item in ratios):
            raise ValueError("feed anchor ratios must be positive, unique and sorted")
        object.__setattr__(self, "feed_anchor_ratios", ratios)

    def as_dict(self) -> dict[str, object]:
        return {
            "m2_preset_id": self.m2_preset_id,
            "m4_preset_id": self.m4_preset_id,
            "m4_event_time_s": self.m4_event_time_s,
            "m4_duration_s": self.m4_duration_s,
            "m4_time_step_s": self.m4_time_step_s,
            "top_k": self.top_k,
            "feed_anchor_ratios": list(self.feed_anchor_ratios),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EvaluationPlanV1:
        strict_keys(
            value,
            required={
                "m2_preset_id",
                "m4_preset_id",
                "m4_event_time_s",
                "m4_duration_s",
                "m4_time_step_s",
                "top_k",
                "feed_anchor_ratios",
            },
            context="evaluation plan",
        )
        return cls(
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
        )


@dataclass(frozen=True)
class SearchPlanV1:
    algorithm_id: str
    algorithm_version: str
    maximum_m2_executions: int
    objective_tie_policy: str
    cache_policy: str

    def __post_init__(self) -> None:
        for name in ("algorithm_id", "algorithm_version", "objective_tie_policy", "cache_policy"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(
            self,
            "maximum_m2_executions",
            integer(self.maximum_m2_executions, context="maximum_m2_executions", minimum=1),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "maximum_m2_executions": self.maximum_m2_executions,
            "objective_tie_policy": self.objective_tie_policy,
            "cache_policy": self.cache_policy,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SearchPlanV1:
        strict_keys(
            value,
            required={
                "algorithm_id",
                "algorithm_version",
                "maximum_m2_executions",
                "objective_tie_policy",
                "cache_policy",
            },
            context="search plan",
        )
        return cls(
            algorithm_id=identifier(value["algorithm_id"], context="algorithm_id"),
            algorithm_version=identifier(value["algorithm_version"], context="algorithm_version"),
            maximum_m2_executions=integer(
                value["maximum_m2_executions"],
                context="maximum_m2_executions",
                minimum=1,
            ),
            objective_tie_policy=identifier(
                value["objective_tie_policy"], context="objective_tie_policy"
            ),
            cache_policy=identifier(value["cache_policy"], context="cache_policy"),
        )


@dataclass(frozen=True)
class OptimizationPolicyV1:
    schema_version: str
    policy_version: str
    policy_id: str
    claim_scope: str
    priority_profile_id: str
    evaluation: EvaluationPlanV1
    search: SearchPlanV1

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("policy_version", "policy_id", "priority_profile_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if not isinstance(self.evaluation, EvaluationPlanV1) or not isinstance(
            self.search, SearchPlanV1
        ):
            raise TypeError("policy requires evaluation and search plan values")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationPolicyV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "policy_version",
                "policy_id",
                "claim_scope",
                "priority_profile_id",
                "evaluation",
                "search",
            },
            context="optimization policy",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            policy_version=identifier(value["policy_version"], context="policy_version"),
            policy_id=identifier(value["policy_id"], context="policy_id"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
            priority_profile_id=identifier(
                value["priority_profile_id"], context="priority_profile_id"
            ),
            evaluation=EvaluationPlanV1.from_mapping(
                as_mapping(value["evaluation"], context="evaluation")
            ),
            search=SearchPlanV1.from_mapping(as_mapping(value["search"], context="search")),
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "policy_id": self.policy_id,
            "claim_scope": self.claim_scope,
            "priority_profile_id": self.priority_profile_id,
            "evaluation": self.evaluation.as_dict(),
            "search": self.search.as_dict(),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.policy_id, self.fingerprint)


@dataclass(frozen=True)
class DecisionDomainV1:
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
        for name in ("nominal_value", "lower_bound", "upper_bound", "coarse_step", "refine_step"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if not self.lower_bound <= self.nominal_value <= self.upper_bound:
            raise ValueError("domain nominal value is outside bounds")

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

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DecisionDomainV1:
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


@dataclass(frozen=True)
class OptimizationProblemV1:
    schema_version: str
    problem_version: str
    intent_ref: ContractRef
    context_ref: ContractRef
    decision_catalog_ref: ContractRef
    kpi_catalog_ref: ContractRef
    constraint_profile_ref: ContractRef
    policy_ref: ContractRef
    decision_domains: tuple[DecisionDomainV1, ...]
    objective_metric_id: str
    objective_sense: ObjectiveSense
    constraints: tuple[ConstraintRuleV1, ...]
    evaluation_plan: EvaluationPlanV1
    search_plan: SearchPlanV1
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
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        domains = tuple(self.decision_domains)
        ids = tuple(item.variable_id for item in domains)
        if not domains or len(ids) != len(set(ids)) or ids != tuple(sorted(ids)):
            raise ValueError("problem decision domains must be non-empty, unique and sorted")
        object.__setattr__(self, "decision_domains", domains)
        object.__setattr__(
            self,
            "objective_metric_id",
            identifier(self.objective_metric_id, context="objective_metric_id"),
        )
        if self.objective_sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        object.__setattr__(self, "constraints", tuple(self.constraints))
        if not isinstance(self.evaluation_plan, EvaluationPlanV1) or not isinstance(
            self.search_plan, SearchPlanV1
        ):
            raise TypeError("problem plans have invalid types")

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
            "decision_domains": [item.as_dict() for item in self.decision_domains],
            "objective_metric_id": self.objective_metric_id,
            "objective_sense": self.objective_sense,
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
    def from_mapping(cls, value: Mapping[str, object]) -> OptimizationProblemV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "problem_version",
                "intent_ref",
                "context_ref",
                "decision_catalog_ref",
                "kpi_catalog_ref",
                "constraint_profile_ref",
                "policy_ref",
                "decision_domains",
                "objective_metric_id",
                "objective_sense",
                "constraints",
                "evaluation_plan",
                "search_plan",
                "claim_scope",
            },
            optional={"problem_id", "problem_fingerprint"},
            context="optimization problem",
        )
        sense = value["objective_sense"]
        if sense not in {"minimize", "maximize"}:
            raise ValueError("unsupported objective sense")
        problem = cls(
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
            decision_domains=tuple(
                DecisionDomainV1.from_mapping(as_mapping(item, context="decision domain"))
                for item in as_sequence(value["decision_domains"], context="decision domains")
            ),
            objective_metric_id=identifier(
                value["objective_metric_id"], context="objective_metric_id"
            ),
            objective_sense=sense,
            constraints=tuple(
                ConstraintRuleV1.from_mapping(as_mapping(item, context="constraint rule"))
                for item in as_sequence(value["constraints"], context="constraints")
            ),
            evaluation_plan=EvaluationPlanV1.from_mapping(
                as_mapping(value["evaluation_plan"], context="evaluation_plan")
            ),
            search_plan=SearchPlanV1.from_mapping(
                as_mapping(value["search_plan"], context="search_plan")
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("problem_id") not in {None, problem.problem_id}:
            raise ValueError("problem_id differs from problem content")
        supplied = value.get("problem_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="problem_fingerprint") != problem.fingerprint
        ):
            raise ValueError("problem_fingerprint differs from problem content")
        return problem


@dataclass(frozen=True)
class CandidateProposalV1:
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
    def from_mapping(cls, value: Mapping[str, object]) -> CandidateProposalV1:
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
class SimulationEvaluationRequestV1:
    schema_version: str
    request_version: str
    stage: EvaluationStage
    pair_id: str
    pair_role: PairRole
    problem_ref: ContractRef
    context_ref: ContractRef
    proposal_ref: ContractRef | None
    provider_id: str
    compiler_version: str
    provider_request: Mapping[str, JsonValue]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("request_version", "pair_id", "provider_id", "compiler_version"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.stage not in {"M2", "M4"} or self.pair_role not in {"baseline", "candidate"}:
            raise ValueError("unsupported simulation stage or pair role")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.context_ref, ContractRef
        ):
            raise TypeError("simulation refs must be ContractRef values")
        if self.pair_role == "candidate" and not isinstance(self.proposal_ref, ContractRef):
            raise TypeError("candidate simulation request requires a proposal ref")
        if self.pair_role == "baseline" and self.proposal_ref is not None:
            raise ValueError("baseline simulation request cannot carry a proposal ref")
        object.__setattr__(
            self,
            "provider_request",
            freeze_json_mapping(self.provider_request, context="provider_request"),
        )

    @property
    def provider_request_fingerprint(self) -> str:
        return canonical_fingerprint(self.provider_request)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "stage": self.stage,
            "pair_id": self.pair_id,
            "pair_role": self.pair_role,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "proposal_ref": None if self.proposal_ref is None else self.proposal_ref.as_dict(),
            "provider_id": self.provider_id,
            "compiler_version": self.compiler_version,
            "provider_request": thaw_json(cast(JsonValue, self.provider_request)),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def request_id(self) -> str:
        return f"simulation-{self.fingerprint[:16]}"

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.request_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "request_id": self.request_id,
            "simulation_request_fingerprint": self.fingerprint,
            "provider_request_fingerprint": self.provider_request_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SimulationEvaluationRequestV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "request_version",
                "stage",
                "pair_id",
                "pair_role",
                "problem_ref",
                "context_ref",
                "proposal_ref",
                "provider_id",
                "compiler_version",
                "provider_request",
                "claim_scope",
            },
            optional={
                "request_id",
                "simulation_request_fingerprint",
                "provider_request_fingerprint",
            },
            context="simulation evaluation request",
        )
        stage = value["stage"]
        pair_role = value["pair_role"]
        if stage not in {"M2", "M4"} or pair_role not in {"baseline", "candidate"}:
            raise ValueError("unsupported simulation stage or pair role")
        raw_proposal_ref = value["proposal_ref"]
        request = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            request_version=identifier(value["request_version"], context="request_version"),
            stage=stage,
            pair_id=identifier(value["pair_id"], context="pair_id"),
            pair_role=pair_role,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            proposal_ref=(
                None
                if raw_proposal_ref is None
                else ContractRef.from_mapping(as_mapping(raw_proposal_ref, context="proposal_ref"))
            ),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            compiler_version=identifier(value["compiler_version"], context="compiler_version"),
            provider_request=freeze_json_mapping(
                value["provider_request"], context="provider_request"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("request_id") not in {None, request.request_id}:
            raise ValueError("request_id differs from request content")
        supplied = value.get("simulation_request_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="simulation_request_fingerprint") != request.fingerprint
        ):
            raise ValueError("simulation_request_fingerprint differs from request content")
        supplied_provider = value.get("provider_request_fingerprint")
        if (
            supplied_provider is not None
            and digest(supplied_provider, context="provider_request_fingerprint")
            != request.provider_request_fingerprint
        ):
            raise ValueError("provider_request_fingerprint differs from provider request")
        return request


@dataclass(frozen=True)
class SimulationPreviewV1:
    schema_version: str
    preview_version: str
    simulation_request_ref: ContractRef
    provider_id: str
    provider_preview_fingerprint: str
    effective_input_fingerprint: str
    base_object_fingerprints: Mapping[str, str]
    effective_object_fingerprints: Mapping[str, str]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        if not isinstance(self.simulation_request_ref, ContractRef):
            raise TypeError("simulation_request_ref must be a ContractRef")
        object.__setattr__(
            self, "preview_version", identifier(self.preview_version, context="preview_version")
        )
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        object.__setattr__(
            self,
            "provider_preview_fingerprint",
            digest(self.provider_preview_fingerprint, context="provider_preview_fingerprint"),
        )
        object.__setattr__(
            self,
            "effective_input_fingerprint",
            digest(self.effective_input_fingerprint, context="effective_input_fingerprint"),
        )
        object.__setattr__(
            self,
            "base_object_fingerprints",
            MappingProxyType(
                {
                    identifier(key, context="base object key"): digest(
                        value, context="base object fingerprint"
                    )
                    for key, value in self.base_object_fingerprints.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "effective_object_fingerprints",
            MappingProxyType(
                {
                    identifier(key, context="effective object key"): digest(
                        value, context="effective object fingerprint"
                    )
                    for key, value in self.effective_object_fingerprints.items()
                }
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "preview_version": self.preview_version,
            "simulation_request_ref": self.simulation_request_ref.as_dict(),
            "provider_id": self.provider_id,
            "provider_preview_fingerprint": self.provider_preview_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "base_object_fingerprints": dict(self.base_object_fingerprints),
            "effective_object_fingerprints": dict(self.effective_object_fingerprints),
            "claim_scope": self.claim_scope,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SimulationPreviewV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "preview_version",
                "simulation_request_ref",
                "provider_id",
                "provider_preview_fingerprint",
                "effective_input_fingerprint",
                "base_object_fingerprints",
                "effective_object_fingerprints",
                "claim_scope",
            },
            context="simulation preview",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            preview_version=identifier(value["preview_version"], context="preview_version"),
            simulation_request_ref=ContractRef.from_mapping(
                as_mapping(value["simulation_request_ref"], context="simulation_request_ref")
            ),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            provider_preview_fingerprint=digest(
                value["provider_preview_fingerprint"],
                context="provider_preview_fingerprint",
            ),
            effective_input_fingerprint=digest(
                value["effective_input_fingerprint"], context="effective_input_fingerprint"
            ),
            base_object_fingerprints=string_mapping(
                value["base_object_fingerprints"], context="base_object_fingerprints"
            ),
            effective_object_fingerprints=string_mapping(
                value["effective_object_fingerprints"],
                context="effective_object_fingerprints",
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )


@dataclass(frozen=True)
class SimulationRunBundleV1:
    schema_version: str
    bundle_version: str
    provider_id: str
    provider_request_fingerprint: str
    run_ref: str
    runtime_status: str
    engine_status: str
    summary: Mapping[str, JsonValue]
    sample_count: int
    event_count: int
    request_fingerprint: str
    effective_input_fingerprint: str
    result_fingerprint: str
    manifest_fingerprint: str
    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    failure_stage: str | None
    failure_reason: str | None
    synthetic: bool
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("bundle_version", "provider_id", "runtime_status", "engine_status"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "run_ref", text(self.run_ref, context="run_ref"))
        for name in (
            "provider_request_fingerprint",
            "request_fingerprint",
            "effective_input_fingerprint",
            "result_fingerprint",
            "manifest_fingerprint",
        ):
            object.__setattr__(self, name, digest(getattr(self, name), context=name))
        object.__setattr__(self, "summary", freeze_json_mapping(self.summary, context="summary"))
        object.__setattr__(self, "sample_count", integer(self.sample_count, context="sample_count"))
        object.__setattr__(self, "event_count", integer(self.event_count, context="event_count"))
        object.__setattr__(
            self,
            "versions",
            MappingProxyType(
                {
                    identifier(key, context="version key"): text(value, context="version value")
                    for key, value in self.versions.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "source_fingerprints",
            MappingProxyType(
                {
                    text(key, context="source key"): digest(value, context="source fingerprint")
                    for key, value in self.source_fingerprints.items()
                }
            ),
        )
        if self.failure_stage is not None:
            object.__setattr__(
                self, "failure_stage", text(self.failure_stage, context="failure_stage")
            )
        if self.failure_reason is not None:
            object.__setattr__(
                self, "failure_reason", text(self.failure_reason, context="failure_reason")
            )
        if not isinstance(self.synthetic, bool) or not self.synthetic:
            raise ValueError("RTO V1 accepts only explicitly synthetic simulation evidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bundle_version": self.bundle_version,
            "provider_id": self.provider_id,
            "provider_request_fingerprint": self.provider_request_fingerprint,
            "run_ref": self.run_ref,
            "runtime_status": self.runtime_status,
            "engine_status": self.engine_status,
            "summary": thaw_json(cast(JsonValue, self.summary)),
            "sample_count": self.sample_count,
            "event_count": self.event_count,
            "request_fingerprint": self.request_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "synthetic": self.synthetic,
            "claim_scope": self.claim_scope,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SimulationRunBundleV1:
        strict_keys(
            value,
            required={
                "schema_version",
                "bundle_version",
                "provider_id",
                "provider_request_fingerprint",
                "run_ref",
                "runtime_status",
                "engine_status",
                "summary",
                "sample_count",
                "event_count",
                "request_fingerprint",
                "effective_input_fingerprint",
                "result_fingerprint",
                "manifest_fingerprint",
                "versions",
                "source_fingerprints",
                "failure_stage",
                "failure_reason",
                "synthetic",
                "claim_scope",
            },
            context="simulation run bundle",
        )
        raw_failure_stage = value["failure_stage"]
        raw_failure_reason = value["failure_reason"]
        source_fingerprints = as_mapping(
            value["source_fingerprints"], context="source_fingerprints"
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            bundle_version=identifier(value["bundle_version"], context="bundle_version"),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            provider_request_fingerprint=digest(
                value["provider_request_fingerprint"],
                context="provider_request_fingerprint",
            ),
            run_ref=text(value["run_ref"], context="run_ref"),
            runtime_status=identifier(value["runtime_status"], context="runtime_status"),
            engine_status=identifier(value["engine_status"], context="engine_status"),
            summary=freeze_json_mapping(value["summary"], context="summary"),
            sample_count=integer(value["sample_count"], context="sample_count"),
            event_count=integer(value["event_count"], context="event_count"),
            request_fingerprint=digest(value["request_fingerprint"], context="request_fingerprint"),
            effective_input_fingerprint=digest(
                value["effective_input_fingerprint"], context="effective_input_fingerprint"
            ),
            result_fingerprint=digest(value["result_fingerprint"], context="result_fingerprint"),
            manifest_fingerprint=digest(
                value["manifest_fingerprint"], context="manifest_fingerprint"
            ),
            versions=string_mapping(value["versions"], context="versions"),
            source_fingerprints={
                text(key, context="source key"): digest(item, context=f"source_fingerprints.{key}")
                for key, item in source_fingerprints.items()
            },
            failure_stage=(
                None
                if raw_failure_stage is None
                else text(raw_failure_stage, context="failure_stage")
            ),
            failure_reason=(
                None
                if raw_failure_reason is None
                else text(raw_failure_reason, context="failure_reason")
            ),
            synthetic=boolean(value["synthetic"], context="synthetic"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
