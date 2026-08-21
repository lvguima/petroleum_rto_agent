from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.rto.solvers import (
    SOLVER_ROUTING_SCHEMA_VERSION,
    CandidateEvaluatorPort,
    ProblemFeatures,
    SolverDescriptor,
    SolverRegistry,
    SolverRouter,
    SolverRoutingPolicy,
    SolverSupport,
)


class _Solver:
    def __init__(
        self,
        solver_id: str,
        *,
        supports_objective_counts: tuple[int, ...],
        deterministic: bool = True,
    ) -> None:
        self._descriptor = SolverDescriptor(
            solver_id=solver_id,
            solver_version=f"{solver_id}-1.0.0",
            deterministic=deterministic,
            supported_result_modes=("pareto-set", "selected-solution"),
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


def _features(*, objective_count: int = 1) -> ProblemFeatures:
    return ProblemFeatures(
        objective_count=objective_count,
        decision_count=2,
        bounded=True,
        grid_cardinality=81,
        result_mode="selected-solution" if objective_count == 1 else "pareto-set",
        deterministic=True,
        maximum_evaluations=81,
        gradient_availability="none",
        evaluator_kind="paired-black-box",
        dynamic_verification_required=True,
    )


def _policy(*solver_order: str) -> SolverRoutingPolicy:
    return SolverRoutingPolicy(
        schema_version=SOLVER_ROUTING_SCHEMA_VERSION,
        policy_version="routing-policy-1.0.0",
        policy_id="default-routing-policy",
        solver_order=solver_order,
    )


def test_problem_features_are_strict_and_fingerprinted() -> None:
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
        "gradient_availability": "none",
        "evaluator_kind": "paired-black-box",
        "dynamic_verification_required": True,
    }
    assert first.fingerprint == second.fingerprint
    with pytest.raises(ValueError, match="objective_count"):
        ProblemFeatures(
            0,
            2,
            True,
            25,
            "selected-solution",
            True,
            33,
            "none",
            "paired-black-box",
            True,
        )
    with pytest.raises(TypeError, match="bounded"):
        ProblemFeatures(  # type: ignore[arg-type]
            1,
            2,
            1,
            25,
            "selected-solution",
            True,
            33,
            "none",
            "paired-black-box",
            True,
        )


def test_registry_rejects_duplicate_solver_ids_and_sorts_descriptors() -> None:
    second = _Solver("solver-b", supports_objective_counts=(1,))
    first = _Solver("solver-a", supports_objective_counts=(1,))
    registry = SolverRegistry((second, first))

    assert tuple(item.solver_id for item in registry.descriptors()) == (
        "solver-a",
        "solver-b",
    )
    assert registry.get("solver-a") is first
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_Solver("solver-a", supports_objective_counts=(1, 2)))


def test_policy_order_selects_stably_independent_of_registration_order() -> None:
    first = _Solver("scalar-a", supports_objective_counts=(1,))
    preferred = _Solver("scalar-b", supports_objective_counts=(1,))
    features = _features()
    policy = _policy("scalar-b", "scalar-a")

    left = SolverRouter().route(features, SolverRegistry((first, preferred)), policy)
    right = SolverRouter().route(features, SolverRegistry((preferred, first)), policy)

    assert left.solver is preferred
    assert right.solver is preferred
    assert left.decision == right.decision
    assert left.decision.status == "selected"
    assert left.decision.selected_solver_id == "scalar-b"
    assert tuple(item.solver_id for item in left.decision.considerations) == (
        "scalar-b",
        "scalar-a",
    )
    assert left.decision.fingerprint == right.decision.fingerprint


def test_router_returns_structured_unsupported_without_solving() -> None:
    scalar = _Solver("scalar-grid", supports_objective_counts=(1,))
    route = SolverRouter().route(
        _features(objective_count=3),
        SolverRegistry((scalar,)),
        _policy("missing-solver", "scalar-grid"),
    )

    assert route.solver is None
    assert route.decision.status == "unsupported"
    assert route.decision.reason_code == "no-compatible-solver"
    assert route.decision.selected_solver_id is None
    assert route.decision.considerations[0].reason_codes == ("solver-not-registered",)
    assert route.decision.considerations[1].reason_codes == ("objective-count-unsupported",)


def test_trusted_override_is_supported_checked_and_audited() -> None:
    scalar = _Solver("scalar-grid", supports_objective_counts=(1,))
    pareto = _Solver("pareto-grid", supports_objective_counts=(2, 3))
    registry = SolverRegistry((scalar, pareto))
    policy = _policy("scalar-grid")

    selected = SolverRouter().route(
        _features(objective_count=3),
        registry,
        policy,
        trusted_override="pareto-grid",
    )
    assert selected.solver is pareto
    assert selected.decision.reason_code == "trusted-override-selected"
    assert selected.decision.trusted_override == "pareto-grid"

    rejected = SolverRouter().route(
        _features(objective_count=3),
        registry,
        policy,
        trusted_override="scalar-grid",
    )
    assert rejected.solver is None
    assert rejected.decision.status == "unsupported"
    assert rejected.decision.reason_code == "trusted-override-unsupported"

    missing = SolverRouter().route(
        _features(),
        registry,
        policy,
        trusted_override="not-registered",
    )
    assert missing.solver is None
    assert missing.decision.reason_code == "trusted-override-not-registered"


def test_deterministic_requirement_is_part_of_solver_support() -> None:
    stochastic = _Solver(
        "stochastic-search",
        supports_objective_counts=(1,),
        deterministic=False,
    )

    route = SolverRouter().route(
        _features(),
        SolverRegistry((stochastic,)),
        _policy("stochastic-search"),
    )

    assert route.solver is None
    assert route.decision.considerations[0].reason_codes == ("determinism-unsupported",)


def test_solver_layer_has_no_simulator_or_cdu_dependency() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    solver_root = repository_root / "src" / "petroleum_rto" / "rto" / "solvers"
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(solver_root.glob("*.py"))
    )

    assert "petroleum_rto.cdu" not in sources
    assert "SimulatorPort" not in sources
