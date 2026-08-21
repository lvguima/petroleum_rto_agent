"""Provider-neutral protocols used by solver plugins."""

from __future__ import annotations

from typing import Protocol

from ..contracts.candidate import CandidateEvaluation, CandidateProposal
from ..contracts.problem import OptimizationProblem
from ..contracts.solver_result import SolverResult
from .models import ProblemFeatures, SolverDescriptor, SolverSupport


class CandidateEvaluatorPort(Protocol):
    """Evaluate one proposal without exposing a simulator implementation."""

    def evaluate(self, proposal: CandidateProposal) -> CandidateEvaluation: ...


class SolverPort(Protocol):
    """Discoverable solver plugin with a side-effect-free compatibility check."""

    @property
    def descriptor(self) -> SolverDescriptor: ...

    def supports(self, features: ProblemFeatures) -> SolverSupport: ...

    def solve(
        self,
        problem: OptimizationProblem,
        evaluator: CandidateEvaluatorPort,
    ) -> SolverResult: ...
