"""Versioned system policy for bounded intent communication."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ..contracts.common import as_mapping, as_sequence, identifier, integer, strict_keys
from .models import COMMUNICATION_SCHEMA_VERSION, MAX_MODEL_ATTEMPTS

INTENT_COMMUNICATION_POLICY_SCHEMA_ID: Final[str] = "intent-communication-policy"
INTENT_COMMUNICATION_POLICY_SCHEMA_VERSION: Final[str] = COMMUNICATION_SCHEMA_VERSION
SUPPORTED_AMBIGUITY_CODES: Final[tuple[str, ...]] = (
    "objective-selection-ambiguous",
    "objective-priority-ambiguous",
    "decision-variable-selection-ambiguous",
    "result-alternatives-ambiguous",
)
MAXIMUM_CLARIFICATION_TURNS: Final[int] = 3
MAXIMUM_QUESTIONS_PER_TURN: Final[int] = 3


def _ambiguity_codes(value: Sequence[object]) -> tuple[str, ...]:
    result = tuple(
        identifier(item, context=f"allowed_ambiguity_codes[{index}]")
        for index, item in enumerate(value)
    )
    if not result:
        raise ValueError("allowed_ambiguity_codes must be non-empty")
    if len(result) != len(set(result)):
        raise ValueError("allowed_ambiguity_codes must be unique")
    return result


@dataclass(frozen=True)
class IntentCommunicationPolicy:
    """System-owned limits and ambiguity vocabulary; model output cannot override it."""

    schema_id: str = INTENT_COMMUNICATION_POLICY_SCHEMA_ID
    schema_version: str = INTENT_COMMUNICATION_POLICY_SCHEMA_VERSION
    maximum_model_attempts: int = MAX_MODEL_ATTEMPTS
    maximum_clarification_turns: int = 3
    maximum_questions_per_turn: int = 3
    allowed_ambiguity_codes: tuple[str, ...] = SUPPORTED_AMBIGUITY_CODES

    def __post_init__(self) -> None:
        if self.schema_id != INTENT_COMMUNICATION_POLICY_SCHEMA_ID:
            raise ValueError("schema_id differs from the intent communication policy")
        if self.schema_version != INTENT_COMMUNICATION_POLICY_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the intent communication policy")
        object.__setattr__(
            self,
            "maximum_model_attempts",
            integer(self.maximum_model_attempts, context="maximum_model_attempts", minimum=1),
        )
        if self.maximum_model_attempts > MAX_MODEL_ATTEMPTS:
            raise ValueError("maximum_model_attempts exceeds the fixed safety ceiling")
        object.__setattr__(
            self,
            "maximum_clarification_turns",
            integer(
                self.maximum_clarification_turns,
                context="maximum_clarification_turns",
                minimum=1,
            ),
        )
        if self.maximum_clarification_turns > MAXIMUM_CLARIFICATION_TURNS:
            raise ValueError("maximum_clarification_turns exceeds the fixed safety ceiling")
        object.__setattr__(
            self,
            "maximum_questions_per_turn",
            integer(
                self.maximum_questions_per_turn,
                context="maximum_questions_per_turn",
                minimum=1,
            ),
        )
        if self.maximum_questions_per_turn > MAXIMUM_QUESTIONS_PER_TURN:
            raise ValueError("maximum_questions_per_turn exceeds the fixed safety ceiling")
        ambiguity_codes = _ambiguity_codes(self.allowed_ambiguity_codes)
        unknown = sorted(set(ambiguity_codes) - set(SUPPORTED_AMBIGUITY_CODES))
        if unknown:
            raise ValueError(f"policy contains unsupported ambiguity codes: {unknown}")
        object.__setattr__(self, "allowed_ambiguity_codes", ambiguity_codes)

    @classmethod
    def from_mapping(cls, value: object) -> IntentCommunicationPolicy:
        raw = as_mapping(value, context="intent communication policy")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "maximum_model_attempts",
                "maximum_clarification_turns",
                "maximum_questions_per_turn",
                "allowed_ambiguity_codes",
            },
            context="intent communication policy",
        )
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            maximum_model_attempts=integer(
                raw["maximum_model_attempts"], context="maximum_model_attempts", minimum=1
            ),
            maximum_clarification_turns=integer(
                raw["maximum_clarification_turns"],
                context="maximum_clarification_turns",
                minimum=1,
            ),
            maximum_questions_per_turn=integer(
                raw["maximum_questions_per_turn"],
                context="maximum_questions_per_turn",
                minimum=1,
            ),
            allowed_ambiguity_codes=_ambiguity_codes(
                as_sequence(raw["allowed_ambiguity_codes"], context="allowed_ambiguity_codes")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "maximum_model_attempts": self.maximum_model_attempts,
            "maximum_clarification_turns": self.maximum_clarification_turns,
            "maximum_questions_per_turn": self.maximum_questions_per_turn,
            "allowed_ambiguity_codes": list(self.allowed_ambiguity_codes),
        }
