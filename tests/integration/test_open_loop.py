from __future__ import annotations

import math
from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ModelConfig,
    load_case_config,
    load_component_catalog,
    load_json,
    load_model_config,
)
from petroleum_rto.cdu.core.types import BalanceReport
from petroleum_rto.cdu.flowsheet.open_loop import (
    BOUNDARY_OUTLET_NAMES,
    MAIN_PRODUCT_NAMES,
    run_open_loop,
)
from petroleum_rto.cdu.flowsheet.results import SteadyFlowsheetResult
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS, ComponentCatalog


def _load_inputs(repo_root: Path) -> tuple[ModelConfig, CaseConfig, ComponentCatalog]:
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")
    catalog = load_component_catalog(repo_root / model.component_catalog_path)
    return model, case, catalog


def _closed(balance: BalanceReport) -> bool:
    return balance.passed(
        mass_atol_kg_s=1e-9,
        component_atol_kg_s=1e-9,
        salt_atol_kg_s=1e-12,
        energy_atol_w=1e-5,
    )


@pytest.fixture(scope="module")
def baseline_inputs(repo_root: Path) -> tuple[ModelConfig, CaseConfig, ComponentCatalog]:
    return _load_inputs(repo_root)


@pytest.fixture(scope="module")
def baseline_result(
    baseline_inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
) -> SteadyFlowsheetResult:
    return run_open_loop(*baseline_inputs)


def test_baseline_closes_all_declared_balances(
    baseline_result: SteadyFlowsheetResult,
) -> None:
    assert baseline_result.status == "success"
    assert baseline_result.balance.energy_residual_w is not None
    assert _closed(baseline_result.balance)
    for unit_name, unit_result in baseline_result.unit_results.items():
        assert unit_result.balance is not None, unit_name
        assert unit_result.balance.energy_residual_w is not None, unit_name
        assert _closed(unit_result.balance), unit_name


def test_boundary_outputs_are_complete_finite_and_nonnegative(
    baseline_result: SteadyFlowsheetResult,
) -> None:
    assert set(baseline_result.products) == set(BOUNDARY_OUTLET_NAMES)
    assert set(baseline_result.qualities) == set(MAIN_PRODUCT_NAMES)
    for stream in baseline_result.products.values():
        scalars = (
            stream.mass_flow_kg_s,
            stream.temperature_k,
            stream.pressure_pa,
            stream.salt_mass_flow_kg_s,
            *stream.mass_fractions.values(),
        )
        assert all(math.isfinite(value) and value >= 0.0 for value in scalars)
        assert stream.temperature_k > 0.0
        assert stream.pressure_pa > 0.0
        assert sum(stream.mass_fractions.values()) == pytest.approx(1.0, abs=1e-12)
    for indicators in baseline_result.qualities.values():
        assert all(math.isfinite(value) and value >= 0.0 for value in indicators.values())


def test_flash_vapor_is_included_in_column_feed(
    baseline_result: SteadyFlowsheetResult,
) -> None:
    flash_vapor = baseline_result.streams["flash_vapor"]
    furnace_outlet = baseline_result.streams["furnace_outlet"]
    column = baseline_result.unit_results["column"]

    assert column.balance is not None
    assert flash_vapor.mass_flow_kg_s > 0.0
    assert column.balance.inlet_kg_s == pytest.approx(
        furnace_outlet.mass_flow_kg_s + flash_vapor.mass_flow_kg_s,
        abs=1e-12,
    )
    for component in ALL_COMPONENTS:
        column_inlet = (
            furnace_outlet.component_flow_kg_s(component)
            + flash_vapor.component_flow_kg_s(component)
        )
        column_outlet = sum(
            stream.component_flow_kg_s(component) for stream in column.outlets.values()
        )
        assert column_outlet == pytest.approx(column_inlet, abs=1e-10)


def test_same_inputs_are_exactly_repeatable(
    baseline_inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
) -> None:
    first = run_open_loop(*baseline_inputs)
    second = run_open_loop(*baseline_inputs)

    assert first.as_dict() == second.as_dict()


def test_higher_flash_temperature_does_not_reduce_vapor_fraction(
    repo_root: Path,
    baseline_inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    baseline_result: SteadyFlowsheetResult,
) -> None:
    model, _, catalog = baseline_inputs
    case_data = load_json(repo_root / "configs/cases/case_20260604.json")
    temperature = case_data["operating_conditions"]["flash_temperature_k"]
    temperature["value"] = float(temperature["value"]) + 10.0
    hotter_case = CaseConfig.from_mapping(case_data)

    hotter = run_open_loop(model, hotter_case, catalog)

    assert hotter.diagnostics["flash_vapor_fraction"] >= baseline_result.diagnostics[
        "flash_vapor_fraction"
    ]


def test_higher_first_cut_point_increases_gasoline(
    repo_root: Path,
    baseline_inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    baseline_result: SteadyFlowsheetResult,
) -> None:
    _, case, catalog = baseline_inputs
    model_data = load_json(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    cut_points = model_data["equipment"]["column"]["cut_points_k"]
    cut_points[0] = float(cut_points[0]) + 10.0
    wider_gasoline_model = ModelConfig.from_mapping(model_data)

    wider = run_open_loop(wider_gasoline_model, case, catalog)

    assert (
        wider.products["gasoline"].mass_flow_kg_s
        > baseline_result.products["gasoline"].mass_flow_kg_s
    )
