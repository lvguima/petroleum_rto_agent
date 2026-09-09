"""Snapshot-bound preparation and explicit M2/M4 runtime entry points.

Uses the existing v4 checkpoint layout unchanged. Static checkpoints are not
completed runs; the new stage receipt is a separate versioned projection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..adapters import CduM7RequestFactory, CduM7Simulator
from ..capabilities import BundleCapabilityView, CapabilityBundle, load_capability_bundle
from ..contracts.context import OperatingContext
from ..contracts.problem import OptimizationProblem
from ..intent import IntentResolver, OptimizationIntent
from ..orchestration.service import OfflineRtoOrchestrator
from ..problem import ProblemBuilder
from .chat_summary import build_chat_operating_status, build_optimization_run_summary


@dataclass(frozen=True)
class PreparedOptimization:
    bundle: CapabilityBundle
    intent: OptimizationIntent
    context: OperatingContext
    problem: OptimizationProblem

    def summary(self) -> dict[str, object]:
        return {
            "problem_ref": self.problem.ref.as_dict(),
            "snapshot_ref": self.context.fingerprint,
            "intent": self.intent.as_dict(),
            "operating_context": build_chat_operating_status(self.context),
            "decision_domains": [item.as_dict() for item in self.problem.decision_domains],
            "hard_constraints": [item.as_dict() for item in self.problem.hard_constraints],
            "publishability_constraints": [
                item.as_dict() for item in self.problem.publishability_constraints
            ],
            "execution_scope": "point: 完整M2静态搜索 + 全部入围候选M4动态复核",
            "dynamic_shortlist_size": self.problem.evaluation_plan.dynamic_shortlist_size,
        }


def prepare_optimization(
    *,
    repo_root: Path,
    context: OperatingContext,
    objectives: Sequence[Mapping[str, object]],
    decision_variables: Sequence[str],
    constraints: Sequence[str] = (),
    max_candidates: int = 1,
) -> PreparedOptimization:
    """Resolve business fields and construct a problem without simulation."""
    fields = {
        "objectives": [dict(item, priority=index) for index, item in enumerate(objectives, 1)],
        "decision_variables": list(decision_variables),
        "constraints": list(constraints),
        "preference": {
            "method": "single-objective" if len(objectives) == 1 else "lexicographic",
            "objective_order": [item["metric_id"] for item in objectives],
        },
        "result_request": {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": max_candidates > 1,
            "max_candidates": max_candidates,
        },
        "ambiguities": [],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fields, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    intent = OptimizationIntent.from_mapping(
        {
            "schema_id": "optimization-intent",
            "schema_version": "1.0.0",
            "intent_id": "agent-" + fingerprint[:16],
            **fields,
        }
    )
    bundle = load_capability_bundle(repo_root)
    resolution = IntentResolver().resolve(intent, BundleCapabilityView(bundle))
    if resolution.status != "resolved" or resolution.resolved_intent is None:
        raise ValueError("business requirements cannot be resolved against current capabilities")
    intent = resolution.resolved_intent
    return PreparedOptimization(
        bundle, intent, context, ProblemBuilder().build(bundle, intent, context)
    )


def render_confirmation(prepared: PreparedOptimization, version: int) -> str:
    # Use the bound bundle, not mutable configuration, for display labels.
    from ..capabilities import build_public_capability_manifest

    manifest = build_public_capability_manifest(prepared.bundle)
    objectives = {str(row["metric_id"]): str(row["business_name"]) for row in manifest.objectives}
    decisions = {str(row["decision_id"]): str(row["business_name"]) for row in manifest.decisions}
    guardrails = {
        str(row["guardrail_id"]): str(row["business_name"]) for row in manifest.guardrails
    }
    goals = "；".join(str(objectives[item.metric_id]) for item in prepared.intent.objectives)
    variables = "、".join(str(decisions[item]) for item in prepared.intent.decision_variables)
    status = build_chat_operating_status(prepared.context)
    ranges = []
    for domain in prepared.problem.decision_domains:
        low, high, unit = domain.lower_bound, domain.upper_bound, domain.canonical_unit
        if unit == "K":
            low, high, unit = low - 273.15, high - 273.15, "°C"
        elif unit == "Pa(a)":
            low, high, unit = low / 1_000_000, high / 1_000_000, "MPa(a)"
        ranges.append(f"{decisions[domain.variable_id]}: {low:g}–{high:g} {unit}")
    domains = "；".join(ranges)
    operators = {"eq": "=", "le": "≤", "ge": "≥"}
    constraints = "; ".join(
        f"{guardrails.get(c.constraint_id, c.constraint_id)} {operators[c.operator]} {c.limit:g} {c.unit}"
        for c in prepared.problem.hard_constraints
    )
    return (
        f"请确认离线优化方案（第{version}版）：\n优化目标（按顺序）：{goals}\n"
        f"允许调整：{variables}\n范围：{domains}\n约束：{constraints}\n"
        f"工况时间：{status['data_timestamp']}；配置离线工况，{status['data_quality']}\n"
        "计算将使用此方案绑定的工况快照。\n"
        f"输出：最多{prepared.intent.result_request.max_candidates}个稳态设定点方案；"
        "通过完整静态搜索及全部入围候选动态复核后才能给出最终结果。\n"
        "本次仅准备问题，未运行仿真。可以修改、取消；下一条消息回复确认或/confirm开始执行。"
    )


def _orchestrator() -> OfflineRtoOrchestrator:
    return OfflineRtoOrchestrator(CduM7RequestFactory(), lambda root: CduM7Simulator(root))


def solve_prepared_optimization(
    prepared: PreparedOptimization, *, run_root: Path
) -> dict[str, object]:
    record = _orchestrator().solve_static(
        prepared.bundle, prepared.intent, prepared.context, prepared.problem, run_root=run_root
    )
    return {
        "schema_id": "optimization-static-receipt",
        "schema_version": "1.0.0",
        "status": "static_complete",
        "workflow_id": record.request.workflow_id,
        "static_ref": record.static_selection.fingerprint,
        "problem_ref": prepared.problem.ref.as_dict(),
        "snapshot_ref": prepared.context.fingerprint,
        "selection": record.static_selection.as_dict(),
        "candidates": [item.as_dict() for item in record.solver_execution.result.proposals],
        "m2_evaluations": [
            {
                "proposal_ref": item.proposal_ref.as_dict(),
                "status": item.status,
                "pair_id": item.pair_id,
                "objectives": [
                    {
                        "metric_id": value.metric_id,
                        "unit": value.unit,
                        "baseline_value": value.baseline_value,
                        "candidate_value": value.candidate_value,
                    }
                    for value in item.objective_outcomes
                ],
                "constraints": [value.as_dict() for value in item.constraints],
                "reason_codes": list(item.reason_codes),
            }
            for item in record.solver_execution.result.evaluations
        ],
        "physical_m2_executions": record.physical_m2_executions,
        "final_result_included": False,
        "message": "本次仅返回静态阶段；请用verify取得包含完整动态复核的最终结果。",
    }


def verify_prepared_optimization(
    prepared: PreparedOptimization, *, static_ref: str, run_root: Path
) -> dict[str, object]:
    # Require an existing M2 checkpoint; a bare verify call must never initiate a solve.
    orchestrator = _orchestrator()
    request = orchestrator._request_for(
        prepared.bundle, prepared.intent, prepared.context, prepared.problem, "point"
    )
    run_dir = run_root.resolve() / request.workflow_id
    if not all(
        (run_dir / name).is_file() and not (run_dir / name).is_symlink()
        for name in (
            "request.json",
            "intent.json",
            "context.json",
            "capability_bundle.json",
            "problem.json",
            "solver_route.json",
            "static_solve.json",
            "static_selection.json",
        )
    ):
        raise ValueError("static checkpoint does not exist")
    static = orchestrator.solve_static(
        prepared.bundle, prepared.intent, prepared.context, prepared.problem, run_root=run_root
    )
    if static.static_selection.fingerprint != static_ref:
        raise ValueError("static reference differs from the bound checkpoint")
    record = orchestrator.run(
        prepared.bundle, prepared.intent, prepared.context, prepared.problem, run_root=run_root
    )
    return {
        "status": "complete",
        "workflow_id": record.request.workflow_id,
        "result_source": f"{record.request.workflow_id}/result.json",
        "result": build_optimization_run_summary(record).as_dict(),
        "physical_m2_executions": record.physical_m2_executions,
        "physical_m4_executions": record.physical_m4_executions,
    }
