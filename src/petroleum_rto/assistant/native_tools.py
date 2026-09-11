"""Stateless domain tools publishing versioned updates into the Agent graph."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from petroleum_rto.rto.runtime.steady import (
    LIMITATIONS,
    context_ref,
    control_variables,
    inspect_comparison,
    load_prepared_comparison,
    prepare_comparison,
    read_context,
    render_confirmation,
)

CONFIRMATION_INPUTS = ("/confirm", "确认", "确认执行")
TOOL_CONTRACT_VERSION = "5.0.0"
CONFIRMATION_RULE = (
    "仅整条输入为"
    + "、".join(f"“{text}”" for text in CONFIRMATION_INPUTS)
    + "时可授权计算（只忽略首尾空白，不忽略标点或附加条件）。"
)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)


class ResultArguments(NoArguments):
    workflow_id: str | None = Field(default=None, pattern=r"^steady-[0-9a-f]{16}$")


class MVTarget(NoArguments):
    variable_id: str = Field(
        description="get_plant_info返回的MV精确标识；不接受CV、任意路径或属性。"
    )
    value: float = Field(allow_inf_nan=False, description="用户要求的绝对目标值。")
    unit: str = Field(description="该MV目录中的显式单位，必须精确匹配。")


class PrepareArguments(NoArguments):
    snapshot_ref: str = Field(description="read_operating_context返回的snapshot_ref。")
    changes: list[MVTarget] = Field(
        min_length=1, max_length=24, description="选定MV的一个或多个设定值；只修改列出的变量。"
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
                "message": "当前支持MV稳态比较，不接受constraints，不能忽略用户额外限制。"
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
        return {
            "status": "ok",
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "process_type": "HYSYS常压蒸馏稳态仿真",
            "model_id": "mjh_atm",
            "control_variables": control_variables(),
            "control_variable_count": 24,
            "available_execution": "读取当前模型、选择一个或多个MV，准备并确认后比较基准与一个候选工况",
            "available_readings": "当前已绑定变量的测量值与单位，包括原油进料、注水、产品流量及TBP点；读取范围不限于T-39。",
            "limitations": list(LIMITATIONS),
        }

    def operating_context(self, state: dict[str, Any]) -> dict[str, Any]:
        context = read_context(self.workspace)
        ref = context_ref(context)
        state["snapshots"] = {**state["snapshots"], ref: context}
        return {
            "status": "ok",
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "snapshot_ref": ref,
            "source": "existing_hysys_memory",
            "model_id": "mjh_atm",
            "observed_at_utc": context["observed_at_utc"],
            "solver_called": False,
            "control_values": [
                {k: r[k] for k in ("variable_id", "unit", "value", "can_modify")}
                for r in context["variables"]
                if r["role"] == "mv"
            ],
            "readings": [
                {
                    key: reading[key]
                    for key in (
                        "variable_id",
                        "object_name",
                        "property_name",
                        "quantity_type",
                        "unit",
                        "value",
                    )
                }
                for reading in context["variables"]
            ],
            "reading_scope": (
                "readings均来自本次HYSYS观测；其中TBP是沸点曲线温度，不是物流出口温度。"
                "物流总流量不等于合格成品产量；本工具未读取组分及相态，也未提供完整物料/能量衡算。"
                "只有目录内的MV可进入调节方案，CV仅读取。"
            ),
            "limitations": list(LIMITATIONS),
        }

    @staticmethod
    def revision_attempt(state: dict[str, Any]) -> None:
        if state["pending"] is not None:
            state["pending"] = {
                **state["pending"],
                "status": "revision_required",
                "result": None,
            }

    def prepare(self, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.revision_attempt(state)
        args = PrepareArguments.model_validate(kwargs)
        pending = state["pending"]
        if pending and args.previous_plan_ref != pending["ref"]:
            raise ValueError("修改必须引用当前plan_ref。")
        if not pending and args.previous_plan_ref is not None:
            raise ValueError("当前没有旧方案。")
        if args.snapshot_ref not in state["snapshots"]:
            raise ValueError("先读取工况并使用其snapshot_ref。")
        prepared = prepare_comparison(
            state["snapshots"][args.snapshot_ref], [change.model_dump() for change in args.changes]
        )
        version = state["plan_version"] + 1
        ref = f"plan-{version}-{prepared.fingerprint[:12]}"
        state["plan_version"] = version
        state["pending"] = {
            "ref": ref,
            "version": version,
            "prepared": prepared.as_dict(),
            "displayed_turn": None,
            "status": "awaiting_display",
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
        receipt = inspect_comparison(directory)
        state["last_result"] = receipt
        return receipt

    @staticmethod
    def project(state: dict[str, Any]) -> dict[str, Any]:
        pending = state["pending"]
        plan = None
        if pending is not None:
            plan = {key: pending[key] for key in ("ref", "version", "status", "result")}
            plan["plan_ref"] = pending["ref"]
            plan["confirmation_status"] = pending["status"]
            plan["confirmation_available"] = pending["status"] == "awaiting_confirmation"
            plan["authorized"] = pending["status"] in {"approved", "completed"}
            plan["summary"] = load_prepared_comparison(pending["prepared"]).summary()
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
                "读取当前稳态联调能力、唯一变量和限制。",
            ),
            (
                "read_operating_context",
                self.operating_context,
                NoArguments,
                "只读当前HYSYS工况及snapshot_ref，返回进料、产品流量、TBP等已绑定变量的readings与单位；不写入或求解。",
            ),
            (
                "prepare_optimization",
                self.prepare,
                PrepareArguments,
                "准备目录内一个或多个MV的稳态比较；changes包含精确标识、目标值和单位，不运行优化搜索；修改必须引用previous_plan_ref。",
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
