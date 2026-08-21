from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import load_component_catalog
from petroleum_rto.cdu.core.types import MaterialStream
from petroleum_rto.cdu.equipment.heat_transfer import EquivalentPreheater, Furnace
from petroleum_rto.cdu.properties.thermo import ReducedThermo


@pytest.fixture
def feed_and_thermo(repo_root: Path) -> tuple[MaterialStream, ReducedThermo]:
    catalog = load_component_catalog(repo_root / "configs/cdu/models/components_v0.1.0.json")
    feed = MaterialStream(
        "feed",
        100.0,
        350.0,
        300000.0,
        {"naphtha": 0.2, "residue": 0.8},
    )
    return feed, ReducedThermo(catalog)


def test_preheater_conserves_and_responds_to_available_duty(
    feed_and_thermo: tuple[MaterialStream, ReducedThermo],
) -> None:
    feed, thermo = feed_and_thermo
    heater = EquivalentPreheater(thermo, 0.8, 500.0, 10000.0)
    full = heater.solve(feed)
    limited = heater.solve(feed, available_duty_w=full.duty_w * 0.5)
    assert full.balance is not None and full.balance.passed(energy_atol_w=1e-6)
    assert full.outlets["heated"].temperature_k > limited.outlets["heated"].temperature_k
    assert full.outlets["heated"].pressure_pa < feed.pressure_pa


def test_furnace_target_and_fuel_modes_are_consistent(
    feed_and_thermo: tuple[MaterialStream, ReducedThermo],
) -> None:
    feed, thermo = feed_and_thermo
    furnace = Furnace(thermo, 0.85, 1.0e6, 650.0, 5000.0)
    target = furnace.solve(feed, outlet_temperature_k=600.0)
    fuel = furnace.solve(feed, fuel_duty_w=target.diagnostics["fuel_duty_w"])
    assert fuel.outlets["furnace_outlet"].temperature_k == pytest.approx(600.0)
    assert target.balance is not None and target.balance.passed(energy_atol_w=1e-6)


def test_furnace_rejects_ambiguous_or_unsafe_inputs(
    feed_and_thermo: tuple[MaterialStream, ReducedThermo],
) -> None:
    feed, thermo = feed_and_thermo
    furnace = Furnace(thermo, 0.85, 1.0e6, 650.0)
    with pytest.raises(ValueError, match="exactly one"):
        furnace.solve(feed)
    with pytest.raises(ValueError, match="outside"):
        furnace.solve(feed, outlet_temperature_k=700.0)
    with pytest.raises(ValueError, match="heat-loss threshold"):
        furnace.solve(feed, fuel_duty_w=1.0e6)
