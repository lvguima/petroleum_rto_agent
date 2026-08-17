from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ModelConfig,
    load_case_config,
    load_component_catalog,
    load_model_config,
)
from petroleum_rto.cdu.core.types import MaterialStream
from petroleum_rto.cdu.dynamics.equations import (
    DynamicEvaluation,
    OpenLoopDynamicModel,
)
from petroleum_rto.cdu.dynamics.initialization import (
    initialize_open_loop_dynamic_model,
)
from petroleum_rto.cdu.dynamics.state import DynamicState
from petroleum_rto.cdu.equipment.quality import quality_proxies
from petroleum_rto.cdu.flowsheet.recycle import RecycleSolveResult, solve_recycle
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS, ComponentCatalog

_RHS_TOLERANCE = 1e-6
_COMPONENT_TOLERANCE_KG_S = 1e-10
_SALT_TOLERANCE_KG_S = 1e-12
_NET_PRODUCT_NAMES = (
    "gasoline",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
)
_PRODUCT_STREAM_FLOW_NAMES = {
    "gasoline": "gasoline",
    "kerosene": "kerosene",
    "light_diesel": "light_diesel",
    "heavy_diesel": "heavy_diesel",
    "residue": "residue_product",
}


@dataclass(frozen=True)
class DynamicCase:
    model_config: ModelConfig
    case_config: CaseConfig
    catalog: ComponentCatalog
    recycle_result: RecycleSolveResult
    dynamic_model: OpenLoopDynamicModel


@pytest.fixture(scope="module")
def dynamic_case(repo_root: Path) -> DynamicCase:
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")
    catalog = load_component_catalog(repo_root / model.component_catalog_path)
    recycle_result = solve_recycle(model, case, catalog)
    assert recycle_result.status == "success"
    dynamic_model = initialize_open_loop_dynamic_model(
        model,
        case,
        catalog,
        recycle_result,
    )
    return DynamicCase(model, case, catalog, recycle_result, dynamic_model)


def _state_with_actuator_change(
    dynamic_model: OpenLoopDynamicModel,
    actuator_name: str,
    factor: float,
) -> DynamicState:
    vector = list(dynamic_model.initial_state.to_vector())
    vector_name = f"actuator_states.{actuator_name}"
    vector[DynamicState.vector_names().index(vector_name)] *= factor
    return DynamicState.from_vector(vector)


def _evaluate_at_actual_actuators(
    dynamic_model: OpenLoopDynamicModel,
    state: DynamicState,
) -> DynamicEvaluation:
    return dynamic_model.evaluate(state, dict(state.actuator_states))


def _named_derivative(evaluation: DynamicEvaluation, vector_name: str) -> float:
    return evaluation.derivative_vector[DynamicState.vector_names().index(vector_name)]


def _liquid_bulk_derivative(
    evaluation: DynamicEvaluation,
    inventory_name: str,
) -> float:
    return sum(
        _named_derivative(
            evaluation,
            f"liquid_inventories.{inventory_name}.component_masses_kg.{component}",
        )
        for component in ALL_COMPONENTS
    )


def _top_gas_bulk_derivative(evaluation: DynamicEvaluation) -> float:
    return sum(
        _named_derivative(
            evaluation,
            f"top_gas_component_masses_kg.{component}",
        )
        for component in ALL_COMPONENTS
    )


def _assert_instantaneous_conservation(evaluation: DynamicEvaluation) -> None:
    for component in ALL_COMPONENTS:
        assert evaluation.boundary_balance.component_residuals_kg_s[
            component
        ] == pytest.approx(0.0, abs=_COMPONENT_TOLERANCE_KG_S)
    assert evaluation.boundary_balance.residual_kg_s == pytest.approx(
        0.0,
        abs=_COMPONENT_TOLERANCE_KG_S,
    )
    assert evaluation.boundary_balance.salt_residual_kg_s == pytest.approx(
        0.0,
        abs=_SALT_TOLERANCE_KG_S,
    )


def _assert_complete_product_observations(
    dynamic_case: DynamicCase,
    evaluation: DynamicEvaluation,
) -> None:
    assert set(evaluation.product_component_flows_kg_s) == set(_NET_PRODUCT_NAMES)
    assert set(evaluation.product_quality_proxies) == set(_NET_PRODUCT_NAMES)
    for product in _NET_PRODUCT_NAMES:
        component_flows = evaluation.product_component_flows_kg_s[product]
        assert set(component_flows) == set(ALL_COMPONENTS)
        total_flow = sum(component_flows.values())
        assert total_flow == pytest.approx(
            evaluation.stream_mass_flows_kg_s[
                _PRODUCT_STREAM_FLOW_NAMES[product]
            ],
            abs=1e-12,
        )
        assert total_flow > 0.0
        observation_stream = MaterialStream(
            name=f"{product}_observation_check",
            mass_flow_kg_s=total_flow,
            temperature_k=300.0,
            pressure_pa=101_325.0,
            mass_fractions={
                component: component_flows[component] / total_flow
                for component in ALL_COMPONENTS
            },
        )
        expected_quality = quality_proxies(
            observation_stream,
            dynamic_case.catalog,
        )
        assert dict(evaluation.product_quality_proxies[product]) == pytest.approx(
            expected_quality,
            abs=1e-12,
        )


def test_successful_conserved_m2_initializes_a_steady_54_state_model(
    dynamic_case: DynamicCase,
) -> None:
    result = dynamic_case.recycle_result
    steady = result.require_converged()
    dynamic_model = dynamic_case.dynamic_model

    assert steady.diagnostics["conservation_gate_passed"] == 1.0
    assert len(dynamic_model.initial_state.to_vector()) == 54
    initial_rhs = dynamic_model.rhs(
        0.0,
        dynamic_model.initial_state,
        dynamic_model.baseline_commands,
    )
    assert len(initial_rhs) == 54
    assert max(abs(value) for value in initial_rhs) <= _RHS_TOLERANCE

    evaluation = dynamic_model.evaluate(dynamic_model.initial_state)
    assert evaluation.top_pressure_pa == pytest.approx(
        dynamic_case.case_config.operating_conditions["tower_top_pressure_pa"],
        abs=1e-9,
    )
    assert set(evaluation.stream_mass_flows_kg_s) == {
        "fresh_crude",
        "wash_water",
        "brine",
        "flash_vapor",
        "flash_liquid_to_furnace",
        "reflux",
        "column_overhead",
        "oil_condensate",
        "offgas_to_drum",
        "aqueous",
        "gasoline",
        "kerosene",
        "light_diesel",
        "heavy_diesel",
        "residue_to_bottom",
        "residue_product",
        "top_gas_vent",
    }
    assert evaluation.stream_mass_flows_kg_s["fresh_crude"] == pytest.approx(
        dynamic_case.case_config.feed.mass_flow_kg_s,
    )
    assert evaluation.stream_mass_flows_kg_s["gasoline"] == pytest.approx(
        steady.products["gasoline"].mass_flow_kg_s,
        abs=1e-8,
    )
    _assert_instantaneous_conservation(evaluation)


def test_nominal_product_observations_are_complete_frozen_and_serializable(
    dynamic_case: DynamicCase,
) -> None:
    evaluation = dynamic_case.dynamic_model.evaluate(
        dynamic_case.dynamic_model.initial_state
    )
    _assert_complete_product_observations(dynamic_case, evaluation)
    assert json.dumps(evaluation.as_dict(), allow_nan=False)

    component_outer = cast(
        MutableMapping[str, Mapping[str, float]],
        evaluation.product_component_flows_kg_s,
    )
    component_inner = cast(
        MutableMapping[str, float],
        evaluation.product_component_flows_kg_s["gasoline"],
    )
    quality_outer = cast(
        MutableMapping[str, Mapping[str, float]],
        evaluation.product_quality_proxies,
    )
    quality_inner = cast(
        MutableMapping[str, float],
        evaluation.product_quality_proxies["gasoline"],
    )
    with pytest.raises(TypeError):
        component_outer["gasoline"] = {}
    with pytest.raises(TypeError):
        component_inner["naphtha"] = 0.0
    with pytest.raises(TypeError):
        quality_outer["gasoline"] = {}
    with pytest.raises(TypeError):
        quality_inner["density_kg_m3_proxy"] = 0.0


def test_perturbed_product_observations_remain_complete_and_serializable(
    dynamic_case: DynamicCase,
) -> None:
    dynamic_model = dynamic_case.dynamic_model
    nominal = dynamic_model.evaluate(dynamic_model.initial_state)
    perturbed_state = _state_with_actuator_change(
        dynamic_model,
        "fresh_feed_flow_kg_s",
        1.05,
    )
    perturbed = _evaluate_at_actual_actuators(dynamic_model, perturbed_state)

    _assert_complete_product_observations(dynamic_case, perturbed)
    assert json.dumps(perturbed.as_dict(), allow_nan=False)
    assert perturbed.stream_mass_flows_kg_s["kerosene"] > nominal.stream_mass_flows_kg_s[
        "kerosene"
    ]


def test_initializer_rejects_unsuccessful_and_nonconserved_recycle_results(
    dynamic_case: DynamicCase,
) -> None:
    failed = RecycleSolveResult(
        status="failed",
        flowsheet=None,
        iterations=0,
        final_residual=None,
        residual_history=(),
        reflux=None,
        failure_reason="deliberate test failure",
        failure_stage="test",
    )
    with pytest.raises(RuntimeError, match="deliberate test failure"):
        initialize_open_loop_dynamic_model(
            dynamic_case.model_config,
            dynamic_case.case_config,
            dynamic_case.catalog,
            failed,
        )

    valid_steady = dynamic_case.recycle_result.require_converged()
    bad_component_residuals = dict(
        valid_steady.balance.component_residuals_kg_s
    )
    bad_component_residuals["naphtha"] = 1.0
    invalid_balance = replace(
        valid_steady.balance,
        component_residuals_kg_s=bad_component_residuals,
    )
    invalid_steady = replace(valid_steady, balance=invalid_balance)
    invalid_recycle = replace(
        dynamic_case.recycle_result,
        flowsheet=invalid_steady,
    )
    with pytest.raises(ValueError, match="overall balance"):
        initialize_open_loop_dynamic_model(
            dynamic_case.model_config,
            dynamic_case.case_config,
            dynamic_case.catalog,
            invalid_recycle,
        )


def test_initializer_uses_configured_dynamic_material_tolerances(
    dynamic_case: DynamicCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_evaluate = OpenLoopDynamicModel.evaluate

    def evaluate_with_mass_residual(
        self: OpenLoopDynamicModel,
        state_or_vector: DynamicState | Sequence[float],
        commands: Mapping[str, float] | None = None,
    ) -> DynamicEvaluation:
        evaluation = original_evaluate(self, state_or_vector, commands)
        injected_residual_kg_s = 1e-7
        invalid_balance = replace(
            evaluation.boundary_balance,
            inlet_kg_s=(
                evaluation.boundary_balance.inlet_kg_s
                + injected_residual_kg_s
            ),
        )
        return replace(
            evaluation,
            boundary_balance=invalid_balance,
            boundary_mass_in_kg_s=(
                evaluation.boundary_mass_in_kg_s
                + injected_residual_kg_s
            ),
        )

    monkeypatch.setattr(
        OpenLoopDynamicModel,
        "evaluate",
        evaluate_with_mass_residual,
    )
    assert 1e-7 < _RHS_TOLERANCE
    assert dynamic_case.model_config.solver["mass_tolerance_kg_s"] == 1e-8
    with pytest.raises(ValueError, match="instantaneous material conservation"):
        initialize_open_loop_dynamic_model(
            dynamic_case.model_config,
            dynamic_case.case_config,
            dynamic_case.catalog,
            dynamic_case.recycle_result,
        )


def test_all_nominal_and_perturbed_points_close_component_and_salt_balances(
    dynamic_case: DynamicCase,
) -> None:
    dynamic_model = dynamic_case.dynamic_model
    evaluations = [dynamic_model.evaluate(dynamic_model.initial_state)]
    for actuator_name in (
        "fresh_feed_flow_kg_s",
        "furnace_fuel_duty_w",
        "condenser_cooling_duty_w",
        "pump_around_1_duty_w",
        "top_gas_vent_kg_s",
        "flash_liquid_outflow_kg_s",
        "gasoline_draw_kg_s",
        "residue_draw_kg_s",
    ):
        state = _state_with_actuator_change(dynamic_model, actuator_name, 1.05)
        evaluations.append(_evaluate_at_actual_actuators(dynamic_model, state))

    for evaluation in evaluations:
        _assert_instantaneous_conservation(evaluation)


def test_five_percent_fresh_feed_and_fuel_changes_have_expected_directions(
    dynamic_case: DynamicCase,
) -> None:
    dynamic_model = dynamic_case.dynamic_model

    fresh_state = _state_with_actuator_change(
        dynamic_model,
        "fresh_feed_flow_kg_s",
        1.05,
    )
    fresh = _evaluate_at_actual_actuators(dynamic_model, fresh_state)
    assert _liquid_bulk_derivative(fresh, "flash_drum") > 0.0
    assert _top_gas_bulk_derivative(fresh) > 0.0

    fuel_state = _state_with_actuator_change(
        dynamic_model,
        "furnace_fuel_duty_w",
        1.05,
    )
    fuel = _evaluate_at_actual_actuators(dynamic_model, fuel_state)
    assert _named_derivative(
        fuel,
        "thermal_states.furnace_outlet_temperature_k",
    ) > 0.0


def test_five_percent_cooling_and_pump_around_changes_have_expected_directions(
    dynamic_case: DynamicCase,
) -> None:
    dynamic_model = dynamic_case.dynamic_model

    cooling_state = _state_with_actuator_change(
        dynamic_model,
        "condenser_cooling_duty_w",
        1.05,
    )
    cooling = _evaluate_at_actual_actuators(dynamic_model, cooling_state)
    assert _top_gas_bulk_derivative(cooling) < 0.0
    assert _liquid_bulk_derivative(cooling, "reflux_drum") > 0.0

    pump_around_state = _state_with_actuator_change(
        dynamic_model,
        "pump_around_1_duty_w",
        1.05,
    )
    pump_around = _evaluate_at_actual_actuators(
        dynamic_model,
        pump_around_state,
    )
    assert _named_derivative(
        pump_around,
        "thermal_states.tower_top_temperature_k",
    ) < 0.0
    assert _named_derivative(
        pump_around,
        "thermal_states.kerosene_temperature_k",
    ) < 0.0


def test_five_percent_inventory_withdrawal_changes_reduce_their_inventories(
    dynamic_case: DynamicCase,
) -> None:
    dynamic_model = dynamic_case.dynamic_model

    top_gas_vent = _evaluate_at_actual_actuators(
        dynamic_model,
        _state_with_actuator_change(
            dynamic_model,
            "top_gas_vent_kg_s",
            1.05,
        ),
    )
    assert _top_gas_bulk_derivative(top_gas_vent) < 0.0

    flash_outflow = _evaluate_at_actual_actuators(
        dynamic_model,
        _state_with_actuator_change(
            dynamic_model,
            "flash_liquid_outflow_kg_s",
            1.05,
        ),
    )
    assert _liquid_bulk_derivative(flash_outflow, "flash_drum") < 0.0

    gasoline_draw = _evaluate_at_actual_actuators(
        dynamic_model,
        _state_with_actuator_change(
            dynamic_model,
            "gasoline_draw_kg_s",
            1.05,
        ),
    )
    assert _liquid_bulk_derivative(gasoline_draw, "reflux_drum") < 0.0

    residue_draw = _evaluate_at_actual_actuators(
        dynamic_model,
        _state_with_actuator_change(
            dynamic_model,
            "residue_draw_kg_s",
            1.05,
        ),
    )
    assert _liquid_bulk_derivative(residue_draw, "tower_bottom") < 0.0


def test_unknown_negative_commands_and_invalid_states_fail_explicitly(
    dynamic_case: DynamicCase,
) -> None:
    dynamic_model = dynamic_case.dynamic_model
    baseline = dict(dynamic_model.baseline_commands)

    unknown = dict(baseline)
    unknown["unknown_command"] = 1.0
    with pytest.raises(ValueError, match="command keys differ"):
        dynamic_model.evaluate(dynamic_model.initial_state, unknown)

    negative = dict(baseline)
    negative["gasoline_draw_kg_s"] = -1.0
    with pytest.raises(ValueError, match="must be non-negative"):
        dynamic_model.evaluate(dynamic_model.initial_state, negative)

    above_twice_nominal = dict(baseline)
    actuator_name = "fresh_feed_flow_kg_s"
    above_twice_nominal[actuator_name] = 3.0 * baseline[actuator_name]
    high_command = dynamic_model.evaluate(
        dynamic_model.initial_state,
        above_twice_nominal,
    )
    actuator_derivative = _named_derivative(
        high_command,
        f"actuator_states.{actuator_name}",
    )
    raw_time_constant = dynamic_case.model_config.dynamic[
        "actuator_time_constant_s"
    ]
    assert isinstance(raw_time_constant, (int, float))
    assert not isinstance(raw_time_constant, bool)
    expected_derivative = (
        above_twice_nominal[actuator_name] - baseline[actuator_name]
    ) / float(raw_time_constant)
    assert actuator_derivative == pytest.approx(expected_derivative, abs=1e-12)

    negative_inventory = list(dynamic_model.initial_state.to_vector())
    negative_inventory[0] = -1.0
    with pytest.raises(ValueError, match="must be non-negative"):
        dynamic_model.rhs(0.0, negative_inventory)

    zero_flash_outflow = list(dynamic_model.initial_state.to_vector())
    flash_outflow_index = DynamicState.vector_names().index(
        "actuator_states.flash_liquid_outflow_kg_s"
    )
    zero_flash_outflow[flash_outflow_index] = 0.0
    with pytest.raises(ValueError, match="flash liquid outflow must be positive"):
        dynamic_model.evaluate(DynamicState.from_vector(zero_flash_outflow))

    insufficient_fuel = list(dynamic_model.initial_state.to_vector())
    fuel_index = DynamicState.vector_names().index(
        "actuator_states.furnace_fuel_duty_w"
    )
    insufficient_fuel[fuel_index] = 0.0
    with pytest.raises(ValueError, match="below the heat-loss threshold"):
        dynamic_model.evaluate(DynamicState.from_vector(insufficient_fuel))


def test_same_inputs_produce_identical_initialization_and_rhs(
    dynamic_case: DynamicCase,
) -> None:
    first = dynamic_case.dynamic_model
    second = initialize_open_loop_dynamic_model(
        dynamic_case.model_config,
        dynamic_case.case_config,
        dynamic_case.catalog,
        dynamic_case.recycle_result,
    )

    assert second.initial_state.to_vector() == first.initial_state.to_vector()
    assert dict(second.baseline_commands) == dict(first.baseline_commands)
    assert second.rhs(0.0, second.initial_state) == first.rhs(
        0.0,
        first.initial_state,
    )
    assert second.evaluate(second.initial_state).as_dict() == first.evaluate(
        first.initial_state
    ).as_dict()
