"""Strict configuration loading and canonical input fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from ..properties.components import ComponentCatalog
from .types import MaterialStream
from .units import parse_quantity
from .versions import VersionBundle

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_OPERATING_DIMENSIONS = {
    "flash_temperature_k": "temperature",
    "flash_pressure_pa": "pressure",
    "furnace_outlet_temperature_k": "temperature",
    "tower_top_temperature_k": "temperature",
    "tower_top_pressure_pa": "pressure",
    "condenser_temperature_k": "temperature",
    "ambient_temperature_k": "temperature",
}


class ConfigurationError(ValueError):
    """Raised when a model configuration is missing or contains invalid content."""


def load_json(path: Path) -> dict[str, Any]:
    """Load a UTF-8 JSON object from path and reject non-finite numbers."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot load JSON configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"configuration must be a JSON object: {path}")
    _reject_nonfinite(value, path="$")
    return value


def _reject_nonfinite(value: object, *, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigurationError(f"non-finite number at {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, path=f"{path}[{index}]")


def require_keys(mapping: Mapping[str, Any], keys: Iterable[str], *, context: str) -> None:
    """Validate required keys and report them together."""

    missing = sorted(key for key in keys if key not in mapping)
    if missing:
        raise ConfigurationError(f"{context} is missing required keys: {', '.join(missing)}")


def strict_keys(
    mapping: Mapping[str, object],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    context: str,
) -> None:
    """Reject both missing and unknown configuration keys."""

    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(mapping))
    unknown = sorted(set(mapping) - allowed)
    if missing or unknown:
        raise ConfigurationError(
            f"{context} fields differ; missing={missing}, unknown={unknown}"
        )


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ConfigurationError(f"{field_name} must be a non-empty identifier")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field_name} must be a non-empty string")
    return value


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{field_name} must be an object with string keys")
    return value


def _string_mapping(value: object, *, field_name: str) -> Mapping[str, str]:
    raw = _mapping(value, field_name=field_name)
    if any(not isinstance(item, str) for item in raw.values()):
        raise ConfigurationError(f"{field_name} values must be strings")
    return MappingProxyType({key: str(item) for key, item in raw.items()})


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ConfigurationError("configuration object keys must be strings")
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _frozen_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    raw = _mapping(value, field_name=field_name)
    return cast(Mapping[str, object], _deep_freeze(raw))


def _nested_mapping(value: object, *, field_name: str) -> Mapping[str, Mapping[str, object]]:
    raw = _mapping(value, field_name=field_name)
    copied: dict[str, Mapping[str, object]] = {}
    for key, item in raw.items():
        copied[key] = _frozen_mapping(item, field_name=f"{field_name}.{key}")
    return MappingProxyType(copied)


def _number(
    value: Mapping[str, object],
    key: str,
    *,
    context: str,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    raw = value[key]
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ConfigurationError(f"{context}.{key} must be numeric")
    result = float(raw)
    if not math.isfinite(result):
        raise ConfigurationError(f"{context}.{key} must be finite")
    if minimum is not None:
        invalid = result <= minimum if minimum_exclusive else result < minimum
        if invalid:
            relation = "greater than" if minimum_exclusive else "at least"
            raise ConfigurationError(f"{context}.{key} must be {relation} {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigurationError(f"{context}.{key} must be at most {maximum}")
    return result


def _integer(
    value: Mapping[str, object],
    key: str,
    *,
    context: str,
    minimum: int,
) -> int:
    raw = value[key]
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ConfigurationError(f"{context}.{key} must be an integer")
    if raw < minimum:
        raise ConfigurationError(f"{context}.{key} must be at least {minimum}")
    return raw


def _number_sequence(
    value: object,
    *,
    field_name: str,
    length: int,
    minimum: float = 0.0,
    minimum_exclusive: bool = False,
) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field_name} must be a sequence")
    if len(value) != length:
        raise ConfigurationError(f"{field_name} must contain {length} values")
    result: list[float] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ConfigurationError(f"{field_name}[{index}] must be numeric")
        number = float(raw)
        if not math.isfinite(number):
            raise ConfigurationError(f"{field_name}[{index}] must be finite")
        invalid = number <= minimum if minimum_exclusive else number < minimum
        if invalid:
            raise ConfigurationError(f"{field_name}[{index}] is outside its allowed range")
        result.append(number)
    return tuple(result)


def _validate_preheater(value: Mapping[str, object], *, context: str) -> None:
    strict_keys(
        value,
        required={"effectiveness", "target_temperature_k", "pressure_drop_pa"},
        context=context,
    )
    _number(value, "effectiveness", context=context, minimum=0.0, maximum=1.0)
    _number(
        value,
        "target_temperature_k",
        context=context,
        minimum=0.0,
        minimum_exclusive=True,
    )
    _number(value, "pressure_drop_pa", context=context, minimum=0.0)


def _validate_model_sections(
    equipment: Mapping[str, object],
    solver: Mapping[str, object],
    dynamic: Mapping[str, object],
) -> None:
    equipment_names = {
        "pre_desalter_preheater",
        "desalter",
        "post_desalter_preheater",
        "pre_furnace_preheater",
        "flash",
        "furnace",
        "column",
        "condenser",
        "recycle",
    }
    strict_keys(equipment, required=equipment_names, context="equipment")
    for name in (
        "pre_desalter_preheater",
        "post_desalter_preheater",
        "pre_furnace_preheater",
    ):
        _validate_preheater(_mapping(equipment[name], field_name=name), context=name)

    desalter = _mapping(equipment["desalter"], field_name="desalter")
    strict_keys(
        desalter,
        required={
            "wash_water_ratio",
            "water_removal_efficiency",
            "salt_removal_efficiency",
            "oil_entrainment_fraction",
            "pressure_drop_pa",
        },
        context="desalter",
    )
    _number(desalter, "wash_water_ratio", context="desalter", minimum=0.0, maximum=1.0)
    _number(
        desalter,
        "water_removal_efficiency",
        context="desalter",
        minimum=0.0,
        maximum=1.0,
    )
    _number(
        desalter,
        "salt_removal_efficiency",
        context="desalter",
        minimum=0.0,
        maximum=1.0,
    )
    _number(
        desalter,
        "oil_entrainment_fraction",
        context="desalter",
        minimum=0.0,
        maximum=0.1,
    )
    _number(desalter, "pressure_drop_pa", context="desalter", minimum=0.0)

    flash = _mapping(equipment["flash"], field_name="flash")
    strict_keys(flash, required={"temperature_k", "pressure_pa"}, context="flash")
    _number(flash, "temperature_k", context="flash", minimum=0.0, minimum_exclusive=True)
    _number(flash, "pressure_pa", context="flash", minimum=0.0, minimum_exclusive=True)

    furnace = _mapping(equipment["furnace"], field_name="furnace")
    strict_keys(
        furnace,
        required={
            "efficiency",
            "heat_loss_w",
            "maximum_outlet_temperature_k",
            "pressure_drop_pa",
        },
        context="furnace",
    )
    _number(
        furnace,
        "efficiency",
        context="furnace",
        minimum=0.0,
        maximum=1.0,
        minimum_exclusive=True,
    )
    _number(furnace, "heat_loss_w", context="furnace", minimum=0.0)
    _number(
        furnace,
        "maximum_outlet_temperature_k",
        context="furnace",
        minimum=0.0,
        minimum_exclusive=True,
    )
    _number(furnace, "pressure_drop_pa", context="furnace", minimum=0.0)

    column = _mapping(equipment["column"], field_name="column")
    strict_keys(
        column,
        required={
            "pressure_pa",
            "cut_points_k",
            "separation_widths_k",
            "product_temperatures_k",
        },
        context="column",
    )
    _number(column, "pressure_pa", context="column", minimum=0.0, minimum_exclusive=True)
    cut_points = _number_sequence(
        column["cut_points_k"],
        field_name="column.cut_points_k",
        length=4,
        minimum=0.0,
        minimum_exclusive=True,
    )
    if any(left >= right for left, right in pairwise(cut_points)):
        raise ConfigurationError("column.cut_points_k must be strictly increasing")
    _number_sequence(
        column["separation_widths_k"],
        field_name="column.separation_widths_k",
        length=4,
        minimum=0.0,
        minimum_exclusive=True,
    )
    product_temperatures = _mapping(
        column["product_temperatures_k"],
        field_name="column.product_temperatures_k",
    )
    strict_keys(
        product_temperatures,
        required={"overhead", "kerosene", "light_diesel", "heavy_diesel", "residue"},
        context="column.product_temperatures_k",
    )
    for name in product_temperatures:
        _number(
            product_temperatures,
            name,
            context="column.product_temperatures_k",
            minimum=0.0,
            minimum_exclusive=True,
        )

    condenser = _mapping(equipment["condenser"], field_name="condenser")
    strict_keys(
        condenser,
        required={"temperature_k", "pressure_pa", "condensation_width_k"},
        context="condenser",
    )
    for key in ("temperature_k", "pressure_pa", "condensation_width_k"):
        _number(
            condenser,
            key,
            context="condenser",
            minimum=0.0,
            minimum_exclusive=True,
        )

    recycle = _mapping(equipment["recycle"], field_name="recycle")
    strict_keys(
        recycle,
        required={
            "reflux_ratio",
            "reflux_sharpness_gain",
            "pump_around_duties_w",
            "heat_recovery_efficiency",
            "maximum_recovered_duty_w",
        },
        context="recycle",
    )
    _number(recycle, "reflux_ratio", context="recycle", minimum=0.0)
    _number(recycle, "reflux_sharpness_gain", context="recycle", minimum=0.0)
    _number_sequence(
        recycle["pump_around_duties_w"],
        field_name="recycle.pump_around_duties_w",
        length=3,
    )
    _number(
        recycle,
        "heat_recovery_efficiency",
        context="recycle",
        minimum=0.0,
        maximum=1.0,
    )
    _number(recycle, "maximum_recovered_duty_w", context="recycle", minimum=0.0)

    strict_keys(
        solver,
        required={
            "mass_tolerance_kg_s",
            "component_tolerance_kg_s",
            "salt_tolerance_kg_s",
            "recycle_tolerance",
            "maximum_iterations",
            "relaxation_factor",
        },
        context="solver",
    )
    for key in (
        "mass_tolerance_kg_s",
        "component_tolerance_kg_s",
        "salt_tolerance_kg_s",
        "recycle_tolerance",
    ):
        _number(solver, key, context="solver", minimum=0.0, minimum_exclusive=True)
    _integer(solver, "maximum_iterations", context="solver", minimum=1)
    _number(
        solver,
        "relaxation_factor",
        context="solver",
        minimum=0.0,
        maximum=1.0,
        minimum_exclusive=True,
    )

    dynamic_names = {
        "default_time_step_s",
        "furnace_time_constant_s",
        "preheater_time_constant_s",
        "condenser_time_constant_s",
        "tower_temperature_time_constant_s",
        "actuator_time_constant_s",
        "sensor_time_constant_s",
        "flash_residence_time_s",
        "reflux_drum_residence_time_s",
        "tower_bottom_residence_time_s",
        "top_gas_volume_m3",
    }
    strict_keys(dynamic, required=dynamic_names, context="dynamic")
    for key in dynamic_names:
        _number(dynamic, key, context="dynamic", minimum=0.0, minimum_exclusive=True)


@dataclass(frozen=True)
class ModelConfig:
    """Validated top-level model and parameter configuration."""

    schema_version: str
    config_version: str
    model_version: str
    parameter_set_version: str
    component_catalog_path: str
    equipment: Mapping[str, Mapping[str, object]]
    solver: Mapping[str, object]
    dynamic: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ModelConfig:
        strict_keys(
            value,
            required={
                "schema_version",
                "config_version",
                "model_version",
                "parameter_set_version",
                "component_catalog_path",
                "equipment",
                "solver",
                "dynamic",
            },
            context="model configuration",
        )
        component_catalog_path = value["component_catalog_path"]
        if not isinstance(component_catalog_path, str) or not component_catalog_path.strip():
            raise ConfigurationError("component_catalog_path must be a non-empty string")
        equipment = _mapping(value["equipment"], field_name="equipment")
        solver = _mapping(value["solver"], field_name="solver")
        dynamic = _mapping(value["dynamic"], field_name="dynamic")
        _validate_model_sections(equipment, solver, dynamic)
        return cls(
            schema_version=_identifier(value["schema_version"], field_name="schema_version"),
            config_version=_identifier(value["config_version"], field_name="config_version"),
            model_version=_identifier(value["model_version"], field_name="model_version"),
            parameter_set_version=_identifier(
                value["parameter_set_version"],
                field_name="parameter_set_version",
            ),
            component_catalog_path=component_catalog_path,
            equipment=_nested_mapping(equipment, field_name="equipment"),
            solver=_frozen_mapping(solver, field_name="solver"),
            dynamic=_frozen_mapping(dynamic, field_name="dynamic"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_version": self.config_version,
            "model_version": self.model_version,
            "parameter_set_version": self.parameter_set_version,
            "component_catalog_path": self.component_catalog_path,
            "equipment": _deep_thaw(self.equipment),
            "solver": _deep_thaw(self.solver),
            "dynamic": _deep_thaw(self.dynamic),
        }


@dataclass(frozen=True)
class CaseConfig:
    """Validated plant case transformed to canonical SI model inputs."""

    schema_version: str
    case_version: str
    model_version: str
    parameter_set_version: str
    name: str
    feed: MaterialStream
    operating_conditions: Mapping[str, float]
    observations: Mapping[str, object]
    metadata: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CaseConfig:
        strict_keys(
            value,
            required={
                "schema_version",
                "case_version",
                "model_version",
                "parameter_set_version",
                "name",
                "feed",
                "operating_conditions",
                "observations",
                "metadata",
            },
            context="case configuration",
        )
        feed_raw = _mapping(value["feed"], field_name="feed")
        strict_keys(
            feed_raw,
            required={
                "name",
                "mass_flow",
                "temperature",
                "pressure",
                "mass_fractions",
                "salt_mass_flow",
                "metadata",
            },
            context="feed",
        )
        fractions_raw = _mapping(feed_raw["mass_fractions"], field_name="feed.mass_fractions")
        fractions: dict[str, float] = {}
        for key, item in fractions_raw.items():
            if not isinstance(item, (int, float)) or isinstance(item, bool):
                raise ConfigurationError("feed mass fractions must be numeric")
            fractions[key] = float(item)
        feed = MaterialStream(
            name=_text(feed_raw["name"], field_name="feed.name"),
            mass_flow_kg_s=parse_quantity(feed_raw["mass_flow"], dimension="mass_flow"),
            temperature_k=parse_quantity(feed_raw["temperature"], dimension="temperature"),
            pressure_pa=parse_quantity(feed_raw["pressure"], dimension="pressure"),
            mass_fractions=fractions,
            salt_mass_flow_kg_s=parse_quantity(
                feed_raw["salt_mass_flow"],
                dimension="mass_flow",
            ),
            metadata=_string_mapping(feed_raw["metadata"], field_name="feed.metadata"),
        )

        operating_raw = _mapping(
            value["operating_conditions"],
            field_name="operating_conditions",
        )
        strict_keys(
            operating_raw,
            required=_OPERATING_DIMENSIONS,
            context="operating_conditions",
        )
        operating = {
            name: parse_quantity(operating_raw[name], dimension=dimension)
            for name, dimension in _OPERATING_DIMENSIONS.items()
        }
        observations = _mapping(value["observations"], field_name="observations")
        return cls(
            schema_version=_identifier(value["schema_version"], field_name="schema_version"),
            case_version=_identifier(value["case_version"], field_name="case_version"),
            model_version=_identifier(value["model_version"], field_name="model_version"),
            parameter_set_version=_identifier(
                value["parameter_set_version"],
                field_name="parameter_set_version",
            ),
            name=_text(value["name"], field_name="name"),
            feed=feed,
            operating_conditions=MappingProxyType(operating),
            observations=_frozen_mapping(observations, field_name="observations"),
            metadata=_string_mapping(value["metadata"], field_name="metadata"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "case_version": self.case_version,
            "model_version": self.model_version,
            "parameter_set_version": self.parameter_set_version,
            "name": self.name,
            "feed": self.feed.as_dict(),
            "operating_conditions_si": dict(self.operating_conditions),
            "observations": _deep_thaw(self.observations),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ScenarioConfig:
    """Validated open-loop scenario definition."""

    schema_version: str
    scenario_version: str
    config_version: str
    case_version: str
    model_version: str
    parameter_set_version: str
    name: str
    duration_s: float
    time_step_s: float
    events: tuple[Mapping[str, object], ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ScenarioConfig:
        strict_keys(
            value,
            required={
                "schema_version",
                "scenario_version",
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
            context="scenario configuration",
        )
        events_raw = value["events"]
        if not isinstance(events_raw, list):
            raise ConfigurationError("scenario events must be a list")
        events: list[Mapping[str, object]] = []
        for index, event in enumerate(events_raw):
            if not isinstance(event, Mapping) or any(not isinstance(key, str) for key in event):
                raise ConfigurationError(f"scenario event {index} must be an object")
            strict_keys(
                event,
                required={"time", "target", "value"},
                optional={"duration"},
                context=f"scenario event {index}",
            )
            event_time_s = parse_quantity(event["time"], dimension="time")
            if event_time_s < 0.0:
                raise ConfigurationError(f"scenario event {index} time cannot be negative")
            target = _identifier(
                event["target"],
                field_name=f"scenario event {index} target",
            )
            raw_value = event["value"]
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                raise ConfigurationError(f"scenario event {index} value must be numeric")
            numeric_value = float(raw_value)
            if not math.isfinite(numeric_value):
                raise ConfigurationError(f"scenario event {index} value must be finite")
            if numeric_value < 0.0:
                raise ConfigurationError(
                    f"scenario event {index} value must be non-negative"
                )
            parsed_event: dict[str, object] = {
                "time_s": event_time_s,
                "target": target,
                "value": numeric_value,
            }
            if "duration" in event:
                duration = parse_quantity(event["duration"], dimension="time")
                if duration <= 0.0:
                    raise ConfigurationError(
                        f"scenario event {index} duration must be positive"
                    )
                parsed_event["duration_s"] = duration
            events.append(_frozen_mapping(parsed_event, field_name=f"scenario event {index}"))
        duration_s = parse_quantity(value["duration"], dimension="time")
        time_step_s = parse_quantity(value["time_step"], dimension="time")
        if duration_s <= 0.0 or time_step_s <= 0.0:
            raise ConfigurationError("scenario duration and time step must be positive")
        if time_step_s > duration_s:
            raise ConfigurationError("scenario time step cannot exceed duration")
        metadata = _string_mapping(value["metadata"], field_name="metadata")
        if metadata.get("synthetic") != "true":
            raise ConfigurationError("scenario metadata.synthetic must be 'true'")
        purpose = metadata.get("purpose")
        if purpose is None or not purpose.strip():
            raise ConfigurationError("scenario metadata.purpose must be a non-empty string")
        return cls(
            schema_version=_identifier(value["schema_version"], field_name="schema_version"),
            scenario_version=_identifier(
                value["scenario_version"],
                field_name="scenario_version",
            ),
            config_version=_identifier(value["config_version"], field_name="config_version"),
            case_version=_identifier(value["case_version"], field_name="case_version"),
            model_version=_identifier(value["model_version"], field_name="model_version"),
            parameter_set_version=_identifier(
                value["parameter_set_version"],
                field_name="parameter_set_version",
            ),
            name=_text(value["name"], field_name="name"),
            duration_s=duration_s,
            time_step_s=time_step_s,
            events=tuple(events),
            metadata=metadata,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scenario_version": self.scenario_version,
            "config_version": self.config_version,
            "case_version": self.case_version,
            "model_version": self.model_version,
            "parameter_set_version": self.parameter_set_version,
            "name": self.name,
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "events": [_deep_thaw(event) for event in self.events],
            "metadata": dict(self.metadata),
        }


def load_model_config(path: Path) -> ModelConfig:
    try:
        return ModelConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid model configuration {path}: {exc}") from exc


def load_case_config(path: Path) -> CaseConfig:
    try:
        return CaseConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid case configuration {path}: {exc}") from exc


def load_scenario_config(path: Path) -> ScenarioConfig:
    try:
        return ScenarioConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid scenario configuration {path}: {exc}") from exc


def load_component_catalog(path: Path) -> ComponentCatalog:
    try:
        return ComponentCatalog.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid component catalog {path}: {exc}") from exc


def validate_config_compatibility(
    model: ModelConfig,
    case: CaseConfig,
    *,
    software_version: str,
    catalog: ComponentCatalog | None = None,
    scenario: ScenarioConfig | None = None,
) -> VersionBundle:
    """Reject version combinations that cannot describe the same run."""

    schema_versions = {model.schema_version, case.schema_version}
    if catalog is not None:
        schema_versions.add(catalog.schema_version)
    if scenario is not None:
        schema_versions.add(scenario.schema_version)
    if len(schema_versions) != 1:
        raise ConfigurationError("configuration schema versions do not match")
    if case.model_version != model.model_version:
        raise ConfigurationError("case and model versions do not match")
    if case.parameter_set_version != model.parameter_set_version:
        raise ConfigurationError("case and model parameter-set versions do not match")
    if catalog is not None and catalog.parameter_set_version != model.parameter_set_version:
        raise ConfigurationError("component catalog and model parameter-set versions do not match")
    if scenario is not None:
        checks = {
            "config_version": (scenario.config_version, model.config_version),
            "case_version": (scenario.case_version, case.case_version),
            "model_version": (scenario.model_version, model.model_version),
            "parameter_set_version": (
                scenario.parameter_set_version,
                model.parameter_set_version,
            ),
        }
        mismatches = sorted(name for name, pair in checks.items() if pair[0] != pair[1])
        if mismatches:
            raise ConfigurationError(
                f"scenario version mismatch: {', '.join(mismatches)}"
            )
    return VersionBundle(
        software_version=software_version,
        model_version=model.model_version,
        parameter_set_version=model.parameter_set_version,
        config_version=model.config_version,
        case_version=case.case_version,
        scenario_version=None if scenario is None else scenario.scenario_version,
    )


def canonical_fingerprint(*values: Mapping[str, object]) -> str:
    """Hash canonical JSON inputs so equivalent configurations share an id."""

    payload = json.dumps(
        [dict(value) for value in values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def input_bundle_fingerprint(
    model: ModelConfig,
    case: CaseConfig,
    versions: VersionBundle,
    *,
    catalog: ComponentCatalog | None = None,
    scenario: ScenarioConfig | None = None,
) -> str:
    """Hash every versioned input required to reproduce one model run."""

    payload: dict[str, object] = {
        "versions": versions.as_dict(),
        "model": model.as_dict(),
        "case": case.as_dict(),
    }
    if catalog is not None:
        payload["component_catalog"] = catalog.as_dict()
    if scenario is not None:
        payload["scenario"] = scenario.as_dict()
    return canonical_fingerprint(payload)


def resolve_repo_path(start: Path, relative_path: str) -> Path:
    """Resolve a repository-relative path while keeping callers location-independent."""

    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate / relative_path
    raise FileNotFoundError("repository root containing pyproject.toml was not found")
