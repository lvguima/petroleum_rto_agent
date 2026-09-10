"""Native domain tools and process-local, version-bound execution eligibility."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from petroleum_rto.rto import load_operating_context
from petroleum_rto.rto.runtime import (
    OfflineInspectionError,
    OptimizationPreparationError,
    PreparedOptimization,
    build_chat_operating_status,
    build_optimization_run_summary,
    capabilities,
    inspect_offline,
    prepare_optimization,
    render_confirmation,
    solve_prepared_optimization,
    verify_prepared_optimization,
)

CONFIRMATION_INPUTS = ("/confirm", "确认", "确认执行")
TOOL_CONTRACT_VERSION = "2.0.0"
CONFIRMATION_RULE = (
    "仅整条输入为"
    + "、".join(f"“{text}”" for text in CONFIRMATION_INPUTS)
    + "时可授权计算（只忽略首尾空白，不忽略标点或附加条件）。"
)


class ConfirmationInputError(ValueError):
    """The actual user input cannot grant execution permission."""


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResultArguments(NoArguments):
    workflow_id: str | None = Field(default=None, pattern=r"^offline-rto-[0-9a-f]{16}$")


class ObjectiveArguments(NoArguments):
    metric_id: str
    sense: Literal["minimize", "maximize"]


class PrepareArguments(NoArguments):
    snapshot_ref: str = Field(
        description="read_operating_context返回的snapshot_ref，不是context_id。"
    )
    objectives: list[ObjectiveArguments] = Field(min_length=1)
    decision_variables: list[str] = Field(
        min_length=1, description="全部允许调整的available变量；可选子集，不能加入deferred变量。"
    )
    max_candidates: int = Field(
        default=1,
        ge=1,
        description="返回方案总数（含推荐），默认1；上限见get_plant_info的preparation规则，不是搜索预算。",
    )
    previous_plan_ref: str | None = None


class PlanArguments(NoArguments):
    plan_ref: str


class ManageArguments(PlanArguments):
    action: Literal["confirm", "cancel", "keep"]
    user_turn_id: int
    user_message: str


class VerifyArguments(PlanArguments):
    static_ref: str


class StrictStructuredTool(StructuredTool):
    def _to_args_and_kwargs(
        self, tool_input: str | dict[str, Any], tool_call_id: str | None
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        # langchain-core 1.6.2 skips empty-schema validation.
        if self.args_schema is NoArguments:
            NoArguments.model_validate(tool_input)
        return super()._to_args_and_kwargs(tool_input, tool_call_id)


@dataclass
class PendingPlan:
    ref: str
    prepared: PreparedOptimization
    displayed_turn: int | None = None
    eligible: bool = False
    authorized: bool = False
    static: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class AgentDomainTools:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.snapshots: dict[str, Any] = {}
        self.last_result: dict[str, Any] | None = None
        self.pending: PendingPlan | None = None
        self.turn_id = 0
        self.user_message = ""
        self._version = 0
        self._prior_eligible = False
        self._prior_authorized = False
        self._revision_attempted = False
        self._display_needed = False
        self._result_changed = False

    def begin_turn(self, message: str) -> None:
        self.turn_id += 1
        self.user_message = message
        self._revision_attempted = False
        self._prior_eligible = bool(self.pending and self.pending.eligible)
        self._prior_authorized = bool(self.pending and self.pending.authorized)
        # No semantic interpretation yet. A pending-turn keep/confirm tool resolves this.
        if self.pending:
            self.pending.eligible = False
            self.pending.authorized = False

    def suspend(self) -> None:
        self._prior_eligible = False
        self._prior_authorized = False
        if self.pending:
            self.pending.eligible = False
            self.pending.authorized = False

    def revision_attempt(self) -> None:
        self._revision_attempted = True
        self._display_needed = False
        self.suspend()

    def _confirmation_status(self) -> dict[str, str]:
        plan = self.pending
        if plan is None:
            state, message = "none", "当前没有待确认方案。"
        elif plan.result is not None:
            state, message = "completed", "当前方案已有完整结果，可直接查看。"
        elif plan.authorized:
            state, message = "authorized", "当前方案已获用户确认，按该方案继续计算。"
        elif self._display_needed:
            state, message = (
                "awaiting_display",
                "新方案已准备；本轮结束时程序将展示方案，请审阅后在下一轮单独回复确认。",
            )
        elif plan.eligible:
            state, message = (
                "awaiting_confirmation",
                "当前方案已展示、尚未执行。可单独回复“确认”“确认执行”或 /confirm 开始计算。",
            )
        elif self._prior_eligible or self._prior_authorized:
            state, message = (
                "awaiting_turn_decision",
                "本轮输入尚未处理完成。请先处理修改或追问，不能据上轮状态宣称现在可直接执行。",
            )
        else:
            state, message = (
                "suspended",
                "方案记录保留，目前不能直接确认执行；请澄清需求并重新准备展示方案。",
            )
        return {"state": state, "message": message}

    def state(self) -> dict[str, Any]:
        return {
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "confirmation_contract": {"version": "2.1.0", "accepted_inputs": CONFIRMATION_INPUTS},
            "user_turn_id": self.turn_id,
            "user_message": self.user_message,
            "pending_plan": None
            if self.pending is None
            else {
                "plan_ref": self.pending.ref,
                "displayed_turn": self.pending.displayed_turn,
                "confirmation_available": self.pending.eligible,
                "confirmation_status": self._confirmation_status(),
                "authorized": self.pending.authorized,
                "static": self.pending.static,
                "result": self.pending.result,
                **self.pending.prepared.summary(),
            },
        }

    def plant_info(self) -> dict[str, Any]:
        context = load_operating_context(self.workspace / "configs/rto/contexts/case_20260604.json")
        manifest = capabilities(repo_root=self.workspace)
        return {
            "status": "ok",
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "provider_id": context.provider_id,
            "process_type": "常压蒸馏（CDU）" if context.provider_id == "cdu-m7" else "未知",
            "model_id": context.model_ref.object_id,
            "claim_scope": context.claim_scope,
            "capabilities": manifest,
            "preparation": {
                "constraints": "系统硬约束与发布改善门禁自动加入；prepare不接受constraints参数。额外业务约束尚不支持，应告知用户，不可静默忽略。",
                "decision_variables": "只选择available变量的非空子集；不要求填写所有登记变量，deferred变量不可加入。",
                "max_candidates": "最终返回方案总数，默认1；对应execution_routes的top_k为上限，maximum_m2_candidates是内部搜索预算。",
            },
            "available_execution": "读取工况、准备方案、确认后完整静态搜索和动态复核",
        }

    def operating_context(self) -> dict[str, Any]:
        context = load_operating_context(self.workspace / "configs/rto/contexts/case_20260604.json")
        self.snapshots[context.fingerprint] = context
        return {
            "status": "ok",
            "snapshot_ref": context.fingerprint,
            "source": "configured_offline_case",
            "context_id": context.context_id,
            **build_chat_operating_status(context),
        }

    def prepare(self, **kwargs: Any) -> dict[str, Any]:
        self.revision_attempt()
        args = PrepareArguments.model_validate(kwargs)
        if self.pending and args.previous_plan_ref != self.pending.ref:
            raise OptimizationPreparationError(
                [
                    {
                        "code": "stale-plan-reference",
                        "json_pointer": "/previous_plan_ref",
                        "message": "修改必须引用当前方案；请inspect_optimization读取当前plan_ref。",
                    }
                ]
            )
        if not self.pending and args.previous_plan_ref is not None:
            raise OptimizationPreparationError(
                [
                    {
                        "code": "unexpected-plan-reference",
                        "json_pointer": "/previous_plan_ref",
                        "message": "当前没有旧方案，新建时省略previous_plan_ref。",
                    }
                ]
            )
        if args.snapshot_ref not in self.snapshots:
            raise OptimizationPreparationError(
                [
                    {
                        "code": "unknown-snapshot-reference",
                        "json_pointer": "/snapshot_ref",
                        "message": "先read_operating_context，使用其snapshot_ref；不能使用context_id或自己编造引用。",
                    }
                ]
            )
        prepared = prepare_optimization(
            repo_root=self.workspace,
            context=self.snapshots[args.snapshot_ref],
            objectives=[item.model_dump() for item in args.objectives],
            decision_variables=args.decision_variables,
            max_candidates=args.max_candidates,
        )
        self._version += 1
        self.pending = PendingPlan(
            f"plan-{self._version}-{prepared.problem.fingerprint[:12]}", prepared
        )
        self._display_needed = True
        return {
            "status": "prepared",
            "solver_called": False,
            "plan_ref": self.pending.ref,
            "confirmation": render_confirmation(prepared, self._version),
            **prepared.summary(),
        }

    def _plan(self, plan_ref: str) -> PendingPlan:
        if self.pending is None or self.pending.ref != plan_ref:
            raise ValueError("unknown or superseded plan")
        return self.pending

    def manage(self, **kwargs: Any) -> dict[str, Any]:
        args = ManageArguments.model_validate(kwargs)
        plan = self._plan(args.plan_ref)
        if args.user_turn_id != self.turn_id or args.user_message != self.user_message:
            raise ValueError("decision must refer to the entire current user message")
        if args.action == "cancel":
            self.pending = None
            self._display_needed = False
            self._prior_eligible = self._prior_authorized = False
            return {"status": "cancelled", "message": "后续执行已取消，已有结果保留。"}
        if self._revision_attempted:
            raise ValueError("a revised plan needs a new displayed confirmation")
        if args.action == "keep":
            plan.eligible = self._prior_eligible
            plan.authorized = self._prior_authorized
            return {"status": "kept", "confirmation_available": plan.eligible}
        if self.user_message.strip() not in CONFIRMATION_INPUTS:
            self.suspend()
            raise ConfirmationInputError(CONFIRMATION_RULE)
        if not (self._prior_eligible or plan.eligible):
            raise ValueError("confirmation is suspended; prepare and display a new plan")
        if plan.displayed_turn is None or plan.displayed_turn >= self.turn_id:
            raise ValueError("confirmation requires a later user turn after display")
        plan.authorized = True
        plan.eligible = False
        self._prior_eligible = False
        return {
            "status": "confirmed",
            "plan_ref": plan.ref,
            "snapshot_ref": plan.prepared.context.fingerprint,
        }

    def solve(self, plan_ref: str) -> dict[str, Any]:
        plan = self._plan(plan_ref)
        if not plan.authorized:
            raise ValueError("plan is not confirmed for execution")
        if plan.static is None:
            plan.static = solve_prepared_optimization(
                plan.prepared, run_root=self.workspace / "runs/rto"
            )
        return plan.static

    def verify(self, plan_ref: str, static_ref: str) -> dict[str, Any]:
        plan = self._plan(plan_ref)
        if not plan.authorized or plan.static is None or static_ref != plan.static["static_ref"]:
            raise ValueError("verify requires the confirmed plan's completed static stage")
        if plan.result is None:
            plan.result = verify_prepared_optimization(
                plan.prepared, static_ref=static_ref, run_root=self.workspace / "runs/rto"
            )
            self.last_result = plan.result
            self._result_changed = True
        return plan.result

    def inspect_result(self, workflow_id: str | None = None) -> dict[str, Any]:
        if workflow_id is None:
            return {"status": "ok", "task": self.state(), "result": self.last_result}
        args = ResultArguments(workflow_id=workflow_id)
        root = self.workspace / "runs/rto"
        directory = root / str(args.workflow_id)
        if any(path.is_symlink() for path in (root.parent, root, directory)):
            raise ValueError("result directory must not be a symbolic link")
        try:
            result = build_optimization_run_summary(inspect_offline(directory)).as_dict()
        except OfflineInspectionError as exc:
            raise ValueError("result evidence could not be verified") from exc
        self.last_result = {"status": "ok", "workflow_id": workflow_id, "result": result}
        return self.last_result

    def displays(self, *, success: bool) -> tuple[str, ...]:
        outputs: list[str] = []
        if not success:
            self.suspend()
        if self._display_needed and self.pending:
            outputs.append(
                render_confirmation(self.pending.prepared, self._version) + "\n" + CONFIRMATION_RULE
            )
            self.pending.displayed_turn = self.turn_id
            self.pending.eligible = success
            self._display_needed = False
            if not success:
                outputs.append("本轮未完整结束，方案确认资格暂停；请重新准备并展示方案后确认。")
        elif self.pending and not self.pending.result:
            # Unresolved prior eligibility cannot survive this user turn.
            if not self.pending.eligible and not self.pending.authorized:
                self.suspend()
            outputs.append("程序状态：" + self._confirmation_status()["message"])
        if self._result_changed and self.last_result:
            outputs.append(
                "程序核验的优化结果：\n"
                + json.dumps(self.last_result, ensure_ascii=False, indent=2)
            )
            self._result_changed = False
        return tuple(outputs)

    def tools(self) -> list[BaseTool]:
        specs: list[tuple[str, Callable[..., Any], type[BaseModel], str]] = [
            (
                "get_plant_info",
                self.plant_info,
                NoArguments,
                "读取装置身份及目标metric_id、sense、变量和约束能力。",
            ),
            (
                "read_operating_context",
                self.operating_context,
                NoArguments,
                "读取配置离线工况和不可变snapshot_ref，不仿真；读取本身不改变已准备方案。",
            ),
            (
                "prepare_optimization",
                self.prepare,
                PrepareArguments,
                "按目标优先级排列objectives，只指定用户允许的available变量。系统约束自动加入，不接受constraints；额外业务约束尚不支持，不可忽略用户要求。max_candidates是返回方案数，默认1，上限为能力中对应路线top_k，不是maximum_m2_candidates。必须使用工况工具返回的snapshot_ref。只构造问题不仿真；修改须带previous_plan_ref，撤销旧确认资格，下一用户轮才能确认。",
            ),
            (
                "manage_optimization",
                self.manage,
                ManageArguments,
                "处理已展示方案："
                + CONFIRMATION_RULE
                + "满足此规则才可confirm；取消用cancel；明确只是聊天/查询或继续已授权执行且不改变方案用keep。必须逐字引用当前完整user_message及user_turn_id。有条件确认、变量排除或要求不明不得confirm/keep，应修改方案或追问。",
            ),
            (
                "solve_optimization",
                self.solve,
                PlanArguments,
                "仅执行已确认plan_ref的完整M2静态搜索及候选排序；包含内部配对仿真。完成后调用verify_optimization，不可当作最终推荐。重复调用返回同一阶段结果。",
            ),
            (
                "verify_optimization",
                self.verify,
                VerifyArguments,
                "使用同一已确认方案及static_ref，复核全部M4入围候选并最终选择，返回可解释的真实结果。不可挑选子集。",
            ),
            (
                "inspect_optimization",
                self.inspect_result,
                ResultArguments,
                "读取当前方案/阶段/结果或指定workflow_id的已有结果，不执行计算，不接受路径。",
            ),
        ]
        return [
            StrictStructuredTool.from_function(
                func=function,
                name=name,
                args_schema=schema,
                description=description,
            )
            for name, function, schema, description in specs
        ]

    def clear(self) -> None:
        self.snapshots.clear()
        self.pending = None
        self.last_result = None
        self._display_needed = self._result_changed = False
        self.suspend()
