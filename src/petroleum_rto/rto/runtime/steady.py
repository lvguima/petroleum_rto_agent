"""Deterministic preparation and evidence-driven single steady comparison.

This runtime does not optimize or invent quality constraints. Backend operations
are confined to the HYSYS adapter; model tools receive summaries, never COM objects.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from petroleum_rto.rto._file_lock import exclusive_file_lock
from petroleum_rto.rto.adapters import hysys_steady as backend

LIMITATIONS = (
    "结果仅属于合成工程仿真。",
    "同目标重复重算存在差异，尚不能可靠排序或推荐最优值。",
    "支持24项MV的单项或多项设定值比较，未发布工艺有效范围或最优搜索。",
    "尚未定义产品质量约束，物流总量不等同合格成品收率；边界热流不等同燃料耗量或实际收益。",
    "工作案例与源案例共享HYSYS进程，阻塞COM调用没有硬超时。",
)


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def context_ref(context: Any) -> str:
    backend.validate_context(context)
    return hashlib.sha256(canonical(context).encode()).hexdigest()


def read_context(workspace: Path) -> dict[str, Any]:
    with exclusive_file_lock(workspace / "runs/simulation/hysys-agent.lock", label="HYSYS Agent"):
        return backend.read_context(workspace)


def control_variables() -> list[dict[str, Any]]:
    return backend.capabilities()


@dataclass(frozen=True)
class PreparedComparison:
    context_json: str
    target_c: float | None
    boundary_sha256: str
    changes_json: str = "[]"
    catalog_json: str = "{}"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(self.as_dict()).encode()).hexdigest()

    @property
    def changes(self) -> list[dict[str, Any]]:
        return list(json.loads(self.changes_json))

    @property
    def baseline_changes(self) -> list[dict[str, Any]]:
        readings = {r["variable_id"]: r for r in json.loads(self.context_json)["variables"]}
        return [c | {"value": readings[c["variable_id"]]["value"]} for c in self.changes]

    def as_dict(self) -> dict[str, Any]:
        if self.target_c is None:
            return {
                "schema_id": "steady-comparison-plan",
                "schema_version": "2.0.0",
                "context": json.loads(self.context_json),
                "changes": self.changes,
                "catalog": json.loads(self.catalog_json),
                "boundary_sha256": self.boundary_sha256,
            }
        return {
            "schema_id": "steady-comparison-plan",
            "schema_version": "1.0.0",
            "context": json.loads(self.context_json),
            "target_c": self.target_c,
            "boundary_sha256": self.boundary_sha256,
        }

    def summary(self) -> dict[str, Any]:
        if self.target_c is None:
            return {
                "process_type": "HYSYS常压蒸馏稳态仿真",
                "snapshot_ref": context_ref(json.loads(self.context_json)),
                "changes": [
                    {
                        "variable_id": c["variable_id"],
                        "unit": c["unit"],
                        "baseline": b["value"],
                        "target": c["value"],
                    }
                    for b, c in zip(self.baseline_changes, self.changes, strict=True)
                ],
                "execution": "保存基准副本→基准重算→选定MV统一写入及候选重算→逐次物料/能量比较",
                "eligible_for_optimization": False,
                "limitations": list(LIMITATIONS),
            }
        return {
            "process_type": "HYSYS常压蒸馏稳态仿真",
            "snapshot_ref": context_ref(json.loads(self.context_json)),
            "variable": "T-39",
            "baseline_c": 156.8,
            "target_c": self.target_c,
            "execution": "保存基准副本→基准重算→候选重算→逐次物料/能量比较",
            "eligible_for_optimization": False,
            "limitations": list(LIMITATIONS),
        }


def load_prepared_comparison(value: Any) -> PreparedComparison:
    if type(value) is dict and value.get("schema_version") == "2.0.0":
        if (
            set(value)
            != {"schema_id", "schema_version", "context", "changes", "catalog", "boundary_sha256"}
            or value["schema_id"] != "steady-comparison-plan"
        ):
            raise ValueError("Unsupported MV plan")
        context_ref(value["context"])
        changes = backend.validate_changes(value["context"], value["changes"], value["catalog"])
        if value["changes"] != changes:
            raise ValueError("MV changes must be canonical")
        if (
            type(value["boundary_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["boundary_sha256"]) is None
        ):
            raise ValueError("Invalid boundary definition digest")
        return PreparedComparison(
            canonical(value["context"]),
            None,
            value["boundary_sha256"],
            canonical(changes),
            canonical(value["catalog"]),
        )
    fields = {"schema_id", "schema_version", "context", "target_c", "boundary_sha256"}
    if (
        type(value) is not dict
        or set(value) != fields
        or value["schema_id"] != "steady-comparison-plan"
        or value["schema_version"] != "1.0.0"
    ):
        raise ValueError("Unsupported steady plan")
    context_ref(value["context"])
    if type(value["target_c"]) not in (float, int) or value["target_c"] not in (156.8, 156.9):
        raise ValueError("Only the two qualified commissioning targets are available")
    if (
        type(value["boundary_sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["boundary_sha256"]) is None
    ):
        raise ValueError("Invalid boundary definition digest")
    return PreparedComparison(
        canonical(value["context"]), float(value["target_c"]), value["boundary_sha256"]
    )


def prepare_comparison(context: Any, changes: Any) -> PreparedComparison:
    catalog = backend.read_catalog()
    return load_prepared_comparison(
        {
            "schema_id": "steady-comparison-plan",
            "schema_version": "2.0.0",
            "context": context,
            "changes": backend.validate_changes(context, changes, catalog),
            "catalog": catalog,
            "boundary_sha256": backend.definition_hash(),
        }
    )


def render_confirmation(prepared: PreparedComparison, version: int) -> str:
    if prepared.target_c is None:
        rows = [
            f"- {c['variable_id']}：{b['value']:.8g} → {c['value']:.8g} {c['unit']}"
            for b, c in zip(prepared.baseline_changes, prepared.changes, strict=True)
        ]
        return "\n".join(
            [
                f"待确认稳态比较方案（第{version}版）",
                "选定MV调整：",
                *rows,
                "确认后保存基准副本，基准与候选分别计算；所有选定MV在暂停求解期间统一写入，未选MV保持不变。",
                "本次不进行最优排名或产品合格判定。",
                "输入/confirm、确认或确认执行开始。",
            ]
        )
    return "\n".join(
        (
            f"待确认稳态比较方案（第{version}版）",
            f"唯一调节项：T-39，基准156.80 ℃ → 候选{prepared.target_c:.2f} ℃。",
            "确认后保存当前工况的基准副本，在独立工作副本各重算一次基准与候选，保存各自物料、能量数据并比较。",
            "重复性尚未通过；本次不进行最优排名或产品合格判定。",
            "输入/confirm、确认或确认执行开始。",
        )
    )


def workflow_id(prepared: PreparedComparison) -> str:
    return "steady-" + prepared.fingerprint[:16]


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value) + "\n")


def _directory(root: Path, identity: str) -> Path:
    if re.fullmatch(r"steady-[0-9a-f]{16}", identity) is None:
        raise ValueError("Invalid steady result identity")
    backend.plain_directory(root)
    directory = root / identity
    if directory.exists():
        backend.plain_directory(directory)
    return directory


def _rebuild(prepared: PreparedComparison, directory: Path) -> dict[str, Any]:
    baseline_dir = directory / "frozen"
    digest = backend.validate_baseline(
        json.loads(prepared.context_json),
        baseline_dir,
        json.loads(prepared.catalog_json) if prepared.target_c is None else None,
    )
    baseline = backend.point_summary(
        directory / "baseline",
        baseline_dir,
        prepared.baseline_changes if prepared.target_c is None else 156.8,
        prepared.boundary_sha256,
    )
    candidate = None
    if baseline["status"] == "passed":
        candidate = backend.point_summary(
            directory / "candidate",
            baseline_dir,
            prepared.changes if prepared.target_c is None else prepared.target_c,
            prepared.boundary_sha256,
        )
    elif (directory / "candidate").exists():
        raise ValueError("Candidate must not run after a failed baseline")
    valid = candidate is not None and candidate["status"] == "passed"
    comparisons = []
    if valid:
        assert candidate is not None
        for before, after in zip(baseline["metrics"], candidate["metrics"], strict=True):
            if any(before[k] != after[k] for k in ("metric_id", "label", "unit")):
                raise ValueError("Paired metric definitions differ")
            comparisons.append(
                {k: before[k] for k in ("metric_id", "label", "unit")}
                | {
                    "baseline": before["value"],
                    "candidate": after["value"],
                    "delta": after["value"] - before["value"],
                }
            )
    return {
        "schema_id": "steady-comparison-result",
        "schema_version": "2.0.0" if prepared.target_c is None else "1.0.0",
        "plan_ref": prepared.fingerprint,
        "status": "comparison_only" if valid else "evaluation_error",
        "baseline_sha256": digest,
        "baseline": baseline,
        "candidate": candidate,
        "comparisons": comparisons,
        "eligible_for_optimization": False,
        "limitations": list(LIMITATIONS)
        if prepared.target_c is None
        else [
            *LIMITATIONS[:2],
            "仅比较两个已联调的离散温度点；它们不是工艺上下限。",
            *LIMITATIONS[3:],
        ],
    }


def inspect_comparison(directory: Path) -> dict[str, Any]:
    backend.plain_directory(directory)
    prepared = load_prepared_comparison(backend.read_json(directory / "prepared.json"))
    if directory.name != workflow_id(prepared):
        raise ValueError("Result directory differs from plan identity")
    result = _rebuild(prepared, directory)
    if canonical(backend.read_json(directory / "result.json")) != canonical(result):
        raise ValueError("Stored result differs from physical evidence")
    return {"status": "complete", "workflow_id": directory.name, "result": result}


def read_prepared_result(prepared: PreparedComparison, *, run_root: Path) -> dict[str, Any]:
    result = inspect_comparison(_directory(run_root, workflow_id(prepared)))
    if result["result"]["plan_ref"] != prepared.fingerprint:
        raise ValueError("Result plan differs")
    return result


def execute_comparison(
    prepared: PreparedComparison,
    *,
    run_root: Path,
    workspace: Path,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    with exclusive_file_lock(workspace / "runs/simulation/hysys-agent.lock", label="HYSYS Agent"):
        return _execute_comparison(
            prepared, run_root=run_root, workspace=workspace, on_progress=on_progress
        )


def _execute_comparison(
    prepared: PreparedComparison,
    *,
    run_root: Path,
    workspace: Path,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    prepared = load_prepared_comparison(prepared.as_dict())
    backend.plain_directory(run_root.parent)
    run_root.mkdir(exist_ok=True)
    directory = _directory(run_root, workflow_id(prepared))
    emit = on_progress or (lambda message: None)
    if directory.exists() and (directory / "result.json").exists():
        result = read_prepared_result(prepared, run_root=run_root)
        emit("进度：已严格重载完整稳态结果，未重新计算。")
        return result
    if prepared.target_c is None and canonical(backend.read_catalog()) != prepared.catalog_json:
        raise ValueError("MV catalog changed; prepare again")
    if backend.definition_hash() != prepared.boundary_sha256:
        raise ValueError("Boundary configuration changed; prepare again")
    if not directory.exists():
        directory.mkdir()
        _write(directory / "prepared.json", prepared.as_dict())
    if load_prepared_comparison(backend.read_json(directory / "prepared.json")) != prepared:
        raise ValueError("Saved plan differs")
    frozen = directory / "frozen"
    if not frozen.exists():
        emit("进度：保存已确认工况的基准副本。")
        backend.capture(json.loads(prepared.context_json), frozen, workspace)
    backend.validate_baseline(
        json.loads(prepared.context_json),
        frozen,
        json.loads(prepared.catalog_json) if prepared.target_c is None else None,
    )
    points: tuple[tuple[str, Any], ...] = (
        (("baseline", prepared.baseline_changes), ("candidate", prepared.changes))
        if prepared.target_c is None
        else (("baseline", 156.8), ("candidate", prepared.target_c))
    )
    for name, target in points:
        point_dir = directory / name
        if not point_dir.exists():
            if (
                prepared.target_c is None
                and canonical(backend.read_catalog()) != prepared.catalog_json
            ):
                raise ValueError("MV catalog changed during execution")
            if backend.definition_hash() != prepared.boundary_sha256:
                raise ValueError("Boundary configuration changed during execution")
            emit(
                f"进度：正在计算{'基准' if name == 'baseline' else '候选'}工况，并采集同次物料与能量。"
            )
            backend.run_point(frozen, target, point_dir)
        # A partial directory fails strict reading; /resume never overwrites or retries it.
        point = backend.point_summary(point_dir, frozen, target, prepared.boundary_sha256)
        if point["status"] != "passed":
            break
    _write(directory / "result.json", _rebuild(prepared, directory))
    result = read_prepared_result(prepared, run_root=run_root)
    emit("进度：稳态比较已结束，结果与限制已保存。")
    return result
