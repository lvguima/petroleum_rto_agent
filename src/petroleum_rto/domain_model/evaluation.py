"""Strict natural-language evaluation contracts for replaceable domain models."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal, cast

from petroleum_rto.rto.communication import CommunicationResult, OptimizationIntent

type ExpectedStatus = Literal[
    "resolved",
    "needs_clarification",
    "unsupported",
    "not_resolved",
    "egress_blocked",
]

_MAX_EVALUATION_BYTES = 1_000_000
_PACKAGED_EVALUATION_FILE = "natural_language_intent_v1.json"


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be a JSON object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, context: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{context} must be a JSON array")
    return cast(Sequence[object], value)


def _strict_keys(
    raw: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
    context: str,
) -> None:
    missing = required - set(raw)
    extra = set(raw) - required - optional
    if missing or extra:
        raise ValueError(
            f"{context} fields differ: missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{context} must be a non-empty string")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"evaluation JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"evaluation JSON contains non-finite constant {value!r}")


@dataclass(frozen=True)
class IntentSemanticTemplate:
    """Identifiers and ordering that determine one gold intent's business semantics."""

    objectives: tuple[tuple[str, str, int], ...]
    decision_variables: tuple[str, ...]
    constraints: tuple[str, ...]
    preference_method: str
    objective_order: tuple[str, ...]
    output_kind: str
    include_alternatives: bool
    max_candidates: int

    @classmethod
    def from_mapping(cls, value: object) -> IntentSemanticTemplate:
        raw = _mapping(value, context="intent template")
        _strict_keys(
            raw,
            required={
                "objectives",
                "decision_variables",
                "constraints",
                "preference_method",
                "objective_order",
                "output_kind",
                "include_alternatives",
                "max_candidates",
            },
            context="intent template",
        )
        objectives: list[tuple[str, str, int]] = []
        for index, item in enumerate(_sequence(raw["objectives"], context="template objectives")):
            row = _sequence(item, context=f"template objective {index}")
            if len(row) != 3:
                raise ValueError("template objective must contain metric, sense and priority")
            metric = _text(row[0], context="template metric")
            sense = _text(row[1], context="template sense")
            priority = row[2]
            if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
                raise TypeError("template objective priority must be a positive integer")
            objectives.append((metric, sense, priority))
        decisions = tuple(
            _text(item, context="template decision variable")
            for item in _sequence(raw["decision_variables"], context="template decision variables")
        )
        constraints = tuple(
            _text(item, context="template constraint")
            for item in _sequence(raw["constraints"], context="template constraints")
        )
        objective_order = tuple(
            _text(item, context="template objective order")
            for item in _sequence(raw["objective_order"], context="template objective order")
        )
        include_alternatives = raw["include_alternatives"]
        if not isinstance(include_alternatives, bool):
            raise TypeError("template include_alternatives must be boolean")
        max_candidates = raw["max_candidates"]
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or max_candidates < 1
        ):
            raise TypeError("template max_candidates must be a positive integer")
        if not objectives or not decisions:
            raise ValueError("intent template requires objectives and decision variables")
        if objective_order != tuple(item[0] for item in objectives):
            raise ValueError("template objective_order must match objective priority order")
        return cls(
            objectives=tuple(objectives),
            decision_variables=decisions,
            constraints=constraints,
            preference_method=_text(raw["preference_method"], context="template preference method"),
            objective_order=objective_order,
            output_kind=_text(raw["output_kind"], context="template output kind"),
            include_alternatives=include_alternatives,
            max_candidates=max_candidates,
        )

    @classmethod
    def from_intent(cls, intent: OptimizationIntent) -> IntentSemanticTemplate:
        return cls(
            objectives=tuple(
                (item.metric_id, item.sense, item.priority) for item in intent.objectives
            ),
            decision_variables=intent.decision_variables,
            constraints=intent.constraints,
            preference_method=intent.preference.method,
            objective_order=intent.preference.objective_order,
            output_kind=intent.result_request.output_kind,
            include_alternatives=intent.result_request.include_alternatives,
            max_candidates=intent.result_request.max_candidates,
        )


@dataclass(frozen=True)
class EvaluationExpectation:
    status: ExpectedStatus
    template_id: str | None = None
    ambiguity_codes: tuple[str, ...] = ()
    reason_code: str | None = None
    error_code: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> EvaluationExpectation:
        raw = _mapping(value, context="evaluation expectation")
        _strict_keys(
            raw,
            required={"status"},
            optional={"template_id", "ambiguity_codes", "reason_code", "error_code"},
            context="evaluation expectation",
        )
        status = _text(raw["status"], context="expected status")
        if status not in {
            "resolved",
            "needs_clarification",
            "unsupported",
            "not_resolved",
            "egress_blocked",
        }:
            raise ValueError("evaluation expectation has unsupported status")
        template_id = raw.get("template_id")
        if template_id is not None:
            template_id = _text(template_id, context="expected template_id")
        ambiguities = tuple(
            _text(item, context="expected ambiguity code")
            for item in _sequence(
                raw.get("ambiguity_codes", ()), context="expected ambiguity codes"
            )
        )
        reason_code = raw.get("reason_code")
        if reason_code is not None:
            reason_code = _text(reason_code, context="expected unsupported reason_code")
        error_code = raw.get("error_code")
        if error_code is not None:
            error_code = _text(error_code, context="expected egress error_code")
        if status == "resolved" and template_id is None:
            raise ValueError("resolved expectation requires template_id")
        if status != "resolved" and template_id is not None:
            raise ValueError("only resolved expectation may reference a template")
        if status == "needs_clarification" and not ambiguities:
            raise ValueError("clarification expectation requires ambiguity codes")
        if status != "needs_clarification" and ambiguities:
            raise ValueError("only clarification expectation may contain ambiguity codes")
        if (status == "unsupported") != (reason_code is not None):
            raise ValueError("unsupported expectation requires exactly one reason_code")
        if (status == "egress_blocked") != (error_code is not None):
            raise ValueError("egress expectation requires exactly one error_code")
        return cls(
            status=cast(ExpectedStatus, status),
            template_id=template_id,
            ambiguity_codes=ambiguities,
            reason_code=reason_code,
            error_code=error_code,
        )


@dataclass(frozen=True)
class NaturalLanguageEvaluationCase:
    case_id: str
    user_text: str
    expected: EvaluationExpectation
    critical: bool
    tags: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> NaturalLanguageEvaluationCase:
        raw = _mapping(value, context="evaluation case")
        _strict_keys(
            raw,
            required={"case_id", "user_text", "expected", "critical", "tags"},
            context="evaluation case",
        )
        critical = raw["critical"]
        if not isinstance(critical, bool):
            raise TypeError("evaluation critical flag must be boolean")
        tags = tuple(
            _text(item, context="evaluation tag")
            for item in _sequence(raw["tags"], context="evaluation tags")
        )
        if not tags:
            raise ValueError("evaluation case requires at least one tag")
        return cls(
            case_id=_text(raw["case_id"], context="evaluation case_id"),
            user_text=_text(raw["user_text"], context="evaluation user_text"),
            expected=EvaluationExpectation.from_mapping(raw["expected"]),
            critical=critical,
            tags=tags,
        )


@dataclass(frozen=True)
class NaturalLanguageEvaluationSuite:
    schema_id: str
    schema_version: str
    suite_id: str
    claim_scope: str
    intent_templates: Mapping[str, IntentSemanticTemplate]
    cases: tuple[NaturalLanguageEvaluationCase, ...]

    @classmethod
    def from_mapping(cls, value: object) -> NaturalLanguageEvaluationSuite:
        raw = _mapping(value, context="evaluation suite")
        _strict_keys(
            raw,
            required={
                "schema_id",
                "schema_version",
                "suite_id",
                "claim_scope",
                "intent_templates",
                "cases",
            },
            context="evaluation suite",
        )
        if raw["schema_id"] != "domain-model-natural-language-eval-suite":
            raise ValueError("evaluation suite schema_id is unsupported")
        if raw["schema_version"] != "1.1.0":
            raise ValueError("evaluation suite schema_version is unsupported")
        templates_raw = _mapping(raw["intent_templates"], context="intent templates")
        templates = {
            _text(key, context="intent template id"): IntentSemanticTemplate.from_mapping(item)
            for key, item in templates_raw.items()
        }
        cases = tuple(
            NaturalLanguageEvaluationCase.from_mapping(item)
            for item in _sequence(raw["cases"], context="evaluation cases")
        )
        ids = tuple(item.case_id for item in cases)
        if len(cases) < 50:
            raise ValueError("evaluation suite must contain at least 50 cases")
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")
        for item in cases:
            template_id = item.expected.template_id
            if template_id is not None and template_id not in templates:
                raise ValueError(f"evaluation case references unknown template {template_id!r}")
        return cls(
            schema_id="domain-model-natural-language-eval-suite",
            schema_version="1.1.0",
            suite_id=_text(raw["suite_id"], context="evaluation suite_id"),
            claim_scope=_text(raw["claim_scope"], context="evaluation claim_scope"),
            intent_templates=templates,
            cases=cases,
        )

    def evaluate(
        self,
        case: NaturalLanguageEvaluationCase,
        result: CommunicationResult,
    ) -> tuple[str, ...]:
        """Return deterministic mismatch codes; an empty tuple means the case passed."""

        expected = case.expected
        if expected.status == "egress_blocked":
            return ("expected-local-egress-block",)
        if expected.status == "not_resolved":
            return () if result.status != "resolved" else ("forbidden-resolved",)
        if result.status != expected.status:
            return (f"status:{result.status}!={expected.status}",)
        if expected.status == "resolved":
            assert expected.template_id is not None
            if result.resolved_intent is None:
                return ("resolved-intent-missing",)
            actual = IntentSemanticTemplate.from_intent(result.resolved_intent)
            required = self.intent_templates[expected.template_id]
            return () if actual == required else ("intent-semantics-mismatch",)
        if expected.status == "needs_clarification":
            if result.candidate_intent is None:
                return ("candidate-intent-missing",)
            actual_codes = tuple(result.candidate_intent.ambiguities)
            return () if actual_codes == expected.ambiguity_codes else ("ambiguity-codes-mismatch",)
        if expected.status == "unsupported":
            assert expected.reason_code is not None
            actual_codes = tuple(item.code for item in result.issues)
            return (
                ()
                if actual_codes == (expected.reason_code,)
                else ("unsupported-reason-code-mismatch",)
            )
        return ()


def load_evaluation_suite(path: Path) -> NaturalLanguageEvaluationSuite:
    """Load one bounded strict UTF-8 JSON evaluation suite."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError("evaluation suite must be an existing JSON file")
    size = resolved.stat().st_size
    if size <= 0 or size > _MAX_EVALUATION_BYTES:
        raise ValueError("evaluation suite size must be between 1 byte and 1 MB")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise ValueError("evaluation suite must be readable") from exc
    return load_evaluation_suite_bytes(payload)


def load_evaluation_suite_bytes(payload: bytes) -> NaturalLanguageEvaluationSuite:
    """Parse the exact bytes later hashed by an evaluation report."""

    if not isinstance(payload, bytes):
        raise TypeError("evaluation suite payload must be bytes")
    return _decode_evaluation_suite(payload)


def packaged_evaluation_suite_bytes() -> bytes:
    """Return exact packaged gold-suite bytes without depending on the checkout."""

    resource = resources.files("petroleum_rto.domain_model.data").joinpath(
        _PACKAGED_EVALUATION_FILE
    )
    try:
        return resource.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("packaged domain-model evaluation suite is missing") from exc


def load_packaged_evaluation_suite(
    repo_root: Path | None = None,
) -> NaturalLanguageEvaluationSuite:
    """Load the package suite and optionally prove checkout bytes are identical."""

    packaged = packaged_evaluation_suite_bytes()
    if repo_root is not None:
        source = (
            Path(repo_root).resolve() / "data" / "domain_model" / "gold" / _PACKAGED_EVALUATION_FILE
        )
        try:
            checkout = source.read_bytes()
        except OSError as exc:
            raise ValueError("checkout domain-model evaluation suite is missing") from exc
        if checkout != packaged:
            raise ValueError("checkout evaluation suite differs byte-for-byte from package data")
    return _decode_evaluation_suite(packaged)


def _decode_evaluation_suite(payload: bytes) -> NaturalLanguageEvaluationSuite:
    if not 0 < len(payload) <= _MAX_EVALUATION_BYTES:
        raise ValueError("evaluation suite size must be between 1 byte and 1 MB")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("evaluation suite must be valid UTF-8 JSON") from exc
    return NaturalLanguageEvaluationSuite.from_mapping(value)
