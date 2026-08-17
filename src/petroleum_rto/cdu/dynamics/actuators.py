"""First-order actuator dynamics with travel and slew-rate limits."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _finite_number(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class ActuatorSpec:
    """Parameters for one bounded first-order actuator."""

    time_constant_s: float
    lower: float
    upper: float
    rate_limit_per_s: float | None = None

    def __post_init__(self) -> None:
        time_constant = _finite_number(self.time_constant_s, name="time_constant_s")
        lower = _finite_number(self.lower, name="lower")
        upper = _finite_number(self.upper, name="upper")
        if time_constant <= 0.0:
            raise ValueError("time_constant_s must be positive")
        if lower > upper:
            raise ValueError("lower cannot exceed upper")
        if self.rate_limit_per_s is not None:
            rate_limit = _finite_number(self.rate_limit_per_s, name="rate_limit_per_s")
            if rate_limit <= 0.0:
                raise ValueError("rate_limit_per_s must be positive when provided")

    def derivative(
        self,
        actual: float,
        command: float,
        failure_position: float | None = None,
    ) -> float:
        """Return actuator velocity after target and symmetric rate limiting."""

        actual_value = _finite_number(actual, name="actual")
        command_value = _finite_number(command, name="command")
        if not self.lower <= actual_value <= self.upper:
            raise ValueError("actual actuator position is outside its bounds")
        target_value = command_value
        if failure_position is not None:
            target_value = _finite_number(failure_position, name="failure_position")
        target_value = min(max(target_value, self.lower), self.upper)
        velocity = (target_value - actual_value) / self.time_constant_s
        if self.rate_limit_per_s is not None:
            velocity = min(max(velocity, -self.rate_limit_per_s), self.rate_limit_per_s)
        return velocity
