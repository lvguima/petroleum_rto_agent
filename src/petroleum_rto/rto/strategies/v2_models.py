"""Compact offline-only RTO V2 strategy draft contracts and repository."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..contracts.common import (
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    canonical_json_bytes,
    digest,
    finite,
    identifier,
    integer,
    numeric_mapping,
    strict_keys,
    text,
)
from ..contracts.models import CLAIM_SCOPE, ContractRef
from ..contracts.multiobjective import RTO_V2_SCHEMA_VERSION
from ..contracts.results_v2 import ObjectiveOutcomeV2


def _timestamp(value: object, *, context: str) -> str:
    raw = text(value, context=context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include an explicit timezone")
    return raw


@dataclass(frozen=True)
class StrategyAnchorV2:
    feed_ratio: float
    context_ref: ContractRef
    feed_mass_flow_kg_s: float
    static_evaluation_ref: ContractRef
    dynamic_evaluation_ref: ContractRef
    objective_summaries: tuple[ObjectiveOutcomeV2, ...]
    minimum_normalized_margin: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "feed_ratio", finite(self.feed_ratio, context="feed_ratio"))
        object.__setattr__(
            self,
            "feed_mass_flow_kg_s",
            finite(self.feed_mass_flow_kg_s, context="feed_mass_flow_kg_s"),
        )
        if self.feed_ratio <= 0.0 or self.feed_mass_flow_kg_s <= 0.0:
            raise ValueError("strategy anchor feed values must be positive")
        for name in ("context_ref", "static_evaluation_ref", "dynamic_evaluation_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        objectives = tuple(self.objective_summaries)
        ids = tuple(item.metric_id for item in objectives)
        if not objectives or len(ids) != len(set(ids)):
            raise ValueError("strategy anchor objectives must be non-empty and unique")
        object.__setattr__(self, "objective_summaries", objectives)
        object.__setattr__(
            self,
            "minimum_normalized_margin",
            finite(self.minimum_normalized_margin, context="minimum_normalized_margin"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "feed_ratio": self.feed_ratio,
            "context_ref": self.context_ref.as_dict(),
            "feed_mass_flow_kg_s": self.feed_mass_flow_kg_s,
            "static_evaluation_ref": self.static_evaluation_ref.as_dict(),
            "dynamic_evaluation_ref": self.dynamic_evaluation_ref.as_dict(),
            "objective_summaries": [item.as_dict() for item in self.objective_summaries],
            "minimum_normalized_margin": self.minimum_normalized_margin,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyAnchorV2:
        strict_keys(
            value,
            required={
                "feed_ratio",
                "context_ref",
                "feed_mass_flow_kg_s",
                "static_evaluation_ref",
                "dynamic_evaluation_ref",
                "objective_summaries",
                "minimum_normalized_margin",
            },
            context="strategy anchor V2",
        )
        return cls(
            feed_ratio=finite(value["feed_ratio"], context="feed_ratio"),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            feed_mass_flow_kg_s=finite(value["feed_mass_flow_kg_s"], context="feed_mass_flow_kg_s"),
            static_evaluation_ref=ContractRef.from_mapping(
                as_mapping(value["static_evaluation_ref"], context="static_evaluation_ref")
            ),
            dynamic_evaluation_ref=ContractRef.from_mapping(
                as_mapping(value["dynamic_evaluation_ref"], context="dynamic_evaluation_ref")
            ),
            objective_summaries=tuple(
                ObjectiveOutcomeV2.from_mapping(as_mapping(item, context="objective summary"))
                for item in as_sequence(value["objective_summaries"], context="objective_summaries")
            ),
            minimum_normalized_margin=finite(
                value["minimum_normalized_margin"], context="minimum_normalized_margin"
            ),
        )


@dataclass(frozen=True)
class StrategyEntryV2:
    schema_version: str
    entry_version: str
    revision: int
    state: str
    context_ref: ContractRef
    problem_ref: ContractRef
    objective_catalog_ref: ContractRef
    preference_profile_ref: ContractRef
    pareto_search_ref: ContractRef
    selection_ref: ContractRef
    optimization_result_ref: ContractRef
    selection_rationale_code: str
    action_setpoints: Mapping[str, float]
    anchors: tuple[StrategyAnchorV2, ...]
    execution_scope: str
    control_authority: str
    field_validated: bool
    dcs_write_capability: bool
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RTO_V2_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the RTO V2 contract")
        if self.claim_scope != CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        object.__setattr__(
            self, "entry_version", identifier(self.entry_version, context="entry_version")
        )
        object.__setattr__(self, "revision", integer(self.revision, context="revision", minimum=1))
        object.__setattr__(self, "state", identifier(self.state, context="state"))
        if self.revision != 1 or self.state != "draft":
            raise ValueError("RTO V2 initial delivery supports only revision-1 drafts")
        for name in (
            "context_ref",
            "problem_ref",
            "objective_catalog_ref",
            "preference_profile_ref",
            "pareto_search_ref",
            "selection_ref",
            "optimization_result_ref",
        ):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be a ContractRef")
        object.__setattr__(
            self,
            "selection_rationale_code",
            identifier(self.selection_rationale_code, context="selection_rationale_code"),
        )
        action = numeric_mapping(self.action_setpoints, context="action_setpoints")
        if set(action) != {
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        }:
            raise ValueError("strategy action must contain exactly two high-level setpoints")
        object.__setattr__(self, "action_setpoints", action)
        anchors = tuple(self.anchors)
        if not anchors or tuple(item.feed_ratio for item in anchors) != tuple(
            sorted({item.feed_ratio for item in anchors})
        ):
            raise ValueError("strategy anchors must be non-empty, unique and sorted")
        object.__setattr__(self, "anchors", anchors)
        for name in ("execution_scope", "control_authority"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.execution_scope != "offline_simulation_only" or self.control_authority != "none":
            raise ValueError("V2 strategy must remain offline and have no control authority")
        if not isinstance(self.field_validated, bool) or not isinstance(
            self.dcs_write_capability, bool
        ):
            raise TypeError("strategy field and DCS flags must be boolean")
        if self.field_validated or self.dcs_write_capability:
            raise ValueError("V2 strategy cannot claim field validation or DCS writes")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "entry_version": self.entry_version,
            "revision": self.revision,
            "state": self.state,
            "context_ref": self.context_ref.as_dict(),
            "problem_ref": self.problem_ref.as_dict(),
            "objective_catalog_ref": self.objective_catalog_ref.as_dict(),
            "preference_profile_ref": self.preference_profile_ref.as_dict(),
            "pareto_search_ref": self.pareto_search_ref.as_dict(),
            "selection_ref": self.selection_ref.as_dict(),
            "optimization_result_ref": self.optimization_result_ref.as_dict(),
            "selection_rationale_code": self.selection_rationale_code,
            "action_setpoints": dict(self.action_setpoints),
            "anchors": [item.as_dict() for item in self.anchors],
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
    def strategy_id(self) -> str:
        return f"strategy-v2-{self.fingerprint[:16]}"

    @property
    def ref(self) -> ContractRef:
        return ContractRef(f"{self.strategy_id}-r{self.revision}", self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "strategy_id": self.strategy_id,
            "strategy_ref": self.ref.as_dict(),
            "strategy_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyEntryV2:
        required = {
            "schema_version",
            "entry_version",
            "revision",
            "state",
            "context_ref",
            "problem_ref",
            "objective_catalog_ref",
            "preference_profile_ref",
            "pareto_search_ref",
            "selection_ref",
            "optimization_result_ref",
            "selection_rationale_code",
            "action_setpoints",
            "anchors",
            "execution_scope",
            "control_authority",
            "field_validated",
            "dcs_write_capability",
            "claim_scope",
        }
        strict_keys(
            value,
            required=required,
            optional={"strategy_id", "strategy_ref", "strategy_fingerprint"},
            context="strategy entry V2",
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            entry_version=identifier(value["entry_version"], context="entry_version"),
            revision=integer(value["revision"], context="revision", minimum=1),
            state=identifier(value["state"], context="state"),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            objective_catalog_ref=ContractRef.from_mapping(
                as_mapping(value["objective_catalog_ref"], context="objective_catalog_ref")
            ),
            preference_profile_ref=ContractRef.from_mapping(
                as_mapping(value["preference_profile_ref"], context="preference_profile_ref")
            ),
            pareto_search_ref=ContractRef.from_mapping(
                as_mapping(value["pareto_search_ref"], context="pareto_search_ref")
            ),
            selection_ref=ContractRef.from_mapping(
                as_mapping(value["selection_ref"], context="selection_ref")
            ),
            optimization_result_ref=ContractRef.from_mapping(
                as_mapping(value["optimization_result_ref"], context="optimization_result_ref")
            ),
            selection_rationale_code=identifier(
                value["selection_rationale_code"], context="selection_rationale_code"
            ),
            action_setpoints=numeric_mapping(value["action_setpoints"], context="action_setpoints"),
            anchors=tuple(
                StrategyAnchorV2.from_mapping(as_mapping(item, context="strategy anchor"))
                for item in as_sequence(value["anchors"], context="anchors")
            ),
            execution_scope=identifier(value["execution_scope"], context="execution_scope"),
            control_authority=identifier(value["control_authority"], context="control_authority"),
            field_validated=boolean(value["field_validated"], context="field_validated"),
            dcs_write_capability=boolean(
                value["dcs_write_capability"], context="dcs_write_capability"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("strategy_id") not in {None, result.strategy_id}:
            raise ValueError("strategy_id differs from entry content")
        if (
            value.get("strategy_ref") is not None
            and ContractRef.from_mapping(as_mapping(value["strategy_ref"], context="strategy_ref"))
            != result.ref
        ):
            raise ValueError("strategy_ref differs from entry content")
        supplied = value.get("strategy_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="strategy_fingerprint") != result.fingerprint
        ):
            raise ValueError("strategy_fingerprint differs from entry content")
        return result


@dataclass(frozen=True)
class StrategyDraftEventV2:
    schema_version: str
    event_version: str
    strategy_ref: ContractRef
    event_type: str
    actor: str
    occurred_at: str
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RTO_V2_SCHEMA_VERSION or self.claim_scope != CLAIM_SCOPE:
            raise ValueError("strategy event scope or schema differs")
        for name in ("event_version", "event_type", "actor"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.event_type != "draft-created":
            raise ValueError("V2 delivery supports only the draft-created event")
        if not isinstance(self.strategy_ref, ContractRef):
            raise TypeError("strategy_ref must be a ContractRef")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, context="occurred_at"))

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_version": self.event_version,
            "strategy_ref": self.strategy_ref.as_dict(),
            "event_type": self.event_type,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "event_fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyDraftEventV2:
        strict_keys(
            value,
            required={
                "schema_version",
                "event_version",
                "strategy_ref",
                "event_type",
                "actor",
                "occurred_at",
                "claim_scope",
            },
            optional={"event_fingerprint"},
            context="strategy draft event V2",
        )
        result = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            event_version=identifier(value["event_version"], context="event_version"),
            strategy_ref=ContractRef.from_mapping(
                as_mapping(value["strategy_ref"], context="strategy_ref")
            ),
            event_type=identifier(value["event_type"], context="event_type"),
            actor=identifier(value["actor"], context="actor"),
            occurred_at=_timestamp(value["occurred_at"], context="occurred_at"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        supplied = value.get("event_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="event_fingerprint") != result.fingerprint
        ):
            raise ValueError("event_fingerprint differs from event content")
        return result


@dataclass(frozen=True)
class StrategyDraftRecordV2:
    entry: StrategyEntryV2
    event: StrategyDraftEventV2


class StrategyDraftRepositoryV2:
    """Persist an immutable V2 draft; intentionally exposes no approval or publish API."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("strategy repository root must be a pathlib.Path")
        self._root = root.resolve()

    def create_draft(
        self,
        entry: StrategyEntryV2,
        *,
        actor: str,
        occurred_at: str,
    ) -> StrategyDraftRecordV2:
        if not isinstance(entry, StrategyEntryV2):
            raise TypeError("entry must be a StrategyEntryV2")
        actor = identifier(actor, context="actor")
        revision_dir = self._revision_dir(entry.strategy_id, entry.revision)
        entry_path = revision_dir / "entry.json"
        event_path = revision_dir / "events.jsonl"
        if entry_path.exists() or event_path.exists():
            record = self.read_ref(entry.ref)
            if record.entry != entry:
                raise ValueError("existing V2 strategy draft differs")
            return record
        revision_dir.mkdir(parents=True, exist_ok=True)
        event = StrategyDraftEventV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            event_version="strategy-draft-event-v2",
            strategy_ref=entry.ref,
            event_type="draft-created",
            actor=actor,
            occurred_at=occurred_at,
            claim_scope=CLAIM_SCOPE,
        )
        _atomic_write(entry_path, canonical_json_bytes(entry.as_dict()))
        _atomic_write(event_path, canonical_json_bytes(event.as_dict()) + b"\n")
        return StrategyDraftRecordV2(entry=entry, event=event)

    def read_ref(self, ref: ContractRef) -> StrategyDraftRecordV2:
        if not isinstance(ref, ContractRef):
            raise TypeError("strategy ref must be a ContractRef")
        if "-r" not in ref.object_id:
            raise ValueError("strategy ref object_id lacks a revision suffix")
        strategy_id, revision_text = ref.object_id.rsplit("-r", 1)
        try:
            revision_value = int(revision_text)
        except ValueError as exc:
            raise ValueError("strategy revision suffix is invalid") from exc
        revision = integer(revision_value, context="revision", minimum=1)
        revision_dir = self._revision_dir(strategy_id, revision)
        try:
            entry_raw = json.loads((revision_dir / "entry.json").read_text(encoding="utf-8"))
            event_lines = (revision_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read strict V2 strategy draft") from exc
        if not isinstance(entry_raw, dict) or len(event_lines) != 1:
            raise ValueError("V2 strategy draft repository is malformed")
        try:
            event_raw = json.loads(event_lines[0])
        except json.JSONDecodeError as exc:
            raise ValueError("V2 strategy draft event is malformed") from exc
        if not isinstance(event_raw, dict):
            raise TypeError("V2 strategy draft event must be an object")
        entry = StrategyEntryV2.from_mapping(entry_raw)
        event = StrategyDraftEventV2.from_mapping(event_raw)
        if entry.ref != ref or event.strategy_ref != ref:
            raise ValueError("V2 strategy repository refs differ")
        return StrategyDraftRecordV2(entry=entry, event=event)

    def _revision_dir(self, strategy_id: str, revision: int) -> Path:
        strategy_id = identifier(strategy_id, context="strategy_id")
        result = (self._root / "v2" / strategy_id / "revisions" / f"{revision:04d}").resolve()
        if not result.is_relative_to(self._root):
            raise ValueError("strategy path escapes the repository root")
        return result


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
