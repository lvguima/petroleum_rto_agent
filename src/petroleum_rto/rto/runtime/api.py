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
from .chat_summary import build_optimization_run_summary

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


def _build_problem_inputs(
    *,
    bundle: CapabilityBundle,
    intent: OptimizationIntent,
    context_file: Path,
) -> tuple[OperatingContext, OptimizationProblem]:
    context = load_operating_context(context_file)
    problem = ProblemBuilder().build(bundle, intent, context)
    return context, problem


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


def _run_problem(
    *,
    bundle: CapabilityBundle,
    intent: OptimizationIntent,
    context: OperatingContext,
    problem: OptimizationProblem,
    run_root: Path,
    coverage_policy: CoveragePolicy,
) -> OfflineRtoRunRecord:
    return OfflineRtoOrchestrator(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run(
        bundle,
        intent,
        context,
        problem,
        run_root=run_root,
        coverage_policy=coverage_policy,
    )


def run_confirmed_optimization(
    *,
    repo_root: Path | None,
    intent: OptimizationIntent,
    context_file: Path,
    run_root: Path,
    coverage_policy: CoveragePolicy = "point",
) -> dict[str, object]:
    """Build from the latest trusted context and return a compact result receipt."""

    bundle = load_capability_bundle(repo_root)
    resolution = IntentResolver().resolve(intent, BundleCapabilityView(bundle))
    if resolution.status != "resolved" or resolution.resolved_intent is None:
        raise ValueError(
            f"optimization intent is {resolution.status} under the current capabilities"
        )
    resolved_intent = resolution.resolved_intent
    context, problem = _build_problem_inputs(
        bundle=bundle,
        intent=resolved_intent,
        context_file=context_file,
    )
    receipt = OfflineRtoOrchestrator(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run_compact(
        bundle,
        resolved_intent,
        context,
        problem,
        run_root=run_root,
        coverage_policy=coverage_policy,
    )
    return receipt.as_dict()


def run_offline(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
    run_root: Path,
    coverage_policy: CoveragePolicy = "point",
) -> OfflineRtoRunRecord:
    """Run or resume the objective-count-neutral offline workflow."""

    bundle, intent, context, problem = _load_problem_inputs(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )
    return _run_problem(
        bundle=bundle,
        intent=intent,
        context=context,
        problem=problem,
        run_root=run_root,
        coverage_policy=coverage_policy,
    )


def inspect_offline(run_dir: Path) -> OfflineRtoRunRecord:
    """Strictly reload a workflow and all referenced evidence."""

    resolved = run_dir.resolve()
    try:
        return read_offline_run(
            resolved,
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
    return build_optimization_run_summary(record).as_dict()


__all__ = [
    "OfflineInspectionError",
    "OfflineRunRecord",
    "approve_strategy",
    "build_intent_communication_service",
    "capabilities",
    "inspect_offline",
    "publish_strategy",
    "query_strategies",
    "run_confirmed_optimization",
    "run_offline",
    "run_summary",
    "validate_intent_file",
    "validate_problem_files",
]
