"""Dependency-free numerical helpers used by the reduced-order model."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence


class NumericalError(RuntimeError):
    """Raised when a numerical routine cannot return a trustworthy result."""


class ConvergenceError(NumericalError):
    """Raised when a numerical or prerequisite solve explicitly does not converge."""


def _require_finite(value: float, *, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def clamp(value: float, lower: float, upper: float) -> float:
    """Return *value* limited to the closed interval ``[lower, upper]``."""

    _require_finite(value, name="value")
    _require_finite(lower, name="lower")
    _require_finite(upper, name="upper")
    if lower > upper:
        raise ValueError("lower bound cannot exceed upper bound")
    return max(lower, min(upper, value))


def logistic(value: float) -> float:
    """Numerically stable logistic function."""

    _require_finite(value, name="value")
    if value >= 0.0:
        exponent = math.exp(-value)
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def normalize(values: Mapping[str, float], *, tolerance: float = 1e-15) -> dict[str, float]:
    """Normalize a non-negative mapping so its values sum to one."""

    if any((not math.isfinite(value)) or value < 0.0 for value in values.values()):
        raise ValueError("composition values must be finite and non-negative")
    total = sum(values.values())
    if total <= tolerance:
        raise ValueError("composition total must be positive")
    return {key: value / total for key, value in values.items()}


def bisect_root(
    function: Callable[[float], float],
    lower: float,
    upper: float,
    *,
    tolerance: float = 1e-10,
    max_iterations: int = 200,
) -> float:
    """Solve a bracketed scalar root with deterministic bisection."""

    _require_finite(lower, name="lower")
    _require_finite(upper, name="upper")
    _require_finite(tolerance, name="tolerance")
    if lower >= upper:
        raise ValueError("lower must be less than upper")
    if tolerance <= 0.0:
        raise ValueError("tolerance must be positive")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")
    f_lower = function(lower)
    f_upper = function(upper)
    _require_finite(f_lower, name="function(lower)")
    _require_finite(f_upper, name="function(upper)")
    if f_lower == 0.0:
        return lower
    if f_upper == 0.0:
        return upper
    if f_lower * f_upper > 0.0:
        raise ValueError("root is not bracketed")
    for _ in range(max_iterations):
        midpoint = 0.5 * (lower + upper)
        f_midpoint = function(midpoint)
        _require_finite(f_midpoint, name="function(midpoint)")
        if abs(f_midpoint) <= tolerance or (upper - lower) <= tolerance:
            return midpoint
        if f_lower * f_midpoint <= 0.0:
            upper = midpoint
            f_upper = f_midpoint
        else:
            lower = midpoint
            f_lower = f_midpoint
    raise ConvergenceError(f"bisection did not converge after {max_iterations} iterations")


def rk4_step(
    derivative: Callable[[float, Sequence[float]], Sequence[float]],
    time_s: float,
    state: Sequence[float],
    dt_s: float,
) -> tuple[float, ...]:
    """Advance an ODE one fixed RK4 step."""

    _require_finite(time_s, name="time_s")
    _require_finite(dt_s, name="dt_s")
    if dt_s <= 0.0:
        raise ValueError("dt_s must be positive")
    initial_state = tuple(state)
    if not initial_state:
        raise ValueError("state cannot be empty")
    if any(not math.isfinite(value) for value in initial_state):
        raise ValueError("state values must be finite")

    def evaluate(stage_time_s: float, stage_state: Sequence[float]) -> tuple[float, ...]:
        slopes = tuple(derivative(stage_time_s, stage_state))
        if len(slopes) != len(initial_state):
            raise ValueError("derivative dimension must match state dimension")
        if any(not math.isfinite(value) for value in slopes):
            raise NumericalError("derivative returned a non-finite value")
        return slopes

    k1 = evaluate(time_s, initial_state)
    s2 = tuple(value + 0.5 * dt_s * slope for value, slope in zip(initial_state, k1))
    if any(not math.isfinite(value) for value in s2):
        raise NumericalError("RK4 stage 2 state is non-finite")
    k2 = evaluate(time_s + 0.5 * dt_s, s2)
    s3 = tuple(value + 0.5 * dt_s * slope for value, slope in zip(initial_state, k2))
    if any(not math.isfinite(value) for value in s3):
        raise NumericalError("RK4 stage 3 state is non-finite")
    k3 = evaluate(time_s + 0.5 * dt_s, s3)
    s4 = tuple(value + dt_s * slope for value, slope in zip(initial_state, k3))
    if any(not math.isfinite(value) for value in s4):
        raise NumericalError("RK4 stage 4 state is non-finite")
    k4 = evaluate(time_s + dt_s, s4)
    result = tuple(
        value + dt_s * (a + 2.0 * b + 2.0 * c + d) / 6.0
        for value, a, b, c, d in zip(initial_state, k1, k2, k3, k4)
    )
    if any(not math.isfinite(value) for value in result):
        raise NumericalError("RK4 result is non-finite")
    return result


def weighted_average(pairs: Iterable[tuple[float, float]], *, default: float = 0.0) -> float:
    """Return a flow-weighted average from ``(value, weight)`` pairs."""

    weighted_sum = 0.0
    total_weight = 0.0
    for value, weight in pairs:
        _require_finite(value, name="value")
        _require_finite(weight, name="weight")
        if weight < 0.0:
            raise ValueError("weights must be non-negative")
        weighted_sum += value * weight
        total_weight += weight
    return default if total_weight == 0.0 else weighted_sum / total_weight
