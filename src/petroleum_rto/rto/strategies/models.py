"""Strict immutable R5 strategy, lifecycle, release and query contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, cast

from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    ContractRef,
)
from ..contracts.common import (
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


def _schema(value: str) -> None:
    if value != RTO_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the RTO V1 contract")


def _claim(value: str) -> None:
    if value != CLAIM_SCOPE:
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


def _refs(value: object, *, context: str) -> tuple[ContractRef, ...]:
    result = tuple(
        ContractRef.from_mapping(as_mapping(item, context=context))
        for item in as_sequence(value, context=context)
    )
    if tuple(sorted(result, key=lambda item: (item.object_id, item.fingerprint))) != result:
        raise ValueError(f"{context} must be sorted")
    if len(result) != len(set(result)):
        raise ValueError(f"{context} must be unique")
    return result


@dataclass(frozen=True)
class StrategyAnchorV1:
    """One explicitly evaluated operating anchor; never implies interpolation."""

    context_ref: ContractRef
    feed_mass_flow_kg_s: float
    action_setpoints: Mapping[str, float]
    static_evaluation_ref: ContractRef
    dynamic_evaluation_ref: ContractRef
    baseline_objective: float
    candidate_objective: float
    relative_improvement: float
    minimum_normalized_margin: float
    evidence_source_refs: tuple[ContractRef, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context_ref, ContractRef):
            raise TypeError("anchor context_ref must be a ContractRef")
        for name in ("static_evaluation_ref", "dynamic_evaluation_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"anchor {name} must be a ContractRef")
        sources = tuple(self.evidence_source_refs)
        if any(not isinstance(item, ContractRef) for item in sources):
            raise TypeError("anchor evidence_source_refs must contain ContractRef values")
        object.__setattr__(self, "evidence_source_refs", sources)
        object.__setattr__(
            self,
            "feed_mass_flow_kg_s",
            finite(self.feed_mass_flow_kg_s, context="feed_mass_flow_kg_s"),
        )
        if self.feed_mass_flow_kg_s <= 0.0:
            raise ValueError("feed_mass_flow_kg_s must be positive")
        action = numeric_mapping(self.action_setpoints, context="anchor action_setpoints")
        if set(action) != {
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        }:
            raise ValueError("anchor action_setpoints must contain exactly the two V1 variables")
        object.__setattr__(self, "action_setpoints", action)
        for name in (
            "baseline_objective",
            "candidate_objective",
            "relative_improvement",
            "minimum_normalized_margin",
        ):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "context_ref": self.context_ref.as_dict(),
            "feed_mass_flow_kg_s": self.feed_mass_flow_kg_s,
            "action_setpoints": dict(self.action_setpoints),
            "static_evaluation_ref": self.static_evaluation_ref.as_dict(),
            "dynamic_evaluation_ref": self.dynamic_evaluation_ref.as_dict(),
            "baseline_objective": self.baseline_objective,
            "candidate_objective": self.candidate_objective,
            "relative_improvement": self.relative_improvement,
            "minimum_normalized_margin": self.minimum_normalized_margin,
        }

    def as_dict(self) -> dict[str, object]:
        return self.fingerprint_payload()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyAnchorV1:
        strict_keys(
            value,
            required={
                "context_ref",
                "feed_mass_flow_kg_s",
                "action_setpoints",
                "static_evaluation_ref",
                "dynamic_evaluation_ref",
                "baseline_objective",
                "candidate_objective",
                "relative_improvement",
                "minimum_normalized_margin",
            },
            optional={"static_evaluation", "dynamic_evaluation"},
            context="strategy anchor",
        )
        result = cls(
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            feed_mass_flow_kg_s=finite(value["feed_mass_flow_kg_s"], context="feed_mass_flow_kg_s"),
            action_setpoints=numeric_mapping(value["action_setpoints"], context="action_setpoints"),
            static_evaluation_ref=ContractRef.from_mapping(
                as_mapping(value["static_evaluation_ref"], context="static_evaluation_ref")
            ),
            dynamic_evaluation_ref=ContractRef.from_mapping(
                as_mapping(value["dynamic_evaluation_ref"], context="dynamic_evaluation_ref")
            ),
            baseline_objective=finite(value["baseline_objective"], context="baseline_objective"),
            candidate_objective=finite(value["candidate_objective"], context="candidate_objective"),
            relative_improvement=finite(
                value["relative_improvement"], context="relative_improvement"
            ),
            minimum_normalized_margin=finite(
                value["minimum_normalized_margin"], context="minimum_normalized_margin"
            ),
        )
        # Read legacy draft files without carrying their duplicated evaluations
        # into the in-memory strategy or any newly serialized payload.
        for evaluation_field, ref_field in (
            ("static_evaluation", "static_evaluation_ref"),
            ("dynamic_evaluation", "dynamic_evaluation_ref"),
        ):
            embedded = value.get(evaluation_field)
            if embedded is not None:
                from ..contracts import CandidateEvaluationV1

                evaluation = CandidateEvaluationV1.from_mapping(
                    as_mapping(embedded, context=evaluation_field)
                )
                if evaluation.ref != getattr(result, ref_field):
                    raise ValueError(f"{ref_field} differs from legacy embedded evaluation")
        return result


@dataclass(frozen=True)
class StrategyEntryV1:
    """Immutable semantic strategy payload; lifecycle state is stored separately."""

    schema_version: str
    entry_version: str
    strategy_id: str
    revision: int
    supersedes: ContractRef | None
    coverage_kind: StrategyCoverage
    case_ref: ContractRef
    operating_mode: str
    anchors: tuple[StrategyAnchorV1, ...]
    action_setpoints: Mapping[str, float]
    baseline_setpoints: Mapping[str, float]
    application_profile_id: str
    event_time_s: float
    hold_policy: str
    stop_conditions: tuple[str, ...]
    problem_ref: ContractRef
    optimization_result_ref: ContractRef
    selected_proposal_ref: ContractRef
    objective_metric_id: str
    dependency_refs: tuple[ContractRef, ...]
    execution_scope: str
    control_authority: str
    field_validated: bool
    dcs_write_capability: bool
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in (
            "entry_version",
            "strategy_id",
            "operating_mode",
            "application_profile_id",
            "hold_policy",
            "objective_metric_id",
            "execution_scope",
            "control_authority",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "revision", integer(self.revision, context="revision", minimum=1))
        for name in ("case_ref", "problem_ref", "optimization_result_ref", "selected_proposal_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        if self.supersedes is not None and not isinstance(self.supersedes, ContractRef):
            raise TypeError("supersedes must be a ContractRef")
        if (self.revision == 1) != (self.supersedes is None):
            raise ValueError("only revisions after one may declare supersedes")
        if self.coverage_kind not in {"point", "sampled_anchors"}:
            raise ValueError("unsupported strategy coverage_kind")
        anchors = tuple(self.anchors)
        if not anchors:
            raise ValueError("strategy requires at least one evaluated anchor")
        if tuple(item.feed_mass_flow_kg_s for item in anchors) != tuple(
            sorted({item.feed_mass_flow_kg_s for item in anchors})
        ):
            raise ValueError("strategy anchors must have unique sorted feed loads")
        if self.coverage_kind == "point" and len(anchors) != 1:
            raise ValueError("point strategy requires exactly one anchor")
        if self.coverage_kind == "sampled_anchors" and len(anchors) < 2:
            raise ValueError("sampled_anchors strategy requires at least two anchors")
        object.__setattr__(self, "anchors", anchors)
        action = numeric_mapping(self.action_setpoints, context="action_setpoints")
        baseline = numeric_mapping(self.baseline_setpoints, context="baseline_setpoints")
        expected = {"furnace_temperature_target_k", "tower_top_pressure_target_pa_a"}
        if set(action) != expected or set(baseline) != expected:
            raise ValueError("strategy setpoints must contain exactly the two V1 variables")
        if any(dict(item.action_setpoints) != dict(action) for item in anchors):
            raise ValueError("all strategy anchors must evaluate the same action setpoints")
        object.__setattr__(self, "action_setpoints", action)
        object.__setattr__(self, "baseline_setpoints", baseline)
        object.__setattr__(self, "event_time_s", finite(self.event_time_s, context="event_time_s"))
        if self.event_time_s < 0.0:
            raise ValueError("event_time_s must be non-negative")
        stops = tuple(identifier(item, context="stop_condition") for item in self.stop_conditions)
        if not stops or len(stops) != len(set(stops)):
            raise ValueError("stop_conditions must be non-empty and unique")
        object.__setattr__(self, "stop_conditions", stops)
        dependencies = tuple(self.dependency_refs)
        if (
            tuple(sorted(dependencies, key=lambda item: (item.object_id, item.fingerprint)))
            != dependencies
        ):
            raise ValueError("dependency_refs must be sorted")
        if not dependencies or len(dependencies) != len(set(dependencies)):
            raise ValueError("dependency_refs must be non-empty and unique")
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
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "anchors": [item.fingerprint_payload() for item in self.anchors],
            "action_setpoints": dict(self.action_setpoints),
            "baseline_setpoints": dict(self.baseline_setpoints),
            "application_profile_id": self.application_profile_id,
            "event_time_s": self.event_time_s,
            "hold_policy": self.hold_policy,
            "stop_conditions": list(self.stop_conditions),
            "problem_ref": self.problem_ref.as_dict(),
            "optimization_result_ref": self.optimization_result_ref.as_dict(),
            "selected_proposal_ref": self.selected_proposal_ref.as_dict(),
            "objective_metric_id": self.objective_metric_id,
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
            "anchors": [item.as_dict() for item in self.anchors],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyEntryV1:
        required = {
            "schema_version",
            "entry_version",
            "strategy_id",
            "revision",
            "supersedes",
            "coverage_kind",
            "case_ref",
            "operating_mode",
            "anchors",
            "action_setpoints",
            "baseline_setpoints",
            "application_profile_id",
            "event_time_s",
            "hold_policy",
            "stop_conditions",
            "problem_ref",
            "optimization_result_ref",
            "selected_proposal_ref",
            "objective_metric_id",
            "dependency_refs",
            "execution_scope",
            "control_authority",
            "field_validated",
            "dcs_write_capability",
            "claim_scope",
        }
        strict_keys(value, required=required, optional={"strategy_ref"}, context="strategy entry")
        coverage = value["coverage_kind"]
        if coverage not in {"point", "sampled_anchors"}:
            raise ValueError("unsupported strategy coverage_kind")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            entry_version=identifier(value["entry_version"], context="entry_version"),
            strategy_id=identifier(value["strategy_id"], context="strategy_id"),
            revision=integer(value["revision"], context="revision", minimum=1),
            supersedes=(
                None
                if value["supersedes"] is None
                else ContractRef.from_mapping(as_mapping(value["supersedes"], context="supersedes"))
            ),
            coverage_kind=coverage,
            case_ref=ContractRef.from_mapping(as_mapping(value["case_ref"], context="case_ref")),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            anchors=tuple(
                StrategyAnchorV1.from_mapping(as_mapping(item, context="strategy anchor"))
                for item in as_sequence(value["anchors"], context="anchors")
            ),
            action_setpoints=numeric_mapping(value["action_setpoints"], context="action_setpoints"),
            baseline_setpoints=numeric_mapping(
                value["baseline_setpoints"], context="baseline_setpoints"
            ),
            application_profile_id=identifier(
                value["application_profile_id"], context="application_profile_id"
            ),
            event_time_s=finite(value["event_time_s"], context="event_time_s"),
            hold_policy=identifier(value["hold_policy"], context="hold_policy"),
            stop_conditions=tuple(
                identifier(item, context="stop_condition")
                for item in as_sequence(value["stop_conditions"], context="stop_conditions")
            ),
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            optimization_result_ref=ContractRef.from_mapping(
                as_mapping(value["optimization_result_ref"], context="optimization_result_ref")
            ),
            selected_proposal_ref=ContractRef.from_mapping(
                as_mapping(value["selected_proposal_ref"], context="selected_proposal_ref")
            ),
            objective_metric_id=identifier(
                value["objective_metric_id"], context="objective_metric_id"
            ),
            dependency_refs=_refs(value["dependency_refs"], context="dependency_refs"),
            execution_scope=identifier(value["execution_scope"], context="execution_scope"),
            control_authority=identifier(value["control_authority"], context="control_authority"),
            field_validated=boolean(value["field_validated"], context="field_validated"),
            dcs_write_capability=boolean(
                value["dcs_write_capability"], context="dcs_write_capability"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("strategy_ref") is not None:
            supplied = ContractRef.from_mapping(
                as_mapping(value["strategy_ref"], context="strategy_ref")
            )
            if supplied != result.ref:
                raise ValueError("strategy_ref differs from strategy content")
        return result


@dataclass(frozen=True)
class StrategyLifecycleEventV1:
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

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(
            self, "event_version", identifier(self.event_version, context="event_version")
        )
        if not isinstance(self.strategy_ref, ContractRef):
            raise TypeError("strategy_ref must be a ContractRef")
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
            if self.from_state not in {"draft", "approved", "published", "pending_revalidation"}:
                raise ValueError("retired event has an invalid source state")
        elif self.from_state != expected_from:
            raise ValueError("strategy event source state is invalid")
        if self.to_state != expected_to:
            raise ValueError("strategy event target state is invalid")
        object.__setattr__(self, "actor", identifier(self.actor, context="actor"))
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, context="occurred_at"))
        object.__setattr__(self, "reason", text(self.reason, context="reason"))
        if self.release_ref is not None and not isinstance(self.release_ref, ContractRef):
            raise TypeError("release_ref must be a ContractRef")
        if (self.event_type == "published") != (self.release_ref is not None):
            raise ValueError("only published events require a release_ref")
        if self.related_strategy_ref is not None and not isinstance(
            self.related_strategy_ref, ContractRef
        ):
            raise TypeError("related_strategy_ref must be a ContractRef")
        if (self.event_type == "superseded") != (self.related_strategy_ref is not None):
            raise ValueError("only superseded events require a related_strategy_ref")
        if self.previous_event_fingerprint is not None:
            object.__setattr__(
                self,
                "previous_event_fingerprint",
                digest(self.previous_event_fingerprint, context="previous_event_fingerprint"),
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
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "event_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyLifecycleEventV1:
        required = {
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
        }
        strict_keys(
            value, required=required, optional={"event_fingerprint"}, context="strategy event"
        )
        event_type = value["event_type"]
        if event_type not in {
            "created",
            "approved",
            "published",
            "revalidation_requested",
            "superseded",
            "retired",
        }:
            raise ValueError("unsupported strategy event_type")
        valid_states = {
            None,
            "draft",
            "approved",
            "published",
            "pending_revalidation",
            "superseded",
            "retired",
        }
        if value["from_state"] not in valid_states or value["to_state"] not in valid_states - {
            None
        }:
            raise ValueError("unsupported strategy state")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            event_version=identifier(value["event_version"], context="event_version"),
            strategy_ref=ContractRef.from_mapping(
                as_mapping(value["strategy_ref"], context="strategy_ref")
            ),
            sequence=integer(value["sequence"], context="sequence"),
            event_type=event_type,
            from_state=cast(StrategyState | None, value["from_state"]),
            to_state=cast(StrategyState, value["to_state"]),
            actor=identifier(value["actor"], context="actor"),
            occurred_at=_timestamp(value["occurred_at"], context="occurred_at"),
            reason=text(value["reason"], context="reason"),
            release_ref=(
                None
                if value["release_ref"] is None
                else ContractRef.from_mapping(
                    as_mapping(value["release_ref"], context="release_ref")
                )
            ),
            related_strategy_ref=(
                None
                if value["related_strategy_ref"] is None
                else ContractRef.from_mapping(
                    as_mapping(value["related_strategy_ref"], context="related_strategy_ref")
                )
            ),
            previous_event_fingerprint=(
                None
                if value["previous_event_fingerprint"] is None
                else digest(
                    value["previous_event_fingerprint"], context="previous_event_fingerprint"
                )
            ),
        )
        supplied = value.get("event_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="event_fingerprint") != result.fingerprint
        ):
            raise ValueError("event_fingerprint differs from event content")
        return result


@dataclass(frozen=True)
class StrategyReleaseManifestV1:
    schema_version: str
    release_version: str
    release_id: str
    entry_refs: tuple[ContractRef, ...]
    created_by: str
    created_at: str
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("release_version", "release_id", "created_by"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        refs = tuple(self.entry_refs)
        if (
            not refs
            or tuple(sorted(refs, key=lambda item: (item.object_id, item.fingerprint))) != refs
        ):
            raise ValueError("release entry_refs must be non-empty and sorted")
        if len(refs) != len(set(refs)):
            raise ValueError("release entry_refs must be unique")
        object.__setattr__(self, "entry_refs", refs)
        object.__setattr__(self, "created_at", _timestamp(self.created_at, context="created_at"))

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "release_id": self.release_id,
            "entry_refs": [item.as_dict() for item in self.entry_refs],
            "created_by": self.created_by,
            "created_at": self.created_at,
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
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleaseManifestV1:
        required = {
            "schema_version",
            "release_version",
            "release_id",
            "entry_refs",
            "created_by",
            "created_at",
            "claim_scope",
        }
        strict_keys(value, required=required, optional={"release_fingerprint"}, context="release")
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            release_version=identifier(value["release_version"], context="release_version"),
            release_id=identifier(value["release_id"], context="release_id"),
            entry_refs=_refs(value["entry_refs"], context="entry_refs"),
            created_by=identifier(value["created_by"], context="created_by"),
            created_at=_timestamp(value["created_at"], context="created_at"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("release_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="release_fingerprint") != result.fingerprint
        ):
            raise ValueError("release_fingerprint differs from release content")
        return result


@dataclass(frozen=True)
class StrategyRecordV1:
    entry: StrategyEntryV1
    events: tuple[StrategyLifecycleEventV1, ...]

    def __post_init__(self) -> None:
        events = tuple(self.events)
        if not events:
            raise ValueError("strategy record requires lifecycle events")
        state: StrategyState | None = None
        previous: str | None = None
        for sequence, event in enumerate(events):
            if event.strategy_ref != self.entry.ref or event.sequence != sequence:
                raise ValueError("strategy event identity or sequence differs")
            if event.from_state != state or event.previous_event_fingerprint != previous:
                raise ValueError("strategy event chain is discontinuous")
            state = event.to_state
            previous = event.fingerprint
        object.__setattr__(self, "events", events)

    @property
    def current_state(self) -> StrategyState:
        return self.events[-1].to_state

    @property
    def release_ref(self) -> ContractRef | None:
        published = [item.release_ref for item in self.events if item.event_type == "published"]
        return None if not published else published[-1]


@dataclass(frozen=True)
class StrategyQueryV1:
    case_ref: ContractRef
    operating_mode: str
    feed_mass_flow_kg_s: float
    measurement_tolerance_kg_s: float
    required_dependency_refs: tuple[ContractRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case_ref, ContractRef):
            raise TypeError("query case_ref must be a ContractRef")
        object.__setattr__(
            self, "operating_mode", identifier(self.operating_mode, context="operating_mode")
        )
        for name in ("feed_mass_flow_kg_s", "measurement_tolerance_kg_s"):
            object.__setattr__(self, name, finite(getattr(self, name), context=name))
        if self.feed_mass_flow_kg_s <= 0.0 or self.measurement_tolerance_kg_s < 0.0:
            raise ValueError("query feed must be positive and tolerance non-negative")
        refs = tuple(self.required_dependency_refs)
        if tuple(sorted(refs, key=lambda item: (item.object_id, item.fingerprint))) != refs:
            raise ValueError("required_dependency_refs must be sorted")
        if len(refs) != len(set(refs)):
            raise ValueError("required_dependency_refs must be unique")
        object.__setattr__(self, "required_dependency_refs", refs)


def strategy_dependencies(
    refs: Sequence[ContractRef],
) -> tuple[ContractRef, ...]:
    """Return a canonical unique dependency list for builders and callers."""

    return tuple(sorted(set(refs), key=lambda item: (item.object_id, item.fingerprint)))


def immutable_numeric_mapping(value: Mapping[str, float]) -> Mapping[str, float]:
    """Expose a typed immutable numeric mapping for strategy callers."""

    return MappingProxyType(dict(numeric_mapping(value, context="strategy numeric mapping")))
