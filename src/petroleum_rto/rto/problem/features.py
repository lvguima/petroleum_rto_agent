"""Pure extraction of solver-relevant facts from a unified problem."""

from __future__ import annotations

import math
from typing import Final

from ..contracts.problem import OptimizationProblem
from ..solvers import ProblemFeatures

_RESULT_MODE: Final[dict[str, str]] = {
    "selected": "selected-solution",
    "ranked-and-selected": "ranked-and-selected",
    "pareto-and-selected": "pareto-and-selected",
}


class ProblemFeatureAnalyzer:
    """Describe a problem without selecting or invoking a solver."""

    def analyze(self, problem: OptimizationProblem) -> ProblemFeatures:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be OptimizationProblem")
        cardinality = 1
        for domain in problem.decision_domains:
            intervals = (domain.upper_bound - domain.lower_bound) / domain.refine_step
            rounded = round(intervals)
            if not math.isclose(intervals, rounded, rel_tol=0.0, abs_tol=1e-9):
                cardinality_value: int | None = None
                break
            cardinality *= rounded + 1
        else:
            cardinality_value = cardinality
        return ProblemFeatures(
            objective_count=len(problem.objectives),
            decision_count=len(problem.decision_domains),
            bounded=True,
            grid_cardinality=cardinality_value,
            result_mode=_RESULT_MODE[problem.result_request.mode],
            deterministic=problem.solve_requirements.deterministic_required,
            maximum_evaluations=problem.solve_requirements.maximum_evaluations,
            gradient_availability=problem.solve_requirements.gradient_availability,
            evaluator_kind="paired-black-box",
            dynamic_verification_required=(problem.evaluation_plan.dynamic_verification_required),
        )
