"""Stable Python API for unified and explicitly legacy offline RTO workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from ..adapters import CduM7RequestFactory, CduM7Simulator
from ..capabilities import (
    BundleCapabilityView,
    UnifiedCapabilityBundle,
    build_public_capability_manifest,
    load_capability_bundle,
)
from ..catalogs import load_rto_v1_bundle, load_rto_v2_bundle
from ..communication import IntentCommunicationService
from ..communication import (
    build_intent_communication_service as _build_intent_communication_service,
)
from ..context import load_operating_context
from ..contracts.context import OperatingContext
from ..contracts.models import RTO_SCHEMA_VERSION
from ..contracts.multiobjective import RTO_V2_SCHEMA_VERSION, ResolvedOptimizationIntentV2
from ..contracts.problem import OptimizationProblem
from ..inputs import (
    BoundExternalOptimizationRequestV1,
    BoundExternalOptimizationRequestV2,
    bind_external_optimization_request,
    bind_external_optimization_request_v2,
    capability_manifest_v2,
    load_domain_optimization_intent_v2,
    load_external_optimization_request,
    load_external_optimization_request_v2,
    validate_domain_intent_v2,
)
from ..inputs.v2_models import (
    ExternalOptimizationRequestV2,
    IntentValidationIssueV2,
    IntentValidationResultV2,
)
from ..orchestration import (
    LegacyOfflineRtoOrchestratorV1,
    LegacyOfflineRtoRunRecordV1,
    OfflineRtoOrchestratorV2,
    OfflineRtoRequestV1,
    OfflineRtoRunRecordV2,
    UnifiedOfflineRtoOrchestrator,
    UnifiedOfflineRtoRunRecord,
    read_legacy_offline_run_v1,
    read_offline_run_v2,
    read_unified_offline_run,
)
from ..orchestration.unified_models import (
    OFFLINE_WORKFLOW_SCHEMA_ID,
    OFFLINE_WORKFLOW_SCHEMA_VERSION,
    UNIFIED_MANIFEST_VERSION,
    CoveragePolicy,
)
from ..problem import UnifiedProblemBuilder
from ..strategies import (
    StrategyDraftRepositoryV2,
    StrategyQueryV1,
    StrategyRecordV1,
    StrategyReleaseManifestV1,
)
from ..strategies import (
    StrategyRepository as LegacyStrategyRepositoryV1,
)
from ..strategies.unified import (
    StrategyQuery,
    StrategyRecord,
    StrategyReleaseManifest,
)
from ..strategies.unified import (
    StrategyRepository as UnifiedStrategyRepository,
)
from ..unified_inputs import (
    IntentResolution,
    IntentResolver,
    OptimizationIntent,
    load_optimization_intent,
)
from .chat_summary import build_chat_result_summary

type OfflineRunRecord = (
    UnifiedOfflineRtoRunRecord | LegacyOfflineRtoRunRecordV1 | OfflineRtoRunRecordV2
)

_MANIFEST_LEGACY_V1 = "offline-rto-manifest-v1"
_MANIFEST_LEGACY_V2 = "offline-rto-manifest-v2"
_MAX_RUNTIME_JSON_BYTES = 1_000_000


class OfflineInspectionError(RuntimeError):
    """Stored workflow evidence could not be strictly recovered or replayed."""


def _reject_constant(value: str) -> object:
    raise ValueError(f"runtime JSON contains non-finite constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"runtime JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_runtime_object(path: Path, *, context: str) -> dict[str, object]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"{context} must be an existing JSON file")
    size = resolved.stat().st_size
    if size <= 0 or size > _MAX_RUNTIME_JSON_BYTES:
        raise ValueError(f"{context} size must be between 1 byte and 1 MB")
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must contain one JSON object")
    return cast(dict[str, object], value)


def capabilities(*, repo_root: Path | None = None) -> dict[str, object]:
    """Return the sanitized unified capability surface without selecting a solver."""

    manifest = build_public_capability_manifest(load_capability_bundle(repo_root))
    return {
        **manifest.as_dict(),
        "capability_fingerprint": manifest.fingerprint,
        "solver_called": False,
    }


def build_intent_communication_service(
    *,
    repo_root: Path | None = None,
) -> IntentCommunicationService:
    """Build the provider-neutral domain-model gateway from the authoritative bundle."""

    return _build_intent_communication_service(repo_root=repo_root)


def validate_intent_file(
    *,
    repo_root: Path | None,
    intent_file: Path,
) -> IntentResolution:
    """Validate a context-free unified intent against the published atomic capabilities."""

    bundle = load_capability_bundle(repo_root)
    intent = load_optimization_intent(intent_file)
    return IntentResolver().resolve(intent, BundleCapabilityView(bundle))


def _load_unified_problem_inputs(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
) -> tuple[
    UnifiedCapabilityBundle,
    OptimizationIntent,
    OperatingContext,
    OptimizationProblem,
]:
    bundle = load_capability_bundle(repo_root)
    intent = load_optimization_intent(intent_file)
    resolution = IntentResolver().resolve(intent, BundleCapabilityView(bundle))
    if resolution.status != "resolved":
        issue_codes = ",".join(item.code for item in resolution.issues)
        raise ValueError(
            f"optimization intent is not executable: {resolution.status}:{issue_codes}"
        )
    context = load_operating_context(context_file)
    problem = UnifiedProblemBuilder().build(bundle, intent, context)
    return bundle, intent, context, problem


def validate_problem_files(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
) -> OptimizationProblem:
    """Build one deterministic unified problem without routing or simulation."""

    return _load_unified_problem_inputs(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )[3]


def run_unified_offline(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
    run_root: Path,
    library_root: Path,
    actor: str,
    coverage_policy: CoveragePolicy = "point",
) -> UnifiedOfflineRtoRunRecord:
    """Run or resume the objective-count-neutral offline workflow."""

    bundle, intent, context, _ = _load_unified_problem_inputs(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )
    return UnifiedOfflineRtoOrchestrator(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run(
        bundle,
        intent,
        context,
        run_root=run_root,
        strategy_repository=UnifiedStrategyRepository(library_root),
        actor=actor,
        coverage_policy=coverage_policy,
    )


def inspect_offline_auto(
    run_dir: Path,
    *,
    repo_root: Path | None,
    library_root: Path,
    legacy_request_file: Path | None = None,
) -> OfflineRunRecord:
    """Inspect by manifest; ``repo_root`` is retained only for call compatibility."""

    resolved_run_dir = run_dir.resolve()
    manifest = _read_runtime_object(
        resolved_run_dir / "manifest.json",
        context="offline RTO manifest",
    )
    signature = (
        manifest.get("schema_id"),
        manifest.get("schema_version"),
        manifest.get("manifest_version"),
    )
    if signature == (
        OFFLINE_WORKFLOW_SCHEMA_ID,
        OFFLINE_WORKFLOW_SCHEMA_VERSION,
        UNIFIED_MANIFEST_VERSION,
    ):
        try:
            return read_unified_offline_run(
                resolved_run_dir,
                strategy_repository=UnifiedStrategyRepository(library_root),
                simulator=CduM7Simulator(resolved_run_dir / "simulator"),
                request_factory=CduM7RequestFactory(),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise OfflineInspectionError(
                f"unified offline evidence inspection failed: {exc}"
            ) from exc
    if signature == (None, RTO_V2_SCHEMA_VERSION, _MANIFEST_LEGACY_V2):
        try:
            external_request = ExternalOptimizationRequestV2.from_mapping(
                _read_runtime_object(
                    resolved_run_dir / "external_request.json",
                    context="legacy V2 external request",
                )
            )
            resolved_intent = ResolvedOptimizationIntentV2.from_mapping(
                _read_runtime_object(
                    resolved_run_dir / "resolved_intent.json",
                    context="legacy V2 resolved intent",
                )
            )
            bound = bind_external_optimization_request_v2(
                load_rto_v2_bundle(),
                external_request,
            )
            if bound.resolved_intent != resolved_intent:
                raise ValueError("stored legacy V2 intent differs from deterministic binding")
            return read_offline_run_v2(
                resolved_run_dir,
                bundle=bound.bundle,
                external_request=external_request,
                resolved_intent=resolved_intent,
                strategy_repository=StrategyDraftRepositoryV2(library_root),
                simulator=CduM7Simulator(resolved_run_dir / "simulator"),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise OfflineInspectionError(
                f"legacy V2 offline evidence inspection failed: {exc}"
            ) from exc
    if signature == (None, RTO_SCHEMA_VERSION, _MANIFEST_LEGACY_V1):
        try:
            request = OfflineRtoRequestV1.from_mapping(
                _read_runtime_object(
                    resolved_run_dir / "request.json",
                    context="legacy V1 workflow request",
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise OfflineInspectionError(
                f"legacy V1 stored request inspection failed: {exc}"
            ) from exc
        if request.external_request_ref is None:
            try:
                return inspect_legacy_v1_offline(
                    resolved_run_dir,
                    repo_root=None,
                    library_root=library_root,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise OfflineInspectionError(
                    f"legacy V1 offline evidence inspection failed: {exc}"
                ) from exc
        if legacy_request_file is None:
            raise ValueError(
                "legacy V1 run references an external request; legacy_request_file is required"
            )
        bound_v1 = validate_legacy_v1_request(
            repo_root=None,
            request_file=legacy_request_file,
        )
        if bound_v1.external_request.ref != request.external_request_ref:
            raise ValueError("legacy V1 request file differs from the stored external request ref")
        try:
            return inspect_legacy_v1_offline(
                resolved_run_dir,
                repo_root=None,
                library_root=library_root,
                request_file=legacy_request_file,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise OfflineInspectionError(
                f"legacy V1 offline evidence inspection failed: {exc}"
            ) from exc
    raise ValueError(
        "unsupported or conflicting offline manifest signature; "
        "expected unified, legacy V1, or legacy V2"
    )


def run_legacy_v1_offline(
    *,
    repo_root: Path | None,
    run_root: Path,
    library_root: Path,
    actor: str,
    coverage_policy: str = "sampled-anchors",
) -> LegacyOfflineRtoRunRecordV1:
    """Legacy V1 compatibility entry; new callers should use run_unified_offline."""

    bundle = load_rto_v1_bundle(repo_root)
    return LegacyOfflineRtoOrchestratorV1(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run(
        bundle,
        run_root=run_root,
        strategy_repository=LegacyStrategyRepositoryV1(library_root),
        actor=actor,
        coverage_policy=coverage_policy,
    )


def validate_legacy_v1_request(
    *,
    repo_root: Path | None,
    request_file: Path,
) -> BoundExternalOptimizationRequestV1:
    """Legacy V1 request validation without simulation or writes."""

    request = load_external_optimization_request(request_file)
    return bind_external_optimization_request(load_rto_v1_bundle(repo_root), request)


def legacy_capabilities_v2(*, repo_root: Path | None) -> dict[str, object]:
    """Legacy V2 capability manifest; use capabilities for the unified surface."""

    return capability_manifest_v2(load_rto_v2_bundle(repo_root)).as_dict()


def validate_legacy_domain_intent_file_v2(
    *,
    repo_root: Path | None,
    intent_file: Path,
) -> IntentValidationResultV2:
    """Legacy V2 intent validation with stable structured errors."""

    try:
        intent = load_domain_optimization_intent_v2(intent_file)
    except (OSError, TypeError, ValueError) as exc:
        return IntentValidationResultV2(
            valid=False,
            status="invalid",
            audit_fingerprint=None,
            semantic_fingerprint=None,
            issues=(
                IntentValidationIssueV2(
                    code="invalid-intent-contract",
                    json_pointer="/",
                    message=str(exc),
                    supported_values=(),
                    retryable=True,
                ),
            ),
        )
    return validate_domain_intent_v2(load_rto_v2_bundle(repo_root), intent)


def validate_legacy_v2_request(
    *,
    repo_root: Path | None,
    request_file: Path,
) -> BoundExternalOptimizationRequestV2:
    """Legacy V2 request validation without simulation or writes."""

    request = load_external_optimization_request_v2(request_file)
    return bind_external_optimization_request_v2(load_rto_v2_bundle(repo_root), request)


def run_legacy_v1_request(
    *,
    repo_root: Path | None,
    request_file: Path,
    run_root: Path,
    library_root: Path,
    actor: str,
) -> LegacyOfflineRtoRunRecordV1:
    """Legacy V1 external-request workflow compatibility entry."""

    bound = validate_legacy_v1_request(repo_root=repo_root, request_file=request_file)
    return LegacyOfflineRtoOrchestratorV1(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run(
        bound.bundle,
        run_root=run_root,
        strategy_repository=LegacyStrategyRepositoryV1(library_root),
        actor=actor,
        coverage_policy=bound.external_request.coverage_policy,
        external_request_ref=bound.external_request.ref,
    )


def run_legacy_v2_request(
    *,
    repo_root: Path | None,
    request_file: Path,
    run_root: Path,
    library_root: Path,
    actor: str,
) -> OfflineRtoRunRecordV2:
    """Legacy V2 external-request workflow compatibility entry."""

    bound = validate_legacy_v2_request(repo_root=repo_root, request_file=request_file)
    return OfflineRtoOrchestratorV2(
        CduM7RequestFactory(),
        lambda output_root: CduM7Simulator(output_root),
    ).run(
        bound.bundle,
        bound.external_request,
        bound.resolved_intent,
        bound.problem,
        run_root=run_root,
        strategy_repository=StrategyDraftRepositoryV2(library_root),
        actor=actor,
    )


def inspect_legacy_v1_offline(
    run_dir: Path,
    *,
    repo_root: Path | None,
    library_root: Path,
    request_file: Path | None = None,
) -> LegacyOfflineRtoRunRecordV1:
    """Strictly inspect one legacy V1 workflow."""

    bound = (
        None
        if request_file is None
        else validate_legacy_v1_request(repo_root=repo_root, request_file=request_file)
    )
    bundle = load_rto_v1_bundle(repo_root) if bound is None else bound.bundle
    return read_legacy_offline_run_v1(
        run_dir,
        bundle=bundle,
        strategy_repository=LegacyStrategyRepositoryV1(library_root),
        simulator=CduM7Simulator(run_dir / "simulator"),
        external_request_ref=(None if bound is None else bound.external_request.ref),
    )


def inspect_legacy_v2_offline(
    run_dir: Path,
    *,
    repo_root: Path | None,
    library_root: Path,
    request_file: Path,
) -> OfflineRtoRunRecordV2:
    """Strictly replay one legacy V2 workflow with its original request file."""

    bound = validate_legacy_v2_request(repo_root=repo_root, request_file=request_file)
    return read_offline_run_v2(
        run_dir,
        bundle=bound.bundle,
        external_request=bound.external_request,
        resolved_intent=bound.resolved_intent,
        strategy_repository=StrategyDraftRepositoryV2(library_root),
        simulator=CduM7Simulator(run_dir / "simulator"),
    )


def approve_strategy(
    *,
    library_root: Path,
    strategy_id: str,
    revision: int,
    actor: str,
    reason: str = "offline-human-review-approved",
) -> StrategyRecord:
    """Explicitly approve one unified draft after human review."""

    return UnifiedStrategyRepository(library_root).approve(
        strategy_id,
        revision,
        actor=actor,
        reason=reason,
    )


def publish_strategy(
    *,
    library_root: Path,
    strategy_id: str,
    revision: int,
    actor: str,
    reason: str = "offline-library-release",
) -> StrategyReleaseManifest:
    """Explicitly publish one already-approved unified strategy."""

    return UnifiedStrategyRepository(library_root).publish(
        strategy_id,
        revision,
        actor=actor,
        reason=reason,
    )


def approve_legacy_v1_strategy(
    *,
    library_root: Path,
    strategy_id: str,
    revision: int,
    actor: str,
    reason: str = "offline-review-approved",
) -> StrategyRecordV1:
    """Explicit legacy V1 lifecycle compatibility entry."""

    return LegacyStrategyRepositoryV1(library_root).approve(
        strategy_id,
        revision,
        actor=actor,
        reason=reason,
    )


def publish_legacy_v1_strategy(
    *,
    library_root: Path,
    strategy_id: str,
    revision: int,
    actor: str,
    reason: str = "offline-library-release",
) -> StrategyReleaseManifestV1:
    """Explicit legacy V1 lifecycle compatibility entry."""

    return LegacyStrategyRepositoryV1(library_root).publish(
        strategy_id,
        revision,
        actor=actor,
        reason=reason,
    )


def query_legacy_v1_strategies(
    *,
    repo_root: Path | None,
    library_root: Path,
    feed_mass_flow_kg_s: float,
    measurement_tolerance_kg_s: float,
) -> tuple[StrategyRecordV1, ...]:
    """Legacy V1 exact-anchor query compatibility entry."""

    context = load_rto_v1_bundle(repo_root).context
    return LegacyStrategyRepositoryV1(library_root).query(
        StrategyQueryV1(
            case_ref=context.case_ref,
            operating_mode=context.operating_mode,
            feed_mass_flow_kg_s=feed_mass_flow_kg_s,
            measurement_tolerance_kg_s=measurement_tolerance_kg_s,
        )
    )


def run_offline(
    *,
    repo_root: Path | None,
    intent_file: Path,
    context_file: Path,
    run_root: Path,
    library_root: Path,
    actor: str,
    coverage_policy: CoveragePolicy = "point",
) -> UnifiedOfflineRtoRunRecord:
    """Canonical objective-count-neutral run entry."""

    return run_unified_offline(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
        run_root=run_root,
        library_root=library_root,
        actor=actor,
        coverage_policy=coverage_policy,
    )


def inspect_offline(
    run_dir: Path,
    *,
    repo_root: Path | None,
    library_root: Path,
    legacy_request_file: Path | None = None,
) -> OfflineRunRecord:
    """Canonical manifest-routed strict inspection entry."""

    return inspect_offline_auto(
        run_dir,
        repo_root=repo_root,
        library_root=library_root,
        legacy_request_file=legacy_request_file,
    )


def query_strategies(
    *,
    library_root: Path,
    query: StrategyQuery,
) -> tuple[StrategyRecord, ...]:
    """Query only published unified strategies at explicit sampled anchors."""

    return UnifiedStrategyRepository(library_root).query(query)


def run_summary_legacy_v1(
    record: LegacyOfflineRtoRunRecordV1,
) -> dict[str, object]:
    selected_setpoints = build_chat_result_summary(record)["selected_setpoints"]
    return {
        "workflow_kind": "legacy-v1",
        "manifest_version": record.manifest.manifest_version,
        "workflow_id": record.request.workflow_id,
        "intent_ref": record.request.intent_ref.as_dict(),
        "context_ref": record.request.context_ref.as_dict(),
        "external_request_ref": (
            None
            if record.request.external_request_ref is None
            else record.request.external_request_ref.as_dict()
        ),
        "status": record.result.status,
        "optimization_status": record.optimization_result.status,
        "coverage_policy": record.request.coverage_policy,
        "requested_anchor_count": record.result.requested_anchor_count,
        "passed_anchor_count": record.result.passed_anchor_count,
        "selected_setpoints": selected_setpoints,
        "strategy_ref": (
            None if record.result.strategy_ref is None else record.result.strategy_ref.as_dict()
        ),
        "strategy_state": (None if record.strategy is None else "draft"),
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


def run_summary_legacy_v2(record: OfflineRtoRunRecordV2) -> dict[str, object]:
    """Return the compact offline-only summary for a legacy V2 workflow."""

    selected_setpoints = build_chat_result_summary(record)["selected_setpoints"]
    return {
        "workflow_kind": "legacy-v2",
        "manifest_version": record.manifest.manifest_version,
        "workflow_id": record.request.workflow_id,
        "request_version": record.external_request.request_version,
        "intent_ref": record.resolved_intent.ref.as_dict(),
        "context_ref": record.problem.context_ref.as_dict(),
        "status": record.result.status,
        "optimization_status": record.optimization_result.status,
        "coverage_policy": record.request.coverage_policy,
        "grid_count": record.pareto_search.grid_count,
        "pareto_count": len(record.pareto_search.pareto_refs),
        "dynamic_shortlist_count": len(record.preference_selection.shortlist_refs),
        "requested_anchor_count": record.result.requested_anchor_count,
        "passed_anchor_count": record.result.passed_anchor_count,
        "selected_setpoints": selected_setpoints,
        "selected_objectives": [
            item.as_dict() for item in record.optimization_result.selected_objectives
        ],
        "strategy_ref": (
            None if record.result.strategy_ref is None else record.result.strategy_ref.as_dict()
        ),
        "strategy_state": None if record.strategy is None else record.strategy.state,
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


def _run_summary_unified(record: UnifiedOfflineRtoRunRecord) -> dict[str, object]:
    selected_ref = record.finalization.result.selected_static_evaluation_ref
    selected = next(
        (item for item in record.solver_execution.result.evaluations if item.ref == selected_ref),
        None,
    )
    selected_setpoints = build_chat_result_summary(record)["selected_setpoints"]
    return {
        "workflow_kind": "unified",
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


def run_summary(record: OfflineRunRecord) -> dict[str, object]:
    """Return one stable offline-only summary across unified and legacy inspection."""

    if isinstance(record, UnifiedOfflineRtoRunRecord):
        return _run_summary_unified(record)
    if isinstance(record, LegacyOfflineRtoRunRecordV1):
        return run_summary_legacy_v1(record)
    if isinstance(record, OfflineRtoRunRecordV2):
        return run_summary_legacy_v2(record)
    raise TypeError("record must be a unified or supported legacy offline run")


def legacy_external_request_summary_v1(
    bound: BoundExternalOptimizationRequestV1,
) -> dict[str, object]:
    """Return an audit-friendly no-solver preview for an external request."""

    return {
        "request_id": bound.external_request.request_id,
        "request_fingerprint": bound.external_request.fingerprint,
        "source_type": bound.external_request.optimization_intent.source_type,
        "coverage_policy": bound.external_request.coverage_policy,
        "context_ref": bound.bundle.context.ref.as_dict(),
        "intent_ref": bound.bundle.intent.ref.as_dict(),
        "problem_ref": bound.problem.ref.as_dict(),
        "feed_mass_flow_t_h": bound.external_request.operating_context.feed_mass_flow_t_h,
        "objective_metric_id": bound.problem.objective_metric_id,
        "objective_sense": bound.problem.objective_sense,
        "decision_variables": [item.variable_id for item in bound.problem.decision_domains],
        "claim_scope": bound.problem.claim_scope,
        "solver_called": False,
    }


def legacy_external_request_summary_v2(
    bound: BoundExternalOptimizationRequestV2,
) -> dict[str, object]:
    """Return an audit-friendly no-solver preview for a V2 request."""

    return {
        "request_version": bound.external_request.request_version,
        "request_id": bound.external_request.request_id,
        "request_fingerprint": bound.external_request.fingerprint,
        "audit_fingerprint": bound.external_request.optimization_intent.audit_fingerprint,
        "semantic_fingerprint": bound.external_request.optimization_intent.semantic_fingerprint,
        "coverage_policy": bound.external_request.coverage_policy,
        "context_ref": bound.bundle.base.context.ref.as_dict(),
        "intent_ref": bound.resolved_intent.ref.as_dict(),
        "problem_ref": bound.problem.ref.as_dict(),
        "feed_mass_flow_t_h": bound.external_request.operating_context.feed_mass_flow_t_h,
        "objectives": [item.as_dict() for item in bound.problem.objectives],
        "selection_profile_id": bound.problem.preference_profile_id,
        "decision_variables": [item.variable_id for item in bound.problem.decision_domains],
        "claim_scope": bound.problem.claim_scope,
        "solver_called": False,
    }
