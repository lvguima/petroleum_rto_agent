"""Objective-count-neutral immutable strategy and lifecycle contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

from ...contracts.common import (
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
from ...contracts.problem import ENGINEERING_CLAIM_SCOPE, ObjectiveSense
from ...contracts.reference import ContractRef

STRATEGY_SCHEMA_VERSION: Final[str] = "1.0.0"

StrategyCoverage = Literal["point", "sampled_anchors"]
StrategyState = Literal[
    "draft",
    "approved",
    "published",
    "pending_revalidation",
    "superseded",
    "retired",
]
StrategyEventType = Literal[
    "created",
    "approved",
    "published",
    "revalidation_requested",
    "superseded",
    "retired",
]


def _require_schema_and_claim(schema_version: str, claim_scope: str) -> None:
    if schema_version != STRATEGY_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the unified strategy contract")
    if claim_scope != ENGINEERING_CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


def _timestamp(value: object, *, context: str) -> str:
    raw = text(value, context=context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include an explicit timezone")
    return raw


def _ref(value: object, *, context: str) -> ContractRef:
    return ContractRef.from_mapping(as_mapping(value, context=context))


def _optional_ref(value: object, *, context: str) -> ContractRef | None:
    return None if value is None else _ref(value, context=context)


def _refs(value: object, *, context: str) -> tuple[ContractRef, ...]:
    return tuple(
        _ref(item, context=f"{context} item") for item in as_sequence(value, context=context)
    )


def canonical_refs(refs: Sequence[ContractRef]) -> tuple[ContractRef, ...]:
    if any(not isinstance(item, ContractRef) for item in refs):
        raise TypeError("strategy dependencies must contain ContractRef values")
    return tuple(sorted(set(refs), key=lambda item: (item.object_id, item.fingerprint)))


def _validate_identity(
    value: Mapping[str, object],
    *,
    object_field: str,
    fingerprint_field: str,
    ref: ContractRef,
) -> None:
    supplied_ref = value.get(object_field)
    if supplied_ref is not None and _ref(supplied_ref, context=object_field) != ref:
        raise ValueError(f"{object_field} differs from strategy content")
    supplied = value.get(fingerprint_field)
    if supplied is not None and digest(supplied, context=fingerprint_field) != ref.fingerprint:
        raise ValueError(f"{fingerprint_field} differs from strategy content")


@dataclass(frozen=True)
class StrategyObjectiveSummary:
    """One objective component retained from the selected M2 paired evaluation."""

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
        if not math.isclose(
            expected,
            self.directional_absolute_improvement,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("objective improvement differs from baseline and candidate")

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
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyObjectiveSummary:
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
            context="strategy objective summary",
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
class StrategyAnchor:
    """One explicitly evaluated context anchor; no interpolation is implied."""

    context_ref: ContractRef
    context_schema_ref: ContractRef
    model_ref: ContractRef
    case_ref: ContractRef
    operating_mode: str
    applicability_values: Mapping[str, float]
    action_values: Mapping[str, float]
    problem_ref: ContractRef
    capability_catalog_ref: ContractRef
    system_policy_ref: ContractRef
    proposal_ref: ContractRef
    static_evaluation_ref: ContractRef
    dynamic_evaluation_ref: ContractRef
    finalization_result_ref: ContractRef
    objective_summaries: tuple[StrategyObjectiveSummary, ...]
    minimum_normalized_margin: float
    evidence_refs: tuple[ContractRef, ...]

    def __post_init__(self) -> None:
        for name in (
            "context_ref",
            "context_schema_ref",
            "model_ref",
            "case_ref",
            "problem_ref",
            "capability_catalog_ref",
            "system_policy_ref",
            "proposal_ref",
            "static_evaluation_ref",
            "dynamic_evaluation_ref",
            "finalization_result_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        object.__setattr__(
            self,
            "operating_mode",
            identifier(self.operating_mode, context="operating_mode"),
        )
        applicability = numeric_mapping(
            self.applicability_values,
            context="applicability_values",
        )
        action = numeric_mapping(self.action_values, context="action_values")
        if not applicability or not action:
            raise ValueError("strategy anchor applicability and action vectors must be non-empty")
        object.__setattr__(self, "applicability_values", applicability)
        object.__setattr__(self, "action_values", action)
        summaries = tuple(self.objective_summaries)
        ids = tuple(item.metric_id for item in summaries)
        if (
            not summaries
            or any(not isinstance(item, StrategyObjectiveSummary) for item in summaries)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("strategy objective summaries must be non-empty and unique")
        object.__setattr__(self, "objective_summaries", summaries)
        margin = finite(
            self.minimum_normalized_margin,
            context="minimum_normalized_margin",
        )
        if margin < 0.0:
            raise ValueError("verified strategy margin must be non-negative")
        object.__setattr__(self, "minimum_normalized_margin", margin)
        evidence = tuple(self.evidence_refs)
        if not evidence or evidence != canonical_refs(evidence):
            raise ValueError("evidence_refs must be non-empty, unique and sorted")
        object.__setattr__(self, "evidence_refs", evidence)

    def as_dict(self) -> dict[str, object]:
        return {
            "context_ref": self.context_ref.as_dict(),
            "context_schema_ref": self.context_schema_ref.as_dict(),
            "model_ref": self.model_ref.as_dict(),
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "applicability_values": dict(self.applicability_values),
            "action_values": dict(self.action_values),
            "problem_ref": self.problem_ref.as_dict(),
            "capability_catalog_ref": self.capability_catalog_ref.as_dict(),
            "system_policy_ref": self.system_policy_ref.as_dict(),
            "proposal_ref": self.proposal_ref.as_dict(),
            "static_evaluation_ref": self.static_evaluation_ref.as_dict(),
            "dynamic_evaluation_ref": self.dynamic_evaluation_ref.as_dict(),
            "finalization_result_ref": self.finalization_result_ref.as_dict(),
            "objective_summaries": [item.as_dict() for item in self.objective_summaries],
            "minimum_normalized_margin": self.minimum_normalized_margin,
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyAnchor:
        strict_keys(
            value,
            required={
                "context_ref",
                "context_schema_ref",
                "model_ref",
                "case_ref",
                "operating_mode",
                "applicability_values",
                "action_values",
                "problem_ref",
                "capability_catalog_ref",
                "system_policy_ref",
                "proposal_ref",
                "static_evaluation_ref",
                "dynamic_evaluation_ref",
                "finalization_result_ref",
                "objective_summaries",
                "minimum_normalized_margin",
                "evidence_refs",
            },
            context="strategy anchor",
        )
        return cls(
            context_ref=_ref(value["context_ref"], context="context_ref"),
            context_schema_ref=_ref(
                value["context_schema_ref"],
                context="context_schema_ref",
            ),
            model_ref=_ref(value["model_ref"], context="model_ref"),
            case_ref=_ref(value["case_ref"], context="case_ref"),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            applicability_values=numeric_mapping(
                value["applicability_values"],
                context="applicability_values",
            ),
            action_values=numeric_mapping(value["action_values"], context="action_values"),
            problem_ref=_ref(value["problem_ref"], context="problem_ref"),
            capability_catalog_ref=_ref(
                value["capability_catalog_ref"],
                context="capability_catalog_ref",
            ),
            system_policy_ref=_ref(
                value["system_policy_ref"],
                context="system_policy_ref",
            ),
            proposal_ref=_ref(value["proposal_ref"], context="proposal_ref"),
            static_evaluation_ref=_ref(
                value["static_evaluation_ref"],
                context="static_evaluation_ref",
            ),
            dynamic_evaluation_ref=_ref(
                value["dynamic_evaluation_ref"],
                context="dynamic_evaluation_ref",
            ),
            finalization_result_ref=_ref(
                value["finalization_result_ref"],
                context="finalization_result_ref",
            ),
            objective_summaries=tuple(
                StrategyObjectiveSummary.from_mapping(
                    as_mapping(item, context="strategy objective summary")
                )
                for item in as_sequence(
                    value["objective_summaries"],
                    context="objective_summaries",
                )
            ),
            minimum_normalized_margin=finite(
                value["minimum_normalized_margin"],
                context="minimum_normalized_margin",
            ),
            evidence_refs=_refs(value["evidence_refs"], context="evidence_refs"),
        )


@dataclass(frozen=True)
class StrategyEntry:
    """Immutable strategy payload; lifecycle state lives only in append-only events."""

    schema_version: str
    entry_version: str
    strategy_id: str
    revision: int
    supersedes: ContractRef | None
    coverage_kind: StrategyCoverage
    central_context_ref: ContractRef
    context_schema_ref: ContractRef
    model_ref: ContractRef
    case_ref: ContractRef
    operating_mode: str
    anchors: tuple[StrategyAnchor, ...]
    action_values: Mapping[str, float]
    action_units: Mapping[str, str]
    baseline_values: Mapping[str, float]
    objective_order: tuple[str, ...]
    application_method: str
    event_time_s: float
    hold_policy: str
    stop_conditions: tuple[str, ...]
    problem_ref: ContractRef
    capability_catalog_ref: ContractRef
    system_policy_ref: ContractRef
    solver_result_ref: ContractRef
    static_selection_ref: ContractRef
    finalization_result_ref: ContractRef
    publishability_assessment_ref: ContractRef
    selected_proposal_ref: ContractRef
    selected_static_evaluation_ref: ContractRef
    selected_dynamic_evaluation_ref: ContractRef
    dependency_refs: tuple[ContractRef, ...]
    execution_scope: str
    control_authority: str
    field_validated: bool
    dcs_write_capability: bool
    claim_scope: str

    def __post_init__(self) -> None:
        _require_schema_and_claim(self.schema_version, self.claim_scope)
        for name in (
            "entry_version",
            "strategy_id",
            "operating_mode",
            "application_method",
            "hold_policy",
            "execution_scope",
            "control_authority",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        revision = integer(self.revision, context="revision", minimum=1)
        object.__setattr__(self, "revision", revision)
        if self.supersedes is not None and not isinstance(self.supersedes, ContractRef):
            raise TypeError("supersedes must be ContractRef or None")
        if (revision == 1) != (self.supersedes is None):
            raise ValueError("only revisions after one may declare supersedes")
        if self.supersedes is not None and self.supersedes.object_id != (
            f"{self.strategy_id}-r{revision - 1}"
        ):
            raise ValueError("supersedes must reference the direct prior revision")
        if self.coverage_kind not in {"point", "sampled_anchors"}:
            raise ValueError("unsupported strategy coverage_kind")
        ref_names = (
            "central_context_ref",
            "context_schema_ref",
            "model_ref",
            "case_ref",
            "problem_ref",
            "capability_catalog_ref",
            "system_policy_ref",
            "solver_result_ref",
            "static_selection_ref",
            "finalization_result_ref",
            "publishability_assessment_ref",
            "selected_proposal_ref",
            "selected_static_evaluation_ref",
            "selected_dynamic_evaluation_ref",
        )
        for name in ref_names:
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        anchors = tuple(self.anchors)
        if any(not isinstance(item, StrategyAnchor) for item in anchors):
            raise TypeError("anchors must contain StrategyAnchor values")
        ordered = tuple(
            sorted(
                anchors,
                key=lambda item: (item.context_ref.object_id, item.context_ref.fingerprint),
            )
        )
        if (
            not anchors
            or anchors != ordered
            or len({item.context_ref for item in anchors}) != len(anchors)
        ):
            raise ValueError("anchors must be non-empty, unique and context-ref sorted")
        if self.coverage_kind == "point" and len(anchors) != 1:
            raise ValueError("point coverage requires exactly one anchor")
        if self.coverage_kind == "sampled_anchors" and len(anchors) < 2:
            raise ValueError("sampled_anchors coverage requires at least two anchors")
        central = tuple(item for item in anchors if item.context_ref == self.central_context_ref)
        if len(central) != 1:
            raise ValueError("strategy requires exactly one central context anchor")
        object.__setattr__(self, "anchors", anchors)
        action = numeric_mapping(self.action_values, context="action_values")
        units = string_mapping(self.action_units, context="action_units")
        baseline = numeric_mapping(self.baseline_values, context="baseline_values")
        if not action or set(action) != set(units) or set(action) != set(baseline):
            raise ValueError("action, unit and baseline vectors must share non-empty ids")
        object.__setattr__(self, "action_values", action)
        object.__setattr__(self, "action_units", units)
        object.__setattr__(self, "baseline_values", baseline)
        objective_order = tuple(
            identifier(item, context="objective_metric_id") for item in self.objective_order
        )
        if not objective_order or len(objective_order) != len(set(objective_order)):
            raise ValueError("objective_order must be non-empty and unique")
        object.__setattr__(self, "objective_order", objective_order)
        central_anchor = central[0]
        central_objective_bindings = tuple(
            (item.metric_id, item.sense, item.unit, item.formula_id)
            for item in central_anchor.objective_summaries
        )
        applicability_ids = set(central_anchor.applicability_values)
        for anchor in anchors:
            if (
                anchor.context_schema_ref != self.context_schema_ref
                or anchor.model_ref != self.model_ref
                or anchor.case_ref != self.case_ref
                or anchor.operating_mode != self.operating_mode
                or anchor.capability_catalog_ref != self.capability_catalog_ref
                or anchor.system_policy_ref != self.system_policy_ref
                or anchor.finalization_result_ref != self.finalization_result_ref
                or dict(anchor.action_values) != dict(action)
                or set(anchor.applicability_values) != applicability_ids
                or tuple(
                    (item.metric_id, item.sense, item.unit, item.formula_id)
                    for item in anchor.objective_summaries
                )
                != central_objective_bindings
            ):
                raise ValueError("strategy anchors differ from shared payload vectors")
        if tuple(item.metric_id for item in central_anchor.objective_summaries) != objective_order:
            raise ValueError("strategy objective order differs from anchor summaries")
        applicability_vectors = tuple(
            tuple(sorted(item.applicability_values.items())) for item in anchors
        )
        if len(applicability_vectors) != len(set(applicability_vectors)):
            raise ValueError("strategy anchors must have unique applicability vectors")
        if (
            central_anchor.problem_ref != self.problem_ref
            or central_anchor.proposal_ref != self.selected_proposal_ref
            or central_anchor.static_evaluation_ref != self.selected_static_evaluation_ref
            or central_anchor.dynamic_evaluation_ref != self.selected_dynamic_evaluation_ref
            or central_anchor.finalization_result_ref != self.finalization_result_ref
        ):
            raise ValueError("central anchor differs from selected finalization evidence")
        event_time = finite(self.event_time_s, context="event_time_s")
        if event_time < 0.0:
            raise ValueError("event_time_s must be non-negative")
        object.__setattr__(self, "event_time_s", event_time)
        stops = tuple(identifier(item, context="stop_condition") for item in self.stop_conditions)
        if not stops or len(stops) != len(set(stops)):
            raise ValueError("stop_conditions must be non-empty and unique")
        object.__setattr__(self, "stop_conditions", stops)
        dependencies = tuple(self.dependency_refs)
        if not dependencies or dependencies != canonical_refs(dependencies):
            raise ValueError("dependency_refs must be non-empty, unique and sorted")
        required_dependencies = {
            self.context_schema_ref,
            self.model_ref,
            self.case_ref,
            self.capability_catalog_ref,
            self.system_policy_ref,
            *(ref for anchor in anchors for ref in anchor.evidence_refs),
        }
        if not required_dependencies.issubset(dependencies):
            raise ValueError("dependency_refs omit required model, policy or evidence refs")
        object.__setattr__(self, "dependency_refs", dependencies)
        if self.execution_scope != "offline_simulation_only":
            raise ValueError("execution_scope must be offline_simulation_only")
        if self.control_authority != "none":
            raise ValueError("control_authority must be none")
        if not isinstance(self.field_validated, bool) or self.field_validated:
            raise ValueError("field_validated must be false")
        if not isinstance(self.dcs_write_capability, bool) or self.dcs_write_capability:
            raise ValueError("dcs_write_capability must be false")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "entry_version": self.entry_version,
            "strategy_id": self.strategy_id,
            "revision": self.revision,
            "supersedes": None if self.supersedes is None else self.supersedes.as_dict(),
            "coverage_kind": self.coverage_kind,
            "central_context_ref": self.central_context_ref.as_dict(),
            "context_schema_ref": self.context_schema_ref.as_dict(),
            "model_ref": self.model_ref.as_dict(),
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "anchors": [item.as_dict() for item in self.anchors],
            "action_values": dict(self.action_values),
            "action_units": dict(self.action_units),
            "baseline_values": dict(self.baseline_values),
            "objective_order": list(self.objective_order),
            "application_method": self.application_method,
            "event_time_s": self.event_time_s,
            "hold_policy": self.hold_policy,
            "stop_conditions": list(self.stop_conditions),
            "problem_ref": self.problem_ref.as_dict(),
            "capability_catalog_ref": self.capability_catalog_ref.as_dict(),
            "system_policy_ref": self.system_policy_ref.as_dict(),
            "solver_result_ref": self.solver_result_ref.as_dict(),
            "static_selection_ref": self.static_selection_ref.as_dict(),
            "finalization_result_ref": self.finalization_result_ref.as_dict(),
            "publishability_assessment_ref": self.publishability_assessment_ref.as_dict(),
            "selected_proposal_ref": self.selected_proposal_ref.as_dict(),
            "selected_static_evaluation_ref": self.selected_static_evaluation_ref.as_dict(),
            "selected_dynamic_evaluation_ref": self.selected_dynamic_evaluation_ref.as_dict(),
            "dependency_refs": [item.as_dict() for item in self.dependency_refs],
            "execution_scope": self.execution_scope,
            "control_authority": self.control_authority,
            "field_validated": self.field_validated,
            "dcs_write_capability": self.dcs_write_capability,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"{self.strategy_id}-r{self.revision}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "strategy_ref": self.ref.as_dict(),
            "strategy_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyEntry:
        required = {
            "schema_version",
            "entry_version",
            "strategy_id",
            "revision",
            "supersedes",
            "coverage_kind",
            "central_context_ref",
            "context_schema_ref",
            "model_ref",
            "case_ref",
            "operating_mode",
            "anchors",
            "action_values",
            "action_units",
            "baseline_values",
            "objective_order",
            "application_method",
            "event_time_s",
            "hold_policy",
            "stop_conditions",
            "problem_ref",
            "capability_catalog_ref",
            "system_policy_ref",
            "solver_result_ref",
            "static_selection_ref",
            "finalization_result_ref",
            "publishability_assessment_ref",
            "selected_proposal_ref",
            "selected_static_evaluation_ref",
            "selected_dynamic_evaluation_ref",
            "dependency_refs",
            "execution_scope",
            "control_authority",
            "field_validated",
            "dcs_write_capability",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"strategy_ref", "strategy_fingerprint"},
            context="strategy entry",
        )
        coverage = value["coverage_kind"]
        if coverage not in {"point", "sampled_anchors"}:
            raise ValueError("unsupported strategy coverage_kind")
        entry = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            entry_version=identifier(value["entry_version"], context="entry_version"),
            strategy_id=identifier(value["strategy_id"], context="strategy_id"),
            revision=integer(value["revision"], context="revision", minimum=1),
            supersedes=_optional_ref(value["supersedes"], context="supersedes"),
            coverage_kind=coverage,
            central_context_ref=_ref(value["central_context_ref"], context="central_context_ref"),
            context_schema_ref=_ref(value["context_schema_ref"], context="context_schema_ref"),
            model_ref=_ref(value["model_ref"], context="model_ref"),
            case_ref=_ref(value["case_ref"], context="case_ref"),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            anchors=tuple(
                StrategyAnchor.from_mapping(as_mapping(item, context="strategy anchor"))
                for item in as_sequence(value["anchors"], context="anchors")
            ),
            action_values=numeric_mapping(value["action_values"], context="action_values"),
            action_units=string_mapping(value["action_units"], context="action_units"),
            baseline_values=numeric_mapping(
                value["baseline_values"],
                context="baseline_values",
            ),
            objective_order=tuple(
                identifier(item, context="objective_metric_id")
                for item in as_sequence(value["objective_order"], context="objective_order")
            ),
            application_method=identifier(
                value["application_method"],
                context="application_method",
            ),
            event_time_s=finite(value["event_time_s"], context="event_time_s"),
            hold_policy=identifier(value["hold_policy"], context="hold_policy"),
            stop_conditions=tuple(
                identifier(item, context="stop_condition")
                for item in as_sequence(value["stop_conditions"], context="stop_conditions")
            ),
            problem_ref=_ref(value["problem_ref"], context="problem_ref"),
            capability_catalog_ref=_ref(
                value["capability_catalog_ref"],
                context="capability_catalog_ref",
            ),
            system_policy_ref=_ref(value["system_policy_ref"], context="system_policy_ref"),
            solver_result_ref=_ref(value["solver_result_ref"], context="solver_result_ref"),
            static_selection_ref=_ref(
                value["static_selection_ref"],
                context="static_selection_ref",
            ),
            finalization_result_ref=_ref(
                value["finalization_result_ref"],
                context="finalization_result_ref",
            ),
            publishability_assessment_ref=_ref(
                value["publishability_assessment_ref"],
                context="publishability_assessment_ref",
            ),
            selected_proposal_ref=_ref(
                value["selected_proposal_ref"],
                context="selected_proposal_ref",
            ),
            selected_static_evaluation_ref=_ref(
                value["selected_static_evaluation_ref"],
                context="selected_static_evaluation_ref",
            ),
            selected_dynamic_evaluation_ref=_ref(
                value["selected_dynamic_evaluation_ref"],
                context="selected_dynamic_evaluation_ref",
            ),
            dependency_refs=_refs(value["dependency_refs"], context="dependency_refs"),
            execution_scope=identifier(value["execution_scope"], context="execution_scope"),
            control_authority=identifier(value["control_authority"], context="control_authority"),
            field_validated=boolean(value["field_validated"], context="field_validated"),
            dcs_write_capability=boolean(
                value["dcs_write_capability"],
                context="dcs_write_capability",
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        _validate_identity(
            value,
            object_field="strategy_ref",
            fingerprint_field="strategy_fingerprint",
            ref=entry.ref,
        )
        return entry


@dataclass(frozen=True)
class StrategyLifecycleEvent:
    schema_version: str
    event_version: str
    strategy_ref: ContractRef
    sequence: int
    event_type: StrategyEventType
    from_state: StrategyState | None
    to_state: StrategyState
    actor: str
    occurred_at: str
    reason: str
    release_ref: ContractRef | None
    related_strategy_ref: ContractRef | None
    previous_event_fingerprint: str | None
    claim_scope: str

    def __post_init__(self) -> None:
        _require_schema_and_claim(self.schema_version, self.claim_scope)
        object.__setattr__(
            self,
            "event_version",
            identifier(self.event_version, context="event_version"),
        )
        if not isinstance(self.strategy_ref, ContractRef):
            raise TypeError("strategy_ref must be ContractRef")
        object.__setattr__(self, "sequence", integer(self.sequence, context="sequence"))
        transitions: dict[StrategyEventType, tuple[StrategyState | None, StrategyState]] = {
            "created": (None, "draft"),
            "approved": ("draft", "approved"),
            "published": ("approved", "published"),
            "revalidation_requested": ("published", "pending_revalidation"),
            "superseded": ("pending_revalidation", "superseded"),
            "retired": (self.from_state, "retired"),
        }
        if self.event_type not in transitions:
            raise ValueError("unsupported strategy event_type")
        expected_from, expected_to = transitions[self.event_type]
        if self.event_type == "retired":
            if self.from_state not in {
                "draft",
                "approved",
                "published",
                "pending_revalidation",
            }:
                raise ValueError("retired event has an invalid source state")
        elif self.from_state != expected_from:
            raise ValueError("strategy event source state is invalid")
        if self.to_state != expected_to:
            raise ValueError("strategy event target state is invalid")
        object.__setattr__(self, "actor", identifier(self.actor, context="actor"))
        object.__setattr__(
            self,
            "occurred_at",
            _timestamp(self.occurred_at, context="occurred_at"),
        )
        object.__setattr__(self, "reason", text(self.reason, context="reason"))
        if self.release_ref is not None and not isinstance(self.release_ref, ContractRef):
            raise TypeError("release_ref must be ContractRef")
        if (self.event_type == "published") != (self.release_ref is not None):
            raise ValueError("only published events require a release_ref")
        if self.related_strategy_ref is not None and not isinstance(
            self.related_strategy_ref,
            ContractRef,
        ):
            raise TypeError("related_strategy_ref must be ContractRef")
        if (self.event_type == "superseded") != (self.related_strategy_ref is not None):
            raise ValueError("only superseded events require a related strategy")
        if self.previous_event_fingerprint is not None:
            object.__setattr__(
                self,
                "previous_event_fingerprint",
                digest(
                    self.previous_event_fingerprint,
                    context="previous_event_fingerprint",
                ),
            )
        if (self.sequence == 0) != (self.previous_event_fingerprint is None):
            raise ValueError("only the first event may omit previous_event_fingerprint")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_version": self.event_version,
            "strategy_ref": self.strategy_ref.as_dict(),
            "sequence": self.sequence,
            "event_type": self.event_type,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
            "reason": self.reason,
            "release_ref": None if self.release_ref is None else self.release_ref.as_dict(),
            "related_strategy_ref": (
                None if self.related_strategy_ref is None else self.related_strategy_ref.as_dict()
            ),
            "previous_event_fingerprint": self.previous_event_fingerprint,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "event_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyLifecycleEvent:
        strict_keys(
            value,
            required={
                "schema_version",
                "event_version",
                "strategy_ref",
                "sequence",
                "event_type",
                "from_state",
                "to_state",
                "actor",
                "occurred_at",
                "reason",
                "release_ref",
                "related_strategy_ref",
                "previous_event_fingerprint",
                "claim_scope",
            },
            optional={"event_fingerprint"},
            context="strategy lifecycle event",
        )
        event_type = value["event_type"]
        states = {
            "draft",
            "approved",
            "published",
            "pending_revalidation",
            "superseded",
            "retired",
        }
        if event_type not in {
            "created",
            "approved",
            "published",
            "revalidation_requested",
            "superseded",
            "retired",
        }:
            raise ValueError("unsupported strategy event_type")
        if value["from_state"] not in {None, *states} or value["to_state"] not in states:
            raise ValueError("unsupported strategy state")
        event = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            event_version=identifier(value["event_version"], context="event_version"),
            strategy_ref=_ref(value["strategy_ref"], context="strategy_ref"),
            sequence=integer(value["sequence"], context="sequence"),
            event_type=event_type,
            from_state=cast(StrategyState | None, value["from_state"]),
            to_state=cast(StrategyState, value["to_state"]),
            actor=identifier(value["actor"], context="actor"),
            occurred_at=_timestamp(value["occurred_at"], context="occurred_at"),
            reason=text(value["reason"], context="reason"),
            release_ref=_optional_ref(value["release_ref"], context="release_ref"),
            related_strategy_ref=_optional_ref(
                value["related_strategy_ref"],
                context="related_strategy_ref",
            ),
            previous_event_fingerprint=(
                None
                if value["previous_event_fingerprint"] is None
                else digest(
                    value["previous_event_fingerprint"],
                    context="previous_event_fingerprint",
                )
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("event_fingerprint")
        if (
            supplied is not None
            and digest(
                supplied,
                context="event_fingerprint",
            )
            != event.fingerprint
        ):
            raise ValueError("event_fingerprint differs from lifecycle event content")
        return event


@dataclass(frozen=True)
class StrategyReleaseManifest:
    schema_version: str
    release_version: str
    release_id: str
    entry_refs: tuple[ContractRef, ...]
    created_by: str
    created_at: str
    review_scope: str
    execution_scope: str
    claim_scope: str

    def __post_init__(self) -> None:
        _require_schema_and_claim(self.schema_version, self.claim_scope)
        for name in ("release_version", "release_id", "created_by", "review_scope"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        refs = tuple(self.entry_refs)
        if not refs or refs != canonical_refs(refs):
            raise ValueError("release entry_refs must be non-empty, unique and sorted")
        object.__setattr__(self, "entry_refs", refs)
        object.__setattr__(
            self,
            "created_at",
            _timestamp(self.created_at, context="created_at"),
        )
        object.__setattr__(
            self,
            "execution_scope",
            identifier(self.execution_scope, context="execution_scope"),
        )
        if self.review_scope != "offline-human-review":
            raise ValueError("review_scope must be offline-human-review")
        if self.execution_scope != "offline_simulation_only":
            raise ValueError("release execution_scope must be offline_simulation_only")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "release_id": self.release_id,
            "entry_refs": [item.as_dict() for item in self.entry_refs],
            "created_by": self.created_by,
            "created_at": self.created_at,
            "review_scope": self.review_scope,
            "execution_scope": self.execution_scope,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.release_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "release_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleaseManifest:
        strict_keys(
            value,
            required={
                "schema_version",
                "release_version",
                "release_id",
                "entry_refs",
                "created_by",
                "created_at",
                "review_scope",
                "execution_scope",
                "claim_scope",
            },
            optional={"release_fingerprint"},
            context="strategy release",
        )
        release = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            release_version=identifier(value["release_version"], context="release_version"),
            release_id=identifier(value["release_id"], context="release_id"),
            entry_refs=_refs(value["entry_refs"], context="entry_refs"),
            created_by=identifier(value["created_by"], context="created_by"),
            created_at=_timestamp(value["created_at"], context="created_at"),
            review_scope=identifier(value["review_scope"], context="review_scope"),
            execution_scope=identifier(value["execution_scope"], context="execution_scope"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("release_fingerprint")
        if (
            supplied is not None
            and digest(
                supplied,
                context="release_fingerprint",
            )
            != release.fingerprint
        ):
            raise ValueError("release_fingerprint differs from release content")
        return release


@dataclass(frozen=True)
class StrategyRecord:
    entry: StrategyEntry
    events: tuple[StrategyLifecycleEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entry, StrategyEntry):
            raise TypeError("entry must be StrategyEntry")
        events = tuple(self.events)
        if not events or any(not isinstance(item, StrategyLifecycleEvent) for item in events):
            raise ValueError("strategy record requires lifecycle events")
        state: StrategyState | None = None
        previous: str | None = None
        prior_time: datetime | None = None
        for sequence, event in enumerate(events):
            if event.strategy_ref != self.entry.ref or event.sequence != sequence:
                raise ValueError("strategy event identity or sequence differs")
            if event.from_state != state or event.previous_event_fingerprint != previous:
                raise ValueError("strategy event chain is discontinuous")
            event_time = datetime.fromisoformat(event.occurred_at)
            if prior_time is not None and event_time < prior_time:
                raise ValueError("strategy event timestamps must be non-decreasing")
            state = event.to_state
            previous = event.fingerprint
            prior_time = event_time
        object.__setattr__(self, "events", events)

    @property
    def current_state(self) -> StrategyState:
        return self.events[-1].to_state

    @property
    def release_ref(self) -> ContractRef | None:
        refs = tuple(item.release_ref for item in self.events if item.event_type == "published")
        return None if not refs else refs[-1]


@dataclass(frozen=True)
class StrategyQuery:
    """Exact-anchor strategy lookup request with explicit measurement tolerances."""

    case_ref: ContractRef
    operating_mode: str
    applicability_values: Mapping[str, float]
    measurement_tolerances: Mapping[str, float]
    required_dependency_refs: tuple[ContractRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case_ref, ContractRef):
            raise TypeError("query case_ref must be ContractRef")
        object.__setattr__(
            self,
            "operating_mode",
            identifier(self.operating_mode, context="operating_mode"),
        )
        values = numeric_mapping(self.applicability_values, context="applicability_values")
        tolerances = numeric_mapping(
            self.measurement_tolerances,
            context="measurement_tolerances",
        )
        if not values or set(values) != set(tolerances):
            raise ValueError("query values and tolerances must share non-empty ids")
        if any(item < 0.0 for item in tolerances.values()):
            raise ValueError("query measurement tolerances must be non-negative")
        dependencies = tuple(self.required_dependency_refs)
        if dependencies != canonical_refs(dependencies):
            raise ValueError("required_dependency_refs must be unique and sorted")
        object.__setattr__(self, "applicability_values", values)
        object.__setattr__(self, "measurement_tolerances", tolerances)
        object.__setattr__(self, "required_dependency_refs", dependencies)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "applicability_values": dict(self.applicability_values),
            "measurement_tolerances": dict(self.measurement_tolerances),
            "required_dependency_refs": [item.as_dict() for item in self.required_dependency_refs],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyQuery:
        strict_keys(
            value,
            required={
                "case_ref",
                "operating_mode",
                "applicability_values",
                "measurement_tolerances",
                "required_dependency_refs",
            },
            context="strategy query",
        )
        return cls(
            case_ref=_ref(value["case_ref"], context="case_ref"),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            applicability_values=numeric_mapping(
                value["applicability_values"],
                context="applicability_values",
            ),
            measurement_tolerances=numeric_mapping(
                value["measurement_tolerances"],
                context="measurement_tolerances",
            ),
            required_dependency_refs=_refs(
                value["required_dependency_refs"],
                context="required_dependency_refs",
            ),
        )


__all__ = [
    "STRATEGY_SCHEMA_VERSION",
    "StrategyAnchor",
    "StrategyCoverage",
    "StrategyEntry",
    "StrategyEventType",
    "StrategyLifecycleEvent",
    "StrategyObjectiveSummary",
    "StrategyQuery",
    "StrategyRecord",
    "StrategyReleaseManifest",
    "StrategyState",
    "canonical_refs",
]
