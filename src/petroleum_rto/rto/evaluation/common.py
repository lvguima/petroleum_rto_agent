"""Shared strict extraction and paired-evidence checks."""

from __future__ import annotations

import math
from collections.abc import Mapping

from ..compilation import CompiledPair, assert_compiled_pair
from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    ConstraintOutcomeV1,
    ConstraintRuleV1,
    EvaluationStage,
    EvaluationStatus,
    OptimizationProblemV1,
    SimulationRunBundleV1,
)


def path_value(root: object, dotted_path: str) -> object:
    current = root
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


def finite_path(root: object, dotted_path: str) -> float:
    value = path_value(root, dotted_path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{dotted_path} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{dotted_path} is not finite")
    return result


def boolean_path(root: object, dotted_path: str) -> bool:
    value = path_value(root, dotted_path)
    if not isinstance(value, bool):
        raise TypeError(f"{dotted_path} is not boolean")
    return value


def normalized_action_l1(
    problem: OptimizationProblemV1,
    proposal: CandidateProposalV1,
) -> float:
    total = 0.0
    for domain in problem.decision_domains:
        width = domain.upper_bound - domain.lower_bound
        if width <= 0.0:
            raise ValueError("decision domain width must be positive")
        total += abs(proposal.decision_values[domain.variable_id] - domain.nominal_value) / width
    return total


def constraint_outcome(
    rule: ConstraintRuleV1,
    candidate_value: float,
    *,
    baseline_value: float | None = None,
) -> ConstraintOutcomeV1:
    if rule.operator == "le":
        margin = (rule.limit - candidate_value) / rule.normalization_scale
        passed = candidate_value <= rule.limit
    elif rule.operator == "ge":
        margin = (candidate_value - rule.limit) / rule.normalization_scale
        passed = candidate_value >= rule.limit
    else:
        margin = -abs(candidate_value - rule.limit) / rule.normalization_scale
        passed = math.isclose(candidate_value, rule.limit, rel_tol=0.0, abs_tol=1e-12)
        if passed:
            margin = 0.0
    return ConstraintOutcomeV1(
        constraint_id=rule.constraint_id,
        stage=rule.stage,
        metric_id=rule.metric_id,
        operator=rule.operator,
        limit=rule.limit,
        candidate_value=candidate_value,
        baseline_value=baseline_value,
        normalized_margin=margin,
        passed=passed,
    )


def validate_pair_bundles(
    pair: CompiledPair,
    baseline: SimulationRunBundleV1,
    candidate: SimulationRunBundleV1,
) -> None:
    assert_compiled_pair(pair)
    if pair.baseline.provider_request_fingerprint != baseline.provider_request_fingerprint:
        raise ValueError("baseline evidence does not match the compiled provider request")
    if pair.candidate.provider_request_fingerprint != candidate.provider_request_fingerprint:
        raise ValueError("candidate evidence does not match the compiled provider request")
    if (
        baseline.provider_id != candidate.provider_id
        or baseline.provider_id != pair.baseline.provider_id
    ):
        raise ValueError("paired evidence provider differs")
    baseline_versions = {
        key: value for key, value in baseline.versions.items() if key != "scenario_version"
    }
    candidate_versions = {
        key: value for key, value in candidate.versions.items() if key != "scenario_version"
    }
    if baseline_versions != candidate_versions:
        raise ValueError("paired evidence versions differ")
    stable_baseline = {
        key: value
        for key, value in baseline.source_fingerprints.items()
        if not key.startswith("runtime_")
    }
    stable_candidate = {
        key: value
        for key, value in candidate.source_fingerprints.items()
        if not key.startswith("runtime_")
    }
    if stable_baseline != stable_candidate:
        raise ValueError("paired evidence stable source fingerprints differ")


def error_evaluation(
    problem: OptimizationProblemV1,
    proposal: CandidateProposalV1,
    *,
    stage: EvaluationStage,
    status: EvaluationStatus,
    reason_code: str,
) -> CandidateEvaluationV1:
    if status not in {"invalid_request", "evaluation_error"}:
        raise ValueError("error evaluation status must be invalid_request or evaluation_error")
    return CandidateEvaluationV1(
        schema_version=RTO_SCHEMA_VERSION,
        evaluation_version="candidate-evaluation-v1",
        stage=stage,
        status=status,
        problem_ref=problem.ref,
        context_ref=problem.context_ref,
        proposal_ref=proposal.ref,
        pair_id=f"pair-{stage.lower()}-{proposal.fingerprint[:16]}",
        objective_metric_id=None,
        baseline_objective=None,
        candidate_objective=None,
        objective_delta=None,
        relative_improvement=None,
        metrics={},
        constraints=(),
        minimum_normalized_margin=None,
        normalized_action_l1=normalized_action_l1(problem, proposal),
        reason_codes=(reason_code,),
        baseline_evidence=None,
        candidate_evidence=None,
        claim_scope=CLAIM_SCOPE,
    )
