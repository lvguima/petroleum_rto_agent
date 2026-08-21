"""Resumable RTO V2 workflow with strict Pareto replay and offline draft output."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ..catalogs import RtoCatalogBundleV2
from ..compilation import MultiObjectiveCandidatePlanCompiler
from ..contracts.common import canonical_json_bytes, identifier
from ..contracts.evaluation import RunEvidenceRefV1
from ..contracts.models import CLAIM_SCOPE, ContractRef, OperatingContextV1
from ..contracts.multiobjective import (
    RTO_V2_SCHEMA_VERSION,
    OptimizationProblemV2,
    ResolvedOptimizationIntentV2,
)
from ..contracts.results_v2 import (
    CandidateEvaluationV2,
    CandidateProposalV2,
    ParetoSearchResultV2,
)
from ..contracts.selection_v2 import (
    DynamicVerificationV2,
    OptimizationResultV2,
    PreferenceSelectionV2,
)
from ..evaluation import (
    MultiObjectiveDynamicEvaluationService,
    MultiObjectiveSteadyEvaluationService,
)
from ..inputs.v2_models import ExternalOptimizationRequestV2
from ..optimizer import (
    DeterministicParetoGridOptimizer,
    MultiObjectiveDynamicFinalSelector,
    ParetoPreferenceSelector,
)
from ..ports import ProviderRequestFactory, SimulatorPort
from ..problem import MultiObjectiveProblemBuilder
from ..strategies import (
    StrategyAnchorV2,
    StrategyDraftRepositoryV2,
    StrategyEntryV2,
    utc_now,
)
from .v2_models import (
    AnchorAttemptV2,
    AnchorValidationResultV2,
    OfflineRtoManifestV2,
    OfflineRtoRequestV2,
    OfflineRtoResultV2,
    OfflineRunStatusV2,
    WorkflowEventV2,
)

SimulatorFactory = Callable[[Path], SimulatorPort]


@dataclass(frozen=True)
class OfflineRtoRunRecordV2:
    run_dir: Path
    request: OfflineRtoRequestV2
    external_request: ExternalOptimizationRequestV2
    resolved_intent: ResolvedOptimizationIntentV2
    problem: OptimizationProblemV2
    pareto_search: ParetoSearchResultV2
    preference_selection: PreferenceSelectionV2
    dynamic_verification: DynamicVerificationV2 | None
    optimization_result: OptimizationResultV2
    anchor_validation: AnchorValidationResultV2 | None
    strategy: StrategyEntryV2 | None
    result: OfflineRtoResultV2
    manifest: OfflineRtoManifestV2
    events: tuple[WorkflowEventV2, ...]
    recovered_stages: tuple[str, ...]
    physical_m2_executions: int
    physical_m4_executions: int


class _ReplayStaticEvaluator:
    def __init__(self, evaluations: tuple[CandidateEvaluationV2, ...]) -> None:
        self._evaluations = {item.proposal_ref: item for item in evaluations}

    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        try:
            return self._evaluations[proposal.ref]
        except KeyError as exc:
            raise ValueError("stored V2 search lacks a generated proposal") from exc


class _ReplayDynamicEvaluator:
    def __init__(self, evaluations: tuple[CandidateEvaluationV2, ...]) -> None:
        self._evaluations = {item.proposal_ref: item for item in evaluations}

    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        try:
            return self._evaluations[proposal.ref]
        except KeyError as exc:
            raise ValueError("stored V2 verification lacks a shortlisted proposal") from exc


class _UnusedDynamicEvaluator:
    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2:
        raise RuntimeError(f"unexpected dynamic evaluation for {proposal.ref.object_id}")


class OfflineRtoOrchestratorV2:
    """Execute or strictly resume one external V2 request synchronously."""

    def __init__(
        self,
        request_factory: ProviderRequestFactory,
        simulator_factory: SimulatorFactory,
    ) -> None:
        self._request_factory = request_factory
        self._simulator_factory = simulator_factory

    def run(
        self,
        bundle: RtoCatalogBundleV2,
        external_request: ExternalOptimizationRequestV2,
        resolved_intent: ResolvedOptimizationIntentV2,
        problem: OptimizationProblemV2,
        *,
        run_root: Path,
        strategy_repository: StrategyDraftRepositoryV2,
        actor: str,
    ) -> OfflineRtoRunRecordV2:
        actor = identifier(actor, context="actor")
        request = _request_from_inputs(
            bundle,
            resolved_intent,
            problem,
            coverage_policy=external_request.coverage_policy,
        )
        run_dir = run_root.resolve() / request.workflow_id
        run_dir.mkdir(parents=True, exist_ok=True)
        simulator = self._simulator_factory(run_dir / "simulator")
        with _workflow_lock(run_dir):
            if (run_dir / "manifest.json").exists():
                return read_offline_run_v2(
                    run_dir,
                    bundle=bundle,
                    external_request=external_request,
                    resolved_intent=resolved_intent,
                    strategy_repository=strategy_repository,
                    simulator=simulator,
                    recovered_stages=(
                        "request",
                        "problem",
                        "pareto-search",
                        "preference-selection",
                        "dynamic-verification",
                        "anchor-validation",
                        "strategy-draft",
                        "workflow-complete",
                    ),
                )

            _write_or_verify(run_dir / "request.json", request.as_dict())
            _write_or_verify(run_dir / "external_request.json", external_request.as_dict())
            _write_or_verify(run_dir / "resolved_intent.json", resolved_intent.as_dict())
            _write_or_verify(run_dir / "problem.json", problem.as_dict())
            events = list(_read_events(run_dir / "events.jsonl", request.ref, allow_missing=True))
            _ensure_event(events, run_dir, request.ref, "request", request.ref)
            _ensure_event(events, run_dir, request.ref, "problem", problem.ref)
            recovered: list[str] = ["request", "problem"]
            physical_m2 = 0
            physical_m4 = 0

            static_path = run_dir / "pareto_search.json"
            if static_path.exists():
                pareto = ParetoSearchResultV2.from_mapping(_read_json(static_path))
                _validate_pareto(problem, bundle.base.context, pareto)
                recovered.append("pareto-search")
            else:
                steady = MultiObjectiveSteadyEvaluationService(
                    problem,
                    bundle.base.context,
                    bundle.base.kpi_catalog,
                    MultiObjectiveCandidatePlanCompiler(),
                    self._request_factory,
                    simulator,
                )
                pareto = DeterministicParetoGridOptimizer().search(
                    problem, bundle.base.context, steady
                )
                physical_m2 += steady.physical_execution_count
                _write_or_verify(static_path, pareto.as_dict())
            _ensure_event(events, run_dir, request.ref, "pareto-search", pareto.ref)

            profile = bundle.preference_catalog.profile_by_id(problem.preference_profile_id)
            preference_path = run_dir / "preference_selection.json"
            expected_preference = ParetoPreferenceSelector().select(problem, pareto, profile)
            if preference_path.exists():
                preference = PreferenceSelectionV2.from_mapping(_read_json(preference_path))
                if preference != expected_preference:
                    raise ValueError("stored preference selection differs from replay")
                recovered.append("preference-selection")
            else:
                preference = expected_preference
                _write_or_verify(preference_path, preference.as_dict())
            _ensure_event(events, run_dir, request.ref, "preference-selection", preference.ref)

            dynamic_path = run_dir / "dynamic_verification.json"
            optimization_path = run_dir / "optimization_result.json"
            if dynamic_path.exists() != optimization_path.exists():
                raise ValueError("dynamic verification and optimization result are incomplete")
            publishability = bundle.publishability_catalog.profile_by_id(
                problem.publishability_profile_id
            )
            if optimization_path.exists():
                dynamic = (
                    None
                    if _read_json(dynamic_path).get("status") == "not-applicable"
                    else DynamicVerificationV2.from_mapping(_read_json(dynamic_path))
                )
                optimization = OptimizationResultV2.from_mapping(_read_json(optimization_path))
                _validate_final_selection(
                    problem,
                    pareto,
                    preference,
                    dynamic,
                    optimization,
                    publishability,
                )
                recovered.append("dynamic-verification")
            else:
                if preference.status == "success":
                    dynamic_service = MultiObjectiveDynamicEvaluationService(
                        problem,
                        bundle.base.context,
                        bundle.base.kpi_catalog,
                        MultiObjectiveCandidatePlanCompiler(),
                        self._request_factory,
                        simulator,
                    )
                    dynamic, optimization = MultiObjectiveDynamicFinalSelector().select(
                        problem,
                        pareto,
                        preference,
                        publishability,
                        dynamic_service,
                    )
                    physical_m4 += dynamic_service.physical_execution_count
                else:
                    dynamic, optimization = MultiObjectiveDynamicFinalSelector().select(
                        problem,
                        pareto,
                        preference,
                        publishability,
                        _UnusedDynamicEvaluator(),
                    )
                _write_or_verify(
                    dynamic_path,
                    (
                        _not_applicable_dynamic(preference.ref)
                        if dynamic is None
                        else dynamic.as_dict()
                    ),
                )
                _write_or_verify(optimization_path, optimization.as_dict())
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "dynamic-verification",
                optimization.ref if dynamic is None else dynamic.ref,
            )

            anchor_validation: AnchorValidationResultV2 | None = None
            strategy: StrategyEntryV2 | None = None
            anchor_path = run_dir / "anchor_validation.json"
            strategy_path = run_dir / "strategy_draft.json"
            if optimization.status == "success":
                if anchor_path.exists():
                    anchor_validation = AnchorValidationResultV2.from_mapping(
                        _read_json(anchor_path)
                    )
                    _validate_anchor_result(
                        bundle,
                        resolved_intent,
                        problem,
                        pareto,
                        dynamic,
                        optimization,
                        anchor_validation,
                        request.coverage_policy,
                    )
                    recovered.append("anchor-validation")
                else:
                    anchor_validation, extra_m2, extra_m4 = self._validate_anchors(
                        bundle,
                        resolved_intent,
                        problem,
                        pareto,
                        cast(DynamicVerificationV2, dynamic),
                        optimization,
                        simulator,
                        coverage_policy=request.coverage_policy,
                    )
                    physical_m2 += extra_m2
                    physical_m4 += extra_m4
                    _write_or_verify(anchor_path, anchor_validation.as_dict())
                _ensure_event(
                    events,
                    run_dir,
                    request.ref,
                    "anchor-validation",
                    anchor_validation.ref,
                )
                if len(anchor_validation.passed_attempts) == len(anchor_validation.attempts):
                    expected_strategy = _build_strategy(
                        problem,
                        pareto,
                        preference,
                        optimization,
                        anchor_validation,
                    )
                    if strategy_path.exists():
                        strategy = StrategyEntryV2.from_mapping(_read_json(strategy_path))
                        if strategy != expected_strategy:
                            raise ValueError("stored V2 strategy differs from replay")
                        recovered.append("strategy-draft")
                    else:
                        strategy = expected_strategy
                        strategy_repository.create_draft(
                            strategy,
                            actor=actor,
                            occurred_at=utc_now(),
                        )
                        _write_or_verify(strategy_path, strategy.as_dict())
                    _ensure_event(
                        events,
                        run_dir,
                        request.ref,
                        "strategy-draft",
                        strategy.ref,
                    )

            offline_result = _offline_result(
                request,
                problem,
                pareto,
                preference,
                optimization,
                anchor_validation,
                strategy,
            )
            _write_or_verify(run_dir / "result.json", offline_result.as_dict())
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "workflow-complete",
                offline_result.ref,
            )
            manifest = _commit_manifest(run_dir, request, offline_result)
            verified = read_offline_run_v2(
                run_dir,
                bundle=bundle,
                external_request=external_request,
                resolved_intent=resolved_intent,
                strategy_repository=strategy_repository,
                simulator=simulator,
                recovered_stages=tuple(recovered),
            )
            return replace(
                verified,
                physical_m2_executions=physical_m2,
                physical_m4_executions=physical_m4,
                manifest=manifest,
            )

    def _validate_anchors(
        self,
        bundle: RtoCatalogBundleV2,
        resolved_intent: ResolvedOptimizationIntentV2,
        problem: OptimizationProblemV2,
        pareto: ParetoSearchResultV2,
        dynamic: DynamicVerificationV2,
        optimization: OptimizationResultV2,
        simulator: SimulatorPort,
        *,
        coverage_policy: str,
    ) -> tuple[AnchorValidationResultV2, int, int]:
        if (
            optimization.selected_proposal_ref is None
            or optimization.selected_static_evaluation_ref is None
            or optimization.selected_dynamic_evaluation_ref is None
        ):
            raise ValueError("publishable V2 result lacks selected refs")
        selected = next(
            item for item in pareto.proposals if item.ref == optimization.selected_proposal_ref
        )
        selected_static = next(
            item
            for item in pareto.evaluations
            if item.ref == optimization.selected_static_evaluation_ref
        )
        selected_dynamic = next(
            item
            for item in dynamic.evaluations
            if item.ref == optimization.selected_dynamic_evaluation_ref
        )
        ratios = (
            (1.0,) if coverage_policy == "point" else problem.evaluation_plan.feed_anchor_ratios
        )
        attempts: list[AnchorAttemptV2] = []
        m2_count = 0
        m4_count = 0
        for index, ratio in enumerate(ratios):
            if abs(ratio - 1.0) <= 1e-12:
                attempts.append(
                    AnchorAttemptV2(
                        ratio=ratio,
                        context=bundle.base.context,
                        resolved_intent=resolved_intent,
                        problem=problem,
                        proposal=selected,
                        static_evaluation=selected_static,
                        dynamic_evaluation=selected_dynamic,
                    )
                )
                continue
            anchor_bundle, anchor_intent, anchor_problem = _anchor_inputs(
                bundle, resolved_intent, ratio
            )
            proposal = CandidateProposalV2(
                schema_version=RTO_V2_SCHEMA_VERSION,
                proposal_version="candidate-proposal-v2",
                candidate_id=f"anchor-v2-{index:02d}",
                sequence=index,
                origin="anchor-validation",
                problem_ref=anchor_problem.ref,
                context_ref=anchor_bundle.base.context.ref,
                decision_values=selected.decision_values,
                output_kind="steady-setpoint-vector",
                claim_scope=CLAIM_SCOPE,
            )
            steady = MultiObjectiveSteadyEvaluationService(
                anchor_problem,
                anchor_bundle.base.context,
                bundle.base.kpi_catalog,
                MultiObjectiveCandidatePlanCompiler(),
                self._request_factory,
                simulator,
            )
            static_evaluation = steady.evaluate(proposal)
            m2_count += steady.physical_execution_count
            dynamic_evaluation: CandidateEvaluationV2 | None = None
            if static_evaluation.status == "feasible":
                dynamic_service = MultiObjectiveDynamicEvaluationService(
                    anchor_problem,
                    anchor_bundle.base.context,
                    bundle.base.kpi_catalog,
                    MultiObjectiveCandidatePlanCompiler(),
                    self._request_factory,
                    simulator,
                )
                dynamic_evaluation = dynamic_service.evaluate(proposal)
                m4_count += dynamic_service.physical_execution_count
            attempts.append(
                AnchorAttemptV2(
                    ratio=ratio,
                    context=anchor_bundle.base.context,
                    resolved_intent=anchor_intent,
                    problem=anchor_problem,
                    proposal=proposal,
                    static_evaluation=static_evaluation,
                    dynamic_evaluation=dynamic_evaluation,
                )
            )
        return (
            AnchorValidationResultV2(
                schema_version=RTO_V2_SCHEMA_VERSION,
                validation_version="sampled-anchor-validation-v2",
                selected_action=selected.decision_values,
                attempts=tuple(attempts),
                claim_scope=CLAIM_SCOPE,
            ),
            m2_count,
            m4_count,
        )


def _request_from_inputs(
    bundle: RtoCatalogBundleV2,
    resolved_intent: ResolvedOptimizationIntentV2,
    problem: OptimizationProblemV2,
    *,
    coverage_policy: str,
) -> OfflineRtoRequestV2:
    base = bundle.base
    if problem.intent_ref != resolved_intent.ref or problem.context_ref != base.context.ref:
        raise ValueError("V2 workflow inputs reference different intent or context")
    return OfflineRtoRequestV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        request_version="offline-rto-request-v2",
        resolved_intent_ref=resolved_intent.ref,
        context_ref=base.context.ref,
        decision_catalog_ref=base.decision_catalog.ref,
        kpi_catalog_ref=base.kpi_catalog.ref,
        constraint_profile_ref=base.constraint_profile.ref,
        policy_ref=bundle.policy.ref,
        objective_catalog_ref=bundle.objective_catalog.ref,
        preference_catalog_ref=bundle.preference_catalog.ref,
        publishability_catalog_ref=bundle.publishability_catalog.ref,
        provider_id=base.context.provider_id,
        coverage_policy=coverage_policy,
        claim_scope=CLAIM_SCOPE,
    )


def _anchor_inputs(
    bundle: RtoCatalogBundleV2,
    resolved_intent: ResolvedOptimizationIntentV2,
    ratio: float,
) -> tuple[RtoCatalogBundleV2, ResolvedOptimizationIntentV2, OptimizationProblemV2]:
    suffix = f"{round(ratio * 1000):04d}"
    context = replace(
        bundle.base.context,
        context_id=f"{bundle.base.context.context_id}-feed-{suffix}",
        feed_mass_flow_kg_s=bundle.base.context.feed_mass_flow_kg_s * ratio,
    )
    anchor_bundle = replace(bundle, base=replace(bundle.base, context=context))
    anchor_intent = replace(
        resolved_intent,
        intent_id=f"{resolved_intent.intent_id}-feed-{suffix}",
        operating_context_ref=context.ref,
    )
    return (
        anchor_bundle,
        anchor_intent,
        MultiObjectiveProblemBuilder().build(anchor_bundle, anchor_intent),
    )


def _validate_pareto(
    problem: OptimizationProblemV2,
    context: OperatingContextV1,
    stored: ParetoSearchResultV2,
) -> None:
    replayed = DeterministicParetoGridOptimizer().search(
        problem, context, _ReplayStaticEvaluator(stored.evaluations)
    )
    if replayed != stored:
        raise ValueError("stored Pareto result differs from deterministic replay")


def _validate_final_selection(
    problem: OptimizationProblemV2,
    pareto: ParetoSearchResultV2,
    preference: PreferenceSelectionV2,
    dynamic: DynamicVerificationV2 | None,
    optimization: OptimizationResultV2,
    publishability: object,
) -> None:
    from ..contracts.multiobjective import PublishabilityProfileV2

    if not isinstance(publishability, PublishabilityProfileV2):
        raise TypeError("publishability must be PublishabilityProfileV2")
    replay_dynamic, replay_result = MultiObjectiveDynamicFinalSelector().select(
        problem,
        pareto,
        preference,
        publishability,
        (
            _UnusedDynamicEvaluator()
            if dynamic is None
            else _ReplayDynamicEvaluator(dynamic.evaluations)
        ),
    )
    if replay_dynamic != dynamic or replay_result != optimization:
        raise ValueError("stored final selection differs from deterministic replay")


def _validate_anchor_result(
    bundle: RtoCatalogBundleV2,
    resolved_intent: ResolvedOptimizationIntentV2,
    problem: OptimizationProblemV2,
    pareto: ParetoSearchResultV2,
    dynamic: DynamicVerificationV2 | None,
    optimization: OptimizationResultV2,
    validation: AnchorValidationResultV2,
    coverage_policy: str,
) -> None:
    if dynamic is None or optimization.selected_proposal_ref is None:
        raise ValueError("anchor validation requires a selected dynamic result")
    selected = next(
        item for item in pareto.proposals if item.ref == optimization.selected_proposal_ref
    )
    if dict(validation.selected_action) != dict(selected.decision_values):
        raise ValueError("anchor selected action differs from optimization")
    ratios = (1.0,) if coverage_policy == "point" else problem.evaluation_plan.feed_anchor_ratios
    if tuple(item.ratio for item in validation.attempts) != ratios:
        raise ValueError("anchor ratios differ from coverage policy")
    for attempt in validation.attempts:
        if abs(attempt.ratio - 1.0) <= 1e-12:
            if (
                attempt.context != bundle.base.context
                or attempt.resolved_intent != resolved_intent
                or attempt.problem != problem
            ):
                raise ValueError("central anchor differs from central problem")
        else:
            anchor_bundle, anchor_intent, anchor_problem = _anchor_inputs(
                bundle, resolved_intent, attempt.ratio
            )
            if (
                attempt.context != anchor_bundle.base.context
                or attempt.resolved_intent != anchor_intent
                or attempt.problem != anchor_problem
            ):
                raise ValueError("anchor attempt differs from deterministic context")


def _build_strategy(
    problem: OptimizationProblemV2,
    pareto: ParetoSearchResultV2,
    preference: PreferenceSelectionV2,
    optimization: OptimizationResultV2,
    validation: AnchorValidationResultV2,
) -> StrategyEntryV2:
    if optimization.selected_proposal_ref is None:
        raise ValueError("strategy requires a selected proposal")
    proposal = next(
        item for item in pareto.proposals if item.ref == optimization.selected_proposal_ref
    )
    anchors: list[StrategyAnchorV2] = []
    for attempt in validation.passed_attempts:
        dynamic = cast(CandidateEvaluationV2, attempt.dynamic_evaluation)
        margins = tuple(
            item
            for item in (
                attempt.static_evaluation.minimum_normalized_margin,
                dynamic.minimum_normalized_margin,
            )
            if item is not None
        )
        anchors.append(
            StrategyAnchorV2(
                feed_ratio=attempt.ratio,
                context_ref=attempt.context.ref,
                feed_mass_flow_kg_s=attempt.context.feed_mass_flow_kg_s,
                static_evaluation_ref=attempt.static_evaluation.ref,
                dynamic_evaluation_ref=dynamic.ref,
                objective_summaries=attempt.static_evaluation.objective_outcomes,
                minimum_normalized_margin=min(margins),
            )
        )
    return StrategyEntryV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        entry_version="strategy-entry-v2",
        revision=1,
        state="draft",
        context_ref=problem.context_ref,
        problem_ref=problem.ref,
        objective_catalog_ref=problem.objective_catalog_ref,
        preference_profile_ref=preference.preference_profile_ref,
        pareto_search_ref=pareto.ref,
        selection_ref=preference.ref,
        optimization_result_ref=optimization.ref,
        selection_rationale_code="lexicographic-first-dynamic-feasible",
        action_setpoints=proposal.decision_values,
        anchors=tuple(anchors),
        execution_scope="offline_simulation_only",
        control_authority="none",
        field_validated=False,
        dcs_write_capability=False,
        claim_scope=CLAIM_SCOPE,
    )


def _offline_result(
    request: OfflineRtoRequestV2,
    problem: OptimizationProblemV2,
    pareto: ParetoSearchResultV2,
    preference: PreferenceSelectionV2,
    optimization: OptimizationResultV2,
    validation: AnchorValidationResultV2 | None,
    strategy: StrategyEntryV2 | None,
) -> OfflineRtoResultV2:
    has_error = optimization.status == "evaluation_error" or (
        validation is not None
        and any(
            attempt.static_evaluation.status in {"invalid_request", "evaluation_error"}
            or (
                attempt.dynamic_evaluation is not None
                and attempt.dynamic_evaluation.status in {"invalid_request", "evaluation_error"}
            )
            for attempt in validation.attempts
        )
    )
    status: OfflineRunStatusV2 = (
        "completed_draft"
        if strategy is not None
        else "failed"
        if has_error
        else "completed_without_strategy"
    )
    reason = (
        "strategy-draft-created"
        if strategy is not None
        else "workflow-evaluation-error"
        if has_error
        else "anchor-validation-incomplete"
        if validation is not None
        else "optimization-not-publishable"
    )
    return OfflineRtoResultV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        result_version="offline-rto-result-v2",
        status=status,
        request_ref=request.ref,
        problem_ref=problem.ref,
        pareto_search_ref=pareto.ref,
        preference_selection_ref=preference.ref,
        optimization_result_ref=optimization.ref,
        anchor_validation_ref=None if validation is None else validation.ref,
        strategy_ref=None if strategy is None else strategy.ref,
        requested_anchor_count=0 if validation is None else len(validation.attempts),
        passed_anchor_count=0 if validation is None else len(validation.passed_attempts),
        termination_reason=reason,
        claim_scope=CLAIM_SCOPE,
    )


def _not_applicable_dynamic(selection_ref: ContractRef) -> dict[str, object]:
    return {
        "schema_version": RTO_V2_SCHEMA_VERSION,
        "status": "not-applicable",
        "selection_ref": selection_ref.as_dict(),
        "claim_scope": CLAIM_SCOPE,
    }


def read_offline_run_v2(
    run_dir: Path,
    *,
    bundle: RtoCatalogBundleV2,
    external_request: ExternalOptimizationRequestV2,
    resolved_intent: ResolvedOptimizationIntentV2,
    strategy_repository: StrategyDraftRepositoryV2,
    simulator: SimulatorPort,
    recovered_stages: tuple[str, ...] = (),
) -> OfflineRtoRunRecordV2:
    manifest = OfflineRtoManifestV2.from_mapping(_read_json(run_dir / "manifest.json"))
    allowed = set(manifest.files) | {"manifest.json", "simulator"}
    actual = {item.name for item in run_dir.iterdir() if not item.name.startswith(".")}
    if actual - allowed:
        raise ValueError("offline V2 run contains unexpected top-level artifacts")
    for relative, expected in manifest.files.items():
        path = run_dir / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"offline V2 artifact hash differs: {relative}")
    request = OfflineRtoRequestV2.from_mapping(_read_json(run_dir / "request.json"))
    problem = OptimizationProblemV2.from_mapping(_read_json(run_dir / "problem.json"))
    expected_request = _request_from_inputs(
        bundle,
        resolved_intent,
        problem,
        coverage_policy=external_request.coverage_policy,
    )
    if request != expected_request or manifest.workflow_ref != request.ref:
        raise ValueError("offline V2 request differs from current strict inputs")
    stored_external = ExternalOptimizationRequestV2.from_mapping(
        _read_json(run_dir / "external_request.json")
    )
    stored_intent = ResolvedOptimizationIntentV2.from_mapping(
        _read_json(run_dir / "resolved_intent.json")
    )
    if stored_external != external_request or stored_intent != resolved_intent:
        raise ValueError("stored external request or resolved intent differs")
    if problem != MultiObjectiveProblemBuilder().build(bundle, resolved_intent):
        raise ValueError("offline V2 problem differs from deterministic builder")
    pareto = ParetoSearchResultV2.from_mapping(_read_json(run_dir / "pareto_search.json"))
    _validate_pareto(problem, bundle.base.context, pareto)
    preference = PreferenceSelectionV2.from_mapping(
        _read_json(run_dir / "preference_selection.json")
    )
    expected_preference = ParetoPreferenceSelector().select(
        problem,
        pareto,
        bundle.preference_catalog.profile_by_id(problem.preference_profile_id),
    )
    if preference != expected_preference:
        raise ValueError("stored preference differs from deterministic replay")
    raw_dynamic = _read_json(run_dir / "dynamic_verification.json")
    dynamic = (
        None
        if raw_dynamic.get("status") == "not-applicable"
        else DynamicVerificationV2.from_mapping(raw_dynamic)
    )
    optimization = OptimizationResultV2.from_mapping(
        _read_json(run_dir / "optimization_result.json")
    )
    _validate_final_selection(
        problem,
        pareto,
        preference,
        dynamic,
        optimization,
        bundle.publishability_catalog.profile_by_id(problem.publishability_profile_id),
    )
    _verify_evaluations(simulator, pareto.evaluations)
    if dynamic is not None:
        _verify_evaluations(simulator, dynamic.evaluations)
    anchor_path = run_dir / "anchor_validation.json"
    validation = (
        None
        if not anchor_path.exists()
        else AnchorValidationResultV2.from_mapping(_read_json(anchor_path))
    )
    if validation is not None:
        _validate_anchor_result(
            bundle,
            resolved_intent,
            problem,
            pareto,
            dynamic,
            optimization,
            validation,
            request.coverage_policy,
        )
        _verify_evaluations(
            simulator,
            tuple(
                evaluation
                for attempt in validation.attempts
                for evaluation in (
                    attempt.static_evaluation,
                    attempt.dynamic_evaluation,
                )
                if evaluation is not None
            ),
        )
    strategy_path = run_dir / "strategy_draft.json"
    strategy = (
        None
        if not strategy_path.exists()
        else StrategyEntryV2.from_mapping(_read_json(strategy_path))
    )
    if strategy is not None:
        if validation is None:
            raise ValueError("V2 strategy exists without anchor validation")
        rebuilt = _build_strategy(problem, pareto, preference, optimization, validation)
        if rebuilt != strategy or strategy_repository.read_ref(strategy.ref).entry != strategy:
            raise ValueError("V2 strategy differs from deterministic workflow or repository")
    result = OfflineRtoResultV2.from_mapping(_read_json(run_dir / "result.json"))
    if (
        result
        != _offline_result(
            request,
            problem,
            pareto,
            preference,
            optimization,
            validation,
            strategy,
        )
        or manifest.result_ref != result.ref
    ):
        raise ValueError("offline V2 result differs from strict evidence")
    events = _read_events(run_dir / "events.jsonl", request.ref, allow_missing=False)
    if not events or events[-1].stage != "workflow-complete" or events[-1].object_ref != result.ref:
        raise ValueError("offline V2 workflow event chain does not end at the result")
    return OfflineRtoRunRecordV2(
        run_dir=run_dir,
        request=request,
        external_request=external_request,
        resolved_intent=resolved_intent,
        problem=problem,
        pareto_search=pareto,
        preference_selection=preference,
        dynamic_verification=dynamic,
        optimization_result=optimization,
        anchor_validation=validation,
        strategy=strategy,
        result=result,
        manifest=manifest,
        events=events,
        recovered_stages=recovered_stages,
        physical_m2_executions=0,
        physical_m4_executions=0,
    )


def _verify_evaluations(
    simulator: SimulatorPort,
    evaluations: tuple[CandidateEvaluationV2, ...],
) -> None:
    verified: dict[str, RunEvidenceRefV1] = {}
    for evaluation in evaluations:
        for evidence in (evaluation.baseline_evidence, evaluation.candidate_evidence):
            if evidence is None:
                continue
            actual = verified.get(evidence.run_ref)
            if actual is None:
                actual = RunEvidenceRefV1.from_bundle(
                    simulator.read_evidence(Path(evidence.run_ref))
                )
                verified[evidence.run_ref] = actual
            if actual != evidence:
                raise ValueError("strict simulator evidence differs from stored V2 evaluation")


def _commit_manifest(
    run_dir: Path,
    request: OfflineRtoRequestV2,
    result: OfflineRtoResultV2,
) -> OfflineRtoManifestV2:
    names = (
        "anchor_validation.json",
        "dynamic_verification.json",
        "events.jsonl",
        "external_request.json",
        "optimization_result.json",
        "pareto_search.json",
        "preference_selection.json",
        "problem.json",
        "request.json",
        "resolved_intent.json",
        "result.json",
        "strategy_draft.json",
    )
    files = {
        name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        for name in names
        if (run_dir / name).is_file()
    }
    manifest = OfflineRtoManifestV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        manifest_version="offline-rto-manifest-v2",
        workflow_ref=request.ref,
        result_ref=result.ref,
        files=dict(sorted(files.items())),
        software_versions={
            "petroleum-rto": "0.1.0",
            "rto-contract": RTO_V2_SCHEMA_VERSION,
        },
        created_at=utc_now(),
        claim_scope=CLAIM_SCOPE,
    )
    _write_or_verify(run_dir / "manifest.json", manifest.as_dict())
    return manifest


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read strict offline V2 JSON: {path}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"offline V2 JSON must be an object: {path}")
    return cast(dict[str, object], value)


def _write_or_verify(path: Path, value: Mapping[str, object]) -> None:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"existing offline V2 artifact differs: {path.name}")
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_events(
    path: Path,
    workflow_ref: ContractRef,
    *,
    allow_missing: bool,
) -> tuple[WorkflowEventV2, ...]:
    if not path.exists():
        if allow_missing:
            return ()
        raise ValueError("offline V2 workflow event log is missing")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or any(not item.strip() for item in lines):
        raise ValueError("offline V2 workflow event log is malformed")
    events: list[WorkflowEventV2] = []
    previous: str | None = None
    for index, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("offline V2 workflow event contains invalid JSON") from exc
        if not isinstance(raw, dict):
            raise TypeError("offline V2 workflow event must be an object")
        event = WorkflowEventV2.from_mapping(raw)
        if (
            event.workflow_ref != workflow_ref
            or event.sequence != index
            or event.previous_event_fingerprint != previous
        ):
            raise ValueError("offline V2 workflow event chain is discontinuous")
        events.append(event)
        previous = event.fingerprint
    return tuple(events)


def _ensure_event(
    events: list[WorkflowEventV2],
    run_dir: Path,
    workflow_ref: ContractRef,
    stage: str,
    object_ref: ContractRef,
) -> None:
    existing = [item for item in events if item.stage == stage]
    if existing:
        if len(existing) != 1 or existing[0].object_ref != object_ref:
            raise ValueError("V2 workflow stage event differs from recovered artifact")
        return
    event = WorkflowEventV2(
        schema_version=RTO_V2_SCHEMA_VERSION,
        event_version="offline-workflow-event-v2",
        workflow_ref=workflow_ref,
        sequence=len(events),
        stage=stage,
        object_ref=object_ref,
        occurred_at=utc_now(),
        previous_event_fingerprint=None if not events else events[-1].fingerprint,
        claim_scope=CLAIM_SCOPE,
    )
    with (run_dir / "events.jsonl").open("ab") as stream:
        stream.write(canonical_json_bytes(event.as_dict()) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    events.append(event)


@contextmanager
def _workflow_lock(run_dir: Path) -> Iterator[None]:
    lock = run_dir / ".workflow.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("offline V2 workflow is locked by another writer") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)
