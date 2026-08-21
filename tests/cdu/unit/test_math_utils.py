from __future__ import annotations

import math

import pytest

from petroleum_rto.cdu.core.math_utils import (
    NumericalError,
    bisect_root,
    clamp,
    normalize,
    rk4_step,
    weighted_average,
)


def test_basic_helpers() -> None:
    assert clamp(2.0, 0.0, 1.0) == 1.0
    assert normalize({"a": 2.0, "b": 3.0}) == {"a": 0.4, "b": 0.6}
    assert weighted_average([(10.0, 1.0), (20.0, 3.0)]) == pytest.approx(17.5)


def test_bisection_converges_or_fails_explicitly() -> None:
    root = bisect_root(lambda value: value * value - 2.0, 0.0, 2.0)
    assert root == pytest.approx(math.sqrt(2.0), rel=1e-9)
    with pytest.raises(ValueError, match="bracketed"):
        bisect_root(lambda value: value * value + 1.0, -1.0, 1.0)
    with pytest.raises(ValueError, match="finite"):
        bisect_root(lambda _value: math.nan, 0.0, 1.0)
    with pytest.raises(NumericalError, match="did not converge"):
        bisect_root(lambda value: value - 0.3, 0.0, 1.0, tolerance=1e-30, max_iterations=1)


def test_rk4_accuracy_and_validation() -> None:
    result = rk4_step(lambda _time, state: state, 0.0, (1.0,), 0.1)
    assert result[0] == pytest.approx(math.exp(0.1), rel=1e-6)
    with pytest.raises(ValueError, match="dimension"):
        rk4_step(lambda _time, _state: (1.0, 2.0), 0.0, (1.0,), 0.1)
    with pytest.raises(NumericalError, match="non-finite"):
        rk4_step(lambda _time, _state: (math.inf,), 0.0, (1.0,), 0.1)


def test_helpers_reject_nonfinite_or_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        clamp(math.nan, 0.0, 1.0)
    with pytest.raises(ValueError):
        normalize({"bad": math.inf})
    with pytest.raises(ValueError):
        weighted_average([(1.0, -1.0)])
