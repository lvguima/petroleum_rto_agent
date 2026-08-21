"""Paired M2 evaluation and cached simulator execution service."""

from __future__ import annotations

from ..capabilities.models import CapabilityCatalog
from ..compilation import (
    CandidateCompilationError,
    CandidatePlanCompiler,
    CompiledPair,
    SystemCompilationError,
)
from ..contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
    EvaluationStatus,
    ObjectiveOutcome,
)
from ..contracts.context import OperatingContext
from ..contracts.evidence import RunEvidenceRef
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from ..contracts.simulation import (
    SimulationEvaluationRequest,
    SimulationRunBundle,
)
from ..ports.interfaces import ProviderRequestFactory, SimulatorPort
from .common import (
    constraint_outcome,
    normalized_action_l1,
    safe_normalized_action_l1,
    validate_pair_bundles,
    validate_preview,
)
from .formulas import PairedMetricValue, TrustedM2FormulaRegistry


class M2PairedEvaluator:
    """Convert any 1..N M2 objective problem into one CandidateEvaluation."""

    def __init__(
        self,
        problem: OptimizationProblem,
        catalog: CapabilityCatalog,
        formula_registry: TrustedM2FormulaRegistry | None = None,
    ) -> None:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("evaluator requires an OptimizationProblem")
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("evaluator requires a CapabilityCatalog")
        if problem.capability_catalog_ref != catalog.ref:
            raise ValueError("problem references another capability catalog")
        if problem.evaluation_plan.static_stage != "M2":
            raise ValueError("M2 evaluator requires M2 as the static stage")
        if any(rule.source == "business" for rule in problem.hard_constraints):
            raise ValueError("unbound business constraints cannot enter M2 evaluation")
        static_constraints = tuple(
            rule for rule in problem.hard_constraints if rule.evaluation_stage == "M2"
        )
        if not static_constraints:
            raise ValueError("M2 evaluation requires at least one M2 hard constraint")
        registry = formula_registry or TrustedM2FormulaRegistry()
        if not isinstance(registry, TrustedM2FormulaRegistry):
            raise TypeError("formula_registry must be a TrustedM2FormulaRegistry")
        metrics = {item.metric_id: item for item in catalog.metrics}
        for objective in problem.objectives:
            metric = metrics.get(objective.metric_id)
            if metric is None:
                raise ValueError(f"M2 objective {objective.metric_id!r} lacks a metric capability")
            if (
                objective.evaluation_stage != "M2"
                or metric.stage != "M2"
                or objective.unit != metric.unit
                or objective.sense != metric.direction
                or objective.formula_id != metric.formula_ref
                or not registry.supports(objective.formula_id)
            ):
                raise ValueError(f"M2 objective {objective.metric_id!r} lacks a trusted binding")
        for rule in static_constraints:
            metric = metrics.get(rule.metric_id)
            if (
                metric is None
                or metric.stage != "M2"
                or rule.unit != metric.unit
                or not registry.supports(metric.formula_ref)
            ):
                raise ValueError(f"M2 constraint {rule.constraint_id!r} lacks a trusted binding")
        for rule in problem.publishability_constraints:
            metric = metrics.get(rule.metric_id)
            if (
                metric is None
                or rule.source != "system"
                or rule.evaluation_stage != "post_selection"
                or metric.stage != "post_selection"
                or rule.unit != metric.unit
                or not registry.supports(metric.formula_ref)
            ):
                raise ValueError(
                    f"publishability constraint {rule.constraint_id!r} lacks a trusted binding"
                )
        self._problem = problem
        self._catalog = catalog
        self._registry = registry
        self._static_constraints = static_constraints

    def evaluate(
        self,
        proposal: CandidateProposal,
        pair: CompiledPair,
        baseline: SimulationRunBundle,
        candidate: SimulationRunBundle,
    ) -> CandidateEvaluation:
        if not isinstance(proposal, CandidateProposal):
            raise TypeError("proposal must be a CandidateProposal")
        if (
            proposal.problem_ref != self._problem.ref
            or proposal.context_ref != self._problem.context_ref
            or pair.candidate.proposal_ref != proposal.ref
        ):
            raise ValueError("proposal, problem, context, or compiled pair identity differs")
        if pair.baseline.stage != "M2" or pair.candidate.stage != "M2":
            raise ValueError("M2 evaluator requires an M2 pair")
        validate_pair_bundles(pair, self._problem, self._catalog, baseline, candidate)
        evidence = (
            RunEvidenceRef.from_bundle(baseline, pair_role="baseline"),
            RunEvidenceRef.from_bundle(candidate, pair_role="candidate"),
        )
        action = normalized_action_l1(self._problem, proposal)

        if baseline.runtime_status != "success" or baseline.engine_status != "success":
            return self._failure(
                proposal,
                pair,
                status="evaluation_error",
                reason="baseline-evaluation-failed",
                action=action,
                evidence=evidence,
            )
        if (
            candidate.runtime_status == "not_converged"
            or candidate.engine_status == "not_converged"
        ):
            return self._failure(
                proposal,
                pair,
                status="process_infeasible",
                reason="m2-not-converged",
                action=action,
                evidence=evidence,
            )
        if candidate.runtime_status == "rejected":
            return self._failure(
                proposal,
                pair,
                status="invalid_request",
                reason="m2-request-rejected",
                action=action,
                evidence=evidence,
            )
        if candidate.runtime_status != "success" or candidate.engine_status != "success":
            return self._failure(
                proposal,
                pair,
                status="evaluation_error",
                reason="m2-execution-error",
                action=action,
                evidence=evidence,
            )

        try:
            values = self._registry.evaluate_required(
                self._problem,
                self._catalog,
                baseline,
                candidate,
            )
            constraints = tuple(
                constraint_outcome(rule, values[rule.metric_id].candidate_value)
                for rule in self._static_constraints
            )
            outcomes = tuple(
                self._objective_outcome(spec.metric_id, values[spec.metric_id])
                for spec in self._problem.objectives
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return self._failure(
                proposal,
                pair,
                status="evaluation_error",
                reason="m2-evidence-incomplete",
                action=action,
                evidence=evidence,
            )

        failed = tuple(sorted(item.constraint_id for item in constraints if not item.passed))
        return CandidateEvaluation(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation",
            stage="M2",
            status="feasible" if not failed else "process_infeasible",
            problem_ref=self._problem.ref,
            context_ref=self._problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_outcomes=outcomes,
            metrics={
                metric_id: value.candidate_value for metric_id, value in sorted(values.items())
            },
            constraints=constraints,
            minimum_normalized_margin=min(item.normalized_margin for item in constraints),
            normalized_action_l1=action,
            reason_codes=failed,
            evidence_refs=evidence,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    def _objective_outcome(
        self,
        metric_id: str,
        values: PairedMetricValue,
    ) -> ObjectiveOutcome:
        spec = next(item for item in self._problem.objectives if item.metric_id == metric_id)
        directional = (
            values.baseline_value - values.candidate_value
            if spec.sense == "minimize"
            else values.candidate_value - values.baseline_value
        )
        relative = (
            None
            if abs(values.baseline_value) <= 1e-12
            else directional / abs(values.baseline_value)
        )
        return ObjectiveOutcome(
            metric_id=spec.metric_id,
            sense=spec.sense,
            unit=spec.unit,
            formula_id=spec.formula_id,
            baseline_value=values.baseline_value,
            candidate_value=values.candidate_value,
            directional_absolute_improvement=directional,
            relative_directional_improvement=relative,
            normalized_directional_improvement=directional / spec.normalization_scale,
        )

    def _failure(
        self,
        proposal: CandidateProposal,
        pair: CompiledPair,
        *,
        status: EvaluationStatus,
        reason: str,
        action: float,
        evidence: tuple[RunEvidenceRef, RunEvidenceRef],
    ) -> CandidateEvaluation:
        return CandidateEvaluation(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation",
            stage="M2",
            status=status,
            problem_ref=self._problem.ref,
            context_ref=self._problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_outcomes=(),
            metrics={},
            constraints=(),
            minimum_normalized_margin=None,
            normalized_action_l1=action,
            reason_codes=(reason,),
            evidence_refs=evidence,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )


class M2EvaluationService:
    """Implement the solver evaluator port over cached paired simulator runs."""

    def __init__(
        self,
        problem: OptimizationProblem,
        context: OperatingContext,
        catalog: CapabilityCatalog,
        compiler: CandidatePlanCompiler,
        request_factory: ProviderRequestFactory,
        simulator: SimulatorPort,
        formula_registry: TrustedM2FormulaRegistry | None = None,
    ) -> None:
        if context.ref != problem.context_ref:
            raise ValueError("problem and evaluation context differ")
        self._problem = problem
        self._context = context
        self._catalog = catalog
        self._compiler = compiler
        self._request_factory = request_factory
        self._simulator = simulator
        self._evaluator = M2PairedEvaluator(problem, catalog, formula_registry)
        self._baseline_request_fingerprint: str | None = None
        self._baseline_bundle: SimulationRunBundle | None = None
        self._cache: dict[str, CandidateEvaluation] = {}
        self._physical_execution_count = 0
        self._cache_hit_count = 0

    @property
    def physical_execution_count(self) -> int:
        return self._physical_execution_count

    @property
    def cache_hit_count(self) -> int:
        return self._cache_hit_count

    def evaluate(self, proposal: CandidateProposal) -> CandidateEvaluation:
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
        except CandidateCompilationError:
            result = self._error_evaluation(
                proposal,
                status="invalid_request",
                reason="candidate-compilation-failed",
            )
            self._cache[proposal.fingerprint] = result
            return result
        except SystemCompilationError:
            result = self._error_evaluation(
                proposal,
                status="evaluation_error",
                reason="system-compilation-failed",
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
            result = self._evaluator.evaluate(
                proposal,
                pair,
                baseline,
                candidate,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            result = self._error_evaluation(
                proposal,
                status="evaluation_error",
                reason="simulator-or-evaluator-error",
                pair_id=pair.baseline.pair_id,
            )
        self._cache[proposal.fingerprint] = result
        return result

    def _baseline(self, pair: CompiledPair) -> SimulationRunBundle:
        fingerprint = pair.baseline.provider_request_fingerprint
        if self._baseline_bundle is None:
            self._baseline_request_fingerprint = fingerprint
            self._baseline_bundle = self._execute(pair.baseline)
        elif self._baseline_request_fingerprint != fingerprint:
            raise ValueError("baseline cache key changed within one evaluation service")
        return self._baseline_bundle

    def _execute(self, request: SimulationEvaluationRequest) -> SimulationRunBundle:
        preview = self._simulator.preview(request)
        validate_preview(request, preview, self._context)
        self._physical_execution_count += 1
        return self._simulator.evaluate(
            request,
            preview.provider_preview_fingerprint,
        )

    def _error_evaluation(
        self,
        proposal: CandidateProposal,
        *,
        status: EvaluationStatus,
        reason: str,
        pair_id: str | None = None,
    ) -> CandidateEvaluation:
        return CandidateEvaluation(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation",
            stage="M2",
            status=status,
            problem_ref=self._problem.ref,
            context_ref=self._problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair_id or f"pair-m2-{proposal.fingerprint[:16]}",
            objective_outcomes=(),
            metrics={},
            constraints=(),
            minimum_normalized_margin=None,
            normalized_action_l1=safe_normalized_action_l1(self._problem, proposal),
            reason_codes=(reason,),
            evidence_refs=(),
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )


__all__ = [
    "M2EvaluationService",
    "M2PairedEvaluator",
    "normalized_action_l1",
]
