"""Whitelisted M6 single-factor overlays and dynamic command mappings."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final

from ..core.config import CaseConfig, ModelConfig, canonical_fingerprint
from ..core.types import MaterialStream

STEADY_FACTOR_IDS: Final[tuple[str, ...]] = (
    "feed_load_ratio",
    "crude_lightness_shift_fraction",
    "feed_temperature_offset_k",
    "reflux_ratio_factor",
    "pump_around_1_duty_ratio",
    "pump_around_2_duty_ratio",
    "pump_around_3_duty_ratio",
    "flash_temperature_offset_k",
    "wash_water_ratio_factor",
    "column_cut_3_offset_k",
    "column_cut_4_offset_k",
)

DYNAMIC_COMMAND_FACTOR_TARGETS: Final[dict[str, str]] = {
    "feed_load_ratio": "fresh_feed_flow_kg_s",
    "available_furnace_duty_ratio": "furnace_fuel_duty_w",
    "condenser_cooling_capacity_ratio": "condenser_cooling_duty_w",
    "reflux_flow_ratio": "reflux_flow_kg_s",
    "pump_around_1_duty_ratio": "pump_around_1_duty_w",
    "pump_around_2_duty_ratio": "pump_around_2_duty_w",
    "pump_around_3_duty_ratio": "pump_around_3_duty_w",
}


def _finite(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


@dataclass(frozen=True)
class FactorApplication:
    """One immutable, traceable single-factor steady input overlay."""

    factor_id: str
    requested_value: float
    model: ModelConfig
    case: CaseConfig
    modified_paths: tuple[str, ...]
    input_fingerprint: str

    def __post_init__(self) -> None:
        if self.factor_id not in STEADY_FACTOR_IDS:
            raise ValueError("factor application has an unknown steady factor")
        object.__setattr__(
            self,
            "requested_value",
            _finite(self.requested_value, context="requested factor value"),
        )
        if not isinstance(self.model, ModelConfig) or not isinstance(self.case, CaseConfig):
            raise TypeError("factor application requires model and case configurations")
        paths = tuple(self.modified_paths)
        if not paths or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError("factor application must identify modified paths")
        if len(set(paths)) != len(paths):
            raise ValueError("factor application modified paths must be unique")
        expected_fingerprint = canonical_fingerprint(
            {
                "factor_id": self.factor_id,
                "requested_value": self.requested_value,
                "modified_paths": list(paths),
                "model": self.model.as_dict(),
                "case": self.case.as_dict(),
            }
        )
        if self.input_fingerprint != expected_fingerprint:
            raise ValueError("factor application fingerprint is inconsistent")
        object.__setattr__(self, "modified_paths", paths)

    def as_dict(self) -> dict[str, object]:
        return {
            "factor_id": self.factor_id,
            "requested_value": self.requested_value,
            "modified_paths": list(self.modified_paths),
            "model_fingerprint": canonical_fingerprint(self.model.as_dict()),
            "case_fingerprint": canonical_fingerprint(self.case.as_dict()),
            "input_fingerprint": self.input_fingerprint,
        }


def _model_with_equipment(model: ModelConfig, equipment: dict[str, object]) -> ModelConfig:
    data = model.as_dict()
    data["equipment"] = equipment
    return ModelConfig.from_mapping(data)


def _factor_result(
    factor_id: str,
    value: float,
    model: ModelConfig,
    case: CaseConfig,
    paths: tuple[str, ...],
) -> FactorApplication:
    fingerprint = canonical_fingerprint(
        {
            "factor_id": factor_id,
            "requested_value": value,
            "modified_paths": list(paths),
            "model": model.as_dict(),
            "case": case.as_dict(),
        }
    )
    return FactorApplication(factor_id, value, model, case, paths, fingerprint)


def apply_steady_factor(
    model: ModelConfig,
    case: CaseConfig,
    factor_id: str,
    requested_value: float,
) -> FactorApplication:
    """Apply exactly one documented M6 factor without mutating its inputs."""

    if not isinstance(model, ModelConfig) or not isinstance(case, CaseConfig):
        raise TypeError("steady factor input requires ModelConfig and CaseConfig")
    if factor_id not in STEADY_FACTOR_IDS:
        raise ValueError(f"unsupported steady factor {factor_id!r}")
    value = _finite(requested_value, context=factor_id)

    if factor_id == "feed_load_ratio":
        if value <= 0.0:
            raise ValueError("feed load ratio must be positive")
        feed = case.feed.at_conditions(
            mass_flow_kg_s=case.feed.mass_flow_kg_s * value
        )
        changed_case = replace(case, feed=feed)
        return _factor_result(
            factor_id,
            value,
            model,
            changed_case,
            ("case.feed.mass_flow_kg_s",),
        )

    if factor_id == "crude_lightness_shift_fraction":
        fractions = dict(case.feed.mass_fractions)
        fractions["naphtha"] = fractions.get("naphtha", 0.0) + value
        fractions["residue"] = fractions.get("residue", 0.0) - value
        if fractions["naphtha"] < 0.0 or fractions["residue"] < 0.0:
            raise ValueError("crude lightness shift makes a component negative")
        feed = MaterialStream(
            name=case.feed.name,
            mass_flow_kg_s=case.feed.mass_flow_kg_s,
            temperature_k=case.feed.temperature_k,
            pressure_pa=case.feed.pressure_pa,
            mass_fractions=fractions,
            salt_mass_flow_kg_s=case.feed.salt_mass_flow_kg_s,
            metadata=case.feed.metadata,
        )
        changed_case = replace(case, feed=feed)
        return _factor_result(
            factor_id,
            value,
            model,
            changed_case,
            (
                "case.feed.mass_fractions.naphtha",
                "case.feed.mass_fractions.residue",
            ),
        )

    if factor_id == "feed_temperature_offset_k":
        temperature = case.feed.temperature_k + value
        if temperature <= 0.0:
            raise ValueError("feed temperature offset implies non-positive temperature")
        changed_case = replace(
            case,
            feed=case.feed.at_conditions(temperature_k=temperature),
        )
        return _factor_result(
            factor_id,
            value,
            model,
            changed_case,
            ("case.feed.temperature_k",),
        )

    if factor_id == "flash_temperature_offset_k":
        conditions = dict(case.operating_conditions)
        temperature = conditions["flash_temperature_k"] + value
        if temperature <= 0.0:
            raise ValueError("flash temperature offset implies non-positive temperature")
        conditions["flash_temperature_k"] = temperature
        changed_case = replace(
            case,
            operating_conditions=MappingProxyType(conditions),
        )
        return _factor_result(
            factor_id,
            value,
            model,
            changed_case,
            ("case.operating_conditions.flash_temperature_k",),
        )

    equipment = model.as_dict()["equipment"]
    if not isinstance(equipment, dict):
        raise TypeError("model equipment did not serialize as an object")

    if factor_id == "reflux_ratio_factor":
        if value <= 0.0:
            raise ValueError("reflux ratio factor must be positive")
        recycle = equipment.get("recycle")
        if not isinstance(recycle, dict):
            raise TypeError("model recycle section is invalid")
        baseline = _finite(recycle["reflux_ratio"], context="reflux ratio")
        recycle["reflux_ratio"] = baseline * value
        changed_model = _model_with_equipment(model, equipment)
        return _factor_result(
            factor_id,
            value,
            changed_model,
            case,
            ("model.equipment.recycle.reflux_ratio",),
        )

    if factor_id.startswith("pump_around_"):
        if value < 0.0:
            raise ValueError("pump-around duty ratio must be non-negative")
        index = int(factor_id.removeprefix("pump_around_").split("_", 1)[0]) - 1
        recycle = equipment.get("recycle")
        if not isinstance(recycle, dict):
            raise TypeError("model recycle section is invalid")
        duties = recycle.get("pump_around_duties_w")
        if not isinstance(duties, list) or len(duties) != 3:
            raise TypeError("model pump-around duties are invalid")
        duties[index] = _finite(duties[index], context="pump-around duty") * value
        changed_model = _model_with_equipment(model, equipment)
        return _factor_result(
            factor_id,
            value,
            changed_model,
            case,
            (f"model.equipment.recycle.pump_around_duties_w[{index}]",),
        )

    if factor_id == "wash_water_ratio_factor":
        if value < 0.0:
            raise ValueError("wash-water ratio factor must be non-negative")
        desalter = equipment.get("desalter")
        if not isinstance(desalter, dict):
            raise TypeError("model desalter section is invalid")
        baseline = _finite(desalter["wash_water_ratio"], context="wash-water ratio")
        desalter["wash_water_ratio"] = baseline * value
        changed_model = _model_with_equipment(model, equipment)
        return _factor_result(
            factor_id,
            value,
            changed_model,
            case,
            ("model.equipment.desalter.wash_water_ratio",),
        )

    column = equipment.get("column")
    if not isinstance(column, dict):
        raise TypeError("model column section is invalid")
    cut_points = column.get("cut_points_k")
    if not isinstance(cut_points, list) or len(cut_points) != 4:
        raise TypeError("model cut points are invalid")
    index = 2 if factor_id == "column_cut_3_offset_k" else 3
    cut_points[index] = _finite(cut_points[index], context="column cut point") + value
    changed_model = _model_with_equipment(model, equipment)
    return _factor_result(
        factor_id,
        value,
        changed_model,
        case,
        (f"model.equipment.column.cut_points_k[{index}]",),
    )


def dynamic_command_for_factor(
    baseline_commands: dict[str, float] | MappingProxyType[str, float],
    factor_id: str,
    requested_ratio: float,
) -> tuple[str, float]:
    """Map one supported dynamic factor to one absolute M3 command value."""

    if factor_id not in DYNAMIC_COMMAND_FACTOR_TARGETS:
        raise ValueError(f"unsupported dynamic command factor {factor_id!r}")
    ratio = _finite(requested_ratio, context=factor_id)
    if ratio < 0.0:
        raise ValueError("dynamic command ratio must be non-negative")
    target = DYNAMIC_COMMAND_FACTOR_TARGETS[factor_id]
    if target not in baseline_commands:
        raise ValueError(f"baseline commands are missing {target!r}")
    baseline = _finite(baseline_commands[target], context=f"baseline command {target}")
    return target, baseline * ratio
