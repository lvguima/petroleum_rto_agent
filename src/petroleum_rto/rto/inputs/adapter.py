"""Bind a strict external request to approved RTO catalogs and profiles."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from ..catalogs import RtoCatalogBundle
from ..contracts import OptimizationIntentV1, OptimizationProblemV1
from ..problem import ProblemBuilder
from .models import ExternalOptimizationRequestV1

type ObjectiveSense = Literal["minimize", "maximize"]

_OBJECTIVE_PROFILES: dict[str, tuple[str, ObjectiveSense]] = {
    "minimize-specific-furnace-energy-v1": (
        "specific_furnace_fuel_energy_mj_per_t",
        "minimize",
    )
}


@dataclass(frozen=True)
class BoundExternalOptimizationRequestV1:
    """Validated external input plus its deterministic internal objects."""

    external_request: ExternalOptimizationRequestV1
    bundle: RtoCatalogBundle
    problem: OptimizationProblemV1


def bind_external_optimization_request(
    base_bundle: RtoCatalogBundle,
    request: ExternalOptimizationRequestV1,
) -> BoundExternalOptimizationRequestV1:
    """Map allowed business fields onto one trusted source-controlled bundle."""

    if not isinstance(base_bundle, RtoCatalogBundle):
        raise TypeError("base_bundle must be RtoCatalogBundle")
    if not isinstance(request, ExternalOptimizationRequestV1):
        raise TypeError("request must be ExternalOptimizationRequestV1")
    context_input = request.operating_context
    intent_input = request.optimization_intent
    if context_input.base_context_ref != base_bundle.context.ref:
        raise ValueError("external base_context_ref differs from the trusted context")
    if intent_input.priority_profile_id != base_bundle.policy.priority_profile_id:
        raise ValueError("external priority_profile_id differs from the trusted policy")
    if intent_input.decision_profile_id != base_bundle.decision_catalog.catalog_id:
        raise ValueError("external decision_profile_id differs from the trusted catalog")
    if intent_input.constraint_profile_id != base_bundle.constraint_profile.profile_id:
        raise ValueError("external constraint_profile_id differs from the trusted profile")
    try:
        objective_metric_id, objective_sense = _OBJECTIVE_PROFILES[
            intent_input.objective_profile_id
        ]
    except KeyError as exc:
        raise ValueError("external objective_profile_id is not implemented") from exc

    context = replace(
        base_bundle.context,
        context_version="external-operating-context-v1",
        context_id=context_input.context_id,
        feed_mass_flow_kg_s=context_input.feed_mass_flow_t_h / 3.6,
        data_timestamp=context_input.data_timestamp,
        data_quality=context_input.data_quality,
    )
    intent = OptimizationIntentV1(
        schema_version=base_bundle.intent.schema_version,
        intent_version="external-optimization-intent-v1",
        intent_id=intent_input.intent_id,
        source_type=intent_input.source_type,
        source_ref=intent_input.source_ref,
        original_text=intent_input.original_text,
        operating_context_ref=context.ref,
        objective_metric_id=objective_metric_id,
        objective_sense=objective_sense,
        priority_profile_id=intent_input.priority_profile_id,
        decision_profile_id=intent_input.decision_profile_id,
        constraint_profile_id=intent_input.constraint_profile_id,
        requested_output=intent_input.requested_output,
        context_policy=intent_input.context_policy,
        claim_scope=base_bundle.intent.claim_scope,
    )
    bundle = replace(base_bundle, context=context, intent=intent)
    problem = ProblemBuilder().build(bundle)
    if request.coverage_policy == "sampled-anchors":
        feed = bundle.decision_catalog.by_id("fresh_feed_load_kg_s")
        anchor_values = tuple(
            context.feed_mass_flow_kg_s * ratio
            for ratio in problem.evaluation_plan.feed_anchor_ratios
        )
        if any(value < feed.lower_bound or value > feed.upper_bound for value in anchor_values):
            raise ValueError("sampled anchor feed is outside the trusted context domain")
    return BoundExternalOptimizationRequestV1(
        external_request=request,
        bundle=bundle,
        problem=problem,
    )
