from __future__ import annotations

from petroleum_rto.rto.capabilities import (
    build_solver_routing_policy,
    load_capability_bundle,
)
from petroleum_rto.rto.contracts.context import (
    OPERATING_CONTEXT_SCHEMA_ID,
    OPERATING_CONTEXT_SCHEMA_VERSION,
    OperatingContext,
)
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.problem import ProblemFeatureAnalyzer, UnifiedProblemBuilder
from petroleum_rto.rto.solvers import (
    CoarseRefineGridSolver,
    FullGridParetoSolver,
    SolverRegistry,
    SolverRouter,
)
from petroleum_rto.rto.unified_inputs import OptimizationIntent
from tests.rto.unit.test_unified_intent import _raw as _intent_raw


def _context(bundle) -> OperatingContext:
    return OperatingContext(
        schema_id=OPERATING_CONTEXT_SCHEMA_ID,
        schema_version=OPERATING_CONTEXT_SCHEMA_VERSION,
        context_version="case-20260604",
        context_id="case-20260604-nominal",
        context_schema_ref=bundle.context_schema.ref,
        provider_id="cdu-m7",
        model_ref=ContractRef("cdu-effective-model", "1" * 64),
        case_ref=ContractRef("case-20260604-effective", "2" * 64),
        operating_mode="normal-steady",
        facts={
            "fresh_feed_load_kg_s": 113.1388888888889,
            "feed_composition": {"naphtha": 0.2, "residue": 0.8},
        },
        current_setpoints={
            "furnace_temperature_target_k": 628.35,
            "tower_top_pressure_target_pa_a": 152325.0,
        },
        initial_state={
            "flash_drum": 1.0,
            "reflux_drum": 1.0,
            "tower_bottom": 1.0,
        },
        data_timestamp="2026-06-04T09:16:00+08:00",
        data_quality="weak-time-alignment",
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def test_same_builder_constructs_single_and_multi_problems_and_routes(repo_root) -> None:
    bundle = load_capability_bundle(repo_root)
    context = _context(bundle)
    builder = UnifiedProblemBuilder()
    single = builder.build(
        bundle,
        OptimizationIntent.from_mapping(_intent_raw(multi=False)),
        context,
    )
    multi = builder.build(
        bundle,
        OptimizationIntent.from_mapping(_intent_raw(multi=True)),
        context,
    )

    assert isinstance(single, OptimizationProblem) and isinstance(multi, OptimizationProblem)
    assert len(single.objectives) == 1
    assert len(multi.objectives) == 3
    assert single.solve_requirements.maximum_evaluations == 33
    assert multi.solve_requirements.maximum_evaluations == 81
    assert single.publishability_constraints == multi.publishability_constraints
    assert tuple(item.constraint_id for item in single.publishability_constraints) == (
        "minimum-publishable-energy-improvement",
    )
    assert all(
        item.source == "system" and item.evaluation_stage == "post_selection"
        for item in single.publishability_constraints
    )
    assert not (
        {item.constraint_id for item in single.hard_constraints}
        & {item.constraint_id for item in single.publishability_constraints}
    )
    assert OptimizationProblem.from_mapping(single.as_dict()) == single
    assert OptimizationProblem.from_mapping(multi.as_dict()) == multi
    assert "profile" not in str(single.as_dict()).lower()
    assert "algorithm" not in str(multi.as_dict()).lower()

    registry = SolverRegistry((CoarseRefineGridSolver(), FullGridParetoSolver()))
    policy = build_solver_routing_policy(bundle)
    single_route = SolverRouter().route(ProblemFeatureAnalyzer().analyze(single), registry, policy)
    multi_route = SolverRouter().route(ProblemFeatureAnalyzer().analyze(multi), registry, policy)

    assert single_route.decision.selected_solver_id == "coarse-grid-local-refine"
    assert multi_route.decision.selected_solver_id == "deterministic-full-grid"


def test_all_objective_and_output_shapes_route_without_schema_forks(repo_root) -> None:
    bundle = load_capability_bundle(repo_root)
    context = _context(bundle)
    builder = UnifiedProblemBuilder()
    registry = SolverRegistry((CoarseRefineGridSolver(), FullGridParetoSolver()))
    policy = build_solver_routing_policy(bundle)
    expected = {
        (False, False): ("selected", "coarse-grid-local-refine"),
        (False, True): ("ranked-and-selected", "coarse-grid-local-refine"),
        (True, False): ("selected", "deterministic-full-grid"),
        (True, True): ("pareto-and-selected", "deterministic-full-grid"),
    }

    for (multi, include_alternatives), (result_mode, solver_id) in expected.items():
        raw = _intent_raw(multi=multi)
        raw["result_request"] = {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": include_alternatives,
            "max_candidates": 2 if include_alternatives else 1,
        }
        problem = builder.build(bundle, OptimizationIntent.from_mapping(raw), context)
        route = SolverRouter().route(
            ProblemFeatureAnalyzer().analyze(problem),
            registry,
            policy,
        )

        assert problem.result_request.mode == result_mode
        assert route.decision.selected_solver_id == solver_id


def test_builder_allows_atomic_decision_subset_without_a_profile(repo_root) -> None:
    bundle = load_capability_bundle(repo_root)
    raw = _intent_raw(multi=False)
    raw["decision_variables"] = ["furnace_temperature_target_k"]

    problem = UnifiedProblemBuilder().build(
        bundle,
        OptimizationIntent.from_mapping(raw),
        _context(bundle),
    )

    assert tuple(item.variable_id for item in problem.decision_domains) == (
        "furnace_temperature_target_k",
    )
    registry = SolverRegistry((CoarseRefineGridSolver(), FullGridParetoSolver()))
    policy = build_solver_routing_policy(bundle)
    route = SolverRouter().route(ProblemFeatureAnalyzer().analyze(problem), registry, policy)
    assert route.decision.selected_solver_id == "coarse-grid-local-refine"


def test_builder_requires_trusted_context_fields_and_matching_schema(repo_root) -> None:
    bundle = load_capability_bundle(repo_root)
    intent = OptimizationIntent.from_mapping(_intent_raw(multi=False))
    context = _context(bundle)
    missing = OperatingContext(
        **{
            **context.__dict__,
            "facts": {"feed_composition": {"naphtha": 0.2, "residue": 0.8}},
        }
    )

    try:
        UnifiedProblemBuilder().build(bundle, intent, missing)
    except ValueError as exc:
        assert "fresh_feed_load_kg_s" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("builder accepted an incomplete trusted context")
