"""First-order measurement dynamics used by the open-loop simulator."""

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
class SensorSpec:
    """Parameters for a first-order sensor with an optional hold state."""

    time_constant_s: float

    def __post_init__(self) -> None:
        time_constant = _finite_number(self.time_constant_s, name="time_constant_s")
        if time_constant <= 0.0:
            raise ValueError("time_constant_s must be positive")

    def derivative(self, measured: float, true_value: float, held: bool = False) -> float:
        """Return the measurement derivative, or zero while the sensor is held."""

        measured_value = _finite_number(measured, name="measured")
        process_value = _finite_number(true_value, name="true_value")
        if not isinstance(held, bool):
            raise TypeError("held must be a boolean")
        if held:
            return 0.0
        return (process_value - measured_value) / self.time_constant_s
