from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import load_component_catalog
from petroleum_rto.cdu.core.types import MaterialStream
from petroleum_rto.cdu.properties.thermo import ReducedThermo


@pytest.fixture
def thermo(repo_root: Path) -> ReducedThermo:
    catalog = load_component_catalog(repo_root / "configs/models/components_v0.1.0.json")
    return ReducedThermo(catalog)


def test_liquid_enthalpy_and_inverse_are_consistent(thermo: ReducedThermo) -> None:
    composition = {"naphtha": 0.4, "residue": 0.6}
    enthalpy = thermo.liquid_specific_enthalpy(composition, 400.0)
    assert thermo.temperature_from_liquid_enthalpy(composition, enthalpy) == pytest.approx(
        400.0
    )


def test_enthalpy_mixing_conserves_components_salt_and_energy(
    thermo: ReducedThermo,
) -> None:
    cold = MaterialStream(
        "cold",
        10.0,
        300.0,
        300000.0,
        {"naphtha": 1.0},
        salt_mass_flow_kg_s=0.001,
    )
    hot = MaterialStream(
        "hot",
        20.0,
        500.0,
        250000.0,
        {"residue": 1.0},
        salt_mass_flow_kg_s=0.002,
    )
    mixed = thermo.mix_by_enthalpy("mixed", [cold, hot])
    assert mixed.mass_flow_kg_s == pytest.approx(30.0)
    assert mixed.component_flow_kg_s("naphtha") == pytest.approx(10.0)
    assert mixed.salt_mass_flow_kg_s == pytest.approx(0.003)
    assert thermo.stream_enthalpy_w(mixed) == pytest.approx(
        thermo.stream_enthalpy_w(cold) + thermo.stream_enthalpy_w(hot)
    )


def test_vapor_pressure_increases_with_temperature(thermo: ReducedThermo) -> None:
    low = thermo.vapor_pressure_pa("naphtha", 350.0)
    high = thermo.vapor_pressure_pa("naphtha", 400.0)
    assert high > low


def test_thermo_rejects_invalid_composition_and_passive_pressure_gain(
    thermo: ReducedThermo,
) -> None:
    feed = MaterialStream("feed", 1.0, 350.0, 200000.0, {"naphtha": 1.0})
    with pytest.raises(ValueError, match="finite and non-negative"):
        thermo.hydrocarbon_mole_fractions({"naphtha": 1.1, "residue": -0.1})
    with pytest.raises(ValueError, match="cannot raise pressure"):
        thermo.mix_by_enthalpy("mixed", [feed], pressure_pa=210000.0)
