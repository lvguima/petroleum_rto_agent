from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import load_component_catalog
from petroleum_rto.cdu.core.types import MaterialStream
from petroleum_rto.cdu.equipment.quality import quality_proxies
from petroleum_rto.cdu.equipment.separation import (
    PRODUCT_NAMES,
    Desalter,
    IsothermalFlash,
    OverheadCondenser,
    ReducedColumn,
)
from petroleum_rto.cdu.properties.thermo import ReducedThermo


@pytest.fixture
def thermo(repo_root: Path) -> ReducedThermo:
    return ReducedThermo(
        load_component_catalog(repo_root / "configs/cdu/models/components_v0.1.0.json")
    )


@pytest.fixture
def representative_feed() -> MaterialStream:
    return MaterialStream(
        "feed",
        100.0,
        500.0,
        300000.0,
        {
            "light_ends": 0.05,
            "naphtha": 0.25,
            "kerosene": 0.20,
            "light_diesel": 0.15,
            "heavy_diesel": 0.15,
            "residue": 0.19,
            "water": 0.01,
        },
        salt_mass_flow_kg_s=0.01,
    )


def test_desalter_conserves_and_removes_water_and_salt(
    thermo: ReducedThermo,
    representative_feed: MaterialStream,
) -> None:
    wash = MaterialStream("wash", 4.0, 330.0, 300000.0, {"water": 1.0})
    result = Desalter(thermo, 0.97, 0.95, 0.0005, 10000.0).solve(
        representative_feed,
        wash,
    )
    assert result.balance is not None and result.balance.passed(energy_atol_w=1e-6)
    assert (
        result.outlets["desalted_crude"].component_flow_kg_s("water")
        < representative_feed.component_flow_kg_s("water")
    )
    assert result.outlets["desalted_crude"].salt_mass_flow_kg_s == pytest.approx(
        0.0005
    )
    assert result.outlets["brine"].salt_mass_flow_kg_s == pytest.approx(0.0095)


def test_flash_is_conservative_and_has_correct_temperature_pressure_directions(
    thermo: ReducedThermo,
    representative_feed: MaterialStream,
) -> None:
    base = IsothermalFlash(thermo, 500.0, 150000.0).solve(representative_feed)
    hotter = IsothermalFlash(thermo, 550.0, 150000.0).solve(representative_feed)
    higher_pressure = IsothermalFlash(thermo, 500.0, 200000.0).solve(
        representative_feed
    )
    assert base.balance is not None and base.balance.passed(energy_atol_w=1e-6)
    assert (
        hotter.diagnostics["mass_vapor_fraction"]
        > base.diagnostics["mass_vapor_fraction"]
    )
    assert (
        higher_pressure.diagnostics["mass_vapor_fraction"]
        < base.diagnostics["mass_vapor_fraction"]
    )
    assert base.outlets["liquid"].salt_mass_flow_kg_s == pytest.approx(0.01)
    assert base.outlets["vapor"].salt_mass_flow_kg_s == 0.0


def make_column(thermo: ReducedThermo, first_cut_k: float = 448.15) -> ReducedColumn:
    return ReducedColumn(
        thermo,
        150000.0,
        (first_cut_k, 524.15, 583.15, 638.15),
        (12.0, 14.0, 16.0, 18.0),
        {
            "overhead": 386.65,
            "kerosene": 438.15,
            "light_diesel": 533.15,
            "heavy_diesel": 573.15,
            "residue": 623.15,
        },
    )


def test_column_matrix_and_products_are_conservative(
    thermo: ReducedThermo,
    representative_feed: MaterialStream,
) -> None:
    flash = IsothermalFlash(thermo, 500.0, 160000.0).solve(representative_feed)
    hot_liquid = flash.outlets["liquid"].at_conditions(
        name="hot_liquid",
        temperature_k=620.0,
    )
    result = make_column(thermo).solve(hot_liquid, flash.outlets["vapor"])
    assert result.unit_result.balance is not None
    assert result.unit_result.balance.passed(energy_atol_w=1e-6)
    for component in representative_feed.mass_fractions:
        assert sum(
            result.split_matrix[product][component] for product in PRODUCT_NAMES
        ) == pytest.approx(1.0)
    assert result.unit_result.outlets["residue"].salt_mass_flow_kg_s == pytest.approx(
        0.01
    )
    assert all(
        stream.mass_flow_kg_s >= 0.0 for stream in result.unit_result.outlets.values()
    )


def test_raising_top_cut_increases_overhead_yield(
    thermo: ReducedThermo,
    representative_feed: MaterialStream,
) -> None:
    flash = IsothermalFlash(thermo, 500.0, 160000.0).solve(representative_feed)
    hot_liquid = flash.outlets["liquid"].at_conditions(temperature_k=620.0)
    base = make_column(thermo).solve(hot_liquid, flash.outlets["vapor"])
    raised = make_column(thermo, first_cut_k=468.15).solve(
        hot_liquid,
        flash.outlets["vapor"],
    )
    assert (
        raised.unit_result.outlets["overhead"].mass_flow_kg_s
        > base.unit_result.outlets["overhead"].mass_flow_kg_s
    )


def test_condenser_closes_three_phase_boundary(
    thermo: ReducedThermo,
    representative_feed: MaterialStream,
) -> None:
    overhead = representative_feed.at_conditions(
        name="overhead",
        temperature_k=390.0,
        pressure_pa=150000.0,
    )
    overhead = MaterialStream(
        overhead.name,
        overhead.mass_flow_kg_s,
        overhead.temperature_k,
        overhead.pressure_pa,
        overhead.mass_fractions,
        salt_mass_flow_kg_s=0.0,
    )
    result = OverheadCondenser(thermo, 313.15, 150000.0).solve(overhead)
    assert result.balance is not None and result.balance.passed(energy_atol_w=1e-6)
    assert set(result.outlets) == {"offgas", "oil_condensate", "aqueous"}
    assert result.outlets["aqueous"].component_flow_kg_s("water") == pytest.approx(1.0)
    assert result.duty_w < 0.0


def test_condenser_temperature_direction_and_invalid_conditions_are_explicit(
    thermo: ReducedThermo,
) -> None:
    overhead = MaterialStream(
        "overhead",
        10.0,
        390.0,
        160000.0,
        {"light_ends": 0.2, "naphtha": 0.8},
    )
    cold = OverheadCondenser(thermo, 300.0, 150000.0, 18.0).solve(overhead)
    warm = OverheadCondenser(thermo, 330.0, 150000.0, 18.0).solve(overhead)
    assert cold.outlets["offgas"].mass_flow_kg_s < warm.outlets["offgas"].mass_flow_kg_s
    with pytest.raises(ValueError, match="raise pressure"):
        OverheadCondenser(thermo, 313.15, 170000.0).solve(overhead)
    with pytest.raises(ValueError, match="cannot exceed inlet"):
        OverheadCondenser(thermo, 400.0, 150000.0).solve(overhead)


def test_column_rejects_zero_flow_and_nonfinite_cut_points(
    thermo: ReducedThermo,
) -> None:
    zero_liquid = MaterialStream(
        "zero_liquid",
        0.0,
        600.0,
        160000.0,
        {"residue": 1.0},
    )
    zero_vapor = MaterialStream(
        "zero_vapor",
        0.0,
        500.0,
        160000.0,
        {"naphtha": 1.0},
    )
    with pytest.raises(ValueError, match="positive total inlet"):
        make_column(thermo).solve(zero_liquid, zero_vapor)
    with pytest.raises(ValueError, match="four increasing"):
        ReducedColumn(
            thermo,
            150000.0,
            (float("nan"), 524.15, 583.15, 638.15),
            (12.0, 14.0, 16.0, 18.0),
            make_column(thermo).product_temperatures_k,
        )


def test_quality_proxies_move_with_product_heaviness(thermo: ReducedThermo) -> None:
    light = MaterialStream("light", 1.0, 300.0, 100000.0, {"naphtha": 1.0})
    heavy = MaterialStream("heavy", 1.0, 400.0, 100000.0, {"heavy_diesel": 1.0})
    light_quality = quality_proxies(light, thermo.catalog)
    heavy_quality = quality_proxies(heavy, thermo.catalog)
    assert (
        heavy_quality["density_kg_m3_proxy"]
        > light_quality["density_kg_m3_proxy"]
    )
    assert heavy_quality["t50_k_proxy"] > light_quality["t50_k_proxy"]
