from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

from petroleum_rto.cdu.validation.uncertainty import (
    EngineeringInputInterval,
    InputSensitivitySpec,
    LocalSensitivityAnalysis,
    OutputSensitivitySpec,
    assert_uncertainty_not_narrower,
    propagate_uncertainty,
    run_local_sensitivity,
)

_BASIS = "a" * 64


def _linear_specs() -> tuple[
    tuple[InputSensitivitySpec, ...],
    tuple[OutputSensitivitySpec, ...],
]:
    return (
        (
            InputSensitivitySpec(
                input_id="feed_ratio",
                reference_value=2.0,
                central_step=0.1,
                normalization_scale=2.0,
                unit="ratio",
            ),
            InputSensitivitySpec(
                input_id="cooling_ratio",
                reference_value=4.0,
                central_step=0.2,
                normalization_scale=4.0,
                unit="ratio",
            ),
        ),
        (
            OutputSensitivitySpec(
                output_id="fuel_duty",
                normalization_scale=10.0,
                unit="W",
                numerical_margin=0.25,
            ),
            OutputSensitivitySpec(
                output_id="quality_proxy",
                normalization_scale=8.0,
                unit="K",
            ),
        ),
    )


def _linear_evaluator(inputs: Mapping[str, float]) -> Mapping[str, float]:
    feed = inputs["feed_ratio"]
    cooling = inputs["cooling_ratio"]
    return {
        "fuel_duty": 3.0 * feed - 2.0 * cooling + 5.0,
        "quality_proxy": feed * cooling,
    }


def test_sensitivity_specs_are_frozen_finite_and_positive() -> None:
    inputs, outputs = _linear_specs()
    with pytest.raises(FrozenInstanceError):
        inputs[0].central_step = 1.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="positive"):
        InputSensitivitySpec("bad", 1.0, 0.0, 1.0)
    with pytest.raises(ValueError, match="non-negative"):
        OutputSensitivitySpec("bad", 1.0, numerical_margin=-1.0)
    with pytest.raises(ValueError, match="finite"):
        EngineeringInputInterval("bad", 0.0, math.inf)
    with pytest.raises(ValueError, match="at least one"):
        EngineeringInputInterval("bad", 0.0, 1.0, confidence_multiplier=0.5)
    assert outputs[0].numerical_margin == 0.25


def test_fixed_reference_central_difference_and_normalized_matrix_are_exact() -> None:
    inputs, outputs = _linear_specs()
    analysis = run_local_sensitivity(
        inputs,
        outputs,
        _linear_evaluator,
        basis_fingerprint=_BASIS,
    )

    assert analysis.status == "success"
    assert analysis.complete
    assert analysis.baseline_outputs == {
        "fuel_duty": 3.0,
        "quality_proxy": 8.0,
    }
    assert len(analysis.evaluations) == 5
    assert tuple(item.label for item in analysis.evaluations) == (
        "baseline",
        "feed_ratio:minus",
        "feed_ratio:plus",
        "cooling_ratio:minus",
        "cooling_ratio:plus",
    )
    assert analysis.matrix[0] == pytest.approx((3.0, -2.0))
    assert analysis.matrix[1] == pytest.approx((4.0, 2.0))
    assert analysis.normalized_matrix[0] == pytest.approx((0.6, -0.8))
    assert analysis.normalized_matrix[1] == pytest.approx((1.0, 1.0))
    assert all(item.status == "success" for item in analysis.evaluations)


def test_local_sensitivity_is_fully_repeatable() -> None:
    inputs, outputs = _linear_specs()
    first = run_local_sensitivity(
        inputs,
        outputs,
        _linear_evaluator,
        basis_fingerprint=_BASIS,
    )
    second = run_local_sensitivity(
        inputs,
        outputs,
        _linear_evaluator,
        basis_fingerprint=_BASIS,
    )

    assert first == second
    assert first.as_dict() == second.as_dict()
    assert first.analysis_fingerprint == second.analysis_fingerprint


def test_one_failed_difference_pair_is_isolated_and_other_columns_survive() -> None:
    calls: list[dict[str, float]] = []
    inputs = (
        InputSensitivitySpec("fragile", 0.0, 0.1, 1.0),
        InputSensitivitySpec("healthy", 0.0, 0.2, 1.0),
    )
    outputs = (OutputSensitivitySpec("result", 1.0),)

    def evaluator(values: Mapping[str, float]) -> Mapping[str, float]:
        calls.append(dict(values))
        if values["fragile"] > 0.0:
            raise RuntimeError("synthetic candidate failed conservation")
        return {"result": 2.0 * values["fragile"] + 4.0 * values["healthy"]}

    analysis = run_local_sensitivity(
        inputs,
        outputs,
        evaluator,
        basis_fingerprint=_BASIS,
    )

    assert len(calls) == 5
    assert analysis.status == "partial"
    assert analysis.matrix[0][0] is None
    assert analysis.matrix[0][1] == pytest.approx(4.0)
    failed = analysis.evaluations[2]
    assert failed.label == "fragile:plus"
    assert failed.status == "failed"
    assert failed.failure_reason == (
        "RuntimeError: synthetic candidate failed conservation"
    )
    with pytest.raises(ValueError, match="complete sensitivity"):
        propagate_uncertainty(
            analysis,
            (
                EngineeringInputInterval("fragile", -0.1, 0.1),
                EngineeringInputInterval("healthy", -0.1, 0.1),
            ),
        )


def test_invalid_evaluator_outputs_are_failed_records_not_valid_numbers() -> None:
    inputs = (InputSensitivitySpec("input", 1.0, 0.1, 1.0),)
    outputs = (OutputSensitivitySpec("output", 1.0),)

    def evaluator(values: Mapping[str, float]) -> Mapping[str, float]:
        if values["input"] > 1.0:
            return {"output": math.nan}
        return {"output": values["input"]}

    analysis = run_local_sensitivity(
        inputs,
        outputs,
        evaluator,
        basis_fingerprint=_BASIS,
    )

    assert analysis.status == "failed"
    assert analysis.matrix == ((None,),)
    assert analysis.evaluations[-1].status == "failed"
    assert analysis.evaluations[-1].outputs == {}
    assert "must be finite" in (analysis.evaluations[-1].failure_reason or "")


def _cancellation_analysis(
    *,
    basis: str = _BASIS,
    second_coefficient: float = -1.0,
) -> LocalSensitivityAnalysis:
    inputs = (
        InputSensitivitySpec("positive", 0.0, 0.1, 1.0),
        InputSensitivitySpec("negative", 0.0, 0.1, 1.0),
    )
    outputs = (
        OutputSensitivitySpec(
            "net_output",
            1.0,
            unit="kg/s",
            numerical_margin=0.25,
        ),
    )

    def evaluator(values: Mapping[str, float]) -> Mapping[str, float]:
        return {
            "net_output": values["positive"]
            + second_coefficient * values["negative"]
        }

    return run_local_sensitivity(inputs, outputs, evaluator, basis_fingerprint=basis)


def test_absolute_jacobian_sum_prevents_signed_cancellation() -> None:
    analysis = _cancellation_analysis()
    result = propagate_uncertainty(
        analysis,
        (
            EngineeringInputInterval("positive", -1.0, 1.0),
            EngineeringInputInterval("negative", -1.0, 1.0),
        ),
    )
    output = result.output_intervals[0]

    assert analysis.matrix[0] == pytest.approx((1.0, -1.0))
    assert output.contributions == {
        "positive": pytest.approx(1.0),
        "negative": pytest.approx(1.0),
    }
    assert output.numerical_margin == 0.25
    assert output.radius == pytest.approx(2.25)
    assert output.lower == pytest.approx(-2.25)
    assert output.upper == pytest.approx(2.25)


def test_nested_input_ranges_cannot_shrink_any_output_interval() -> None:
    analysis = _cancellation_analysis()
    narrow = propagate_uncertainty(
        analysis,
        (
            EngineeringInputInterval("positive", -0.5, 0.5),
            EngineeringInputInterval("negative", -0.25, 0.25),
        ),
    )
    wide = propagate_uncertainty(
        analysis,
        (
            EngineeringInputInterval("positive", -1.0, 1.0),
            EngineeringInputInterval("negative", -0.5, 0.5),
        ),
    )

    wide.assert_not_narrower_than(narrow)
    assert_uncertainty_not_narrower(wide, narrow)
    assert wide.output_intervals[0].lower <= narrow.output_intervals[0].lower
    assert wide.output_intervals[0].upper >= narrow.output_intervals[0].upper
    assert wide.output_intervals[0].width >= narrow.output_intervals[0].width

    with pytest.raises(ValueError, match="not nested"):
        narrow.assert_not_narrower_than(wide)


def test_lower_confidence_multiplier_cannot_shrink_interval() -> None:
    analysis = _cancellation_analysis()
    bounded = propagate_uncertainty(
        analysis,
        (
            EngineeringInputInterval("positive", -0.5, 0.5),
            EngineeringInputInterval("negative", -0.5, 0.5),
        ),
    )
    low_confidence = propagate_uncertainty(
        analysis,
        (
            EngineeringInputInterval(
                "positive",
                -0.5,
                0.5,
                confidence_multiplier=2.0,
                confidence_label="low",
            ),
            EngineeringInputInterval(
                "negative",
                -0.5,
                0.5,
                confidence_multiplier=2.0,
                confidence_label="low",
            ),
        ),
    )

    low_confidence.assert_not_narrower_than(bounded)
    assert low_confidence.input_radii == {
        "positive": pytest.approx(1.0),
        "negative": pytest.approx(1.0),
    }
    assert low_confidence.output_intervals[0].radius > bounded.output_intervals[0].radius


def test_comparison_rejects_different_bases_or_jacobians() -> None:
    intervals = (
        EngineeringInputInterval("positive", -0.5, 0.5),
        EngineeringInputInterval("negative", -0.5, 0.5),
    )
    first = propagate_uncertainty(_cancellation_analysis(), intervals)
    different_basis = propagate_uncertainty(
        _cancellation_analysis(basis="b" * 64),
        intervals,
    )
    with pytest.raises(ValueError, match="different bases"):
        first.assert_not_narrower_than(different_basis)

    different_jacobian = propagate_uncertainty(
        _cancellation_analysis(second_coefficient=-2.0),
        intervals,
    )
    with pytest.raises(ValueError, match="different Jacobians"):
        first.assert_not_narrower_than(different_jacobian)


def test_interval_set_must_match_inputs_and_contain_fixed_reference() -> None:
    analysis = _cancellation_analysis()
    with pytest.raises(ValueError, match="ids differ"):
        propagate_uncertainty(
            analysis,
            (EngineeringInputInterval("positive", -1.0, 1.0),),
        )
    with pytest.raises(ValueError, match="must contain its reference"):
        propagate_uncertainty(
            analysis,
            (
                EngineeringInputInterval("positive", 1.0, 2.0),
                EngineeringInputInterval("negative", -1.0, 1.0),
            ),
        )


def test_propagation_result_and_fingerprint_are_repeatable() -> None:
    analysis = _cancellation_analysis()
    intervals = (
        EngineeringInputInterval("negative", -1.0, 1.0),
        EngineeringInputInterval("positive", -1.0, 1.0),
    )
    first = propagate_uncertainty(analysis, intervals)
    second = propagate_uncertainty(analysis, tuple(reversed(intervals)))

    assert first == second
    assert first.as_dict() == second.as_dict()
    assert first.result_fingerprint == second.result_fingerprint
    assert first.interval_semantics == "deterministic_engineering_envelope"
