from __future__ import annotations

from collections.abc import Callable

import pytest

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.compilation import CandidatePlanCompiler
from petroleum_rto.rto.contracts import (
    CandidateEvaluationV1,
    CandidateProposalV1,
    OptimizationProblemV1,
    SimulationRunBundleV1,
)
from petroleum_rto.rto.evaluation import DynamicPairedEvaluator, SteadyPairedEvaluator


def test_steady_pair_computes_objective_constraints_and_round_trips(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_proposal: Callable[..., CandidateProposalV1],
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle, problem = rto_basis
    proposal = make_proposal(problem, bundle.context.ref)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M2",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(
        pair.baseline.provider_request_fingerprint,
        stage="M2",
        objective=188.378985,
    )
    candidate = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M2",
        objective=185.0,
        quality_scale=1.001,
        yield_delta=-0.001,
    )

    result = SteadyPairedEvaluator(bundle.kpi_catalog).evaluate(
        problem, proposal, pair, baseline, candidate
    )

    assert result.status == "feasible"
    assert result.baseline_objective == pytest.approx(188.378985)
    assert result.candidate_objective == pytest.approx(185.0)
    assert result.metrics["quality_proxy_max_abs_relative_change"] == pytest.approx(0.001)
    assert result.metrics["valuable_distillate_yield_delta"] == pytest.approx(-0.001)
    assert CandidateEvaluationV1.from_mapping(result.as_dict()) == result


def test_steady_quality_violation_is_process_infeasible(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_proposal: Callable[..., CandidateProposalV1],
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle, problem = rto_basis
    proposal = make_proposal(problem, bundle.context.ref)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M2",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M2")
    candidate = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M2",
        quality_scale=1.006,
    )

    result = SteadyPairedEvaluator(bundle.kpi_catalog).evaluate(
        problem, proposal, pair, baseline, candidate
    )

    assert result.status == "process_infeasible"
    assert result.reason_codes == ("quality-proxy-preservation",)
    assert result.constraints[1].normalized_margin < 0.0


def test_steady_version_drift_is_rejected(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_proposal: Callable[..., CandidateProposalV1],
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle, problem = rto_basis
    proposal = make_proposal(problem, bundle.context.ref)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M2",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M2")
    candidate = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M2",
    )
    candidate = SimulationRunBundleV1.from_mapping(
        {**candidate.as_dict(), "versions": {"model_version": "v2", "simulation_stage": "M2"}}
    )

    with pytest.raises(ValueError, match="versions differ"):
        SteadyPairedEvaluator(bundle.kpi_catalog).evaluate(
            problem, proposal, pair, baseline, candidate
        )


def test_dynamic_evaluator_requires_complete_existing_m4_acceptance(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_proposal: Callable[..., CandidateProposalV1],
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle, problem = rto_basis
    proposal = make_proposal(problem, bundle.context.ref)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M4",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M4")
    candidate = make_bundle(pair.candidate.provider_request_fingerprint, stage="M4")
    baseline = SimulationRunBundleV1.from_mapping(
        {
            **baseline.as_dict(),
            "versions": {
                **baseline.as_dict()["versions"],
                "scenario_version": "baseline-empty-events",
            },
        }
    )
    candidate = SimulationRunBundleV1.from_mapping(
        {
            **candidate.as_dict(),
            "versions": {
                **candidate.as_dict()["versions"],
                "scenario_version": "candidate-setpoint-events",
            },
        }
    )

    result = DynamicPairedEvaluator(bundle.kpi_catalog).evaluate(
        problem, proposal, pair, baseline, candidate
    )

    assert result.status == "feasible"
    assert result.metrics["m4_acceptance_passed"] == 1.0
    assert len(result.constraints) == 1
    assert CandidateEvaluationV1.from_mapping(result.as_dict()) == result


def test_dynamic_failure_is_not_confused_with_system_error(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_proposal: Callable[..., CandidateProposalV1],
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle, problem = rto_basis
    proposal = make_proposal(problem, bundle.context.ref)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M4",
        request_factory=CduM7RequestFactory(),
    )
    baseline = make_bundle(pair.baseline.provider_request_fingerprint, stage="M4")
    process_failure = make_bundle(
        pair.candidate.provider_request_fingerprint,
        stage="M4",
        accepted=False,
    )
    system_failure = SimulationRunBundleV1.from_mapping(
        {
            **process_failure.as_dict(),
            "summary": {"failure": "resource loading"},
            "failure_stage": "resource_loading",
            "failure_reason": "missing resource",
        }
    )

    evaluator = DynamicPairedEvaluator(bundle.kpi_catalog)
    process = evaluator.evaluate(problem, proposal, pair, baseline, process_failure)
    system = evaluator.evaluate(problem, proposal, pair, baseline, system_failure)

    assert process.status == "process_infeasible"
    assert system.status == "evaluation_error"
