"""Steady result rendering preserves values and never invents optimization claims."""

import copy

from steady_helpers import receipt

from petroleum_rto.assistant.presentation import render_optimization_result


def test_steady_comparison_shows_delta_and_uncertainty_without_mutating_evidence():
    value = receipt()
    original = copy.deepcopy(value)
    text = render_optimization_result(value)
    assert "156.80" in text and "156.90" in text
    assert "差值1000.00 kg/h" in text
    assert "尚不能可靠排序" in text
    assert value == original
    assert "M2" not in text and "M4" not in text


def test_small_nonzero_change_is_not_rendered_as_zero():
    value = receipt()
    value["result"]["comparisons"][0]["delta"] = 0.0002
    assert "差值0.0002" in render_optimization_result(value)


def test_failed_baseline_does_not_display_a_candidate_or_success():
    value = receipt()
    value["result"].update(status="evaluation_error", candidate=None, comparisons=[])
    value["result"]["baseline"]["status"] = "failed"
    text = render_optimization_result(value)
    assert "未形成有效比较" in text and "候选：未执行" in text


def test_legacy_contract_is_not_interpreted_as_steady_result():
    assert "不兼容" in render_optimization_result({"result": {"status": "success"}})
