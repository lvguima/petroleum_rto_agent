from __future__ import annotations

from dataclasses import replace

import pytest

from petroleum_rto.rto.contracts.problem import (
    ENGINEERING_CLAIM_SCOPE,
    OPTIMIZATION_PROBLEM_SCHEMA_ID,
    OPTIMIZATION_PROBLEM_SCHEMA_VERSION,
    ConstraintRule,
    DecisionDomain,
    EvaluationPlan,
    ObjectiveSpec,
    OptimizationProblem,
    ResultRequest,
    SelectionPreference,
    SolveRequirements,
)
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.problem import ProblemFeatureAnalyzer


def _ref(name: str, digit: str) -> ContractRef:
    return ContractRef(name, digit * 64)


def _problem(*objectives: ObjectiveSpec) -> OptimizationProblem:
    return OptimizationProblem(
        schema_id=OPTIMIZATION_PROBLEM_SCHEMA_ID,
        schema_version=OPTIMIZATION_PROBLEM_SCHEMA_VERSION,
        problem_version="optimization-problem-unified",
        intent_ref=_ref("intent", "1"),
        context_ref=_ref("context", "2"),
        capability_catalog_ref=_ref("catalog", "3"),
        system_policy_ref=_ref("policy", "4"),
        decision_domains=(
            DecisionDomain(
                variable_id="furnace_temperature_target_k",
                display_unit="degC",
                canonical_unit="K",
                nominal_value=628.35,
                lower_bound=626.35,
                upper_bound=630.35,
                coarse_step=1.0,
                refine_step=0.5,
            ),
            DecisionDomain(
                variable_id="tower_top_pressure_target_pa_a",
                display_unit="MPa(g)",
                canonical_unit="Pa(a)",
                nominal_value=152325.0,
                lower_bound=150325.0,
                upper_bound=154325.0,
                coarse_step=1000.0,
                refine_step=500.0,
            ),
        ),
        objectives=objectives,
        hard_constraints=(
            ConstraintRule(
                constraint_id="m2-structural-numeric",
                priority=0,
                metric_id="m2_evaluable",
                evaluation_stage="M2",
                operator="eq",
                limit=1.0,
                unit="1",
                normalization_scale=1.0,
                source="system",
            ),
        ),
        publishability_constraints=(
            ConstraintRule(
                constraint_id="minimum-publishable-energy-improvement",
                priority=4,
                metric_id="specific_furnace_fuel_improvement_fraction",
                evaluation_stage="post_selection",
                operator="ge",
                limit=0.005,
                unit="1",
                normalization_scale=0.005,
                source="system",
            ),
        ),
        preference=SelectionPreference(
            method="lexicographic",
            objective_order=tuple(item.metric_id for item in objectives),
            tie_breaks=("proposal-fingerprint-asc",),
        ),
        result_request=ResultRequest(
            mode="selected" if len(objectives) == 1 else "pareto-and-selected",
            maximum_returned_candidates=1 if len(objectives) == 1 else 5,
        ),
        evaluation_plan=EvaluationPlan(
            static_stage="M2",
            dynamic_stage="M4",
            m2_preset_id="steady-baseline",
            m4_preset_id="closed-loop-feed-step",
            m4_event_time_s=600.0,
            m4_duration_s=7200.0,
            m4_time_step_s=1.0,
            dynamic_verification_required=True,
            dynamic_shortlist_size=5,
            context_anchor_ratios=(1.0,),
        ),
        solve_requirements=SolveRequirements(
            maximum_evaluations=81,
            deterministic_required=True,
            gradient_availability="none",
        ),
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _objective(metric_id: str, sense: str = "minimize") -> ObjectiveSpec:
    assert sense in {"minimize", "maximize"}
    return ObjectiveSpec(
        metric_id=metric_id,
        sense=sense,  # type: ignore[arg-type]
        unit="1",
        evaluation_stage="M2",
        formula_id=f"formula-{metric_id}",
        normalization_scale=1.0,
    )


@pytest.mark.parametrize("objective_count", [1, 2, 3])
def test_problem_uses_one_contract_for_one_to_many_objectives(objective_count: int) -> None:
    objectives = tuple(_objective(f"metric-{index}") for index in range(objective_count))
    problem = _problem(*objectives)

    restored = OptimizationProblem.from_mapping(problem.as_dict())

    assert restored == problem
    assert len(restored.objectives) == objective_count
    assert restored.fingerprint == problem.fingerprint
    assert "algorithm" not in str(problem.as_dict()).lower()
    assert "profile" not in str(problem.as_dict()).lower()


def test_problem_rejects_empty_duplicate_and_misaligned_objectives() -> None:
    first = _objective("energy")
    with pytest.raises(ValueError, match="non-empty"):
        _problem()
    with pytest.raises(ValueError, match="unique"):
        _problem(first, first)
    with pytest.raises(ValueError, match="preference"):
        replace(
            _problem(first),
            preference=SelectionPreference(
                method="lexicographic",
                objective_order=("yield",),
                tie_breaks=(),
            ),
        )


def test_problem_rejects_unknown_fields_nonfinite_values_and_tampered_fingerprint() -> None:
    problem = _problem(_objective("energy"))
    raw = problem.as_dict()
    raw["unknown"] = True
    with pytest.raises(ValueError, match="fields differ"):
        OptimizationProblem.from_mapping(raw)

    raw = problem.as_dict()
    raw["decision_domains"][0]["nominal_value"] = float("nan")  # type: ignore[index]
    with pytest.raises(ValueError, match="finite"):
        OptimizationProblem.from_mapping(raw)

    raw = problem.as_dict()
    raw["problem_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        OptimizationProblem.from_mapping(raw)


def test_publishability_constraints_are_strict_and_fingerprinted_separately() -> None:
    problem = _problem(_objective("energy"))
    rule = problem.publishability_constraints[0]

    assert OptimizationProblem.from_mapping(problem.as_dict()) == problem
    assert (
        replace(
            problem,
            publishability_constraints=(replace(rule, limit=0.01),),
        ).fingerprint
        != problem.fingerprint
    )
    with pytest.raises(ValueError, match="post-selection"):
        replace(
            problem,
            publishability_constraints=(replace(rule, evaluation_stage="M2"),),
        )
    with pytest.raises(ValueError, match="system-sourced"):
        replace(
            problem,
            publishability_constraints=(replace(rule, source="business"),),
        )
    with pytest.raises(ValueError, match="repeat hard constraint ids"):
        replace(
            problem,
            publishability_constraints=(replace(rule, constraint_id="m2-structural-numeric"),),
        )
    with pytest.raises(ValueError, match="unique, ordered priorities"):
        replace(
            problem,
            publishability_constraints=(
                rule,
                replace(rule, constraint_id="another-publishability-gate"),
            ),
        )
    with pytest.raises(ValueError, match="must not be mixed"):
        replace(
            problem,
            hard_constraints=(
                replace(problem.hard_constraints[0], evaluation_stage="post_selection"),
            ),
        )


def test_selected_result_mode_requires_one_candidate_and_pure_pareto_is_not_exposed() -> None:
    problem = _problem(_objective("energy"))
    with pytest.raises(ValueError, match="exactly one"):
        replace(
            problem,
            result_request=replace(
                problem.result_request,
                maximum_returned_candidates=2,
            ),
        )
    with pytest.raises(ValueError, match="unsupported result mode"):
        type(problem.result_request)(mode="pareto", maximum_returned_candidates=2)


def test_feature_analyzer_describes_one_or_many_objectives_without_routing() -> None:
    single = ProblemFeatureAnalyzer().analyze(_problem(_objective("energy")))
    multi = ProblemFeatureAnalyzer().analyze(
        _problem(
            _objective("quality"),
            _objective("yield", "maximize"),
            _objective("energy"),
        )
    )

    assert single.objective_count == 1
    assert single.result_mode == "selected-solution"
    assert multi.objective_count == 3
    assert multi.result_mode == "pareto-and-selected"
    assert single.decision_count == multi.decision_count == 2
    assert single.grid_cardinality == multi.grid_cardinality == 81
    assert single.deterministic and multi.deterministic
