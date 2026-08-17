"""Deterministic open-loop command events and schedule evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _finite_number(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class CommandEvent:
    """A persistent step or a finite-duration pulse for one command target."""

    time_s: float
    target: str
    value: float
    duration_s: float | None = None

    def __post_init__(self) -> None:
        start = _finite_number(self.time_s, name="time_s")
        command_value = _finite_number(self.value, name="value")
        if start < 0.0:
            raise ValueError("time_s cannot be negative")
        if command_value < 0.0:
            raise ValueError("value must be non-negative")
        if not isinstance(self.target, str):
            raise TypeError("target must be a string")
        if not self.target.strip():
            raise ValueError("target cannot be empty")
        if self.duration_s is not None:
            duration = _finite_number(self.duration_s, name="duration_s")
            if duration <= 0.0:
                raise ValueError("duration_s must be positive when provided")
            if not math.isfinite(start + duration):
                raise ValueError("event end time must be finite")


@dataclass(frozen=True)
class CommandSchedule:
    """Frozen baseline commands plus stable time-ordered command events."""

    baseline_commands: Mapping[str, float]
    events: tuple[CommandEvent, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        copied_baseline = dict(self.baseline_commands)
        if not copied_baseline:
            raise ValueError("baseline_commands cannot be empty")
        if any(not isinstance(target, str) for target in copied_baseline):
            raise TypeError("baseline command targets must be strings")
        if any(not target.strip() for target in copied_baseline):
            raise ValueError("baseline command targets cannot be empty")
        frozen_baseline = {
            target: _finite_number(value, name=f"baseline_commands[{target!r}]")
            for target, value in copied_baseline.items()
        }
        negative_targets = sorted(
            target for target, value in frozen_baseline.items() if value < 0.0
        )
        if negative_targets:
            raise ValueError(
                "baseline commands must be non-negative: "
                + ", ".join(negative_targets)
            )
        copied_events = tuple(self.events)
        if any(not isinstance(event, CommandEvent) for event in copied_events):
            raise TypeError("events must contain only CommandEvent values")
        unknown_targets = sorted(
            {event.target for event in copied_events} - set(frozen_baseline)
        )
        if unknown_targets:
            raise ValueError(
                f"event targets are absent from baseline_commands: {', '.join(unknown_targets)}"
            )
        object.__setattr__(self, "baseline_commands", MappingProxyType(frozen_baseline))
        object.__setattr__(
            self,
            "events",
            tuple(sorted(copied_events, key=lambda event: event.time_s)),
        )

    def values_at(self, time_s: float) -> Mapping[str, float]:
        """Return frozen commands at time, using stable event order for ties."""

        query_time = _finite_number(time_s, name="time_s")
        if query_time < 0.0:
            raise ValueError("time_s cannot be negative")
        selected = dict(self.baseline_commands)
        priorities = {target: (-math.inf, -1) for target in selected}
        for index, event in enumerate(self.events):
            if event.time_s > query_time:
                break
            active = event.duration_s is None or query_time < event.time_s + event.duration_s
            if active and (event.time_s, index) >= priorities[event.target]:
                selected[event.target] = event.value
                priorities[event.target] = (event.time_s, index)
        return MappingProxyType(selected)
