from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.rto.capabilities import ExecutionRoute
from petroleum_rto.rto.problem import ProblemFeatureAnalyzer
from petroleum_rto.rto.solvers import (
    CandidateEvaluatorPort,
    ProblemFeatures,
    SolverDescriptor,
    SolverRegistry,
    SolverRouter,
    SolverRoutingDecision,
    SolverSupport,
)
from tests.rto.unit.test_unified_problem_contract import _objective, _problem


class _Solver:
    def __init__(
        self,
        solver_id: str,
        solver_version: str,
        *,
        supports_objective_counts: tuple[int, ...],
        deterministic: bool = True,
    ) -> None:
        self._descriptor = SolverDescriptor(
            solver_id=solver_id,
            solver_version=solver_version,
            deterministic=deterministic,
            supported_result_modes=(
                "pareto-and-selected",
                "ranked-and-selected",
                "selected-solution",
            ),
        )
        self._counts = supports_objective_counts
        self.support_calls: list[ProblemFeatures] = []

    @property
    def descriptor(self) -> SolverDescriptor:
        return self._descriptor

    def supports(self, features: ProblemFeatures) -> SolverSupport:
        self.support_calls.append(features)
        reasons: list[str] = []
        if features.objective_count not in self._counts:
            reasons.append("objective-count-unsupported")
        if features.result_mode not in self.descriptor.supported_result_modes:
            reasons.append("result-mode-unsupported")
        if features.deterministic and not self.descriptor.deterministic:
            reasons.append("determinism-unsupported")
        return SolverSupport.no(*reasons) if reasons else SolverSupport.yes()

    def solve(self, problem: object, evaluator: CandidateEvaluatorPort) -> object:
        raise AssertionError("routing must not call solve")


def _execution_route(
    algorithm_id: str,
    algorithm_version: str,
    *,
    objective_count: int = 1,
) -> ExecutionRoute:
    return ExecutionRoute(
        route_id=f"route-{algorithm_id}",
        selector_id="single-objective-selector",
        minimum_objectives=objective_count,
        maximum_objectives=objective_count,
        search_algorithm_id=algorithm_id,
        search_algorithm_version=algorithm_version,
        maximum_m2_candidates=81,
        m2_preset_id="steady-baseline",
        m4_preset_id="closed-loop-feed-step",
        m4_event_time_s=600.0,
        m4_duration_s=7200.0,
        m4_time_step_s=1.0,
        top_k=3,
        feed_anchor_ratios=(1.0,),
        tie_breaks=("proposal-fingerprint-asc",),
    )


def _routing_inputs(
    algorithm_id: str = "scalar-grid",
    algorithm_version: str = "1.0.0",
    *,
    objective_count: int = 1,
):
    route = _execution_route(
        algorithm_id,
        algorithm_version,
        objective_count=objective_count,
    )
    objectives = tuple(_objective(f"metric-{index}") for index in range(objective_count))
    problem = replace(_problem(*objectives), execution_route_ref=route.ref)
    return problem, ProblemFeatureAnalyzer().analyze(problem), route


def _features() -> ProblemFeatures:
    return ProblemFeatures(
        objective_count=1,
        decision_count=2,
        bounded=True,
        grid_cardinality=81,
        result_mode="selected-solution",
        deterministic=True,
        maximum_evaluations=81,
    )


def test_problem_features_are_minimal_strict_and_fingerprinted() -> None:
    first = _features()
    second = _features()

    assert first.as_dict() == {
        "objective_count": 1,
        "decision_count": 2,
        "bounded": True,
        "grid_cardinality": 81,
        "result_mode": "selected-solution",
        "deterministic": True,
        "maximum_evaluations": 81,
    }
    assert first.fingerprint == second.fingerprint
    with pytest.raises(ValueError, match="objective_count"):
        replace(first, objective_count=0)
    with pytest.raises(TypeError, match="bounded"):
        replace(first, bounded=1)  # type: ignore[arg-type]


def test_registry_rejects_duplicate_solver_ids_and_sorts_descriptors() -> None:
    second = _Solver("solver-b", "1.0.0", supports_objective_counts=(1,))
    first = _Solver("solver-a", "1.0.0", supports_objective_counts=(1,))
    registry = SolverRegistry((second, first))

    assert tuple(item.solver_id for item in registry.descriptors()) == (
        "solver-a",
        "solver-b",
    )
    assert registry.get("solver-a") is first
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_Solver("solver-a", "1.0.0", supports_objective_counts=(1, 2)))


def test_execution_route_is_the_only_algorithm_source() -> None:
    problem, features, route = _routing_inputs()
    selected = _Solver("scalar-grid", "1.0.0", supports_objective_counts=(1,))
    competing = _Solver("other-grid", "1.0.0", supports_objective_counts=(1, 2, 3))

    left = SolverRouter().route(
        problem,
        features,
        SolverRegistry((competing, selected)),
        route,
    )
    right = SolverRouter().route(
        problem,
        features,
        SolverRegistry((selected, competing)),
        route,
    )

    assert left.solver is selected
    assert right.solver is selected
    assert left.decision == right.decision
    assert left.decision.execution_route_ref == route.ref
    assert left.decision.algorithm_id == "scalar-grid"
    assert competing.support_calls == []


def test_router_never_flattens_other_routes_when_required_solver_is_missing() -> None:
    problem, features, route = _routing_inputs("missing-grid")
    compatible = _Solver("compatible-grid", "1.0.0", supports_objective_counts=(1,))

    result = SolverRouter().route(problem, features, SolverRegistry((compatible,)), route)

    assert result.solver is None
    assert result.decision.status == "unsupported"
    assert result.decision.reason_codes == ("solver-not-registered",)
    assert compatible.support_calls == []


def test_router_requires_the_exact_route_algorithm_version_before_support_check() -> None:
    problem, features, route = _routing_inputs("scalar-grid", "2.0.0")
    wrong_version = _Solver("scalar-grid", "1.0.0", supports_objective_counts=(1,))

    result = SolverRouter().route(problem, features, SolverRegistry((wrong_version,)), route)

    assert result.solver is None
    assert result.decision.reason_codes == ("solver-version-mismatch",)
    assert wrong_version.support_calls == []


def test_router_keeps_solver_support_as_a_secondary_capability_guard() -> None:
    problem, features, route = _routing_inputs(objective_count=3)
    scalar = _Solver("scalar-grid", "1.0.0", supports_objective_counts=(1,))

    result = SolverRouter().route(problem, features, SolverRegistry((scalar,)), route)

    assert result.solver is None
    assert result.decision.reason_codes == ("objective-count-unsupported",)
    assert scalar.support_calls == [features]


def test_routing_decision_round_trips_and_rejects_removed_override_fields() -> None:
    problem, features, route = _routing_inputs()
    solver = _Solver("scalar-grid", "1.0.0", supports_objective_counts=(1,))
    decision = SolverRouter().route(problem, features, SolverRegistry((solver,)), route).decision

    assert SolverRoutingDecision.from_mapping(decision.as_dict()) == decision
    stale = {**decision.as_dict(), "trusted_override": "scalar-grid"}
    with pytest.raises(ValueError, match="fields differ"):
        SolverRoutingDecision.from_mapping(stale)


def test_solver_layer_has_no_simulator_or_cdu_dependency() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    solver_root = repository_root / "src" / "petroleum_rto" / "rto" / "solvers"
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(solver_root.glob("*.py"))
    )

    assert "petroleum_rto.cdu" not in sources
    assert "SimulatorPort" not in sources
