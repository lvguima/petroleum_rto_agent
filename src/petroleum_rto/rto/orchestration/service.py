"""Synchronous, resumable R6 orchestration over the neutral simulator port."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ..catalogs import RtoCatalogBundle
from ..compilation import CandidatePlanCompiler
from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    ContractRef,
    OptimizationProblemV1,
    OptimizationResultV1,
    RunEvidenceRefV1,
    StaticSearchResultV1,
)
from ..contracts.common import canonical_json_bytes
from ..evaluation import DynamicEvaluationService, SteadyEvaluationService
from ..optimizer import DeterministicGridOptimizer, DynamicFinalSelector
from ..ports import ProviderRequestFactory, SimulatorPort
from ..problem import ProblemBuilder
from ..strategies import (
    StrategyBuilder,
    StrategyEntryV1,
    StrategyRepository,
    anchor_from_evaluations,
    optimization_result_ref,
    utc_now,
)
from .models import (
    AnchorAttemptV1,
    AnchorValidationResultV1,
    OfflineRtoManifestV1,
    OfflineRtoRequestV1,
    OfflineRtoResultV1,
    OfflineRunStatus,
    WorkflowEventV1,
)

SimulatorFactory = Callable[[Path], SimulatorPort]


@dataclass(frozen=True)
class OfflineRtoRunRecord:
    run_dir: Path
    request: OfflineRtoRequestV1
    problem: OptimizationProblemV1
    static_search: StaticSearchResultV1
    optimization_result: OptimizationResultV1
    anchor_validation: AnchorValidationResultV1 | None
    strategy: StrategyEntryV1 | None
    result: OfflineRtoResultV1
    manifest: OfflineRtoManifestV1
    events: tuple[WorkflowEventV1, ...]
    recovered_stages: tuple[str, ...]
    physical_m2_executions: int
    physical_m4_executions: int


class _ReplayDynamicEvaluator:
    def __init__(self, evaluations: tuple[CandidateEvaluationV1, ...]) -> None:
        self._evaluations = {item.proposal_ref: item for item in evaluations}

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        try:
            return self._evaluations[proposal.ref]
        except KeyError as exc:
            raise ValueError("replayed dynamic evidence lacks a shortlisted proposal") from exc


class OfflineRtoOrchestrator:
    """Run or resume one deterministic offline RTO workflow without auto-approval."""

    def __init__(
        self,
        request_factory: ProviderRequestFactory,
        simulator_factory: SimulatorFactory,
    ) -> None:
        self._request_factory = request_factory
        self._simulator_factory = simulator_factory

    def run(
        self,
        bundle: RtoCatalogBundle,
        *,
        run_root: Path,
        strategy_repository: StrategyRepository,
        actor: str,
        coverage_policy: str = "sampled-anchors",
        external_request_ref: ContractRef | None = None,
    ) -> OfflineRtoRunRecord:
        request = _request_from_bundle(
            bundle,
            coverage_policy=coverage_policy,
            external_request_ref=external_request_ref,
        )
        run_dir = run_root / request.workflow_id
        run_dir.mkdir(parents=True, exist_ok=True)
        simulator = self._simulator_factory(run_dir / "simulator")
        with _workflow_lock(run_dir):
            if (run_dir / "manifest.json").exists():
                return read_offline_run(
                    run_dir,
                    bundle=bundle,
                    strategy_repository=strategy_repository,
                    simulator=simulator,
                    recovered_stages=("manifest",),
                    external_request_ref=external_request_ref,
                )
            recovered: list[str] = []
            events = list(_read_events(run_dir / "events.jsonl", request.ref, allow_missing=True))
            _write_or_verify(run_dir / "request.json", request.as_dict())
            _ensure_event(events, run_dir, request.ref, "request-ready", request.ref)

            expected_problem = ProblemBuilder().build(bundle)
            problem_path = run_dir / "problem.json"
            if problem_path.exists():
                problem = OptimizationProblemV1.from_mapping(_read_json(problem_path))
                if problem != expected_problem:
                    raise ValueError("recovered problem differs from current deterministic inputs")
                recovered.append("problem")
            else:
                problem = expected_problem
                _write_or_verify(problem_path, problem.as_dict())
            _ensure_event(events, run_dir, request.ref, "problem-ready", problem.ref)

            static_path = run_dir / "static_search.json"
            m2_executions = 0
            if static_path.exists():
                static = StaticSearchResultV1.from_mapping(_read_json(static_path))
                _validate_static(problem, static)
                _verify_evaluations(simulator, static.evaluations)
                recovered.append("static-search")
            else:
                steady = SteadyEvaluationService(
                    problem,
                    bundle.context,
                    bundle.kpi_catalog,
                    CandidatePlanCompiler(),
                    self._request_factory,
                    simulator,
                )
                static = DeterministicGridOptimizer().search(
                    problem,
                    bundle.context,
                    steady,
                )
                m2_executions += steady.physical_execution_count
                _write_or_verify(static_path, static.as_dict())
            _ensure_event(events, run_dir, request.ref, "static-search-ready", static.ref)

            optimization_path = run_dir / "optimization_result.json"
            m4_executions = 0
            if optimization_path.exists():
                optimization = OptimizationResultV1.from_mapping(_read_json(optimization_path))
                _validate_optimization(problem, static, optimization)
                _verify_evaluations(simulator, optimization.dynamic_evaluations)
                recovered.append("optimization-result")
            else:
                dynamic = DynamicEvaluationService(
                    problem,
                    bundle.context,
                    bundle.kpi_catalog,
                    CandidatePlanCompiler(),
                    self._request_factory,
                    simulator,
                )
                optimization = DynamicFinalSelector().select(problem, static, dynamic)
                m4_executions += dynamic.physical_execution_count
                _write_or_verify(optimization_path, optimization.as_dict())
            optimization_ref = optimization_result_ref(optimization)
            _ensure_event(
                events,
                run_dir,
                request.ref,
                "optimization-result-ready",
                optimization_ref,
            )

            anchor_validation: AnchorValidationResultV1 | None = None
            strategy: StrategyEntryV1 | None = None
            if optimization.status == "success":
                anchors_path = run_dir / "anchor_validation.json"
                if anchors_path.exists():
                    anchor_validation = AnchorValidationResultV1.from_mapping(
                        _read_json(anchors_path)
                    )
                    _validate_anchor_result(
                        bundle, problem, static, optimization, anchor_validation
                    )
                    _verify_anchor_evidence(simulator, anchor_validation)
                    recovered.append("anchor-validation")
                else:
                    anchor_validation, added_m2, added_m4 = self._validate_anchors(
                        bundle,
                        problem,
                        static,
                        optimization,
                        simulator,
                        coverage_policy=coverage_policy,
                    )
                    m2_executions += added_m2
                    m4_executions += added_m4
                    _write_or_verify(anchors_path, anchor_validation.as_dict())
                _ensure_event(
                    events,
                    run_dir,
                    request.ref,
                    "anchor-validation-ready",
                    anchor_validation.ref,
                )
                if any(
                    item.static_evaluation.status in {"invalid_request", "evaluation_error"}
                    or (
                        item.dynamic_evaluation is not None
                        and item.dynamic_evaluation.status
                        in {"invalid_request", "evaluation_error"}
                    )
                    for item in anchor_validation.attempts
                ):
                    strategy = None
                else:
                    anchors = tuple(
                        anchor_from_evaluations(
                            item.context,
                            item.proposal,
                            item.static_evaluation,
                            cast(CandidateEvaluationV1, item.dynamic_evaluation),
                        )
                        for item in anchor_validation.passed_attempts
                    )
                    strategy = StrategyBuilder().build(
                        problem,
                        bundle.context,
                        static,
                        optimization,
                        anchors,
                    )
                    strategy_path = run_dir / "strategy.json"
                    _write_or_verify(strategy_path, strategy.as_dict())
                    stored = strategy_repository.create_draft(strategy, actor=actor)
                    if stored.entry != strategy or stored.current_state != "draft":
                        raise ValueError(
                            "workflow strategy repository entry is not an unchanged draft"
                        )
                    _ensure_event(
                        events,
                        run_dir,
                        request.ref,
                        "strategy-draft-ready",
                        strategy.ref,
                    )

            requested_anchors = 0 if anchor_validation is None else len(anchor_validation.attempts)
            passed_anchors = (
                0 if anchor_validation is None else len(anchor_validation.passed_attempts)
            )
            if strategy is not None:
                workflow_status: OfflineRunStatus = "completed_draft"
                reason = (
                    "sampled-anchor-draft-created"
                    if passed_anchors == requested_anchors
                    else "partial-anchor-draft-created"
                )
            elif optimization.status == "evaluation_error" or (
                anchor_validation is not None
                and any(
                    item.static_evaluation.status in {"invalid_request", "evaluation_error"}
                    or (
                        item.dynamic_evaluation is not None
                        and item.dynamic_evaluation.status
                        in {"invalid_request", "evaluation_error"}
                    )
                    for item in anchor_validation.attempts
                )
            ):
                workflow_status = "failed"
                reason = "offline-evaluation-error"
            else:
                workflow_status = "completed_without_strategy"
                reason = "optimization-not-publishable"
            result = OfflineRtoResultV1(
                schema_version=RTO_SCHEMA_VERSION,
                result_version="offline-rto-result-v1",
                status=workflow_status,
                request_ref=request.ref,
                problem_ref=problem.ref,
                static_search_ref=static.ref,
                optimization_result_ref=optimization_ref,
                strategy_ref=None if strategy is None else strategy.ref,
                requested_anchor_count=requested_anchors,
                passed_anchor_count=passed_anchors,
                termination_reason=reason,
                claim_scope=CLAIM_SCOPE,
            )
            _write_or_verify(run_dir / "result.json", result.as_dict())
            _ensure_event(events, run_dir, request.ref, "workflow-complete", result.ref)
            _commit_manifest(run_dir, request, result)
        verified = read_offline_run(
            run_dir,
            bundle=bundle,
            strategy_repository=strategy_repository,
            simulator=simulator,
            recovered_stages=tuple(recovered),
            external_request_ref=external_request_ref,
        )
        return replace(
            verified,
            physical_m2_executions=m2_executions,
            physical_m4_executions=m4_executions,
        )

    def _validate_anchors(
        self,
        bundle: RtoCatalogBundle,
        problem: OptimizationProblemV1,
        static: StaticSearchResultV1,
        optimization: OptimizationResultV1,
        simulator: SimulatorPort,
        *,
        coverage_policy: str,
    ) -> tuple[AnchorValidationResultV1, int, int]:
        selected_ref = optimization.selected_proposal_ref
        selected_static_ref = optimization.selected_static_evaluation_ref
        selected_dynamic_ref = optimization.selected_dynamic_evaluation_ref
        if selected_ref is None or selected_static_ref is None or selected_dynamic_ref is None:
            raise ValueError("publishable result lacks selected refs")
        selected = next(item for item in static.proposals if item.ref == selected_ref)
        selected_static = next(
            item for item in static.evaluations if item.ref == selected_static_ref
        )
        selected_dynamic = next(
            item for item in optimization.dynamic_evaluations if item.ref == selected_dynamic_ref
        )
        ratios = (
            (1.0,) if coverage_policy == "point" else problem.evaluation_plan.feed_anchor_ratios
        )
        attempts: list[AnchorAttemptV1] = []
        m2_count = 0
        m4_count = 0
        for index, ratio in enumerate(ratios):
            if abs(ratio - 1.0) <= 1e-12:
                attempts.append(
                    AnchorAttemptV1(
                        ratio=ratio,
                        context=bundle.context,
                        problem=problem,
                        proposal=selected,
                        static_evaluation=selected_static,
                        dynamic_evaluation=selected_dynamic,
                    )
                )
                continue
            anchor_bundle = _anchor_bundle(bundle, ratio)
            anchor_problem = ProblemBuilder().build(anchor_bundle)
            proposal = CandidateProposalV1(
                schema_version=RTO_SCHEMA_VERSION,
                proposal_version="candidate-proposal-v1",
                candidate_id=f"anchor-{index:02d}",
                sequence=index,
                origin="anchor-validation",
                problem_ref=anchor_problem.ref,
                context_ref=anchor_bundle.context.ref,
                decision_values=selected.decision_values,
                output_kind="steady-setpoint-vector",
                claim_scope=CLAIM_SCOPE,
            )
            steady = SteadyEvaluationService(
                anchor_problem,
                anchor_bundle.context,
                bundle.kpi_catalog,
                CandidatePlanCompiler(),
                self._request_factory,
                simulator,
            )
            static_evaluation = steady.evaluate(proposal)
            m2_count += steady.physical_execution_count
            dynamic_evaluation: CandidateEvaluationV1 | None = None
            if static_evaluation.status == "feasible":
                dynamic = DynamicEvaluationService(
                    anchor_problem,
                    anchor_bundle.context,
                    bundle.kpi_catalog,
                    CandidatePlanCompiler(),
                    self._request_factory,
                    simulator,
                )
                dynamic_evaluation = dynamic.evaluate(proposal)
                m4_count += dynamic.physical_execution_count
            attempts.append(
                AnchorAttemptV1(
                    ratio=ratio,
                    context=anchor_bundle.context,
                    problem=anchor_problem,
                    proposal=proposal,
                    static_evaluation=static_evaluation,
                    dynamic_evaluation=dynamic_evaluation,
                )
            )
        return (
            AnchorValidationResultV1(
                schema_version=RTO_SCHEMA_VERSION,
                validation_version="sampled-anchor-validation-v1",
                selected_action=selected.decision_values,
                attempts=tuple(attempts),
                claim_scope=CLAIM_SCOPE,
            ),
            m2_count,
            m4_count,
        )


def _request_from_bundle(
    bundle: RtoCatalogBundle,
    *,
    coverage_policy: str,
    external_request_ref: ContractRef | None = None,
) -> OfflineRtoRequestV1:
    return OfflineRtoRequestV1(
        schema_version=RTO_SCHEMA_VERSION,
        request_version="offline-rto-request-v1",
        intent_ref=bundle.intent.ref,
        context_ref=bundle.context.ref,
        decision_catalog_ref=bundle.decision_catalog.ref,
        kpi_catalog_ref=bundle.kpi_catalog.ref,
        constraint_profile_ref=bundle.constraint_profile.ref,
        policy_ref=bundle.policy.ref,
        provider_id=bundle.context.provider_id,
        coverage_policy=coverage_policy,
        claim_scope=CLAIM_SCOPE,
        external_request_ref=external_request_ref,
    )


def _anchor_bundle(bundle: RtoCatalogBundle, ratio: float) -> RtoCatalogBundle:
    if abs(ratio - 1.0) <= 1e-12:
        return bundle
    suffix = f"{round(ratio * 1000):04d}"
    context = replace(
        bundle.context,
        context_id=f"{bundle.context.context_id}-feed-{suffix}",
        feed_mass_flow_kg_s=bundle.context.feed_mass_flow_kg_s * ratio,
    )
    intent = replace(
        bundle.intent,
        intent_id=f"{bundle.intent.intent_id}-feed-{suffix}",
        operating_context_ref=context.ref,
    )
    return replace(bundle, context=context, intent=intent)


def _validate_static(problem: OptimizationProblemV1, static: StaticSearchResultV1) -> None:
    if static.problem_ref != problem.ref or static.context_ref != problem.context_ref:
        raise ValueError("static search references another problem or context")
    feasible = [item for item in static.evaluations if item.status == "feasible"]
    expected = tuple(
        sorted(
            feasible,
            key=lambda item: (
                cast(float, item.candidate_objective),
                -cast(float, item.minimum_normalized_margin),
                item.normalized_action_l1,
                item.proposal_ref.fingerprint,
            ),
        )
    )
    if static.ranked_feasible != expected:
        raise ValueError("static ranking differs from the deterministic sort policy")


def _validate_optimization(
    problem: OptimizationProblemV1,
    static: StaticSearchResultV1,
    optimization: OptimizationResultV1,
) -> None:
    replayed = DynamicFinalSelector().select(
        problem,
        static,
        _ReplayDynamicEvaluator(optimization.dynamic_evaluations),
    )
    if replayed != optimization:
        raise ValueError("optimization result differs from deterministic final selection")


def _validate_anchor_result(
    bundle: RtoCatalogBundle,
    problem: OptimizationProblemV1,
    static: StaticSearchResultV1,
    optimization: OptimizationResultV1,
    validation: AnchorValidationResultV1,
) -> None:
    selected_ref = optimization.selected_proposal_ref
    if selected_ref is None:
        raise ValueError("anchor validation requires a selected proposal")
    selected = next(item for item in static.proposals if item.ref == selected_ref)
    if dict(validation.selected_action) != dict(selected.decision_values):
        raise ValueError("anchor validation action differs from selected proposal")
    for attempt in validation.attempts:
        expected_bundle = _anchor_bundle(bundle, attempt.ratio)
        expected_problem = ProblemBuilder().build(expected_bundle)
        if attempt.context != expected_bundle.context or attempt.problem != expected_problem:
            raise ValueError("anchor attempt differs from deterministic sampled context")
        if attempt.ratio == 1.0 and attempt.problem != problem:
            raise ValueError("central anchor differs from central optimization problem")


def _verify_anchor_evidence(
    simulator: SimulatorPort,
    validation: AnchorValidationResultV1,
) -> None:
    evaluations = tuple(
        evaluation
        for item in validation.attempts
        for evaluation in (item.static_evaluation, item.dynamic_evaluation)
        if evaluation is not None
    )
    _verify_evaluations(simulator, evaluations)


def _verify_evaluations(
    simulator: SimulatorPort,
    evaluations: tuple[CandidateEvaluationV1, ...],
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
                raise ValueError("strict simulator evidence differs from stored RTO evaluation")


def _commit_manifest(
    run_dir: Path,
    request: OfflineRtoRequestV1,
    result: OfflineRtoResultV1,
) -> OfflineRtoManifestV1:
    names = [
        "anchor_validation.json",
        "events.jsonl",
        "optimization_result.json",
        "problem.json",
        "request.json",
        "result.json",
        "static_search.json",
        "strategy.json",
    ]
    files = {
        name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        for name in names
        if (run_dir / name).is_file()
    }
    manifest = OfflineRtoManifestV1(
        schema_version=RTO_SCHEMA_VERSION,
        manifest_version="offline-rto-manifest-v1",
        workflow_ref=request.ref,
        result_ref=result.ref,
        files=dict(sorted(files.items())),
        software_versions={
            "petroleum-rto": "0.1.0",
            "rto-contract": RTO_SCHEMA_VERSION,
        },
        created_at=utc_now(),
        claim_scope=CLAIM_SCOPE,
    )
    _write_or_verify(run_dir / "manifest.json", manifest.as_dict())
    return manifest


def read_offline_run(
    run_dir: Path,
    *,
    bundle: RtoCatalogBundle,
    strategy_repository: StrategyRepository,
    simulator: SimulatorPort,
    recovered_stages: tuple[str, ...] = (),
    external_request_ref: ContractRef | None = None,
) -> OfflineRtoRunRecord:
    manifest = OfflineRtoManifestV1.from_mapping(_read_json(run_dir / "manifest.json"))
    allowed = set(manifest.files) | {"manifest.json", "simulator"}
    actual_top = {item.name for item in run_dir.iterdir() if not item.name.startswith(".")}
    if actual_top - allowed:
        raise ValueError("offline run contains unexpected top-level artifacts")
    for relative, expected in manifest.files.items():
        path = run_dir / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"offline artifact hash differs: {relative}")
    request = OfflineRtoRequestV1.from_mapping(_read_json(run_dir / "request.json"))
    expected_request = _request_from_bundle(
        bundle,
        coverage_policy=request.coverage_policy,
        external_request_ref=external_request_ref,
    )
    if request != expected_request or manifest.workflow_ref != request.ref:
        raise ValueError("offline request differs from current strict policy inputs")
    problem = OptimizationProblemV1.from_mapping(_read_json(run_dir / "problem.json"))
    if problem != ProblemBuilder().build(bundle):
        raise ValueError("offline problem differs from deterministic builder output")
    static = StaticSearchResultV1.from_mapping(_read_json(run_dir / "static_search.json"))
    _validate_static(problem, static)
    optimization = OptimizationResultV1.from_mapping(
        _read_json(run_dir / "optimization_result.json")
    )
    _validate_optimization(problem, static, optimization)
    _verify_evaluations(simulator, static.evaluations)
    _verify_evaluations(simulator, optimization.dynamic_evaluations)
    anchor_path = run_dir / "anchor_validation.json"
    anchor_validation = (
        None
        if not anchor_path.exists()
        else AnchorValidationResultV1.from_mapping(_read_json(anchor_path))
    )
    if anchor_validation is not None:
        _validate_anchor_result(bundle, problem, static, optimization, anchor_validation)
        _verify_anchor_evidence(simulator, anchor_validation)
    strategy_path = run_dir / "strategy.json"
    strategy = (
        None
        if not strategy_path.exists()
        else StrategyEntryV1.from_mapping(_read_json(strategy_path))
    )
    if strategy is not None:
        if anchor_validation is None:
            raise ValueError("offline strategy exists without anchor validation")
        anchors = tuple(
            anchor_from_evaluations(
                item.context,
                item.proposal,
                item.static_evaluation,
                cast(CandidateEvaluationV1, item.dynamic_evaluation),
            )
            for item in anchor_validation.passed_attempts
        )
        rebuilt = StrategyBuilder().build(
            problem,
            bundle.context,
            static,
            optimization,
            anchors,
        )
        if rebuilt != strategy:
            raise ValueError("offline strategy differs from deterministic strategy builder")
        stored = strategy_repository.read_ref(strategy.ref)
        if stored.entry != strategy:
            raise ValueError("strategy repository differs from offline workflow strategy")
    result = OfflineRtoResultV1.from_mapping(_read_json(run_dir / "result.json"))
    expected_result = OfflineRtoResultV1(
        schema_version=RTO_SCHEMA_VERSION,
        result_version="offline-rto-result-v1",
        status=(
            "completed_draft"
            if strategy is not None
            else "failed"
            if optimization.status == "evaluation_error"
            or (
                anchor_validation is not None
                and any(
                    item.static_evaluation.status in {"invalid_request", "evaluation_error"}
                    or (
                        item.dynamic_evaluation is not None
                        and item.dynamic_evaluation.status
                        in {"invalid_request", "evaluation_error"}
                    )
                    for item in anchor_validation.attempts
                )
            )
            else "completed_without_strategy"
        ),
        request_ref=request.ref,
        problem_ref=problem.ref,
        static_search_ref=static.ref,
        optimization_result_ref=optimization_result_ref(optimization),
        strategy_ref=None if strategy is None else strategy.ref,
        requested_anchor_count=0 if anchor_validation is None else len(anchor_validation.attempts),
        passed_anchor_count=(
            0 if anchor_validation is None else len(anchor_validation.passed_attempts)
        ),
        termination_reason=result.termination_reason,
        claim_scope=CLAIM_SCOPE,
    )
    if result != expected_result or manifest.result_ref != result.ref:
        raise ValueError("offline result differs from strict embedded evidence")
    events = _read_events(run_dir / "events.jsonl", request.ref, allow_missing=False)
    if not events or events[-1].stage != "workflow-complete" or events[-1].object_ref != result.ref:
        raise ValueError("offline workflow event log does not end at the result")
    return OfflineRtoRunRecord(
        run_dir=run_dir,
        request=request,
        problem=problem,
        static_search=static,
        optimization_result=optimization,
        anchor_validation=anchor_validation,
        strategy=strategy,
        result=result,
        manifest=manifest,
        events=events,
        recovered_stages=recovered_stages,
        physical_m2_executions=0,
        physical_m4_executions=0,
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read strict offline JSON: {path}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"offline JSON must be an object: {path}")
    return cast(dict[str, object], value)


def _write_or_verify(path: Path, value: Mapping[str, object]) -> None:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"existing offline artifact differs: {path.name}")
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
) -> tuple[WorkflowEventV1, ...]:
    if not path.exists():
        if allow_missing:
            return ()
        raise ValueError("offline workflow event log is missing")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or any(not item.strip() for item in lines):
        raise ValueError("offline workflow event log is empty or malformed")
    events: list[WorkflowEventV1] = []
    previous: str | None = None
    for index, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("offline workflow event contains invalid JSON") from exc
        if not isinstance(raw, dict):
            raise TypeError("offline workflow event must be an object")
        event = WorkflowEventV1.from_mapping(cast(dict[str, object], raw))
        if (
            event.workflow_ref != workflow_ref
            or event.sequence != index
            or event.previous_event_fingerprint != previous
        ):
            raise ValueError("offline workflow event chain is discontinuous")
        events.append(event)
        previous = event.fingerprint
    return tuple(events)


def _ensure_event(
    events: list[WorkflowEventV1],
    run_dir: Path,
    workflow_ref: ContractRef,
    stage: str,
    object_ref: ContractRef,
) -> None:
    existing = [item for item in events if item.stage == stage]
    if existing:
        if len(existing) != 1 or existing[0].object_ref != object_ref:
            raise ValueError("workflow stage event differs from recovered artifact")
        return
    event = WorkflowEventV1(
        schema_version=RTO_SCHEMA_VERSION,
        event_version="offline-workflow-event-v1",
        workflow_ref=workflow_ref,
        sequence=len(events),
        stage=stage,
        object_ref=object_ref,
        occurred_at=utc_now(),
        previous_event_fingerprint=None if not events else events[-1].fingerprint,
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
        raise RuntimeError("offline RTO workflow is locked by another writer") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)
