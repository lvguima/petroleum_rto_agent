"""Fixed, non-scanning preset registry for the M7 runtime."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from .contracts import (
    RUN_REQUEST_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RunRequest,
    RunType,
)

EngineLayer = Literal["M2", "M3", "M4", "M6_portable"]

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class RuntimePreset:
    """One immutable pointer from a public preset id to an accepted model layer."""

    preset_id: str
    run_type: RunType
    engine_layer: EngineLayer
    description: str
    scenario_id: str | None = None
    duration_s: float | None = None
    time_step_s: float | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.preset_id):
            raise ValueError("preset_id must be a non-empty identifier")
        if not self.description.strip():
            raise ValueError("preset description must be non-empty")
        if self.scenario_id is not None and not _IDENTIFIER.fullmatch(self.scenario_id):
            raise ValueError("scenario_id must be an identifier or None")
        requires_scenario = self.run_type != "steady_recycle"
        if requires_scenario != (self.scenario_id is not None):
            raise ValueError("preset scenario_id differs from its run type")
        if (self.duration_s is None) != (self.time_step_s is None):
            raise ValueError("preset duration and time step must both be set or null")
        if self.duration_s is not None:
            if (
                isinstance(self.duration_s, bool)
                or not isinstance(self.duration_s, (int, float))
                or self.time_step_s is None
                or isinstance(self.time_step_s, bool)
                or not isinstance(self.time_step_s, (int, float))
            ):
                raise TypeError("preset duration and time step must be numeric")
            if (
                not math.isfinite(self.duration_s)
                or not math.isfinite(self.time_step_s)
                or self.duration_s <= 0.0
                or self.time_step_s <= 0.0
                or self.time_step_s > self.duration_s
            ):
                raise ValueError("preset duration and time step must form a positive grid")
        if self.run_type in {"open_loop_dynamic", "closed_loop_dynamic"} and (
            self.duration_s is None
        ):
            raise ValueError("dynamic preset requires duration and time step")
        if self.run_type == "steady_recycle" and self.duration_s is not None:
            raise ValueError("steady preset cannot carry a duration")
        expected_layer: dict[RunType, EngineLayer] = {
            "steady_recycle": "M2",
            "open_loop_dynamic": "M3",
            "closed_loop_dynamic": "M4",
            "validation_scenario": "M6_portable",
        }
        if self.engine_layer != expected_layer[self.run_type]:
            raise ValueError("preset engine layer differs from its run type")

    def to_request(self) -> RunRequest:
        """Return the deterministic default request for this preset."""

        return RunRequest(
            schema_version=RUNTIME_SCHEMA_VERSION,
            request_version=RUN_REQUEST_VERSION,
            preset_id=self.preset_id,
            run_type=self.run_type,
            random_seed=0,
            parameters={},
            overrides={},
            metadata={"preset.source": "M7_fixed_registry"},
        )


_PRESETS: Final[tuple[RuntimePreset, ...]] = (
    RuntimePreset(
        preset_id="steady-baseline",
        run_type="steady_recycle",
        engine_layer="M2",
        description="M2 steady recycle on the source-closed M5 effective basis.",
    ),
    RuntimePreset(
        preset_id="open-loop-feed-step",
        run_type="open_loop_dynamic",
        engine_layer="M3",
        scenario_id="open-loop-feed-step-v0.1.0",
        duration_s=7200.0,
        time_step_s=1.0,
        description="M3 two-hour open-loop fresh-feed step on the M5 effective basis.",
    ),
    RuntimePreset(
        preset_id="closed-loop-feed-step",
        run_type="closed_loop_dynamic",
        engine_layer="M4",
        scenario_id="closed-loop-feed-step-v0.1.0",
        duration_s=7200.0,
        time_step_s=1.0,
        description="M4 two-hour seven-loop feed-setpoint step on the M5 effective basis.",
    ),
    RuntimePreset(
        preset_id="m6-abnormal-pump-trip",
        run_type="validation_scenario",
        engine_layer="M6_portable",
        scenario_id="limited_pump_around_1_trip",
        duration_s=600.0,
        time_step_s=1.0,
        description=(
            "Portable replay of the packaged M6 pump-around-1 trip scenario; "
            "not a fresh full source-closed M6 matrix run."
        ),
    ),
    RuntimePreset(
        preset_id="m6-structural-rejection",
        run_type="validation_scenario",
        engine_layer="M6_portable",
        scenario_id="rejected_stripping_steam_request",
        description=(
            "Portable pre-solver replay of the packaged M6 unsupported stripping-steam request."
        ),
    ),
)

PRESET_IDS: Final[tuple[str, ...]] = tuple(item.preset_id for item in _PRESETS)
PRESET_REGISTRY: Final[MappingProxyType[str, RuntimePreset]] = MappingProxyType(
    {item.preset_id: item for item in _PRESETS}
)


def list_presets() -> tuple[RuntimePreset, ...]:
    """Return every preset in stable presentation and execution order."""

    return _PRESETS


def get_preset(preset_id: str) -> RuntimePreset:
    """Resolve one exact preset id without scanning a filesystem or entry points."""

    if not isinstance(preset_id, str):
        raise TypeError("preset_id must be a string")
    try:
        return PRESET_REGISTRY[preset_id]
    except KeyError as exc:
        raise KeyError(f"unknown runtime preset {preset_id!r}") from exc


def load_preset(preset_id: str) -> RunRequest:
    """Load one preset as the stable immutable request accepted by the runtime."""

    return get_preset(preset_id).to_request()


__all__ = [
    "PRESET_IDS",
    "PRESET_REGISTRY",
    "EngineLayer",
    "RuntimePreset",
    "get_preset",
    "list_presets",
    "load_preset",
]
