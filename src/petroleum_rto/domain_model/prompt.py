"""Deterministic, provider-neutral prompt compilation for the D0 communication contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from petroleum_rto.rto.communication import (
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DOMAIN_MODEL_UNSUPPORTED_SCHEMA_ID,
    DOMAIN_MODEL_UNSUPPORTED_SCHEMA_VERSION,
    UNSUPPORTED_SAFE_MESSAGES,
    DomainModelRequest,
)

from ._json import (
    JsonValue,
    as_mapping,
    canonical_fingerprint,
    canonical_json_bytes,
    decode_json_object,
    digest,
    freeze_json_mapping,
    identifier,
    strict_keys,
    text,
    thaw_json,
    version,
)
from .egress import MAX_REQUEST_BYTES, EgressGuard

PROMPT_ID: Final[str] = "rto-business-intent"
PROMPT_VERSION: Final[str] = "1.2.0"
RESPONSE_SCHEMA_ID: Final[str] = DOMAIN_MODEL_RESPONSE_SCHEMA_ID
RESPONSE_SCHEMA_VERSION: Final[str] = DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION

_IDENTIFIER_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_DIGEST_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
_SYSTEM_INSTRUCTION: Final[str] = (
    """你是离线RTO的业务意图结构化器。你的唯一职责是把用户表达翻译成请求内公开能力允许的完整业务意图，或用响应Schema中版本化的unsupported分支明确拒绝超出边界的请求。只可使用公开能力ID与方向；不得生成或猜测OperatingContext、进料/组成/当前设定值/初态等受信事实，不得选择求解器、算法或适配器，不得创建公式、自由阈值、系统硬门禁或控制指令。unsupported只能使用Schema列出的reason_code及其配套固定safe_message，不得自由解释。每次必须返回完整替代响应，不能返回JSON Patch。只返回一个符合给定JSON Schema的JSON对象：不得使用Markdown代码围栏，不得添加解释、思维过程、分析、推理轨迹或额外字段。request_ref和capability_manifest_ref必须逐字复制请求中的对应引用。"""
)


def _contract_ref_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["object_id", "fingerprint"],
        "properties": {
            "object_id": {"type": "string", "pattern": _IDENTIFIER_PATTERN},
            "fingerprint": {"type": "string", "pattern": _DIGEST_PATTERN},
        },
    }


def _response_schema() -> dict[str, object]:
    identifier = {"type": "string", "pattern": _IDENTIFIER_PATTERN}
    intent_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_id",
            "schema_version",
            "intent_id",
            "objectives",
            "decision_variables",
            "constraints",
            "preference",
            "result_request",
            "ambiguities",
        ],
        "properties": {
            "schema_id": {"const": "optimization-intent"},
            "schema_version": {"const": "1.0.0"},
            "intent_id": identifier,
            "objectives": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["metric_id", "sense", "priority"],
                    "properties": {
                        "metric_id": identifier,
                        "sense": {"enum": ["minimize", "maximize"]},
                        "priority": {"type": "integer", "minimum": 1},
                    },
                },
            },
            "decision_variables": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": identifier,
            },
            "constraints": {
                "type": "array",
                "uniqueItems": True,
                "items": identifier,
            },
            "preference": {
                "type": "object",
                "additionalProperties": False,
                "required": ["method", "objective_order"],
                "properties": {
                    "method": identifier,
                    "objective_order": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": identifier,
                    },
                },
            },
            "result_request": {
                "type": "object",
                "additionalProperties": False,
                "required": ["output_kind", "include_alternatives", "max_candidates"],
                "properties": {
                    "output_kind": identifier,
                    "include_alternatives": {"type": "boolean"},
                    "max_candidates": {"type": "integer", "minimum": 1},
                },
            },
            "ambiguities": {
                "type": "array",
                "uniqueItems": True,
                "items": identifier,
            },
        },
    }
    common_properties: dict[str, object] = {
        "schema_id": {"const": DOMAIN_MODEL_RESPONSE_SCHEMA_ID},
        "schema_version": {"const": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION},
        "response_id": identifier,
        "request_ref": _contract_ref_schema(),
        "capability_manifest_ref": _contract_ref_schema(),
    }
    unsupported_variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_id", "schema_version", "reason_code", "safe_message"],
            "properties": {
                "schema_id": {"const": DOMAIN_MODEL_UNSUPPORTED_SCHEMA_ID},
                "schema_version": {"const": DOMAIN_MODEL_UNSUPPORTED_SCHEMA_VERSION},
                "reason_code": {"const": reason_code},
                "safe_message": {"const": safe_message},
            },
        }
        for reason_code, safe_message in UNSUPPORTED_SAFE_MESSAGES.items()
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RESPONSE_SCHEMA_ID,
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "schema_id",
                    "schema_version",
                    "response_id",
                    "request_ref",
                    "capability_manifest_ref",
                    "outcome",
                    "intent",
                ],
                "properties": {
                    **common_properties,
                    "outcome": {"const": "intent"},
                    "intent": intent_schema,
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "schema_id",
                    "schema_version",
                    "response_id",
                    "request_ref",
                    "capability_manifest_ref",
                    "outcome",
                    "unsupported",
                ],
                "properties": {
                    **common_properties,
                    "outcome": {"const": "unsupported"},
                    "unsupported": {"oneOf": unsupported_variants},
                },
            },
        ],
    }


_RESPONSE_SCHEMA: Final[Mapping[str, JsonValue]] = freeze_json_mapping(
    _response_schema(), context="domain-model response schema"
)
_PROMPT_FINGERPRINT: Final[str] = canonical_fingerprint(
    {
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "system_instruction": _SYSTEM_INSTRUCTION,
    }
)
_SCHEMA_FINGERPRINT: Final[str] = canonical_fingerprint(_RESPONSE_SCHEMA)

_EGRESS_REQUEST_FIELDS: Final[set[str]] = {
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
}


def _validated_ref(value: object, *, context: str) -> tuple[str, str]:
    raw = as_mapping(value, context=context)
    strict_keys(raw, required={"object_id", "fingerprint"}, context=context)
    return (
        identifier(raw["object_id"], context=f"{context} object_id"),
        digest(raw["fingerprint"], context=f"{context} fingerprint"),
    )


def _validate_compiler_input(
    value: Mapping[str, object],
    *,
    request_fingerprint: str,
) -> None:
    strict_keys(
        value,
        required={
            "egress_request",
            "required_response_binding",
            "response_schema",
            "response_schema_fingerprint",
        },
        context="compiled prompt input",
    )
    if canonical_fingerprint(value["response_schema"]) != _SCHEMA_FINGERPRINT:
        raise ValueError("compiled prompt embeds an unsupported response schema")
    if (
        digest(
            value["response_schema_fingerprint"],
            context="embedded response_schema_fingerprint",
        )
        != _SCHEMA_FINGERPRINT
    ):
        raise ValueError("compiled prompt response schema fingerprint is not supported")

    binding = as_mapping(
        value["required_response_binding"],
        context="required response binding",
    )
    strict_keys(
        binding,
        required={
            "schema_id",
            "schema_version",
            "request_ref",
            "capability_manifest_ref",
        },
        context="required response binding",
    )
    if binding["schema_id"] != DOMAIN_MODEL_RESPONSE_SCHEMA_ID:
        raise ValueError("required response binding has an unsupported schema_id")
    if binding["schema_version"] != RESPONSE_SCHEMA_VERSION:
        raise ValueError("required response binding has an unsupported schema_version")
    request_ref = _validated_ref(binding["request_ref"], context="bound request_ref")
    if request_ref[1] != request_fingerprint:
        raise ValueError("bound request_ref differs from request_fingerprint")

    egress_request = as_mapping(value["egress_request"], context="egress request")
    strict_keys(
        egress_request,
        required=_EGRESS_REQUEST_FIELDS,
        context="egress request",
    )
    if identifier(egress_request["request_id"], context="egress request_id") != request_ref[0]:
        raise ValueError("egress request_id differs from bound request_ref")
    if canonical_fingerprint(egress_request) != request_fingerprint:
        raise ValueError("egress request differs from request_fingerprint")
    embedded_manifest_ref = _validated_ref(
        egress_request["capability_manifest_ref"],
        context="egress capability_manifest_ref",
    )
    bound_manifest_ref = _validated_ref(
        binding["capability_manifest_ref"],
        context="bound capability_manifest_ref",
    )
    if embedded_manifest_ref != bound_manifest_ref:
        raise ValueError("egress capability manifest reference differs from response binding")
    projected_manifest = as_mapping(
        egress_request["capability_manifest"],
        context="egress capability manifest",
    )
    strict_keys(
        projected_manifest,
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
        context="egress capability manifest",
    )
    if projected_manifest["manifest_id"] != embedded_manifest_ref[0]:
        raise ValueError("egress capability manifest id differs from its bound reference")


@dataclass(frozen=True)
class CompiledPrompt:
    """Provider-neutral prompt material plus immutable version/fingerprint evidence."""

    prompt_id: str
    prompt_version: str
    prompt_fingerprint: str
    schema_id: str
    schema_version: str
    schema_fingerprint: str
    request_fingerprint: str
    input_fingerprint: str
    system_instruction: str
    input_json: str
    response_schema: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if identifier(self.prompt_id, context="prompt_id") != PROMPT_ID:
            raise ValueError("prompt_id differs from the supported prompt")
        if version(self.prompt_version, context="prompt_version") != PROMPT_VERSION:
            raise ValueError("prompt_version differs from the supported prompt")
        if digest(self.prompt_fingerprint, context="prompt_fingerprint") != _PROMPT_FINGERPRINT:
            raise ValueError("prompt_fingerprint differs from the supported prompt")
        if identifier(self.schema_id, context="schema_id") != RESPONSE_SCHEMA_ID:
            raise ValueError("schema_id differs from the supported response schema")
        if version(self.schema_version, context="schema_version") != RESPONSE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the supported response schema")
        if digest(self.schema_fingerprint, context="schema_fingerprint") != _SCHEMA_FINGERPRINT:
            raise ValueError("schema_fingerprint differs from the supported response schema")
        digest(self.request_fingerprint, context="request_fingerprint")
        digest(self.input_fingerprint, context="input_fingerprint")
        if text(self.system_instruction, context="system_instruction") != _SYSTEM_INSTRUCTION:
            raise ValueError("system_instruction differs from the versioned prompt")
        object.__setattr__(
            self,
            "response_schema",
            freeze_json_mapping(self.response_schema, context="compiled response schema"),
        )
        if self.response_schema != _RESPONSE_SCHEMA:
            raise ValueError("response_schema differs from the supported response schema")
        input_payload = decode_json_object(
            self.input_json,
            context="compiled prompt input",
            maximum_bytes=MAX_REQUEST_BYTES,
        )
        if canonical_fingerprint(input_payload) != self.input_fingerprint:
            raise ValueError("compiled input differs from input_fingerprint")
        _validate_compiler_input(
            input_payload,
            request_fingerprint=self.request_fingerprint,
        )
        # The instruction is trusted, version-pinned material. Only the
        # compiler input contains user-controlled content and receives the
        # semantic outbound scan.
        EgressGuard().inspect_request(input_payload)

    @classmethod
    def from_mapping(cls, value: object) -> CompiledPrompt:
        raw = as_mapping(value, context="compiled prompt")
        strict_keys(
            raw,
            required={
                "prompt_id",
                "prompt_version",
                "prompt_fingerprint",
                "schema_id",
                "schema_version",
                "schema_fingerprint",
                "request_fingerprint",
                "input_fingerprint",
                "system_instruction",
                "input_json",
                "response_schema",
            },
            context="compiled prompt",
        )
        return cls(
            prompt_id=identifier(raw["prompt_id"], context="prompt_id"),
            prompt_version=version(raw["prompt_version"], context="prompt_version"),
            prompt_fingerprint=digest(raw["prompt_fingerprint"], context="prompt_fingerprint"),
            schema_id=identifier(raw["schema_id"], context="schema_id"),
            schema_version=version(raw["schema_version"], context="schema_version"),
            schema_fingerprint=digest(raw["schema_fingerprint"], context="schema_fingerprint"),
            request_fingerprint=digest(raw["request_fingerprint"], context="request_fingerprint"),
            input_fingerprint=digest(raw["input_fingerprint"], context="input_fingerprint"),
            system_instruction=text(raw["system_instruction"], context="system_instruction"),
            input_json=text(raw["input_json"], context="input_json"),
            response_schema=freeze_json_mapping(raw["response_schema"], context="response_schema"),
        )

    @property
    def system_prompt(self) -> str:
        return self.system_instruction

    @property
    def user_prompt(self) -> str:
        return self.input_json

    @property
    def json_schema(self) -> Mapping[str, JsonValue]:
        return self.response_schema

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_fingerprint": self.prompt_fingerprint,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "schema_fingerprint": self.schema_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "system_instruction": self.system_instruction,
            "input_json": self.input_json,
            "response_schema": thaw_json(self.response_schema),
        }


class PromptCompiler:
    """Compile a strict D0 request without provider-specific wire formatting."""

    def __init__(self, *, egress_guard: EgressGuard | None = None) -> None:
        self._egress_guard = egress_guard or EgressGuard()

    def compile(self, request: DomainModelRequest | Mapping[str, object]) -> CompiledPrompt:
        normalized = (
            request
            if isinstance(request, DomainModelRequest)
            else DomainModelRequest.from_mapping(request)
        )
        for message in normalized.user_messages:
            self._egress_guard.inspect_text(message.text, context="user message")
        request_payload = normalized.as_dict()
        compiler_input = {
            "egress_request": request_payload,
            "required_response_binding": {
                "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
                "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
                "request_ref": normalized.ref.as_dict(),
                "capability_manifest_ref": normalized.capability_manifest_ref.as_dict(),
            },
            "response_schema": thaw_json(_RESPONSE_SCHEMA),
            "response_schema_fingerprint": _SCHEMA_FINGERPRINT,
        }
        input_json = canonical_json_bytes(compiler_input).decode("utf-8")
        return CompiledPrompt(
            prompt_id=PROMPT_ID,
            prompt_version=PROMPT_VERSION,
            prompt_fingerprint=_PROMPT_FINGERPRINT,
            schema_id=RESPONSE_SCHEMA_ID,
            schema_version=RESPONSE_SCHEMA_VERSION,
            schema_fingerprint=_SCHEMA_FINGERPRINT,
            request_fingerprint=normalized.fingerprint,
            input_fingerprint=canonical_fingerprint(compiler_input),
            system_instruction=_SYSTEM_INSTRUCTION,
            input_json=input_json,
            response_schema=_RESPONSE_SCHEMA,
        )
