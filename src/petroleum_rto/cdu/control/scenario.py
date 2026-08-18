"""Strict versioned scenarios for the M4 feedback simulation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ..core.config import ConfigurationError, load_json, strict_keys
from ..core.units import parse_quantity

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{field_name} must be a non-empty identifier")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class SetpointEvent:
    """One right-continuous target-ratio event for a named control loop."""

    time_s: float
    loop_id: str
    setpoint_ratio: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.time_s, bool)
            or not isinstance(self.time_s, (int, float))
            or not math.isfinite(self.time_s)
            or self.time_s < 0.0
        ):
            raise ValueError("setpoint event time_s must be finite and non-negative")
        if not isinstance(self.loop_id, str) or not _IDENTIFIER.fullmatch(
            self.loop_id
        ):
            raise ValueError("setpoint event loop_id must be an identifier")
        if (
            isinstance(self.setpoint_ratio, bool)
            or not isinstance(self.setpoint_ratio, (int, float))
            or not math.isfinite(self.setpoint_ratio)
            or self.setpoint_ratio <= 0.0
        ):
            raise ValueError("setpoint event ratio must be finite and positive")
        object.__setattr__(self, "time_s", float(self.time_s))
        object.__setattr__(self, "setpoint_ratio", float(self.setpoint_ratio))

    def as_dict(self) -> dict[str, object]:
        return {
            "time_s": self.time_s,
            "target": f"{self.loop_id}.setpoint_ratio",
            "value": self.setpoint_ratio,
        }


@dataclass(frozen=True)
class ClosedLoopScenarioConfig:
    """Validated M4 scenario separated from M3 actuator schedules."""

    schema_version: str
    scenario_version: str
    control_version: str
    config_version: str
    case_version: str
    model_version: str
    parameter_set_version: str
    name: str
    duration_s: float
    time_step_s: float
    events: tuple[SetpointEvent, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration_s <= 0.0 or self.time_step_s <= 0.0:
            raise ValueError("scenario duration and time step must be positive")
        if self.time_step_s > self.duration_s:
            raise ValueError("scenario time step cannot exceed duration")
        step_ratio = self.duration_s / self.time_step_s
        if not math.isclose(step_ratio, round(step_ratio), rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("scenario duration must be an integer multiple of time step")
        if any(event.time_s > self.duration_s for event in self.events):
            raise ValueError("setpoint event time cannot exceed scenario duration")
        if any(
            later.time_s < earlier.time_s
            for earlier, later in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("setpoint events must be ordered by time")
        if self.metadata.get("synthetic") != "true":
            raise ValueError("closed-loop scenario metadata.synthetic must be 'true'")
        if not self.metadata.get("purpose", "").strip():
            raise ValueError("closed-loop scenario purpose must be non-empty")
        object.__setattr__(self, "duration_s", float(self.duration_s))
        object.__setattr__(self, "time_step_s", float(self.time_step_s))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> ClosedLoopScenarioConfig:
        strict_keys(
            value,
            required={
                "schema_version",
                "scenario_version",
                "control_version",
                "config_version",
                "case_version",
                "model_version",
                "parameter_set_version",
                "name",
                "duration",
                "time_step",
                "events",
                "metadata",
            },
            context="closed-loop scenario configuration",
        )
        raw_events = value["events"]
        if not isinstance(raw_events, list):
            raise ConfigurationError("closed-loop scenario events must be a list")
        events: list[SetpointEvent] = []
        for index, raw_event in enumerate(raw_events):
            if not isinstance(raw_event, Mapping) or any(
                not isinstance(key, str) for key in raw_event
            ):
                raise ConfigurationError(f"scenario event {index} must be an object")
            strict_keys(
                raw_event,
                required={"time", "target", "value"},
                context=f"closed-loop scenario event {index}",
            )
            target = _identifier(
                raw_event["target"],
                field_name=f"scenario event {index} target",
            )
            suffix = ".setpoint_ratio"
            if not target.endswith(suffix):
                raise ConfigurationError(
                    f"scenario event {index} must target '<loop>.setpoint_ratio'"
                )
            loop_id = target[: -len(suffix)]
            raw_ratio = raw_event["value"]
            if isinstance(raw_ratio, bool) or not isinstance(raw_ratio, (int, float)):
                raise ConfigurationError(
                    f"scenario event {index} value must be numeric"
                )
            events.append(
                SetpointEvent(
                    time_s=parse_quantity(raw_event["time"], dimension="time"),
                    loop_id=loop_id,
                    setpoint_ratio=float(raw_ratio),
                )
            )
        raw_metadata = value["metadata"]
        if not isinstance(raw_metadata, Mapping) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in raw_metadata.items()
        ):
            raise ConfigurationError("closed-loop scenario metadata must map strings")
        try:
            return cls(
                schema_version=_identifier(
                    value["schema_version"], field_name="schema_version"
                ),
                scenario_version=_identifier(
                    value["scenario_version"], field_name="scenario_version"
                ),
                control_version=_identifier(
                    value["control_version"], field_name="control_version"
                ),
                config_version=_identifier(
                    value["config_version"], field_name="config_version"
                ),
                case_version=_identifier(
                    value["case_version"], field_name="case_version"
                ),
                model_version=_identifier(
                    value["model_version"], field_name="model_version"
                ),
                parameter_set_version=_identifier(
                    value["parameter_set_version"],
                    field_name="parameter_set_version",
                ),
                name=_text(value["name"], field_name="name"),
                duration_s=parse_quantity(value["duration"], dimension="time"),
                time_step_s=parse_quantity(value["time_step"], dimension="time"),
                events=tuple(events),
                metadata={str(key): str(item) for key, item in raw_metadata.items()},
            )
        except ConfigurationError:
            raise
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid closed-loop scenario: {exc}") from exc

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scenario_version": self.scenario_version,
            "control_version": self.control_version,
            "config_version": self.config_version,
            "case_version": self.case_version,
            "model_version": self.model_version,
            "parameter_set_version": self.parameter_set_version,
            "name": self.name,
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "events": [event.as_dict() for event in self.events],
            "metadata": dict(self.metadata),
        }


def load_closed_loop_scenario(path: Path) -> ClosedLoopScenarioConfig:
    """Load a strict M4 scenario from one UTF-8 JSON file."""

    return ClosedLoopScenarioConfig.from_mapping(load_json(path))
