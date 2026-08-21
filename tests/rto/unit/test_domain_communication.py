from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from petroleum_rto.rto.capabilities import (
    BundleCapabilityView,
    PublicCapabilityManifest,
    build_public_capability_manifest,
    load_capability_bundle,
)
from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    ClarificationAnswer,
    CommunicationResult,
    DomainModelInvocationResult,
    DomainModelPort,
    DomainModelRequest,
    DomainModelResponse,
    IntentCommunicationPolicy,
    IntentCommunicationService,
    ProviderAttempt,
    ProviderError,
    ProviderUsage,
)
from petroleum_rto.rto.communication.models import (
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DOMAIN_MODEL_UNSUPPORTED_SCHEMA_ID,
    DOMAIN_MODEL_UNSUPPORTED_SCHEMA_VERSION,
    UNSUPPORTED_SAFE_MESSAGES,
)
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.intent import OptimizationIntent
from petroleum_rto.rto.runtime import build_intent_communication_service


def _raw_intent(
    *,
    multi: bool = False,
    decisions: list[str] | None = None,
    constraints: list[str] | None = None,
    ambiguities: list[str] | None = None,
) -> dict[str, Any]:
    objectives: list[dict[str, object]] = [
        {
            "metric_id": "specific_furnace_fuel_energy_mj_per_t",
            "sense": "minimize",
            "priority": 1,
        }
    ]
    if multi:
        objectives = [
            {
                "metric_id": "quality_proxy_max_abs_relative_change",
                "sense": "minimize",
                "priority": 1,
            },
            {
                "metric_id": "valuable_distillate_yield",
                "sense": "maximize",
                "priority": 2,
            },
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "sense": "minimize",
                "priority": 3,
            },
        ]
    objective_order = [str(item["metric_id"]) for item in objectives]
    return {
        "schema_id": "optimization-intent",
        "schema_version": "1.0.0",
        "intent_id": "domain-multi" if multi else "domain-single",
        "objectives": objectives,
        "decision_variables": decisions
        or ["furnace_temperature_target_k", "tower_top_pressure_target_pa_a"],
        "constraints": constraints or [],
        "preference": {
            "method": "lexicographic" if multi else "single-objective",
            "objective_order": objective_order,
        },
        "result_request": {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": multi,
            "max_candidates": 5 if multi else 1,
        },
        "ambiguities": ambiguities or [],
    }


def _service(repo_root: Path) -> IntentCommunicationService:
    return IntentCommunicationService.from_bundle(load_capability_bundle(repo_root))


def _response(
    request: DomainModelRequest,
    intent: OptimizationIntent | dict[str, Any],
    *,
    request_ref: ContractRef | None = None,
    capability_ref: ContractRef | None = None,
) -> dict[str, object]:
    return {
        "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
        "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
        "response_id": f"response-{request.turn_index}-{request.model_attempt}",
        "request_ref": (request_ref or request.ref).as_dict(),
        "capability_manifest_ref": (capability_ref or request.capability_manifest_ref).as_dict(),
        "outcome": "intent",
        "intent": intent.as_dict() if isinstance(intent, OptimizationIntent) else intent,
    }


def _unsupported_response(
    request: DomainModelRequest,
    *,
    reason_code: str = "solver-selection-forbidden",
    safe_message: str | None = None,
    request_ref: ContractRef | None = None,
) -> dict[str, object]:
    return {
        "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
        "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
        "response_id": f"response-{request.turn_index}-{request.model_attempt}",
        "request_ref": (request_ref or request.ref).as_dict(),
        "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
        "outcome": "unsupported",
        "unsupported": {
            "schema_id": DOMAIN_MODEL_UNSUPPORTED_SCHEMA_ID,
            "schema_version": DOMAIN_MODEL_UNSUPPORTED_SCHEMA_VERSION,
            "reason_code": reason_code,
            "safe_message": (
                UNSUPPORTED_SAFE_MESSAGES["solver-selection-forbidden"]
                if safe_message is None
                else safe_message
            ),
        },
    }


def _successful_invocation(
    request: DomainModelRequest,
    response: dict[str, object],
) -> DomainModelInvocationResult:
    return DomainModelInvocationResult(
        schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
        schema_version=COMMUNICATION_SCHEMA_VERSION,
        invocation_id=f"invoke-{request.request_id}",
        request_ref=request.ref,
        status="succeeded",
        attempts=(
            ProviderAttempt(
                attempt_index=1,
                provider_id="fake-provider",
                provider_version="2026-08-20",
                status="succeeded",
                provider_request_id="provider-request-1",
                served_model="fake-model-v1",
                finish_reason="stop",
                duration_ms=12,
                usage=ProviderUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
                error=None,
            ),
        ),
        response=json.dumps(response, ensure_ascii=False),
        error=None,
    )


def test_public_manifest_round_trips_and_exposes_stable_ref(repo_root: Path) -> None:
    manifest = build_public_capability_manifest(load_capability_bundle(repo_root))

    restored = PublicCapabilityManifest.from_mapping(manifest.as_dict())

    assert restored == manifest
    assert restored.ref == ContractRef(manifest.manifest_id, manifest.fingerprint)
    with pytest.raises(ValueError, match="fields differ"):
        PublicCapabilityManifest.from_mapping({**manifest.as_dict(), "internal_path": "x"})


def test_intent_communication_policy_is_versioned_strict_and_injectable(
    repo_root: Path,
) -> None:
    default = IntentCommunicationPolicy()

    assert IntentCommunicationPolicy.from_mapping(default.as_dict()) == default
    assert default.maximum_model_attempts == 2
    assert default.maximum_clarification_turns == 3
    assert default.maximum_questions_per_turn == 3
    assert set(default.allowed_ambiguity_codes) == {
        "objective-selection-ambiguous",
        "objective-priority-ambiguous",
        "decision-variable-selection-ambiguous",
        "result-alternatives-ambiguous",
    }
    with pytest.raises(ValueError, match="fields differ"):
        IntentCommunicationPolicy.from_mapping({**default.as_dict(), "model_id": "forbidden"})
    with pytest.raises(ValueError, match="unsupported ambiguity"):
        IntentCommunicationPolicy(allowed_ambiguity_codes=("model-invented-question",))

    with pytest.raises(ValueError, match="safety ceiling"):
        replace(default, maximum_model_attempts=3)
    with pytest.raises(ValueError, match="safety ceiling"):
        replace(default, maximum_clarification_turns=4)
    with pytest.raises(ValueError, match="safety ceiling"):
        replace(default, maximum_questions_per_turn=4)

    custom = replace(default, maximum_questions_per_turn=2)
    service = IntentCommunicationService.from_bundle(
        load_capability_bundle(repo_root),
        policy=custom,
    )
    request = service.start(
        session_id="session-policy",
        message_id="user-1",
        user_text="降低能耗。",
    )
    assert service.policy == custom
    assert request.output_policy.maximum_model_attempts == 2
    assert service.policy.maximum_questions_per_turn == 2


def test_supplier_neutral_invocation_contract_round_trips_and_satisfies_port(
    repo_root: Path,
) -> None:
    request = _service(repo_root).start(
        session_id="session-invocation",
        message_id="user-1",
        user_text="降低能耗。",
    )
    response = _response(request, OptimizationIntent.from_mapping(_raw_intent()))
    invocation = _successful_invocation(request, response)

    class FakeProvider:
        @property
        def provider_id(self) -> str:
            return "fake-provider"

        @property
        def provider_version(self) -> str:
            return "2026-08-20"

        def invoke(self, model_request: DomainModelRequest) -> DomainModelInvocationResult:
            assert model_request == request
            return invocation

    port: DomainModelPort = FakeProvider()

    assert port.invoke(request) == invocation
    assert DomainModelInvocationResult.from_mapping(invocation.as_dict()) == invocation
    assert json.loads(str(invocation.as_dict()["response"])) == response
    payload = invocation.as_dict()
    payload["provider_secret"] = "must-not-be-accepted"
    with pytest.raises(ValueError, match="fields differ"):
        DomainModelInvocationResult.from_mapping(payload)


def test_invocation_failure_is_disjoint_from_success_and_preserves_attempts(
    repo_root: Path,
) -> None:
    request = _service(repo_root).start(
        session_id="session-provider-failure",
        message_id="user-1",
        user_text="降低能耗。",
    )
    timeout = ProviderError(
        category="provider_server",
        code="deadline-exceeded",
        message="provider deadline exceeded",
        retryable=True,
        http_status=504,
    )
    terminal = ProviderError(
        category="authentication",
        code="invalid-api-key",
        message="provider authentication failed",
        retryable=False,
        http_status=401,
    )
    attempts = (
        ProviderAttempt(
            attempt_index=1,
            provider_id="fake-provider",
            provider_version="2026-08-20",
            status="failed",
            provider_request_id=None,
            served_model=None,
            finish_reason=None,
            duration_ms=100,
            usage=ProviderUsage(input_tokens=None, output_tokens=None, total_tokens=None),
            error=timeout,
        ),
        ProviderAttempt(
            attempt_index=2,
            provider_id="fake-provider",
            provider_version="2026-08-20",
            status="failed",
            provider_request_id="provider-request-2",
            served_model=None,
            finish_reason=None,
            duration_ms=20,
            usage=None,
            error=terminal,
        ),
    )
    failed = DomainModelInvocationResult(
        schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
        schema_version=COMMUNICATION_SCHEMA_VERSION,
        invocation_id="invoke-failed",
        request_ref=request.ref,
        status="failed",
        attempts=attempts,
        response=None,
        error=terminal,
    )

    assert DomainModelInvocationResult.from_mapping(failed.as_dict()) == failed
    with pytest.raises(ValueError, match="must not contain a response"):
        replace(failed, response="{}")
    with pytest.raises(ValueError, match="final attempt error"):
        replace(failed, error=timeout)
    with pytest.raises(ValueError, match="requires served_model and finish_reason"):
        replace(attempts[-1], status="succeeded", error=None)

    with pytest.raises(ValueError, match="not retryable"):
        ProviderError(
            category="authentication",
            code="invalid-api-key",
            message="provider authentication failed",
            retryable=True,
            http_status=401,
        )
    with pytest.raises(ValueError, match="differs from its HTTP status"):
        ProviderError(
            category="authentication",
            code="wrong-status",
            message="provider failure",
            retryable=False,
            http_status=500,
        )


def test_preflight_invocation_failure_has_no_physical_attempt(repo_root: Path) -> None:
    request = _service(repo_root).start(
        session_id="session-preflight-failure",
        message_id="user-1",
        user_text="降低能耗。",
    )
    error = ProviderError(
        category="authentication",
        code="credential-missing-or-invalid",
        message="provider credential is unavailable",
        retryable=False,
        http_status=None,
    )
    failed = DomainModelInvocationResult(
        schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
        schema_version=COMMUNICATION_SCHEMA_VERSION,
        invocation_id="preflight-invocation",
        request_ref=request.ref,
        status="failed",
        attempts=(),
        response=None,
        error=error,
    )

    assert DomainModelInvocationResult.from_mapping(failed.as_dict()) == failed
    with pytest.raises(ValueError, match="must not contain a response"):
        replace(failed, response="{}")


def test_deeply_nested_model_json_becomes_safe_repair(repo_root: Path) -> None:
    request = _service(repo_root).start(
        session_id="session-deep-json",
        message_id="user-1",
        user_text="降低能耗。",
    )
    deeply_nested = "[" * 20_000 + "0" + "]" * 20_000

    result = _service(repo_root).evaluate_response(request, deeply_nested)

    assert result.status == "repair_required"
    assert result.issues[0].code == "invalid-model-response"
    assert deeply_nested not in result.issues[0].message


def test_runtime_factory_hides_internal_bundle_from_future_domain_module(
    repo_root: Path,
) -> None:
    service = build_intent_communication_service(repo_root=repo_root)

    request = service.start(
        session_id="session-runtime-factory",
        message_id="user-1",
        user_text="降低能耗。",
    )

    assert isinstance(service, IntentCommunicationService)
    assert request.capability_manifest_ref == service.capability_manifest.ref


def test_first_request_is_self_contained_but_contains_no_operating_context(repo_root: Path) -> None:
    service = _service(repo_root)

    request = service.start(
        session_id="session-energy",
        message_id="user-1",
        user_text="在当前工况下降低单位进料炉燃料热负荷。",
    )
    payload = request.as_dict()

    assert DomainModelRequest.from_mapping(payload) == request
    assert request.capability_manifest_ref == service.capability_manifest.ref
    assert request.output_schema_id == "optimization-intent"
    assert request.output_policy.constraints_mode == "system-only"
    assert request.output_policy.operating_context_mode == "excluded"
    assert request.output_policy.solver_selection_mode == "forbidden"
    assert request.output_policy.response_mode == "full-replacement"
    assert request.output_policy.maximum_model_attempts == 2
    assert request.turn_index == request.model_attempt == 1
    capability_payload = payload["capability_manifest"]
    assert isinstance(capability_payload, Mapping)
    assert set(capability_payload) == {
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
    }
    assert capability_payload["result_output_rules"] == [
        {
            "rule_id": "result-output-2-3",
            "minimum_objectives": 2,
            "maximum_objectives": 3,
            "output_kind": "steady-setpoint-vector",
            "default_include_alternatives": True,
            "default_max_candidates": 5,
            "maximum_candidates": 5,
        },
        {
            "rule_id": "result-output-1-1",
            "minimum_objectives": 1,
            "maximum_objectives": 1,
            "output_kind": "steady-setpoint-vector",
            "default_include_alternatives": False,
            "default_max_candidates": 1,
            "maximum_candidates": 3,
        },
    ]
    capability_text = json.dumps(capability_payload, ensure_ascii=False).lower()
    for forbidden in (
        "context_fields",
        "lower_bound",
        "upper_bound",
        "normalization_scale",
        "hard_guardrails",
        "publishability_guardrails",
        "execution_routes",
        "source_authority",
    ):
        assert forbidden not in capability_text
    assert not ({"operating_context", "facts", "current_setpoints"} & set(payload))
    payload_text = json.dumps(payload, ensure_ascii=False).lower()
    assert "solver_id" not in payload_text
    assert "algorithm_id" not in payload_text


@pytest.mark.parametrize("multi", [False, True])
def test_one_or_many_objectives_use_the_same_resolved_protocol(
    repo_root: Path,
    multi: bool,
) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-objectives",
        message_id="user-1",
        user_text="优化当前工况。",
    )
    intent = OptimizationIntent.from_mapping(_raw_intent(multi=multi))

    result = service.evaluate_response(request, _response(request, intent))

    assert result.status == "resolved"
    assert result.resolved_intent == intent
    assert result.candidate_intent == intent
    assert result.issues == ()
    assert CommunicationResult.from_mapping(result.as_dict()) == result


def test_explicit_unsupported_response_maps_without_a_candidate_intent(
    repo_root: Path,
) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-explicit-unsupported",
        message_id="user-1",
        user_text="指定遗传算法求解。",
    )
    payload = _unsupported_response(request)

    parsed = DomainModelResponse.from_mapping(payload)
    result = service.evaluate_response(request, payload)

    assert parsed.outcome == "unsupported"
    assert parsed.intent is None
    assert parsed.unsupported is not None
    assert DomainModelResponse.from_mapping(parsed.as_dict()) == parsed
    assert result.status == "unsupported"
    assert result.candidate_intent is None
    assert result.resolved_intent is None
    assert result.issues[0].code == "solver-selection-forbidden"
    assert result.issues[0].message == UNSUPPORTED_SAFE_MESSAGES["solver-selection-forbidden"]
    assert result.issues[0].json_pointer == "/unsupported/reason_code"
    assert result.issues[0].source == "capability"
    assert result.issues[0].retryable is False
    assert CommunicationResult.from_mapping(result.as_dict()) == result


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update({"intent": _raw_intent()}), "fields differ"),
        (lambda value: value.update({"operating_context": {"feed": 400}}), "fields differ"),
        (lambda value: value.update({"solver_id": "genetic-algorithm"}), "fields differ"),
        (
            lambda value: value["unsupported"].update({"details": "current feed is 400"}),
            "fields differ",
        ),
        (
            lambda value: value["unsupported"].update({"reason_code": "provider-timeout"}),
            "reason_code is not published",
        ),
        (
            lambda value: value["unsupported"].update(
                {"safe_message": "Current pressure is 152325 Pa; use solver X."}
            ),
            "safe_message differs",
        ),
    ],
)
def test_unsupported_response_rejects_mixed_or_free_form_fields(
    repo_root: Path,
    mutation: Any,
    match: str,
) -> None:
    request = _service(repo_root).start(
        session_id="session-unsupported-strict",
        message_id="user-1",
        user_text="指定算法求解。",
    )
    payload = _unsupported_response(request)

    mutation(payload)

    with pytest.raises(ValueError, match=match):
        DomainModelResponse.from_mapping(payload)


def test_intent_response_rejects_unsupported_variant_and_requires_tag(repo_root: Path) -> None:
    request = _service(repo_root).start(
        session_id="session-intent-strict",
        message_id="user-1",
        user_text="降低能耗。",
    )
    payload = _response(request, _raw_intent())
    mixed = {
        **payload,
        "unsupported": _unsupported_response(request)["unsupported"],
    }
    untagged = dict(payload)
    untagged.pop("outcome")

    with pytest.raises(ValueError, match="fields differ"):
        DomainModelResponse.from_mapping(mixed)
    with pytest.raises(ValueError, match="fields differ"):
        DomainModelResponse.from_mapping(untagged)


def test_stale_explicit_unsupported_response_is_repaired_before_classification(
    repo_root: Path,
) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-unsupported-stale",
        message_id="user-1",
        user_text="指定算法求解。",
    )
    stale = ContractRef("stale", "0" * 64)

    result = service.evaluate_response(
        request,
        _unsupported_response(request, request_ref=stale),
    )

    assert result.status == "repair_required"
    assert result.candidate_intent is None
    assert tuple(item.code for item in result.issues) == ("request-ref-mismatch",)


def test_invalid_response_gets_one_full_replacement_retry_then_fails(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-repair",
        message_id="user-1",
        user_text="降低能耗。",
    )
    invalid = _response(request, _raw_intent())
    invalid_intent = invalid["intent"]
    assert isinstance(invalid_intent, dict)
    invalid_intent.pop("preference")

    first = service.evaluate_response(request, invalid)

    assert first.status == "repair_required"
    assert first.repair is not None
    assert first.repair.required_action == "return-full-replacement"
    assert first.repair.next_model_attempt == 2
    retry = service.build_repair_retry(request, first)
    assert retry.turn_index == 1
    assert retry.model_attempt == 2
    assert retry.feedback_issues == first.issues

    second_invalid = _response(retry, invalid_intent)
    second = service.evaluate_response(retry, second_invalid)

    assert second.status == "failed"
    assert second.repair is None
    assert {item.code for item in second.issues} == {"model-repair-exhausted"}
    with pytest.raises(ValueError, match="does not authorize"):
        service.build_repair_retry(retry, second)


@pytest.mark.parametrize(
    "raw_response",
    [
        '{"schema_id":"x","schema_id":"y"}',
        '{"schema_id":NaN}',
        b"\xff",
        "[]",
    ],
)
def test_raw_json_response_preserves_duplicate_nonfinite_and_encoding_errors(
    repo_root: Path,
    raw_response: str | bytes,
) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-raw-json",
        message_id="user-1",
        user_text="降低能耗。",
    )

    result = service.evaluate_response(request, raw_response)

    assert result.status == "repair_required"
    assert tuple(item.code for item in result.issues) == ("invalid-model-response",)


def test_invalid_provider_output_is_never_echoed_into_repair_feedback(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-safe-repair",
        message_id="user-1",
        user_text="降低能耗。",
    )
    provider_controlled = "q9W8e7R6t5Y4u3I2o1P0"

    result = service.evaluate_response(
        request,
        f'{{"{provider_controlled}":1,"{provider_controlled}":2}}',
    )
    retry = service.build_repair_retry(request, result)

    assert result.status == "repair_required"
    assert provider_controlled not in json.dumps(result.as_dict(), ensure_ascii=False)
    assert provider_controlled not in json.dumps(retry.as_dict(), ensure_ascii=False)


@pytest.mark.parametrize("stale_field", ["request", "capability"])
def test_stale_echo_is_a_machine_repair_not_a_semantic_intent(
    repo_root: Path,
    stale_field: str,
) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-stale",
        message_id="user-1",
        user_text="降低能耗。",
    )
    stale = ContractRef("stale", "0" * 64)
    payload = _response(
        request,
        OptimizationIntent.from_mapping(_raw_intent()),
        request_ref=stale if stale_field == "request" else None,
        capability_ref=stale if stale_field == "capability" else None,
    )

    result = service.evaluate_response(request, payload)

    assert result.status == "repair_required"
    assert result.candidate_intent is None
    assert result.issues[0].audience == "model"
    assert result.issues[0].source == "protocol"


def test_known_ambiguity_becomes_bounded_capability_backed_question(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-clarify",
        message_id="user-1",
        user_text="降低能耗，但我还没决定允许调整哪些设定值。",
    )
    intent = OptimizationIntent.from_mapping(
        _raw_intent(
            decisions=["furnace_temperature_target_k"],
            ambiguities=["decision-variable-selection-ambiguous"],
        )
    )

    result = service.evaluate_response(request, _response(request, intent))

    assert result.status == "needs_clarification"
    assert result.clarification is not None
    question = result.clarification.questions[0]
    assert question.answer_kind == "multi-select"
    assert question.minimum_selections == 1
    assert question.maximum_selections == len(question.options)
    option_values = {item.value for item in question.options}
    assert option_values == {
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    }
    assert "reflux_ratio_target" not in option_values
    assert CommunicationResult.from_mapping(result.as_dict()) == result


def test_selection_limits_and_question_count_come_from_manifest_and_policy(
    repo_root: Path,
) -> None:
    bundle = load_capability_bundle(repo_root)
    base_manifest = build_public_capability_manifest(bundle)
    extra_objective = {
        **dict(base_manifest.objectives[0]),
        "objective_id": "synthetic-fourth-objective",
        "metric_id": "synthetic-fourth-metric",
        "business_name": "合成第四目标",
        "availability": "available",
        "availability_reason": None,
    }
    extra_decision = {
        **dict(base_manifest.decisions[0]),
        "decision_id": "synthetic-third-decision",
        "business_name": "合成第三决策",
        "availability": "available",
        "availability_reason": None,
    }
    manifest = replace(
        base_manifest,
        objectives=(*base_manifest.objectives, extra_objective),
        decisions=(*base_manifest.decisions, extra_decision),
    )
    policy = IntentCommunicationPolicy(maximum_questions_per_turn=2)
    service = IntentCommunicationService(
        manifest,
        BundleCapabilityView(bundle),
        policy=policy,
    )
    request = service.start(
        session_id="session-derived-limits",
        message_id="user-1",
        user_text="目标和变量都需要确认。",
    )
    ambiguous = OptimizationIntent.from_mapping(
        _raw_intent(
            ambiguities=[
                "objective-selection-ambiguous",
                "decision-variable-selection-ambiguous",
                "result-alternatives-ambiguous",
            ]
        )
    )

    result = service.evaluate_response(request, _response(request, ambiguous))

    assert result.status == "needs_clarification"
    assert result.clarification is not None
    assert len(result.clarification.questions) == 2
    objective_question, decision_question = result.clarification.questions
    assert objective_question.maximum_selections == len(objective_question.options)
    assert objective_question.maximum_selections > 3
    assert decision_question.maximum_selections == len(decision_question.options)
    assert decision_question.maximum_selections > 2


def test_clarification_answer_creates_new_turn_and_requires_full_replacement(
    repo_root: Path,
) -> None:
    service = _service(repo_root)
    first_request = service.start(
        session_id="session-followup",
        message_id="user-1",
        user_text="降低能耗，但先确认调整变量。",
    )
    ambiguous = OptimizationIntent.from_mapping(
        _raw_intent(
            decisions=["furnace_temperature_target_k"],
            ambiguities=["decision-variable-selection-ambiguous"],
        )
    )
    first_result = service.evaluate_response(
        first_request,
        _response(first_request, ambiguous),
    )
    assert first_result.clarification is not None
    question = first_result.clarification.questions[0]
    answers = (
        ClarificationAnswer(
            question_id=question.question_id,
            values=(
                "furnace_temperature_target_k",
                "tower_top_pressure_target_pa_a",
            ),
        ),
    )

    followup = service.build_clarification_followup(
        first_request,
        first_result,
        message_id="user-2",
        user_text="允许同时调整炉出口温度和塔顶压力。",
        answers=answers,
    )

    assert followup.turn_index == 2
    assert followup.model_attempt == 1
    assert followup.prior_intent == ambiguous
    assert followup.prior_clarification == first_result.clarification
    assert followup.clarification_answers == answers
    assert len(followup.user_messages) == 2
    assert DomainModelRequest.from_mapping(followup.as_dict()) == followup

    replacement = OptimizationIntent.from_mapping(_raw_intent())
    resolved = service.evaluate_response(followup, _response(followup, replacement))
    assert resolved.status == "resolved"
    assert resolved.resolved_intent == replacement


def test_clarification_answers_must_exactly_cover_declared_choices(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-bad-answer",
        message_id="user-1",
        user_text="变量不明确。",
    )
    ambiguous = OptimizationIntent.from_mapping(
        _raw_intent(ambiguities=["result-alternatives-ambiguous"])
    )
    result = service.evaluate_response(request, _response(request, ambiguous))
    assert result.clarification is not None
    question = result.clarification.questions[0]

    with pytest.raises(ValueError, match="unsupported value"):
        service.build_clarification_followup(
            request,
            result,
            message_id="user-2",
            user_text="随便。",
            answers=(
                ClarificationAnswer(
                    question_id=question.question_id,
                    values=("unknown-choice",),
                ),
            ),
        )


def test_deferred_decision_is_unsupported_instead_of_silently_remapped(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-deferred",
        message_id="user-1",
        user_text="通过调整回流比降低能耗。",
    )
    intent = OptimizationIntent.from_mapping(_raw_intent(decisions=["reflux_ratio_target"]))

    result = service.evaluate_response(request, _response(request, intent))

    assert result.status == "unsupported"
    assert {item.code for item in result.issues} == {"unsupported-decision-variable"}
    assert result.resolved_intent is None


def test_unbound_business_constraint_is_rejected_at_the_communication_boundary(
    repo_root: Path,
) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-constraint",
        message_id="user-1",
        user_text="满足稳定约束并降低能耗。",
    )
    intent = OptimizationIntent.from_mapping(_raw_intent(constraints=["m2-structural-numeric"]))

    result = service.evaluate_response(request, _response(request, intent))

    assert result.status == "unsupported"
    assert tuple(item.code for item in result.issues) == (
        "business-constraint-binding-unavailable",
    )
    assert result.issues[0].json_pointer == "/intent/constraints"


def test_unknown_ambiguity_gets_one_machine_repair_then_fails(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-generic",
        message_id="user-1",
        user_text="还需要确认一个业务条件。",
    )
    intent = OptimizationIntent.from_mapping(_raw_intent(ambiguities=["confirm-shift-plan"]))

    first = service.evaluate_response(request, _response(request, intent))

    assert first.status == "repair_required"
    assert first.clarification is None
    assert {item.code for item in first.issues} == {"unknown-ambiguity-code"}
    assert first.issues[0].supported_values == service.policy.allowed_ambiguity_codes
    retry = service.build_repair_retry(request, first)
    second = service.evaluate_response(retry, _response(retry, intent))

    assert second.status == "failed"
    assert second.clarification is None
    assert {item.code for item in second.issues} == {"model-repair-exhausted"}


def test_clarification_turn_limit_fails_instead_of_opening_another_turn(
    repo_root: Path,
) -> None:
    policy = IntentCommunicationPolicy(maximum_clarification_turns=2)
    service = IntentCommunicationService.from_bundle(
        load_capability_bundle(repo_root),
        policy=policy,
    )
    request = service.start(
        session_id="session-turn-limit",
        message_id="user-1",
        user_text="是否返回备选方案还没决定。",
    )
    ambiguous = OptimizationIntent.from_mapping(
        _raw_intent(ambiguities=["result-alternatives-ambiguous"])
    )
    first = service.evaluate_response(request, _response(request, ambiguous))
    assert first.clarification is not None
    question = first.clarification.questions[0]
    followup = service.build_clarification_followup(
        request,
        first,
        message_id="user-2",
        user_text="我仍然没有决定。",
        answers=(
            ClarificationAnswer(
                question_id=question.question_id,
                values=("selected-only",),
            ),
        ),
    )

    exhausted = service.evaluate_response(followup, _response(followup, ambiguous))

    assert followup.turn_index == 2
    assert exhausted.status == "failed"
    assert exhausted.clarification is None
    assert {item.code for item in exhausted.issues} == {"clarification-turn-limit-exhausted"}


def test_gold_examples_cover_resolved_clarification_and_unsupported(
    repo_root: Path,
) -> None:
    payload = json.loads(
        (repo_root / "data" / "rto" / "gold" / "domain_communication_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["schema_version"] == "1.0.0"
    cases = payload["cases"]
    assert isinstance(cases, list)
    assert len(cases) >= 5
    assert {item["expected_status"] for item in cases} >= {
        "resolved",
        "needs_clarification",
        "unsupported",
    }

    service = _service(repo_root)
    for index, case in enumerate(cases, start=1):
        request = service.start(
            session_id=f"gold-{index}",
            message_id="user-1",
            user_text=case["user_text"],
        )
        intent = OptimizationIntent.from_mapping(case["expected_intent"])
        result = service.evaluate_response(request, _response(request, intent))
        assert result.status == case["expected_status"], case["case_id"]
        assert [item.code for item in result.issues] == case["expected_issue_codes"]


def test_result_rejects_cross_field_tampering(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-tamper",
        message_id="user-1",
        user_text="降低能耗。",
    )
    result = service.evaluate_response(
        request,
        _response(request, OptimizationIntent.from_mapping(_raw_intent())),
    )
    payload = result.as_dict()
    payload["resolved_intent"] = None

    with pytest.raises(ValueError, match="inconsistent fields"):
        CommunicationResult.from_mapping(payload)


def test_request_rejects_embedded_manifest_reference_tampering(repo_root: Path) -> None:
    service = _service(repo_root)
    request = service.start(
        session_id="session-manifest-tamper",
        message_id="user-1",
        user_text="降低能耗。",
    )

    with pytest.raises(ValueError, match="embedded manifest"):
        replace(
            request,
            capability_manifest_ref=ContractRef("stale", "0" * 64),
        )
