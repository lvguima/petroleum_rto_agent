"""Deterministic selection of the one algorithm named by an execution route."""

from __future__ import annotations

from dataclasses import dataclass

from ..capabilities.models import ExecutionRoute
from ..contracts.problem import OptimizationProblem
from .models import (
    SOLVER_ROUTING_SCHEMA_VERSION,
    ProblemFeatures,
    SolverRoutingDecision,
    SolverSupport,
)
from .port import SolverPort
from .registry import SolverRegistry


@dataclass(frozen=True)
class SolverRoute:
    """Runtime plugin plus the serializable decision that selected it."""

    decision: SolverRoutingDecision
    solver: SolverPort | None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, SolverRoutingDecision):
            raise TypeError("decision must be a SolverRoutingDecision")
        if self.decision.status == "selected":
            if self.solver is None:
                raise ValueError("selected route requires a solver instance")
            descriptor = self.solver.descriptor
            if (
                descriptor.solver_id != self.decision.selected_solver_id
                or descriptor.solver_version != self.decision.selected_solver_version
            ):
                raise ValueError("runtime solver differs from the routing decision")
        elif self.solver is not None:
            raise ValueError("unsupported route cannot contain a solver instance")


class SolverRouter:
    """Check exactly the algorithm and version named by the problem's route."""

    def route(
        self,
        problem: OptimizationProblem,
        features: ProblemFeatures,
        registry: SolverRegistry,
        execution_route: ExecutionRoute,
    ) -> SolverRoute:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be OptimizationProblem")
        if not isinstance(features, ProblemFeatures):
            raise TypeError("features must be ProblemFeatures")
        if not isinstance(registry, SolverRegistry):
            raise TypeError("registry must be SolverRegistry")
        if not isinstance(execution_route, ExecutionRoute):
            raise TypeError("execution_route must be ExecutionRoute")
        if problem.execution_route_ref != execution_route.ref:
            raise ValueError("execution route differs from the immutable problem")

        solver = registry.find(execution_route.search_algorithm_id)
        if solver is None:
            return self._unsupported(
                problem,
                features,
                execution_route,
                ("solver-not-registered",),
            )
        descriptor = solver.descriptor
        if descriptor.solver_version != execution_route.search_algorithm_version:
            return self._unsupported(
                problem,
                features,
                execution_route,
                ("solver-version-mismatch",),
            )
        support = solver.supports(features)
        if not isinstance(support, SolverSupport):
            raise TypeError("solver supports() must return SolverSupport")
        if not support.supported:
            return self._unsupported(
                problem,
                features,
                execution_route,
                support.reason_codes,
            )
        return SolverRoute(
            decision=self._decision(
                problem,
                features,
                execution_route,
                selected=solver,
                reason_codes=(),
            ),
            solver=solver,
        )

    def _unsupported(
        self,
        problem: OptimizationProblem,
        features: ProblemFeatures,
        execution_route: ExecutionRoute,
        reason_codes: tuple[str, ...],
    ) -> SolverRoute:
        return SolverRoute(
            decision=self._decision(
                problem,
                features,
                execution_route,
                selected=None,
                reason_codes=reason_codes,
            ),
            solver=None,
        )

    @staticmethod
    def _decision(
        problem: OptimizationProblem,
        features: ProblemFeatures,
        execution_route: ExecutionRoute,
        *,
        selected: SolverPort | None,
        reason_codes: tuple[str, ...],
    ) -> SolverRoutingDecision:
        descriptor = None if selected is None else selected.descriptor
        return SolverRoutingDecision(
            schema_version=SOLVER_ROUTING_SCHEMA_VERSION,
            routing_version="solver-routing-decision",
            status="unsupported" if descriptor is None else "selected",
            problem_ref=problem.ref,
            features_fingerprint=features.fingerprint,
            execution_route_ref=execution_route.ref,
            algorithm_id=execution_route.search_algorithm_id,
            algorithm_version=execution_route.search_algorithm_version,
            selected_solver_id=None if descriptor is None else descriptor.solver_id,
            selected_solver_version=None if descriptor is None else descriptor.solver_version,
            selected_solver_fingerprint=None if descriptor is None else descriptor.fingerprint,
            reason_codes=reason_codes,
        )
