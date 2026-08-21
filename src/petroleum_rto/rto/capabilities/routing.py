"""Deterministically project system execution routes into solver policy."""

from __future__ import annotations

from ..solvers import SOLVER_ROUTING_SCHEMA_VERSION, SolverRoutingPolicy
from .models import UnifiedCapabilityBundle


def build_solver_routing_policy(
    bundle: UnifiedCapabilityBundle,
) -> SolverRoutingPolicy:
    """Create the router policy without exposing solver IDs in business intent."""

    if not isinstance(bundle, UnifiedCapabilityBundle):
        raise TypeError("bundle must be UnifiedCapabilityBundle")
    solver_order: list[str] = []
    for route in bundle.system_policy.execution_routes:
        if route.search_algorithm_id not in solver_order:
            solver_order.append(route.search_algorithm_id)
    return SolverRoutingPolicy(
        schema_version=SOLVER_ROUTING_SCHEMA_VERSION,
        policy_version=bundle.system_policy.policy_version,
        policy_id=f"{bundle.system_policy.policy_id}-solver-routing",
        solver_order=tuple(solver_order),
    )
