from __future__ import annotations

from pathlib import Path

from petroleum_rto.rto.adapters import CduM7RequestFactory, CduM7Simulator
from petroleum_rto.rto.capabilities import load_capability_bundle
from petroleum_rto.rto.compilation import UnifiedCandidatePlanCompiler
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
)
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE
from petroleum_rto.rto.evaluation import UnifiedM2EvaluationService
from petroleum_rto.rto.problem import UnifiedProblemBuilder
from petroleum_rto.rto.unified_inputs import load_optimization_intent


def test_one_unified_candidate_runs_through_real_cdu_m2_and_reloads_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    bundle = load_capability_bundle(repo_root)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    intent = load_optimization_intent(
        repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    )
    problem = UnifiedProblemBuilder().build(bundle, intent, context)
    proposal = CandidateProposal(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        proposal_version="candidate-proposal",
        candidate_id="real-cdu-m2-candidate",
        sequence=0,
        origin="integration-fixture",
        problem_ref=problem.ref,
        context_ref=context.ref,
        decision_values={
            "furnace_temperature_target_k": 627.35,
            "tower_top_pressure_target_pa_a": 151325.0,
        },
        output_kind="steady-setpoint-vector",
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    simulator = CduM7Simulator(tmp_path / "runs")
    service = UnifiedM2EvaluationService(
        problem,
        context,
        bundle.catalog,
        UnifiedCandidatePlanCompiler(bundle.catalog),
        CduM7RequestFactory(),
        simulator,
    )

    result = service.evaluate(proposal)
    cached = service.evaluate(proposal)

    assert isinstance(result, CandidateEvaluation)
    assert result.status == "feasible"
    assert cached is result
    assert service.physical_execution_count == 2
    assert service.cache_hit_count == 1
    assert tuple(item.metric_id for item in result.objective_outcomes) == (
        "specific_furnace_fuel_energy_mj_per_t",
    )
    assert tuple(item.pair_role for item in result.evidence_refs) == (
        "baseline",
        "candidate",
    )
    for evidence in result.evidence_refs:
        reloaded = simulator.read_evidence(Path(evidence.run_ref))
        assert reloaded.provider_request_fingerprint == evidence.provider_request_fingerprint
        assert reloaded.request_fingerprint == evidence.request_fingerprint
        assert reloaded.result_fingerprint == evidence.result_fingerprint
        assert reloaded.manifest_fingerprint == evidence.manifest_fingerprint
