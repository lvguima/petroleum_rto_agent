"""RTO V2 paired objective-vector evaluation and cached execution service."""

from __future__ import annotations

from ..compilation import CompiledPair, MultiObjectiveCandidatePlanCompiler
from ..contracts import KpiCatalogV1, OperatingContextV1, SimulationRunBundleV1
from ..contracts.evaluation import RunEvidenceRefV1
from ..contracts.models import CLAIM_SCOPE, EvaluationStage, SimulationEvaluationRequestV1
from ..contracts.multiobjective import RTO_V2_SCHEMA_VERSION, OptimizationProblemV2
from ..contracts.results_v2 import (
    CandidateEvaluationV2,
    CandidateProposalV2,
    EvaluationStatusV2,
    ObjectiveOutcomeV2,
)
from ..ports import ProviderRequestFactory, SimulatorPort
from .common import constraint_outcome, finite_path, validate_pair_bundles


def normalized_action_l1_v2(
    problem: OptimizationProblemV2,
    proposal: CandidateProposalV2,
) -> float:
    total = 0.0
    for domain in problem.decision_domains:
        width = domain.upper_bound - domain.lower_bound
        if width <= 0.0:
            raise ValueError("decision domain width must be positive")
        total += abs(proposal.decision_values[domain.variable_id] - domain.nominal_value) / width
    return total


def error_evaluation_v2(
    problem: OptimizationProblemV2,
    proposal: CandidateProposalV2,
    *,
    stage: EvaluationStage,
    status: EvaluationStatusV2,
    reason_code: str,
) -> CandidateEvaluationV2:
    if stage not in {"M2", "M4"}:
        raise ValueError("unsupported evaluation stage")
    if status not in {"invalid_request", "evaluation_error"}:
        raise ValueError("error status must be invalid_request or evaluation_error")
    return CandidateEvaluationV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        evaluation_version="candidate-evaluation-v2",
        stage=stage,
        status=status,
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-v2-{stage.lower()}-{proposal.fingerprint[:16]}",
        objective_outcomes=(),
        metrics={},
        constraints=(),
        minimum_normalized_margin=None,
        normalized_action_l1=normalized_action_l1_v2(problem, proposal),
        reason_codes=(reason_code,),
        baseline_evidence=None,
        candidate_evidence=None,
        claim_scope=CLAIM_SCOPE,
    )


class MultiObjectiveSteadyPairedEvaluator:
    """Extract all frozen M2 objectives once and apply unchanged hard constraints."""

    def __init__(self, kpi_catalog: KpiCatalogV1) -> None:
        if not isinstance(kpi_catalog, KpiCatalogV1):
            raise TypeError("evaluator requires a KpiCatalogV1")
        self._kpis = kpi_catalog

    def evaluate(
        self,
        problem: OptimizationProblemV2,
        proposal: CandidateProposalV2,
        pair: CompiledPair,
        baseline: SimulationRunBundleV1,
        candidate: SimulationRunBundleV1,
    ) -> CandidateEvaluationV2:
        if pair.baseline.stage != "M2" or pair.candidate.stage != "M2":
            raise ValueError("steady evaluator requires an M2 pair")
        if problem.kpi_catalog_ref != self._kpis.ref:
            raise ValueError("problem KPI catalog reference differs from evaluator catalog")
        if proposal.ref != pair.candidate.proposal_ref:
            raise ValueError("proposal differs from compiled candidate request")
        validate_pair_bundles(pair, baseline, candidate)
        baseline_evidence = RunEvidenceRefV1.from_bundle(baseline)
        candidate_evidence = RunEvidenceRefV1.from_bundle(candidate)
        action = normalized_action_l1_v2(problem, proposal)

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
            quality_change = self._quality_change(baseline, candidate)
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

        baseline_metrics["quality_proxy_max_abs_relative_change"] = 0.0
        candidate_metrics["quality_proxy_max_abs_relative_change"] = quality_change
        yield_delta = (
            candidate_metrics["valuable_distillate_yield"]
            - baseline_metrics["valuable_distillate_yield"]
        )
        energy_baseline = baseline_metrics["specific_furnace_fuel_energy_mj_per_t"]
        energy_candidate = candidate_metrics["specific_furnace_fuel_energy_mj_per_t"]
        energy_improvement = (energy_baseline - energy_candidate) / energy_baseline
        metrics = {
            "m2_evaluable": 1.0,
            "quality_proxy_max_abs_relative_change": quality_change,
            "specific_furnace_fuel_energy_mj_per_t": energy_candidate,
            "specific_furnace_fuel_improvement_fraction": energy_improvement,
            "valuable_distillate_yield": candidate_metrics["valuable_distillate_yield"],
            "valuable_distillate_yield_delta": yield_delta,
        }
        constraints = tuple(
            constraint_outcome(
                rule,
                metrics[rule.metric_id],
                baseline_value=baseline_metrics.get(rule.metric_id),
            )
            for rule in problem.constraints
            if rule.stage == "M2" and rule.kind == "hard"
        )
        failed = tuple(item.constraint_id for item in constraints if not item.passed)
        outcomes = tuple(
            self._objective_outcome(
                spec,
                baseline_metrics[spec.metric_id],
                candidate_metrics[spec.metric_id],
            )
            for spec in problem.objectives
        )
        return CandidateEvaluationV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation-v2",
            stage="M2",
            status="feasible" if not failed else "process_infeasible",
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_outcomes=outcomes,
            metrics=metrics,
            constraints=constraints,
            minimum_normalized_margin=min(item.normalized_margin for item in constraints),
            normalized_action_l1=action,
            reason_codes=failed,
            baseline_evidence=baseline_evidence,
            candidate_evidence=candidate_evidence,
            claim_scope=CLAIM_SCOPE,
        )

    def _extract(self, bundle: SimulationRunBundleV1) -> dict[str, float]:
        energy_kpi = self._kpis.by_id("specific_furnace_fuel_energy_mj_per_t")
        fuel = finite_path(bundle.as_dict(), energy_kpi.source_paths[0])
        feed = finite_path(bundle.as_dict(), energy_kpi.source_paths[1])
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
        baseline_dict = baseline.as_dict()
        candidate_dict = candidate.as_dict()
        return max(
            abs(finite_path(candidate_dict, path) - finite_path(baseline_dict, path))
            / max(abs(finite_path(baseline_dict, path)), 1e-12)
            for path in kpi.source_paths
        )

    @staticmethod
    def _objective_outcome(
        spec: object,
        baseline_value: float,
        candidate_value: float,
    ) -> ObjectiveOutcomeV2:
        from ..contracts.multiobjective import ObjectiveSpecV2

        if not isinstance(spec, ObjectiveSpecV2):
            raise TypeError("objective must be ObjectiveSpecV2")
        directional = (
            baseline_value - candidate_value
            if spec.sense == "minimize"
            else candidate_value - baseline_value
        )
        if spec.relative_improvement_policy == "zero-baseline-null" or abs(baseline_value) <= 1e-12:
            relative = None
            reason = "zero-baseline"
        else:
            relative = directional / abs(baseline_value)
            reason = None
        return ObjectiveOutcomeV2(
            metric_id=spec.metric_id,
            sense=spec.sense,
            unit=spec.unit,
            kpi_formula_id=spec.kpi_formula_id,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            directional_absolute_improvement=directional,
            relative_directional_improvement=relative,
            relative_unavailable_reason=reason,
            normalized_directional_improvement=directional / spec.normalization_scale,
        )

    @staticmethod
    def _failure(
        problem: OptimizationProblemV2,
        proposal: CandidateProposalV2,
        pair: CompiledPair,
        status: EvaluationStatusV2,
        reason: str,
        action: float,
        baseline_evidence: RunEvidenceRefV1,
        candidate_evidence: RunEvidenceRefV1,
    ) -> CandidateEvaluationV2:
        return CandidateEvaluationV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation-v2",
            stage="M2",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_outcomes=(),
            metrics={},
            constraints=(),
            minimum_normalized_margin=None,
            normalized_action_l1=action,
            reason_codes=(reason,),
            baseline_evidence=baseline_evidence,
            candidate_evidence=candidate_evidence,
            claim_scope=CLAIM_SCOPE,
        )


class MultiObjectiveSteadyEvaluationService:
    """Cache one M2 baseline and each unique V2 physical decision vector."""

    def __init__(
        self,
        problem: OptimizationProblemV2,
        context: OperatingContextV1,
        kpi_catalog: KpiCatalogV1,
        compiler: MultiObjectiveCandidatePlanCompiler,
        request_factory: ProviderRequestFactory,
        simulator: SimulatorPort,
    ) -> None:
        if context.ref != problem.context_ref:
            raise ValueError("problem and evaluation context differ")
        self._problem = problem
        self._context = context
        self._kpis = kpi_catalog
        self._compiler = compiler
        self._request_factory = request_factory
        self._simulator = simulator
        self._evaluator = MultiObjectiveSteadyPairedEvaluator(kpi_catalog)
        self._baseline_request_fingerprint: str | None = None
        self._baseline_bundle: SimulationRunBundleV1 | None = None
        self._cache: dict[str, CandidateEvaluationV2] = {}
        self._physical_execution_count = 0
        self._cache_hit_count = 0

    @property
    def physical_execution_count(self) -> int:
        return self._physical_execution_count

    @property
    def cache_hit_count(self) -> int:
        return self._cache_hit_count

    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        cached = self._cache.get(proposal.fingerprint)
        if cached is not None:
            self._cache_hit_count += 1
            return cached
        try:
            pair = self._compiler.compile_pair(
                self._problem,
                self._context,
                proposal,
                stage="M2",
                request_factory=self._request_factory,
            )
        except (TypeError, ValueError):
            result = error_evaluation_v2(
                self._problem,
                proposal,
                stage="M2",
                status="invalid_request",
                reason_code="candidate-compilation-failed",
            )
            self._cache[proposal.fingerprint] = result
            return result
        try:
            baseline = self._baseline(pair)
            if (
                pair.candidate.provider_request_fingerprint
                == pair.baseline.provider_request_fingerprint
            ):
                candidate = baseline
            else:
                candidate = self._execute(pair.candidate)
            result = self._evaluator.evaluate(self._problem, proposal, pair, baseline, candidate)
        except (OSError, RuntimeError, TypeError, ValueError):
            result = error_evaluation_v2(
                self._problem,
                proposal,
                stage="M2",
                status="evaluation_error",
                reason_code="simulator-or-evaluator-error",
            )
        self._cache[proposal.fingerprint] = result
        return result

    def _baseline(self, pair: CompiledPair) -> SimulationRunBundleV1:
        fingerprint = pair.baseline.provider_request_fingerprint
        if self._baseline_bundle is None:
            self._baseline_request_fingerprint = fingerprint
            self._baseline_bundle = self._execute(pair.baseline)
        elif self._baseline_request_fingerprint != fingerprint:
            raise ValueError("baseline cache key changed within one evaluation service")
        return self._baseline_bundle

    def _execute(self, request: SimulationEvaluationRequestV1) -> SimulationRunBundleV1:
        preview = self._simulator.preview(request)
        self._physical_execution_count += 1
        return self._simulator.evaluate(request, preview.provider_preview_fingerprint)
