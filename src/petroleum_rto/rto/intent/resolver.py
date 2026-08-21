"""Capability negotiation for the business intent contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ._validation import identifier, text
from .models import ObjectiveSense, OptimizationIntent, PreferenceRequest, ResultRequest

type ResolutionStatus = Literal["resolved", "needs_clarification", "unsupported"]


class CapabilityView(Protocol):
    """Minimum RTO capability surface needed to validate business intent."""

    def objective_sense(self, metric_id: str) -> ObjectiveSense | None: ...

    def supports_decision_variable(self, variable_id: str) -> bool: ...

    def supports_constraint(self, constraint_id: str) -> bool: ...

    def supports_preference(
        self,
        preference: PreferenceRequest,
        objective_ids: tuple[str, ...],
    ) -> bool: ...

    def supports_result_request(
        self,
        result_request: ResultRequest,
        objective_ids: tuple[str, ...],
    ) -> bool: ...


@dataclass(frozen=True)
class IntentResolutionIssue:
    code: str
    json_pointer: str
    message: str
    supported_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", identifier(self.code, context="issue code"))
        if not isinstance(self.json_pointer, str) or not self.json_pointer.startswith("/"):
            raise ValueError("issue json_pointer must start with /")
        object.__setattr__(self, "message", text(self.message, context="issue message"))
        supported = tuple(
            identifier(item, context="supported value") for item in self.supported_values
        )
        if len(supported) != len(set(supported)):
            raise ValueError("issue supported_values must be unique")
        object.__setattr__(self, "supported_values", supported)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "json_pointer": self.json_pointer,
            "message": self.message,
            "supported_values": list(self.supported_values),
        }


@dataclass(frozen=True)
class IntentResolution:
    status: ResolutionStatus
    resolved_intent: OptimizationIntent | None
    issues: tuple[IntentResolutionIssue, ...]

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "needs_clarification", "unsupported"}:
            raise ValueError("unsupported intent resolution status")
        issues = tuple(self.issues)
        if any(not isinstance(item, IntentResolutionIssue) for item in issues):
            raise TypeError("issues must contain IntentResolutionIssue values")
        object.__setattr__(self, "issues", issues)
        if self.status == "resolved":
            if not isinstance(self.resolved_intent, OptimizationIntent) or issues:
                raise ValueError("resolved status requires an intent and no issues")
        elif self.resolved_intent is not None or not issues:
            raise ValueError("unresolved status requires issues and no resolved intent")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "resolved_intent": (
                None if self.resolved_intent is None else self.resolved_intent.as_dict()
            ),
            "issues": [item.as_dict() for item in self.issues],
        }


class IntentResolver:
    """Validate IDs, directions and compatibility without context or solver selection."""

    def resolve(self, intent: OptimizationIntent, capabilities: CapabilityView) -> IntentResolution:
        if not isinstance(intent, OptimizationIntent):
            raise TypeError("intent must be an OptimizationIntent")

        issues: list[IntentResolutionIssue] = [
            IntentResolutionIssue(
                code="needs-clarification",
                json_pointer=f"/ambiguities/{index}",
                message=f"ambiguity {ambiguity!r} must be resolved before negotiation",
            )
            for index, ambiguity in enumerate(intent.ambiguities)
        ]

        objective_ids = tuple(item.metric_id for item in intent.objectives)
        for index, objective in enumerate(intent.objectives):
            supported_sense = capabilities.objective_sense(objective.metric_id)
            if supported_sense is None:
                issues.append(
                    IntentResolutionIssue(
                        code="unsupported-objective",
                        json_pointer=f"/objectives/{index}/metric_id",
                        message="objective is not published by the RTO capability view",
                    )
                )
            elif supported_sense != objective.sense:
                issues.append(
                    IntentResolutionIssue(
                        code="objective-sense-mismatch",
                        json_pointer=f"/objectives/{index}/sense",
                        message="objective direction differs from the published capability",
                        supported_values=(supported_sense,),
                    )
                )

        for index, variable_id in enumerate(intent.decision_variables):
            if not capabilities.supports_decision_variable(variable_id):
                issues.append(
                    IntentResolutionIssue(
                        code="unsupported-decision-variable",
                        json_pointer=f"/decision_variables/{index}",
                        message="decision variable is not published by the RTO capability view",
                    )
                )

        for index, constraint_id in enumerate(intent.constraints):
            if not capabilities.supports_constraint(constraint_id):
                issues.append(
                    IntentResolutionIssue(
                        code="unsupported-constraint",
                        json_pointer=f"/constraints/{index}",
                        message="constraint is not published by the RTO capability view",
                    )
                )

        if not capabilities.supports_preference(intent.preference, objective_ids):
            issues.append(
                IntentResolutionIssue(
                    code="unsupported-preference",
                    json_pointer="/preference",
                    message="preference is incompatible with the requested objective sequence",
                )
            )
        if not capabilities.supports_result_request(intent.result_request, objective_ids):
            issues.append(
                IntentResolutionIssue(
                    code="unsupported-result-request",
                    json_pointer="/result_request",
                    message="result request is incompatible with the requested objectives",
                )
            )

        if intent.ambiguities:
            return IntentResolution(
                status="needs_clarification",
                resolved_intent=None,
                issues=tuple(issues),
            )
        if issues:
            return IntentResolution(
                status="unsupported",
                resolved_intent=None,
                issues=tuple(issues),
            )
        return IntentResolution(status="resolved", resolved_intent=intent, issues=())
