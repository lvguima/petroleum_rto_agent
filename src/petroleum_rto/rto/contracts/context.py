"""Trusted operating-context contract kept outside business intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .common import (
    JsonValue,
    as_mapping,
    canonical_fingerprint,
    finite,
    freeze_json_mapping,
    identifier,
    numeric_mapping,
    strict_keys,
    text,
    thaw_json,
)
from .problem import ENGINEERING_CLAIM_SCOPE
from .reference import ContractRef

OPERATING_CONTEXT_SCHEMA_ID: Final[str] = "operating-context"
OPERATING_CONTEXT_SCHEMA_VERSION: Final[str] = "2.0.0"


def _timestamp(value: object) -> str:
    raw = text(value, context="data_timestamp")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("data_timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("data_timestamp must include a UTC offset")
    return raw


@dataclass(frozen=True)
class OperatingContext:
    """One immutable, trusted snapshot of non-LLM operating facts."""

    schema_id: str
    schema_version: str
    context_version: str
    context_id: str
    provider_id: str
    model_ref: ContractRef
    case_ref: ContractRef
    operating_mode: str
    facts: Mapping[str, JsonValue]
    current_setpoints: Mapping[str, float]
    initial_state: Mapping[str, float]
    data_timestamp: str
    data_quality: str
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_id != OPERATING_CONTEXT_SCHEMA_ID:
            raise ValueError("schema_id differs from the operating context contract")
        if self.schema_version != OPERATING_CONTEXT_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the operating context contract")
        if self.claim_scope != ENGINEERING_CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        for name in ("context_version", "context_id", "provider_id", "operating_mode"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        for name in ("model_ref", "case_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        facts = freeze_json_mapping(self.facts, context="context facts")
        if not facts:
            raise ValueError("context facts must be non-empty")
        feed = finite(facts.get("fresh_feed_load_kg_s"), context="fresh_feed_load_kg_s")
        if feed <= 0.0:
            raise ValueError("fresh_feed_load_kg_s must be positive")
        composition = numeric_mapping(facts.get("feed_composition"), context="feed_composition")
        if not composition:
            raise ValueError("feed_composition must be non-empty")
        object.__setattr__(self, "facts", facts)
        setpoints = numeric_mapping(self.current_setpoints, context="current_setpoints")
        if not setpoints:
            raise ValueError("current_setpoints must be non-empty")
        object.__setattr__(self, "current_setpoints", setpoints)
        object.__setattr__(
            self, "initial_state", numeric_mapping(self.initial_state, context="initial_state")
        )
        object.__setattr__(self, "data_timestamp", _timestamp(self.data_timestamp))
        object.__setattr__(
            self, "data_quality", identifier(self.data_quality, context="data_quality")
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OperatingContext:
        strict_keys(
            value,
            required={
                "schema_id",
                "schema_version",
                "context_version",
                "context_id",
                "provider_id",
                "model_ref",
                "case_ref",
                "operating_mode",
                "facts",
                "current_setpoints",
                "initial_state",
                "data_timestamp",
                "data_quality",
                "claim_scope",
            },
            optional={"context_fingerprint"},
            context="operating context",
        )
        context = cls(
            schema_id=text(value["schema_id"], context="schema_id"),
            schema_version=text(value["schema_version"], context="schema_version"),
            context_version=identifier(value["context_version"], context="context_version"),
            context_id=identifier(value["context_id"], context="context_id"),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            model_ref=ContractRef.from_mapping(as_mapping(value["model_ref"], context="model_ref")),
            case_ref=ContractRef.from_mapping(as_mapping(value["case_ref"], context="case_ref")),
            operating_mode=identifier(value["operating_mode"], context="operating_mode"),
            facts=freeze_json_mapping(value["facts"], context="context facts"),
            current_setpoints=numeric_mapping(
                value["current_setpoints"], context="current_setpoints"
            ),
            initial_state=numeric_mapping(value["initial_state"], context="initial_state"),
            data_timestamp=_timestamp(value["data_timestamp"]),
            data_quality=identifier(value["data_quality"], context="data_quality"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("context_fingerprint") not in {None, context.fingerprint}:
            raise ValueError("context_fingerprint differs from context content")
        return context

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "context_version": self.context_version,
            "context_id": self.context_id,
            "provider_id": self.provider_id,
            "model_ref": self.model_ref.as_dict(),
            "case_ref": self.case_ref.as_dict(),
            "operating_mode": self.operating_mode,
            "facts": thaw_json(self.facts),
            "current_setpoints": dict(self.current_setpoints),
            "initial_state": dict(self.initial_state),
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

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "context_fingerprint": self.fingerprint}
