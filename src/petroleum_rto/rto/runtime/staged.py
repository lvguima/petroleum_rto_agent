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
from ..contracts.common import as_mapping, canonical_fingerprint, strict_keys
from ..contracts.context import OperatingContext
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE, OptimizationProblem
from ..intent import IntentResolver, OptimizationIntent
from ..orchestration.models import (
    OFFLINE_WORKFLOW_SCHEMA_ID,
    OFFLINE_WORKFLOW_SCHEMA_VERSION,
    CapabilityBundleSnapshot,
)
from ..orchestration.service import OfflineRtoOrchestrator, OfflineRtoRunRecord, StaticRtoRunRecord
from ..problem import ProblemBuilder
from ..progress import RtoProgress, RtoProgressCallback
from .chat_summary import build_chat_operating_status, build_optimization_run_summary


class OptimizationPreparationError(ValueError):
    """Safe, structured business-field failures for the public preparation boundary."""

    def __init__(self, issues: list[dict[str, object]]) -> None:
        super().__init__("optimization preparation rejected")
        self.issues = issues


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


def dump_prepared_optimization(prepared: PreparedOptimization) -> dict[str, object]:
    """Encode the complete bound inputs; no live configuration or opaque objects."""
    snapshot = CapabilityBundleSnapshot(
        schema_id=OFFLINE_WORKFLOW_SCHEMA_ID,
        schema_version=OFFLINE_WORKFLOW_SCHEMA_VERSION,
        snapshot_version="capability-bundle-snapshot",
        bundle=prepared.bundle,
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )
    payload: dict[str, object] = {
        "schema_id": "prepared-optimization",
        "schema_version": "1.0.0",
        "capability_bundle": snapshot.as_dict(),
        "intent": prepared.intent.as_dict(),
        "context": prepared.context.as_dict(),
        "problem": prepared.problem.as_dict(),
    }
    return {**payload, "prepared_fingerprint": canonical_fingerprint(payload)}


def load_prepared_optimization(value: Mapping[str, object]) -> PreparedOptimization:
    """Strictly rebuild a saved problem from its own immutable configuration.

    Fingerprints detect corruption, not malicious replacement of an entire record.
    This boundary performs neither simulation nor reads of current configuration.
    """
    raw = as_mapping(value, context="prepared optimization")
    strict_keys(
        raw,
        required={
            "schema_id",
            "schema_version",
            "capability_bundle",
            "intent",
            "context",
            "problem",
            "prepared_fingerprint",
        },
        context="prepared optimization",
    )
    if raw["schema_id"] != "prepared-optimization" or raw["schema_version"] != "1.0.0":
        raise ValueError("prepared optimization schema or version is unsupported")
    payload = {key: item for key, item in raw.items() if key != "prepared_fingerprint"}
    if raw["prepared_fingerprint"] != canonical_fingerprint(payload):
        raise ValueError("prepared_fingerprint differs from saved problem content")
    snapshot = CapabilityBundleSnapshot.from_mapping(
        as_mapping(raw["capability_bundle"], context="capability_bundle")
    )
    intent = OptimizationIntent.from_mapping(as_mapping(raw["intent"], context="intent"))
    context = OperatingContext.from_mapping(as_mapping(raw["context"], context="context"))
    problem = OptimizationProblem.from_mapping(as_mapping(raw["problem"], context="problem"))
    if ProblemBuilder().build(snapshot.bundle, intent, context) != problem:
        raise ValueError("saved prepared problem differs from deterministic reconstruction")
    prepared = PreparedOptimization(snapshot.bundle, intent, context, problem)
    # Contract readers also accept some source-file forms without fingerprints;
    # a persisted preparation must retain the complete versioned writer format.
    if dump_prepared_optimization(prepared) != raw:
        raise ValueError("saved prepared optimization is incomplete or has unsupported metadata")
    return prepared


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
    if constraints:
        raise OptimizationPreparationError(
            [
                {
                    "code": "unsupported-business-constraints",
                    "json_pointer": "/constraints",
                    "message": "系统约束自动加入；当前不支持额外业务约束，不能通过删掉用户要求来绕过。",
                }
            ]
        )
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
    route = BundleCapabilityView(bundle).route_for_objective_count(len(intent.objectives))
    if route is not None and max_candidates > route.top_k:
        raise OptimizationPreparationError(
            [
                {
                    "code": "result-count-out-of-range",
                    "json_pointer": "/max_candidates",
                    "message": "max_candidates是返回方案总数，不是M2搜索预算；保留目标与变量，仅修正返回数量。",
                    "minimum": 1,
                    "maximum": route.top_k,
                }
            ]
        )
    resolution = IntentResolver().resolve(intent, BundleCapabilityView(bundle))
    if resolution.status != "resolved" or resolution.resolved_intent is None:
        raise OptimizationPreparationError([issue.as_dict() for issue in resolution.issues])
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
    prepared: PreparedOptimization,
    *,
    run_root: Path,
    on_progress: RtoProgressCallback | None = None,
) -> dict[str, object]:
    try:
        record = _orchestrator().solve_static(
            prepared.bundle,
            prepared.intent,
            prepared.context,
            prepared.problem,
            run_root=run_root,
            on_progress=on_progress,
        )
    except (Exception, KeyboardInterrupt):
        if on_progress is not None:
            on_progress(RtoProgress("m2", "error"))
        raise
    return _static_receipt(prepared, record)


def read_prepared_static(prepared: PreparedOptimization, *, run_root: Path) -> dict[str, object]:
    """Return a strictly reloaded M2 receipt; missing evidence cannot start a solve."""
    record = _orchestrator().read_static(
        prepared.bundle, prepared.intent, prepared.context, prepared.problem, run_root=run_root
    )
    return _static_receipt(prepared, record)


def _static_receipt(
    prepared: PreparedOptimization, record: StaticRtoRunRecord
) -> dict[str, object]:
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


def read_prepared_result(prepared: PreparedOptimization, *, run_root: Path) -> dict[str, object]:
    """Return a strictly reloaded final receipt without executing pending stages."""
    record = _orchestrator().read_completed(
        prepared.bundle, prepared.intent, prepared.context, prepared.problem, run_root=run_root
    )
    return _result_receipt(record)


def _result_receipt(record: OfflineRtoRunRecord) -> dict[str, object]:
    return {
        "status": "complete",
        "workflow_id": record.request.workflow_id,
        "result_source": f"{record.request.workflow_id}/result.json",
        "result": build_optimization_run_summary(record).as_dict(),
        "physical_m2_executions": record.physical_m2_executions,
        "physical_m4_executions": record.physical_m4_executions,
    }


def verify_prepared_optimization(
    prepared: PreparedOptimization,
    *,
    static_ref: str,
    run_root: Path,
    on_progress: RtoProgressCallback | None = None,
) -> dict[str, object]:
    def m4_progress(event: RtoProgress) -> None:
        # M2 is only reloaded here; verify reports its own M4 stage exactly once.
        if on_progress is not None and event.stage == "m4":
            on_progress(event)

    try:
        orchestrator = _orchestrator()
        static = orchestrator.read_static(
            prepared.bundle, prepared.intent, prepared.context, prepared.problem, run_root=run_root
        )
        if static.static_selection.fingerprint != static_ref:
            raise ValueError("static reference differs from the bound checkpoint")
        record = orchestrator.run(
            prepared.bundle,
            prepared.intent,
            prepared.context,
            prepared.problem,
            run_root=run_root,
            on_progress=m4_progress if on_progress is not None else None,
        )
        return _result_receipt(record)
    except (Exception, KeyboardInterrupt):
        if on_progress is not None:
            on_progress(RtoProgress("m4", "error"))
        raise
