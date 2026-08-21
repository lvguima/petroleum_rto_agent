"""Controlled custom inputs and solve-free runtime previews.

The packaged presets remain immutable templates.  This module is the only place
where public custom input names are translated into the model's SI objects.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final, Literal, cast

from ..control.scenario import ClosedLoopScenarioConfig, SetpointEvent
from ..core.config import (
    CaseConfig,
    ModelConfig,
    ScenarioConfig,
    canonical_fingerprint,
    validate_config_compatibility,
)
from ..core.types import MaterialStream
from ..dynamics.state import (
    ACTUATOR_STATE_NAMES,
    DynamicState,
    InventoryState,
)
from ..properties.components import ALL_COMPONENTS
from .contracts import CUSTOM_INPUT_VERSION, RunRequest, RuntimeInputEvent, RunType
from .presets import RuntimePreset, get_preset
from .resources import RuntimeResourceBundle, load_runtime_resource_bundle

InputKind = Literal["parameter", "override", "initial_state"]

_DYNAMIC_TYPES: Final[frozenset[RunType]] = frozenset({"open_loop_dynamic", "closed_loop_dynamic"})
_MODEL_TYPES: Final[tuple[RunType, ...]] = (
    "steady_recycle",
    "open_loop_dynamic",
    "closed_loop_dynamic",
)
_DYNAMIC_RUN_TYPES: Final[tuple[RunType, ...]] = (
    "open_loop_dynamic",
    "closed_loop_dynamic",
)
_ATMOSPHERIC_PRESSURE_PA: Final[float] = 101_325.0


@dataclass(frozen=True)
class RuntimeInputSpec:
    """One versioned public input name and its broad numeric domain."""

    input_id: str
    kind: InputKind
    display_unit: str
    si_unit: str
    minimum: float
    maximum: float
    run_types: tuple[RunType, ...]
    description: str

    def __post_init__(self) -> None:
        if not self.input_id or not self.description.strip():
            raise ValueError("runtime input specification text cannot be empty")
        if self.minimum >= self.maximum:
            raise ValueError("runtime input specification range must increase")
        if not self.run_types:
            raise ValueError("runtime input specification needs an applicable run type")

    def validate(self, value: float, *, run_type: RunType) -> float:
        if run_type not in self.run_types:
            raise ValueError(f"input {self.input_id!r} is not available for run type {run_type!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"input {self.input_id!r} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"input {self.input_id!r} must be finite")
        if number < self.minimum or number > self.maximum:
            raise ValueError(
                f"input {self.input_id!r} must be between {self.minimum:g} and "
                f"{self.maximum:g} {self.display_unit}"
            )
        return number

    def as_dict(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            "kind": self.kind,
            "display_unit": self.display_unit,
            "si_unit": self.si_unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "run_types": list(self.run_types),
            "description": self.description,
        }


def _spec(
    input_id: str,
    kind: InputKind,
    display_unit: str,
    si_unit: str,
    minimum: float,
    maximum: float,
    description: str,
    *,
    run_types: tuple[RunType, ...] = _MODEL_TYPES,
) -> RuntimeInputSpec:
    return RuntimeInputSpec(
        input_id,
        kind,
        display_unit,
        si_unit,
        minimum,
        maximum,
        run_types,
        description,
    )


_INPUT_SPECS: Final[tuple[RuntimeInputSpec, ...]] = (
    _spec(
        "feed.mass_flow_t_h", "parameter", "t/h", "kg/s", 0.001, 2_000.0, "Fresh crude mass flow."
    ),
    _spec(
        "feed.temperature_c", "parameter", "degC", "K", -100.0, 500.0, "Fresh crude temperature."
    ),
    _spec(
        "feed.pressure_mpa_g",
        "parameter",
        "MPa(g)",
        "Pa(a)",
        -0.1,
        10.0,
        "Fresh crude gauge pressure.",
    ),
    _spec(
        "feed.salt_mass_flow_kg_s",
        "parameter",
        "kg/s",
        "kg/s",
        0.0,
        10.0,
        "Fresh crude salt tracer flow.",
    ),
    *(
        _spec(
            f"feed.mass_fraction.{component}",
            "parameter",
            "fraction",
            "fraction",
            0.0,
            1.0,
            f"Fresh crude {component} pseudo-component mass fraction.",
        )
        for component in ALL_COMPONENTS
    ),
    _spec(
        "operating.flash_temperature_c",
        "parameter",
        "degC",
        "K",
        -100.0,
        500.0,
        "Flash operating temperature.",
    ),
    _spec(
        "operating.flash_pressure_mpa_g",
        "parameter",
        "MPa(g)",
        "Pa(a)",
        -0.1,
        5.0,
        "Flash gauge pressure.",
    ),
    _spec(
        "operating.furnace_outlet_temperature_c",
        "parameter",
        "degC",
        "K",
        -100.0,
        370.0,
        "Furnace outlet target temperature.",
    ),
    _spec(
        "operating.tower_top_temperature_c",
        "parameter",
        "degC",
        "K",
        -100.0,
        350.0,
        "Tower top temperature.",
    ),
    _spec(
        "operating.tower_top_pressure_mpa_g",
        "parameter",
        "MPa(g)",
        "Pa(a)",
        -0.1,
        3.0,
        "Tower top gauge pressure.",
    ),
    _spec(
        "operating.condenser_temperature_c",
        "parameter",
        "degC",
        "K",
        -100.0,
        250.0,
        "Condenser temperature.",
    ),
    _spec(
        "operating.ambient_temperature_c",
        "parameter",
        "degC",
        "K",
        -100.0,
        100.0,
        "Ambient temperature.",
    ),
    _spec(
        "operation.wash_water_ratio",
        "parameter",
        "fraction",
        "fraction",
        0.0,
        0.5,
        "Wash-water to crude mass ratio.",
    ),
    _spec(
        "operation.reflux_ratio", "parameter", "ratio", "ratio", 0.0, 5.0, "Column reflux ratio."
    ),
    _spec(
        "operation.pump_around_1_duty_mw",
        "parameter",
        "MW",
        "W",
        0.0,
        200.0,
        "Pump-around 1 removed duty.",
    ),
    _spec(
        "operation.pump_around_2_duty_mw",
        "parameter",
        "MW",
        "W",
        0.0,
        200.0,
        "Pump-around 2 removed duty.",
    ),
    _spec(
        "operation.pump_around_3_duty_mw",
        "parameter",
        "MW",
        "W",
        0.0,
        200.0,
        "Pump-around 3 removed duty.",
    ),
    _spec(
        "column.cut_3_temperature_c",
        "override",
        "degC",
        "K",
        -100.0,
        500.0,
        "Third pseudo-cut temperature.",
    ),
    _spec(
        "column.cut_4_temperature_c",
        "override",
        "degC",
        "K",
        -100.0,
        500.0,
        "Fourth pseudo-cut temperature.",
    ),
    *(
        _spec(
            f"dynamic.{name}",
            "override",
            "s",
            "s",
            0.001,
            1_000_000.0,
            f"Dynamic model {name.replace('_', ' ')}.",
            run_types=_DYNAMIC_RUN_TYPES,
        )
        for name in (
            "furnace_time_constant_s",
            "preheater_time_constant_s",
            "condenser_time_constant_s",
            "tower_temperature_time_constant_s",
            "actuator_time_constant_s",
            "sensor_time_constant_s",
            "flash_residence_time_s",
            "reflux_drum_residence_time_s",
            "tower_bottom_residence_time_s",
        )
    ),
    _spec(
        "dynamic.top_gas_volume_m3",
        "override",
        "m3",
        "m3",
        0.001,
        1_000_000.0,
        "Reduced tower-top gas volume.",
        run_types=_DYNAMIC_RUN_TYPES,
    ),
    _spec(
        "inventory.flash_drum_ratio",
        "initial_state",
        "nominal ratio",
        "nominal ratio",
        0.1,
        10.0,
        "Initial flash-drum bulk and salt inventory ratio.",
        run_types=_DYNAMIC_RUN_TYPES,
    ),
    _spec(
        "inventory.reflux_drum_ratio",
        "initial_state",
        "nominal ratio",
        "nominal ratio",
        0.1,
        10.0,
        "Initial reflux-drum bulk and salt inventory ratio.",
        run_types=_DYNAMIC_RUN_TYPES,
    ),
    _spec(
        "inventory.tower_bottom_ratio",
        "initial_state",
        "nominal ratio",
        "nominal ratio",
        0.1,
        10.0,
        "Initial tower-bottom bulk and salt inventory ratio.",
        run_types=_DYNAMIC_RUN_TYPES,
    ),
)
_SPEC_BY_ID: Final[Mapping[str, RuntimeInputSpec]] = MappingProxyType(
    {item.input_id: item for item in _INPUT_SPECS}
)


def list_runtime_input_specs(
    preset_id: str | None = None,
) -> tuple[RuntimeInputSpec, ...]:
    """List the stable whitelist, optionally filtered by one preset."""

    if preset_id is None:
        return _INPUT_SPECS
    run_type = get_preset(preset_id).run_type
    return tuple(item for item in _INPUT_SPECS if run_type in item.run_types)


def _to_si(input_id: str, value: float) -> float:
    if input_id.endswith(("_c", "temperature_c")):
        return value + 273.15
    if input_id.endswith("_mpa_g"):
        return value * 1_000_000.0 + _ATMOSPHERIC_PRESSURE_PA
    if input_id == "feed.mass_flow_t_h":
        return value / 3.6
    if input_id.endswith("_duty_mw"):
        return value * 1_000_000.0
    return value


def _from_si(input_id: str, value: float) -> float:
    if input_id.endswith(("_c", "temperature_c")):
        return value - 273.15
    if input_id.endswith("_mpa_g"):
        return (value - _ATMOSPHERIC_PRESSURE_PA) / 1_000_000.0
    if input_id == "feed.mass_flow_t_h":
        return value * 3.6
    if input_id.endswith("_duty_mw"):
        return value / 1_000_000.0
    return value


def _base_value(spec: RuntimeInputSpec, model: ModelConfig, case: CaseConfig) -> float:
    name = spec.input_id
    if name == "feed.mass_flow_t_h":
        value = case.feed.mass_flow_kg_s
    elif name == "feed.temperature_c":
        value = case.feed.temperature_k
    elif name == "feed.pressure_mpa_g":
        value = case.feed.pressure_pa
    elif name == "feed.salt_mass_flow_kg_s":
        value = case.feed.salt_mass_flow_kg_s
    elif name.startswith("feed.mass_fraction."):
        value = case.feed.mass_fractions[name.removeprefix("feed.mass_fraction.")]
    elif name.startswith("operating."):
        field_name = {
            "operating.flash_temperature_c": "flash_temperature_k",
            "operating.flash_pressure_mpa_g": "flash_pressure_pa",
            "operating.furnace_outlet_temperature_c": "furnace_outlet_temperature_k",
            "operating.tower_top_temperature_c": "tower_top_temperature_k",
            "operating.tower_top_pressure_mpa_g": "tower_top_pressure_pa",
            "operating.condenser_temperature_c": "condenser_temperature_k",
            "operating.ambient_temperature_c": "ambient_temperature_k",
        }[name]
        value = case.operating_conditions[field_name]
    elif name == "operation.wash_water_ratio":
        value = cast(float, model.equipment["desalter"]["wash_water_ratio"])
    elif name == "operation.reflux_ratio":
        value = cast(float, model.equipment["recycle"]["reflux_ratio"])
    elif name.startswith("operation.pump_around_"):
        index = int(name.removeprefix("operation.pump_around_")[0]) - 1
        duties = cast(
            tuple[float, ...] | list[float], model.equipment["recycle"]["pump_around_duties_w"]
        )
        value = float(duties[index])
    elif name == "column.cut_3_temperature_c":
        value = float(
            cast(tuple[float, ...] | list[float], model.equipment["column"]["cut_points_k"])[2]
        )
    elif name == "column.cut_4_temperature_c":
        value = float(
            cast(tuple[float, ...] | list[float], model.equipment["column"]["cut_points_k"])[3]
        )
    elif name.startswith("dynamic."):
        value = cast(float, model.dynamic[name.removeprefix("dynamic.")])
    else:  # pragma: no cover - initial-state entries have a fixed nominal base
        value = 1.0
    return _from_si(name, float(value))


def _apply_case_inputs(
    base_case: CaseConfig,
    values: Mapping[str, float],
) -> CaseConfig:
    if not values:
        return base_case
    feed_values = {
        "mass_flow_kg_s": base_case.feed.mass_flow_kg_s,
        "temperature_k": base_case.feed.temperature_k,
        "pressure_pa": base_case.feed.pressure_pa,
        "salt_mass_flow_kg_s": base_case.feed.salt_mass_flow_kg_s,
    }
    fractions = dict(base_case.feed.mass_fractions)
    operating = dict(base_case.operating_conditions)
    operating_names = {
        "operating.flash_temperature_c": "flash_temperature_k",
        "operating.flash_pressure_mpa_g": "flash_pressure_pa",
        "operating.furnace_outlet_temperature_c": "furnace_outlet_temperature_k",
        "operating.tower_top_temperature_c": "tower_top_temperature_k",
        "operating.tower_top_pressure_mpa_g": "tower_top_pressure_pa",
        "operating.condenser_temperature_c": "condenser_temperature_k",
        "operating.ambient_temperature_c": "ambient_temperature_k",
    }
    for name, value in values.items():
        normalized = _to_si(name, value)
        if name == "feed.mass_flow_t_h":
            feed_values["mass_flow_kg_s"] = normalized
        elif name == "feed.temperature_c":
            feed_values["temperature_k"] = normalized
        elif name == "feed.pressure_mpa_g":
            feed_values["pressure_pa"] = normalized
        elif name == "feed.salt_mass_flow_kg_s":
            feed_values["salt_mass_flow_kg_s"] = normalized
        elif name.startswith("feed.mass_fraction."):
            fractions[name.removeprefix("feed.mass_fraction.")] = normalized
        elif name in operating_names:
            operating[operating_names[name]] = normalized
    feed = MaterialStream(
        name=base_case.feed.name,
        mass_flow_kg_s=feed_values["mass_flow_kg_s"],
        temperature_k=feed_values["temperature_k"],
        pressure_pa=feed_values["pressure_pa"],
        mass_fractions=fractions,
        salt_mass_flow_kg_s=feed_values["salt_mass_flow_kg_s"],
        metadata=base_case.feed.metadata,
    )
    return replace(
        base_case,
        feed=feed,
        operating_conditions=operating,
        metadata={**dict(base_case.metadata), "runtime_custom_input_version": CUSTOM_INPUT_VERSION},
    )


def _apply_model_inputs(
    base_model: ModelConfig,
    values: Mapping[str, float],
) -> ModelConfig:
    if not values:
        return base_model
    payload = base_model.as_dict()
    equipment = cast(dict[str, dict[str, object]], payload["equipment"])
    dynamic = cast(dict[str, object], payload["dynamic"])
    for name, value in values.items():
        normalized = _to_si(name, value)
        if name == "operation.wash_water_ratio":
            equipment["desalter"]["wash_water_ratio"] = normalized
        elif name == "operation.reflux_ratio":
            equipment["recycle"]["reflux_ratio"] = normalized
        elif name.startswith("operation.pump_around_"):
            index = int(name.removeprefix("operation.pump_around_")[0]) - 1
            duties = list(cast(list[float], equipment["recycle"]["pump_around_duties_w"]))
            duties[index] = normalized
            equipment["recycle"]["pump_around_duties_w"] = duties
        elif name == "column.cut_3_temperature_c":
            cuts = list(cast(list[float], equipment["column"]["cut_points_k"]))
            cuts[2] = normalized
            equipment["column"]["cut_points_k"] = cuts
        elif name == "column.cut_4_temperature_c":
            cuts = list(cast(list[float], equipment["column"]["cut_points_k"]))
            cuts[3] = normalized
            equipment["column"]["cut_points_k"] = cuts
        elif name.startswith("dynamic."):
            dynamic[name.removeprefix("dynamic.")] = normalized
    return ModelConfig.from_mapping(payload)


def _scenario_fingerprint_payload(
    request: RunRequest,
    preset: RuntimePreset,
    events: tuple[RuntimeInputEvent, ...] | None,
    duration_s: float | None,
    time_step_s: float | None,
) -> dict[str, object]:
    return {
        "template_scenario_id": preset.scenario_id,
        "duration_s": duration_s,
        "time_step_s": time_step_s,
        "events": None if events is None else [event.as_dict() for event in events],
        "request_fingerprint": request.request_fingerprint,
    }


@dataclass(frozen=True)
class ResolvedRuntimeInputs:
    """Canonical, solve-free effective inputs used by preview and execution."""

    request: RunRequest
    preset: RuntimePreset
    model: ModelConfig
    case: CaseConfig
    open_loop_scenario: ScenarioConfig | None
    closed_loop_scenario: ClosedLoopScenarioConfig | None
    event_requests: tuple[RuntimeInputEvent, ...] | None
    duration_s: float | None
    time_step_s: float | None
    initial_inventory_ratios: Mapping[str, float]
    applied_inputs: Mapping[str, Mapping[str, object]]
    base_object_fingerprints: Mapping[str, str]
    effective_object_fingerprints: Mapping[str, str]
    execution_input_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "initial_inventory_ratios",
            MappingProxyType(dict(self.initial_inventory_ratios)),
        )
        object.__setattr__(
            self,
            "applied_inputs",
            MappingProxyType(
                {name: MappingProxyType(dict(value)) for name, value in self.applied_inputs.items()}
            ),
        )
        object.__setattr__(
            self,
            "base_object_fingerprints",
            MappingProxyType(dict(self.base_object_fingerprints)),
        )
        object.__setattr__(
            self,
            "effective_object_fingerprints",
            MappingProxyType(dict(self.effective_object_fingerprints)),
        )

    @property
    def is_custom(self) -> bool:
        return bool(
            self.request.parameters
            or self.request.overrides
            or self.request.initial_state
            or self.request.scenario is not None
        )

    @property
    def preview_fingerprint(self) -> str:
        return canonical_fingerprint(self.preview_payload())

    def preview_payload(self) -> dict[str, object]:
        return {
            "schema_version": CUSTOM_INPUT_VERSION,
            "preset_id": self.request.preset_id,
            "run_type": self.request.run_type,
            "request_fingerprint": self.request.request_fingerprint,
            "customized": self.is_custom,
            "applied_inputs": {name: dict(value) for name, value in self.applied_inputs.items()},
            "effective_case": {
                "feed": self.case.feed.as_dict(),
                "operating_conditions_si": dict(self.case.operating_conditions),
            },
            "effective_model": {
                "wash_water_ratio": self.model.equipment["desalter"]["wash_water_ratio"],
                "reflux_ratio": self.model.equipment["recycle"]["reflux_ratio"],
                "pump_around_duties_w": self.model.equipment["recycle"]["pump_around_duties_w"],
                "column_cut_points_k": self.model.equipment["column"]["cut_points_k"],
                "dynamic": dict(self.model.dynamic),
            },
            "scenario": {
                "duration_s": self.duration_s,
                "time_step_s": self.time_step_s,
                "events": (
                    None
                    if self.event_requests is None
                    else [event.as_dict() for event in self.event_requests]
                ),
            },
            "initial_inventory_ratios": {
                inventory_name: self.initial_inventory_ratios.get(inventory_name, 1.0)
                for inventory_name in (
                    "flash_drum",
                    "reflux_drum",
                    "tower_bottom",
                )
            },
            "base_object_fingerprints": dict(self.base_object_fingerprints),
            "effective_object_fingerprints": dict(self.effective_object_fingerprints),
            "execution_input_fingerprint": self.execution_input_fingerprint,
        }

    def as_dict(self) -> dict[str, object]:
        payload = self.preview_payload()
        payload["preview_fingerprint"] = self.preview_fingerprint
        return payload


def _validate_requested_inputs(request: RunRequest) -> dict[str, float]:
    if request.run_type == "validation_scenario" and (
        request.parameters
        or request.overrides
        or request.initial_state
        or request.scenario is not None
    ):
        raise ValueError("portable M6 validation presets do not accept custom inputs")
    supplied: dict[str, float] = {}
    for kind, values in (
        ("parameter", request.parameters),
        ("override", request.overrides),
        ("initial_state", request.initial_state),
    ):
        for name, raw in values.items():
            try:
                spec = _SPEC_BY_ID[name]
            except KeyError as exc:
                raise ValueError(f"unknown controlled custom input {name!r}") from exc
            if spec.kind != kind:
                raise ValueError(f"input {name!r} belongs in {spec.kind}, not {kind}")
            supplied[name] = spec.validate(raw, run_type=request.run_type)
    return supplied


def validate_runtime_request_shape(request: RunRequest) -> None:
    """Reject unknown or structurally inapplicable custom fields without I/O."""

    if not isinstance(request, RunRequest):
        raise TypeError("validate_runtime_request_shape requires a RunRequest")
    preset = get_preset(request.preset_id)
    if request.run_type != preset.run_type:
        raise ValueError("request run_type differs from its preset template")
    _validate_requested_inputs(request)
    if request.run_type not in _DYNAMIC_TYPES and request.scenario is not None:
        raise ValueError("only dynamic runs accept scenario overrides")


def _resolve_scenario(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
) -> tuple[
    ScenarioConfig | None,
    ClosedLoopScenarioConfig | None,
    tuple[RuntimeInputEvent, ...] | None,
    float | None,
    float | None,
]:
    if request.run_type not in _DYNAMIC_TYPES:
        if request.scenario is not None:
            raise ValueError("only dynamic runs accept scenario overrides")
        return None, None, None, preset.duration_s, preset.time_step_s
    base_open = None
    base_closed = None
    if request.run_type == "open_loop_dynamic":
        base_open = next(
            item
            for item in bundle.open_loop_scenarios.values()
            if item.scenario_version == preset.scenario_id
        )
        base_duration = base_open.duration_s
        base_step = base_open.time_step_s
    else:
        base_closed = next(
            item
            for item in bundle.closed_loop_scenarios.values()
            if item.scenario_version == preset.scenario_id
        )
        base_duration = base_closed.duration_s
        base_step = base_closed.time_step_s
    duration_s = (
        base_duration
        if request.scenario is None or request.scenario.duration_s is None
        else request.scenario.duration_s
    )
    time_step_s = (
        base_step
        if request.scenario is None or request.scenario.time_step_s is None
        else request.scenario.time_step_s
    )
    if time_step_s > duration_s:
        raise ValueError("scenario time_step_s cannot exceed duration_s")
    step_count = duration_s / time_step_s
    if not math.isclose(step_count, round(step_count), rel_tol=0.0, abs_tol=1.0e-10):
        raise ValueError("scenario duration_s must be an integer multiple of time_step_s")

    custom_events = None if request.scenario is None else request.scenario.events
    if custom_events is None:
        if base_open is not None:
            event_requests = tuple(
                RuntimeInputEvent(
                    float(cast(float, event["time_s"])),
                    cast(str, event["target"]),
                    float(cast(float, event["value"])),
                    "absolute",
                    cast(float | None, event.get("duration_s")),
                )
                for event in base_open.events
            )
        else:
            assert base_closed is not None
            event_requests = tuple(
                RuntimeInputEvent(
                    event.time_s,
                    f"{event.loop_id}.setpoint_ratio",
                    event.setpoint_ratio,
                    "setpoint_ratio",
                )
                for event in base_closed.events
            )
    else:
        event_requests = custom_events
    if any(event.time_s > duration_s for event in event_requests):
        raise ValueError("scenario event time exceeds the effective duration")

    suffix = request.request_fingerprint[:12]
    if base_open is not None:
        for event in event_requests:
            if event.target not in ACTUATOR_STATE_NAMES:
                raise ValueError(f"unknown open-loop actuator target {event.target!r}")
            if event.value_basis not in {"absolute", "nominal_ratio"}:
                raise ValueError("open-loop events use absolute or nominal_ratio values")
            if event.value < 0.0:
                raise ValueError("open-loop event values must be non-negative")
        scenario = ScenarioConfig(
            schema_version=base_open.schema_version,
            scenario_version=(
                base_open.scenario_version if request.scenario is None else f"runtime-open-{suffix}"
            ),
            config_version=base_open.config_version,
            case_version=base_open.case_version,
            model_version=base_open.model_version,
            parameter_set_version=base_open.parameter_set_version,
            name=(base_open.name if request.scenario is None else f"runtime_open_{suffix}"),
            duration_s=duration_s,
            time_step_s=time_step_s,
            events=tuple(
                {
                    "time_s": event.time_s,
                    "target": event.target,
                    "value": event.value,
                    **({} if event.duration_s is None else {"duration_s": event.duration_s}),
                }
                for event in event_requests
            ),
            metadata={
                **dict(base_open.metadata),
                **(
                    {}
                    if request.scenario is None
                    else {"custom_input_version": CUSTOM_INPUT_VERSION}
                ),
            },
        )
        return scenario, None, event_requests, duration_s, time_step_s

    assert base_closed is not None
    setpoint_events: list[SetpointEvent] = []
    control = bundle.require_control()
    loop_ids = set(control.loops)
    for event in event_requests:
        suffix_text = ".setpoint_ratio"
        if not event.target.endswith(suffix_text):
            raise ValueError("closed-loop events target '<loop>.setpoint_ratio'")
        loop_id = event.target[: -len(suffix_text)]
        if loop_id not in loop_ids:
            raise ValueError(f"unknown closed-loop control loop {loop_id!r}")
        if event.value_basis != "setpoint_ratio" or event.duration_s is not None:
            raise ValueError("closed-loop events require setpoint_ratio values without duration")
        setpoint_events.append(SetpointEvent(event.time_s, loop_id, event.value))
    closed = ClosedLoopScenarioConfig(
        schema_version=base_closed.schema_version,
        scenario_version=(
            base_closed.scenario_version if request.scenario is None else f"runtime-closed-{suffix}"
        ),
        control_version=base_closed.control_version,
        config_version=base_closed.config_version,
        case_version=base_closed.case_version,
        model_version=base_closed.model_version,
        parameter_set_version=base_closed.parameter_set_version,
        name=(base_closed.name if request.scenario is None else f"runtime_closed_{suffix}"),
        duration_s=duration_s,
        time_step_s=time_step_s,
        events=tuple(setpoint_events),
        metadata={
            **dict(base_closed.metadata),
            **({} if request.scenario is None else {"custom_input_version": CUSTOM_INPUT_VERSION}),
        },
    )
    return None, closed, event_requests, duration_s, time_step_s


def resolve_runtime_inputs(
    request: RunRequest,
    *,
    bundle: RuntimeResourceBundle | None = None,
) -> ResolvedRuntimeInputs:
    """Resolve and validate a request without running M2, M3 or M4."""

    if not isinstance(request, RunRequest):
        raise TypeError("resolve_runtime_inputs requires a RunRequest")
    preset = get_preset(request.preset_id)
    if request.run_type != preset.run_type:
        raise ValueError("request run_type differs from its preset template")
    resources = load_runtime_resource_bundle(preset) if bundle is None else bundle
    supplied = _validate_requested_inputs(request)
    case_values = {
        name: value for name, value in supplied.items() if name.startswith(("feed.", "operating."))
    }
    model_values = {
        name: value
        for name, value in supplied.items()
        if name.startswith(("operation.", "column.", "dynamic."))
    }
    model = _apply_model_inputs(resources.effective_model, model_values)
    case = _apply_case_inputs(resources.effective_case, case_values)
    validate_config_compatibility(
        model,
        case,
        software_version="0.1.0",
        catalog=resources.catalog,
    )
    open_scenario, closed_scenario, events, duration_s, time_step_s = _resolve_scenario(
        request,
        preset,
        resources,
    )
    if open_scenario is not None:
        validate_config_compatibility(
            model,
            case,
            software_version="0.1.0",
            catalog=resources.catalog,
            scenario=open_scenario,
        )
    initial_ratios = {
        name.removeprefix("inventory.").removesuffix("_ratio"): value
        for name, value in supplied.items()
        if name.startswith("inventory.")
    }
    applied: dict[str, Mapping[str, object]] = {}
    for name, value in supplied.items():
        spec = _SPEC_BY_ID[name]
        applied[name] = {
            "kind": spec.kind,
            "requested_value": value,
            "requested_unit": spec.display_unit,
            "normalized_value": _to_si(name, value),
            "normalized_unit": spec.si_unit,
            "template_value": _base_value(
                spec,
                resources.effective_model,
                resources.effective_case,
            ),
        }
    base_fingerprints = {
        "model": canonical_fingerprint(resources.effective_model.as_dict()),
        "case": canonical_fingerprint(resources.effective_case.as_dict()),
        "catalog": canonical_fingerprint(resources.catalog.as_dict()),
    }
    scenario_payload = _scenario_fingerprint_payload(
        request,
        preset,
        events,
        duration_s,
        time_step_s,
    )
    effective_fingerprints = {
        "model": canonical_fingerprint(model.as_dict()),
        "case": canonical_fingerprint(case.as_dict()),
        "scenario": canonical_fingerprint(scenario_payload),
    }
    execution_payload: dict[str, object] = {
        "request": request.fingerprint_payload(),
        "effective_model": model.as_dict(),
        "effective_case": case.as_dict(),
        "component_catalog": resources.catalog.as_dict(),
        "m5_analysis_basis": resources.m5_overlay.analysis_basis_fingerprint,
        "resource_fingerprints": dict(resources.resource_fingerprints),
    }
    if open_scenario is not None:
        execution_payload["scenario"] = open_scenario.as_dict()
    if closed_scenario is not None:
        execution_payload["control"] = resources.require_control().as_dict()
        execution_payload["scenario"] = closed_scenario.as_dict()
    if request.scenario is not None:
        execution_payload["runtime_scenario_request"] = scenario_payload
    if initial_ratios:
        execution_payload["initial_inventory_ratios"] = initial_ratios
    execution_fingerprint = canonical_fingerprint(execution_payload)
    return ResolvedRuntimeInputs(
        request=request,
        preset=preset,
        model=model,
        case=case,
        open_loop_scenario=open_scenario,
        closed_loop_scenario=closed_scenario,
        event_requests=events,
        duration_s=duration_s,
        time_step_s=time_step_s,
        initial_inventory_ratios=initial_ratios,
        applied_inputs=applied,
        base_object_fingerprints=base_fingerprints,
        effective_object_fingerprints=effective_fingerprints,
        execution_input_fingerprint=execution_fingerprint,
    )


def runtime_request_template(preset_id: str) -> dict[str, object]:
    """Return a sparse editable request plus the applicable whitelist."""

    get_preset(preset_id)
    request: dict[str, object] = {
        "preset_id": preset_id,
        "parameters": {},
        "overrides": {},
        "initial_state": {},
    }
    return {
        "custom_input_version": CUSTOM_INPUT_VERSION,
        "request": request,
        "available_inputs": [spec.as_dict() for spec in list_runtime_input_specs(preset_id)],
    }


def runtime_request_from_mapping(value: Mapping[str, object]) -> RunRequest:
    """Merge one sparse user request over its immutable preset template.

    The normalized :class:`RunRequest` remains fully explicit and strict.  This
    adapter only makes the user-authored JSON sparse: omitted fields inherit the
    selected preset, while supplied maps overlay their corresponding defaults.
    """

    if not isinstance(value, Mapping):
        raise TypeError("runtime request file must contain one mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError("runtime request file keys must be strings")
    allowed = {
        "schema_version",
        "request_version",
        "preset_id",
        "run_type",
        "random_seed",
        "parameters",
        "overrides",
        "metadata",
        "run_id",
        "requested_at_utc",
        "scenario",
        "initial_state",
        "request_fingerprint",
    }
    unknown = sorted(set(value) - allowed)
    if "preset_id" not in value or unknown:
        raise ValueError(
            "runtime request file fields differ; "
            f"missing={[] if 'preset_id' in value else ['preset_id']}, unknown={unknown}"
        )
    preset_id = value["preset_id"]
    if not isinstance(preset_id, str):
        raise TypeError("runtime request file preset_id must be a string")
    if ".." in preset_id or "/" in preset_id or "\\" in preset_id:
        raise ValueError("runtime request file preset_id must not contain a path traversal")
    base = get_preset(preset_id).to_request()
    payload = base.as_dict()
    payload.pop("request_fingerprint", None)

    for field_name in (
        "schema_version",
        "request_version",
        "run_type",
        "random_seed",
        "run_id",
        "requested_at_utc",
        "scenario",
    ):
        if field_name in value:
            payload[field_name] = value[field_name]

    inherited_mappings: dict[str, Mapping[str, object]] = {
        "parameters": base.parameters,
        "overrides": base.overrides,
        "metadata": base.metadata,
        "initial_state": base.initial_state,
    }
    for field_name, inherited in inherited_mappings.items():
        if field_name not in value:
            continue
        supplied = value[field_name]
        if not isinstance(supplied, Mapping):
            raise TypeError(f"runtime request file {field_name} must be a mapping")
        if field_name == "metadata":
            for key, inherited_value in inherited.items():
                if key in supplied and supplied[key] != inherited_value:
                    raise ValueError(f"preset metadata key {key!r} cannot be overridden")
        payload[field_name] = {**dict(inherited), **dict(supplied)}

    if "request_fingerprint" in value:
        payload["request_fingerprint"] = value["request_fingerprint"]
    request = RunRequest.from_mapping(payload)
    validate_runtime_request_shape(request)
    return request


def apply_initial_inventory_ratios(
    initial_state: DynamicState,
    ratios: Mapping[str, float],
) -> DynamicState:
    """Scale only the three public liquid inventories and their measurements."""

    if not isinstance(initial_state, DynamicState):
        raise TypeError("initial_state must be a DynamicState")
    allowed = {"flash_drum", "reflux_drum", "tower_bottom"}
    unknown = sorted(set(ratios) - allowed)
    if unknown:
        raise ValueError("unknown initial inventory ratios: " + ", ".join(unknown))
    inventories = dict(initial_state.liquid_inventories)
    sensors = dict(initial_state.sensor_states)
    sensor_names = {
        "flash_drum": "flash_drum_inventory_kg",
        "reflux_drum": "reflux_drum_inventory_kg",
        "tower_bottom": "tower_bottom_inventory_kg",
    }
    for name, raw_ratio in ratios.items():
        if isinstance(raw_ratio, bool) or not isinstance(raw_ratio, (int, float)):
            raise TypeError(f"inventory ratio {name!r} must be numeric")
        ratio = float(raw_ratio)
        if not math.isfinite(ratio) or ratio <= 0.0:
            raise ValueError(f"inventory ratio {name!r} must be finite and positive")
        source = inventories[name]
        scaled = InventoryState(
            name,
            {
                component: source.component_masses_kg[component] * ratio
                for component in ALL_COMPONENTS
            },
            source.salt_mass_kg * ratio,
        )
        inventories[name] = scaled
        sensors[sensor_names[name]] = scaled.total_mass_kg
    return DynamicState(
        liquid_inventories=inventories,
        top_gas_component_masses_kg=initial_state.top_gas_component_masses_kg,
        thermal_states=initial_state.thermal_states,
        actuator_states=initial_state.actuator_states,
        sensor_states=sensors,
    )


__all__ = [
    "ResolvedRuntimeInputs",
    "RuntimeInputSpec",
    "apply_initial_inventory_ratios",
    "list_runtime_input_specs",
    "resolve_runtime_inputs",
    "runtime_request_from_mapping",
    "runtime_request_template",
    "validate_runtime_request_shape",
]
