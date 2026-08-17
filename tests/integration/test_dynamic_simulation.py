from __future__ import annotations

import json
import math
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
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
from petroleum_rto.cdu.core.types import BalanceReport
from petroleum_rto.cdu.dynamics.equations import OpenLoopDynamicModel
from petroleum_rto.cdu.dynamics.initialization import (
    initialize_open_loop_dynamic_model,
)
from petroleum_rto.cdu.dynamics.schedule import CommandEvent, CommandSchedule
from petroleum_rto.cdu.dynamics.simulation import DynamicSimulationResult, simulate_dynamic
from petroleum_rto.cdu.dynamics.state import (
    ACTUATOR_STATE_NAMES,
    SENSOR_STATE_NAMES,
    THERMAL_STATE_NAMES,
    DynamicState,
    InventoryState,
)
from petroleum_rto.cdu.flowsheet.recycle import solve_recycle
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS, ComponentCatalog

_SOURCE_FINGERPRINT = "a" * 64
_VERSIONS = {
    "software_version": "test-software-v0.1.0",
    "model_version": "test-model-v0.1.0",
    "parameter_set_version": "test-parameters-v0.1.0",
    "config_version": "test-config-v0.1.0",
    "case_version": "test-case-v0.1.0",
}
_METADATA = {
    "scenario_name": "toy_dynamic_scenario",
    "scenario_version": "toy-dynamic-scenario-v0.1.0",
    "purpose": "M3 integration-engine verification",
}
_REAL_BASELINE_METADATA = {
    "scenario_name": "direct_real_nominal_hold",
    "scenario_version": "direct-real-nominal-hold-v0.1.0",
    "purpose": "M3 four-hour steady-hold verification",
}
_TEMPERATURE_STATE = "thermal_states.furnace_outlet_temperature_k"
_FLASH_NAPHTHA_STATE = (
    "liquid_inventories.flash_drum.component_masses_kg.naphtha"
)
_FLASH_SALT_STATE = "liquid_inventories.flash_drum.salt_mass_kg"


def _toy_state() -> DynamicState:
    inventories = {
        name: InventoryState(
            name,
            {component: 10.0 for component in ALL_COMPONENTS},
            salt_mass_kg=1.0,
        )
        for name in ("flash_drum", "reflux_drum", "tower_bottom")
    }
    thermal = {name: 500.0 for name in THERMAL_STATE_NAMES}
    thermal["furnace_outlet_temperature_k"] = 600.0
    thermal["preheater_duty_w"] = 1_000_000.0
    return DynamicState(
        liquid_inventories=inventories,
        top_gas_component_masses_kg={component: 1.0 for component in ALL_COMPONENTS},
        thermal_states=thermal,
        actuator_states={name: 1.0 for name in ACTUATOR_STATE_NAMES},
        sensor_states={name: 1.0 for name in SENSOR_STATE_NAMES},
    )


@dataclass(frozen=True)
class _ToyEvaluation:
    derivative_vector: tuple[float, ...]
    boundary_balance: BalanceReport
    boundary_mass_in_kg_s: float
    boundary_mass_out_kg_s: float
    boundary_salt_in_kg_s: float
    boundary_salt_out_kg_s: float
    boundary_component_in_kg_s: Mapping[str, float]
    boundary_component_out_kg_s: Mapping[str, float]

    @property
    def maximum_absolute_component_residual_kg_s(self) -> float:
        return max(
            (abs(value) for value in self.boundary_balance.component_residuals_kg_s.values()),
            default=0.0,
        )

    @property
    def maximum_absolute_material_residual_kg_s(self) -> float:
        return max(
            abs(self.boundary_balance.residual_kg_s),
            self.maximum_absolute_component_residual_kg_s,
            abs(self.boundary_balance.salt_residual_kg_s),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "derivative_vector": list(self.derivative_vector),
            "boundary_balance": self.boundary_balance.as_dict(),
            "boundary_mass_in_kg_s": self.boundary_mass_in_kg_s,
            "boundary_mass_out_kg_s": self.boundary_mass_out_kg_s,
            "boundary_salt_in_kg_s": self.boundary_salt_in_kg_s,
            "boundary_salt_out_kg_s": self.boundary_salt_out_kg_s,
        }


class _ToyDynamicModel:
    initial_state: DynamicState
    baseline_commands: Mapping[str, float]

    def __init__(
        self,
        *,
        inventory_rate_kg_s: float = 0.0,
        salt_rate_kg_s: float = 0.0,
        reported_inventory_rate_kg_s: float | None = None,
        reported_salt_rate_kg_s: float | None = None,
    ) -> None:
        self.initial_state = _toy_state()
        self.baseline_commands = {
            "temperature_target_k": 600.0,
            "failure_switch": 0.0,
            "conservation_switch": 0.0,
        }
        self._inventory_rate_kg_s = inventory_rate_kg_s
        self._salt_rate_kg_s = salt_rate_kg_s
        self._reported_inventory_rate_kg_s = (
            inventory_rate_kg_s
            if reported_inventory_rate_kg_s is None
            else reported_inventory_rate_kg_s
        )
        self._reported_salt_rate_kg_s = (
            salt_rate_kg_s
            if reported_salt_rate_kg_s is None
            else reported_salt_rate_kg_s
        )
        names = DynamicState.vector_names()
        self._temperature_index = names.index(_TEMPERATURE_STATE)
        self._inventory_index = names.index(_FLASH_NAPHTHA_STATE)
        self._salt_index = names.index(_FLASH_SALT_STATE)

    def evaluate(
        self,
        state: DynamicState,
        commands: Mapping[str, float],
    ) -> _ToyEvaluation:
        if commands["failure_switch"] > 0.0:
            raise RuntimeError("scheduled toy-model failure")
        derivative = [0.0] * len(DynamicState.vector_names())
        derivative[self._temperature_index] = (
            commands["temperature_target_k"]
            - state.thermal_states["furnace_outlet_temperature_k"]
        ) / 20.0
        derivative[self._inventory_index] = self._inventory_rate_kg_s
        derivative[self._salt_index] = self._salt_rate_kg_s
        component_in = {component: 0.0 for component in ALL_COMPONENTS}
        component_out = {component: 0.0 for component in ALL_COMPONENTS}
        if self._reported_inventory_rate_kg_s >= 0.0:
            component_in["naphtha"] = self._reported_inventory_rate_kg_s
        else:
            component_out["naphtha"] = -self._reported_inventory_rate_kg_s
        salt_in = max(0.0, self._reported_salt_rate_kg_s)
        salt_out = max(0.0, -self._reported_salt_rate_kg_s)
        conservation_switch = commands["conservation_switch"]
        component_residuals = {component: 0.0 for component in ALL_COMPONENTS}
        if conservation_switch == 1.0:
            component_residuals["naphtha"] = 1e-4
        salt_residual = 1e-4 if conservation_switch == 2.0 else 0.0
        total_residual = 1e-4 if conservation_switch == 3.0 else 0.0
        balance = BalanceReport(
            inlet_kg_s=sum(component_in.values()),
            outlet_kg_s=sum(component_out.values()),
            accumulation_kg_s=self._reported_inventory_rate_kg_s - total_residual,
            component_residuals_kg_s=component_residuals,
            salt_residual_kg_s=salt_residual,
        )
        return _ToyEvaluation(
            derivative_vector=tuple(derivative),
            boundary_balance=balance,
            boundary_mass_in_kg_s=sum(component_in.values()),
            boundary_mass_out_kg_s=sum(component_out.values()),
            boundary_salt_in_kg_s=salt_in,
            boundary_salt_out_kg_s=salt_out,
            boundary_component_in_kg_s=component_in,
            boundary_component_out_kg_s=component_out,
        )

    def rhs(
        self,
        time_s: float,
        state_vector: Sequence[float],
        commands: Mapping[str, float],
    ) -> Sequence[float]:
        del time_s
        return self.evaluate(DynamicState.from_vector(state_vector), commands).derivative_vector


class _PureIntegratorModel(_ToyDynamicModel):
    def __init__(self) -> None:
        super().__init__()
        vector = list(self.initial_state.to_vector())
        self._integrator_index = DynamicState.vector_names().index(
            "actuator_states.fresh_feed_flow_kg_s"
        )
        vector[self._integrator_index] = 0.0
        self.initial_state = DynamicState.from_vector(vector)
        self.baseline_commands = {"u": 0.0}

    def evaluate(
        self,
        state: DynamicState,
        commands: Mapping[str, float],
    ) -> _ToyEvaluation:
        del state
        derivative = [0.0] * len(DynamicState.vector_names())
        derivative[self._integrator_index] = commands["u"]
        zeros = {component: 0.0 for component in ALL_COMPONENTS}
        balance = BalanceReport(
            inlet_kg_s=0.0,
            outlet_kg_s=0.0,
            component_residuals_kg_s=zeros,
        )
        return _ToyEvaluation(
            derivative_vector=tuple(derivative),
            boundary_balance=balance,
            boundary_mass_in_kg_s=0.0,
            boundary_mass_out_kg_s=0.0,
            boundary_salt_in_kg_s=0.0,
            boundary_salt_out_kg_s=0.0,
            boundary_component_in_kg_s=zeros,
            boundary_component_out_kg_s=zeros,
        )


@pytest.fixture(scope="module")
def real_dynamic_model(repo_root: Path) -> OpenLoopDynamicModel:
    model: ModelConfig = load_model_config(
        repo_root / "configs/models/cdu_mini_v0.1.0.json"
    )
    case: CaseConfig = load_case_config(repo_root / "configs/cases/case_20260604.json")
    catalog: ComponentCatalog = load_component_catalog(
        repo_root / model.component_catalog_path
    )
    recycle = solve_recycle(model, case, catalog)
    return initialize_open_loop_dynamic_model(model, case, catalog, recycle)


def _run_toy(
    model: _ToyDynamicModel,
    schedule: CommandSchedule,
    duration_s: float,
    dt_s: float,
) -> DynamicSimulationResult:
    return simulate_dynamic(
        model,
        schedule,
        duration_s,
        dt_s,
        fingerprint=_SOURCE_FINGERPRINT,
        versions=_VERSIONS,
        metadata=_METADATA,
    )


def test_real_nominal_model_holds_for_four_hours_and_conserves(
    real_dynamic_model: OpenLoopDynamicModel,
) -> None:
    schedule = CommandSchedule(real_dynamic_model.baseline_commands)

    result = simulate_dynamic(
        real_dynamic_model,
        schedule,
        4.0 * 60.0 * 60.0,
        1.0,
        fingerprint=real_dynamic_model.input_fingerprint,
        versions=real_dynamic_model.versions,
        metadata=_REAL_BASELINE_METADATA,
    )

    assert result.status == "success"
    assert len(result.samples) == 14_401
    assert result.samples[0].state.to_vector() == pytest.approx(
        result.samples[-1].state.to_vector(),
        rel=1e-7,
        abs=1e-5,
    )
    assert result.balance.passed(mass_atol_kg=1e-5, salt_atol_kg=1e-8)
    assert result.diagnostics["max_instantaneous_component_residual_kg_s"] <= 1e-6
    assert result.versions["simulation_stage"] == "M3"
    assert result.versions["scenario_version"] == (
        _REAL_BASELINE_METADATA["scenario_version"]
    )
    assert result.conservation_tolerances.instantaneous_mass_atol_kg_s == (
        real_dynamic_model.model.solver["mass_tolerance_kg_s"]
    )


def test_step_direction_and_time_step_sensitivity() -> None:
    model = _ToyDynamicModel()
    schedule = CommandSchedule(
        model.baseline_commands,
        (CommandEvent(10.0, "temperature_target_k", 620.0),),
    )

    fine = _run_toy(model, schedule, 100.0, 0.5).require_success()
    coarse = _run_toy(model, schedule, 100.0, 1.0).require_success()
    initial = fine.samples[0].state.thermal_states["furnace_outlet_temperature_k"]
    fine_final = fine.samples[-1].state.thermal_states["furnace_outlet_temperature_k"]
    coarse_final = coarse.samples[-1].state.thermal_states[
        "furnace_outlet_temperature_k"
    ]

    assert fine_final > initial
    assert abs(fine_final - coarse_final) / 20.0 < 1e-4


def test_component_and_salt_cumulative_balance_is_integrated_with_state() -> None:
    model = _ToyDynamicModel(inventory_rate_kg_s=0.2, salt_rate_kg_s=0.01)
    result = _run_toy(
        model,
        CommandSchedule(model.baseline_commands),
        10.0,
        1.0,
    ).require_success()

    assert result.balance.cumulative_component_in_kg["naphtha"] == pytest.approx(2.0)
    assert result.balance.cumulative_salt_in_kg == pytest.approx(0.1)
    assert result.balance.maximum_absolute_component_residual_kg < 1e-10
    assert abs(result.balance.salt_residual_kg) < 1e-12
    assert result.balance.passed(mass_atol_kg=1e-10, salt_atol_kg=1e-12)


def test_repeatability_immutability_and_json_serialization() -> None:
    model = _ToyDynamicModel()
    schedule = CommandSchedule(model.baseline_commands)

    first = _run_toy(model, schedule, 5.0, 1.0)
    second = _run_toy(model, schedule, 5.0, 1.0)

    assert first.as_dict() == second.as_dict()
    assert first.input_fingerprint == second.input_fingerprint
    assert json.dumps(first.as_dict(), allow_nan=False)
    assert first.metadata["synthetic"] == "true"
    assert first.metadata["data_origin"] == "M3_open_loop_simulation"
    assert first.metadata["scenario_name"] == _METADATA["scenario_name"]
    assert first.metadata["scenario_version"] == _METADATA["scenario_version"]
    assert first.metadata["purpose"] == _METADATA["purpose"]
    frozen_commands = cast(MutableMapping[str, float], first.samples[0].commands)
    frozen_components = cast(
        MutableMapping[str, float],
        first.balance.component_residuals_kg,
    )
    frozen_metadata = cast(MutableMapping[str, str], first.metadata)
    with pytest.raises(TypeError):
        frozen_commands["temperature_target_k"] = 700.0
    with pytest.raises(TypeError):
        frozen_components["naphtha"] = 1.0
    with pytest.raises(TypeError):
        frozen_metadata["synthetic"] = "false"


def test_non_aligned_right_continuous_step_has_no_pre_event_effect() -> None:
    model = _PureIntegratorModel()
    schedule = CommandSchedule(
        model.baseline_commands,
        (
            CommandEvent(10.0, "u", 3.0),
            CommandEvent(10.0, "u", 6.0),
        ),
    )

    result = _run_toy(model, schedule, 18.0, 6.0).require_success()
    samples = {sample.time_s: sample for sample in result.samples}
    at_event = samples[10.0]
    after_event = samples[12.0]

    assert at_event.state.actuator_states["fresh_feed_flow_kg_s"] == 0.0
    assert at_event.commands["u"] == 6.0
    assert after_event.state.actuator_states["fresh_feed_flow_kg_s"] == pytest.approx(
        12.0
    )
    assert result.diagnostics["requested_nominal_steps"] == 3.0
    assert result.diagnostics["completed_nominal_steps"] == 3.0
    assert result.diagnostics["requested_integration_substeps"] == 4.0
    assert result.diagnostics["completed_integration_substeps"] == 4.0
    assert "requested_steps" not in result.diagnostics
    assert "completed_steps" not in result.diagnostics


@pytest.mark.parametrize("switch", [1.0, 2.0, 3.0])
def test_instantaneous_conservation_violation_fails_at_event_without_commit(
    switch: float,
) -> None:
    model = _ToyDynamicModel()
    schedule = CommandSchedule(
        model.baseline_commands,
        (CommandEvent(1.5, "conservation_switch", switch),),
    )

    result = _run_toy(model, schedule, 4.0, 1.0)

    assert result.status == "failed"
    assert result.failure_stage == "conservation"
    assert result.failure_time_s == 1.5
    assert tuple(sample.time_s for sample in result.samples) == (0.0, 1.0)
    assert "instantaneous boundary balance failed" in (result.failure_reason or "")


@pytest.mark.parametrize(
    ("model"),
    [
        _ToyDynamicModel(
            inventory_rate_kg_s=0.01,
            reported_inventory_rate_kg_s=0.0,
        ),
        _ToyDynamicModel(
            salt_rate_kg_s=0.01,
            reported_salt_rate_kg_s=0.0,
        ),
    ],
)
def test_cumulative_conservation_violation_cannot_return_success(
    model: _ToyDynamicModel,
) -> None:
    result = _run_toy(
        model,
        CommandSchedule(model.baseline_commands),
        2.0,
        1.0,
    )

    assert result.status == "failed"
    assert result.failure_stage == "conservation"
    assert result.failure_time_s == 1.0
    assert tuple(sample.time_s for sample in result.samples) == (0.0,)
    assert "cumulative boundary balance failed" in (result.failure_reason or "")
    assert "threshold=1e-06" in (result.failure_reason or "")


def test_runtime_failure_retains_only_last_complete_endpoint() -> None:
    model = _ToyDynamicModel()
    schedule = CommandSchedule(
        model.baseline_commands,
        (CommandEvent(1.5, "failure_switch", 1.0),),
    )

    result = _run_toy(model, schedule, 4.0, 1.0)

    assert result.status == "failed"
    assert tuple(sample.time_s for sample in result.samples) == (0.0, 1.0)
    assert result.completed_time_s == 1.0
    assert result.failure_time_s == 1.5
    assert result.last_valid_state is result.samples[-1].state
    assert "scheduled toy-model failure" in (result.failure_reason or "")
    with pytest.raises(RuntimeError, match="scheduled toy-model failure"):
        result.require_success()


def test_negative_intermediate_inventory_fails_without_clipping() -> None:
    model = _ToyDynamicModel(inventory_rate_kg_s=-30.0)
    schedule = CommandSchedule(model.baseline_commands)

    result = _run_toy(model, schedule, 2.0, 1.0)

    assert result.status == "failed"
    assert tuple(sample.time_s for sample in result.samples) == (0.0,)
    assert result.failure_time_s == pytest.approx(0.5)
    assert result.last_valid_state == model.initial_state
    assert result.last_valid_state is not None
    assert all(value >= 0.0 for value in result.last_valid_state.to_vector())
    assert "non-negative" in (result.failure_reason or "")


def test_runner_rejects_non_nominal_grid_or_mismatched_schedule() -> None:
    model = _ToyDynamicModel()

    decimal_grid = _run_toy(
        model,
        CommandSchedule(model.baseline_commands),
        0.3,
        0.1,
    ).require_success()
    assert decimal_grid.samples[-1].time_s == 0.3
    with pytest.raises(ValueError, match="integer multiple"):
        _run_toy(model, CommandSchedule(model.baseline_commands), 1.0, 0.3)
    with pytest.raises(ValueError, match="exactly match"):
        _run_toy(model, CommandSchedule({"other": 1.0}), 1.0, 1.0)
    with pytest.raises(ValueError, match="SHA-256"):
        simulate_dynamic(
            model,
            CommandSchedule(model.baseline_commands),
            1.0,
            1.0,
            fingerprint="bad",
            versions=_VERSIONS,
            metadata=_METADATA,
        )


def test_simulator_rejects_incomplete_or_disguised_traceability_before_run() -> None:
    model = _ToyDynamicModel()
    schedule = CommandSchedule(model.baseline_commands)

    with pytest.raises(ValueError, match="metadata must identify"):
        simulate_dynamic(
            model,
            schedule,
            1.0,
            1.0,
            fingerprint=_SOURCE_FINGERPRINT,
            versions=_VERSIONS,
        )
    with pytest.raises(ValueError, match="versions is missing required fields"):
        simulate_dynamic(
            model,
            schedule,
            1.0,
            1.0,
            fingerprint=_SOURCE_FINGERPRINT,
            versions={},
            metadata=_METADATA,
        )
    with pytest.raises(ValueError, match="cannot claim non-synthetic"):
        simulate_dynamic(
            model,
            schedule,
            1.0,
            1.0,
            fingerprint=_SOURCE_FINGERPRINT,
            versions=_VERSIONS,
            metadata={**_METADATA, "synthetic": "false"},
        )
    with pytest.raises(ValueError, match="data_origin"):
        simulate_dynamic(
            model,
            schedule,
            1.0,
            1.0,
            fingerprint=_SOURCE_FINGERPRINT,
            versions=_VERSIONS,
            metadata={**_METADATA, "data_origin": "field_measurement"},
        )
    with pytest.raises(ValueError, match="scenario_version must match"):
        simulate_dynamic(
            model,
            schedule,
            1.0,
            1.0,
            fingerprint=_SOURCE_FINGERPRINT,
            versions={**_VERSIONS, "scenario_version": "other-scenario-v0.1.0"},
            metadata=_METADATA,
        )


def test_toy_response_matches_first_order_solution() -> None:
    model = _ToyDynamicModel()
    schedule = CommandSchedule(
        model.baseline_commands,
        (CommandEvent(0.0, "temperature_target_k", 620.0),),
    )

    result = _run_toy(model, schedule, 20.0, 0.25).require_success()
    actual = result.samples[-1].state.thermal_states["furnace_outlet_temperature_k"]
    expected = 620.0 - 20.0 * math.exp(-1.0)

    assert actual == pytest.approx(expected, abs=1e-8)
