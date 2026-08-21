"""Synchronous, resumable, objective-count-neutral offline RTO workflow."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .._file_lock import exclusive_file_lock
from ..capabilities import (
    CapabilityCatalog,
    UnifiedCapabilityBundle,
    build_solver_routing_policy,
)
from ..compilation import UnifiedCandidatePlanCompiler
from ..contracts.candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateEvaluation,
    CandidateProposal,
)
from ..contracts.common import (
    JsonValue,
    as_mapping,
    canonical_json_bytes,
    identifier,
    thaw_json,
)
from ..contracts.context import OperatingContext
from ..contracts.evidence import RunEvidenceRef
from ..contracts.finalization import StaticPreferenceSelection
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from ..contracts.reference import ContractRef
from ..contracts.simulation import SimulationRunBundle
from ..contracts.solver_result import SOLVER_RESULT_SCHEMA_VERSION, SolverResult
from ..evaluation import (
    UnifiedM2EvaluationService,
    UnifiedM2PairedEvaluator,
    UnifiedM4EvaluationService,
    UnifiedM4PairedEvaluator,
)
from ..ports.unified import UnifiedProviderRequestFactory, UnifiedSimulatorPort
from ..problem import ProblemFeatureAnalyzer, UnifiedProblemBuilder
from ..selection import FinalizationArtifacts, UnifiedFinalSelector
from ..solvers import (
    CoarseRefineGridSolver,
    FullGridParetoSolver,
    SolverRegistry,
    SolverRouter,
    SolverRoutingDecision,
)
from ..strategies.unified.models import StrategyEntry
from ..unified_inputs import OptimizationIntent
from .unified_models import (
    OFFLINE_WORKFLOW_SCHEMA_ID,
    OFFLINE_WORKFLOW_SCHEMA_VERSION,
    UNIFIED_MANIFEST_VERSION,
    AnchorAttempt,
    AnchorValidationResult,
    CapabilityBundleSnapshot,
    CoveragePolicy,
    DynamicVerificationArtifact,
    FinalizationArtifact,
    OfflineRtoManifest,
    OfflineRtoRequest,
    OfflineRtoResult,
    OfflineRunStatus,
    SolverExecutionArtifact,
    WorkflowEvent,
    routing_ref,
)

if TYPE_CHECKING:
    from ..strategies.unified.repository import StrategyRepository

SimulatorFactory = Callable[[Path], UnifiedSimulatorPort]

_SOFTWARE_VERSIONS = {
    "offline_workflow": "1.0.0",
    "optimization_problem": "1.0.0",
    "candidate_evaluation": "1.0.0",
    "strategy_entry": "1.0.0",
}
_STATIC_STAGES = (
    "inputs-ready",
    "problem-ready",
    "route-ready",
    "static-solve-ready",
    "static-selection-ready",
    "dynamic-evaluations-ready",
    "finalization-ready",
)
_REQUIRED_MANIFEST_FILES = frozenset(
    {
        "request.json",
        "intent.json",
        "context.json",
        "capability_bundle.json",
        "problem.json",
        "solver_route.json",
        "static_solve.json",
        "static_selection.json",
        "dynamic_evaluations.json",
        "finalization.json",
        "result.json",
        "events.jsonl",
    }
)
_OPTIONAL_MANIFEST_FILES = frozenset({"anchor_validation.json", "strategy_draft.json"})


@dataclass(frozen=True)
class OfflineRtoRunRecord:
    run_dir: Path
    request: OfflineRtoRequest
    intent: OptimizationIntent
    context: OperatingContext
    capability_snapshot: CapabilityBundleSnapshot
    problem: OptimizationProblem
    routing: SolverRoutingDecision
    solver_execution: SolverExecutionArtifact
    static_selection: StaticPreferenceSelection
    dynamic_verification: DynamicVerificationArtifact
    finalization: FinalizationArtifact
    anchor_validation: AnchorValidationResult | None
    strategy: StrategyEntry | None
    result: OfflineRtoResult
    manifest: OfflineRtoManifest
    events: tuple[WorkflowEvent, ...]
    recovered_stages: tuple[str, ...]
    physical_m2_executions: int
    physical_m4_executions: int


class _ReplayEvaluator:
    def __init__(self, evaluations: tuple[CandidateEvaluation, ...]) -> None:
        self._evaluations = {item.proposal_ref: item for item in evaluations}

    def evaluate(self, proposal: CandidateProposal) -> CandidateEvaluation:
        try:
            return self._evaluations[proposal.ref]
        except KeyError as exc:
            raise ValueError("stored solver execution lacks a generated proposal") from exc


class OfflineRtoOrchestrator:
    """Execute or strictly resume one unified offline workflow."""

    def __init__(
        self,
        request_factory: UnifiedProviderRequestFactory,
        simulator_factory: SimulatorFactory,
    ) -> None:
        self._request_factory = request_factory
        self._simulator_factory = simulator_factory

    def run(
        self,
        bundle: UnifiedCapabilityBundle,
        intent: OptimizationIntent,
        context: OperatingContext,
        *,
        run_root: Path,
        strategy_repository: StrategyRepository,
        actor: str,
        coverage_policy: CoveragePolicy = "point",
    ) -> OfflineRtoRunRecord:
        actor = identifier(actor, context="actor")
        problem = UnifiedProblemBuilder().build(bundle, intent, context)
        policy = build_solver_routing_policy(bundle)
        policy_ref = ContractRef(policy.policy_id, policy.fingerprint)
        request = OfflineRtoRequest(
            schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
            schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
            request_version="offline-rto-request",
            intent_ref=ContractRef(intent.intent_id, intent.fingerprint),
            context_ref=context.ref,
            capability_catalog_ref=bundle.catalog.ref,
            context_schema_ref=bundle.context_schema.ref,
            system_policy_ref=bundle.system_policy.ref,
            solver_policy_ref=policy_ref,
            provider_id=self._request_factory.provider_id,
            coverage_policy=coverage_policy,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
        snapshot = CapabilityBundleSnapshot(
            schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
            schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
            snapshot_version="capability-bundle-snapshot",
            bundle=bundle,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
        run_dir = run_root.resolve() / request.workflow_id
        run_dir.mkdir(parents=True, exist_ok=True)
        simulator = self._simulator_factory(run_dir / "simulator")
        with _workflow_lock(run_dir):
            if (run_dir / "manifest.json").exists():
                return read_offline_run(
                    run_dir,
                    strategy_repository=strategy_repository,
                    request_factory=self._request_factory,
                    simulator=simulator,
                    expected_intent=intent,
                    expected_context=context,
                    expected_bundle=bundle,
                )

            events = list(_read_events(run_dir / "events.jsonl", request.ref, allow_missing=True))
            input_paths = (
                run_dir / "request.json",
                run_dir / "intent.json",
                run_dir / "context.json",
                run_dir / "capability_bundle.json",
            )
            _reject_event_without_artifacts(events, "inputs-ready", input_paths)
            inputs_were_complete = all(path.exists() for path in input_paths)
            _write_or_verify(run_dir / "request.json", request.as_dict())
            _write_or_verify(run_dir / "intent.json", intent.as_dict())
            _write_or_verify(run_dir / "context.json", context.as_dict())
            _write_or_verify(run_dir / "capability_bundle.json", snapshot.as_dict())
            _ensure_event(events, run_dir, request.ref, "inputs-ready", request.ref)
            recovered: list[str] = ["inputs-ready"] if inputs_were_complete else []
            physical_m2 = 0
            physical_m4 = 0

            expected_problem = problem
            problem_path = run_dir / "problem.json"
            _reject_event_without_artifacts(events, "problem-ready", (problem_path,))
            if problem_path.exists():
                problem = OptimizationProblem.from_mapping(_read_json(problem_path))
                if problem != expected_problem:
                    raise ValueError("stored problem differs from deterministic reconstruction")
                recovered.append("problem-ready")
            else:
                _write_or_verify(problem_path, problem.as_dict())
            _ensure_event(events, run_dir, request.ref, "problem-ready", problem.ref)

            registry = _solver_registry()
            features = ProblemFeatureAnalyzer().analyze(problem)
            expected_route = SolverRouter().route(features, registry, policy)
            route_path = run_dir / "solver_route.json"
            _reject_event_without_artifacts(events, "route-ready", (route_path,))
            if route_path.exists():
                routing = SolverRoutingDecision.from_mapping(_read_json(route_path))
                if routing != expected_route.decision:
                    raise ValueError("stored solver route differs from deterministic routing")
                recovered.append("route-ready")
            else:
                routing = expected_route.decision
                _write_or_verify(route_path, routing.as_dict())
            route_contract_ref = routing_ref(routing)
            _ensure_event(events, run_dir, request.ref, "route-ready", route_contract_ref)

            execution_path = run_dir / "static_solve.json"
            _reject_event_without_artifacts(events, "static-solve-ready", (execution_path,))
            if execution_path.exists():
                solver_execution = SolverExecutionArtifact.from_mapping(_read_json(execution_path))
                _validate_solver_execution(
                    problem, routing, solver_execution, expected_route.solver
                )
                recovered.append("static-solve-ready")
            else:
                if expected_route.solver is None:
                    solver_result = _unsupported_solver_result(problem, routing)
                else:
                    steady = UnifiedM2EvaluationService(
                        problem,
                        context,
                        bundle.catalog,
                        UnifiedCandidatePlanCompiler(bundle.catalog),
                        self._request_factory,
                        simulator,
                    )
                    solver_result = expected_route.solver.solve(problem, steady)
                    physical_m2 += steady.physical_execution_count
                    solver_result = _relativize_solver_result(solver_result, run_dir)
                solver_execution = SolverExecutionArtifact(
                    schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
                    schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
                    execution_version="solver-execution",
                    routing_fingerprint=routing.fingerprint,
                    result=solver_result,
                    claim_scope=ENGINEERING_CLAIM_SCOPE,
                )
            _replay_evaluations(
                solver_execution.result.evaluations,
                proposals=solver_execution.result.proposals,
                problem=problem,
                context=context,
                catalog=bundle.catalog,
                request_factory=self._request_factory,
                run_dir=run_dir,
                simulator=simulator,
            )
            _write_or_verify(execution_path, solver_execution.as_dict())
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "static-solve-ready",
                solver_execution.ref,
            )

            m2 = {item.proposal_ref: item for item in solver_execution.result.evaluations}
            selector = UnifiedFinalSelector()
            expected_selection = selector.rank_static(problem, solver_execution.result, m2)
            selection_path = run_dir / "static_selection.json"
            _reject_event_without_artifacts(events, "static-selection-ready", (selection_path,))
            if selection_path.exists():
                static_selection = StaticPreferenceSelection.from_mapping(
                    _read_json(selection_path)
                )
                if static_selection != expected_selection:
                    raise ValueError("stored static selection differs from deterministic replay")
                recovered.append("static-selection-ready")
            else:
                static_selection = expected_selection
                _write_or_verify(selection_path, static_selection.as_dict())
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "static-selection-ready",
                static_selection.ref,
            )

            dynamic_path = run_dir / "dynamic_evaluations.json"
            _reject_event_without_artifacts(events, "dynamic-evaluations-ready", (dynamic_path,))
            if dynamic_path.exists():
                dynamic = DynamicVerificationArtifact.from_mapping(_read_json(dynamic_path))
                _validate_dynamic_artifact(problem, static_selection, dynamic)
                recovered.append("dynamic-evaluations-ready")
            else:
                dynamic_evaluations: tuple[CandidateEvaluation, ...] = ()
                if static_selection.status == "ready":
                    proposal_by_ref = {item.ref: item for item in solver_execution.result.proposals}
                    service = UnifiedM4EvaluationService(
                        problem,
                        context,
                        bundle.catalog,
                        UnifiedCandidatePlanCompiler(bundle.catalog),
                        self._request_factory,
                        simulator,
                    )
                    dynamic_evaluations = tuple(
                        _relativize_evaluation(service.evaluate(proposal_by_ref[ref]), run_dir)
                        for ref in static_selection.shortlist_proposal_refs
                    )
                    physical_m4 += service.physical_execution_count
                dynamic = DynamicVerificationArtifact(
                    schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
                    schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
                    verification_version="dynamic-verification",
                    problem_ref=problem.ref,
                    static_selection_ref=static_selection.ref,
                    evaluations=dynamic_evaluations,
                    applicable=bool(dynamic_evaluations),
                    termination_reason=(
                        "dynamic-shortlist-evaluated"
                        if dynamic_evaluations
                        else "static-selection-not-ready"
                    ),
                    claim_scope=ENGINEERING_CLAIM_SCOPE,
                )
            _replay_evaluations(
                dynamic.evaluations,
                proposals=solver_execution.result.proposals,
                problem=problem,
                context=context,
                catalog=bundle.catalog,
                request_factory=self._request_factory,
                run_dir=run_dir,
                simulator=simulator,
            )
            _write_or_verify(dynamic_path, dynamic.as_dict())
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "dynamic-evaluations-ready",
                dynamic.ref,
            )

            m4 = {item.proposal_ref: item for item in dynamic.evaluations}
            expected_finalization = selector.select(
                problem,
                solver_execution.result,
                m2,
                m4,
                bundle,
            )
            expected_final_artifact = FinalizationArtifact(
                schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
                schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
                artifact_version="finalization-artifact",
                static_selection_ref=expected_finalization.static_selection.ref,
                publishability=expected_finalization.publishability,
                result=expected_finalization.result,
                claim_scope=ENGINEERING_CLAIM_SCOPE,
            )
            final_path = run_dir / "finalization.json"
            _reject_event_without_artifacts(events, "finalization-ready", (final_path,))
            if final_path.exists():
                finalization = FinalizationArtifact.from_mapping(_read_json(final_path))
                if finalization != expected_final_artifact:
                    raise ValueError("stored finalization differs from deterministic replay")
                recovered.append("finalization-ready")
            else:
                finalization = expected_final_artifact
                _write_or_verify(final_path, finalization.as_dict())
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "finalization-ready",
                finalization.ref,
            )

            anchor_validation, anchor_m2, anchor_m4 = self._anchors(
                run_dir,
                request,
                intent,
                context,
                bundle,
                problem,
                solver_execution,
                dynamic,
                expected_finalization,
                simulator,
                events,
                recovered,
            )
            physical_m2 += anchor_m2
            physical_m4 += anchor_m4

            strategy = self._strategy(
                run_dir,
                strategy_repository,
                actor,
                problem,
                context,
                solver_execution,
                dynamic,
                expected_finalization,
                anchor_validation,
                events,
                request,
                recovered,
            )

            result = _offline_result(
                request,
                problem,
                routing,
                solver_execution,
                static_selection,
                dynamic,
                finalization,
                anchor_validation,
                strategy,
            )
            result_path = run_dir / "result.json"
            _reject_event_without_artifacts(events, "workflow-complete", (result_path,))
            if result_path.exists():
                stored_result = OfflineRtoResult.from_mapping(_read_json(result_path))
                if stored_result != result:
                    raise ValueError("stored offline result differs from deterministic replay")
                result = stored_result
                recovered.append("workflow-complete")
            else:
                _write_or_verify(result_path, result.as_dict())
            _ensure_event(events, run_dir, request.ref, "workflow-complete", result.ref)

            _validate_event_stages(
                tuple(events),
                request=request,
                problem=problem,
                routing=routing,
                solver_execution=solver_execution,
                selection=static_selection,
                dynamic=dynamic,
                finalization=finalization,
                anchors=anchor_validation,
                strategy=strategy,
                result=result,
            )
            manifest = _commit_manifest(run_dir, request, result)
            verified = read_offline_run(
                run_dir,
                strategy_repository=strategy_repository,
                request_factory=self._request_factory,
                simulator=simulator,
                expected_intent=intent,
                expected_context=context,
                expected_bundle=bundle,
            )
            if verified.result != result or verified.manifest != manifest:
                raise ValueError("new workflow failed strict post-commit verification")
            return OfflineRtoRunRecord(
                run_dir=run_dir,
                request=request,
                intent=intent,
                context=context,
                capability_snapshot=snapshot,
                problem=problem,
                routing=routing,
                solver_execution=solver_execution,
                static_selection=static_selection,
                dynamic_verification=dynamic,
                finalization=finalization,
                anchor_validation=anchor_validation,
                strategy=strategy,
                result=result,
                manifest=manifest,
                events=tuple(events),
                recovered_stages=tuple(recovered),
                physical_m2_executions=physical_m2,
                physical_m4_executions=physical_m4,
            )

    def _anchors(
        self,
        run_dir: Path,
        request: OfflineRtoRequest,
        intent: OptimizationIntent,
        context: OperatingContext,
        bundle: UnifiedCapabilityBundle,
        problem: OptimizationProblem,
        solver_execution: SolverExecutionArtifact,
        dynamic: DynamicVerificationArtifact,
        finalization: FinalizationArtifacts,
        simulator: UnifiedSimulatorPort,
        events: list[WorkflowEvent],
        recovered: list[str],
    ) -> tuple[AnchorValidationResult | None, int, int]:
        path = run_dir / "anchor_validation.json"
        _reject_event_without_artifacts(events, "anchor-validation-ready", (path,))
        if finalization.result.status != "success":
            if path.exists():
                raise ValueError("non-publishable workflow cannot contain anchor validation")
            return None, 0, 0
        selected_ref = finalization.result.selected_proposal_ref
        selected_static_ref = finalization.result.selected_static_evaluation_ref
        selected_dynamic_ref = finalization.result.selected_dynamic_evaluation_ref
        if selected_ref is None or selected_static_ref is None or selected_dynamic_ref is None:
            raise ValueError("successful finalization lacks selected evidence")
        proposal = next(
            item for item in solver_execution.result.proposals if item.ref == selected_ref
        )
        static = next(
            item for item in solver_execution.result.evaluations if item.ref == selected_static_ref
        )
        central_dynamic = next(
            item for item in dynamic.evaluations if item.ref == selected_dynamic_ref
        )
        if path.exists():
            validation = AnchorValidationResult.from_mapping(_read_json(path))
            _validate_anchor_result(
                validation,
                request=request,
                intent=intent,
                context=context,
                bundle=bundle,
                problem=problem,
                selected_action=proposal.decision_values,
            )
            for attempt in validation.attempts:
                _replay_anchor_attempt(
                    attempt,
                    catalog=bundle.catalog,
                    request_factory=self._request_factory,
                    run_dir=run_dir,
                    simulator=simulator,
                )
            recovered.append("anchor-validation-ready")
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "anchor-validation-ready",
                validation.ref,
            )
            return validation, 0, 0

        ratios = (
            (1.0,)
            if request.coverage_policy == "point"
            else problem.evaluation_plan.context_anchor_ratios
        )
        attempts: list[AnchorAttempt] = []
        physical_m2 = 0
        physical_m4 = 0
        for ratio in ratios:
            if abs(ratio - 1.0) <= 1e-12:
                attempts.append(
                    AnchorAttempt(
                        ratio=ratio,
                        context=context,
                        problem=problem,
                        proposal=proposal,
                        static_evaluation=static,
                        dynamic_evaluation=central_dynamic,
                    )
                )
                continue
            anchor_context = _anchor_context(context, ratio)
            anchor_problem = UnifiedProblemBuilder().build(bundle, intent, anchor_context)
            anchor_proposal = CandidateProposal(
                schema_version=CANDIDATE_SCHEMA_VERSION,
                proposal_version="candidate-proposal",
                candidate_id=f"anchor-{_ratio_id(ratio)}",
                sequence=0,
                origin="anchor-validation",
                problem_ref=anchor_problem.ref,
                context_ref=anchor_context.ref,
                decision_values=proposal.decision_values,
                output_kind=proposal.output_kind,
                claim_scope=ENGINEERING_CLAIM_SCOPE,
            )
            m2_service = UnifiedM2EvaluationService(
                anchor_problem,
                anchor_context,
                bundle.catalog,
                UnifiedCandidatePlanCompiler(bundle.catalog),
                self._request_factory,
                simulator,
            )
            anchor_static = _relativize_evaluation(m2_service.evaluate(anchor_proposal), run_dir)
            physical_m2 += m2_service.physical_execution_count
            anchor_dynamic: CandidateEvaluation | None = None
            if anchor_static.status == "feasible":
                m4_service = UnifiedM4EvaluationService(
                    anchor_problem,
                    anchor_context,
                    bundle.catalog,
                    UnifiedCandidatePlanCompiler(bundle.catalog),
                    self._request_factory,
                    simulator,
                )
                anchor_dynamic = _relativize_evaluation(
                    m4_service.evaluate(anchor_proposal), run_dir
                )
                physical_m4 += m4_service.physical_execution_count
            attempts.append(
                AnchorAttempt(
                    ratio=ratio,
                    context=anchor_context,
                    problem=anchor_problem,
                    proposal=anchor_proposal,
                    static_evaluation=anchor_static,
                    dynamic_evaluation=anchor_dynamic,
                )
            )
        validation = AnchorValidationResult(
            schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
            schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
            validation_version="sampled-anchor-validation",
            central_problem_ref=problem.ref,
            selected_action=proposal.decision_values,
            attempts=tuple(attempts),
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
        for attempt in validation.attempts:
            _replay_anchor_attempt(
                attempt,
                catalog=bundle.catalog,
                request_factory=self._request_factory,
                run_dir=run_dir,
                simulator=simulator,
            )
        _write_or_verify(path, validation.as_dict())
        _ensure_event(
            events,
            run_dir,
            request.ref,
            "anchor-validation-ready",
            validation.ref,
        )
        return validation, physical_m2, physical_m4

    def _strategy(
        self,
        run_dir: Path,
        repository: StrategyRepository,
        actor: str,
        problem: OptimizationProblem,
        context: OperatingContext,
        solver_execution: SolverExecutionArtifact,
        dynamic: DynamicVerificationArtifact,
        finalization: FinalizationArtifacts,
        anchors: AnchorValidationResult | None,
        events: list[WorkflowEvent],
        request: OfflineRtoRequest,
        recovered: list[str],
    ) -> StrategyEntry | None:
        path = run_dir / "strategy_draft.json"
        _reject_event_without_artifacts(events, "strategy-draft-ready", (path,))
        if finalization.result.status != "success" or anchors is None or not anchors.passed:
            if path.exists():
                raise ValueError("workflow without passing anchors cannot contain a strategy")
            return None
        expected = _build_expected_strategy(
            problem,
            context,
            solver_execution,
            dynamic,
            finalization,
            anchors,
        )
        if path.exists():
            strategy = StrategyEntry.from_mapping(_read_json(path))
            if strategy != expected:
                raise ValueError("stored strategy draft differs from deterministic reconstruction")
            stored_record = repository.read(strategy.strategy_id, strategy.revision)
            if stored_record.entry != strategy:
                raise ValueError("strategy repository payload differs from workflow draft")
            recovered.append("strategy-draft-ready")
        else:
            strategy = expected
            repository.create_draft(strategy, actor=actor)
            _write_or_verify(path, strategy.as_dict())
        _ensure_event(
            events,
            run_dir,
            request.ref,
            "strategy-draft-ready",
            strategy.ref,
        )
        return strategy


def _build_expected_strategy(
    problem: OptimizationProblem,
    context: OperatingContext,
    solver_execution: SolverExecutionArtifact,
    dynamic: DynamicVerificationArtifact,
    finalization: FinalizationArtifacts,
    anchors: AnchorValidationResult,
) -> StrategyEntry:
    # Imported lazily so legacy readers remain independent of the unified strategy package.
    from ..strategies.unified import StrategyBuilder, anchor_from_verified_candidate

    selected_ref = finalization.result.selected_proposal_ref
    static_ref = finalization.result.selected_static_evaluation_ref
    dynamic_ref = finalization.result.selected_dynamic_evaluation_ref
    if selected_ref is None or static_ref is None or dynamic_ref is None:
        raise ValueError("successful finalization lacks selected strategy evidence")
    proposal = next(item for item in solver_execution.result.proposals if item.ref == selected_ref)
    static = next(item for item in solver_execution.result.evaluations if item.ref == static_ref)
    selected_dynamic = next(item for item in dynamic.evaluations if item.ref == dynamic_ref)
    additional = tuple(
        anchor_from_verified_candidate(
            attempt.problem,
            attempt.context,
            attempt.proposal,
            attempt.static_evaluation,
            cast(CandidateEvaluation, attempt.dynamic_evaluation),
            finalization_result_ref=finalization.result.ref,
        )
        for attempt in anchors.attempts
        if attempt.context.ref != context.ref
    )
    return StrategyBuilder().build(
        problem,
        context,
        proposal,
        static,
        selected_dynamic,
        finalization,
        additional_anchors=additional,
    )


def _solver_registry() -> SolverRegistry:
    return SolverRegistry((CoarseRefineGridSolver(), FullGridParetoSolver()))


def _unsupported_solver_result(
    problem: OptimizationProblem,
    routing: SolverRoutingDecision,
) -> SolverResult:
    return SolverResult(
        schema_version=SOLVER_RESULT_SCHEMA_VERSION,
        result_version="unsupported-solver-result",
        status="unsupported_problem",
        problem_ref=problem.ref,
        solver_ref=ContractRef("unsupported-solver-route", routing.fingerprint),
        proposals=(),
        evaluations=(),
        solution_representation="ordered",
        solution_groups=(),
        termination_reason="no-compatible-solver",
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def _validate_solver_execution(
    problem: OptimizationProblem,
    routing: SolverRoutingDecision,
    artifact: SolverExecutionArtifact,
    solver: object,
) -> None:
    if artifact.routing_fingerprint != routing.fingerprint:
        raise ValueError("solver execution references another routing decision")
    if artifact.result.problem_ref != problem.ref:
        raise ValueError("solver execution references another problem")
    if solver is None:
        expected = _unsupported_solver_result(problem, routing)
    else:
        if not hasattr(solver, "solve"):
            raise TypeError("selected solver does not implement solve")
        expected = solver.solve(problem, _ReplayEvaluator(artifact.result.evaluations))
    if artifact.result != expected:
        raise ValueError("stored solver execution differs from deterministic replay")


def _validate_dynamic_artifact(
    problem: OptimizationProblem,
    selection: StaticPreferenceSelection,
    artifact: DynamicVerificationArtifact,
) -> None:
    if artifact.problem_ref != problem.ref or artifact.static_selection_ref != selection.ref:
        raise ValueError("dynamic verification references another problem or selection")
    refs = tuple(item.proposal_ref for item in artifact.evaluations)
    if selection.status == "ready":
        if refs != selection.shortlist_proposal_refs:
            raise ValueError("dynamic verification must cover the full shortlist in order")
        expected_reason = "dynamic-shortlist-evaluated"
    elif artifact.evaluations:
        raise ValueError("non-ready selection cannot contain dynamic evaluations")
    else:
        expected_reason = "static-selection-not-ready"
    if (
        artifact.verification_version != "dynamic-verification"
        or artifact.termination_reason != expected_reason
    ):
        raise ValueError("dynamic verification metadata differs from deterministic execution")


def _anchor_context(context: OperatingContext, ratio: float) -> OperatingContext:
    facts = cast(dict[str, JsonValue], thaw_json(cast(JsonValue, context.facts)))
    feed = facts.get("fresh_feed_load_kg_s")
    if isinstance(feed, bool) or not isinstance(feed, (int, float)):
        raise TypeError("anchor context requires numeric fresh_feed_load_kg_s")
    facts["fresh_feed_load_kg_s"] = float(feed) * ratio
    return replace(
        context,
        context_id=f"{context.context_id}-feed-{_ratio_id(ratio)}",
        facts=facts,
    )


def _ratio_id(ratio: float) -> str:
    return f"{ratio:.6f}".rstrip("0").rstrip(".").replace(".", "p")


def _validate_anchor_result(
    validation: AnchorValidationResult,
    *,
    request: OfflineRtoRequest,
    intent: OptimizationIntent,
    context: OperatingContext,
    bundle: UnifiedCapabilityBundle,
    problem: OptimizationProblem,
    selected_action: Mapping[str, float],
) -> None:
    if validation.central_problem_ref != problem.ref or dict(validation.selected_action) != dict(
        selected_action
    ):
        raise ValueError("anchor validation differs from the selected central action")
    ratios = (
        (1.0,)
        if request.coverage_policy == "point"
        else problem.evaluation_plan.context_anchor_ratios
    )
    if tuple(item.ratio for item in validation.attempts) != ratios:
        raise ValueError("anchor ratios differ from the trusted coverage policy")
    for attempt in validation.attempts:
        expected_context = (
            context
            if abs(attempt.ratio - 1.0) <= 1e-12
            else _anchor_context(context, attempt.ratio)
        )
        expected_problem = UnifiedProblemBuilder().build(bundle, intent, expected_context)
        if attempt.context != expected_context or attempt.problem != expected_problem:
            raise ValueError("stored anchor inputs differ from deterministic reconstruction")


def _offline_result(
    request: OfflineRtoRequest,
    problem: OptimizationProblem,
    routing: SolverRoutingDecision,
    solver_execution: SolverExecutionArtifact,
    selection: StaticPreferenceSelection,
    dynamic: DynamicVerificationArtifact,
    finalization: FinalizationArtifact,
    anchors: AnchorValidationResult | None,
    strategy: StrategyEntry | None,
) -> OfflineRtoResult:
    if (
        finalization.result.status == "success"
        and anchors is not None
        and anchors.passed
        and strategy is None
    ):
        raise ValueError("successful verified finalization requires a strategy draft")
    if strategy is not None and (
        finalization.result.status != "success" or anchors is None or not anchors.passed
    ):
        raise ValueError("strategy draft requires successful finalization and anchor coverage")
    if strategy is not None:
        status: OfflineRunStatus = "completed_draft"
        reason = "strategy-draft-created"
    elif finalization.result.status in {
        "invalid_request",
        "evaluation_error",
        "unsupported_problem",
    }:
        status = "failed"
        reason = finalization.result.termination_reason
    elif anchors is not None and not anchors.passed:
        status = "completed_without_strategy"
        reason = "anchor-validation-failed"
    else:
        status = "completed_without_strategy"
        reason = finalization.result.termination_reason
    return OfflineRtoResult(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        result_version="offline-rto-result",
        status=status,
        request_ref=request.ref,
        problem_ref=problem.ref,
        routing_ref=routing_ref(routing),
        solver_execution_ref=solver_execution.ref,
        static_selection_ref=selection.ref,
        dynamic_verification_ref=dynamic.ref,
        finalization_ref=finalization.ref,
        anchor_validation_ref=None if anchors is None else anchors.ref,
        strategy_ref=None if strategy is None else strategy.ref,
        requested_anchor_count=0 if anchors is None else len(anchors.attempts),
        passed_anchor_count=(
            0 if anchors is None else sum(item.passed for item in anchors.attempts)
        ),
        termination_reason=reason,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )


def read_offline_run(
    run_dir: Path,
    *,
    strategy_repository: StrategyRepository,
    request_factory: UnifiedProviderRequestFactory,
    simulator: UnifiedSimulatorPort,
    expected_intent: OptimizationIntent | None = None,
    expected_context: OperatingContext | None = None,
    expected_bundle: UnifiedCapabilityBundle | None = None,
) -> OfflineRtoRunRecord:
    """Strictly reload and deterministically replay one completed unified run."""

    run_dir = run_dir.resolve()
    manifest = OfflineRtoManifest.from_mapping(_read_json(run_dir / "manifest.json"))
    _verify_manifest(run_dir, manifest)
    request = OfflineRtoRequest.from_mapping(_read_json(run_dir / "request.json"))
    intent = OptimizationIntent.from_mapping(_read_json(run_dir / "intent.json"))
    context = OperatingContext.from_mapping(_read_json(run_dir / "context.json"))
    snapshot = CapabilityBundleSnapshot.from_mapping(_read_json(run_dir / "capability_bundle.json"))
    bundle = snapshot.bundle
    expected_snapshot = CapabilityBundleSnapshot(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        snapshot_version="capability-bundle-snapshot",
        bundle=bundle,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    if snapshot != expected_snapshot:
        raise ValueError("capability bundle snapshot metadata is unsupported")
    if expected_intent is not None and intent != expected_intent:
        raise ValueError("stored intent differs from the caller-supplied intent")
    if expected_context is not None and context != expected_context:
        raise ValueError("stored context differs from the caller-supplied context")
    if expected_bundle is not None and bundle != expected_bundle:
        raise ValueError("stored capability bundle differs from the caller-supplied bundle")
    policy = build_solver_routing_policy(bundle)
    expected_request = OfflineRtoRequest(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        request_version="offline-rto-request",
        intent_ref=ContractRef(intent.intent_id, intent.fingerprint),
        context_ref=context.ref,
        capability_catalog_ref=bundle.catalog.ref,
        context_schema_ref=bundle.context_schema.ref,
        system_policy_ref=bundle.system_policy.ref,
        solver_policy_ref=ContractRef(policy.policy_id, policy.fingerprint),
        provider_id=request.provider_id,
        coverage_policy=request.coverage_policy,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    if request != expected_request:
        raise ValueError("stored request differs from its nested immutable inputs")
    if request_factory.provider_id != request.provider_id:
        raise ValueError("provider request factory differs from the workflow request")
    if manifest.workflow_ref != request.ref:
        raise ValueError("manifest references another workflow request")

    problem = OptimizationProblem.from_mapping(_read_json(run_dir / "problem.json"))
    expected_problem = UnifiedProblemBuilder().build(bundle, intent, context)
    if problem != expected_problem:
        raise ValueError("stored problem differs from deterministic reconstruction")
    registry = _solver_registry()
    features = ProblemFeatureAnalyzer().analyze(problem)
    route = SolverRouter().route(features, registry, policy)
    routing = SolverRoutingDecision.from_mapping(_read_json(run_dir / "solver_route.json"))
    if routing != route.decision:
        raise ValueError("stored solver route differs from deterministic routing")
    solver_execution = SolverExecutionArtifact.from_mapping(
        _read_json(run_dir / "static_solve.json")
    )
    _validate_solver_execution(problem, routing, solver_execution, route.solver)
    _replay_evaluations(
        solver_execution.result.evaluations,
        proposals=solver_execution.result.proposals,
        problem=problem,
        context=context,
        catalog=bundle.catalog,
        request_factory=request_factory,
        run_dir=run_dir,
        simulator=simulator,
    )

    m2 = {item.proposal_ref: item for item in solver_execution.result.evaluations}
    selector = UnifiedFinalSelector()
    static_selection = StaticPreferenceSelection.from_mapping(
        _read_json(run_dir / "static_selection.json")
    )
    expected_selection = selector.rank_static(problem, solver_execution.result, m2)
    if static_selection != expected_selection:
        raise ValueError("stored static selection differs from deterministic replay")
    dynamic = DynamicVerificationArtifact.from_mapping(
        _read_json(run_dir / "dynamic_evaluations.json")
    )
    _validate_dynamic_artifact(problem, static_selection, dynamic)
    _replay_evaluations(
        dynamic.evaluations,
        proposals=solver_execution.result.proposals,
        problem=problem,
        context=context,
        catalog=bundle.catalog,
        request_factory=request_factory,
        run_dir=run_dir,
        simulator=simulator,
    )
    m4 = {item.proposal_ref: item for item in dynamic.evaluations}
    expected_final = selector.select(problem, solver_execution.result, m2, m4, bundle)
    finalization = FinalizationArtifact.from_mapping(_read_json(run_dir / "finalization.json"))
    expected_final_artifact = FinalizationArtifact(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        artifact_version="finalization-artifact",
        static_selection_ref=expected_final.static_selection.ref,
        publishability=expected_final.publishability,
        result=expected_final.result,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    if finalization != expected_final_artifact:
        raise ValueError("stored finalization differs from deterministic replay")

    anchor_path = run_dir / "anchor_validation.json"
    anchor_validation = (
        None
        if not anchor_path.exists()
        else AnchorValidationResult.from_mapping(_read_json(anchor_path))
    )
    if anchor_validation is not None:
        selected_ref = finalization.result.selected_proposal_ref
        if selected_ref is None:
            raise ValueError("anchor validation exists without a selected proposal")
        selected = next(
            item for item in solver_execution.result.proposals if item.ref == selected_ref
        )
        _validate_anchor_result(
            anchor_validation,
            request=request,
            intent=intent,
            context=context,
            bundle=bundle,
            problem=problem,
            selected_action=selected.decision_values,
        )
        for attempt in anchor_validation.attempts:
            _replay_anchor_attempt(
                attempt,
                catalog=bundle.catalog,
                request_factory=request_factory,
                run_dir=run_dir,
                simulator=simulator,
            )
    elif finalization.result.status == "success":
        raise ValueError("successful finalization requires explicit coverage validation")

    strategy_path = run_dir / "strategy_draft.json"
    strategy = (
        None
        if not strategy_path.exists()
        else StrategyEntry.from_mapping(_read_json(strategy_path))
    )
    if strategy is not None:
        if anchor_validation is None or not anchor_validation.passed:
            raise ValueError("strategy draft exists without passing anchor coverage")
        expected_strategy = _build_expected_strategy(
            problem,
            context,
            solver_execution,
            dynamic,
            expected_final,
            anchor_validation,
        )
        if strategy != expected_strategy:
            raise ValueError("strategy draft differs from deterministic reconstruction")
        stored_record = strategy_repository.read(strategy.strategy_id, strategy.revision)
        if stored_record.entry != strategy:
            raise ValueError("strategy repository payload differs from workflow draft")

    result = OfflineRtoResult.from_mapping(_read_json(run_dir / "result.json"))
    expected_result = _offline_result(
        request,
        problem,
        routing,
        solver_execution,
        static_selection,
        dynamic,
        finalization,
        anchor_validation,
        strategy,
    )
    if result != expected_result or manifest.result_ref != result.ref:
        raise ValueError("stored offline result or manifest differs from deterministic replay")
    _verify_manifest(run_dir, manifest, result=result)
    events = _read_events(run_dir / "events.jsonl", request.ref, allow_missing=False)
    _validate_event_stages(
        events,
        request=request,
        problem=problem,
        routing=routing,
        solver_execution=solver_execution,
        selection=static_selection,
        dynamic=dynamic,
        finalization=finalization,
        anchors=anchor_validation,
        strategy=strategy,
        result=result,
    )
    return OfflineRtoRunRecord(
        run_dir=run_dir,
        request=request,
        intent=intent,
        context=context,
        capability_snapshot=snapshot,
        problem=problem,
        routing=routing,
        solver_execution=solver_execution,
        static_selection=static_selection,
        dynamic_verification=dynamic,
        finalization=finalization,
        anchor_validation=anchor_validation,
        strategy=strategy,
        result=result,
        manifest=manifest,
        events=events,
        recovered_stages=tuple(item.stage for item in events) + ("manifest-committed",),
        physical_m2_executions=0,
        physical_m4_executions=0,
    )


def _relativize_solver_result(result: SolverResult, run_dir: Path) -> SolverResult:
    return replace(
        result,
        evaluations=tuple(_relativize_evaluation(item, run_dir) for item in result.evaluations),
    )


def _relativize_evaluation(
    evaluation: CandidateEvaluation,
    run_dir: Path,
) -> CandidateEvaluation:
    evidence = tuple(
        replace(item, run_ref=_relative_run_ref(item.run_ref, run_dir))
        for item in evaluation.evidence_refs
    )
    return replace(evaluation, evidence_refs=evidence)


def _relative_run_ref(value: str, run_dir: Path) -> str:
    source = Path(value)
    if not source.is_absolute():
        raise ValueError("simulator evidence must initially provide an absolute run_ref")
    resolved = source.resolve()
    try:
        relative = resolved.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            "unified simulator evidence must live inside its workflow directory"
        ) from exc
    if not relative.parts or relative.parts[0] != "simulator" or ".." in relative.parts:
        raise ValueError("unified simulator evidence must use a safe simulator-relative path")
    return relative.as_posix()


def _replay_evaluations(
    evaluations: tuple[CandidateEvaluation, ...],
    *,
    proposals: tuple[CandidateProposal, ...],
    problem: OptimizationProblem,
    context: OperatingContext,
    catalog: CapabilityCatalog,
    request_factory: UnifiedProviderRequestFactory,
    run_dir: Path,
    simulator: UnifiedSimulatorPort,
) -> None:
    proposal_by_ref = {item.ref: item for item in proposals}
    if len(proposal_by_ref) != len(proposals):
        raise ValueError("candidate proposals contain duplicate semantic identities")
    compiler = UnifiedCandidatePlanCompiler(catalog)
    for evaluation in evaluations:
        try:
            proposal = proposal_by_ref[evaluation.proposal_ref]
        except KeyError as exc:
            raise ValueError("candidate evaluation lacks its proposal") from exc
        if len(evaluation.evidence_refs) != 2:
            raise ValueError("candidate evaluation lacks replayable paired evidence")
        evidence_by_role = {item.pair_role: item for item in evaluation.evidence_refs}
        if set(evidence_by_role) != {"baseline", "candidate"}:
            raise ValueError("candidate evaluation evidence roles are incomplete")
        pair = compiler.compile_pair(
            problem,
            context,
            proposal,
            stage=evaluation.stage,
            request_factory=request_factory,
        )
        baseline = _reload_evidence(
            evidence_by_role["baseline"], run_dir=run_dir, simulator=simulator
        )
        candidate = _reload_evidence(
            evidence_by_role["candidate"], run_dir=run_dir, simulator=simulator
        )
        if evaluation.stage == "M2":
            replayed = UnifiedM2PairedEvaluator(problem, catalog).evaluate(
                proposal, pair, baseline, candidate
            )
        else:
            replayed = UnifiedM4PairedEvaluator(problem, catalog).evaluate(
                proposal, pair, baseline, candidate
            )
        if replayed.ref != evaluation.ref:
            raise ValueError("candidate evaluation differs from strict evidence replay")


def _replay_anchor_attempt(
    attempt: AnchorAttempt,
    *,
    catalog: CapabilityCatalog,
    request_factory: UnifiedProviderRequestFactory,
    run_dir: Path,
    simulator: UnifiedSimulatorPort,
) -> None:
    _replay_evaluations(
        (attempt.static_evaluation,),
        proposals=(attempt.proposal,),
        problem=attempt.problem,
        context=attempt.context,
        catalog=catalog,
        request_factory=request_factory,
        run_dir=run_dir,
        simulator=simulator,
    )
    if attempt.dynamic_evaluation is not None:
        _replay_evaluations(
            (attempt.dynamic_evaluation,),
            proposals=(attempt.proposal,),
            problem=attempt.problem,
            context=attempt.context,
            catalog=catalog,
            request_factory=request_factory,
            run_dir=run_dir,
            simulator=simulator,
        )


def _reload_evidence(
    evidence: RunEvidenceRef,
    *,
    run_dir: Path,
    simulator: UnifiedSimulatorPort,
) -> SimulationRunBundle:
    relative = Path(evidence.run_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unified run evidence locator must be relative and traversal-free")
    resolved = (run_dir / relative).resolve()
    simulator_root = (run_dir / "simulator").resolve()
    if not resolved.is_relative_to(simulator_root):
        raise ValueError("unified run evidence locator escapes the simulator directory")
    reloaded = simulator.read_evidence(resolved)
    actual = RunEvidenceRef.from_bundle(reloaded, pair_role=evidence.pair_role)
    if actual.semantic_payload() != evidence.semantic_payload():
        raise ValueError("strictly reloaded simulator evidence differs from workflow refs")
    return reloaded


def _validate_event_stages(
    events: tuple[WorkflowEvent, ...],
    *,
    request: OfflineRtoRequest,
    problem: OptimizationProblem,
    routing: SolverRoutingDecision,
    solver_execution: SolverExecutionArtifact,
    selection: StaticPreferenceSelection,
    dynamic: DynamicVerificationArtifact,
    finalization: FinalizationArtifact,
    anchors: AnchorValidationResult | None,
    strategy: StrategyEntry | None,
    result: OfflineRtoResult,
) -> None:
    expected: list[tuple[str, ContractRef]] = [
        ("inputs-ready", request.ref),
        ("problem-ready", problem.ref),
        ("route-ready", routing_ref(routing)),
        ("static-solve-ready", solver_execution.ref),
        ("static-selection-ready", selection.ref),
        ("dynamic-evaluations-ready", dynamic.ref),
        ("finalization-ready", finalization.ref),
    ]
    if anchors is not None:
        expected.append(("anchor-validation-ready", anchors.ref))
    if strategy is not None:
        expected.append(("strategy-draft-ready", strategy.ref))
    expected.append(("workflow-complete", result.ref))
    actual = tuple((item.stage, item.object_ref) for item in events)
    if actual != tuple(expected):
        raise ValueError("workflow event stages or object refs differ from committed artifacts")


def _expected_manifest_files(result: OfflineRtoResult) -> frozenset[str]:
    expected = set(_REQUIRED_MANIFEST_FILES)
    if result.anchor_validation_ref is not None:
        expected.add("anchor_validation.json")
    if result.strategy_ref is not None:
        expected.add("strategy_draft.json")
    return frozenset(expected)


def _verify_manifest(
    run_dir: Path,
    manifest: OfflineRtoManifest,
    *,
    result: OfflineRtoResult | None = None,
) -> None:
    if dict(manifest.software_versions) != _SOFTWARE_VERSIONS:
        raise ValueError("workflow manifest software versions are unsupported")
    declared = set(manifest.files)
    allowed = _REQUIRED_MANIFEST_FILES | _OPTIONAL_MANIFEST_FILES
    if not _REQUIRED_MANIFEST_FILES.issubset(declared) or not declared.issubset(allowed):
        raise ValueError("workflow manifest contains a missing or unsupported artifact name")
    if result is not None and declared != set(_expected_manifest_files(result)):
        raise ValueError("workflow manifest files differ from the committed result shape")
    _validate_top_level_entries(
        run_dir,
        expected_files=frozenset(manifest.files),
        manifest_required=True,
    )
    for name, expected in manifest.files.items():
        if _file_sha256(run_dir / name) != expected:
            raise ValueError(f"workflow artifact hash differs from manifest: {name}")


def _commit_manifest(
    run_dir: Path,
    request: OfflineRtoRequest,
    result: OfflineRtoResult,
) -> OfflineRtoManifest:
    path = run_dir / "manifest.json"
    if path.exists():
        manifest = OfflineRtoManifest.from_mapping(_read_json(path))
        _verify_manifest(run_dir, manifest, result=result)
        return manifest
    expected_names = _expected_manifest_files(result)
    _validate_top_level_entries(
        run_dir,
        expected_files=expected_names,
        manifest_required=False,
    )
    files = {name: _file_sha256(run_dir / name) for name in sorted(expected_names)}
    manifest = OfflineRtoManifest(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        manifest_version=UNIFIED_MANIFEST_VERSION,
        workflow_ref=request.ref,
        result_ref=result.ref,
        files=files,
        software_versions=_SOFTWARE_VERSIONS,
        created_at=_utc_now(),
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    _write_or_verify(path, manifest.as_dict())
    _verify_manifest(run_dir, manifest, result=result)
    return manifest


def _validate_top_level_entries(
    run_dir: Path,
    *,
    expected_files: frozenset[str],
    manifest_required: bool,
) -> None:
    entries = {item.name: item for item in run_dir.iterdir()}
    allowed = set(expected_files) | {"manifest.json", ".workflow.lock", "simulator"}
    unexpected = set(entries) - allowed
    if unexpected:
        raise ValueError(f"workflow contains an unexpected top-level entry: {sorted(unexpected)!r}")
    if any(item.is_symlink() for item in entries.values()):
        raise ValueError("workflow contains a symbolic link")
    for name in expected_files:
        item = entries.get(name)
        if item is None or not item.is_file():
            raise ValueError(f"workflow artifact must be a regular top-level file: {name}")
    manifest = entries.get("manifest.json")
    if manifest_required and (manifest is None or not manifest.is_file()):
        raise ValueError("workflow manifest must be a regular top-level file")
    if not manifest_required and manifest is not None:
        raise ValueError("workflow manifest already exists before commit")
    lock = entries.get(".workflow.lock")
    if lock is not None and not lock.is_file():
        raise ValueError("workflow lock must be a regular top-level file")
    simulator = entries.get("simulator")
    if simulator is not None and not simulator.is_dir():
        raise ValueError("workflow simulator entry must be a top-level directory")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"workflow artifact must not be a symbolic link: {path.name}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"workflow artifact is not valid UTF-8 JSON: {path.name}") from exc
    return dict(as_mapping(value, context=path.name))


def _reject_constant(value: str) -> object:
    raise ValueError(f"workflow JSON contains non-finite constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"workflow JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _write_or_verify(path: Path, value: Mapping[str, object]) -> None:
    if path.is_symlink():
        raise ValueError(f"workflow artifact must not be a symbolic link: {path.name}")
    payload = canonical_json_bytes(value) + b"\n"
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"existing workflow artifact differs: {path.name}")
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_events(
    path: Path,
    workflow_ref: ContractRef,
    *,
    allow_missing: bool,
) -> tuple[WorkflowEvent, ...]:
    if not path.exists():
        if allow_missing:
            return ()
        raise ValueError("workflow event chain is missing")
    if path.is_symlink():
        raise ValueError("workflow event chain must not be a symbolic link")
    try:
        payload = path.read_bytes()
        if payload and not payload.endswith(b"\n"):
            raise ValueError("workflow event chain has an incomplete final line")
        text_payload = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("workflow event chain is not valid UTF-8") from exc
    events: list[WorkflowEvent] = []
    for line_number, raw in enumerate(text_payload.splitlines(), start=1):
        if not raw.strip():
            raise ValueError(f"workflow event line {line_number} is empty")
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        event = WorkflowEvent.from_mapping(as_mapping(value, context="workflow event"))
        expected_previous = None if not events else events[-1].fingerprint
        if (
            event.workflow_ref != workflow_ref
            or event.sequence != len(events)
            or event.previous_event_fingerprint != expected_previous
        ):
            raise ValueError("workflow event chain identity or continuity is invalid")
        if event.stage not in _STAGE_ORDER:
            raise ValueError("workflow event contains an unsupported stage")
        if events and _STAGE_ORDER[event.stage] <= _STAGE_ORDER[events[-1].stage]:
            raise ValueError("workflow event stages are not strictly ordered")
        events.append(event)
    result = tuple(events)
    _validate_event_prefix(result)
    return result


_STAGE_ORDER = {
    stage: index
    for index, stage in enumerate(
        (
            *_STATIC_STAGES,
            "anchor-validation-ready",
            "strategy-draft-ready",
            "workflow-complete",
        )
    )
}

_LEGAL_EVENT_BRANCHES = (
    (*_STATIC_STAGES, "workflow-complete"),
    (*_STATIC_STAGES, "anchor-validation-ready", "workflow-complete"),
    (
        *_STATIC_STAGES,
        "anchor-validation-ready",
        "strategy-draft-ready",
        "workflow-complete",
    ),
)


def _validate_event_prefix(events: tuple[WorkflowEvent, ...]) -> None:
    stages = tuple(item.stage for item in events)
    if not any(stages == branch[: len(stages)] for branch in _LEGAL_EVENT_BRANCHES):
        raise ValueError("workflow event stages are not a legal contiguous branch prefix")


def _ensure_event(
    events: list[WorkflowEvent],
    run_dir: Path,
    workflow_ref: ContractRef,
    stage: str,
    object_ref: ContractRef,
) -> None:
    matches = [item for item in events if item.stage == stage]
    if matches:
        if len(matches) != 1 or matches[0].object_ref != object_ref:
            raise ValueError(f"stored workflow event differs for stage {stage}")
        return
    current_rank = _STAGE_ORDER[stage]
    if any(_STAGE_ORDER[item.stage] > current_rank for item in events):
        raise ValueError(f"workflow event exists after missing stage {stage}")
    event = WorkflowEvent(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        event_version="workflow-event",
        workflow_ref=workflow_ref,
        sequence=len(events),
        stage=stage,
        object_ref=object_ref,
        occurred_at=_utc_now(),
        previous_event_fingerprint=None if not events else events[-1].fingerprint,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    path = run_dir / "events.jsonl"
    was_missing = not path.exists()
    payload = canonical_json_bytes(event.as_dict()) + b"\n"
    with path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if was_missing:
        _fsync_directory(run_dir)
    events.append(event)


def _reject_event_without_artifacts(
    events: list[WorkflowEvent],
    stage: str,
    paths: tuple[Path, ...],
) -> None:
    if any(item.stage == stage for item in events) and any(not path.exists() for path in paths):
        missing = ", ".join(path.name for path in paths if not path.exists())
        raise ValueError(
            f"workflow event exists but committed artifact is missing for {stage}: {missing}"
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _workflow_lock(run_dir: Path) -> Iterator[None]:
    with exclusive_file_lock(
        run_dir / ".workflow.lock",
        label=f"workflow {run_dir.name}",
    ):
        yield


__all__ = ["OfflineRtoOrchestrator", "OfflineRtoRunRecord", "read_offline_run"]
