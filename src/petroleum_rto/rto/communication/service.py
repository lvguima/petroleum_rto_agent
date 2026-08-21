"""Deterministic negotiation around an untrusted domain-model response."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from ..capabilities import (
    BundleCapabilityView,
    PublicCapabilityManifest,
    UnifiedCapabilityBundle,
    build_public_capability_manifest,
)
from ..contracts.common import JsonValue
from ..contracts.reference import ContractRef
from ..unified_inputs import CapabilityView, IntentResolutionIssue, IntentResolver
from ..unified_inputs.models import OptimizationIntent
from .loader import decode_domain_model_response
from .models import (
    COMMUNICATION_RESULT_SCHEMA_ID,
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_REQUEST_SCHEMA_ID,
    DOMAIN_MODEL_REQUEST_SCHEMA_VERSION,
    ClarificationAnswer,
    ClarificationOption,
    ClarificationQuestion,
    ClarificationRequest,
    CommunicationResult,
    CommunicationStatus,
    DomainCapabilityManifest,
    DomainModelRequest,
    DomainModelResponse,
    DomainOutputPolicy,
    ProtocolIssue,
    RepairDirective,
    UserMessage,
)
from .policy import IntentCommunicationPolicy


class IntentCommunicationService:
    """Build requests and classify model responses without context, solving or simulation."""

    def __init__(
        self,
        manifest: PublicCapabilityManifest,
        capabilities: CapabilityView,
        *,
        policy: IntentCommunicationPolicy | None = None,
    ) -> None:
        if not isinstance(manifest, PublicCapabilityManifest):
            raise TypeError("manifest must be PublicCapabilityManifest")
        if policy is not None and not isinstance(policy, IntentCommunicationPolicy):
            raise TypeError("policy must be IntentCommunicationPolicy or None")
        self._manifest = manifest
        self._domain_manifest = DomainCapabilityManifest.from_public(manifest)
        self._capabilities = capabilities
        self._policy = IntentCommunicationPolicy() if policy is None else policy

    @classmethod
    def from_bundle(
        cls,
        bundle: UnifiedCapabilityBundle,
        *,
        policy: IntentCommunicationPolicy | None = None,
    ) -> IntentCommunicationService:
        """Create the public gateway while keeping the internal bundle out of model requests."""

        if not isinstance(bundle, UnifiedCapabilityBundle):
            raise TypeError("bundle must be UnifiedCapabilityBundle")
        return cls(
            build_public_capability_manifest(bundle),
            BundleCapabilityView(bundle),
            policy=policy,
        )

    @property
    def capability_manifest(self) -> DomainCapabilityManifest:
        return self._domain_manifest

    @property
    def policy(self) -> IntentCommunicationPolicy:
        return self._policy

    def start(
        self,
        *,
        session_id: str,
        message_id: str,
        user_text: str,
    ) -> DomainModelRequest:
        """Create the first self-contained request delivered to a model provider."""

        message = UserMessage(message_id=message_id, text=user_text)
        return DomainModelRequest(
            schema_id=DOMAIN_MODEL_REQUEST_SCHEMA_ID,
            schema_version=DOMAIN_MODEL_REQUEST_SCHEMA_VERSION,
            request_id=self._request_id(session_id, turn_index=1, model_attempt=1),
            session_id=session_id,
            turn_index=1,
            model_attempt=1,
            capability_manifest=self._domain_manifest,
            capability_manifest_ref=self._domain_manifest.ref,
            user_messages=(message,),
            prior_intent=None,
            prior_clarification=None,
            clarification_answers=(),
            feedback_issues=(),
            output_schema_id="optimization-intent",
            output_schema_version="1.0.0",
            output_policy=DomainOutputPolicy(
                constraints_mode="system-only",
                operating_context_mode="excluded",
                solver_selection_mode="forbidden",
                response_mode="full-replacement",
                maximum_model_attempts=self._policy.maximum_model_attempts,
            ),
        )

    def evaluate_response(
        self,
        request: DomainModelRequest,
        raw_response: object,
    ) -> CommunicationResult:
        """Strictly parse, correlate and resolve one untrusted structured response."""

        self._validate_request(request)
        try:
            if isinstance(raw_response, (str, bytes, bytearray)):
                raw_response = decode_domain_model_response(raw_response)
            response = DomainModelResponse.from_mapping(raw_response)
        except (TypeError, ValueError, RecursionError):
            issue = ProtocolIssue(
                code="invalid-model-response",
                json_pointer="/",
                message="model response violates the strict response contract",
                audience="model",
                source="contract",
                retryable=True,
            )
            return self._repair_or_fail(request, response_ref=None, issues=(issue,))

        correlation_issues: list[ProtocolIssue] = []
        if response.request_ref != request.ref:
            correlation_issues.append(
                ProtocolIssue(
                    code="request-ref-mismatch",
                    json_pointer="/request_ref",
                    message="model response does not reference the current request",
                    audience="model",
                    source="protocol",
                    retryable=True,
                )
            )
        if response.capability_manifest_ref != request.capability_manifest_ref:
            correlation_issues.append(
                ProtocolIssue(
                    code="capability-ref-mismatch",
                    json_pointer="/capability_manifest_ref",
                    message="model response was produced against another capability manifest",
                    audience="model",
                    source="protocol",
                    retryable=True,
                )
            )
        if correlation_issues:
            return self._repair_or_fail(
                request,
                response_ref=response.ref,
                issues=tuple(correlation_issues),
            )

        if response.outcome == "unsupported":
            unsupported = response.unsupported
            assert unsupported is not None
            issue = ProtocolIssue(
                code=unsupported.reason_code,
                json_pointer="/unsupported/reason_code",
                message=unsupported.safe_message,
                audience="user",
                source="capability",
                retryable=False,
            )
            return self._result(
                request=request,
                response=response,
                status="unsupported",
                candidate=None,
                issues=(issue,),
            )

        intent = response.intent
        assert intent is not None
        if intent.constraints:
            issue = ProtocolIssue(
                code="business-constraint-binding-unavailable",
                json_pointer="/intent/constraints",
                message=(
                    "additional business constraints are not parameter-bound; system hard "
                    "guardrails remain mandatory and must not be echoed by the model"
                ),
                audience="user",
                source="capability",
                retryable=False,
            )
            return self._result(
                request=request,
                response=response,
                status="unsupported",
                candidate=intent,
                issues=(issue,),
            )

        unknown_ambiguity_issues = tuple(
            ProtocolIssue(
                code="unknown-ambiguity-code",
                json_pointer=f"/intent/ambiguities/{index}",
                message="model response used an ambiguity code outside communication policy",
                audience="model",
                source="protocol",
                retryable=True,
                supported_values=self._policy.allowed_ambiguity_codes,
            )
            for index, code in enumerate(intent.ambiguities)
            if code not in self._policy.allowed_ambiguity_codes
        )
        if unknown_ambiguity_issues:
            return self._repair_or_fail(
                request,
                response_ref=response.ref,
                issues=unknown_ambiguity_issues,
            )

        resolution = IntentResolver().resolve(intent, self._capabilities)
        if resolution.status == "resolved":
            return self._result(
                request=request,
                response=response,
                status="resolved",
                candidate=intent,
                resolved=intent,
            )

        issues = tuple(self._from_resolution_issue(item) for item in resolution.issues)
        if resolution.status == "unsupported":
            return self._result(
                request=request,
                response=response,
                status="unsupported",
                candidate=intent,
                issues=issues,
            )

        if request.turn_index >= self._policy.maximum_clarification_turns:
            issue = ProtocolIssue(
                code="clarification-turn-limit-exhausted",
                json_pointer="/intent/ambiguities",
                message="clarification turn limit exhausted without a resolved intent",
                audience="operator",
                source="ambiguity",
                retryable=False,
            )
            return self._failed_result(
                request,
                response_ref=response.ref,
                issues=(issue,),
            )

        clarification = ClarificationRequest(
            request_ref=request.ref,
            candidate_intent_ref=ContractRef(
                intent.intent_id,
                intent.fingerprint,
            ),
            questions=self._questions(
                request=request,
                intent=intent,
                resolution_issues=resolution.issues,
            ),
        )
        return self._result(
            request=request,
            response=response,
            status="needs_clarification",
            candidate=intent,
            clarification=clarification,
            issues=issues,
        )

    def build_repair_retry(
        self,
        request: DomainModelRequest,
        result: CommunicationResult,
    ) -> DomainModelRequest:
        """Reissue the same turn once with machine-readable repair feedback."""

        self._validate_request(request)
        if result.status != "repair_required" or result.repair is None:
            raise ValueError("result does not authorize a model repair retry")
        if result.request_ref != request.ref or result.repair.request_ref != request.ref:
            raise ValueError("repair result references another request")
        next_attempt = result.repair.next_model_attempt
        return DomainModelRequest(
            schema_id=DOMAIN_MODEL_REQUEST_SCHEMA_ID,
            schema_version=DOMAIN_MODEL_REQUEST_SCHEMA_VERSION,
            request_id=self._request_id(
                request.session_id,
                turn_index=request.turn_index,
                model_attempt=next_attempt,
            ),
            session_id=request.session_id,
            turn_index=request.turn_index,
            model_attempt=next_attempt,
            capability_manifest=request.capability_manifest,
            capability_manifest_ref=request.capability_manifest_ref,
            user_messages=request.user_messages,
            prior_intent=request.prior_intent,
            prior_clarification=request.prior_clarification,
            clarification_answers=request.clarification_answers,
            feedback_issues=result.issues,
            output_schema_id=request.output_schema_id,
            output_schema_version=request.output_schema_version,
            output_policy=request.output_policy,
        )

    def build_clarification_followup(
        self,
        request: DomainModelRequest,
        result: CommunicationResult,
        *,
        message_id: str,
        user_text: str,
        answers: Sequence[ClarificationAnswer],
    ) -> DomainModelRequest:
        """Create a new turn; the model must return a complete replacement intent."""

        self._validate_request(request)
        if (
            result.status != "needs_clarification"
            or result.clarification is None
            or result.candidate_intent is None
        ):
            raise ValueError("result does not contain a clarification request")
        if result.request_ref != request.ref or result.clarification.request_ref != request.ref:
            raise ValueError("clarification result references another request")
        if request.turn_index >= self._policy.maximum_clarification_turns:
            raise ValueError("clarification turn limit is already exhausted")
        checked_answers = self._validate_answers(result.clarification, answers)
        messages = (*request.user_messages, UserMessage(message_id=message_id, text=user_text))
        next_turn = request.turn_index + 1
        return DomainModelRequest(
            schema_id=DOMAIN_MODEL_REQUEST_SCHEMA_ID,
            schema_version=DOMAIN_MODEL_REQUEST_SCHEMA_VERSION,
            request_id=self._request_id(
                request.session_id,
                turn_index=next_turn,
                model_attempt=1,
            ),
            session_id=request.session_id,
            turn_index=next_turn,
            model_attempt=1,
            capability_manifest=request.capability_manifest,
            capability_manifest_ref=request.capability_manifest_ref,
            user_messages=messages,
            prior_intent=result.candidate_intent,
            prior_clarification=result.clarification,
            clarification_answers=checked_answers,
            feedback_issues=result.issues,
            output_schema_id=request.output_schema_id,
            output_schema_version=request.output_schema_version,
            output_policy=request.output_policy,
        )

    def _validate_request(self, request: DomainModelRequest) -> None:
        if not isinstance(request, DomainModelRequest):
            raise TypeError("request must be DomainModelRequest")
        if request.capability_manifest_ref != self._domain_manifest.ref:
            raise ValueError("request references a stale capability manifest")
        if request.output_policy.maximum_model_attempts != self._policy.maximum_model_attempts:
            raise ValueError("request references another intent communication policy")

    @staticmethod
    def _request_id(session_id: str, *, turn_index: int, model_attempt: int) -> str:
        return f"intent-request-{session_id}-t{turn_index}-a{model_attempt}"

    @staticmethod
    def _result_id(request: DomainModelRequest, status: str) -> str:
        return f"intent-result-{request.session_id}-t{request.turn_index}-a{request.model_attempt}-{status}"

    def _repair_or_fail(
        self,
        request: DomainModelRequest,
        *,
        response_ref: ContractRef | None,
        issues: tuple[ProtocolIssue, ...],
    ) -> CommunicationResult:
        if request.model_attempt < self._policy.maximum_model_attempts:
            repair = RepairDirective(
                request_ref=request.ref,
                next_model_attempt=request.model_attempt + 1,
                required_action="return-full-replacement",
                issues=issues,
            )
            return CommunicationResult(
                schema_id=COMMUNICATION_RESULT_SCHEMA_ID,
                schema_version=COMMUNICATION_SCHEMA_VERSION,
                result_id=self._result_id(request, "repair-required"),
                status="repair_required",
                request_ref=request.ref,
                response_ref=response_ref,
                capability_manifest_ref=request.capability_manifest_ref,
                candidate_intent=None,
                resolved_intent=None,
                clarification=None,
                repair=repair,
                issues=issues,
            )
        exhausted = tuple(
            ProtocolIssue(
                code="model-repair-exhausted",
                json_pointer=item.json_pointer,
                message=f"model repair limit exhausted: {item.message}",
                audience="operator",
                source="protocol",
                retryable=False,
                supported_values=item.supported_values,
            )
            for item in issues
        )
        return self._failed_result(
            request,
            response_ref=response_ref,
            issues=exhausted,
        )

    def _failed_result(
        self,
        request: DomainModelRequest,
        *,
        response_ref: ContractRef | None,
        issues: tuple[ProtocolIssue, ...],
    ) -> CommunicationResult:
        return CommunicationResult(
            schema_id=COMMUNICATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            result_id=self._result_id(request, "failed"),
            status="failed",
            request_ref=request.ref,
            response_ref=response_ref,
            capability_manifest_ref=request.capability_manifest_ref,
            candidate_intent=None,
            resolved_intent=None,
            clarification=None,
            repair=None,
            issues=issues,
        )

    def _result(
        self,
        *,
        request: DomainModelRequest,
        response: DomainModelResponse,
        status: str,
        candidate: OptimizationIntent | None,
        resolved: OptimizationIntent | None = None,
        clarification: ClarificationRequest | None = None,
        issues: tuple[ProtocolIssue, ...] = (),
    ) -> CommunicationResult:
        if status not in {"resolved", "needs_clarification", "unsupported"}:
            raise ValueError("unsupported semantic result status")
        return CommunicationResult(
            schema_id=COMMUNICATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            result_id=self._result_id(request, status.replace("_", "-")),
            status=cast(CommunicationStatus, status),
            request_ref=request.ref,
            response_ref=response.ref,
            capability_manifest_ref=request.capability_manifest_ref,
            candidate_intent=candidate,
            resolved_intent=resolved,
            clarification=clarification,
            repair=None,
            issues=issues,
        )

    @staticmethod
    def _from_resolution_issue(issue: IntentResolutionIssue) -> ProtocolIssue:
        ambiguity = issue.code == "needs-clarification"
        return ProtocolIssue(
            code=issue.code,
            json_pointer=f"/intent{issue.json_pointer}",
            message=issue.message,
            audience="user",
            source="ambiguity" if ambiguity else "capability",
            retryable=False,
            supported_values=issue.supported_values,
        )

    def _questions(
        self,
        *,
        request: DomainModelRequest,
        intent: OptimizationIntent,
        resolution_issues: Sequence[IntentResolutionIssue],
    ) -> tuple[ClarificationQuestion, ...]:
        questions: list[ClarificationQuestion] = []
        for issue in resolution_issues:
            if issue.code != "needs-clarification":
                continue
            try:
                index = int(issue.json_pointer.rsplit("/", 1)[1])
                ambiguity_code = intent.ambiguities[index]
            except (IndexError, ValueError) as exc:
                raise ValueError("resolver returned an invalid ambiguity pointer") from exc
            questions.append(
                self._question_for_ambiguity(
                    request=request,
                    intent=intent,
                    index=index,
                    ambiguity_code=ambiguity_code,
                )
            )
            if len(questions) == self._policy.maximum_questions_per_turn:
                break
        if not questions:
            raise ValueError("needs_clarification resolution produced no questions")
        return tuple(questions)

    def _question_for_ambiguity(
        self,
        *,
        request: DomainModelRequest,
        intent: OptimizationIntent,
        index: int,
        ambiguity_code: str,
    ) -> ClarificationQuestion:
        question_id = f"clarify-{request.session_id}-t{request.turn_index}-q{index + 1}"
        pointer = f"/intent/ambiguities/{index}"
        if ambiguity_code == "objective-selection-ambiguous":
            options = self._manifest_options(
                self._manifest.objectives,
                value_key="metric_id",
            )
            return ClarificationQuestion(
                question_id=question_id,
                ambiguity_code=ambiguity_code,
                json_pointer=pointer,
                prompt="请选择本次优化希望处理的一个或多个目标。",
                answer_kind="multi-select",
                options=options,
                minimum_selections=1,
                maximum_selections=len(options),
            )
        if ambiguity_code == "objective-priority-ambiguous":
            options = tuple(
                ClarificationOption(value=item.metric_id, label=item.metric_id)
                for item in intent.objectives
            )
            return ClarificationQuestion(
                question_id=question_id,
                ambiguity_code=ambiguity_code,
                json_pointer=pointer,
                prompt="请按从高到低的顺序排列优化目标。",
                answer_kind="ordered-select",
                options=options,
                minimum_selections=len(options),
                maximum_selections=len(options),
            )
        if ambiguity_code == "decision-variable-selection-ambiguous":
            options = self._manifest_options(
                self._manifest.decisions,
                value_key="decision_id",
            )
            return ClarificationQuestion(
                question_id=question_id,
                ambiguity_code=ambiguity_code,
                json_pointer=pointer,
                prompt="请选择允许RTO调整的一个或多个高层设定值。",
                answer_kind="multi-select",
                options=options,
                minimum_selections=1,
                maximum_selections=len(options),
            )
        if ambiguity_code == "result-alternatives-ambiguous":
            return ClarificationQuestion(
                question_id=question_id,
                ambiguity_code=ambiguity_code,
                json_pointer=pointer,
                prompt="请选择只返回最终方案，还是同时返回经过排序的备选方案。",
                answer_kind="single-select",
                options=(
                    ClarificationOption(value="selected-only", label="只返回最终方案"),
                    ClarificationOption(
                        value="include-alternatives",
                        label="同时返回备选方案",
                    ),
                ),
                minimum_selections=1,
                maximum_selections=1,
            )
        raise ValueError("communication policy allowed an unsupported ambiguity code")

    @staticmethod
    def _manifest_options(
        rows: Sequence[Mapping[str, JsonValue]],
        *,
        value_key: str,
    ) -> tuple[ClarificationOption, ...]:
        options: list[ClarificationOption] = []
        for row in rows:
            if row.get("availability") != "available":
                continue
            raw_value = row.get(value_key)
            raw_label = row.get("business_name")
            if not isinstance(raw_value, str) or not isinstance(raw_label, str):
                raise TypeError("public capability row contains invalid option fields")
            options.append(
                ClarificationOption(
                    value=raw_value,
                    label=raw_label,
                )
            )
        if not options:
            raise ValueError("public capability manifest contains no available options")
        return tuple(options)

    @staticmethod
    def _validate_answers(
        clarification: ClarificationRequest,
        answers: Sequence[ClarificationAnswer],
    ) -> tuple[ClarificationAnswer, ...]:
        checked = tuple(answers)
        if any(not isinstance(item, ClarificationAnswer) for item in checked):
            raise TypeError("answers must contain ClarificationAnswer values")
        by_id = {item.question_id: item for item in checked}
        if len(by_id) != len(checked):
            raise ValueError("clarification answers must have unique question ids")
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
        return tuple(by_id[item.question_id] for item in clarification.questions)
