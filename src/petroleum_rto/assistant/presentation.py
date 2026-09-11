"""Readable views of trusted RTO receipts; persisted values stay unchanged."""

from __future__ import annotations

import math
from typing import Any, TypeGuard


def _numeric(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value: Any) -> str:
    if not _numeric(value):
        return "未提供"
    if value == 0:
        return "0.00"
    # Keep small nonzero changes visible instead of rounding them to signed zero.
    return f"{value:.2f}" if abs(value) >= 0.01 else f"{value:.2g}"


def render_optimization_result(receipt: dict[str, Any]) -> str:
    """Render the recomputed steady receipt; never rank uncertain physical results."""
    result = receipt.get("result")
    if not isinstance(result, dict) or not result:
        return "当前会话尚无已完成的稳态比较结果。"
    if result.get("schema_id") != "steady-comparison-result":
        return "结果合同不兼容，未按稳态流程解释。"
    valid = result["status"] == "comparison_only"
    lines = [
        "稳态比较结果："
        + ("基准与候选计算完成。" if valid else "计算或证据检查未通过，未形成有效比较。")
    ]
    for name, label in (("baseline", "基准"), ("candidate", "候选")):
        point = result.get(name)
        if point and "changes" in point:
            lines.append(
                label + ("：通过单点检查" if point["status"] == "passed" else "：单点检查失败")
            )
            responses = {r["variable_id"]: r for r in point["responses"]}
            for change in point["changes"]:
                response = responses.get(change["variable_id"], {})
                lines.append(
                    f"- {change['variable_id']}：目标{_number(change['value'])} {change['unit']}，设定读回{_number(response.get('readback'))}，实际{_number(response.get('actual'))} {change['unit']}。"
                )
        elif point:
            lines.append(
                f"{label}T-39：目标{_number(point['target_c'])} ℃，实际{_number(point['actual_c'])} ℃；"
                + ("通过单点检查" if point["status"] == "passed" else "单点检查失败")
            )
        else:
            lines.append(label + "：未执行。")
    if valid:
        lines += ["", "同次物料与能量（基准 → 候选；差值）："]
        lines += [
            f"- {row['label']}：{_number(row['baseline'])} → {_number(row['candidate'])} {row['unit']}；差值{_number(row['delta'])} {row['unit']}"
            for row in result["comparisons"]
        ]
    lines += ["", *result["limitations"], "结果编号：" + receipt["workflow_id"]]
    return "\n".join(lines)
