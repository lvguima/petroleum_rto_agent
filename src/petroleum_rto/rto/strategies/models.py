"""Minimal immutable strategy and lifecycle contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

from ..contracts.common import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    finite,
    identifier,
    integer,
    numeric_mapping,
    strict_keys,
    text,
)
from ..contracts.reference import ContractRef

STRATEGY_SCHEMA_VERSION: Final[str] = "3.0.0"

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


def _require_schema(schema_version: str) -> None:
    if schema_version != STRATEGY_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the strategy contract")


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
        raise TypeError("strategy references must contain ContractRef values")
    return tuple(sorted(set(refs), key=lambda item: (item.object_id, item.fingerprint)))


def _validate_fingerprint(value: object, *, expected: str, context: str) -> None:
    if digest(value, context=context) != expected:
        raise ValueError(f"{context} differs from content")


@dataclass(frozen=True)
class StrategyAdjustment:
    """One human-readable current-to-recommended setpoint change."""

    variable_id: str
    current: float
    recommended: float
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "variable_id", identifier(self.variable_id, context="variable_id"))
        object.__setattr__(self, "current", finite(self.current, context="current"))
        object.__setattr__(self, "recommended", finite(self.recommended, context="recommended"))
        object.__setattr__(self, "unit", text(self.unit, context="unit"))

    def as_dict(self) -> dict[str, object]:
        return {
            "variable_id": self.variable_id,
            "current": self.current,
            "recommended": self.recommended,
            "unit": self.unit,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyAdjustment:
        strict_keys(
            value,
            required={"variable_id", "current", "recommended", "unit"},
            context="strategy adjustment",
        )
        return cls(
            variable_id=identifier(value["variable_id"], context="variable_id"),
            current=finite(value["current"], context="current"),
            recommended=finite(value["recommended"], context="recommended"),
            unit=text(value["unit"], context="unit"),
        )


@dataclass(frozen=True)
class StrategyObjectiveSummary:
    """One compact expected effect at a verified applicability anchor."""

    metric_id: str
    baseline: float
    expected: float
    unit: str
    relative_improvement: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", identifier(self.metric_id, context="metric_id"))
        object.__setattr__(self, "baseline", finite(self.baseline, context="baseline"))
        object.__setattr__(self, "expected", finite(self.expected, context="expected"))
        object.__setattr__(self, "unit", text(self.unit, context="unit"))
        if self.relative_improvement is not None:
            object.__setattr__(
                self,
                "relative_improvement",
                finite(self.relative_improvement, context="relative_improvement"),
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "baseline": self.baseline,
            "expected": self.expected,
            "unit": self.unit,
            "relative_improvement": self.relative_improvement,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyObjectiveSummary:
        strict_keys(
            value,
            required={
                "metric_id",
                "baseline",
                "expected",
                "unit",
                "relative_improvement",
            },
            context="strategy effect",
        )
        relative = value["relative_improvement"]
        return cls(
            metric_id=identifier(value["metric_id"], context="metric_id"),
            baseline=finite(value["baseline"], context="baseline"),
            expected=finite(value["expected"], context="expected"),
            unit=text(value["unit"], context="unit"),
            relative_improvement=(
                None if relative is None else finite(relative, context="relative_improvement")
            ),
        )


@dataclass(frozen=True)
class StrategyAnchor:
    """One explicitly evaluated applicability point; interpolation is not implied."""

    context_ref: ContractRef
    conditions: Mapping[str, float]
    effects: tuple[StrategyObjectiveSummary, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.context_ref, ContractRef):
            raise TypeError("context_ref must be ContractRef")
        conditions = numeric_mapping(self.conditions, context="conditions")
        if not conditions:
            raise ValueError("strategy anchor conditions must be non-empty")
        object.__setattr__(self, "conditions", conditions)
        effects = tuple(self.effects)
        metric_ids = tuple(item.metric_id for item in effects)
        if (
            not effects
            or any(not isinstance(item, StrategyObjectiveSummary) for item in effects)
            or len(metric_ids) != len(set(metric_ids))
        ):
            raise ValueError("strategy anchor effects must be non-empty and unique")
        object.__setattr__(self, "effects", effects)

    def as_dict(self) -> dict[str, object]:
        return {
            "context_ref": self.context_ref.as_dict(),
            "conditions": dict(self.conditions),
            "effects": [item.as_dict() for item in self.effects],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyAnchor:
        strict_keys(
            value,
            required={"context_ref", "conditions", "effects"},
            context="strategy anchor",
        )
        return cls(
            context_ref=_ref(value["context_ref"], context="context_ref"),
            conditions=numeric_mapping(value["conditions"], context="conditions"),
            effects=tuple(
                StrategyObjectiveSummary.from_mapping(as_mapping(item, context="strategy effect"))
                for item in as_sequence(value["effects"], context="effects")
            ),
        )


@dataclass(frozen=True)
class StrategyApplicability:
    case_ref: ContractRef
    operating_mode: str
    coverage: StrategyCoverage
    anchors: tuple[StrategyAnchor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.case_ref, ContractRef):
            raise TypeError("case_ref must be ContractRef")
        object.__setattr__(
            self,
            "operating_mode",
            identifier(self.operating_mode, context="operating_mode"),
        )
        if self.coverage not in {"point", "sampled_anchors"}:
            raise ValueError("unsupported strategy coverage")
        anchors = tuple(self.anchors)
        if any(not isinstance(item, StrategyAnchor) for item in anchors):
            raise TypeError("anchors must contain StrategyAnchor values")
        ordered = tuple(
            sorted(
                anchors, key=lambda item: (item.context_ref.object_id, item.context_ref.fingerprint)
            )
        )
        if (
            not anchors
            or anchors != ordered
            or len({item.context_ref for item in anchors}) != len(anchors)
        ):
            raise ValueError("anchors must be non-empty, unique and context-ref sorted")
        if self.coverage == "point" and len(anchors) != 1:
            raise ValueError("point coverage requires exactly one anchor")
        if self.coverage == "sampled_anchors" and len(anchors) < 2:
            raise ValueError("sampled_anchors coverage requires at least two anchors")
        condition_ids = set(anchors[0].conditions)
        effect_ids = tuple(item.metric_id for item in anchors[0].effects)
        for anchor in anchors:
            if (
                set(anchor.conditions) != condition_ids
                or tuple(item.metric_id for item in anchor.effects) != effect_ids
            ):
                raise ValueError("strategy anchors must share condition and effect ids")
        condition_vectors = tuple(tuple(sorted(item.conditions.items())) for item in anchors)
        if len(condition_vectors) != len(set(condition_vectors)):
            raise ValueError("strategy anchors must have unique condition vectors")
        object.__setattr__(self, "anchors", anchors)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "coverage": self.coverage,
            "anchors": [item.as_dict() for item in self.anchors],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyApplicability:
        strict_keys(
            value,
            required={"case_ref", "operating_mode", "coverage", "anchors"},
            context="strategy applicability",
        )
        coverage = value["coverage"]
        if coverage not in {"point", "sampled_anchors"}:
            raise ValueError("unsupported strategy coverage")
        return cls(
            case_ref=_ref(value["case_ref"], context="case_ref"),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            coverage=coverage,
            anchors=tuple(
                StrategyAnchor.from_mapping(as_mapping(item, context="strategy anchor"))
                for item in as_sequence(value["anchors"], context="anchors")
            ),
        )


@dataclass(frozen=True)
class StrategyEvidence:
    """Minimum links needed to reconstruct the strategy from its source workflow."""

    problem_ref: ContractRef
    finalization_result_ref: ContractRef
    coverage_ref: ContractRef

    def __post_init__(self) -> None:
        for name in ("problem_ref", "finalization_result_ref", "coverage_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")

    def as_dict(self) -> dict[str, object]:
        return {
            "problem_ref": self.problem_ref.as_dict(),
            "finalization_result_ref": self.finalization_result_ref.as_dict(),
            "coverage_ref": self.coverage_ref.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyEvidence:
        strict_keys(
            value,
            required={"problem_ref", "finalization_result_ref", "coverage_ref"},
            context="strategy evidence",
        )
        return cls(
            problem_ref=_ref(value["problem_ref"], context="problem_ref"),
            finalization_result_ref=_ref(
                value["finalization_result_ref"], context="finalization_result_ref"
            ),
            coverage_ref=_ref(value["coverage_ref"], context="coverage_ref"),
        )


@dataclass(frozen=True)
class StrategyEntry:
    """Readable immutable strategy payload; lifecycle state remains in events."""

    schema_version: str
    strategy_id: str
    revision: int
    supersedes: ContractRef | None
    adjustments: tuple[StrategyAdjustment, ...]
    applicability: StrategyApplicability
    evidence: StrategyEvidence

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "strategy_id", identifier(self.strategy_id, context="strategy_id"))
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
        adjustments = tuple(self.adjustments)
        variable_ids = tuple(item.variable_id for item in adjustments)
        if (
            not adjustments
            or any(not isinstance(item, StrategyAdjustment) for item in adjustments)
            or len(variable_ids) != len(set(variable_ids))
        ):
            raise ValueError("strategy adjustments must be non-empty and unique")
        object.__setattr__(self, "adjustments", adjustments)
        if not isinstance(self.applicability, StrategyApplicability):
            raise TypeError("applicability must be StrategyApplicability")
        if not isinstance(self.evidence, StrategyEvidence):
            raise TypeError("evidence must be StrategyEvidence")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "revision": self.revision,
            "supersedes": None if self.supersedes is None else self.supersedes.as_dict(),
            "adjustments": [item.as_dict() for item in self.adjustments],
            "applicability": self.applicability.as_dict(),
            "evidence": self.evidence.as_dict(),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"{self.strategy_id}-r{self.revision}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyEntry:
        strict_keys(
            value,
            required={
                "schema_version",
                "strategy_id",
                "revision",
                "supersedes",
                "adjustments",
                "applicability",
                "evidence",
                "fingerprint",
            },
            context="strategy entry",
        )
        entry = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            strategy_id=identifier(value["strategy_id"], context="strategy_id"),
            revision=integer(value["revision"], context="revision", minimum=1),
            supersedes=_optional_ref(value["supersedes"], context="supersedes"),
            adjustments=tuple(
                StrategyAdjustment.from_mapping(as_mapping(item, context="strategy adjustment"))
                for item in as_sequence(value["adjustments"], context="adjustments")
            ),
            applicability=StrategyApplicability.from_mapping(
                as_mapping(value["applicability"], context="applicability")
            ),
            evidence=StrategyEvidence.from_mapping(
                as_mapping(value["evidence"], context="evidence")
            ),
        )
        _validate_fingerprint(
            value["fingerprint"], expected=entry.fingerprint, context="fingerprint"
        )
        return entry


@dataclass(frozen=True)
class StrategyLifecycleEvent:
    schema_version: str
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
        _require_schema(self.schema_version)
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
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, context="occurred_at"))
        object.__setattr__(self, "reason", text(self.reason, context="reason"))
        if self.release_ref is not None and not isinstance(self.release_ref, ContractRef):
            raise TypeError("release_ref must be ContractRef")
        if (self.event_type == "published") != (self.release_ref is not None):
            raise ValueError("only published events require a release_ref")
        if self.related_strategy_ref is not None and not isinstance(
            self.related_strategy_ref, ContractRef
        ):
            raise TypeError("related_strategy_ref must be ContractRef")
        if (self.event_type == "superseded") != (self.related_strategy_ref is not None):
            raise ValueError("only superseded events require a related strategy")
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
        return {**self.fingerprint_payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyLifecycleEvent:
        strict_keys(
            value,
            required={
                "schema_version",
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
                "fingerprint",
            },
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
                value["related_strategy_ref"], context="related_strategy_ref"
            ),
            previous_event_fingerprint=(
                None
                if value["previous_event_fingerprint"] is None
                else digest(
                    value["previous_event_fingerprint"], context="previous_event_fingerprint"
                )
            ),
        )
        _validate_fingerprint(
            value["fingerprint"], expected=event.fingerprint, context="fingerprint"
        )
        return event


@dataclass(frozen=True)
class StrategyReleaseManifest:
    schema_version: str
    release_id: str
    entry_refs: tuple[ContractRef, ...]
    created_by: str
    created_at: str

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        object.__setattr__(self, "release_id", identifier(self.release_id, context="release_id"))
        object.__setattr__(self, "created_by", identifier(self.created_by, context="created_by"))
        refs = tuple(self.entry_refs)
        if not refs or refs != canonical_refs(refs):
            raise ValueError("release entry_refs must be non-empty, unique and sorted")
        object.__setattr__(self, "entry_refs", refs)
        object.__setattr__(self, "created_at", _timestamp(self.created_at, context="created_at"))

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "entry_refs": [item.as_dict() for item in self.entry_refs],
            "created_by": self.created_by,
            "created_at": self.created_at,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.release_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleaseManifest:
        strict_keys(
            value,
            required={
                "schema_version",
                "release_id",
                "entry_refs",
                "created_by",
                "created_at",
                "fingerprint",
            },
            context="strategy release",
        )
        release = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            release_id=identifier(value["release_id"], context="release_id"),
            entry_refs=_refs(value["entry_refs"], context="entry_refs"),
            created_by=identifier(value["created_by"], context="created_by"),
            created_at=_timestamp(value["created_at"], context="created_at"),
        )
        _validate_fingerprint(
            value["fingerprint"], expected=release.fingerprint, context="fingerprint"
        )
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
    """Exact-anchor lookup with caller-provided measurement tolerances."""

    case_ref: ContractRef
    operating_mode: str
    conditions: Mapping[str, float]
    measurement_tolerances: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.case_ref, ContractRef):
            raise TypeError("query case_ref must be ContractRef")
        object.__setattr__(
            self,
            "operating_mode",
            identifier(self.operating_mode, context="operating_mode"),
        )
        conditions = numeric_mapping(self.conditions, context="conditions")
        tolerances = numeric_mapping(self.measurement_tolerances, context="measurement_tolerances")
        if not conditions or set(conditions) != set(tolerances):
            raise ValueError("query conditions and tolerances must share non-empty ids")
        if any(item < 0.0 for item in tolerances.values()):
            raise ValueError("query measurement tolerances must be non-negative")
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(self, "measurement_tolerances", tolerances)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "conditions": dict(self.conditions),
            "measurement_tolerances": dict(self.measurement_tolerances),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyQuery:
        strict_keys(
            value,
            required={"case_ref", "operating_mode", "conditions", "measurement_tolerances"},
            context="strategy query",
        )
        return cls(
            case_ref=_ref(value["case_ref"], context="case_ref"),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            conditions=numeric_mapping(value["conditions"], context="conditions"),
            measurement_tolerances=numeric_mapping(
                value["measurement_tolerances"], context="measurement_tolerances"
            ),
        )


__all__ = [
    "STRATEGY_SCHEMA_VERSION",
    "StrategyAdjustment",
    "StrategyAnchor",
    "StrategyApplicability",
    "StrategyCoverage",
    "StrategyEntry",
    "StrategyEventType",
    "StrategyEvidence",
    "StrategyLifecycleEvent",
    "StrategyObjectiveSummary",
    "StrategyQuery",
    "StrategyRecord",
    "StrategyReleaseManifest",
    "StrategyState",
    "canonical_refs",
]
