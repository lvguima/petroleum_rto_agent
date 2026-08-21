"""RTO V2 Pareto preference ordering, M4 verification, and final selection."""

from __future__ import annotations

from typing import Protocol

from ..contracts.common import canonical_fingerprint
from ..contracts.models import CLAIM_SCOPE, ContractRef
from ..contracts.multiobjective import (
    RTO_V2_SCHEMA_VERSION,
    OptimizationProblemV2,
    PreferenceProfileV2,
    PublishabilityProfileV2,
)
from ..contracts.results_v2 import (
    CandidateEvaluationV2,
    CandidateProposalV2,
    ParetoSearchResultV2,
)
from ..contracts.selection_v2 import (
    DynamicVerificationV2,
    OptimizationResultStatusV2,
    OptimizationResultV2,
    PreferenceSelectionStatusV2,
    PreferenceSelectionV2,
)


class DynamicEvaluatorPortV2(Protocol):
    def evaluate(self, proposal: CandidateProposalV2) -> CandidateEvaluationV2: ...


def preference_profile_ref(profile: PreferenceProfileV2) -> ContractRef:
    return ContractRef(profile.profile_id, canonical_fingerprint(profile.as_dict()))


def publishability_profile_ref(profile: PublishabilityProfileV2) -> ContractRef:
    return ContractRef(profile.profile_id, canonical_fingerprint(profile.as_dict()))


class ParetoPreferenceSelector:
    """Order only first-front representatives using the explicit lexicographic profile."""

    def select(
        self,
        problem: OptimizationProblemV2,
        search: ParetoSearchResultV2,
        profile: PreferenceProfileV2,
    ) -> PreferenceSelectionV2:
        if search.problem_ref != problem.ref or search.context_ref != problem.context_ref:
            raise ValueError("Pareto search references another problem or context")
        if profile.profile_id != problem.preference_profile_id:
            raise ValueError("preference profile differs from the optimization problem")
        if profile.objective_order != tuple(item.metric_id for item in problem.objectives):
            raise ValueError("preference order differs from problem objectives")
        if search.status != "success":
            status: PreferenceSelectionStatusV2 = (
                "no_static_feasible"
                if search.status == "no_static_feasible"
                else "evaluation_error"
            )
            return PreferenceSelectionV2(
                schema_version=RTO_V2_SCHEMA_VERSION,
                selection_version="pareto-preference-selection-v2",
                status=status,
                problem_ref=problem.ref,
                context_ref=problem.context_ref,
                pareto_search_ref=search.ref,
                preference_profile_ref=preference_profile_ref(profile),
                ranked_pareto_refs=(),
                shortlist_refs=(),
                termination_reason=(
                    "no-static-feasible"
                    if status == "no_static_feasible"
                    else "static-evaluation-error"
                ),
                claim_scope=CLAIM_SCOPE,
            )
        front = tuple(search.evaluation_by_ref(ref) for ref in search.pareto_refs)
        ordered = tuple(sorted(front, key=lambda item: self._sort_key(problem, item)))
        proposal_refs = tuple(item.proposal_ref for item in ordered)
        return PreferenceSelectionV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            selection_version="pareto-preference-selection-v2",
            status="success",
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            pareto_search_ref=search.ref,
            preference_profile_ref=preference_profile_ref(profile),
            ranked_pareto_refs=proposal_refs,
            shortlist_refs=proposal_refs[: problem.evaluation_plan.top_k],
            termination_reason="pareto-preference-ranked",
            claim_scope=CLAIM_SCOPE,
        )

    @staticmethod
    def _sort_key(
        problem: OptimizationProblemV2,
        evaluation: CandidateEvaluationV2,
    ) -> tuple[object, ...]:
        if evaluation.minimum_normalized_margin is None:
            raise ValueError("Pareto evaluation lacks a hard-constraint margin")
        objectives = tuple(
            (
                evaluation.outcome_by_id(spec.metric_id).candidate_value
                if spec.sense == "minimize"
                else -evaluation.outcome_by_id(spec.metric_id).candidate_value
            )
            for spec in problem.objectives
        )
        return (
            *objectives,
            -evaluation.minimum_normalized_margin,
            evaluation.normalized_action_l1,
            evaluation.proposal_ref.fingerprint,
        )


class MultiObjectiveDynamicFinalSelector:
    """Verify every Pareto Top-5 candidate, select the first feasible, then publish-gate."""

    def select(
        self,
        problem: OptimizationProblemV2,
        search: ParetoSearchResultV2,
        preference: PreferenceSelectionV2,
        publishability: PublishabilityProfileV2,
        evaluator: DynamicEvaluatorPortV2,
    ) -> tuple[DynamicVerificationV2 | None, OptimizationResultV2]:
        if (
            search.problem_ref != problem.ref
            or preference.problem_ref != problem.ref
            or search.context_ref != problem.context_ref
            or preference.context_ref != problem.context_ref
            or preference.pareto_search_ref != search.ref
        ):
            raise ValueError("selection inputs reference another problem or context")
        publish_ref = publishability_profile_ref(publishability)
        if publishability.profile_id != problem.publishability_profile_id:
            raise ValueError("publishability profile differs from the problem")
        if preference.status != "success":
            status: OptimizationResultStatusV2 = (
                "no_static_feasible"
                if preference.status == "no_static_feasible"
                else "evaluation_error"
            )
            return None, self._result(
                problem,
                search,
                preference,
                None,
                publish_ref,
                status=status,
                selected_static=None,
                selected_dynamic=None,
                reason=(
                    "no-static-feasible"
                    if status == "no_static_feasible"
                    else "static-selection-error"
                ),
            )

        proposals = {item.ref: item for item in search.proposals}
        static_by_proposal = {item.proposal_ref: item for item in search.evaluations}
        if any(
            ref not in proposals or ref not in static_by_proposal
            for ref in preference.shortlist_refs
        ):
            raise ValueError("preference shortlist references an unknown Pareto proposal")
        dynamic = tuple(evaluator.evaluate(proposals[ref]) for ref in preference.shortlist_refs)
        for proposal_ref, evaluation in zip(preference.shortlist_refs, dynamic, strict=True):
            if evaluation.stage != "M4" or evaluation.proposal_ref != proposal_ref:
                raise ValueError("dynamic evaluation differs from the Pareto shortlist")
        if any(item.status in {"invalid_request", "evaluation_error"} for item in dynamic):
            verification = DynamicVerificationV2(
                schema_version=RTO_V2_SCHEMA_VERSION,
                verification_version="dynamic-verification-v2",
                status="evaluation_error",
                problem_ref=problem.ref,
                context_ref=problem.context_ref,
                selection_ref=preference.ref,
                shortlist_refs=preference.shortlist_refs,
                evaluations=dynamic,
                selected_static_evaluation_ref=None,
                selected_dynamic_evaluation_ref=None,
                termination_reason="dynamic-evaluation-error",
                claim_scope=CLAIM_SCOPE,
            )
            return verification, self._result(
                problem,
                search,
                preference,
                verification,
                publish_ref,
                status="evaluation_error",
                selected_static=None,
                selected_dynamic=None,
                reason="dynamic-evaluation-error",
            )

        selected_static: CandidateEvaluationV2 | None = None
        selected_dynamic: CandidateEvaluationV2 | None = None
        for proposal_ref, dynamic_item in zip(preference.shortlist_refs, dynamic, strict=True):
            if dynamic_item.status == "feasible":
                selected_static = static_by_proposal[proposal_ref]
                selected_dynamic = dynamic_item
                break
        if selected_static is None or selected_dynamic is None:
            verification = DynamicVerificationV2(
                schema_version=RTO_V2_SCHEMA_VERSION,
                verification_version="dynamic-verification-v2",
                status="pareto_shortlist_dynamic_failed",
                problem_ref=problem.ref,
                context_ref=problem.context_ref,
                selection_ref=preference.ref,
                shortlist_refs=preference.shortlist_refs,
                evaluations=dynamic,
                selected_static_evaluation_ref=None,
                selected_dynamic_evaluation_ref=None,
                termination_reason="pareto-shortlist-dynamic-failed",
                claim_scope=CLAIM_SCOPE,
            )
            return verification, self._result(
                problem,
                search,
                preference,
                verification,
                publish_ref,
                status="pareto_shortlist_dynamic_failed",
                selected_static=None,
                selected_dynamic=None,
                reason="pareto-shortlist-dynamic-failed",
            )

        verification = DynamicVerificationV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            verification_version="dynamic-verification-v2",
            status="success",
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            selection_ref=preference.ref,
            shortlist_refs=preference.shortlist_refs,
            evaluations=dynamic,
            selected_static_evaluation_ref=selected_static.ref,
            selected_dynamic_evaluation_ref=selected_dynamic.ref,
            termination_reason="first-dynamic-feasible-selected",
            claim_scope=CLAIM_SCOPE,
        )
        publish_outcome = selected_static.outcome_by_id(publishability.metric_id)
        improvement = publish_outcome.relative_directional_improvement
        publishable = improvement is not None and improvement >= publishability.limit
        return verification, self._result(
            problem,
            search,
            preference,
            verification,
            publish_ref,
            status="success" if publishable else "feasible_not_publishable",
            selected_static=selected_static,
            selected_dynamic=selected_dynamic,
            reason=("selected-publishable" if publishable else "selected-below-publish-gate"),
        )

    @staticmethod
    def _result(
        problem: OptimizationProblemV2,
        search: ParetoSearchResultV2,
        preference: PreferenceSelectionV2,
        verification: DynamicVerificationV2 | None,
        publishability_ref: ContractRef,
        *,
        status: OptimizationResultStatusV2,
        selected_static: CandidateEvaluationV2 | None,
        selected_dynamic: CandidateEvaluationV2 | None,
        reason: str,
    ) -> OptimizationResultV2:
        return OptimizationResultV2(
            schema_version=RTO_V2_SCHEMA_VERSION,
            result_version="optimization-result-v2",
            status=status,
            problem_ref=problem.ref,
            context_ref=problem.context_ref,
            pareto_search_ref=search.ref,
            preference_selection_ref=preference.ref,
            dynamic_verification_ref=(None if verification is None else verification.ref),
            pareto_refs=search.pareto_refs,
            selected_proposal_ref=(
                None if selected_static is None else selected_static.proposal_ref
            ),
            selected_static_evaluation_ref=(
                None if selected_static is None else selected_static.ref
            ),
            selected_dynamic_evaluation_ref=(
                None if selected_dynamic is None else selected_dynamic.ref
            ),
            selected_objectives=(
                () if selected_static is None else selected_static.objective_outcomes
            ),
            publishability_profile_ref=publishability_ref,
            publishable=status == "success",
            termination_reason=reason,
            claim_scope=CLAIM_SCOPE,
        )
