"""Explicit conversions at the model input and output boundaries."""

from __future__ import annotations

import math
from collections.abc import Mapping


class UnitConversionError(ValueError):
    """Raised for an unsupported unit or invalid quantity."""


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise UnitConversionError("quantity value must be finite")
    return value


def mass_flow_to_si(value: float, unit: str) -> float:
    """Convert a mass flow to kg/s."""

    factors = {"kg/s": 1.0, "t/h": 1000.0 / 3600.0}
    try:
        return _finite(value) * factors[unit]
    except KeyError as exc:
        raise UnitConversionError(f"unsupported mass-flow unit: {unit!r}") from exc


def mass_flow_from_si(value: float, unit: str) -> float:
    """Convert a kg/s mass flow to the requested display unit."""

    factors = {"kg/s": 1.0, "t/h": 3600.0 / 1000.0}
    try:
        return _finite(value) * factors[unit]
    except KeyError as exc:
        raise UnitConversionError(f"unsupported mass-flow unit: {unit!r}") from exc


def temperature_to_si(value: float, unit: str) -> float:
    """Convert an absolute temperature to kelvin."""

    value = _finite(value)
    if unit == "K":
        result = value
    elif unit in {"degC", "℃"}:
        result = value + 273.15
    else:
        raise UnitConversionError(f"unsupported temperature unit: {unit!r}")
    if result <= 0.0:
        raise UnitConversionError("absolute temperature must be positive")
    return result


def temperature_from_si(value: float, unit: str) -> float:
    """Convert a kelvin temperature to the requested display unit."""

    value = _finite(value)
    if value <= 0.0:
        raise UnitConversionError("absolute temperature must be positive")
    if unit == "K":
        return value
    if unit in {"degC", "℃"}:
        return value - 273.15
    raise UnitConversionError(f"unsupported temperature unit: {unit!r}")


def pressure_to_si(
    value: float,
    unit: str,
    *,
    basis: str = "absolute",
    atmospheric_pressure_pa: float = 101325.0,
) -> float:
    """Convert an absolute pressure to pascal."""

    factors = {"Pa": 1.0, "kPa": 1_000.0, "MPa": 1_000_000.0}
    try:
        result = _finite(value) * factors[unit]
    except KeyError as exc:
        raise UnitConversionError(f"unsupported pressure unit: {unit!r}") from exc
    atmospheric_pressure_pa = _finite(atmospheric_pressure_pa)
    if atmospheric_pressure_pa <= 0.0:
        raise UnitConversionError("atmospheric pressure must be positive")
    if basis == "gauge":
        result += atmospheric_pressure_pa
    elif basis != "absolute":
        raise UnitConversionError(f"unsupported pressure basis: {basis!r}")
    if result <= 0.0:
        raise UnitConversionError("absolute pressure must be positive")
    return result


def pressure_from_si(
    value: float,
    unit: str,
    *,
    basis: str = "absolute",
    atmospheric_pressure_pa: float = 101325.0,
) -> float:
    """Convert a pascal pressure to the requested display unit."""

    factors = {"Pa": 1.0, "kPa": 1.0 / 1_000.0, "MPa": 1.0 / 1_000_000.0}
    value = _finite(value)
    if value <= 0.0:
        raise UnitConversionError("absolute pressure must be positive")
    atmospheric_pressure_pa = _finite(atmospheric_pressure_pa)
    if basis == "gauge":
        value -= atmospheric_pressure_pa
    elif basis != "absolute":
        raise UnitConversionError(f"unsupported pressure basis: {basis!r}")
    try:
        return value * factors[unit]
    except KeyError as exc:
        raise UnitConversionError(f"unsupported pressure unit: {unit!r}") from exc


def time_to_si(value: float, unit: str) -> float:
    """Convert a duration to seconds."""

    factors = {"s": 1.0, "min": 60.0, "h": 3600.0}
    try:
        result = _finite(value) * factors[unit]
    except KeyError as exc:
        raise UnitConversionError(f"unsupported time unit: {unit!r}") from exc
    if result < 0.0:
        raise UnitConversionError("duration cannot be negative")
    return result


def parse_quantity(value: object, *, dimension: str) -> float:
    """Parse a strict ``{"value": number, "unit": str}`` quantity."""

    if not isinstance(value, Mapping):
        raise UnitConversionError("quantity must be an object")
    expected = {"value", "unit", "basis"} if dimension == "pressure" else {"value", "unit"}
    if set(value) != expected:
        raise UnitConversionError(f"{dimension} quantity fields must be exactly {sorted(expected)}")
    raw_value = value["value"]
    unit = value["unit"]
    if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
        raise UnitConversionError("quantity value must be numeric")
    if not isinstance(unit, str):
        raise UnitConversionError("quantity unit must be a string")
    if dimension == "pressure":
        basis = value["basis"]
        if not isinstance(basis, str):
            raise UnitConversionError("pressure basis must be a string")
        return pressure_to_si(float(raw_value), unit, basis=basis)
    converters = {
        "mass_flow": mass_flow_to_si,
        "temperature": temperature_to_si,
        "time": time_to_si,
    }
    try:
        converter = converters[dimension]
    except KeyError as exc:
        raise UnitConversionError(f"unsupported quantity dimension: {dimension!r}") from exc
    return converter(float(raw_value), unit)
