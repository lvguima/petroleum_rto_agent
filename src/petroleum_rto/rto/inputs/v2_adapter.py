"""Validate and bind strict V2 domain intentions to trusted RTO inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from ..catalogs import RtoCatalogBundleV2
from ..contracts import (
    CLAIM_SCOPE,
    RTO_V2_SCHEMA_VERSION,
    ObjectiveSpecV2,
    OptimizationProblemV2,
    ResolvedOptimizationIntentV2,
)
from ..problem import MultiObjectiveProblemBuilder
from .v2_models import (
    DomainOptimizationIntentV2,
    ExternalOptimizationRequestV2,
    IntentValidationIssueV2,
    IntentValidationResultV2,
    RtoCapabilityManifestV2,
)


@dataclass(frozen=True)
class BoundExternalOptimizationRequestV2:
    """Validated V2 input, trusted context, resolved intent, and deterministic problem."""

    external_request: ExternalOptimizationRequestV2
    bundle: RtoCatalogBundleV2
    resolved_intent: ResolvedOptimizationIntentV2
    problem: OptimizationProblemV2


def capability_manifest_v2(bundle: RtoCatalogBundleV2) -> RtoCapabilityManifestV2:
    if not isinstance(bundle, RtoCatalogBundleV2):
        raise TypeError("bundle must be RtoCatalogBundleV2")
    policy = bundle.policy
    return RtoCapabilityManifestV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        manifest_version="rto-capability-manifest-v2",
        objective_catalog_ref=bundle.objective_catalog.ref,
        preference_catalog_ref=bundle.preference_catalog.ref,
        publishability_catalog_ref=bundle.publishability_catalog.ref,
        supported_request_versions=("external-optimization-request-v2",),
        objective_profiles=tuple(item.profile_id for item in bundle.objective_catalog.profiles),
        objectives=tuple(
            MappingProxyType({"metric_id": item.metric_id, "sense": item.sense, "unit": item.unit})
            for item in bundle.objective_catalog.objectives
        ),
        selection_profiles=tuple(item.profile_id for item in bundle.preference_catalog.profiles),
        decision_profiles=(policy.decision_profile_id,),
        business_constraint_profiles=(policy.business_constraint_profile_id,),
        requested_outputs=(policy.requested_output,),
        context_policies=(policy.context_policy,),
        allowed_assumptions=policy.allowed_assumptions,
        maximum_objectives=bundle.objective_catalog.maximum_objectives,
        maximum_returned_candidates=policy.evaluation.top_k,
        claim_scope=CLAIM_SCOPE,
    )


def validate_domain_intent_v2(
    bundle: RtoCatalogBundleV2,
    intent: DomainOptimizationIntentV2,
) -> IntentValidationResultV2:
    """Validate only business intent; never bind context or call a solver."""

    if not isinstance(bundle, RtoCatalogBundleV2):
        raise TypeError("bundle must be RtoCatalogBundleV2")
    if not isinstance(intent, DomainOptimizationIntentV2):
        raise TypeError("intent must be DomainOptimizationIntentV2")
    issues: list[IntentValidationIssueV2] = []
    policy = bundle.policy
    profile_ids = tuple(item.profile_id for item in bundle.objective_catalog.profiles)
    selection_ids = tuple(item.profile_id for item in bundle.preference_catalog.profiles)

    def issue(
        code: str,
        pointer: str,
        message: str,
        supported: tuple[str, ...] = (),
    ) -> None:
        issues.append(
            IntentValidationIssueV2(
                code=code,
                json_pointer=pointer,
                message=message,
                supported_values=supported,
                retryable=True,
            )
        )

    if intent.ambiguities:
        issue(
            "needs-clarification",
            "/ambiguities",
            "ambiguities must be resolved before problem construction",
        )
    if intent.objective_profile_id not in profile_ids:
        issue(
            "unsupported-objective-profile",
            "/objective_profile_id",
            "objective profile is not published by the RTO",
            profile_ids,
        )
    else:
        profile = bundle.objective_catalog.profile_by_id(intent.objective_profile_id)
        requested_ids = tuple(item.metric_id for item in intent.objectives)
        if requested_ids != profile.objective_ids:
            issue(
                "objective-profile-mismatch",
                "/objectives",
                "objective sequence differs from the selected profile",
                profile.objective_ids,
            )
    if len(intent.objectives) > bundle.objective_catalog.maximum_objectives:
        issue(
            "too-many-objectives",
            "/objectives",
            "objective count exceeds the capability maximum",
        )
    for index, requested in enumerate(intent.objectives):
        try:
            definition = bundle.objective_catalog.objective_by_id(requested.metric_id)
        except KeyError:
            issue(
                "unsupported-objective",
                f"/objectives/{index}/metric_id",
                "objective is not published by the RTO",
                tuple(item.metric_id for item in bundle.objective_catalog.objectives),
            )
            continue
        if requested.sense != definition.sense:
            issue(
                "objective-sense-mismatch",
                f"/objectives/{index}/sense",
                "objective direction differs from the catalog",
                (definition.sense,),
            )
    if intent.selection.selection_profile_id not in selection_ids:
        issue(
            "unsupported-selection-profile",
            "/selection/selection_profile_id",
            "selection profile is not published by the RTO",
            selection_ids,
        )
    if intent.selection.max_returned_candidates > policy.evaluation.top_k:
        issue(
            "shortlist-limit-exceeded",
            "/selection/max_returned_candidates",
            "requested candidate count exceeds the dynamic verification budget",
        )
    expected_values = (
        (
            intent.decision_profile_id,
            policy.decision_profile_id,
            "/decision_profile_id",
            "unsupported-decision-profile",
        ),
        (
            intent.business_constraint_profile_id,
            policy.business_constraint_profile_id,
            "/business_constraint_profile_id",
            "unsupported-constraint-profile",
        ),
        (
            intent.requested_output,
            policy.requested_output,
            "/requested_output",
            "unsupported-requested-output",
        ),
        (
            intent.context_policy,
            policy.context_policy,
            "/context_policy",
            "unsupported-context-policy",
        ),
    )
    for actual, expected, pointer, code in expected_values:
        if actual != expected:
            issue(code, pointer, "value is not implemented by the RTO", (expected,))
    unsupported_assumptions = tuple(
        item for item in intent.assumptions if item not in policy.allowed_assumptions
    )
    if unsupported_assumptions:
        issue(
            "unsupported-assumption",
            "/assumptions",
            "one or more assumption codes are not published",
            policy.allowed_assumptions,
        )
    status: Literal["valid", "invalid", "needs_clarification"] = (
        "needs_clarification" if intent.ambiguities else "invalid" if issues else "valid"
    )
    return IntentValidationResultV2(
        valid=not issues,
        status=status,
        audit_fingerprint=intent.audit_fingerprint,
        semantic_fingerprint=intent.semantic_fingerprint,
        issues=tuple(issues),
    )


def resolve_domain_intent_v2(
    bundle: RtoCatalogBundleV2,
    intent: DomainOptimizationIntentV2,
    *,
    operating_context_ref: object,
) -> ResolvedOptimizationIntentV2:
    validation = validate_domain_intent_v2(bundle, intent)
    if not validation.valid:
        codes = ",".join(item.code for item in validation.issues)
        raise ValueError(f"domain intent is not executable: {codes}")
    from ..contracts import ContractRef

    if not isinstance(operating_context_ref, ContractRef):
        raise TypeError("operating_context_ref must be a ContractRef")
    specifications = []
    for requested in intent.objectives:
        definition = bundle.objective_catalog.objective_by_id(requested.metric_id)
        specifications.append(
            ObjectiveSpecV2(
                metric_id=definition.metric_id,
                sense=definition.sense,
                priority_tier=requested.priority_tier,
                unit=definition.unit,
                kpi_formula_id=definition.kpi_formula_id,
                normalization_scale=definition.normalization_scale,
                relative_improvement_policy=definition.relative_improvement_policy,
            )
        )
    return ResolvedOptimizationIntentV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        intent_version="resolved-optimization-intent-v2",
        intent_id=intent.intent_id,
        operating_context_ref=operating_context_ref,
        audit_fingerprint=intent.audit_fingerprint,
        semantic_fingerprint=intent.semantic_fingerprint,
        objective_profile_id=intent.objective_profile_id,
        objectives=tuple(specifications),
        selection_profile_id=intent.selection.selection_profile_id,
        return_pareto_front=intent.selection.return_pareto_front,
        max_returned_candidates=intent.selection.max_returned_candidates,
        decision_profile_id=intent.decision_profile_id,
        constraint_profile_id=bundle.policy.constraint_profile_id,
        publishability_profile_id=bundle.policy.publishability_profile_id,
        requested_output=intent.requested_output,
        context_policy=intent.context_policy,
        claim_scope=CLAIM_SCOPE,
    )


def bind_external_optimization_request_v2(
    base_bundle: RtoCatalogBundleV2,
    request: ExternalOptimizationRequestV2,
) -> BoundExternalOptimizationRequestV2:
    if not isinstance(base_bundle, RtoCatalogBundleV2):
        raise TypeError("base_bundle must be RtoCatalogBundleV2")
    if not isinstance(request, ExternalOptimizationRequestV2):
        raise TypeError("request must be ExternalOptimizationRequestV2")
    context_input = request.operating_context
    if context_input.base_context_ref != base_bundle.base.context.ref:
        raise ValueError("external base_context_ref differs from the trusted context")
    context = replace(
        base_bundle.base.context,
        context_version="external-operating-context-v2",
        context_id=context_input.context_id,
        feed_mass_flow_kg_s=context_input.feed_mass_flow_t_h / 3.6,
        data_timestamp=context_input.data_timestamp,
        data_quality=context_input.data_quality,
    )
    bundle = replace(base_bundle, base=replace(base_bundle.base, context=context))
    resolved = resolve_domain_intent_v2(
        bundle,
        request.optimization_intent,
        operating_context_ref=context.ref,
    )
    problem = MultiObjectiveProblemBuilder().build(bundle, resolved)
    if request.coverage_policy == "sampled-anchors":
        feed = bundle.base.decision_catalog.by_id("fresh_feed_load_kg_s")
        anchor_values = tuple(
            context.feed_mass_flow_kg_s * ratio
            for ratio in problem.evaluation_plan.feed_anchor_ratios
        )
        if any(value < feed.lower_bound or value > feed.upper_bound for value in anchor_values):
            raise ValueError("sampled anchor feed is outside the trusted context domain")
    return BoundExternalOptimizationRequestV2(
        external_request=request,
        bundle=bundle,
        resolved_intent=resolved,
        problem=problem,
    )
