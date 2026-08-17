from __future__ import annotations

import math

import pytest

from petroleum_rto.cdu.core.units import (
    UnitConversionError,
    mass_flow_from_si,
    mass_flow_to_si,
    parse_quantity,
    pressure_from_si,
    pressure_to_si,
    temperature_from_si,
    temperature_to_si,
    time_to_si,
)


def test_supported_unit_round_trips() -> None:
    assert mass_flow_to_si(3.6, "t/h") == pytest.approx(1.0)
    assert mass_flow_from_si(1.0, "t/h") == pytest.approx(3.6)
    assert temperature_to_si(25.0, "degC") == pytest.approx(298.15)
    assert temperature_from_si(298.15, "℃") == pytest.approx(25.0)
    assert pressure_to_si(0.1, "MPa") == pytest.approx(100000.0)
    assert pressure_from_si(100000.0, "kPa") == pytest.approx(100.0)
    assert pressure_to_si(0.051, "MPa", basis="gauge") == pytest.approx(152325.0)
    assert pressure_from_si(152325.0, "MPa", basis="gauge") == pytest.approx(0.051)
    assert time_to_si(2.0, "h") == pytest.approx(7200.0)


def test_unsupported_or_ambiguous_units_are_rejected() -> None:
    with pytest.raises(UnitConversionError):
        mass_flow_to_si(1.0, "tons")
    with pytest.raises(UnitConversionError):
        temperature_to_si(25.0, "C")
    with pytest.raises(UnitConversionError):
        pressure_to_si(1.0, "bar")
    with pytest.raises(UnitConversionError):
        parse_quantity({"value": 0.1, "unit": "MPa"}, dimension="pressure")
    with pytest.raises(UnitConversionError):
        parse_quantity({"value": 1.0, "unit": "kg/s", "extra": 1}, dimension="mass_flow")
    with pytest.raises(UnitConversionError):
        parse_quantity({"value": math.nan, "unit": "kg/s"}, dimension="mass_flow")
