"""Readable views of trusted RTO receipts; persisted values stay unchanged."""

from __future__ import annotations

import math
from typing import Any, TypeGuard

_RESULT_STATUS = {
    "feasible_not_publishable": "已有可行方案，但未达到发布改善门槛。",
    "no_feasible": "未找到满足约束的可行方案，暂无推荐。",
    "no_verified_candidate": "未得到通过动态复核的方案，暂无已核验推荐。",
    "invalid_request": "请求无效，未形成推荐方案。",
    "evaluation_error": "评价发生错误，未形成可靠推荐。",
    "unsupported_problem": "当前能力不支持该优化问题，未形成推荐方案。",
}
_VERIFICATION_STATUS = {
    "feasible": "可行",
    "process_infeasible": "工艺不可行",
    "invalid_request": "请求无效",
    "evaluation_error": "评价错误",
    "not_evaluated": "未评价",
}
_OPERATING_MODES = {
    "normal-steady": "正常稳态",
    "steady_crude_distillation": "稳态常压蒸馏",
}
_DATA_QUALITY = {
    "weak-time-alignment": "时间对齐较弱",
    "trusted_synthetic_fixture": "受信合成数据",
}


def _numeric(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value: Any) -> str:
    if not _numeric(value):
        return "未提供"
    if value == 0:
        return "0.00"
    # Keep small nonzero changes visible instead of rounding them to signed zero.
    return f"{value:.2f}" if abs(value) >= 0.01 else f"{value:.2g}"


def _quantity(value: Any, unit: str, *, difference: bool = False) -> str:
    if not _numeric(value):
        return "未提供"
    if unit in {"1", "mass_fraction"}:
        return _number(value * 100) + ("个百分点" if difference else "%")
    return f"{_number(value)} {unit}".rstrip()


def _rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _adjustment(item: dict[str, Any]) -> str:
    unit = str(item.get("unit", ""))
    baseline, recommended = item.get("baseline_value"), item.get("recommended_value")
    before, after = _quantity(baseline, unit), _quantity(recommended, unit)
    if unit == "K":
        if _numeric(baseline):
            before += f"（{_number(baseline - 273.15)} ℃）"
        if _numeric(recommended):
            after += f"（{_number(recommended - 273.15)} ℃）"
    change = item.get("adjustment")
    if not _numeric(change):
        movement = "调整量未提供"
    elif change == 0:
        movement = "保持不变"
    else:
        movement = ("上调 " if change > 0 else "下调 ") + _quantity(
            abs(change), unit, difference=True
        )
    name = item.get("business_name") or item.get("variable_id") or "调整项"
    return f"{name}：{before} → {after}；{movement}"


def _effect(effect: dict[str, Any], target: dict[str, Any], baseline: dict[str, Any]) -> str:
    unit = str(effect.get("unit", target.get("unit", "")))
    name = target.get("business_name") or effect.get("metric_id") or "目标"
    direction = {"minimize": "越低越好", "maximize": "越高越好"}.get(target.get("sense", ""))
    label = f"{name}（{direction}）" if direction else str(name)
    value = effect.get("directional_improvement")
    if not _numeric(value):
        improvement = "改善量未提供"
    elif value == 0:
        improvement = "无改善"
    else:
        improvement = ("改善 " if value > 0 else "变差 ") + _quantity(
            abs(value), unit, difference=True
        )
    relative = effect.get("relative_improvement")
    if not _numeric(relative):
        relative_text = "相对改善未提供"
    elif relative == 0:
        relative_text = "相对变化 0.00%"
    else:
        relative_text = ("相对改善 " if relative > 0 else "相对变差 ") + _quantity(
            abs(relative), "1"
        )
    return (
        f"{label}：{_quantity(baseline.get('value'), unit)} → "
        f"{_quantity(effect.get('predicted_value'), unit)}；{improvement}；{relative_text}"
    )


def render_optimization_result(receipt: dict[str, Any]) -> str:
    """Render a strictly obtained staged/inspection receipt, without IO or mutation."""
    result = receipt.get("result")
    if not isinstance(result, dict) or not result:
        return "当前会话尚无已完成的优化结果。"

    status = result.get("status", "")
    adjustments = _rows(result.get("recommended_adjustments"))
    effects = _rows(result.get("predicted_effects"))
    selected = status in {"success", "feasible_not_publishable"}
    if status == "success":
        message = (
            "已选出通过静态与动态核验的推荐方案。"
            if adjustments and effects
            else "本次未提供完整推荐方案或预测效果，暂无可展示的推荐。"
        )
    else:
        message = _RESULT_STATUS.get(status, "结果状态无法确认，暂无可展示的推荐。")
    lines = ["优化结果：" + message]

    targets = _rows(result.get("targets"))
    target_by_id = {item.get("metric_id"): item for item in targets}
    baseline_by_id = {item.get("metric_id"): item for item in _rows(result.get("baseline_values"))}
    if selected:
        lines += ["", "推荐调整：" if status == "success" else "可行方案调整（未通过发布门槛）："]
        lines += ["- " + _adjustment(item) for item in adjustments] or ["- 未提供调整数据。"]
        lines += ["", "预期效果（同工况基准 → 预测）："]
        lines += [
            "- "
            + _effect(
                item,
                target_by_id.get(item.get("metric_id"), {}),
                baseline_by_id.get(item.get("metric_id"), {}),
            )
            for item in effects
        ] or ["- 未提供预测数据。"]
    elif targets:
        names = [
            str(item.get("business_name") or item.get("metric_id") or "目标") for item in targets
        ]
        lines += ["", "本次目标：" + "；".join(names)]

    alternatives = _rows(result.get("alternative_candidates")) if selected else []
    if alternatives:
        lines += ["", "其他候选（按原始排名，不替代推荐方案）："]
        for candidate in alternatives:
            stage = candidate.get("verification_stage", "")
            stage_label = {"M2": "M2静态评价", "M4": "M4动态复核"}.get(stage, "核验阶段未提供")
            verification = _VERIFICATION_STATUS.get(
                candidate.get("verification_status", ""), "状态未提供"
            )
            suffix = "；尚未动态复核" if stage == "M2" else ""
            lines.append(
                f"- 第{candidate.get('rank', '未知')}名：{stage_label}{verification}{suffix}"
            )
            lines += [
                "  调整：" + _adjustment(item) for item in _rows(candidate.get("adjustments"))
            ]
            lines += [
                "  M2预测："
                + _effect(
                    item,
                    target_by_id.get(item.get("metric_id"), {}),
                    baseline_by_id.get(item.get("metric_id"), {}),
                )
                for item in _rows(candidate.get("predicted_effects"))
            ]

    context = result.get("operating_context")
    context = context if isinstance(context, dict) else {}
    mode = context.get("operating_mode") or "未提供"
    quality = context.get("data_quality") or "未提供"
    timestamp = str(context.get("data_timestamp") or "未提供").replace("T", " ", 1)
    lines += [
        "",
        f"工况：{_OPERATING_MODES.get(mode, mode)}；进料 "
        + _quantity(context.get("fresh_feed_load_t_per_h"), "t/h"),
        f"数据时间：{timestamp}；数据质量：{_DATA_QUALITY.get(quality, quality)}",
        "结果编号：" + str(receipt.get("workflow_id") or "未提供"),
    ]
    return "\n".join(lines)
