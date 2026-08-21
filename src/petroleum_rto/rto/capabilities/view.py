"""Adapter from the internal capability bundle to intent negotiation."""

from __future__ import annotations

from ..contracts.reference import ContractRef
from ..intent.models import ObjectiveSense, PreferenceRequest, ResultRequest
from .models import CapabilityBundle, ExecutionRoute


class BundleCapabilityView:
    """Expose only the semantic checks required by IntentResolver."""

    def __init__(self, bundle: CapabilityBundle) -> None:
        if not isinstance(bundle, CapabilityBundle):
            raise TypeError("bundle must be CapabilityBundle")
        self._bundle = bundle

    def objective_sense(self, metric_id: str) -> ObjectiveSense | None:
        for item in self._bundle.catalog.objectives:
            if item.metric_id == metric_id and item.availability == "available":
                return item.sense
        return None

    def supports_decision_variable(self, variable_id: str) -> bool:
        return any(
            item.decision_id == variable_id and item.availability == "available"
            for item in self._bundle.catalog.decisions
        )

    def supports_constraint(self, constraint_id: str) -> bool:
        return any(
            item.guardrail_id == constraint_id and item.availability == "available"
            for item in self._bundle.catalog.guardrails
        )

    def supports_preference(
        self,
        preference: PreferenceRequest,
        objective_ids: tuple[str, ...],
    ) -> bool:
        count = len(objective_ids)
        return preference.objective_order == objective_ids and any(
            item.method == preference.method
            and item.availability == "available"
            and item.minimum_objectives <= count <= item.maximum_objectives
            for item in self._bundle.catalog.selectors
        )

    def supports_result_request(
        self,
        result_request: ResultRequest,
        objective_ids: tuple[str, ...],
    ) -> bool:
        if result_request.output_kind != "steady-setpoint-vector":
            return False
        route = self.route_for_objective_count(len(objective_ids))
        if route is None or result_request.max_candidates > route.top_k:
            return False
        return result_request.include_alternatives or result_request.max_candidates == 1

    def route_for_objective_count(self, objective_count: int) -> ExecutionRoute | None:
        matches = tuple(
            item
            for item in self._bundle.system_policy.execution_routes
            if item.minimum_objectives <= objective_count <= item.maximum_objectives
        )
        if len(matches) > 1:
            raise ValueError("system policy contains overlapping execution routes")
        return None if not matches else matches[0]

    def route_by_ref(self, route_ref: ContractRef) -> ExecutionRoute:
        """Resolve exactly one immutable route already bound into a problem."""

        if not isinstance(route_ref, ContractRef):
            raise TypeError("route_ref must be ContractRef")
        matches = tuple(
            item for item in self._bundle.system_policy.execution_routes if item.ref == route_ref
        )
        if len(matches) != 1:
            raise ValueError("execution route reference is not present exactly once in policy")
        return matches[0]
