"""Steady-consistent initialization of the M3 open-loop dynamic model."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from ..core.config import CaseConfig, ModelConfig
from ..core.types import BalanceReport, MaterialStream
from ..flowsheet.recycle import RecycleSettings, RecycleSolveResult
from ..properties.components import ALL_COMPONENTS, ComponentCatalog
from ..properties.thermo import GAS_CONSTANT_J_MOL_K
from .equations import OpenLoopDynamicModel
from .state import ACTUATOR_STATE_NAMES, DynamicState, InventoryState

DEFAULT_INITIALIZATION_RESIDUAL_TOLERANCE = 1e-6
_ENERGY_TOLERANCE_W = 1e-5


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{context} must be finite")
    return converted


def _positive_dynamic_value(model: ModelConfig, name: str) -> float:
    value = _finite_number(model.dynamic[name], context=f"dynamic.{name}")
    if value <= 0.0:
        raise ValueError(f"dynamic.{name} must be positive")
    return value


def _solver_tolerance(model: ModelConfig, name: str) -> float:
    value = _finite_number(model.solver[name], context=f"solver.{name}")
    if value < 0.0:
        raise ValueError(f"solver.{name} must be non-negative")
    return value


def _balance_passed(model: ModelConfig, balance: BalanceReport) -> bool:
    return balance.passed(
        mass_atol_kg_s=_solver_tolerance(model, "mass_tolerance_kg_s"),
        component_atol_kg_s=_solver_tolerance(
            model,
            "component_tolerance_kg_s",
        ),
        salt_atol_kg_s=_solver_tolerance(model, "salt_tolerance_kg_s"),
        energy_atol_w=_ENERGY_TOLERANCE_W,
    )


def _inventory_from_source(
    name: str,
    source: MaterialStream,
    residence_time_s: float,
) -> InventoryState:
    if source.mass_flow_kg_s <= 0.0:
        raise ValueError(f"cannot initialize {name!r} from a zero-flow M2 stream")
    return InventoryState(
        name=name,
        component_masses_kg={
            component: source.component_flow_kg_s(component) * residence_time_s
            for component in ALL_COMPONENTS
        },
        salt_mass_kg=source.salt_mass_flow_kg_s * residence_time_s,
    )


def _top_gas_masses(
    source: MaterialStream,
    *,
    pressure_pa: float,
    temperature_k: float,
    volume_m3: float,
    catalog: ComponentCatalog,
) -> dict[str, float]:
    if source.mass_flow_kg_s <= 0.0:
        raise ValueError("cannot initialize top gas from a zero-flow M2 stream")
    mole_per_bulk_kg = sum(
        source.mass_fractions.get(component, 0.0)
        / catalog.components[component].molecular_weight_kg_mol
        for component in ALL_COMPONENTS
    )
    if mole_per_bulk_kg <= 0.0:
        raise ValueError("M2 top gas composition has no finite molar density")
    target_moles = pressure_pa * volume_m3 / (
        GAS_CONSTANT_J_MOL_K * temperature_k
    )
    total_mass_kg = target_moles / mole_per_bulk_kg
    return {
        component: total_mass_kg * source.mass_fractions.get(component, 0.0)
        for component in ALL_COMPONENTS
    }


def _baseline_pump_around_duties(
    model: ModelConfig,
    reported_total_w: float,
) -> tuple[float, float, float]:
    configured = RecycleSettings.from_model(model).pump_around_duties_w
    configured_total = sum(configured)
    if reported_total_w < 0.0:
        raise ValueError("M2 pump-around removed duty cannot be negative")
    if configured_total == 0.0:
        if reported_total_w != 0.0:
            raise ValueError("cannot infer individual M2 pump-around duties")
        return (0.0, 0.0, 0.0)
    factor = reported_total_w / configured_total
    return cast(
        tuple[float, float, float],
        tuple(value * factor for value in configured),
    )


def initialize_open_loop_dynamic_model(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    recycle_result: RecycleSolveResult,
    *,
    residual_tolerance: float = DEFAULT_INITIALIZATION_RESIDUAL_TOLERANCE,
) -> OpenLoopDynamicModel:
    """Build and verify one M3 model from a successful, conserved M2 solution."""

    tolerance = _finite_number(
        residual_tolerance,
        context="residual_tolerance",
    )
    if tolerance < 0.0:
        raise ValueError("residual_tolerance must be non-negative")
    steady = recycle_result.require_converged()
    if not _balance_passed(model, steady.balance):
        raise ValueError("M2 overall balance did not pass the configured conservation gate")
    failed_units = sorted(
        name
        for name, result in steady.unit_results.items()
        if result.balance is None or not _balance_passed(model, result.balance)
    )
    if failed_units:
        raise ValueError(
            "M2 unit balances did not pass the configured conservation gate: "
            + ", ".join(failed_units)
        )

    required_units = {"pre_furnace_preheater", "furnace", "column", "condenser"}
    missing_units = sorted(required_units - set(steady.unit_results))
    if missing_units:
        raise ValueError(
            "M2 result is missing units required by M3: " + ", ".join(missing_units)
        )
    required_streams = {
        "flash_liquid",
        "oil_condensate",
        "offgas",
        "gasoline",
        "reflux",
        "residue",
        "overhead",
        "kerosene",
        "light_diesel",
        "heavy_diesel",
    }
    missing_streams = sorted(required_streams - set(steady.streams))
    if missing_streams:
        raise ValueError(
            "M2 result is missing streams required by M3: "
            + ", ".join(missing_streams)
        )

    flash_inventory = _inventory_from_source(
        "flash_drum",
        steady.streams["flash_liquid"],
        _positive_dynamic_value(model, "flash_residence_time_s"),
    )
    reflux_inventory = _inventory_from_source(
        "reflux_drum",
        steady.streams["oil_condensate"],
        _positive_dynamic_value(model, "reflux_drum_residence_time_s"),
    )
    bottom_inventory = _inventory_from_source(
        "tower_bottom",
        steady.streams["residue"],
        _positive_dynamic_value(model, "tower_bottom_residence_time_s"),
    )

    tower_top_temperature = steady.streams["overhead"].temperature_k
    tower_top_pressure = case.operating_conditions["tower_top_pressure_pa"]
    top_gas_masses = _top_gas_masses(
        steady.streams["offgas"],
        pressure_pa=tower_top_pressure,
        temperature_k=tower_top_temperature,
        volume_m3=_positive_dynamic_value(model, "top_gas_volume_m3"),
        catalog=catalog,
    )

    reported_pump_duty = _finite_number(
        steady.diagnostics.get(
            "pump_around_removed_duty_w",
            sum(RecycleSettings.from_model(model).pump_around_duties_w),
        ),
        context="M2 pump-around removed duty",
    )
    pump_duties = _baseline_pump_around_duties(model, reported_pump_duty)
    fuel_duty = _finite_number(
        steady.diagnostics["furnace_fuel_duty_w"],
        context="M2 furnace fuel duty",
    )
    cooling_duty = -steady.unit_results["condenser"].duty_w
    if fuel_duty < 0.0 or cooling_duty <= 0.0:
        raise ValueError("M2 furnace fuel and condenser cooling duties must be positive")

    baseline_commands: Mapping[str, float] = {
        "fresh_feed_flow_kg_s": case.feed.mass_flow_kg_s,
        "flash_liquid_outflow_kg_s": steady.streams["flash_liquid"].mass_flow_kg_s,
        "gasoline_draw_kg_s": steady.streams["gasoline"].mass_flow_kg_s,
        "reflux_flow_kg_s": steady.streams["reflux"].mass_flow_kg_s,
        "residue_draw_kg_s": steady.streams["residue"].mass_flow_kg_s,
        "top_gas_vent_kg_s": steady.streams["offgas"].mass_flow_kg_s,
        "furnace_fuel_duty_w": fuel_duty,
        "condenser_cooling_duty_w": cooling_duty,
        "pump_around_1_duty_w": pump_duties[0],
        "pump_around_2_duty_w": pump_duties[1],
        "pump_around_3_duty_w": pump_duties[2],
    }
    if set(baseline_commands) != set(ACTUATOR_STATE_NAMES):
        raise AssertionError("internal baseline command construction is incomplete")

    thermal_states = {
        "furnace_outlet_temperature_k": steady.streams[
            "furnace_outlet"
        ].temperature_k,
        "tower_top_temperature_k": tower_top_temperature,
        "kerosene_temperature_k": steady.streams["kerosene"].temperature_k,
        "light_diesel_temperature_k": steady.streams["light_diesel"].temperature_k,
        "heavy_diesel_temperature_k": steady.streams["heavy_diesel"].temperature_k,
        "preheater_duty_w": steady.unit_results["pre_furnace_preheater"].duty_w,
    }
    sensors = {
        "furnace_outlet_temperature_k": thermal_states[
            "furnace_outlet_temperature_k"
        ],
        "tower_top_pressure_pa": tower_top_pressure,
        "tower_top_temperature_k": tower_top_temperature,
        "flash_drum_inventory_kg": flash_inventory.total_mass_kg,
        "reflux_drum_inventory_kg": reflux_inventory.total_mass_kg,
        "tower_bottom_inventory_kg": bottom_inventory.total_mass_kg,
    }
    initial_state = DynamicState(
        liquid_inventories={
            "flash_drum": flash_inventory,
            "reflux_drum": reflux_inventory,
            "tower_bottom": bottom_inventory,
        },
        top_gas_component_masses_kg=top_gas_masses,
        thermal_states=thermal_states,
        actuator_states=baseline_commands,
        sensor_states=sensors,
    )
    dynamic_model = OpenLoopDynamicModel(
        model=model,
        case=case,
        catalog=catalog,
        recycle_result=recycle_result,
        initial_state=initial_state,
        baseline_commands=baseline_commands,
    )
    residual = dynamic_model.rhs(
        0.0,
        initial_state,
        dynamic_model.baseline_commands,
    )
    maximum_residual = max((abs(value) for value in residual), default=0.0)
    if maximum_residual > tolerance:
        raise ValueError(
            "M3 initialization is not steady-consistent: "
            f"maximum absolute RHS residual {maximum_residual:.16g} "
            f"exceeds tolerance {tolerance:.16g}"
        )
    evaluation = dynamic_model.evaluate(
        initial_state,
        dynamic_model.baseline_commands,
    )
    dynamic_balance = evaluation.boundary_balance
    if not dynamic_balance.passed(
        mass_atol_kg_s=_solver_tolerance(model, "mass_tolerance_kg_s"),
        component_atol_kg_s=_solver_tolerance(
            model,
            "component_tolerance_kg_s",
        ),
        salt_atol_kg_s=_solver_tolerance(model, "salt_tolerance_kg_s"),
    ):
        raise ValueError(
            "M3 initialization fails instantaneous material conservation: "
            f"mass residual {dynamic_balance.residual_kg_s:.16g} kg/s, "
            "maximum component residual "
            f"{evaluation.maximum_absolute_component_residual_kg_s:.16g} kg/s, "
            f"salt residual {dynamic_balance.salt_residual_kg_s:.16g} kg/s"
        )
    return dynamic_model


def initialize_dynamic_model(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    recycle_result: RecycleSolveResult,
    *,
    residual_tolerance: float = DEFAULT_INITIALIZATION_RESIDUAL_TOLERANCE,
) -> OpenLoopDynamicModel:
    """Short alias for :func:`initialize_open_loop_dynamic_model`."""

    return initialize_open_loop_dynamic_model(
        model,
        case,
        catalog,
        recycle_result,
        residual_tolerance=residual_tolerance,
    )
