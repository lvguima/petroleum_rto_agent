"""Strict contracts for communication between a domain model and RTO."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast

from ..capabilities import PublicCapabilityManifest
from ..contracts.common import (
    JsonValue,
    as_mapping,
    as_sequence,
    boolean,
    canonical_fingerprint,
    freeze_json_mapping,
    identifier,
    integer,
    strict_keys,
    text,
    thaw_json,
)
from ..contracts.reference import ContractRef
from ..intent import (
    OPTIMIZATION_INTENT_SCHEMA_ID,
    OPTIMIZATION_INTENT_SCHEMA_VERSION,
    OptimizationIntent,
)

COMMUNICATION_SCHEMA_VERSION: Final[str] = "1.0.0"
DOMAIN_MODEL_REQUEST_SCHEMA_ID: Final[str] = "domain-model-intent-request"
DOMAIN_MODEL_REQUEST_SCHEMA_VERSION: Final[str] = "1.2.0"
DOMAIN_CAPABILITY_MANIFEST_SCHEMA_ID: Final[str] = "domain-model-capability-manifest"
DOMAIN_CAPABILITY_MANIFEST_SCHEMA_VERSION: Final[str] = "1.1.0"
DOMAIN_MODEL_RESPONSE_SCHEMA_ID: Final[str] = "domain-model-intent-response"
DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION: Final[str] = "1.1.0"
DOMAIN_MODEL_UNSUPPORTED_SCHEMA_ID: Final[str] = "domain-model-unsupported"
DOMAIN_MODEL_UNSUPPORTED_SCHEMA_VERSION: Final[str] = "1.0.0"
COMMUNICATION_RESULT_SCHEMA_ID: Final[str] = "intent-communication-result"
MAX_MODEL_ATTEMPTS: Final[int] = 2

type AnswerKind = Literal["single-select", "multi-select", "ordered-select", "free-text"]
type IssueAudience = Literal["model", "user", "operator"]
type IssueSource = Literal["contract", "protocol", "ambiguity", "capability"]
type CommunicationStatus = Literal[
    "repair_required",
    "needs_clarification",
    "resolved",
    "unsupported",
    "failed",
]
type DomainModelResponseOutcome = Literal["intent", "unsupported"]
type UnsupportedReasonCode = Literal[
    "objective-not-published",
    "decision-variable-not-published",
    "business-constraint-not-supported",
    "operating-context-request-forbidden",
    "solver-selection-forbidden",
    "system-policy-override-forbidden",
    "outside-domain-scope",
]

UNSUPPORTED_SAFE_MESSAGES: Final[Mapping[UnsupportedReasonCode, str]] = MappingProxyType(
    {
        "objective-not-published": (
            "The requested business objective is not published in the current capability manifest."
        ),
        "decision-variable-not-published": (
            "The requested decision variable is not published in the current capability manifest."
        ),
        "business-constraint-not-supported": (
            "Additional business constraints are not supported by the current intent contract."
        ),
        "operating-context-request-forbidden": (
            "Operating-context values cannot be supplied or inferred through the intent contract."
        ),
        "solver-selection-forbidden": (
            "Solver selection is outside the domain-model intent boundary."
        ),
        "system-policy-override-forbidden": (
            "System policy and hard guardrails cannot be changed through business intent."
        ),
        "outside-domain-scope": ("The request is outside the published domain-model intent scope."),
    }
)


def _unique_identifiers(
    value: Sequence[object],
    *,
    context: str,
    require_non_empty: bool,
) -> tuple[str, ...]:
    result = tuple(
        identifier(item, context=f"{context}[{index}]") for index, item in enumerate(value)
    )
    if require_non_empty and not result:
        raise ValueError(f"{context} must be non-empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{context} must be unique")
    return result


def _texts(
    value: Sequence[object],
    *,
    context: str,
    require_non_empty: bool,
) -> tuple[str, ...]:
    result = tuple(text(item, context=f"{context}[{index}]") for index, item in enumerate(value))
    if require_non_empty and not result:
        raise ValueError(f"{context} must be non-empty")
    return result


def _project_rows(
    rows: tuple[Mapping[str, JsonValue], ...],
    fields: tuple[str, ...],
) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(
        freeze_json_mapping(
            {field: thaw_json(row[field]) for field in fields},
            context="domain capability row",
        )
        for row in rows
    )


def _project_result_output_rules(
    rows: tuple[Mapping[str, JsonValue], ...],
) -> tuple[Mapping[str, JsonValue], ...]:
    projected: list[Mapping[str, JsonValue]] = []
    for index, row in enumerate(rows):
        minimum = integer(
            row["minimum_objectives"],
            context=f"execution_routes[{index}].minimum_objectives",
            minimum=1,
        )
        maximum = integer(
            row["maximum_objectives"],
            context=f"execution_routes[{index}].maximum_objectives",
            minimum=minimum,
        )
        top_k = integer(
            row["top_k"],
            context=f"execution_routes[{index}].top_k",
            minimum=1,
        )
        if minimum == 1 and maximum != 1:
            raise ValueError("execution routes must separate single and multiple objectives")
        include_alternatives = minimum >= 2
        projected.append(
            freeze_json_mapping(
                {
                    "rule_id": f"result-output-{minimum}-{maximum}",
                    "minimum_objectives": minimum,
                    "maximum_objectives": maximum,
                    "output_kind": "steady-setpoint-vector",
                    "default_include_alternatives": include_alternatives,
                    "default_max_candidates": top_k if include_alternatives else 1,
                    "maximum_candidates": top_k,
                },
                context="domain capability result output rule",
            )
        )
    return tuple(projected)


def _strict_capability_rows(
    value: object,
    *,
    fields: tuple[str, ...],
    identity_field: str,
    context: str,
) -> tuple[Mapping[str, JsonValue], ...]:
    rows: list[Mapping[str, JsonValue]] = []
    identifiers: list[str] = []
    for index, item in enumerate(as_sequence(value, context=context)):
        raw = as_mapping(item, context=f"{context}[{index}]")
        strict_keys(raw, required=set(fields), context=f"{context}[{index}]")
        identifiers.append(
            identifier(raw[identity_field], context=f"{context}[{index}].{identity_field}")
        )
        rows.append(freeze_json_mapping(raw, context=f"{context}[{index}]"))
    if not rows:
        raise ValueError(f"{context} must be non-empty")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{context} identities must be unique")
    return tuple(rows)


@dataclass(frozen=True)
class UserMessage:
    message_id: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", identifier(self.message_id, context="message_id"))
        object.__setattr__(self, "text", text(self.text, context="user message text"))

    @classmethod
    def from_mapping(cls, value: object) -> UserMessage:
        raw = as_mapping(value, context="user message")
        strict_keys(raw, required={"message_id", "text"}, context="user message")
        return cls(
            message_id=identifier(raw["message_id"], context="message_id"),
            text=text(raw["text"], context="user message text"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"message_id": self.message_id, "text": self.text}


@dataclass(frozen=True)
class ProtocolIssue:
    code: str
    json_pointer: str
    message: str
    audience: IssueAudience
    source: IssueSource
    retryable: bool
    supported_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", identifier(self.code, context="issue code"))
        if not isinstance(self.json_pointer, str) or not self.json_pointer.startswith("/"):
            raise ValueError("issue json_pointer must start with /")
        object.__setattr__(self, "message", text(self.message, context="issue message"))
        if self.audience not in {"model", "user", "operator"}:
            raise ValueError("unsupported issue audience")
        if self.source not in {"contract", "protocol", "ambiguity", "capability"}:
            raise ValueError("unsupported issue source")
        object.__setattr__(self, "retryable", boolean(self.retryable, context="retryable"))
        object.__setattr__(
            self,
            "supported_values",
            _unique_identifiers(
                self.supported_values,
                context="issue supported_values",
                require_non_empty=False,
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> ProtocolIssue:
        raw = as_mapping(value, context="protocol issue")
        strict_keys(
            raw,
            required={
                "code",
                "json_pointer",
                "message",
                "audience",
                "source",
                "retryable",
                "supported_values",
            },
            context="protocol issue",
        )
        audience = raw["audience"]
        source = raw["source"]
        if audience not in {"model", "user", "operator"}:
            raise ValueError("unsupported issue audience")
        if source not in {"contract", "protocol", "ambiguity", "capability"}:
            raise ValueError("unsupported issue source")
        return cls(
            code=identifier(raw["code"], context="issue code"),
            json_pointer=text(raw["json_pointer"], context="issue json_pointer"),
            message=text(raw["message"], context="issue message"),
            audience=audience,
            source=source,
            retryable=boolean(raw["retryable"], context="retryable"),
            supported_values=_unique_identifiers(
                as_sequence(raw["supported_values"], context="supported_values"),
                context="issue supported_values",
                require_non_empty=False,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "json_pointer": self.json_pointer,
            "message": self.message,
            "audience": self.audience,
            "source": self.source,
            "retryable": self.retryable,
            "supported_values": list(self.supported_values),
        }


@dataclass(frozen=True)
class ClarificationOption:
    value: str
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", identifier(self.value, context="option value"))
        object.__setattr__(self, "label", text(self.label, context="option label"))

    @classmethod
    def from_mapping(cls, value: object) -> ClarificationOption:
        raw = as_mapping(value, context="clarification option")
        strict_keys(raw, required={"value", "label"}, context="clarification option")
        return cls(
            value=identifier(raw["value"], context="option value"),
            label=text(raw["label"], context="option label"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True)
class ClarificationQuestion:
    question_id: str
    ambiguity_code: str
    json_pointer: str
    prompt: str
    answer_kind: AnswerKind
    options: tuple[ClarificationOption, ...]
    minimum_selections: int
    maximum_selections: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_id", identifier(self.question_id, context="question_id"))
        object.__setattr__(
            self,
            "ambiguity_code",
            identifier(self.ambiguity_code, context="ambiguity_code"),
        )
        if not isinstance(self.json_pointer, str) or not self.json_pointer.startswith("/"):
            raise ValueError("question json_pointer must start with /")
        object.__setattr__(self, "prompt", text(self.prompt, context="question prompt"))
        if self.answer_kind not in {
            "single-select",
            "multi-select",
            "ordered-select",
            "free-text",
        }:
            raise ValueError("unsupported clarification answer kind")
        options = tuple(self.options)
        if any(not isinstance(item, ClarificationOption) for item in options):
            raise TypeError("options must contain ClarificationOption values")
        option_values = tuple(item.value for item in options)
        if len(option_values) != len(set(option_values)):
            raise ValueError("clarification option values must be unique")
        object.__setattr__(self, "options", options)
        minimum = integer(self.minimum_selections, context="minimum_selections", minimum=1)
        maximum = integer(self.maximum_selections, context="maximum_selections", minimum=1)
        if minimum > maximum:
            raise ValueError("minimum_selections must not exceed maximum_selections")
        if self.answer_kind == "free-text":
            if options or minimum != 1 or maximum != 1:
                raise ValueError("free-text questions require no options and one answer")
        else:
            if not options or maximum > len(options):
                raise ValueError("selection questions require enough declared options")
            if self.answer_kind == "single-select" and (minimum != 1 or maximum != 1):
                raise ValueError("single-select questions require exactly one selection")
        object.__setattr__(self, "minimum_selections", minimum)
        object.__setattr__(self, "maximum_selections", maximum)

    @classmethod
    def from_mapping(cls, value: object) -> ClarificationQuestion:
        raw = as_mapping(value, context="clarification question")
        strict_keys(
            raw,
            required={
                "question_id",
                "ambiguity_code",
                "json_pointer",
                "prompt",
                "answer_kind",
                "options",
                "minimum_selections",
                "maximum_selections",
            },
            context="clarification question",
        )
        answer_kind = raw["answer_kind"]
        if answer_kind not in {
            "single-select",
            "multi-select",
            "ordered-select",
            "free-text",
        }:
            raise ValueError("unsupported clarification answer kind")
        return cls(
            question_id=identifier(raw["question_id"], context="question_id"),
            ambiguity_code=identifier(raw["ambiguity_code"], context="ambiguity_code"),
            json_pointer=text(raw["json_pointer"], context="question json_pointer"),
            prompt=text(raw["prompt"], context="question prompt"),
            answer_kind=answer_kind,
            options=tuple(
                ClarificationOption.from_mapping(item)
                for item in as_sequence(raw["options"], context="question options")
            ),
            minimum_selections=integer(
                raw["minimum_selections"], context="minimum_selections", minimum=1
            ),
            maximum_selections=integer(
                raw["maximum_selections"], context="maximum_selections", minimum=1
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "ambiguity_code": self.ambiguity_code,
            "json_pointer": self.json_pointer,
            "prompt": self.prompt,
            "answer_kind": self.answer_kind,
            "options": [item.as_dict() for item in self.options],
            "minimum_selections": self.minimum_selections,
            "maximum_selections": self.maximum_selections,
        }


@dataclass(frozen=True)
class ClarificationAnswer:
    question_id: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_id", identifier(self.question_id, context="question_id"))
        values = _texts(self.values, context="clarification answer values", require_non_empty=True)
        if len(values) != len(set(values)):
            raise ValueError("clarification answer values must be unique")
        object.__setattr__(self, "values", values)

    @classmethod
    def from_mapping(cls, value: object) -> ClarificationAnswer:
        raw = as_mapping(value, context="clarification answer")
        strict_keys(raw, required={"question_id", "values"}, context="clarification answer")
        return cls(
            question_id=identifier(raw["question_id"], context="question_id"),
            values=_texts(
                as_sequence(raw["values"], context="clarification answer values"),
                context="clarification answer values",
                require_non_empty=True,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {"question_id": self.question_id, "values": list(self.values)}


@dataclass(frozen=True)
class ClarificationRequest:
    request_ref: ContractRef
    candidate_intent_ref: ContractRef
    questions: tuple[ClarificationQuestion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request_ref, ContractRef):
            raise TypeError("request_ref must be ContractRef")
        if not isinstance(self.candidate_intent_ref, ContractRef):
            raise TypeError("candidate_intent_ref must be ContractRef")
        questions = tuple(self.questions)
        if not questions:
            raise ValueError("clarification request must contain at least one question")
        if any(not isinstance(item, ClarificationQuestion) for item in questions):
            raise TypeError("questions must contain ClarificationQuestion values")
        ids = tuple(item.question_id for item in questions)
        if len(ids) != len(set(ids)):
            raise ValueError("clarification question ids must be unique")
        object.__setattr__(self, "questions", questions)

    @classmethod
    def from_mapping(cls, value: object) -> ClarificationRequest:
        raw = as_mapping(value, context="clarification request")
        strict_keys(
            raw,
            required={"request_ref", "candidate_intent_ref", "questions"},
            context="clarification request",
        )
        return cls(
            request_ref=ContractRef.from_mapping(
                as_mapping(raw["request_ref"], context="request_ref")
            ),
            candidate_intent_ref=ContractRef.from_mapping(
                as_mapping(raw["candidate_intent_ref"], context="candidate_intent_ref")
            ),
            questions=tuple(
                ClarificationQuestion.from_mapping(item)
                for item in as_sequence(raw["questions"], context="clarification questions")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "request_ref": self.request_ref.as_dict(),
            "candidate_intent_ref": self.candidate_intent_ref.as_dict(),
            "questions": [item.as_dict() for item in self.questions],
        }


def _validate_answer_set(
    clarification: ClarificationRequest,
    answers: tuple[ClarificationAnswer, ...],
) -> None:
    by_id = {item.question_id: item for item in answers}
    questions = {item.question_id: item for item in clarification.questions}
    if set(by_id) != set(questions):
        raise ValueError("clarification answers must cover exactly the pending questions")
    for question_id, answer in by_id.items():
        question = questions[question_id]
        if not question.minimum_selections <= len(answer.values) <= question.maximum_selections:
            raise ValueError(f"answer count is invalid for question {question_id!r}")
        if question.answer_kind != "free-text":
            allowed = {item.value for item in question.options}
            if not set(answer.values) <= allowed:
                raise ValueError(f"answer contains unsupported value for {question_id!r}")


@dataclass(frozen=True)
class RepairDirective:
    request_ref: ContractRef
    next_model_attempt: int
    required_action: Literal["return-full-replacement"]
    issues: tuple[ProtocolIssue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request_ref, ContractRef):
            raise TypeError("request_ref must be ContractRef")
        attempt = integer(self.next_model_attempt, context="next_model_attempt", minimum=2)
        object.__setattr__(self, "next_model_attempt", attempt)
        if self.required_action != "return-full-replacement":
            raise ValueError("repair requires a complete replacement response")
        issues = tuple(self.issues)
        if not issues or any(not isinstance(item, ProtocolIssue) for item in issues):
            raise TypeError("repair issues must contain ProtocolIssue values")
        if any(not item.retryable or item.audience != "model" for item in issues):
            raise ValueError("repair issues must be retryable model issues")
        object.__setattr__(self, "issues", issues)

    @classmethod
    def from_mapping(cls, value: object) -> RepairDirective:
        raw = as_mapping(value, context="repair directive")
        strict_keys(
            raw,
            required={"request_ref", "next_model_attempt", "required_action", "issues"},
            context="repair directive",
        )
        action = raw["required_action"]
        if action != "return-full-replacement":
            raise ValueError("repair requires a complete replacement response")
        return cls(
            request_ref=ContractRef.from_mapping(
                as_mapping(raw["request_ref"], context="request_ref")
            ),
            next_model_attempt=integer(
                raw["next_model_attempt"], context="next_model_attempt", minimum=2
            ),
            required_action="return-full-replacement",
            issues=tuple(
                ProtocolIssue.from_mapping(item)
                for item in as_sequence(raw["issues"], context="repair issues")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "request_ref": self.request_ref.as_dict(),
            "next_model_attempt": self.next_model_attempt,
            "required_action": self.required_action,
            "issues": [item.as_dict() for item in self.issues],
        }


@dataclass(frozen=True)
class DomainOutputPolicy:
    constraints_mode: Literal["system-only"]
    operating_context_mode: Literal["excluded"]
    solver_selection_mode: Literal["forbidden"]
    response_mode: Literal["full-replacement"]
    maximum_model_attempts: int

    def __post_init__(self) -> None:
        if self.constraints_mode != "system-only":
            raise ValueError("domain models must not create or override system constraints")
        if self.operating_context_mode != "excluded":
            raise ValueError("trusted operating context must remain outside model output")
        if self.solver_selection_mode != "forbidden":
            raise ValueError("domain models must not select an optimization solver")
        if self.response_mode != "full-replacement":
            raise ValueError("domain model responses must be complete replacements")
        maximum = integer(
            self.maximum_model_attempts,
            context="maximum_model_attempts",
            minimum=1,
        )
        if maximum > MAX_MODEL_ATTEMPTS:
            raise ValueError("maximum_model_attempts exceeds the fixed safety ceiling")
        object.__setattr__(self, "maximum_model_attempts", maximum)

    @classmethod
    def from_mapping(cls, value: object) -> DomainOutputPolicy:
        raw = as_mapping(value, context="domain output policy")
        strict_keys(
            raw,
            required={
                "constraints_mode",
                "operating_context_mode",
                "solver_selection_mode",
                "response_mode",
                "maximum_model_attempts",
            },
            context="domain output policy",
        )
        if raw["constraints_mode"] != "system-only":
            raise ValueError("domain models must not create or override system constraints")
        if raw["operating_context_mode"] != "excluded":
            raise ValueError("trusted operating context must remain outside model output")
        if raw["solver_selection_mode"] != "forbidden":
            raise ValueError("domain models must not select an optimization solver")
        if raw["response_mode"] != "full-replacement":
            raise ValueError("domain model responses must be complete replacements")
        return cls(
            constraints_mode="system-only",
            operating_context_mode="excluded",
            solver_selection_mode="forbidden",
            response_mode="full-replacement",
            maximum_model_attempts=integer(
                raw["maximum_model_attempts"],
                context="maximum_model_attempts",
                minimum=1,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "constraints_mode": self.constraints_mode,
            "operating_context_mode": self.operating_context_mode,
            "solver_selection_mode": self.solver_selection_mode,
            "response_mode": self.response_mode,
            "maximum_model_attempts": self.maximum_model_attempts,
        }


@dataclass(frozen=True)
class DomainCapabilityManifest:
    """The only capability projection authorized to leave the trusted RTO boundary."""

    schema_id: str
    schema_version: str
    manifest_id: str
    manifest_version: str
    claim_scope: str
    metrics: tuple[Mapping[str, JsonValue], ...]
    objectives: tuple[Mapping[str, JsonValue], ...]
    decisions: tuple[Mapping[str, JsonValue], ...]
    selectors: tuple[Mapping[str, JsonValue], ...]
    cardinality_rules: tuple[Mapping[str, JsonValue], ...]
    result_output_rules: tuple[Mapping[str, JsonValue], ...]

    def __post_init__(self) -> None:
        if self.schema_id != DOMAIN_CAPABILITY_MANIFEST_SCHEMA_ID:
            raise ValueError("schema_id differs from the domain capability manifest")
        if self.schema_version != DOMAIN_CAPABILITY_MANIFEST_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the domain capability manifest")
        object.__setattr__(self, "manifest_id", identifier(self.manifest_id, context="manifest_id"))
        object.__setattr__(
            self,
            "manifest_version",
            identifier(self.manifest_version, context="manifest_version"),
        )
        if self.claim_scope != "engineering_simulation_only":
            raise ValueError("claim_scope must be engineering_simulation_only")
        row_specs = (
            (
                "metrics",
                (
                    "metric_id",
                    "business_name",
                    "unit",
                    "direction",
                    "proxy",
                    "availability",
                ),
                "metric_id",
            ),
            (
                "objectives",
                ("objective_id", "business_name", "metric_id", "sense", "availability"),
                "objective_id",
            ),
            (
                "decisions",
                ("decision_id", "business_name", "display_unit", "availability"),
                "decision_id",
            ),
            (
                "selectors",
                (
                    "selector_id",
                    "business_name",
                    "method",
                    "minimum_objectives",
                    "maximum_objectives",
                    "availability",
                ),
                "selector_id",
            ),
            (
                "cardinality_rules",
                ("rule_id", "subject_kind", "subject_ids", "minimum_count", "maximum_count"),
                "rule_id",
            ),
            (
                "result_output_rules",
                (
                    "rule_id",
                    "minimum_objectives",
                    "maximum_objectives",
                    "output_kind",
                    "default_include_alternatives",
                    "default_max_candidates",
                    "maximum_candidates",
                ),
                "rule_id",
            ),
        )
        for field_name, fields, identity_field in row_specs:
            rows = _strict_capability_rows(
                [thaw_json(item) for item in getattr(self, field_name)],
                fields=fields,
                identity_field=identity_field,
                context=f"domain capability {field_name}",
            )
            object.__setattr__(self, field_name, rows)
        for index, row in enumerate(self.result_output_rules):
            minimum = integer(
                row["minimum_objectives"],
                context=f"result_output_rules[{index}].minimum_objectives",
                minimum=1,
            )
            maximum = integer(
                row["maximum_objectives"],
                context=f"result_output_rules[{index}].maximum_objectives",
                minimum=minimum,
            )
            if minimum == 1 and maximum != 1:
                raise ValueError("result output rules must not mix single and multiple objectives")
            if row["output_kind"] != "steady-setpoint-vector":
                raise ValueError("domain-model result output_kind is unsupported")
            include_alternatives = boolean(
                row["default_include_alternatives"],
                context=f"result_output_rules[{index}].default_include_alternatives",
            )
            default_candidates = integer(
                row["default_max_candidates"],
                context=f"result_output_rules[{index}].default_max_candidates",
                minimum=1,
            )
            maximum_candidates = integer(
                row["maximum_candidates"],
                context=f"result_output_rules[{index}].maximum_candidates",
                minimum=1,
            )
            if default_candidates > maximum_candidates:
                raise ValueError("default_max_candidates exceeds the published maximum")
            if not include_alternatives and default_candidates != 1:
                raise ValueError("a non-alternative default must return one candidate")

    @classmethod
    def from_public(cls, manifest: PublicCapabilityManifest) -> DomainCapabilityManifest:
        if not isinstance(manifest, PublicCapabilityManifest):
            raise TypeError("manifest must be PublicCapabilityManifest")
        available_objectives = tuple(
            row for row in manifest.objectives if row["availability"] == "available"
        )
        available_decisions = tuple(
            row for row in manifest.decisions if row["availability"] == "available"
        )
        objective_minimum = min(
            integer(row["minimum_objectives"], context="minimum_objectives", minimum=1)
            for row in manifest.execution_routes
        )
        objective_maximum = max(
            integer(row["maximum_objectives"], context="maximum_objectives", minimum=1)
            for row in manifest.execution_routes
        )
        cardinality_rules = tuple(
            freeze_json_mapping(row, context="derived cardinality rule")
            for row in (
                {
                    "rule_id": "available-decision-cardinality",
                    "subject_kind": "decision",
                    "subject_ids": tuple(row["decision_id"] for row in available_decisions),
                    "minimum_count": 1,
                    "maximum_count": len(available_decisions),
                },
                {
                    "rule_id": "execution-route-objective-cardinality",
                    "subject_kind": "objective",
                    "subject_ids": tuple(row["objective_id"] for row in available_objectives),
                    "minimum_count": objective_minimum,
                    "maximum_count": objective_maximum,
                },
            )
        )
        return cls(
            schema_id=DOMAIN_CAPABILITY_MANIFEST_SCHEMA_ID,
            schema_version=DOMAIN_CAPABILITY_MANIFEST_SCHEMA_VERSION,
            manifest_id=f"{manifest.manifest_id}.domain-model",
            manifest_version=manifest.manifest_version,
            claim_scope=manifest.claim_scope,
            metrics=_project_rows(
                manifest.metrics,
                (
                    "metric_id",
                    "business_name",
                    "unit",
                    "direction",
                    "proxy",
                    "availability",
                ),
            ),
            objectives=_project_rows(
                manifest.objectives,
                ("objective_id", "business_name", "metric_id", "sense", "availability"),
            ),
            decisions=_project_rows(
                manifest.decisions,
                ("decision_id", "business_name", "display_unit", "availability"),
            ),
            selectors=_project_rows(
                manifest.selectors,
                (
                    "selector_id",
                    "business_name",
                    "method",
                    "minimum_objectives",
                    "maximum_objectives",
                    "availability",
                ),
            ),
            cardinality_rules=_project_rows(
                cardinality_rules,
                ("rule_id", "subject_kind", "subject_ids", "minimum_count", "maximum_count"),
            ),
            result_output_rules=_project_result_output_rules(manifest.execution_routes),
        )

    @classmethod
    def from_mapping(cls, value: object) -> DomainCapabilityManifest:
        raw = as_mapping(value, context="domain capability manifest")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "manifest_id",
                "manifest_version",
                "claim_scope",
                "metrics",
                "objectives",
                "decisions",
                "selectors",
                "cardinality_rules",
                "result_output_rules",
            },
            context="domain capability manifest",
        )
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            manifest_id=identifier(raw["manifest_id"], context="manifest_id"),
            manifest_version=identifier(raw["manifest_version"], context="manifest_version"),
            claim_scope=text(raw["claim_scope"], context="claim_scope"),
            metrics=_strict_capability_rows(
                raw["metrics"],
                fields=(
                    "metric_id",
                    "business_name",
                    "unit",
                    "direction",
                    "proxy",
                    "availability",
                ),
                identity_field="metric_id",
                context="domain capability metrics",
            ),
            objectives=_strict_capability_rows(
                raw["objectives"],
                fields=("objective_id", "business_name", "metric_id", "sense", "availability"),
                identity_field="objective_id",
                context="domain capability objectives",
            ),
            decisions=_strict_capability_rows(
                raw["decisions"],
                fields=("decision_id", "business_name", "display_unit", "availability"),
                identity_field="decision_id",
                context="domain capability decisions",
            ),
            selectors=_strict_capability_rows(
                raw["selectors"],
                fields=(
                    "selector_id",
                    "business_name",
                    "method",
                    "minimum_objectives",
                    "maximum_objectives",
                    "availability",
                ),
                identity_field="selector_id",
                context="domain capability selectors",
            ),
            cardinality_rules=_strict_capability_rows(
                raw["cardinality_rules"],
                fields=("rule_id", "subject_kind", "subject_ids", "minimum_count", "maximum_count"),
                identity_field="rule_id",
                context="domain capability cardinality_rules",
            ),
            result_output_rules=_strict_capability_rows(
                raw["result_output_rules"],
                fields=(
                    "rule_id",
                    "minimum_objectives",
                    "maximum_objectives",
                    "output_kind",
                    "default_include_alternatives",
                    "default_max_candidates",
                    "maximum_candidates",
                ),
                identity_field="rule_id",
                context="domain capability result_output_rules",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "claim_scope": self.claim_scope,
            "metrics": [thaw_json(item) for item in self.metrics],
            "objectives": [thaw_json(item) for item in self.objectives],
            "decisions": [thaw_json(item) for item in self.decisions],
            "selectors": [thaw_json(item) for item in self.selectors],
            "cardinality_rules": [thaw_json(item) for item in self.cardinality_rules],
            "result_output_rules": [thaw_json(item) for item in self.result_output_rules],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.manifest_id, self.fingerprint)


@dataclass(frozen=True)
class DomainModelRequest:
    schema_id: str
    schema_version: str
    request_id: str
    session_id: str
    turn_index: int
    model_attempt: int
    capability_manifest: DomainCapabilityManifest
    capability_manifest_ref: ContractRef
    user_messages: tuple[UserMessage, ...]
    prior_intent: OptimizationIntent | None
    prior_clarification: ClarificationRequest | None
    clarification_answers: tuple[ClarificationAnswer, ...]
    feedback_issues: tuple[ProtocolIssue, ...]
    output_schema_id: str
    output_schema_version: str
    output_policy: DomainOutputPolicy

    def __post_init__(self) -> None:
        if self.schema_id != DOMAIN_MODEL_REQUEST_SCHEMA_ID:
            raise ValueError("schema_id differs from the domain model request contract")
        if self.schema_version != DOMAIN_MODEL_REQUEST_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the domain model request contract")
        object.__setattr__(self, "request_id", identifier(self.request_id, context="request_id"))
        object.__setattr__(self, "session_id", identifier(self.session_id, context="session_id"))
        if not isinstance(self.output_policy, DomainOutputPolicy):
            raise TypeError("output_policy must be DomainOutputPolicy")
        turn = integer(self.turn_index, context="turn_index", minimum=1)
        attempt = integer(self.model_attempt, context="model_attempt", minimum=1)
        if attempt > self.output_policy.maximum_model_attempts:
            raise ValueError("model_attempt exceeds the repair limit")
        object.__setattr__(self, "turn_index", turn)
        object.__setattr__(self, "model_attempt", attempt)
        if not isinstance(self.capability_manifest, DomainCapabilityManifest):
            raise TypeError("capability_manifest must be DomainCapabilityManifest")
        if self.capability_manifest_ref != self.capability_manifest.ref:
            raise ValueError("capability_manifest_ref differs from the embedded manifest")
        messages = tuple(self.user_messages)
        if not messages or any(not isinstance(item, UserMessage) for item in messages):
            raise TypeError("user_messages must contain at least one UserMessage")
        message_ids = tuple(item.message_id for item in messages)
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("user message ids must be unique")
        object.__setattr__(self, "user_messages", messages)
        if self.prior_intent is not None and not isinstance(self.prior_intent, OptimizationIntent):
            raise TypeError("prior_intent must be OptimizationIntent or None")
        if self.prior_clarification is not None and not isinstance(
            self.prior_clarification, ClarificationRequest
        ):
            raise TypeError("prior_clarification must be ClarificationRequest or None")
        answers = tuple(self.clarification_answers)
        if any(not isinstance(item, ClarificationAnswer) for item in answers):
            raise TypeError("clarification_answers must contain ClarificationAnswer values")
        answer_ids = tuple(item.question_id for item in answers)
        if len(answer_ids) != len(set(answer_ids)):
            raise ValueError("clarification answer question ids must be unique")
        object.__setattr__(self, "clarification_answers", answers)
        issues = tuple(self.feedback_issues)
        if any(not isinstance(item, ProtocolIssue) for item in issues):
            raise TypeError("feedback_issues must contain ProtocolIssue values")
        object.__setattr__(self, "feedback_issues", issues)
        if turn == 1 and (
            self.prior_intent is not None or self.prior_clarification is not None or answers
        ):
            raise ValueError("the first turn must not contain prior clarification state")
        if turn > 1 and (self.prior_intent is None or self.prior_clarification is None):
            raise ValueError("later turns require the prior intent and clarification request")
        if self.prior_clarification is not None:
            assert self.prior_intent is not None
            prior_ref = ContractRef(self.prior_intent.intent_id, self.prior_intent.fingerprint)
            if self.prior_clarification.candidate_intent_ref != prior_ref:
                raise ValueError("prior clarification references another intent")
            if not answers:
                raise ValueError("later turns require clarification answers")
            _validate_answer_set(self.prior_clarification, answers)
        if attempt > 1 and not issues:
            raise ValueError("repair attempts require structured feedback issues")
        if self.output_schema_id != OPTIMIZATION_INTENT_SCHEMA_ID:
            raise ValueError("output_schema_id must be the OptimizationIntent schema")
        if self.output_schema_version != OPTIMIZATION_INTENT_SCHEMA_VERSION:
            raise ValueError("output_schema_version differs from OptimizationIntent")

    @classmethod
    def from_mapping(cls, value: object) -> DomainModelRequest:
        raw = as_mapping(value, context="domain model request")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "request_id",
                "session_id",
                "turn_index",
                "model_attempt",
                "capability_manifest",
                "capability_manifest_ref",
                "user_messages",
                "prior_intent",
                "prior_clarification",
                "clarification_answers",
                "feedback_issues",
                "output_schema_id",
                "output_schema_version",
                "output_policy",
            },
            context="domain model request",
        )
        prior_intent_raw = raw["prior_intent"]
        prior_clarification_raw = raw["prior_clarification"]
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            request_id=identifier(raw["request_id"], context="request_id"),
            session_id=identifier(raw["session_id"], context="session_id"),
            turn_index=integer(raw["turn_index"], context="turn_index", minimum=1),
            model_attempt=integer(raw["model_attempt"], context="model_attempt", minimum=1),
            capability_manifest=DomainCapabilityManifest.from_mapping(raw["capability_manifest"]),
            capability_manifest_ref=ContractRef.from_mapping(
                as_mapping(raw["capability_manifest_ref"], context="capability_manifest_ref")
            ),
            user_messages=tuple(
                UserMessage.from_mapping(item)
                for item in as_sequence(raw["user_messages"], context="user_messages")
            ),
            prior_intent=(
                None
                if prior_intent_raw is None
                else OptimizationIntent.from_mapping(prior_intent_raw)
            ),
            prior_clarification=(
                None
                if prior_clarification_raw is None
                else ClarificationRequest.from_mapping(prior_clarification_raw)
            ),
            clarification_answers=tuple(
                ClarificationAnswer.from_mapping(item)
                for item in as_sequence(
                    raw["clarification_answers"], context="clarification_answers"
                )
            ),
            feedback_issues=tuple(
                ProtocolIssue.from_mapping(item)
                for item in as_sequence(raw["feedback_issues"], context="feedback_issues")
            ),
            output_schema_id=identifier(raw["output_schema_id"], context="output_schema_id"),
            output_schema_version=identifier(
                raw["output_schema_version"], context="output_schema_version"
            ),
            output_policy=DomainOutputPolicy.from_mapping(raw["output_policy"]),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "model_attempt": self.model_attempt,
            "capability_manifest": self.capability_manifest.as_dict(),
            "capability_manifest_ref": self.capability_manifest_ref.as_dict(),
            "user_messages": [item.as_dict() for item in self.user_messages],
            "prior_intent": None if self.prior_intent is None else self.prior_intent.as_dict(),
            "prior_clarification": (
                None if self.prior_clarification is None else self.prior_clarification.as_dict()
            ),
            "clarification_answers": [item.as_dict() for item in self.clarification_answers],
            "feedback_issues": [item.as_dict() for item in self.feedback_issues],
            "output_schema_id": self.output_schema_id,
            "output_schema_version": self.output_schema_version,
            "output_policy": self.output_policy.as_dict(),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.request_id, self.fingerprint)


@dataclass(frozen=True)
class DomainModelUnsupported:
    """Closed, user-safe explanation for an explicitly unsupported business request."""

    schema_id: str
    schema_version: str
    reason_code: UnsupportedReasonCode
    safe_message: str

    def __post_init__(self) -> None:
        if self.schema_id != DOMAIN_MODEL_UNSUPPORTED_SCHEMA_ID:
            raise ValueError("schema_id differs from the unsupported response contract")
        if self.schema_version != DOMAIN_MODEL_UNSUPPORTED_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the unsupported response contract")
        if self.reason_code not in UNSUPPORTED_SAFE_MESSAGES:
            raise ValueError("unsupported reason_code is not published")
        message = text(self.safe_message, context="unsupported safe_message")
        if message != UNSUPPORTED_SAFE_MESSAGES[self.reason_code]:
            raise ValueError("safe_message differs from the published reason message")
        object.__setattr__(self, "safe_message", message)

    @classmethod
    def from_mapping(cls, value: object) -> DomainModelUnsupported:
        raw = as_mapping(value, context="domain model unsupported response")
        strict_keys(
            raw,
            required={"schema_id", "schema_version", "reason_code", "safe_message"},
            context="domain model unsupported response",
        )
        reason_code = raw["reason_code"]
        if reason_code not in UNSUPPORTED_SAFE_MESSAGES:
            raise ValueError("unsupported reason_code is not published")
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            reason_code=cast(UnsupportedReasonCode, reason_code),
            safe_message=text(raw["safe_message"], context="unsupported safe_message"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "reason_code": self.reason_code,
            "safe_message": self.safe_message,
        }


@dataclass(frozen=True)
class DomainModelResponse:
    schema_id: str
    schema_version: str
    response_id: str
    request_ref: ContractRef
    capability_manifest_ref: ContractRef
    outcome: DomainModelResponseOutcome
    intent: OptimizationIntent | None
    unsupported: DomainModelUnsupported | None

    def __post_init__(self) -> None:
        if self.schema_id != DOMAIN_MODEL_RESPONSE_SCHEMA_ID:
            raise ValueError("schema_id differs from the domain model response contract")
        if self.schema_version != DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the domain model response contract")
        object.__setattr__(self, "response_id", identifier(self.response_id, context="response_id"))
        if not isinstance(self.request_ref, ContractRef):
            raise TypeError("request_ref must be ContractRef")
        if not isinstance(self.capability_manifest_ref, ContractRef):
            raise TypeError("capability_manifest_ref must be ContractRef")
        if self.outcome == "intent":
            if not isinstance(self.intent, OptimizationIntent) or self.unsupported is not None:
                raise ValueError("intent outcome contains inconsistent variant fields")
        elif self.outcome == "unsupported":
            if self.intent is not None or not isinstance(self.unsupported, DomainModelUnsupported):
                raise ValueError("unsupported outcome contains inconsistent variant fields")
        else:
            raise ValueError("unsupported domain model response outcome")

    @classmethod
    def from_mapping(cls, value: object) -> DomainModelResponse:
        raw = as_mapping(value, context="domain model response")
        common_fields = {
            "schema_id",
            "schema_version",
            "response_id",
            "request_ref",
            "capability_manifest_ref",
            "outcome",
        }
        outcome = raw.get("outcome")
        if outcome == "intent":
            strict_keys(
                raw,
                required=common_fields | {"intent"},
                context="domain model intent response",
            )
            intent = OptimizationIntent.from_mapping(raw["intent"])
            unsupported = None
        elif outcome == "unsupported":
            strict_keys(
                raw,
                required=common_fields | {"unsupported"},
                context="domain model unsupported response",
            )
            intent = None
            unsupported = DomainModelUnsupported.from_mapping(raw["unsupported"])
        else:
            strict_keys(
                raw,
                required=common_fields,
                optional={"intent", "unsupported"},
                context="domain model response",
            )
            raise ValueError("unsupported domain model response outcome")
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            response_id=identifier(raw["response_id"], context="response_id"),
            request_ref=ContractRef.from_mapping(
                as_mapping(raw["request_ref"], context="request_ref")
            ),
            capability_manifest_ref=ContractRef.from_mapping(
                as_mapping(raw["capability_manifest_ref"], context="capability_manifest_ref")
            ),
            outcome=outcome,
            intent=intent,
            unsupported=unsupported,
        )

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "response_id": self.response_id,
            "request_ref": self.request_ref.as_dict(),
            "capability_manifest_ref": self.capability_manifest_ref.as_dict(),
            "outcome": self.outcome,
        }
        if self.outcome == "intent":
            assert self.intent is not None
            result["intent"] = self.intent.as_dict()
        else:
            assert self.unsupported is not None
            result["unsupported"] = self.unsupported.as_dict()
        return result

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.response_id, self.fingerprint)


@dataclass(frozen=True)
class CommunicationResult:
    schema_id: str
    schema_version: str
    result_id: str
    status: CommunicationStatus
    request_ref: ContractRef
    response_ref: ContractRef | None
    capability_manifest_ref: ContractRef
    candidate_intent: OptimizationIntent | None
    resolved_intent: OptimizationIntent | None
    clarification: ClarificationRequest | None
    repair: RepairDirective | None
    issues: tuple[ProtocolIssue, ...]

    def __post_init__(self) -> None:
        if self.schema_id != COMMUNICATION_RESULT_SCHEMA_ID:
            raise ValueError("schema_id differs from the communication result contract")
        if self.schema_version != COMMUNICATION_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the communication contract")
        object.__setattr__(self, "result_id", identifier(self.result_id, context="result_id"))
        if self.status not in {
            "repair_required",
            "needs_clarification",
            "resolved",
            "unsupported",
            "failed",
        }:
            raise ValueError("unsupported communication result status")
        if not isinstance(self.request_ref, ContractRef):
            raise TypeError("request_ref must be ContractRef")
        if self.response_ref is not None and not isinstance(self.response_ref, ContractRef):
            raise TypeError("response_ref must be ContractRef or None")
        if not isinstance(self.capability_manifest_ref, ContractRef):
            raise TypeError("capability_manifest_ref must be ContractRef")
        if self.candidate_intent is not None and not isinstance(
            self.candidate_intent, OptimizationIntent
        ):
            raise TypeError("candidate_intent must be OptimizationIntent or None")
        if self.resolved_intent is not None and not isinstance(
            self.resolved_intent, OptimizationIntent
        ):
            raise TypeError("resolved_intent must be OptimizationIntent or None")
        issues = tuple(self.issues)
        if any(not isinstance(item, ProtocolIssue) for item in issues):
            raise TypeError("issues must contain ProtocolIssue values")
        object.__setattr__(self, "issues", issues)
        if self.status == "resolved":
            if (
                self.response_ref is None
                or self.candidate_intent is None
                or self.resolved_intent != self.candidate_intent
                or self.clarification is not None
                or self.repair is not None
                or issues
            ):
                raise ValueError("resolved result contains inconsistent fields")
        elif self.status == "needs_clarification":
            if (
                self.response_ref is None
                or self.candidate_intent is None
                or self.resolved_intent is not None
                or not isinstance(self.clarification, ClarificationRequest)
                or self.repair is not None
                or not issues
            ):
                raise ValueError("needs_clarification result contains inconsistent fields")
            assert self.clarification is not None
            assert self.candidate_intent is not None
            expected_intent_ref = ContractRef(
                self.candidate_intent.intent_id,
                self.candidate_intent.fingerprint,
            )
            if (
                self.clarification.request_ref != self.request_ref
                or self.clarification.candidate_intent_ref != expected_intent_ref
            ):
                raise ValueError("clarification result references inconsistent objects")
        elif self.status == "unsupported":
            if (
                self.response_ref is None
                or self.resolved_intent is not None
                or self.clarification is not None
                or self.repair is not None
                or not issues
            ):
                raise ValueError("unsupported result contains inconsistent fields")
        elif self.status == "repair_required":
            if (
                self.candidate_intent is not None
                or self.resolved_intent is not None
                or self.clarification is not None
                or not isinstance(self.repair, RepairDirective)
                or not issues
            ):
                raise ValueError("repair_required result contains inconsistent fields")
            assert self.repair is not None
            if self.repair.request_ref != self.request_ref or self.repair.issues != issues:
                raise ValueError("repair result references inconsistent objects")
        elif (
            self.candidate_intent is not None
            or self.resolved_intent is not None
            or self.clarification is not None
            or self.repair is not None
            or not issues
        ):
            raise ValueError("failed result contains inconsistent fields")

    @classmethod
    def from_mapping(cls, value: object) -> CommunicationResult:
        raw = as_mapping(value, context="communication result")
        strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "result_id",
                "status",
                "request_ref",
                "response_ref",
                "capability_manifest_ref",
                "candidate_intent",
                "resolved_intent",
                "clarification",
                "repair",
                "issues",
            },
            context="communication result",
        )
        status = raw["status"]
        if status not in {
            "repair_required",
            "needs_clarification",
            "resolved",
            "unsupported",
            "failed",
        }:
            raise ValueError("unsupported communication result status")
        response_ref_raw = raw["response_ref"]
        candidate_raw = raw["candidate_intent"]
        resolved_raw = raw["resolved_intent"]
        clarification_raw = raw["clarification"]
        repair_raw = raw["repair"]
        return cls(
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            result_id=identifier(raw["result_id"], context="result_id"),
            status=status,
            request_ref=ContractRef.from_mapping(
                as_mapping(raw["request_ref"], context="request_ref")
            ),
            response_ref=(
                None
                if response_ref_raw is None
                else ContractRef.from_mapping(as_mapping(response_ref_raw, context="response_ref"))
            ),
            capability_manifest_ref=ContractRef.from_mapping(
                as_mapping(raw["capability_manifest_ref"], context="capability_manifest_ref")
            ),
            candidate_intent=(
                None if candidate_raw is None else OptimizationIntent.from_mapping(candidate_raw)
            ),
            resolved_intent=(
                None if resolved_raw is None else OptimizationIntent.from_mapping(resolved_raw)
            ),
            clarification=(
                None
                if clarification_raw is None
                else ClarificationRequest.from_mapping(clarification_raw)
            ),
            repair=(None if repair_raw is None else RepairDirective.from_mapping(repair_raw)),
            issues=tuple(
                ProtocolIssue.from_mapping(item)
                for item in as_sequence(raw["issues"], context="issues")
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "result_id": self.result_id,
            "status": self.status,
            "request_ref": self.request_ref.as_dict(),
            "response_ref": None if self.response_ref is None else self.response_ref.as_dict(),
            "capability_manifest_ref": self.capability_manifest_ref.as_dict(),
            "candidate_intent": (
                None if self.candidate_intent is None else self.candidate_intent.as_dict()
            ),
            "resolved_intent": (
                None if self.resolved_intent is None else self.resolved_intent.as_dict()
            ),
            "clarification": None if self.clarification is None else self.clarification.as_dict(),
            "repair": None if self.repair is None else self.repair.as_dict(),
            "issues": [item.as_dict() for item in self.issues],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.result_id, self.fingerprint)
