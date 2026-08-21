"""Compile unified candidate vectors into strictly paired simulator requests."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from ..capabilities.models import CapabilityCatalog
from ..contracts.candidate import CandidateProposal
from ..contracts.common import JsonValue, canonical_fingerprint, thaw_json
from ..contracts.context import OperatingContext
from ..contracts.models import CLAIM_SCOPE, RTO_SCHEMA_VERSION
from ..contracts.problem import OptimizationProblem
from ..contracts.simulation import SimulationEvaluationRequest, SimulationStage
from ..ports.unified import UnifiedProviderRequestFactory
from .compiler import CompiledPair


def _plain(value: Mapping[str, JsonValue]) -> dict[str, object]:
    thawed = thaw_json(cast(JsonValue, value))
    if not isinstance(thawed, dict):  # pragma: no cover - contract guarantees an object
        raise TypeError("provider request did not thaw to an object")
    return cast(dict[str, object], thawed)


class UnifiedCompilationError(ValueError):
    """Base class for classified failures in unified candidate compilation."""


class CandidateCompilationError(UnifiedCompilationError):
    """The candidate payload is incompatible with its immutable problem."""


class SystemCompilationError(UnifiedCompilationError):
    """Trusted problem, catalog, context, or provider-factory configuration drifted."""


class UnifiedCandidatePlanCompiler:
    """Apply catalog bindings while keeping provider syntax behind a port."""

    def __init__(self, catalog: CapabilityCatalog) -> None:
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("compiler requires a CapabilityCatalog")
        self._catalog = catalog

    def compile_pair(
        self,
        problem: OptimizationProblem,
        context: OperatingContext,
        proposal: CandidateProposal,
        *,
        stage: str,
        request_factory: UnifiedProviderRequestFactory,
    ) -> CompiledPair:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be an OptimizationProblem")
        if not isinstance(context, OperatingContext):
            raise TypeError("context must be an OperatingContext")
        if not isinstance(proposal, CandidateProposal):
            raise TypeError("proposal must be a CandidateProposal")
        if proposal.problem_ref != problem.ref or proposal.context_ref != context.ref:
            raise CandidateCompilationError(
                "proposal references another problem or operating context"
            )
        if context.ref != problem.context_ref:
            raise SystemCompilationError("problem references another operating context")
        expected_stages = {
            problem.evaluation_plan.static_stage,
            problem.evaluation_plan.dynamic_stage,
        }
        if stage not in expected_stages or stage not in {"M2", "M4"}:
            raise SystemCompilationError("stage differs from the problem evaluation plan")
        if stage == "M4" and not problem.evaluation_plan.dynamic_verification_required:
            raise SystemCompilationError("problem does not require dynamic verification")
        if problem.capability_catalog_ref != self._catalog.ref:
            raise SystemCompilationError("problem references another capability catalog")
        if proposal.output_kind != "steady-setpoint-vector":
            raise CandidateCompilationError("proposal output kind is unsupported")

        expected_ids = tuple(item.variable_id for item in problem.decision_domains)
        if tuple(sorted(proposal.decision_values)) != expected_ids:
            raise CandidateCompilationError(
                "proposal decision vector differs from the problem domain"
            )
        for domain in problem.decision_domains:
            value = proposal.decision_values[domain.variable_id]
            if not domain.lower_bound <= value <= domain.upper_bound:
                raise CandidateCompilationError(
                    f"proposal value is outside the local domain: {domain.variable_id}"
                )

        try:
            baseline_values = {
                variable_id: context.current_setpoints[variable_id] for variable_id in expected_ids
            }
            return self._compile_trusted_pair(
                problem,
                context,
                proposal,
                stage=stage,
                request_factory=request_factory,
                baseline_values=baseline_values,
            )
        except SystemCompilationError:
            raise
        except Exception as exc:
            raise SystemCompilationError(
                "trusted compilation configuration or provider request factory failed"
            ) from exc

    def _compile_trusted_pair(
        self,
        problem: OptimizationProblem,
        context: OperatingContext,
        proposal: CandidateProposal,
        *,
        stage: str,
        request_factory: UnifiedProviderRequestFactory,
        baseline_values: Mapping[str, float],
    ) -> CompiledPair:
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

        typed_stage = cast(SimulationStage, stage)
        pair = CompiledPair(
            baseline=SimulationEvaluationRequest(
                schema_version=RTO_SCHEMA_VERSION,
                request_version="simulation-evaluation-request",
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
            candidate=SimulationEvaluationRequest(
                schema_version=RTO_SCHEMA_VERSION,
                request_version="simulation-evaluation-request",
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
        assert_unified_compiled_pair(pair, problem, self._catalog)
        if stage == "M4":
            _assert_m4_absolute_ratios(
                pair,
                problem,
                context,
                proposal,
                self._catalog,
            )
        return pair


def assert_unified_compiled_pair(
    pair: CompiledPair,
    problem: OptimizationProblem,
    catalog: CapabilityCatalog,
) -> None:
    """Reject every provider-request difference outside cataloged decisions."""

    if problem.capability_catalog_ref != catalog.ref:
        raise ValueError("problem references another capability catalog")
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
        or baseline.problem_ref != problem.ref
        or baseline.context_ref != problem.context_ref
    ):
        raise ValueError("compiled pair identity differs")

    decision_by_id = {item.decision_id: item for item in catalog.decisions}
    selected = tuple(item.variable_id for item in problem.decision_domains)
    try:
        capabilities = tuple(decision_by_id[item] for item in selected)
    except KeyError as exc:  # pragma: no cover - ProblemBuilder closes this reference
        raise ValueError("problem contains an unknown decision capability") from exc
    left = _plain(baseline.provider_request)
    right = _plain(candidate.provider_request)

    if baseline.stage == "M2":
        if left.get("preset_id") != problem.evaluation_plan.m2_preset_id:
            raise ValueError("M2 provider request differs from the planned preset")
        left_parameters = left.get("parameters")
        right_parameters = right.get("parameters")
        if not isinstance(left_parameters, dict) or not isinstance(right_parameters, dict):
            raise TypeError("M2 provider requests require parameter objects")
        if set(left_parameters) != set(right_parameters):
            raise ValueError("M2 parameter keys differ between baseline and candidate")
        allowed = {item.m2_parameter_path for item in capabilities}
        if None in allowed:
            raise ValueError("selected decision lacks an M2 compiler binding")
        changed = {key for key in left_parameters if left_parameters[key] != right_parameters[key]}
        if not changed.issubset(allowed):
            raise ValueError("M2 pair differs outside selected decision bindings")
        right["parameters"] = left_parameters
    elif baseline.stage == "M4":
        if left.get("preset_id") != problem.evaluation_plan.m4_preset_id:
            raise ValueError("M4 provider request differs from the planned preset")
        left_scenario = left.get("scenario")
        right_scenario = right.get("scenario")
        if not isinstance(left_scenario, dict) or not isinstance(right_scenario, dict):
            raise TypeError("M4 provider requests require scenario objects")
        plan = problem.evaluation_plan
        expected_timing = (plan.m4_duration_s, plan.m4_time_step_s)
        if (
            left_scenario.get("duration_s"),
            left_scenario.get("time_step_s"),
        ) != expected_timing or (
            right_scenario.get("duration_s"),
            right_scenario.get("time_step_s"),
        ) != expected_timing:
            raise ValueError("M4 scenario timing differs from the evaluation plan")
        if left_scenario.get("events") != []:
            raise ValueError("M4 baseline must explicitly contain an empty event array")
        events = right_scenario.get("events")
        if not isinstance(events, list) or len(events) != len(capabilities):
            raise ValueError("M4 candidate event count differs from selected decisions")
        expected_targets = []
        for capability in capabilities:
            if capability.m4_loop_id is None:
                raise ValueError("selected decision lacks an M4 compiler binding")
            expected_targets.append(f"{capability.m4_loop_id}.setpoint_ratio")
        actual_targets: list[object] = []
        for event in events:
            if not isinstance(event, dict):
                raise TypeError("M4 candidate events must be objects")
            time_s = event.get("time_s")
            value = event.get("value")
            if (
                isinstance(time_s, bool)
                or not isinstance(time_s, (int, float))
                or not math.isclose(float(time_s), plan.m4_event_time_s, rel_tol=0.0, abs_tol=1e-12)
                or event.get("value_basis") != "setpoint_ratio"
                or event.get("duration_s") is not None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError("M4 candidate setpoint event is invalid")
            actual_targets.append(event.get("target"))
        if actual_targets != expected_targets:
            raise ValueError("M4 candidate event targets differ from selected decisions")
        right_scenario["events"] = []
    else:  # pragma: no cover - SimulationStage closes this branch
        raise ValueError("unsupported compiled stage")
    if canonical_fingerprint(left) != canonical_fingerprint(right):
        raise ValueError("compiled pair differs outside the stage whitelist")


def _assert_m4_absolute_ratios(
    pair: CompiledPair,
    problem: OptimizationProblem,
    context: OperatingContext,
    proposal: CandidateProposal,
    catalog: CapabilityCatalog,
) -> None:
    payload = _plain(pair.candidate.provider_request)
    scenario = payload.get("scenario")
    if not isinstance(scenario, dict) or not isinstance(scenario.get("events"), list):
        raise TypeError("M4 candidate provider request lacks events")
    events = cast(list[object], scenario["events"])
    capabilities = {item.decision_id: item for item in catalog.decisions}
    by_target: dict[str, Mapping[str, object]] = {}
    for raw in events:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("target"), str):
            raise TypeError("M4 event target is invalid")
        by_target[cast(str, raw["target"])] = cast(Mapping[str, object], raw)
    for domain in problem.decision_domains:
        capability = capabilities[domain.variable_id]
        if capability.m4_loop_id is None:
            raise ValueError("selected decision lacks an M4 compiler binding")
        target = f"{capability.m4_loop_id}.setpoint_ratio"
        event = by_target.get(target)
        if event is None:
            raise ValueError("M4 candidate event is missing a selected decision")
        raw_value = event.get("value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TypeError("M4 setpoint ratio must be numeric")
        baseline_value = context.current_setpoints[domain.variable_id]
        expected = proposal.decision_values[domain.variable_id] / baseline_value
        if not math.isclose(float(raw_value), expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("M4 setpoint ratio is not based on canonical absolute values")


__all__ = [
    "CandidateCompilationError",
    "SystemCompilationError",
    "UnifiedCandidatePlanCompiler",
    "UnifiedCompilationError",
    "assert_unified_compiled_pair",
]
