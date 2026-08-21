"""Objective-count-neutral M4 dynamic verification over paired evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..capabilities.models import CapabilityCatalog, MetricCapability
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
)
from ..contracts.common import digest, finite
from ..contracts.context import OperatingContext
from ..contracts.evidence import RunEvidenceRef
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from ..contracts.simulation import SimulationEvaluationRequest, SimulationRunBundle
from ..ports.interfaces import ProviderRequestFactory, SimulatorPort
from .common import (
    constraint_outcome,
    normalized_action_l1,
    safe_normalized_action_l1,
    validate_pair_bundles,
    validate_preview,
)

_M4_FORMULA_ID = "existing_m4_acceptance_v1"
_M4_ACCEPTANCE_CHECKS = {
    "plant_execution",
    "plant_conservation",
    "automatic_initialization_no_bump",
    "baseline_hold",
    "loop_performance",
    "true_inventory_safety",
}


def _path_value(root: object, dotted_path: str) -> object:
    current = root
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


@dataclass(frozen=True)
class _AcceptanceEvidence:
    accepted: bool
    control_fingerprint: str
    checks: Mapping[str, bool]
    loops: Mapping[str, Mapping[str, object]]


def _acceptance_evidence(
    bundle: SimulationRunBundle,
    metric: MetricCapability,
) -> _AcceptanceEvidence:
    if metric.formula_ref != _M4_FORMULA_ID or len(metric.source_paths) != 2:
        raise ValueError("M4 acceptance metric lacks its trusted binding")
    accepted = _path_value(bundle.as_dict(), metric.source_paths[0])
    if not isinstance(accepted, bool):
        raise TypeError("M4 acceptance result must be boolean")
    control_fingerprint = digest(
        _path_value(bundle.as_dict(), metric.source_paths[1]),
        context="M4 control fingerprint",
    )
    raw_checks = _path_value(bundle.as_dict(), "summary.acceptance_checks")
    raw_loops = _path_value(bundle.as_dict(), "summary.loop_performance")
    if not isinstance(raw_checks, Mapping) or set(raw_checks) != _M4_ACCEPTANCE_CHECKS:
        raise ValueError("M4 acceptance checks are incomplete")
    if any(
        not isinstance(key, str) or not isinstance(value, bool) for key, value in raw_checks.items()
    ):
        raise TypeError("M4 acceptance checks must be boolean")
    if not isinstance(raw_loops, Mapping) or len(raw_loops) != 7:
        raise ValueError("M4 acceptance must contain seven loop summaries")
    loops: dict[str, Mapping[str, object]] = {}
    for key, value in raw_loops.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise TypeError("M4 loop summaries must be named objects")
        if not isinstance(value.get("passed"), bool):
            raise TypeError("M4 loop acceptance must be boolean")
        loops[key] = value
    checks = {str(key): bool(value) for key, value in raw_checks.items()}
    if accepted and (
        not all(checks.values()) or not all(value["passed"] is True for value in loops.values())
    ):
        raise ValueError("accepted M4 evidence contains a failed acceptance component")
    if (
        not accepted
        and all(checks.values())
        and all(value["passed"] is True for value in loops.values())
    ):
        raise ValueError("failed M4 acceptance has no failed acceptance component")
    return _AcceptanceEvidence(
        accepted=accepted,
        control_fingerprint=control_fingerprint,
        checks=checks,
        loops=loops,
    )


def _optional_loop_value(value: object, *, context: str) -> float | None:
    if value is None:
        return None
    return finite(value, context=context)


def _dynamic_metrics(evidence: _AcceptanceEvidence) -> dict[str, float]:
    settling: list[float] = []
    saturation: list[float] = []
    final_errors: list[float] = []
    for loop_id, loop in evidence.loops.items():
        raw_settling = _optional_loop_value(
            loop.get("settling_time_s"), context=f"{loop_id}.settling_time_s"
        )
        raw_saturation = _optional_loop_value(
            loop.get("longest_continuous_saturation_s"),
            context=f"{loop_id}.longest_continuous_saturation_s",
        )
        raw_error = _optional_loop_value(
            loop.get("final_error_fraction"),
            context=f"{loop_id}.final_error_fraction",
        )
        if raw_settling is not None:
            settling.append(raw_settling)
        if raw_saturation is not None:
            saturation.append(raw_saturation)
        if raw_error is not None:
            final_errors.append(raw_error)
    return {
        "m4_acceptance_passed": 1.0 if evidence.accepted else 0.0,
        "m4_failed_check_count": float(
            sum(value is not True for value in evidence.checks.values())
        ),
        "m4_failed_loop_count": float(
            sum(value["passed"] is not True for value in evidence.loops.values())
        ),
        "m4_max_settling_time_s": max(settling, default=0.0),
        "m4_max_continuous_saturation_s": max(saturation, default=0.0),
        "m4_max_final_error_fraction": max(final_errors, default=0.0),
    }


class M4PairedEvaluator:
    """Apply existing M4 acceptance without recomputing any M2 objective."""

    def __init__(self, problem: OptimizationProblem, catalog: CapabilityCatalog) -> None:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("evaluator requires an OptimizationProblem")
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("evaluator requires a CapabilityCatalog")
        if problem.capability_catalog_ref != catalog.ref:
            raise ValueError("problem references another capability catalog")
        if problem.evaluation_plan.dynamic_stage != "M4":
            raise ValueError("M4 evaluator requires M4 as the dynamic stage")
        if not problem.evaluation_plan.dynamic_verification_required:
            raise ValueError("problem does not require dynamic verification")
        if any(rule.source == "business" for rule in problem.hard_constraints):
            raise ValueError("unbound business constraints cannot enter M4 evaluation")
        rules = tuple(rule for rule in problem.hard_constraints if rule.evaluation_stage == "M4")
        if not rules:
            raise ValueError("M4 evaluation requires an M4 hard constraint")
        if {rule.metric_id for rule in rules} != {"m4_acceptance_passed"}:
            raise ValueError("M4 hard constraints must use the existing acceptance metric")
        metrics = {item.metric_id: item for item in catalog.metrics}
        guardrails = {item.guardrail_id: item for item in catalog.guardrails}
        for rule in rules:
            metric = metrics.get(rule.metric_id)
            guardrail = guardrails.get(rule.constraint_id)
            if (
                metric is None
                or guardrail is None
                or metric.stage != "M4"
                or metric.unit != rule.unit
                or metric.formula_ref != _M4_FORMULA_ID
                or len(metric.source_paths) != 2
                or guardrail.metric_id != rule.metric_id
                or guardrail.stage != rule.evaluation_stage
                or guardrail.unit != rule.unit
                or rule.operator not in guardrail.allowed_operators
            ):
                raise ValueError(
                    f"M4 constraint {rule.constraint_id!r} lacks a trusted acceptance binding"
                )
        self._problem = problem
        self._catalog = catalog
        self._rules = rules
        self._metrics = metrics

    def validate_baseline(
        self,
        pair: CompiledPair,
        baseline: SimulationRunBundle,
    ) -> None:
        """Reject an unusable shared baseline before any candidate execution."""

        if pair.baseline.stage != "M4":
            raise ValueError("M4 baseline requires an M4 request")
        if (
            baseline.provider_id != pair.baseline.provider_id
            or baseline.provider_request_fingerprint != pair.baseline.provider_request_fingerprint
        ):
            raise ValueError("M4 baseline evidence differs from its compiled request")
        metric = self._metrics[self._rules[0].metric_id]
        acceptance = _acceptance_evidence(baseline, metric)
        if (
            baseline.runtime_status != "success"
            or baseline.engine_status != "success"
            or not acceptance.accepted
        ):
            raise ValueError("M4 baseline is not dynamically eligible")

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
        if pair.baseline.stage != "M4" or pair.candidate.stage != "M4":
            raise ValueError("M4 evaluator requires an M4 pair")
        validate_pair_bundles(pair, self._problem, self._catalog, baseline, candidate)
        evidence = (
            RunEvidenceRef.from_bundle(baseline, pair_role="baseline"),
            RunEvidenceRef.from_bundle(candidate, pair_role="candidate"),
        )
        action = normalized_action_l1(self._problem, proposal)
        metric = self._metrics[self._rules[0].metric_id]
        try:
            self.validate_baseline(pair, baseline)
            baseline_acceptance = _acceptance_evidence(baseline, metric)
        except (KeyError, TypeError, ValueError):
            return self._failure(
                proposal,
                pair,
                status="evaluation_error",
                reason="m4-baseline-invalid",
                action=action,
                evidence=evidence,
            )
        if candidate.runtime_status == "rejected":
            return self._failure(
                proposal,
                pair,
                status="invalid_request",
                reason="m4-request-rejected",
                action=action,
                evidence=evidence,
            )
        try:
            candidate_acceptance = _acceptance_evidence(candidate, metric)
            if baseline_acceptance.control_fingerprint != candidate_acceptance.control_fingerprint:
                raise ValueError("paired M4 control fingerprints differ")
            metrics = _dynamic_metrics(candidate_acceptance)
        except (KeyError, TypeError, ValueError):
            return self._failure(
                proposal,
                pair,
                status="evaluation_error",
                reason="m4-evidence-incomplete",
                action=action,
                evidence=evidence,
            )
        outcomes = tuple(constraint_outcome(rule, metrics[rule.metric_id]) for rule in self._rules)
        failed = tuple(sorted(item.constraint_id for item in outcomes if not item.passed))
        execution_success = (
            candidate.runtime_status == "success" and candidate.engine_status == "success"
        )
        performance_failure = (
            not candidate_acceptance.accepted and candidate.failure_stage == "performance"
        )
        if execution_success and candidate_acceptance.accepted and not failed:
            status: EvaluationStatus = "feasible"
            reasons: tuple[str, ...] = ()
        elif (execution_success or performance_failure) and (
            not candidate_acceptance.accepted or failed
        ):
            status = "process_infeasible"
            reasons = failed or ("m4-dynamic-acceptance-failed",)
        else:
            return self._failure(
                proposal,
                pair,
                status="evaluation_error",
                reason="m4-execution-error",
                action=action,
                evidence=evidence,
            )
        return CandidateEvaluation(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation",
            stage="M4",
            status=status,
            problem_ref=self._problem.ref,
            context_ref=self._problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair.baseline.pair_id,
            objective_outcomes=(),
            metrics=metrics,
            constraints=outcomes,
            minimum_normalized_margin=min(item.normalized_margin for item in outcomes),
            normalized_action_l1=action,
            reason_codes=reasons,
            evidence_refs=evidence,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
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
            stage="M4",
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


class M4EvaluationService:
    """Execute and cache M4 pairs for any objective count with one baseline."""

    def __init__(
        self,
        problem: OptimizationProblem,
        context: OperatingContext,
        catalog: CapabilityCatalog,
        compiler: CandidatePlanCompiler,
        request_factory: ProviderRequestFactory,
        simulator: SimulatorPort,
    ) -> None:
        if context.ref != problem.context_ref:
            raise ValueError("problem and evaluation context differ")
        self._problem = problem
        self._context = context
        self._catalog = catalog
        self._compiler = compiler
        self._request_factory = request_factory
        self._simulator = simulator
        self._evaluator = M4PairedEvaluator(problem, catalog)
        self._baseline_request_fingerprint: str | None = None
        self._baseline_bundle: SimulationRunBundle | None = None
        self._baseline_error_reason: str | None = None
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
                stage="M4",
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
            if self._baseline_error_reason is not None:
                result = self._error_evaluation(
                    proposal,
                    status="evaluation_error",
                    reason=self._baseline_error_reason,
                    pair_id=pair.baseline.pair_id,
                )
                self._cache[proposal.fingerprint] = result
                return result
            candidate = self._execute(pair.candidate)
            result = self._evaluator.evaluate(proposal, pair, baseline, candidate)
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
            try:
                self._evaluator.validate_baseline(pair, self._baseline_bundle)
            except (KeyError, TypeError, ValueError):
                self._baseline_error_reason = "m4-baseline-invalid"
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
            stage="M4",
            status=status,
            problem_ref=self._problem.ref,
            context_ref=self._problem.context_ref,
            proposal_ref=proposal.ref,
            pair_id=pair_id or f"pair-m4-{proposal.fingerprint[:16]}",
            objective_outcomes=(),
            metrics={},
            constraints=(),
            minimum_normalized_margin=None,
            normalized_action_l1=safe_normalized_action_l1(self._problem, proposal),
            reason_codes=(reason,),
            evidence_refs=(),
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )


__all__ = ["M4EvaluationService", "M4PairedEvaluator"]
