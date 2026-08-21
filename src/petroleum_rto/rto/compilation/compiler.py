"""Compile one canonical proposal into paired simulator requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ..contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateProposalV1,
    EvaluationStage,
    JsonValue,
    OperatingContextV1,
    OptimizationProblemV1,
    SimulationEvaluationRequestV1,
    canonical_fingerprint,
)
from ..contracts.common import thaw_json
from ..ports import ProviderRequestFactory


@dataclass(frozen=True)
class CompiledPair:
    baseline: SimulationEvaluationRequestV1
    candidate: SimulationEvaluationRequestV1


class CandidatePlanCompiler:
    """Apply problem bounds and delegate provider syntax to an injected factory."""

    def compile_pair(
        self,
        problem: OptimizationProblemV1,
        context: OperatingContextV1,
        proposal: CandidateProposalV1,
        *,
        stage: str,
        request_factory: ProviderRequestFactory,
    ) -> CompiledPair:
        if not isinstance(problem, OptimizationProblemV1):
            raise TypeError("problem must be an OptimizationProblemV1")
        if not isinstance(context, OperatingContextV1):
            raise TypeError("context must be an OperatingContextV1")
        if not isinstance(proposal, CandidateProposalV1):
            raise TypeError("proposal must be a CandidateProposalV1")
        if proposal.problem_ref != problem.ref or proposal.context_ref != context.ref:
            raise ValueError("proposal references another problem or operating context")
        if context.ref != problem.context_ref:
            raise ValueError("problem references another operating context")
        if stage not in {"M2", "M4"}:
            raise ValueError("stage must be M2 or M4")

        expected_ids = tuple(domain.variable_id for domain in problem.decision_domains)
        if tuple(sorted(proposal.decision_values)) != expected_ids:
            raise ValueError("proposal decision vector differs from the problem domain")
        for domain in problem.decision_domains:
            value = proposal.decision_values[domain.variable_id]
            if not domain.lower_bound <= value <= domain.upper_bound:
                raise ValueError(
                    f"proposal value is outside the local domain: {domain.variable_id}"
                )

        baseline_values = {
            variable_id: context.current_setpoints[variable_id] for variable_id in expected_ids
        }
        pair_id = f"pair-{stage.lower()}-{proposal.fingerprint[:16]}"
        if stage == "M2":
            baseline_provider = request_factory.build_m2_request(context, baseline_values)
            candidate_provider = request_factory.build_m2_request(context, proposal.decision_values)
        else:
            plan = problem.evaluation_plan
            baseline_provider = request_factory.build_m4_request(
                context,
                baseline_values,
                candidate=False,
                event_time_s=plan.m4_event_time_s,
                duration_s=plan.m4_duration_s,
                time_step_s=plan.m4_time_step_s,
            )
            candidate_provider = request_factory.build_m4_request(
                context,
                proposal.decision_values,
                candidate=True,
                event_time_s=plan.m4_event_time_s,
                duration_s=plan.m4_duration_s,
                time_step_s=plan.m4_time_step_s,
            )

        typed_stage = cast(EvaluationStage, stage)
        pair = CompiledPair(
            baseline=SimulationEvaluationRequestV1(
                schema_version=RTO_SCHEMA_VERSION,
                request_version="simulation-evaluation-request-v1",
                stage=typed_stage,
                pair_id=pair_id,
                pair_role="baseline",
                problem_ref=problem.ref,
                context_ref=context.ref,
                proposal_ref=None,
                provider_id=request_factory.provider_id,
                compiler_version=request_factory.compiler_version,
                provider_request=baseline_provider,
                claim_scope=CLAIM_SCOPE,
            ),
            candidate=SimulationEvaluationRequestV1(
                schema_version=RTO_SCHEMA_VERSION,
                request_version="simulation-evaluation-request-v1",
                stage=typed_stage,
                pair_id=pair_id,
                pair_role="candidate",
                problem_ref=problem.ref,
                context_ref=context.ref,
                proposal_ref=proposal.ref,
                provider_id=request_factory.provider_id,
                compiler_version=request_factory.compiler_version,
                provider_request=candidate_provider,
                claim_scope=CLAIM_SCOPE,
            ),
        )
        assert_compiled_pair(pair)
        return pair


def _plain(value: Mapping[str, JsonValue]) -> dict[str, object]:
    thawed = thaw_json(cast(JsonValue, value))
    if not isinstance(thawed, dict):  # pragma: no cover - mapping input
        raise TypeError("provider request did not thaw to an object")
    return cast(dict[str, object], thawed)


def assert_compiled_pair(pair: CompiledPair) -> None:
    """Reject any baseline/candidate drift outside the stage whitelist."""

    baseline = pair.baseline
    candidate = pair.candidate
    if (
        baseline.pair_id != candidate.pair_id
        or baseline.stage != candidate.stage
        or baseline.problem_ref != candidate.problem_ref
        or baseline.context_ref != candidate.context_ref
        or baseline.provider_id != candidate.provider_id
        or baseline.compiler_version != candidate.compiler_version
        or baseline.pair_role != "baseline"
        or candidate.pair_role != "candidate"
    ):
        raise ValueError("compiled pair identity differs")
    left = _plain(baseline.provider_request)
    right = _plain(candidate.provider_request)
    if baseline.stage == "M2":
        left_parameters = left.get("parameters")
        right_parameters = right.get("parameters")
        if not isinstance(left_parameters, dict) or not isinstance(right_parameters, dict):
            raise ValueError("M2 provider requests require parameter objects")
        if set(left_parameters) != set(right_parameters):
            raise ValueError("M2 parameter keys differ between baseline and candidate")
        allowed = {
            "operating.furnace_outlet_temperature_c",
            "operating.tower_top_pressure_mpa_g",
        }
        changed = {key for key in left_parameters if left_parameters[key] != right_parameters[key]}
        if not changed.issubset(allowed):
            raise ValueError("M2 pair differs outside the decision parameter whitelist")
        right["parameters"] = left_parameters
    else:
        left_scenario = left.get("scenario")
        right_scenario = right.get("scenario")
        if not isinstance(left_scenario, dict) or not isinstance(right_scenario, dict):
            raise ValueError("M4 provider requests require scenario objects")
        if left_scenario.get("events") != []:
            raise ValueError("M4 baseline must explicitly contain an empty event array")
        events = right_scenario.get("events")
        if not isinstance(events, list) or len(events) != 2:
            raise ValueError("M4 candidate must contain exactly two setpoint events")
        right_scenario["events"] = []
    if canonical_fingerprint(left) != canonical_fingerprint(right):
        raise ValueError("compiled pair differs outside the stage whitelist")
