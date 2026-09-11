from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from petroleum_rto.assistant.presentation import render_optimization_result
from petroleum_rto.rto import runtime


def receipt() -> dict[str, Any]:
    """The user's result, constructed with the public seven-field result types."""
    metric = "specific_furnace_fuel_energy_mj_per_t"

    def adjustment(value: float) -> runtime.OptimizationAdjustmentSummary:
        return runtime.OptimizationAdjustmentSummary(
            variable_id="furnace_temperature_target_k",
            business_name="炉出口温度目标",
            unit="K",
            baseline_value=628.35,
            recommended_value=value,
            adjustment=value - 628.35,
        )

    def effect(
        value: float, improvement: float, relative: float
    ) -> runtime.OptimizationPredictedEffectSummary:
        return runtime.OptimizationPredictedEffectSummary(
            metric_id=metric,
            predicted_value=value,
            unit="MJ/t",
            directional_improvement=improvement,
            relative_improvement=relative,
        )

    summary = runtime.OptimizationRunSummary(
        status="success",
        targets=(
            runtime.OptimizationTargetSummary(
                metric, "单位进料炉燃料热负荷", "minimize", 1, "MJ/t"
            ),
        ),
        operating_context=runtime.OptimizationContextSummary(
            "normal-steady",
            113.1388888888889,
            407.3,
            "2026-06-04T09:16:00+08:00",
            "weak-time-alignment",
        ),
        baseline_values=(runtime.OptimizationBaselineSummary(metric, 188.37898479334825, "MJ/t"),),
        recommended_adjustments=(adjustment(626.35),),
        predicted_effects=(effect(183.99306755689793, 4.385917236450325, 0.02328241253270197),),
        alternative_candidates=(
            runtime.OptimizationAlternativeCandidateSummary(
                2,
                (adjustment(626.85),),
                (effect(185.08954686601052, 3.2894379273377297, 0.017461809399526403),),
                "M4",
                "feasible",
            ),
            runtime.OptimizationAlternativeCandidateSummary(
                3,
                (adjustment(627.35),),
                (effect(186.1860261751231, 2.1929586182251626, 0.011641206266350986),),
                "M4",
                "feasible",
            ),
        ),
    )
    return {
        "status": "complete",
        "workflow_id": "offline-rto-0123456789abcdef",
        "result_source": "internal-private-path/result.json",
        "physical_m2_executions": 19,
        "physical_m4_executions": 7,
        "result": summary.as_dict(),
    }


def test_user_result_is_readable_without_internal_fields_or_lost_precision() -> None:
    data = receipt()
    original = copy.deepcopy(data)
    encoded = json.dumps(data, ensure_ascii=False)

    text = render_optimization_result(data)

    assert "已选出通过静态与动态核验的推荐方案" in text
    assert "628.35 K（355.20 ℃） → 626.35 K（353.20 ℃）；下调 2.00 K" in text
    assert "188.38 MJ/t → 183.99 MJ/t；改善 4.39 MJ/t；相对改善 2.33%" in text
    assert "第2名：M4动态复核可行" in text
    assert "626.85 K（353.70 ℃）；下调 1.50 K" in text
    assert "185.09 MJ/t；改善 3.29 MJ/t；相对改善 1.75%" in text
    assert "第3名：M4动态复核可行" in text
    assert "627.35 K（354.20 ℃）；下调 1.00 K" in text
    assert "186.19 MJ/t；改善 2.19 MJ/t；相对改善 1.16%" in text
    assert text.index("推荐调整") < text.index("预期效果") < text.index("其他候选")
    assert "进料 407.30 t/h" in text
    assert "2026-06-04 09:16:00+08:00" in text and "数据质量：时间对齐较弱" in text
    assert "结果编号：offline-rto-0123456789abcdef" in text
    for internal in ("internal-private-path", "physical_m2", "physical_m4", '"result"', "{"):
        assert internal not in text
    assert data == original and json.dumps(data, ensure_ascii=False) == encoded


def test_feasible_but_unpublishable_keeps_values_without_claiming_recommendation_success() -> None:
    data = receipt()
    data["result"]["status"] = "feasible_not_publishable"
    text = render_optimization_result(data)
    assert "已有可行方案，但未达到发布改善门槛" in text
    assert "可行方案调整（未通过发布门槛）" in text
    assert "183.99 MJ/t" in text
    assert "已选出" not in text and "推荐调整：" not in text


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("no_feasible", "未找到满足约束的可行方案"),
        ("no_verified_candidate", "未得到通过动态复核的方案"),
        ("invalid_request", "请求无效"),
        ("evaluation_error", "评价发生错误"),
        ("unsupported_problem", "当前能力不支持"),
    ],
)
def test_unselected_result_status_never_turns_leftover_values_into_a_recommendation(
    status: str, reason: str
) -> None:
    data = receipt()
    data["result"]["status"] = status
    text = render_optimization_result(data)
    assert reason in text and "本次目标：单位进料炉燃料热负荷" in text
    assert "已选出" not in text and "推荐调整" not in text
    assert "183.99" not in text and "第2名" not in text


@pytest.mark.parametrize("value", [{}, {"result": None}, {"result": {}}])
def test_no_result_does_not_claim_computation_completed(value: dict[str, Any]) -> None:
    assert render_optimization_result(value) == "当前会话尚无已完成的优化结果。"


def test_partial_success_and_unknown_status_are_explicitly_incomplete() -> None:
    text = render_optimization_result({"result": {"status": "success"}})
    assert "未提供完整推荐方案或预测效果" in text and "未提供调整数据" in text
    assert "未提供预测数据" in text and "数据时间：未提供" in text
    assert "已选出" not in text
    unknown = render_optimization_result({"result": {"status": "unexpected"}})
    assert "结果状态无法确认" in unknown and "已选出" not in unknown


@pytest.mark.parametrize("unit", ["1", "mass_fraction"])
def test_multiple_goals_match_baselines_and_format_ratio_units(unit: str) -> None:
    data = receipt()
    result = data["result"]
    result["alternative_candidates"] = []
    result["targets"].append(
        {
            "metric_id": "yield",
            "business_name": "馏分收率",
            "sense": "maximize",
            "priority": 2,
            "unit": unit,
        }
    )
    result["baseline_values"].insert(0, {"metric_id": "yield", "value": 0.591234, "unit": unit})
    result["predicted_effects"].append(
        {
            "metric_id": "yield",
            "predicted_value": 0.596234,
            "directional_improvement": 0.005,
            "relative_improvement": 0.005 / 0.591234,
            "unit": unit,
        }
    )
    original = copy.deepcopy(data)
    text = render_optimization_result(data)
    assert data == original
    assert "188.38 MJ/t → 183.99 MJ/t" in text
    assert "馏分收率（越高越好）：59.12% → 59.62%；改善 0.50个百分点；相对改善 0.85%" in text


@pytest.mark.parametrize(
    ("stage", "status", "expected"),
    [
        ("M2", "feasible", "M2静态评价可行；尚未动态复核"),
        ("M4", "process_infeasible", "M4动态复核工艺不可行"),
        ("M4", "invalid_request", "M4动态复核请求无效"),
        ("M4", "evaluation_error", "M4动态复核评价错误"),
        ("M4", "not_evaluated", "M4动态复核未评价"),
    ],
)
def test_alternatives_preserve_actual_verification_status(
    stage: str, status: str, expected: str
) -> None:
    data = receipt()
    data["result"]["alternative_candidates"] = data["result"]["alternative_candidates"][:1]
    alternative = data["result"]["alternative_candidates"][0]
    alternative.update(verification_stage=stage, verification_status=status)
    text = render_optimization_result(data)
    assert "第2名：" + expected in text and "M2预测：" in text
    assert "第2名：M4动态复核可行" not in text


@pytest.mark.parametrize(
    ("change", "expected"),
    [(-0.00001, "下调 1e-05 K"), (0.00001, "上调 1e-05 K"), (-0.0, "保持不变")],
)
def test_small_adjustments_are_not_rounded_to_zero(change: float, expected: str) -> None:
    data = receipt()
    data["result"]["alternative_candidates"] = []
    item = data["result"]["recommended_adjustments"][0]
    item.update(recommended_value=item["baseline_value"] + change, adjustment=change)
    text = render_optimization_result(data)
    assert expected in text and "-0.00" not in text
    assert "下调 0.00 K" not in text and "上调 0.00 K" not in text


def test_small_deterioration_and_unavailable_relative_improvement_are_not_reported_as_gains() -> (
    None
):
    data = receipt()
    data["result"]["alternative_candidates"] = []
    effect = data["result"]["predicted_effects"][0]
    effect.update(directional_improvement=-0.00004, relative_improvement=-0.000002)
    text = render_optimization_result(data)
    assert "变差 4e-05 MJ/t；相对变差 0.0002%" in text
    effect["relative_improvement"] = None
    assert "相对改善未提供" in render_optimization_result(data)


def test_missing_numeric_values_are_not_fabricated_as_zero() -> None:
    data = receipt()
    data["result"]["alternative_candidates"] = []
    data["result"]["baseline_values"] = []
    data["result"]["recommended_adjustments"][0].pop("adjustment")
    data["result"]["predicted_effects"][0].pop("directional_improvement")
    text = render_optimization_result(data)
    assert "调整量未提供" in text and "改善量未提供" in text
    assert "未提供 → 183.99 MJ/t" in text
