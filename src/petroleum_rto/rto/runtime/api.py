"""Stable Python API for the objective-count-neutral offline RTO workflow."""

from __future__ import annotations

from pathlib import Path

from ..adapters import CduM7RequestFactory, CduM7Simulator
from ..capabilities import (
    BundleCapabilityView,
    CapabilityBundle,
    build_public_capability_manifest,
    load_capability_bundle,
)
from ..communication import IntentCommunicationService
from ..communication import (
    build_intent_communication_service as _build_intent_communication_service,
)
from ..context import load_operating_context
from ..contracts.context import OperatingContext
from ..contracts.problem import OptimizationProblem
from ..intent import (
    IntentResolution,
    IntentResolver,
    OptimizationIntent,
    load_optimization_intent,
)
from ..orchestration import OfflineRtoOrchestrator, OfflineRtoRunRecord, read_offline_run
from ..orchestration.models import CoveragePolicy
from ..problem import ProblemBuilder
from ..strategies import (
    StrategyQuery,
    StrategyRecord,
    StrategyReleaseManifest,
    StrategyRepository,
)
from .chat_summary import build_chat_result_summary

type OfflineRunRecord = OfflineRtoRunRecord


class OfflineInspectionError(RuntimeError):
    """Stored workflow evidence could not be strictly recovered or replayed."""


def capabilities(*, repo_root: Path | None = None) -> dict[str, object]:
    """Return the sanitized capability surface without selecting a solver."""

    manifest = build_public_capability_manifest(load_capability_bundle(repo_root))
    return {
        **manifest.as_dict(),
        "capability_fingerprint": manifest.fingerprint,
        "solver_called": False,
    }


def build_intent_communication_service(
    *, repo_root: Path | None = None
) -> IntentCommunicationService:
    """Build the provider-neutral domain-model gateway."""

    return _build_intent_communication_service(repo_root=repo_root)


def validate_intent_file(*, repo_root: Path | None, intent_file: Path) -> IntentResolution:
    """Validate one context-free intent against the published capabilities."""

    bundle = load_capability_bundle(repo_root)
    intent = load_optimization_intent(intent_file)
    return IntentResolver().resolve(intent, BundleCapabilityView(bundle))


def _load_problem_inputs(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
) -> tuple[CapabilityBundle, OptimizationIntent, OperatingContext, OptimizationProblem]:
    bundle = load_capability_bundle(repo_root)
    intent = load_optimization_intent(intent_file)
    context = load_operating_context(context_file)
    problem = ProblemBuilder().build(bundle, intent, context)
    return bundle, intent, context, problem


def validate_problem_files(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
) -> OptimizationProblem:
    """Build one deterministic problem without routing or simulation."""

    return _load_problem_inputs(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )[3]


def run_offline(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
    run_root: Path,
    library_root: Path,
    actor: str,
    coverage_policy: CoveragePolicy = "point",
) -> OfflineRtoRunRecord:
    """Run or resume the objective-count-neutral offline workflow."""

    bundle, intent, context, problem = _load_problem_inputs(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )
    return OfflineRtoOrchestrator(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run(
        bundle,
        intent,
        context,
        problem,
        run_root=run_root,
        strategy_repository=StrategyRepository(library_root),
        actor=actor,
        coverage_policy=coverage_policy,
    )


def inspect_offline(
    run_dir: Path,
    *,
    library_root: Path,
) -> OfflineRtoRunRecord:
    """Strictly reload a workflow and all referenced evidence."""

    resolved = run_dir.resolve()
    try:
        return read_offline_run(
            resolved,
            strategy_repository=StrategyRepository(library_root),
            simulator=CduM7Simulator(resolved / "simulator"),
            request_factory=CduM7RequestFactory(),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OfflineInspectionError(f"offline evidence inspection failed: {exc}") from exc


def approve_strategy(
    *,
    library_root: Path,
    strategy_id: str,
    revision: int,
    actor: str,
    reason: str = "offline-human-review-approved",
) -> StrategyRecord:
    """Approve one draft after explicit human review."""

    return StrategyRepository(library_root).approve(
        strategy_id, revision, actor=actor, reason=reason
    )


def publish_strategy(
    *,
    library_root: Path,
    strategy_id: str,
    revision: int,
    actor: str,
    reason: str = "offline-library-release",
) -> StrategyReleaseManifest:
    """Publish one already-approved strategy."""

    return StrategyRepository(library_root).publish(
        strategy_id, revision, actor=actor, reason=reason
    )


def query_strategies(*, library_root: Path, query: StrategyQuery) -> tuple[StrategyRecord, ...]:
    """Query only published strategies at explicit sampled anchors."""

    return StrategyRepository(library_root).query(query)


def run_summary(record: OfflineRtoRunRecord) -> dict[str, object]:
    """Return a compact offline-only workflow summary."""

    if not isinstance(record, OfflineRtoRunRecord):
        raise TypeError("record must be an OfflineRtoRunRecord")
    selected_ref = record.finalization.result.selected_static_evaluation_ref
    selected = next(
        (item for item in record.solver_execution.result.evaluations if item.ref == selected_ref),
        None,
    )
    selected_setpoints = build_chat_result_summary(record)["selected_setpoints"]
    return {
        "manifest_version": record.manifest.manifest_version,
        "workflow_id": record.request.workflow_id,
        "intent_ref": record.request.intent_ref.as_dict(),
        "context_ref": record.request.context_ref.as_dict(),
        "problem_ref": record.problem.ref.as_dict(),
        "status": record.result.status,
        "optimization_status": record.finalization.result.status,
        "coverage_policy": record.request.coverage_policy,
        "objective_count": len(record.problem.objectives),
        "decision_count": len(record.problem.decision_domains),
        "result_mode": record.problem.result_request.mode,
        "selected_solver_id": record.routing.selected_solver_id,
        "static_evaluation_count": len(record.solver_execution.result.evaluations),
        "dynamic_shortlist_count": len(record.dynamic_verification.evaluations),
        "requested_anchor_count": record.result.requested_anchor_count,
        "passed_anchor_count": record.result.passed_anchor_count,
        "selected_setpoints": selected_setpoints,
        "selected_objectives": (
            [] if selected is None else [item.as_dict() for item in selected.objective_outcomes]
        ),
        "strategy_ref": (
            None if record.result.strategy_ref is None else record.result.strategy_ref.as_dict()
        ),
        "strategy_state": None if record.strategy is None else "draft",
        "run_dir": str(record.run_dir.resolve()),
        "manifest_fingerprint": record.manifest.fingerprint,
        "offline_result_fingerprint": record.result.fingerprint,
        "physical_m2_executions_this_call": record.physical_m2_executions,
        "physical_m4_executions_this_call": record.physical_m4_executions,
        "recovered_stages": list(record.recovered_stages),
        "execution_scope": "offline_simulation_only",
        "control_authority": "none",
        "field_validated": False,
        "dcs_write_capability": False,
    }


__all__ = [
    "OfflineInspectionError",
    "OfflineRunRecord",
    "approve_strategy",
    "build_intent_communication_service",
    "capabilities",
    "inspect_offline",
    "publish_strategy",
    "query_strategies",
    "run_offline",
    "run_summary",
    "validate_intent_file",
    "validate_problem_files",
]
