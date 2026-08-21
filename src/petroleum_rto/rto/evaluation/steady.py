"""R3 M2 paired KPI extraction and policy evaluation."""

from __future__ import annotations

from ..compilation import CompiledPair
from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    EvaluationStatus,
    KpiCatalogV1,
    OptimizationProblemV1,
    RunEvidenceRefV1,
    SimulationRunBundleV1,
)
from .common import (
    constraint_outcome,
    finite_path,
    normalized_action_l1,
    validate_pair_bundles,
)


class SteadyPairedEvaluator:
    """Convert paired M2 evidence into deterministic objective and hard constraints."""

    def __init__(self, kpi_catalog: KpiCatalogV1) -> None:
        if not isinstance(kpi_catalog, KpiCatalogV1):
            raise TypeError("SteadyPairedEvaluator requires a KpiCatalogV1")
        self._kpis = kpi_catalog

    def evaluate(
        self,
        problem: OptimizationProblemV1,
        proposal: CandidateProposalV1,
        pair: CompiledPair,
        baseline: SimulationRunBundleV1,
        candidate: SimulationRunBundleV1,
    ) -> CandidateEvaluationV1:
        if pair.baseline.stage != "M2" or pair.candidate.stage != "M2":
            raise ValueError("steady evaluator requires an M2 pair")
        if problem.kpi_catalog_ref != self._kpis.ref:
            raise ValueError("problem KPI catalog reference differs from evaluator catalog")
        if proposal.ref != pair.candidate.proposal_ref:
            raise ValueError("proposal differs from compiled candidate request")
        validate_pair_bundles(pair, baseline, candidate)
        baseline_evidence = RunEvidenceRefV1.from_bundle(baseline)
        candidate_evidence = RunEvidenceRefV1.from_bundle(candidate)
        action = normalized_action_l1(problem, proposal)

        if baseline.runtime_status != "success" or baseline.engine_status != "success":
            return self._failure(
                problem,
                proposal,
                pair,
                "evaluation_error",
                "baseline-evaluation-failed",
                action,
                baseline_evidence,
                candidate_evidence,
            )
        if (
            candidate.runtime_status == "not_converged"
            or candidate.engine_status == "not_converged"
        ):
            return self._failure(
                problem,
                proposal,
                pair,
                "process_infeasible",
                "m2-not-converged",
                action,
                baseline_evidence,
                candidate_evidence,
            )
        if candidate.runtime_status == "rejected":
            return self._failure(
                problem,
                proposal,
                pair,
                "invalid_request",
                "m2-request-rejected",
                action,
                baseline_evidence,
                candidate_evidence,
            )
        if candidate.runtime_status != "success" or candidate.engine_status != "success":
            return self._failure(
                problem,
                proposal,
                pair,
                "evaluation_error",
                "m2-execution-error",
                action,
                baseline_evidence,
                candidate_evidence,
            )

        try:
            baseline_metrics = self._extract(baseline)
            candidate_metrics = self._extract(candidate)
        except (KeyError, TypeError, ValueError):
            return self._failure(
                problem,
                proposal,
                pair,
                "evaluation_error",
                "m2-evidence-incomplete",
                action,
                baseline_evidence,
                candidate_evidence,
            )
        baseline_objective = baseline_metrics["specific_furnace_fuel_energy_mj_per_t"]
        candidate_objective = candidate_metrics["specific_furnace_fuel_energy_mj_per_t"]
        objective_delta = candidate_objective - baseline_objective
        relative_improvement = (baseline_objective - candidate_objective) / baseline_objective
        quality_change = self._quality_change(baseline, candidate)
        yield_delta = (
            candidate_metrics["valuable_distillate_yield"]
            - baseline_metrics["valuable_distillate_yield"]
        )
        metrics = {
            "m2_evaluable": 1.0,
            "quality_proxy_max_abs_relative_change": quality_change,
            "specific_furnace_fuel_energy_mj_per_t": candidate_objective,
            "specific_furnace_fuel_improvement_fraction": relative_improvement,
            "valuable_distillate_yield": candidate_metrics["valuable_distillate_yield"],
            "valuable_distillate_yield_delta": yield_delta,
        }
        outcomes = tuple(
            constraint_outcome(
                rule,
                metrics[rule.metric_id],
                baseline_value=(
                    baseline_metrics.get(rule.metric_id)
                    if rule.metric_id in baseline_metrics
                    else None
                ),
            )
            for rule in problem.constraints
            if rule.stage == "M2" and rule.kind == "hard"
        )
        failed = tuple(item.constraint_id for item in outcomes if not item.passed)
        return CandidateEvaluationV1(
            schema_version=RTO_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation-v1",
            stage="M2",
            status="feasible" if not failed else "process_infeasible",
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_metric_id=problem.objective_metric_id,
            baseline_objective=baseline_objective,
            candidate_objective=candidate_objective,
            objective_delta=objective_delta,
            relative_improvement=relative_improvement,
            metrics=metrics,
            constraints=outcomes,
            minimum_normalized_margin=min(item.normalized_margin for item in outcomes),
            normalized_action_l1=action,
            reason_codes=failed,
            baseline_evidence=baseline_evidence,
            candidate_evidence=candidate_evidence,
            claim_scope=CLAIM_SCOPE,
        )

    def _extract(self, bundle: SimulationRunBundleV1) -> dict[str, float]:
        objective = self._kpis.by_id("specific_furnace_fuel_energy_mj_per_t")
        fuel = finite_path(bundle.as_dict(), objective.source_paths[0])
        feed = finite_path(bundle.as_dict(), objective.source_paths[1])
        if feed <= 0.0:
            raise ValueError("fresh feed must be positive")
        yield_kpi = self._kpis.by_id("valuable_distillate_yield")
        return {
            "m2_evaluable": finite_path(
                bundle.as_dict(),
                "summary.flowsheet.diagnostics.conservation_gate_passed",
            ),
            "specific_furnace_fuel_energy_mj_per_t": fuel / feed / 1000.0,
            "valuable_distillate_yield": sum(
                finite_path(bundle.as_dict(), path) for path in yield_kpi.source_paths
            ),
        }

    def _quality_change(
        self,
        baseline: SimulationRunBundleV1,
        candidate: SimulationRunBundleV1,
    ) -> float:
        kpi = self._kpis.by_id("quality_proxy_max_abs_relative_change")
        changes = []
        baseline_dict = baseline.as_dict()
        candidate_dict = candidate.as_dict()
        for path in kpi.source_paths:
            base = finite_path(baseline_dict, path)
            value = finite_path(candidate_dict, path)
            changes.append(abs(value - base) / max(abs(base), 1e-12))
        return max(changes)

    @staticmethod
    def _failure(
        problem: OptimizationProblemV1,
        proposal: CandidateProposalV1,
        pair: CompiledPair,
        status: EvaluationStatus,
        reason: str,
        action: float,
        baseline_evidence: RunEvidenceRefV1,
        candidate_evidence: RunEvidenceRefV1,
    ) -> CandidateEvaluationV1:
        return CandidateEvaluationV1(
            schema_version=RTO_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation-v1",
            stage="M2",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_metric_id=None,
            baseline_objective=None,
            candidate_objective=None,
            objective_delta=None,
            relative_improvement=None,
            metrics={},
            constraints=(),
            minimum_normalized_margin=None,
            normalized_action_l1=action,
            reason_codes=(reason,),
            baseline_evidence=baseline_evidence,
            candidate_evidence=candidate_evidence,
            claim_scope=CLAIM_SCOPE,
        )
