"""Neutral solver plugin discovery and deterministic routing."""

from .models import (
    SOLVER_ROUTING_SCHEMA_VERSION,
    ProblemFeatures,
    SolverConsideration,
    SolverDescriptor,
    SolverRoutingDecision,
    SolverRoutingPolicy,
    SolverSupport,
)
from .pareto_grid import FullGridParetoSolver
from .port import CandidateEvaluatorPort, SolverPort
from .registry import SolverRegistry
from .router import SolverRoute, SolverRouter
from .scalar_grid import CoarseRefineGridSolver

__all__ = [
    "SOLVER_ROUTING_SCHEMA_VERSION",
    "CandidateEvaluatorPort",
    "CoarseRefineGridSolver",
    "FullGridParetoSolver",
    "ProblemFeatures",
    "SolverConsideration",
    "SolverDescriptor",
    "SolverPort",
    "SolverRegistry",
    "SolverRoute",
    "SolverRouter",
    "SolverRoutingDecision",
    "SolverRoutingPolicy",
    "SolverSupport",
]
