from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ModelConfig,
    load_case_config,
    load_component_catalog,
    load_model_config,
)
from petroleum_rto.cdu.core.types import BalanceReport, MaterialStream
from petroleum_rto.cdu.flowsheet.open_loop import BOUNDARY_OUTLET_NAMES, run_open_loop
from petroleum_rto.cdu.flowsheet.recycle import (
    RecycleSettings,
    RecycleSolveResult,
    solve_recycle,
)
from petroleum_rto.cdu.flowsheet.results import SteadyFlowsheetResult
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS, ComponentCatalog
from petroleum_rto.cdu.repository import resolve_cdu_repository_path


def _load_inputs(repo_root: Path) -> tuple[ModelConfig, CaseConfig, ComponentCatalog]:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    catalog = load_component_catalog(
        resolve_cdu_repository_path(repo_root, model.component_catalog_path)
    )
    return model, case, catalog


def _closed(balance: BalanceReport) -> bool:
    return balance.passed(
        mass_atol_kg_s=1e-9,
        component_atol_kg_s=1e-9,
        salt_atol_kg_s=1e-12,
        energy_atol_w=1e-5,
    )


@pytest.fixture(scope="module")
def inputs(repo_root: Path) -> tuple[ModelConfig, CaseConfig, ComponentCatalog]:
    return _load_inputs(repo_root)


@pytest.fixture(scope="module")
def settings(inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog]) -> RecycleSettings:
    return RecycleSettings.from_model(inputs[0])


@pytest.fixture(scope="module")
def default_solve(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
) -> RecycleSolveResult:
    return solve_recycle(*inputs)


@pytest.fixture(scope="module")
def m1_result(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
) -> SteadyFlowsheetResult:
    return run_open_loop(*inputs)


def test_default_recycle_converges_with_decreasing_residuals(
    settings: RecycleSettings,
    default_solve: RecycleSolveResult,
) -> None:
    assert default_solve.status == "success"
    assert default_solve.converged
    assert default_solve.final_residual is not None
    assert default_solve.final_residual <= settings.tolerance
    assert len(default_solve.residual_history) == default_solve.iterations
    assert default_solve.final_residual == default_solve.residual_history[-1]
    assert default_solve.residual_history[-1] < default_solve.residual_history[0]
    decreases = sum(later <= earlier for earlier, later in pairwise(default_solve.residual_history))
    assert decreases >= (len(default_solve.residual_history) - 1) // 2


def test_zero_reflux_with_nominal_heat_recovery_exactly_reduces_to_m1(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    settings: RecycleSettings,
    m1_result: SteadyFlowsheetResult,
) -> None:
    zero_settings = replace(
        settings,
        reflux_ratio=0.0,
    )
    recycled = solve_recycle(*inputs, settings=zero_settings).require_converged()

    assert recycled.products == m1_result.products
    assert (
        recycled.diagnostics["furnace_process_duty_w"]
        == m1_result.diagnostics["furnace_process_duty_w"]
    )
    assert (
        recycled.diagnostics["furnace_fuel_duty_w"] == m1_result.diagnostics["furnace_fuel_duty_w"]
    )


def test_initial_reflux_guesses_converge_to_same_solution(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    settings: RecycleSettings,
    m1_result: SteadyFlowsheetResult,
) -> None:
    _, case, _ = inputs
    zero = MaterialStream(
        "initial_reflux",
        0.0,
        case.operating_conditions["condenser_temperature_k"],
        case.operating_conditions["tower_top_pressure_pa"],
        {"naphtha": 1.0},
    )
    oil_estimate = m1_result.products["gasoline"].at_conditions(
        name="initial_reflux",
        mass_flow_kg_s=(
            m1_result.products["gasoline"].mass_flow_kg_s
            * settings.reflux_ratio
            / (1.0 + settings.reflux_ratio)
        ),
    )
    high_estimate = oil_estimate.at_conditions(mass_flow_kg_s=1.8 * oil_estimate.mass_flow_kg_s)
    solved = [
        solve_recycle(*inputs, initial_reflux=guess)
        for guess in (zero, oil_estimate, high_estimate)
    ]

    assert all(result.status == "success" for result in solved)
    reference = solved[0].require_converged()
    reference_reflux = solved[0].reflux
    assert reference_reflux is not None
    for result in solved[1:]:
        flowsheet = result.require_converged()
        assert result.reflux is not None
        assert result.reflux.mass_flow_kg_s == pytest.approx(
            reference_reflux.mass_flow_kg_s,
            abs=5e-6,
        )
        for component in ALL_COMPONENTS:
            assert result.reflux.component_flow_kg_s(component) == pytest.approx(
                reference_reflux.component_flow_kg_s(component),
                abs=5e-6,
            )
        for product_name, product in reference.products.items():
            candidate = flowsheet.products[product_name]
            assert candidate.mass_flow_kg_s == pytest.approx(product.mass_flow_kg_s, abs=5e-6)
            for component in ALL_COMPONENTS:
                assert candidate.component_flow_kg_s(component) == pytest.approx(
                    product.component_flow_kg_s(component),
                    abs=5e-6,
                )


def test_higher_reflux_ratio_increases_realized_ratio_and_not_gasoline_t90(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    settings: RecycleSettings,
    default_solve: RecycleSolveResult,
) -> None:
    baseline = default_solve.require_converged()
    higher = solve_recycle(
        *inputs,
        settings=replace(settings, reflux_ratio=0.8),
    ).require_converged()

    assert (
        higher.diagnostics["realized_reflux_ratio"] > baseline.diagnostics["realized_reflux_ratio"]
    )
    assert (
        higher.qualities["gasoline"]["t90_k_proxy"] <= baseline.qualities["gasoline"]["t90_k_proxy"]
    )


def test_more_recovered_heat_reduces_furnace_fuel_at_fixed_outlet(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    settings: RecycleSettings,
) -> None:
    low = solve_recycle(
        *inputs,
        settings=replace(settings, maximum_recovered_duty_w=8_000_000.0),
    ).require_converged()
    high = solve_recycle(
        *inputs,
        settings=replace(settings, maximum_recovered_duty_w=18_000_000.0),
    ).require_converged()

    assert high.diagnostics["actual_recovered_duty_w"] > low.diagnostics["actual_recovered_duty_w"]
    assert high.streams["furnace_outlet"].temperature_k == pytest.approx(
        low.streams["furnace_outlet"].temperature_k,
        abs=1e-12,
    )
    assert high.diagnostics["furnace_fuel_duty_w"] < low.diagnostics["furnace_fuel_duty_w"]


def test_iteration_limit_preserves_nonconverged_last_state(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
    settings: RecycleSettings,
) -> None:
    result = solve_recycle(*inputs, settings=replace(settings, maximum_iterations=1))

    assert result.status == "not_converged"
    assert not result.converged
    assert result.iterations == 1
    assert result.residual_history
    assert result.final_residual == result.residual_history[-1]
    assert result.reflux is not None
    assert result.flowsheet is not None
    assert result.flowsheet.status == "not_converged"
    assert result.flowsheet.products
    assert result.failure_reason
    assert result.failure_stage == "convergence"
    assert result.flowsheet.streams["reflux"] == result.reflux
    with pytest.raises(RuntimeError):
        result.require_converged()


def test_excessive_initial_reflux_is_rejected(
    inputs: tuple[ModelConfig, CaseConfig, ComponentCatalog],
) -> None:
    _, case, _ = inputs
    excessive = MaterialStream(
        "initial_reflux",
        5.1 * case.feed.mass_flow_kg_s,
        case.operating_conditions["condenser_temperature_k"],
        case.operating_conditions["tower_top_pressure_pa"],
        {"naphtha": 1.0},
    )

    result = solve_recycle(*inputs, initial_reflux=excessive)

    assert result.status == "rejected"
    assert not result.converged
    assert result.iterations == 0
    assert result.flowsheet is None
    assert not result.residual_history
    assert result.failure_reason
    assert result.failure_stage == "initial_reflux"


def test_invalid_custom_recycle_settings_are_rejected(
    settings: RecycleSettings,
) -> None:
    with pytest.raises(TypeError, match="non-boolean integer"):
        replace(settings, maximum_iterations=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly three"):
        replace(settings, pump_around_duties_w=(1.0, 2.0))  # type: ignore[arg-type]


def test_successful_recycle_closes_net_boundary_and_all_units(
    default_solve: RecycleSolveResult,
) -> None:
    flowsheet = default_solve.require_converged()

    assert set(flowsheet.products) == set(BOUNDARY_OUTLET_NAMES)
    assert "reflux" not in flowsheet.products
    assert flowsheet.balance.energy_residual_w is not None
    assert _closed(flowsheet.balance)
    for unit_name, unit_result in flowsheet.unit_results.items():
        assert unit_result.balance is not None, unit_name
        assert unit_result.balance.energy_residual_w is not None, unit_name
        assert _closed(unit_result.balance), unit_name
