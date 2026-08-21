from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.catalogs import load_rto_v2_bundle
from petroleum_rto.rto.compilation import MultiObjectiveCandidatePlanCompiler
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_V2_SCHEMA_VERSION,
    CandidateEvaluationV2,
    CandidateProposalV2,
    ConstraintOutcomeV1,
    ObjectiveOutcomeV2,
    OptimizationProblemV2,
    ParetoSearchResultV2,
    RunEvidenceRefV1,
    SimulationRunBundleV1,
)
from petroleum_rto.rto.evaluation import MultiObjectiveSteadyPairedEvaluator
from petroleum_rto.rto.inputs import (
    bind_external_optimization_request_v2,
    load_external_optimization_request_v2,
)
from petroleum_rto.rto.optimizer import DeterministicParetoGridOptimizer


def _basis(repo_root: Path) -> tuple[object, OptimizationProblemV2]:
    bundle = load_rto_v2_bundle(repo_root)
    request = load_external_optimization_request_v2(
        repo_root / "configs/rto/requests/multiobjective_example_v2.json"
    )
    bound = bind_external_optimization_request_v2(bundle, request)
    return bound.bundle, bound.problem


def _evidence(seed: str) -> RunEvidenceRefV1:
    return RunEvidenceRefV1(
        provider_id="fake-provider",
        run_ref=f"/tmp/{seed}",
        provider_request_fingerprint=seed * 64,
        request_fingerprint=seed * 64,
        effective_input_fingerprint=seed * 64,
        result_fingerprint=seed * 64,
        manifest_fingerprint=seed * 64,
        versions={"model": "v1"},
        source_fingerprints={"source": seed * 64},
    )


def _proposal(
    problem: OptimizationProblemV2,
    context_ref: object,
    index: int,
    *,
    candidate_id: str | None = None,
) -> CandidateProposalV2:
    from petroleum_rto.rto.contracts import ContractRef

    assert isinstance(context_ref, ContractRef)
    return CandidateProposalV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        proposal_version="candidate-proposal-v2",
        candidate_id=candidate_id or f"fixture-v2-{index}",
        sequence=index,
        origin="fixture",
        problem_ref=problem.ref,
        context_ref=context_ref,
        decision_values={
            "furnace_temperature_target_k": 626.35 + 0.5 * index,
            "tower_top_pressure_target_pa_a": 150325.0,
        },
        output_kind="steady-setpoint-vector",
        claim_scope=CLAIM_SCOPE,
    )


def _evaluation(
    problem: OptimizationProblemV2,
    proposal: CandidateProposalV2,
    values: tuple[float, float, float],
    *,
    margin: float = 1.0,
    status: str = "feasible",
) -> CandidateEvaluationV2:
    if status not in {
        "feasible",
        "process_infeasible",
        "invalid_request",
        "evaluation_error",
    }:
        raise ValueError("bad fixture status")
    feasible = status == "feasible"
    baselines = (0.0, 0.49, 188.0)
    outcomes = tuple(
        ObjectiveOutcomeV2(
            metric_id=spec.metric_id,
            sense=spec.sense,
            unit=spec.unit,
            kpi_formula_id=spec.kpi_formula_id,
            baseline_value=baseline,
            candidate_value=value,
            directional_absolute_improvement=(
                baseline - value if spec.sense == "minimize" else value - baseline
            ),
            relative_directional_improvement=(
                None
                if spec.relative_improvement_policy == "zero-baseline-null"
                else (
                    (baseline - value if spec.sense == "minimize" else value - baseline)
                    / abs(baseline)
                )
            ),
            relative_unavailable_reason=(
                "zero-baseline"
                if spec.relative_improvement_policy == "zero-baseline-null"
                else None
            ),
            normalized_directional_improvement=(
                (baseline - value if spec.sense == "minimize" else value - baseline)
                / spec.normalization_scale
            ),
        )
        for spec, baseline, value in zip(problem.objectives, baselines, values, strict=True)
    )
    constraint = ConstraintOutcomeV1(
        constraint_id="fixture-hard-gate",
        stage="M2",
        metric_id="m2_evaluable",
        operator="ge",
        limit=1.0,
        candidate_value=1.0 if feasible else 0.0,
        baseline_value=1.0,
        normalized_margin=margin if feasible else -1.0,
        passed=feasible,
    )
    return CandidateEvaluationV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        evaluation_version="candidate-evaluation-v2",
        stage="M2",
        status=cast(Any, status),
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-v2-m2-{proposal.fingerprint[:16]}",
        objective_outcomes=outcomes if feasible else (),
        metrics={"m2_evaluable": 1.0 if feasible else 0.0},
        constraints=(constraint,),
        minimum_normalized_margin=margin if feasible else -1.0,
        normalized_action_l1=float(proposal.sequence) / 100.0,
        reason_codes=() if feasible else ("fixture-failure",),
        baseline_evidence=_evidence("a") if feasible else None,
        candidate_evidence=_evidence("b") if feasible else None,
        claim_scope=CLAIM_SCOPE,
    )


def test_vector_evaluator_handles_zero_baseline_and_maximize_direction(
    repo_root: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    raw_bundle, problem = _basis(repo_root)
    from petroleum_rto.rto.catalogs import RtoCatalogBundleV2

    assert isinstance(raw_bundle, RtoCatalogBundleV2)
    proposal = _proposal(problem, raw_bundle.base.context.ref, 0)
    pair = MultiObjectiveCandidatePlanCompiler().compile_pair(
        problem,
        raw_bundle.base.context,
        proposal,
        stage="M2",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(
        pair.baseline.provider_request_fingerprint,
        stage="M2",
        objective=188.0,
    )
    candidate = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M2",
        objective=185.0,
        quality_scale=1.001,
        yield_delta=0.001,
    )

    result = MultiObjectiveSteadyPairedEvaluator(raw_bundle.base.kpi_catalog).evaluate(
        problem, proposal, pair, baseline, candidate
    )

    assert result.status == "feasible"
    assert tuple(item.metric_id for item in result.objective_outcomes) == tuple(
        item.metric_id for item in problem.objectives
    )
    quality = result.outcome_by_id("quality_proxy_max_abs_relative_change")
    yield_outcome = result.outcome_by_id("valuable_distillate_yield")
    energy = result.outcome_by_id("specific_furnace_fuel_energy_mj_per_t")
    assert quality.baseline_value == 0.0
    assert quality.relative_directional_improvement is None
    assert quality.relative_unavailable_reason == "zero-baseline"
    assert yield_outcome.directional_absolute_improvement == pytest.approx(0.001)
    assert energy.directional_absolute_improvement == pytest.approx(3.0)
    assert CandidateEvaluationV2.from_mapping(result.as_dict()) == result


def test_handcrafted_dominance_equivalence_and_order_independence(
    repo_root: Path,
) -> None:
    raw_bundle, problem = _basis(repo_root)
    from petroleum_rto.rto.catalogs import RtoCatalogBundleV2

    assert isinstance(raw_bundle, RtoCatalogBundleV2)
    proposals = tuple(_proposal(problem, raw_bundle.base.context.ref, index) for index in range(5))
    evaluations = (
        _evaluation(problem, proposals[0], (0.001, 0.50, 185.0)),
        _evaluation(problem, proposals[1], (0.002, 0.51, 184.0)),
        _evaluation(problem, proposals[2], (0.003, 0.49, 186.0)),
        _evaluation(problem, proposals[3], (0.001, 0.50, 185.0), margin=2.0),
        _evaluation(problem, proposals[4], (0.004, 0.48, 187.0)),
    )

    first_layers, first_groups = DeterministicParetoGridOptimizer.rank_feasible(
        problem, evaluations
    )
    second_layers, second_groups = DeterministicParetoGridOptimizer.rank_feasible(
        problem, reversed(evaluations)
    )

    assert first_layers == second_layers
    assert first_groups == second_groups
    assert set(first_layers[0].evaluation_refs) == {
        evaluations[1].ref,
        evaluations[3].ref,
    }
    assert first_groups[0].representative_ref == evaluations[3].ref
    assert set(first_groups[0].member_refs) == {
        evaluations[0].ref,
        evaluations[3].ref,
    }
    assert DeterministicParetoGridOptimizer.dominates(problem, evaluations[0], evaluations[2])


def test_existing_evidence_gold_front_is_reproduced(repo_root: Path) -> None:
    raw_bundle, problem = _basis(repo_root)
    from petroleum_rto.rto.catalogs import RtoCatalogBundleV2

    assert isinstance(raw_bundle, RtoCatalogBundleV2)
    fixture = cast(
        Mapping[str, Any],
        json.loads(
            (repo_root / "data/rto/gold/r7_existing_evidence_pareto_v2.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    raw_candidates = cast(list[Mapping[str, Any]], fixture["candidates"])
    proposals = tuple(
        _proposal(
            problem,
            raw_bundle.base.context.ref,
            index,
            candidate_id=cast(str, item["proposal_id"]),
        )
        for index, item in enumerate(raw_candidates)
    )
    evaluations = tuple(
        _evaluation(
            problem,
            proposal,
            cast(tuple[float, float, float], tuple(item["objective_values"])),
        )
        for proposal, item in zip(proposals, raw_candidates, strict=True)
    )

    layers, _ = DeterministicParetoGridOptimizer.rank_feasible(problem, evaluations)
    by_evaluation = {
        evaluation.ref: proposal.candidate_id
        for proposal, evaluation in zip(proposals, evaluations, strict=True)
    }

    assert [by_evaluation[ref] for ref in layers[0].evaluation_refs] == cast(
        list[str], fixture["expected_first_front"]
    )


class _GridEvaluator:
    def __init__(self, problem: OptimizationProblemV2, *, error_at: int | None = None) -> None:
        self._problem = problem
        self._error_at = error_at
        self.calls: list[CandidateProposalV2] = []

    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        self.calls.append(proposal)
        if proposal.sequence == self._error_at:
            return _evaluation(
                self._problem,
                proposal,
                (0.0, 0.49, 188.0),
                status="evaluation_error",
            )
        temperature = proposal.decision_values["furnace_temperature_target_k"]
        pressure = proposal.decision_values["tower_top_pressure_target_pa_a"]
        return _evaluation(
            self._problem,
            proposal,
            (
                abs(temperature - 628.35) / 1000.0,
                0.49 + (pressure - 152325.0) / 10_000_000.0,
                188.0 - (630.35 - temperature),
            ),
        )


def test_full_grid_has_81_points_and_round_trips(repo_root: Path) -> None:
    raw_bundle, problem = _basis(repo_root)
    from petroleum_rto.rto.catalogs import RtoCatalogBundleV2

    assert isinstance(raw_bundle, RtoCatalogBundleV2)
    evaluator = _GridEvaluator(problem)

    result = DeterministicParetoGridOptimizer().search(problem, raw_bundle.base.context, evaluator)

    assert result.status == "success"
    assert result.grid_count == 81
    assert len(evaluator.calls) == 81
    assert result.pareto_refs
    assert ParetoSearchResultV2.from_mapping(result.as_dict()) == result


def test_system_error_does_not_expose_partial_pareto_front(repo_root: Path) -> None:
    raw_bundle, problem = _basis(repo_root)
    from petroleum_rto.rto.catalogs import RtoCatalogBundleV2

    assert isinstance(raw_bundle, RtoCatalogBundleV2)
    result = DeterministicParetoGridOptimizer().search(
        problem,
        raw_bundle.base.context,
        _GridEvaluator(problem, error_at=3),
    )

    assert result.status == "evaluation_error"
    assert result.error_count == 1
    assert result.pareto_layers == ()
    assert result.pareto_refs == ()
