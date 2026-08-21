"""Deterministic solver selection over a versioned routing policy."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.common import identifier
from .models import (
    SOLVER_ROUTING_SCHEMA_VERSION,
    ProblemFeatures,
    SolverConsideration,
    SolverRoutingDecision,
    SolverRoutingPolicy,
    SolverSupport,
)
from .port import SolverPort
from .registry import SolverRegistry


@dataclass(frozen=True)
class SolverRoute:
    """Runtime plugin plus the serializable audit decision that selected it."""

    decision: SolverRoutingDecision
    solver: SolverPort | None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, SolverRoutingDecision):
            raise TypeError("decision must be a SolverRoutingDecision")
        if self.decision.status == "selected":
            if self.solver is None:
                raise ValueError("selected route requires a solver instance")
            if self.solver.descriptor.solver_id != self.decision.selected_solver_id:
                raise ValueError("runtime solver differs from the routing decision")
        elif self.solver is not None:
            raise ValueError("unsupported route cannot contain a solver instance")


class SolverRouter:
    """Choose the first supported policy entry, or validate one trusted override."""

    def route(
        self,
        features: ProblemFeatures,
        registry: SolverRegistry,
        policy: SolverRoutingPolicy,
        *,
        trusted_override: str | None = None,
    ) -> SolverRoute:
        if not isinstance(features, ProblemFeatures):
            raise TypeError("features must be ProblemFeatures")
        if not isinstance(registry, SolverRegistry):
            raise TypeError("registry must be SolverRegistry")
        if not isinstance(policy, SolverRoutingPolicy):
            raise TypeError("policy must be SolverRoutingPolicy")
        if trusted_override is not None:
            trusted_override = identifier(trusted_override, context="trusted_override")
            return self._override_route(features, registry, policy, trusted_override)
        return self._policy_route(features, registry, policy)

    def _override_route(
        self,
        features: ProblemFeatures,
        registry: SolverRegistry,
        policy: SolverRoutingPolicy,
        solver_id: str,
    ) -> SolverRoute:
        solver = registry.find(solver_id)
        if solver is None:
            consideration = self._missing(solver_id)
            return SolverRoute(
                decision=self._decision(
                    features,
                    policy,
                    (consideration,),
                    trusted_override=solver_id,
                    selected=None,
                    reason_code="trusted-override-not-registered",
                ),
                solver=None,
            )
        consideration = self._consider(solver, features)
        if not consideration.supported:
            return SolverRoute(
                decision=self._decision(
                    features,
                    policy,
                    (consideration,),
                    trusted_override=solver_id,
                    selected=None,
                    reason_code="trusted-override-unsupported",
                ),
                solver=None,
            )
        return SolverRoute(
            decision=self._decision(
                features,
                policy,
                (consideration,),
                trusted_override=solver_id,
                selected=solver,
                reason_code="trusted-override-selected",
            ),
            solver=solver,
        )

    def _policy_route(
        self,
        features: ProblemFeatures,
        registry: SolverRegistry,
        policy: SolverRoutingPolicy,
    ) -> SolverRoute:
        considerations: list[SolverConsideration] = []
        selected: SolverPort | None = None
        for solver_id in policy.solver_order:
            solver = registry.find(solver_id)
            consideration = (
                self._missing(solver_id) if solver is None else self._consider(solver, features)
            )
            considerations.append(consideration)
            if selected is None and solver is not None and consideration.supported:
                selected = solver
        if selected is None:
            return SolverRoute(
                decision=self._decision(
                    features,
                    policy,
                    tuple(considerations),
                    trusted_override=None,
                    selected=None,
                    reason_code="no-compatible-solver",
                ),
                solver=None,
            )
        return SolverRoute(
            decision=self._decision(
                features,
                policy,
                tuple(considerations),
                trusted_override=None,
                selected=selected,
                reason_code="policy-order-selected",
            ),
            solver=selected,
        )

    @staticmethod
    def _consider(solver: SolverPort, features: ProblemFeatures) -> SolverConsideration:
        support = solver.supports(features)
        if not isinstance(support, SolverSupport):
            raise TypeError("solver supports() must return SolverSupport")
        descriptor = solver.descriptor
        return SolverConsideration(
            solver_id=descriptor.solver_id,
            solver_version=descriptor.solver_version,
            solver_fingerprint=descriptor.fingerprint,
            supported=support.supported,
            reason_codes=support.reason_codes,
        )

    @staticmethod
    def _missing(solver_id: str) -> SolverConsideration:
        return SolverConsideration(
            solver_id=solver_id,
            solver_version=None,
            solver_fingerprint=None,
            supported=False,
            reason_codes=("solver-not-registered",),
        )

    @staticmethod
    def _decision(
        features: ProblemFeatures,
        policy: SolverRoutingPolicy,
        considerations: tuple[SolverConsideration, ...],
        *,
        trusted_override: str | None,
        selected: SolverPort | None,
        reason_code: str,
    ) -> SolverRoutingDecision:
        descriptor = None if selected is None else selected.descriptor
        return SolverRoutingDecision(
            schema_version=SOLVER_ROUTING_SCHEMA_VERSION,
            routing_version="solver-routing-decision-v1",
            status="unsupported" if descriptor is None else "selected",
            features_fingerprint=features.fingerprint,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_fingerprint=policy.fingerprint,
            trusted_override=trusted_override,
            selected_solver_id=None if descriptor is None else descriptor.solver_id,
            selected_solver_version=None if descriptor is None else descriptor.solver_version,
            selected_solver_fingerprint=None if descriptor is None else descriptor.fingerprint,
            considerations=considerations,
            reason_code=reason_code,
        )
