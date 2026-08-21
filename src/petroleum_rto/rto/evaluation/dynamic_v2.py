"""RTO V2 M4 dynamic verification without recomputing M2 objectives."""

from __future__ import annotations

from collections.abc import Mapping

from ..compilation import CompiledPair, MultiObjectiveCandidatePlanCompiler
from ..contracts import KpiCatalogV1, OperatingContextV1, SimulationRunBundleV1
from ..contracts.evaluation import ConstraintOutcomeV1, RunEvidenceRefV1
from ..contracts.models import CLAIM_SCOPE, SimulationEvaluationRequestV1
from ..contracts.multiobjective import RTO_V2_SCHEMA_VERSION, OptimizationProblemV2
from ..contracts.results_v2 import (
    CandidateEvaluationV2,
    CandidateProposalV2,
    EvaluationStatusV2,
)
from ..ports import ProviderRequestFactory, SimulatorPort
from .common import boolean_path, constraint_outcome, path_value, validate_pair_bundles
from .multiobjective import error_evaluation_v2, normalized_action_l1_v2

_M4_ACCEPTANCE_CHECKS = {
    "plant_execution",
    "plant_conservation",
    "automatic_initialization_no_bump",
    "baseline_hold",
    "loop_performance",
    "true_inventory_safety",
}


class MultiObjectiveDynamicPairedEvaluator:
    """Apply existing M4 acceptance as a V2 hard gate and preserve M2 objectives."""

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
        if pair.baseline.stage != "M4" or pair.candidate.stage != "M4":
            raise ValueError("dynamic evaluator requires an M4 pair")
        if problem.kpi_catalog_ref != self._kpis.ref:
            raise ValueError("problem KPI catalog reference differs from evaluator catalog")
        if proposal.ref != pair.candidate.proposal_ref:
            raise ValueError("proposal differs from compiled candidate request")
        validate_pair_bundles(pair, baseline, candidate)
        baseline_evidence = RunEvidenceRefV1.from_bundle(baseline)
        candidate_evidence = RunEvidenceRefV1.from_bundle(candidate)
        action = normalized_action_l1_v2(problem, proposal)
        try:
            baseline_acceptance = self._acceptance(baseline)
        except (KeyError, TypeError, ValueError):
            baseline_acceptance = False
        if (
            baseline.runtime_status != "success"
            or baseline.engine_status != "success"
            or not baseline_acceptance
        ):
            return self._result(
                problem,
                proposal,
                pair,
                "evaluation_error",
                ("m4-baseline-invalid",),
                {},
                (),
                action,
                baseline_evidence,
                candidate_evidence,
            )
        try:
            candidate_acceptance = self._acceptance(candidate)
            if self._control_fingerprint(baseline) != self._control_fingerprint(candidate):
                raise ValueError("paired M4 control fingerprints differ")
            metrics = self._dynamic_metrics(candidate, candidate_acceptance)
        except (KeyError, TypeError, ValueError):
            return self._result(
                problem,
                proposal,
                pair,
                "evaluation_error",
                ("m4-evidence-incomplete",),
                {},
                (),
                action,
                baseline_evidence,
                candidate_evidence,
            )
        rules = tuple(
            rule for rule in problem.constraints if rule.stage == "M4" and rule.kind == "hard"
        )
        outcomes = tuple(
            constraint_outcome(rule, metrics[rule.metric_id], baseline_value=1.0) for rule in rules
        )
        process_evidence = self._has_process_evidence(candidate)
        status: EvaluationStatusV2
        if (
            candidate_acceptance
            and candidate.runtime_status == "success"
            and candidate.engine_status == "success"
        ):
            status = "feasible"
            reasons: tuple[str, ...] = ()
        elif process_evidence:
            status = "process_infeasible"
            reasons = tuple(item.constraint_id for item in outcomes if not item.passed) or (
                "m4-dynamic-failure",
            )
        else:
            status = "evaluation_error"
            reasons = ("m4-execution-error",)
        return self._result(
            problem,
            proposal,
            pair,
            status,
            reasons,
            metrics,
            outcomes,
            action,
            baseline_evidence,
            candidate_evidence,
        )

    def _acceptance(self, bundle: SimulationRunBundleV1) -> bool:
        kpi = self._kpis.by_id("m4_acceptance_passed")
        accepted = boolean_path(bundle.as_dict(), kpi.source_paths[0])
        control_fingerprint = path_value(bundle.as_dict(), kpi.source_paths[1])
        if not isinstance(control_fingerprint, str) or len(control_fingerprint) != 64:
            raise ValueError("M4 control fingerprint is invalid")
        checks = path_value(bundle.as_dict(), "summary.acceptance_checks")
        loops = path_value(bundle.as_dict(), "summary.loop_performance")
        if not isinstance(checks, Mapping) or not isinstance(loops, Mapping):
            raise TypeError("M4 acceptance evidence is incomplete")
        if accepted:
            if set(checks) != _M4_ACCEPTANCE_CHECKS or not all(
                value is True for value in checks.values()
            ):
                raise ValueError("accepted M4 evidence contains a failed check")
            if len(loops) != 7 or not all(
                isinstance(value, Mapping) and value.get("passed") is True
                for value in loops.values()
            ):
                raise ValueError("accepted M4 evidence lacks seven passing loops")
        return accepted

    def _control_fingerprint(self, bundle: SimulationRunBundleV1) -> str:
        kpi = self._kpis.by_id("m4_acceptance_passed")
        value = path_value(bundle.as_dict(), kpi.source_paths[1])
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("M4 control fingerprint is invalid")
        return value

    @staticmethod
    def _has_process_evidence(bundle: SimulationRunBundleV1) -> bool:
        return isinstance(bundle.summary.get("acceptance_passed"), bool) and isinstance(
            bundle.summary.get("acceptance_checks"), Mapping
        )

    @staticmethod
    def _dynamic_metrics(
        bundle: SimulationRunBundleV1,
        accepted: bool,
    ) -> dict[str, float]:
        loops = path_value(bundle.as_dict(), "summary.loop_performance")
        checks = path_value(bundle.as_dict(), "summary.acceptance_checks")
        if not isinstance(loops, Mapping) or not isinstance(checks, Mapping):
            raise TypeError("M4 dynamic summaries are not mappings")
        settling: list[float] = []
        saturation: list[float] = []
        final_errors: list[float] = []
        for value in loops.values():
            if not isinstance(value, Mapping):
                raise TypeError("M4 loop summary is not a mapping")
            raw_settling = value.get("settling_time_s")
            raw_saturation = value.get("longest_continuous_saturation_s")
            raw_error = value.get("final_error_fraction")
            if isinstance(raw_settling, (int, float)) and not isinstance(raw_settling, bool):
                settling.append(float(raw_settling))
            if isinstance(raw_saturation, (int, float)) and not isinstance(raw_saturation, bool):
                saturation.append(float(raw_saturation))
            if isinstance(raw_error, (int, float)) and not isinstance(raw_error, bool):
                final_errors.append(float(raw_error))
        return {
            "m4_acceptance_passed": 1.0 if accepted else 0.0,
            "m4_failed_check_count": float(sum(value is not True for value in checks.values())),
            "m4_max_settling_time_s": max(settling, default=0.0),
            "m4_max_continuous_saturation_s": max(saturation, default=0.0),
            "m4_max_final_error_fraction": max(final_errors, default=0.0),
        }

    @staticmethod
    def _result(
        problem: OptimizationProblemV2,
        proposal: CandidateProposalV2,
        pair: CompiledPair,
        status: EvaluationStatusV2,
        reasons: tuple[str, ...],
        metrics: Mapping[str, float],
        outcomes: tuple[ConstraintOutcomeV1, ...],
        action: float,
        baseline_evidence: RunEvidenceRefV1,
        candidate_evidence: RunEvidenceRefV1,
    ) -> CandidateEvaluationV2:
        minimum = min(item.normalized_margin for item in outcomes) if outcomes else None
        return CandidateEvaluationV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation-v2",
            stage="M4",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_outcomes=(),
            metrics=metrics,
            constraints=outcomes,
            minimum_normalized_margin=minimum,
            normalized_action_l1=action,
            reason_codes=reasons,
            baseline_evidence=baseline_evidence,
            candidate_evidence=candidate_evidence,
            claim_scope=CLAIM_SCOPE,
        )


class MultiObjectiveDynamicEvaluationService:
    """Execute and cache V2 M4 pairs with one reusable baseline."""

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
        self._compiler = compiler
        self._request_factory = request_factory
        self._simulator = simulator
        self._evaluator = MultiObjectiveDynamicPairedEvaluator(kpi_catalog)
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
                stage="M4",
                request_factory=self._request_factory,
            )
        except (TypeError, ValueError):
            result = error_evaluation_v2(
                self._problem,
                proposal,
                stage="M4",
                status="invalid_request",
                reason_code="candidate-compilation-failed",
            )
            self._cache[proposal.fingerprint] = result
            return result
        try:
            baseline = self._baseline(pair)
            candidate = self._execute(pair.candidate)
            result = self._evaluator.evaluate(self._problem, proposal, pair, baseline, candidate)
        except (OSError, RuntimeError, TypeError, ValueError):
            result = error_evaluation_v2(
                self._problem,
                proposal,
                stage="M4",
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
