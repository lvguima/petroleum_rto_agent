"""Compile RTO V2 proposals into the unchanged M7 simulation boundary."""

from __future__ import annotations

from typing import cast

from ..contracts.models import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    EvaluationStage,
    OperatingContextV1,
    SimulationEvaluationRequestV1,
)
from ..contracts.multiobjective import OptimizationProblemV2
from ..contracts.results_v2 import CandidateProposalV2
from ..ports import ProviderRequestFactory
from .compiler import CompiledPair, assert_compiled_pair


class MultiObjectiveCandidatePlanCompiler:
    """Map one V2 steady vector to paired M2 or M4 provider requests."""

    def compile_pair(
        self,
        problem: OptimizationProblemV2,
        context: OperatingContextV1,
        proposal: CandidateProposalV2,
        *,
        stage: str,
        request_factory: ProviderRequestFactory,
    ) -> CompiledPair:
        if not isinstance(problem, OptimizationProblemV2):
            raise TypeError("problem must be an OptimizationProblemV2")
        if not isinstance(context, OperatingContextV1):
            raise TypeError("context must be an OperatingContextV1")
        if not isinstance(proposal, CandidateProposalV2):
            raise TypeError("proposal must be a CandidateProposalV2")
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
        pair_id = f"pair-v2-{stage.lower()}-{proposal.fingerprint[:16]}"
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
