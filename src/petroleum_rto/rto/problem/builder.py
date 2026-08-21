"""Deterministic construction of one objective-count-neutral problem."""

from __future__ import annotations

from ..capabilities import BundleCapabilityView, CapabilityBundle
from ..capabilities.models import GuardrailCapability
from ..contracts.context import OperatingContext
from ..contracts.problem import (
    ENGINEERING_CLAIM_SCOPE,
    OPTIMIZATION_PROBLEM_SCHEMA_ID,
    OPTIMIZATION_PROBLEM_SCHEMA_VERSION,
    ConstraintRule,
    DecisionDomain,
    EvaluationPlan,
    ObjectiveSpec,
    OptimizationProblem,
    ResultMode,
    ResultRequest,
    SelectionPreference,
    SolveRequirements,
)
from ..contracts.reference import ContractRef
from ..intent import IntentResolver, OptimizationIntent


class ProblemBuilder:
    """Bind a resolved business intent to trusted context and immutable policy."""

    def build(
        self,
        bundle: CapabilityBundle,
        intent: OptimizationIntent,
        context: OperatingContext,
    ) -> OptimizationProblem:
        if not isinstance(bundle, CapabilityBundle):
            raise TypeError("bundle must be CapabilityBundle")
        if not isinstance(intent, OptimizationIntent):
            raise TypeError("intent must be OptimizationIntent")
        if not isinstance(context, OperatingContext):
            raise TypeError("context must be OperatingContext")
        view = BundleCapabilityView(bundle)
        resolution = IntentResolver().resolve(intent, view)
        if resolution.status != "resolved":
            codes = ",".join(item.code for item in resolution.issues)
            raise ValueError(f"intent is not resolved: {codes}")
        if intent.constraints:
            raise ValueError(
                "business constraint bindings are not implemented; "
                "published system hard guardrails remain mandatory"
            )

        route = view.route_for_objective_count(len(intent.objectives))
        if route is None:
            raise ValueError("system policy has no execution route for objective count")
        selector = next(
            item for item in bundle.catalog.selectors if item.selector_id == route.selector_id
        )
        if selector.method != intent.preference.method:
            raise ValueError("intent preference differs from the execution route selector")

        decisions = self._decision_domains(bundle, intent, context)
        objectives = self._objective_specs(bundle, intent)
        constraints = self._hard_constraints(bundle)
        publishability_constraints = self._publishability_constraints(bundle)
        result_mode: ResultMode = (
            "selected"
            if not intent.result_request.include_alternatives
            else "pareto-and-selected"
            if len(objectives) > 1
            else "ranked-and-selected"
        )
        return OptimizationProblem(
            schema_id=OPTIMIZATION_PROBLEM_SCHEMA_ID,
            schema_version=OPTIMIZATION_PROBLEM_SCHEMA_VERSION,
            problem_version="optimization-problem-2.0.0",
            intent_ref=self._intent_ref(intent),
            context_ref=context.ref,
            capability_catalog_ref=bundle.catalog.ref,
            system_policy_ref=bundle.system_policy.ref,
            execution_route_ref=route.ref,
            decision_domains=decisions,
            objectives=objectives,
            hard_constraints=constraints,
            publishability_constraints=publishability_constraints,
            preference=SelectionPreference(
                method=intent.preference.method,
                objective_order=tuple(item.metric_id for item in objectives),
                tie_breaks=route.tie_breaks,
            ),
            result_request=ResultRequest(
                mode=result_mode,
                maximum_returned_candidates=intent.result_request.max_candidates,
            ),
            evaluation_plan=EvaluationPlan(
                static_stage="M2",
                dynamic_stage="M4",
                m2_preset_id=route.m2_preset_id,
                m4_preset_id=route.m4_preset_id,
                m4_event_time_s=route.m4_event_time_s,
                m4_duration_s=route.m4_duration_s,
                m4_time_step_s=route.m4_time_step_s,
                dynamic_verification_required=True,
                dynamic_shortlist_size=route.top_k,
                context_anchor_ratios=route.feed_anchor_ratios,
            ),
            solve_requirements=SolveRequirements(
                maximum_evaluations=route.maximum_m2_candidates,
                deterministic_required=True,
            ),
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    @staticmethod
    def _intent_ref(intent: OptimizationIntent) -> ContractRef:
        return ContractRef(intent.intent_id, intent.fingerprint)

    @staticmethod
    def _decision_domains(
        bundle: CapabilityBundle,
        intent: OptimizationIntent,
        context: OperatingContext,
    ) -> tuple[DecisionDomain, ...]:
        by_id = {item.decision_id: item for item in bundle.catalog.decisions}
        domains: list[DecisionDomain] = []
        for variable_id in sorted(intent.decision_variables):
            capability = by_id[variable_id]
            if capability.availability != "available":
                raise ValueError(f"decision {variable_id!r} is not available")
            nominal = context.current_setpoints.get(variable_id)
            if nominal is None:
                raise ValueError(f"context has no current setpoint for {variable_id!r}")
            domains.append(
                DecisionDomain(
                    variable_id=variable_id,
                    display_unit=capability.display_unit,
                    canonical_unit=capability.canonical_unit,
                    nominal_value=nominal,
                    lower_bound=capability.lower_bound,
                    upper_bound=capability.upper_bound,
                    coarse_step=capability.coarse_step,
                    refine_step=capability.refine_step,
                )
            )
        return tuple(domains)

    @staticmethod
    def _objective_specs(
        bundle: CapabilityBundle,
        intent: OptimizationIntent,
    ) -> tuple[ObjectiveSpec, ...]:
        objectives_by_metric = {item.metric_id: item for item in bundle.catalog.objectives}
        metrics_by_id = {item.metric_id: item for item in bundle.catalog.metrics}
        result: list[ObjectiveSpec] = []
        for requested in intent.objectives:
            capability = objectives_by_metric[requested.metric_id]
            metric = metrics_by_id[requested.metric_id]
            if capability.sense != requested.sense:
                raise ValueError("intent objective direction differs from capability")
            result.append(
                ObjectiveSpec(
                    metric_id=requested.metric_id,
                    sense=requested.sense,
                    unit=metric.unit,
                    evaluation_stage=metric.stage,
                    formula_id=metric.formula_ref,
                    normalization_scale=capability.normalization_scale,
                )
            )
        return tuple(result)

    @staticmethod
    def _hard_constraints(bundle: CapabilityBundle) -> tuple[ConstraintRule, ...]:
        guardrails: dict[str, GuardrailCapability] = {
            item.guardrail_id: item for item in bundle.catalog.guardrails
        }
        return tuple(
            ConstraintRule(
                constraint_id=binding.guardrail_id,
                priority=binding.priority,
                metric_id=guardrails[binding.guardrail_id].metric_id,
                evaluation_stage=guardrails[binding.guardrail_id].stage,
                operator=binding.operator,
                limit=binding.limit,
                unit=guardrails[binding.guardrail_id].unit,
                normalization_scale=binding.normalization_scale,
                source="system",
            )
            for binding in sorted(
                bundle.system_policy.hard_guardrails,
                key=lambda item: item.priority,
            )
        )

    @staticmethod
    def _publishability_constraints(
        bundle: CapabilityBundle,
    ) -> tuple[ConstraintRule, ...]:
        guardrails: dict[str, GuardrailCapability] = {
            item.guardrail_id: item for item in bundle.catalog.guardrails
        }
        return tuple(
            ConstraintRule(
                constraint_id=binding.guardrail_id,
                priority=binding.priority,
                metric_id=guardrails[binding.guardrail_id].metric_id,
                evaluation_stage=guardrails[binding.guardrail_id].stage,
                operator=binding.operator,
                limit=binding.limit,
                unit=guardrails[binding.guardrail_id].unit,
                normalization_scale=binding.normalization_scale,
                source="system",
            )
            for binding in sorted(
                bundle.system_policy.publishability_guardrails,
                key=lambda item: item.priority,
            )
        )
