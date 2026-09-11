"""Stateless domain tools publishing versioned updates into the Agent graph."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from petroleum_rto.rto import load_operating_context
from petroleum_rto.rto.runtime import (
    OfflineInspectionError,
    OperatingContext,
    OptimizationPreparationError,
    build_chat_operating_status,
    build_optimization_run_summary,
    capabilities,
    dump_prepared_optimization,
    inspect_offline,
    load_prepared_optimization,
    prepare_optimization,
    render_confirmation,
)

CONFIRMATION_INPUTS = ("/confirm", "确认", "确认执行")
TOOL_CONTRACT_VERSION = "3.0.0"
CONFIRMATION_RULE = (
    "仅整条输入为"
    + "、".join(f"“{text}”" for text in CONFIRMATION_INPUTS)
    + "时可授权计算（只忽略首尾空白，不忽略标点或附加条件）。"
)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)


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


def argument_error(schema: type[BaseModel], exc: ValidationError) -> dict[str, Any]:
    fields = set(schema.model_fields)
    return {
        "status": "error",
        "code": "invalid-tool-arguments",
        "issues": [
            {
                "code": e["type"],
                "json_pointer": "/" + str(e["loc"][0])
                if e["loc"] and (e["loc"][0] in fields or e["loc"][0] == "constraints")
                else "/",
                "message": "prepare不接受constraints；系统门禁自动保留，额外业务限制不可忽略。"
                if e["loc"] and e["loc"][0] == "constraints"
                else "参数不符合工具Schema；不改变用户目标与变量。",
            }
            for e in exc.errors(include_input=False, include_context=False)
        ],
    }


class AgentDomainTools:
    """Stateless domain services; all active business facts live in graph state."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def plant_info(self, state: dict[str, Any]) -> dict[str, Any]:
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

    def operating_context(self, state: dict[str, Any]) -> dict[str, Any]:
        context = load_operating_context(self.workspace / "configs/rto/contexts/case_20260604.json")
        state["snapshots"] = {**state["snapshots"], context.fingerprint: context.as_dict()}
        return {
            "status": "ok",
            "snapshot_ref": context.fingerprint,
            "source": "configured_offline_case",
            "context_id": context.context_id,
            **build_chat_operating_status(context),
        }

    @staticmethod
    def revision_attempt(state: dict[str, Any]) -> None:
        if state["pending"] is not None:
            state["pending"] = {
                **state["pending"],
                "status": "revision_required",
                "static": None,
                "result": None,
            }

    def prepare(self, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.revision_attempt(state)
        args = PrepareArguments.model_validate(kwargs)
        pending = state["pending"]
        if pending and args.previous_plan_ref != pending["ref"]:
            raise OptimizationPreparationError(
                [
                    {
                        "code": "stale-plan-reference",
                        "json_pointer": "/previous_plan_ref",
                        "message": "修改必须引用当前plan_ref。",
                    }
                ]
            )
        if not pending and args.previous_plan_ref is not None:
            raise OptimizationPreparationError(
                [
                    {
                        "code": "unexpected-plan-reference",
                        "json_pointer": "/previous_plan_ref",
                        "message": "当前没有旧方案，新建时省略previous_plan_ref。",
                    }
                ]
            )
        if args.snapshot_ref not in state["snapshots"]:
            raise OptimizationPreparationError(
                [
                    {
                        "code": "unknown-snapshot-reference",
                        "json_pointer": "/snapshot_ref",
                        "message": "先读取工况并使用其snapshot_ref。",
                    }
                ]
            )
        prepared = prepare_optimization(
            repo_root=self.workspace,
            context=OperatingContext.from_mapping(state["snapshots"][args.snapshot_ref]),
            objectives=[item.model_dump() for item in args.objectives],
            decision_variables=args.decision_variables,
            max_candidates=args.max_candidates,
        )
        version = state["plan_version"] + 1
        ref = f"plan-{version}-{prepared.problem.fingerprint[:12]}"
        state["plan_version"] = version
        state["pending"] = {
            "ref": ref,
            "version": version,
            "prepared": dump_prepared_optimization(prepared),
            "displayed_turn": None,
            "status": "awaiting_display",
            "static": None,
            "result": None,
        }
        return {
            "status": "prepared",
            "solver_called": False,
            "plan_ref": ref,
            "confirmation": render_confirmation(prepared, version),
            **prepared.summary(),
        }

    def cancel(self, state: dict[str, Any], plan_ref: str) -> dict[str, Any]:
        if state["pending"] is None or state["pending"]["ref"] != plan_ref:
            raise ValueError("unknown current plan")
        state["pending"] = None
        return {"status": "cancelled", "message": "后续执行已取消，已有结果保留。"}

    def inspect_result(
        self, state: dict[str, Any], workflow_id: str | None = None
    ) -> dict[str, Any]:
        if workflow_id is None:
            return {"status": "ok", "task": self.project(state), "result": state["last_result"]}
        args = ResultArguments(workflow_id=workflow_id)
        root = self.workspace / "runs/rto"
        directory = root / str(args.workflow_id)
        if any(path.is_symlink() for path in (root.parent, root, directory)):
            raise ValueError("result directory must not be a symbolic link")
        try:
            result = build_optimization_run_summary(inspect_offline(directory)).as_dict()
        except OfflineInspectionError as exc:
            raise ValueError("result evidence could not be verified") from exc
        receipt = {"status": "ok", "workflow_id": workflow_id, "result": result}
        state["last_result"] = receipt
        return receipt

    @staticmethod
    def project(state: dict[str, Any]) -> dict[str, Any]:
        pending = state["pending"]
        plan = None
        if pending is not None:
            plan = {key: pending[key] for key in ("ref", "version", "status", "static", "result")}
            plan["plan_ref"] = pending["ref"]
            plan["confirmation_status"] = pending["status"]
            plan["confirmation_available"] = pending["status"] == "awaiting_confirmation"
            plan["authorized"] = pending["status"] in {"approved", "completed"}
            plan["summary"] = load_prepared_optimization(pending["prepared"]).summary()
        return {
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "user_turn_id": state["turn_id"],
            "pending_plan": plan,
            "confirmation_rule": CONFIRMATION_RULE,
        }

    def tools(self) -> list[BaseTool]:
        from copy import deepcopy

        from langchain_core.messages import ToolMessage
        from pydantic import ValidationError

        specs: list[tuple[str, Callable[..., Any], type[BaseModel], str]] = [
            (
                "get_plant_info",
                self.plant_info,
                NoArguments,
                "读取装置身份及可用目标、变量、系统门禁。",
            ),
            (
                "read_operating_context",
                self.operating_context,
                NoArguments,
                "读取配置工况和固定snapshot_ref，不仿真。",
            ),
            (
                "prepare_optimization",
                self.prepare,
                PrepareArguments,
                "只构造待审批方案；只选用户允许的available变量。系统门禁自动保留，不接受constraints，额外限制不可忽略。max_candidates是返回数量，不是搜索预算；修改必须引用当前previous_plan_ref。",
            ),
            (
                "cancel_optimization",
                self.cancel,
                PlanArguments,
                "取消当前plan_ref的待执行任务，保留已有结果。",
            ),
            (
                "inspect_optimization",
                self.inspect_result,
                ResultArguments,
                "读取当前方案或严格重载指定workflow_id，不计算，不接受路径。",
            ),
        ]

        def runner(
            name: str, function: Callable[..., Any], schema: type[BaseModel]
        ) -> Callable[..., Any]:
            def run(runtime: ToolRuntime[Any, Any], **kwargs: Any) -> Command[Any]:
                original = runtime.state["session"]
                state = deepcopy(original)
                if name == "prepare_optimization":
                    self.revision_attempt(state)
                status = "success"
                try:
                    calls = runtime.state["messages"][-1].tool_calls
                    if len(calls) > 1 and any(
                        c["name"] in {"prepare_optimization", "cancel_optimization"} for c in calls
                    ):
                        self.revision_attempt(state)
                        raise ValueError("sequential-tools-required")
                    schema.model_validate(kwargs)
                    result = function(state, **kwargs)
                except ValidationError as exc:
                    status = "error"
                    result = argument_error(schema, exc)
                except OptimizationPreparationError as exc:
                    status = "error"
                    result = {
                        "status": "error",
                        "code": "optimization-preparation-rejected",
                        "issues": exc.issues,
                    }
                except (ValueError, TypeError, KeyError, OSError):
                    status = "error"
                    result = {
                        "status": "error",
                        "code": "domain-tool-rejected",
                        "message": "参数、方案引用或阶段证据无效，请检查具体需求和本地证据。",
                    }
                updates = {key: value for key, value in state.items() if value != original.get(key)}
                return Command(
                    update={
                        "session": updates,
                        "messages": [
                            ToolMessage(
                                name=name,
                                tool_call_id=runtime.tool_call_id,
                                status=status,
                                content=json.dumps(result, ensure_ascii=False),
                            )
                        ],
                    }
                )

            return run

        return [
            StructuredTool.from_function(
                func=runner(name, function, schema),
                name=name,
                args_schema=schema.model_json_schema(),
                metadata={"argument_schema": schema},
                description=description,
            )
            for name, function, schema, description in specs
        ]
