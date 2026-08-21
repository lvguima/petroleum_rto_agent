from __future__ import annotations

import json
from pathlib import Path

from petroleum_rto.domain_model import load_provider_catalog
from petroleum_rto.domain_model.prompt import PromptCompiler
from petroleum_rto.domain_model.runtime import DomainIntentRuntime
from petroleum_rto.rto.capabilities import load_capability_bundle
from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DomainModelInvocationResult,
    DomainModelRequest,
    ProviderAttempt,
)
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.problem import UnifiedProblemBuilder
from petroleum_rto.rto.runtime import build_intent_communication_service
from petroleum_rto.rto.unified_inputs import OptimizationIntent


class _IntentOnlyPort:
    provider_id = "dmx-cn"
    provider_version = "1.0.0"

    def __init__(self, intent: OptimizationIntent) -> None:
        self._intent = intent
        self.requests: list[DomainModelRequest] = []

    def invoke(self, request: DomainModelRequest) -> DomainModelInvocationResult:
        self.requests.append(request)
        response = {
            "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
            "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
            "response_id": "integration-response",
            "request_ref": request.ref.as_dict(),
            "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
            "outcome": "intent",
            "intent": self._intent.as_dict(),
        }
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id="integration-invocation",
            request_ref=request.ref,
            status="succeeded",
            attempts=(
                ProviderAttempt(
                    attempt_index=1,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status="succeeded",
                    provider_request_id="integration-provider-request",
                    served_model="deepseek-v4-flash-0731",
                    finish_reason="stop",
                    duration_ms=1,
                    usage=None,
                    error=None,
                ),
            ),
            response=json.dumps(response, ensure_ascii=False),
            error=None,
        )


def test_resolved_model_intent_and_trusted_context_only_meet_at_problem_builder(
    repo_root: Path,
) -> None:
    intent = OptimizationIntent.from_mapping(
        json.loads(
            (repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json").read_text(
                encoding="utf-8"
            )
        )
    )
    catalog = load_provider_catalog(repo_root)
    provider = catalog.provider("dmx-cn")
    model = provider.model("deepseek-v4-flash-0731")
    port = _IntentOnlyPort(intent)
    runtime = DomainIntentRuntime(
        provider_profile=provider,
        model_profile=model,
        port=port,
        communication_service=build_intent_communication_service(repo_root=repo_root),
        prompt_compiler=PromptCompiler(),
    )

    outcome = runtime.interpret("请降低单位进料的加热炉燃料能耗。")

    assert outcome.status == "resolved"
    assert outcome.communication_result is not None
    resolved = outcome.communication_result.resolved_intent
    assert resolved is not None
    outbound = json.dumps(port.requests[0].as_dict(), ensure_ascii=False).lower()
    assert '"operating_context":' not in outbound
    assert '"current_setpoints":' not in outbound
    assert '"feed_composition":' not in outbound

    bundle = load_capability_bundle(repo_root)
    trusted_context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    problem = UnifiedProblemBuilder().build(bundle, resolved, trusted_context)

    assert problem.intent_ref.object_id == resolved.intent_id
    assert problem.context_ref.object_id == trusted_context.context_id
    assert tuple(item.metric_id for item in problem.objectives) == (
        "specific_furnace_fuel_energy_mj_per_t",
    )
