"""Deterministic RTO V2 multi-objective problem construction."""

from __future__ import annotations

import math

from ..catalogs import RtoCatalogBundleV2
from ..contracts import (
    CLAIM_SCOPE,
    RTO_V2_SCHEMA_VERSION,
    DecisionDomainV1,
    OptimizationProblemV2,
    ResolvedOptimizationIntentV2,
)


class MultiObjectiveProblemBuilder:
    """Build one immutable V2 problem without search, simulation, or inference."""

    def build(
        self,
        bundle: RtoCatalogBundleV2,
        intent: ResolvedOptimizationIntentV2,
    ) -> OptimizationProblemV2:
        if not isinstance(bundle, RtoCatalogBundleV2):
            raise TypeError("bundle must be RtoCatalogBundleV2")
        if not isinstance(intent, ResolvedOptimizationIntentV2):
            raise TypeError("intent must be ResolvedOptimizationIntentV2")
        base = bundle.base
        policy = bundle.policy
        context = base.context
        if intent.operating_context_ref != context.ref:
            raise ValueError("resolved intent references another operating context")
        if policy.objective_catalog_id != bundle.objective_catalog.catalog_id:
            raise ValueError("policy objective catalog differs from loaded catalog")
        if policy.preference_catalog_id != bundle.preference_catalog.catalog_id:
            raise ValueError("policy preference catalog differs from loaded catalog")
        if policy.decision_profile_id != base.decision_catalog.catalog_id:
            raise ValueError("policy decision profile differs from loaded catalog")
        if policy.constraint_profile_id != base.constraint_profile.profile_id:
            raise ValueError("policy constraint profile differs from loaded profile")
        if intent.decision_profile_id != policy.decision_profile_id:
            raise ValueError("intent decision profile differs from policy")
        if intent.constraint_profile_id != policy.constraint_profile_id:
            raise ValueError("intent constraint profile differs from policy")
        if intent.publishability_profile_id != policy.publishability_profile_id:
            raise ValueError("intent publishability profile differs from policy")
        if intent.requested_output != policy.requested_output:
            raise ValueError("intent requested output differs from policy")
        if intent.context_policy != policy.context_policy:
            raise ValueError("intent context policy differs from policy")
        if intent.objective_profile_id != policy.objective_profile_id:
            raise ValueError("intent objective profile differs from policy")
        if intent.selection_profile_id != policy.selection_profile_id:
            raise ValueError("intent selection profile differs from policy")
        if intent.max_returned_candidates > policy.evaluation.top_k:
            raise ValueError("intent requests more candidates than the dynamic shortlist")

        objective_profile = bundle.objective_catalog.profile_by_id(intent.objective_profile_id)
        if tuple(item.metric_id for item in intent.objectives) != objective_profile.objective_ids:
            raise ValueError("intent objectives differ from the objective profile")
        for objective in intent.objectives:
            definition = bundle.objective_catalog.objective_by_id(objective.metric_id)
            kpi = base.kpi_catalog.by_id(objective.metric_id)
            if (
                objective.sense != definition.sense
                or objective.unit != definition.unit
                or objective.kpi_formula_id != definition.kpi_formula_id
                or objective.normalization_scale != definition.normalization_scale
                or objective.relative_improvement_policy != definition.relative_improvement_policy
            ):
                raise ValueError("resolved objective differs from the objective catalog")
            if kpi.stage != "M2" or kpi.direction != objective.sense:
                raise ValueError("objective KPI cannot support the requested stage or direction")
            if kpi.unit != objective.unit or kpi.formula_id != objective.kpi_formula_id:
                raise ValueError("objective definition differs from the KPI catalog")

        preference = bundle.preference_catalog.profile_by_id(intent.selection_profile_id)
        if preference.objective_order != tuple(item.metric_id for item in intent.objectives):
            raise ValueError("preference order differs from objective priority tiers")
        publishability = bundle.publishability_catalog.profile_by_id(
            intent.publishability_profile_id
        )
        if publishability.metric_id not in objective_profile.objective_ids:
            raise ValueError("publishability metric is absent from the objective profile")
        for rule in base.constraint_profile.rules:
            base.kpi_catalog.by_id(rule.metric_id)

        feed = base.decision_catalog.by_id("fresh_feed_load_kg_s")
        if feed.role != "context" or feed.enabled:
            raise ValueError("fresh feed must remain a disabled context variable")
        if not feed.lower_bound <= context.feed_mass_flow_kg_s <= feed.upper_bound:
            raise ValueError("context feed is outside the sampled decision catalog domain")

        enabled = tuple(
            item
            for item in base.decision_catalog.variables
            if item.enabled and item.role == "decision"
        )
        if tuple(item.variable_id for item in enabled) != (
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        ):
            raise ValueError("RTO V2 must enable exactly the two frozen decisions")
        domains: list[DecisionDomainV1] = []
        for variable in enabled:
            if variable.m2_parameter is None or variable.m4_loop is None:
                raise ValueError(f"decision {variable.variable_id!r} lacks an end-to-end mapping")
            nominal = context.current_setpoints.get(variable.variable_id)
            if nominal is None or not math.isclose(
                nominal, variable.nominal_value, rel_tol=0.0, abs_tol=1e-9
            ):
                raise ValueError(f"context nominal differs for {variable.variable_id!r}")
            point_count = (
                round((variable.upper_bound - variable.lower_bound) / variable.refine_step) + 1
            )
            if point_count != policy.search.points_per_dimension or not math.isclose(
                variable.lower_bound + (point_count - 1) * variable.refine_step,
                variable.upper_bound,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError("decision domain does not define the frozen V2 grid")
            domains.append(
                DecisionDomainV1(
                    variable_id=variable.variable_id,
                    display_unit=variable.display_unit,
                    canonical_unit=variable.canonical_unit,
                    nominal_value=variable.nominal_value,
                    lower_bound=variable.lower_bound,
                    upper_bound=variable.upper_bound,
                    coarse_step=variable.coarse_step,
                    refine_step=variable.refine_step,
                )
            )
        if (
            policy.search.points_per_dimension ** len(domains)
            != policy.search.maximum_m2_candidates
        ):
            raise ValueError("multi-objective search budget differs from the decision grid")

        return OptimizationProblemV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            problem_version="optimization-problem-v2",
            intent_ref=intent.ref,
            context_ref=context.ref,
            decision_catalog_ref=base.decision_catalog.ref,
            kpi_catalog_ref=base.kpi_catalog.ref,
            constraint_profile_ref=base.constraint_profile.ref,
            policy_ref=policy.ref,
            objective_catalog_ref=bundle.objective_catalog.ref,
            preference_catalog_ref=bundle.preference_catalog.ref,
            preference_profile_id=preference.profile_id,
            publishability_catalog_ref=bundle.publishability_catalog.ref,
            publishability_profile_id=publishability.profile_id,
            decision_domains=tuple(domains),
            objectives=intent.objectives,
            constraints=base.constraint_profile.rules,
            evaluation_plan=policy.evaluation,
            search_plan=policy.search,
            claim_scope=CLAIM_SCOPE,
        )
