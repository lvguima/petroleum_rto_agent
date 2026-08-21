"""Synchronous cached services that execute one candidate through a simulator port."""

from __future__ import annotations

from ..compilation import CandidatePlanCompiler, CompiledPair
from ..contracts import (
    CandidateEvaluationV1,
    CandidateProposalV1,
    EvaluationStage,
    KpiCatalogV1,
    OperatingContextV1,
    OptimizationProblemV1,
    SimulationEvaluationRequestV1,
    SimulationRunBundleV1,
)
from ..ports import ProviderRequestFactory, SimulatorPort
from .common import error_evaluation
from .dynamic import DynamicPairedEvaluator
from .steady import SteadyPairedEvaluator


class _EvaluationService:
    def __init__(
        self,
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        compiler: CandidatePlanCompiler,
        request_factory: ProviderRequestFactory,
        simulator: SimulatorPort,
        *,
        stage: EvaluationStage,
        evaluator: SteadyPairedEvaluator | DynamicPairedEvaluator,
    ) -> None:
        if context.ref != problem.context_ref:
            raise ValueError("problem and evaluation context differ")
        self._problem = problem
        self._context = context
        self._compiler = compiler
        self._request_factory = request_factory
        self._simulator = simulator
        self._stage = stage
        self._evaluator = evaluator
        self._baseline_request_fingerprint: str | None = None
        self._baseline_bundle: SimulationRunBundleV1 | None = None
        self._cache: dict[str, CandidateEvaluationV1] = {}
        self._physical_execution_count = 0
        self._cache_hit_count = 0

    @property
    def physical_execution_count(self) -> int:
        return self._physical_execution_count

    @property
    def cache_hit_count(self) -> int:
        return self._cache_hit_count

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        cached = self._cache.get(proposal.fingerprint)
        if cached is not None:
            self._cache_hit_count += 1
            return cached
        try:
            pair = self._compiler.compile_pair(
                self._problem,
                self._context,
                proposal,
                stage=self._stage,
                request_factory=self._request_factory,
            )
        except (TypeError, ValueError):
            result = error_evaluation(
                self._problem,
                proposal,
                stage=self._stage,
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
            result = self._evaluator.evaluate(
                self._problem,
                proposal,
                pair,
                baseline,
                candidate,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            result = error_evaluation(
                self._problem,
                proposal,
                stage=self._stage,
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


class SteadyEvaluationService(_EvaluationService):
    def __init__(
        self,
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        kpi_catalog: KpiCatalogV1,
        compiler: CandidatePlanCompiler,
        request_factory: ProviderRequestFactory,
        simulator: SimulatorPort,
    ) -> None:
        super().__init__(
            problem,
            context,
            compiler,
            request_factory,
            simulator,
            stage="M2",
            evaluator=SteadyPairedEvaluator(kpi_catalog),
        )


class DynamicEvaluationService(_EvaluationService):
    def __init__(
        self,
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        kpi_catalog: KpiCatalogV1,
        compiler: CandidatePlanCompiler,
        request_factory: ProviderRequestFactory,
        simulator: SimulatorPort,
    ) -> None:
        super().__init__(
            problem,
            context,
            compiler,
            request_factory,
            simulator,
            stage="M4",
            evaluator=DynamicPairedEvaluator(kpi_catalog),
        )
