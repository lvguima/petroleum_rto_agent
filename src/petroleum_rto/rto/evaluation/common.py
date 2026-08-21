"""Stage-neutral invariants shared by paired evaluators."""

from __future__ import annotations

import math

from ..capabilities.models import CapabilityCatalog
from ..compilation import CompiledPair
from ..compilation.compiler import assert_compiled_pair
from ..contracts.candidate import CandidateProposal, ConstraintOutcome
from ..contracts.context import OperatingContext
from ..contracts.problem import ConstraintRule, OptimizationProblem
from ..contracts.simulation import (
    SimulationEvaluationRequest,
    SimulationPreview,
    SimulationRunBundle,
)


def normalized_action_l1(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
) -> float:
    total = 0.0
    for domain in problem.decision_domains:
        width = domain.upper_bound - domain.lower_bound
        if width <= 0.0:
            raise ValueError("decision domain width must be positive")
        total += abs(proposal.decision_values[domain.variable_id] - domain.nominal_value) / width
    return total


def safe_normalized_action_l1(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
) -> float:
    try:
        return normalized_action_l1(problem, proposal)
    except (KeyError, TypeError, ValueError):
        return 0.0


def constraint_outcome(rule: ConstraintRule, value: float) -> ConstraintOutcome:
    if rule.operator == "le":
        margin = (rule.limit - value) / rule.normalization_scale
        passed = value <= rule.limit
    elif rule.operator == "ge":
        margin = (value - rule.limit) / rule.normalization_scale
        passed = value >= rule.limit
    else:
        passed = math.isclose(value, rule.limit, rel_tol=0.0, abs_tol=1e-12)
        margin = 0.0 if passed else -abs(value - rule.limit) / rule.normalization_scale
    return ConstraintOutcome(
        constraint_id=rule.constraint_id,
        metric_id=rule.metric_id,
        raw_value=value,
        limit=rule.limit,
        normalized_margin=margin,
        passed=passed,
    )


def validate_pair_bundles(
    pair: CompiledPair,
    problem: OptimizationProblem,
    catalog: CapabilityCatalog,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
) -> None:
    assert_compiled_pair(pair, problem, catalog)
    if pair.baseline.provider_request_fingerprint != baseline.provider_request_fingerprint:
        raise ValueError("baseline evidence does not match its compiled provider request")
    if pair.candidate.provider_request_fingerprint != candidate.provider_request_fingerprint:
        raise ValueError("candidate evidence does not match its compiled provider request")
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


def validate_preview(
    request: SimulationEvaluationRequest,
    preview: SimulationPreview,
    context: OperatingContext,
) -> None:
    if preview.simulation_request_ref != request.ref:
        raise ValueError("preview references another simulation request")
    if preview.provider_id != request.provider_id:
        raise ValueError("preview provider differs from the simulation request")
    if preview.base_object_fingerprints.get("model") != context.model_ref.fingerprint:
        raise ValueError("preview model fingerprint differs from the operating context")
    if preview.base_object_fingerprints.get("case") != context.case_ref.fingerprint:
        raise ValueError("preview case fingerprint differs from the operating context")


__all__ = [
    "constraint_outcome",
    "normalized_action_l1",
    "safe_normalized_action_l1",
    "validate_pair_bundles",
    "validate_preview",
]
