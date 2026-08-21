"""Build one immutable OptimizationProblemV1 without solving or simulation."""

from __future__ import annotations

import math

from ..catalogs import RtoCatalogBundle
from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    DecisionDomainV1,
    OptimizationProblemV1,
)


class ProblemBuilder:
    """Pure deterministic assembly of an RTO problem from strict inputs."""

    def build(self, bundle: RtoCatalogBundle) -> OptimizationProblemV1:
        if not isinstance(bundle, RtoCatalogBundle):
            raise TypeError("ProblemBuilder requires an RtoCatalogBundle")
        intent = bundle.intent
        context = bundle.context
        decision_catalog = bundle.decision_catalog
        kpi_catalog = bundle.kpi_catalog
        constraint_profile = bundle.constraint_profile
        policy = bundle.policy

        if intent.operating_context_ref != context.ref:
            raise ValueError("intent operating context reference differs from the loaded context")
        if intent.decision_profile_id != decision_catalog.catalog_id:
            raise ValueError("intent decision profile differs from the loaded catalog")
        if intent.constraint_profile_id != constraint_profile.profile_id:
            raise ValueError("intent constraint profile differs from the loaded profile")
        if intent.priority_profile_id != policy.priority_profile_id:
            raise ValueError("intent priority profile differs from the loaded policy")
        if intent.requested_output != "steady-setpoint-vector":
            raise ValueError("RTO V1 only supports a steady setpoint vector")
        if intent.context_policy != "feed-as-fixed-context":
            raise ValueError("RTO V1 requires feed to remain fixed context")

        objective = kpi_catalog.by_id(intent.objective_metric_id)
        if objective.stage != "M2" or objective.direction != intent.objective_sense:
            raise ValueError("objective KPI cannot support the requested stage or direction")
        for rule in constraint_profile.rules:
            kpi_catalog.by_id(rule.metric_id)

        feed = decision_catalog.by_id("fresh_feed_load_kg_s")
        if feed.role != "context" or feed.enabled:
            raise ValueError("fresh feed must remain a disabled context variable")
        if not feed.lower_bound <= context.feed_mass_flow_kg_s <= feed.upper_bound:
            raise ValueError("context feed is outside the sampled decision catalog domain")

        enabled = tuple(
            item for item in decision_catalog.variables if item.enabled and item.role == "decision"
        )
        if tuple(item.variable_id for item in enabled) != (
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        ):
            raise ValueError("RTO V1 must enable exactly the two frozen decisions")
        domains: list[DecisionDomainV1] = []
        for item in enabled:
            if item.m2_parameter is None or item.m4_loop is None:
                raise ValueError(f"decision {item.variable_id!r} lacks an end-to-end mapping")
            context_nominal = context.current_setpoints.get(item.variable_id)
            if context_nominal is None or not math.isclose(
                context_nominal, item.nominal_value, rel_tol=0.0, abs_tol=1e-9
            ):
                raise ValueError(f"context nominal differs for {item.variable_id!r}")
            domains.append(
                DecisionDomainV1(
                    variable_id=item.variable_id,
                    display_unit=item.display_unit,
                    canonical_unit=item.canonical_unit,
                    nominal_value=item.nominal_value,
                    lower_bound=item.lower_bound,
                    upper_bound=item.upper_bound,
                    coarse_step=item.coarse_step,
                    refine_step=item.refine_step,
                )
            )

        return OptimizationProblemV1(
            schema_version=RTO_SCHEMA_VERSION,
            problem_version="optimization-problem-v1",
            intent_ref=intent.ref,
            context_ref=context.ref,
            decision_catalog_ref=decision_catalog.ref,
            kpi_catalog_ref=kpi_catalog.ref,
            constraint_profile_ref=constraint_profile.ref,
            policy_ref=policy.ref,
            decision_domains=tuple(domains),
            objective_metric_id=intent.objective_metric_id,
            objective_sense=intent.objective_sense,
            constraints=constraint_profile.rules,
            evaluation_plan=policy.evaluation,
            search_plan=policy.search,
            claim_scope=CLAIM_SCOPE,
        )
