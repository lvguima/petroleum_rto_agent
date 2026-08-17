"""Conservative reduced-order equations for the M3 open-loop CDU model."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import CaseConfig, ModelConfig, validate_config_compatibility
from ..core.types import BalanceReport, MaterialStream
from ..equipment.quality import quality_proxies
from ..flowsheet.recycle import RecycleSettings, RecycleSolveResult
from ..flowsheet.results import SteadyFlowsheetResult
from ..properties.components import ALL_COMPONENTS, ComponentCatalog
from ..properties.thermo import GAS_CONSTANT_J_MOL_K, ReducedThermo
from .sensors import SensorSpec
from .state import (
    ACTUATOR_STATE_NAMES,
    LIQUID_INVENTORY_NAMES,
    SENSOR_STATE_NAMES,
    THERMAL_STATE_NAMES,
    DynamicState,
)

COLUMN_OUTLET_NAMES: tuple[str, ...] = (
    "overhead",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
)
NET_HYDROCARBON_PRODUCT_NAMES: tuple[str, ...] = (
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

# Low-confidence engineering seeds for local (approximately +/-5 %) perturbations.
# A single M2 operating point cannot identify these sensitivities; they must be
# calibrated or replaced once sufficiently aligned plant dynamics become available.
CONDENSER_LOGIT_COOLING_SENSITIVITY = 1.0
TOWER_TOP_FURNACE_TEMPERATURE_GAIN = 0.20
KEROSENE_FURNACE_TEMPERATURE_GAIN = 0.35
LIGHT_DIESEL_FURNACE_TEMPERATURE_GAIN = 0.50
HEAVY_DIESEL_FURNACE_TEMPERATURE_GAIN = 0.65


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{context} must be finite")
    return converted


def _positive_config_value(values: Mapping[str, object], name: str) -> float:
    value = _finite_number(values[name], context=name)
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_config_value(values: Mapping[str, object], name: str) -> float:
    value = _finite_number(values[name], context=name)
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _freeze_numeric_mapping(
    values: Mapping[str, float],
    *,
    context: str,
) -> Mapping[str, float]:
    copied = {
        name: _finite_number(value, context=f"{context}.{name}")
        for name, value in values.items()
    }
    return MappingProxyType(copied)


def _component_flows(stream: MaterialStream) -> dict[str, float]:
    return {
        component: stream.component_flow_kg_s(component)
        for component in ALL_COMPONENTS
    }


def _scaled_component_flows(stream: MaterialStream, scale: float) -> dict[str, float]:
    return {
        component: scale * stream.component_flow_kg_s(component)
        for component in ALL_COMPONENTS
    }


def _inventory_outflow(
    state: DynamicState,
    inventory_name: str,
    total_flow_kg_s: float,
) -> tuple[dict[str, float], float]:
    inventory = state.liquid_inventories[inventory_name]
    fractions = inventory.mass_fractions
    component_flows = {
        component: total_flow_kg_s * fractions[component]
        for component in ALL_COMPONENTS
    }
    salt_flow = total_flow_kg_s * inventory.salt_mass_kg / inventory.total_mass_kg
    return component_flows, salt_flow


def _stable_logistic(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


@dataclass(frozen=True)
class DynamicEvaluation:
    """One immutable RHS evaluation plus its instantaneous boundary audit."""

    derivative_vector: tuple[float, ...]
    top_pressure_pa: float
    boundary_balance: BalanceReport
    boundary_mass_in_kg_s: float
    boundary_mass_out_kg_s: float
    boundary_salt_in_kg_s: float
    boundary_salt_out_kg_s: float
    boundary_component_in_kg_s: Mapping[str, float]
    boundary_component_out_kg_s: Mapping[str, float]
    stream_mass_flows_kg_s: Mapping[str, float]
    product_component_flows_kg_s: Mapping[str, Mapping[str, float]]
    product_quality_proxies: Mapping[str, Mapping[str, float]]
    diagnostics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected_dimension = len(DynamicState.vector_names())
        if len(self.derivative_vector) != expected_dimension:
            raise ValueError(
                "derivative vector has dimension "
                f"{len(self.derivative_vector)}, expected {expected_dimension}"
            )
        derivative = tuple(
            _finite_number(value, context=f"derivative_vector[{index}]")
            for index, value in enumerate(self.derivative_vector)
        )
        pressure = _finite_number(self.top_pressure_pa, context="top_pressure_pa")
        if pressure <= 0.0:
            raise ValueError("top_pressure_pa must be positive")
        rate_names = (
            "boundary_mass_in_kg_s",
            "boundary_mass_out_kg_s",
            "boundary_salt_in_kg_s",
            "boundary_salt_out_kg_s",
        )
        rates = tuple(
            _finite_number(getattr(self, name), context=name)
            for name in rate_names
        )
        if any(value < 0.0 for value in rates):
            raise ValueError("boundary rates must be non-negative")
        component_in = _freeze_numeric_mapping(
            self.boundary_component_in_kg_s,
            context="boundary_component_in_kg_s",
        )
        component_out = _freeze_numeric_mapping(
            self.boundary_component_out_kg_s,
            context="boundary_component_out_kg_s",
        )
        if set(component_in) != set(ALL_COMPONENTS):
            raise ValueError("boundary component inlet mapping must cover all components")
        if set(component_out) != set(ALL_COMPONENTS):
            raise ValueError("boundary component outlet mapping must cover all components")
        if any(value < 0.0 for value in (*component_in.values(), *component_out.values())):
            raise ValueError("boundary component rates must be non-negative")
        stream_flows = _freeze_numeric_mapping(
            self.stream_mass_flows_kg_s,
            context="stream_mass_flows_kg_s",
        )
        if any(value < 0.0 for value in stream_flows.values()):
            raise ValueError("stream mass flows must be non-negative")
        if set(self.product_component_flows_kg_s) != set(
            NET_HYDROCARBON_PRODUCT_NAMES
        ):
            raise ValueError(
                "product component-flow mapping must cover the five net hydrocarbon products"
            )
        product_component_flows: dict[str, Mapping[str, float]] = {}
        for product in NET_HYDROCARBON_PRODUCT_NAMES:
            component_flows = _freeze_numeric_mapping(
                self.product_component_flows_kg_s[product],
                context=f"product_component_flows_kg_s.{product}",
            )
            if set(component_flows) != set(ALL_COMPONENTS):
                raise ValueError(
                    f"product {product!r} component flows must cover all components"
                )
            if any(value < 0.0 for value in component_flows.values()):
                raise ValueError("product component flows must be non-negative")
            stream_flow_name = _PRODUCT_STREAM_FLOW_NAMES[product]
            if stream_flow_name not in stream_flows:
                raise ValueError(
                    f"stream mass flows are missing product {stream_flow_name!r}"
                )
            component_total = sum(component_flows.values())
            stream_total = stream_flows[stream_flow_name]
            if not math.isclose(
                component_total,
                stream_total,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    f"product {product!r} component flows do not sum to its stream flow"
                )
            product_component_flows[product] = component_flows
        if set(self.product_quality_proxies) != set(NET_HYDROCARBON_PRODUCT_NAMES):
            raise ValueError(
                "product quality mapping must cover the five net hydrocarbon products"
            )
        product_qualities: dict[str, Mapping[str, float]] = {}
        for product in NET_HYDROCARBON_PRODUCT_NAMES:
            qualities = _freeze_numeric_mapping(
                self.product_quality_proxies[product],
                context=f"product_quality_proxies.{product}",
            )
            if not qualities:
                raise ValueError(f"product {product!r} quality mapping cannot be empty")
            product_qualities[product] = qualities
        object.__setattr__(self, "derivative_vector", derivative)
        object.__setattr__(self, "top_pressure_pa", pressure)
        object.__setattr__(self, "boundary_component_in_kg_s", component_in)
        object.__setattr__(self, "boundary_component_out_kg_s", component_out)
        object.__setattr__(self, "stream_mass_flows_kg_s", stream_flows)
        object.__setattr__(
            self,
            "product_component_flows_kg_s",
            MappingProxyType(product_component_flows),
        )
        object.__setattr__(
            self,
            "product_quality_proxies",
            MappingProxyType(product_qualities),
        )
        object.__setattr__(
            self,
            "diagnostics",
            _freeze_numeric_mapping(self.diagnostics, context="diagnostics"),
        )

    @property
    def maximum_absolute_component_residual_kg_s(self) -> float:
        return max(
            (
                abs(value)
                for value in self.boundary_balance.component_residuals_kg_s.values()
            ),
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
        """Return a JSON-serializable representation."""

        return {
            "derivative_vector": list(self.derivative_vector),
            "top_pressure_pa": self.top_pressure_pa,
            "boundary_balance": self.boundary_balance.as_dict(),
            "boundary_mass_in_kg_s": self.boundary_mass_in_kg_s,
            "boundary_mass_out_kg_s": self.boundary_mass_out_kg_s,
            "boundary_salt_in_kg_s": self.boundary_salt_in_kg_s,
            "boundary_salt_out_kg_s": self.boundary_salt_out_kg_s,
            "boundary_component_in_kg_s": dict(self.boundary_component_in_kg_s),
            "boundary_component_out_kg_s": dict(self.boundary_component_out_kg_s),
            "stream_mass_flows_kg_s": dict(self.stream_mass_flows_kg_s),
            "product_component_flows_kg_s": {
                product: dict(self.product_component_flows_kg_s[product])
                for product in NET_HYDROCARBON_PRODUCT_NAMES
            },
            "product_quality_proxies": {
                product: dict(self.product_quality_proxies[product])
                for product in NET_HYDROCARBON_PRODUCT_NAMES
            },
            "maximum_absolute_component_residual_kg_s": (
                self.maximum_absolute_component_residual_kg_s
            ),
            "maximum_absolute_material_residual_kg_s": (
                self.maximum_absolute_material_residual_kg_s
            ),
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class OpenLoopDynamicModel:
    """Reduced M3 model calibrated around one conservation-qualified M2 result.

    Liquid and gas withdrawal compositions are always calculated from their
    corresponding inventories.  The tower is a component-wise, conservative
    allocation matrix obtained from the M2 gross column streams.  Consequently,
    reflux and pump-around heat are internal flows and never appear twice in the
    overall dynamic boundary.
    """

    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    recycle_result: RecycleSolveResult
    initial_state: DynamicState
    baseline_commands: Mapping[str, float]
    _steady: SteadyFlowsheetResult = field(init=False, repr=False)
    _tower_splits: Mapping[str, Mapping[str, float]] = field(init=False, repr=False)
    _nominal_gas_fractions: Mapping[str, float] = field(init=False, repr=False)
    _nominal_pump_around_duties_w: tuple[float, float, float] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        validate_config_compatibility(
            self.model,
            self.case,
            software_version=SOFTWARE_VERSION,
            catalog=self.catalog,
        )
        steady = self.recycle_result.require_converged()
        required_streams = {
            "wash_water",
            "brine",
            "flash_liquid",
            "flash_vapor",
            "furnace_feed",
            "furnace_outlet",
            "overhead",
            "oil_condensate",
            "offgas",
            "aqueous",
            "gasoline",
            "reflux",
            "kerosene",
            "light_diesel",
            "heavy_diesel",
            "residue",
        }
        missing_streams = sorted(required_streams - set(steady.streams))
        if missing_streams:
            raise ValueError(
                "M2 result is missing streams required by M3: "
                + ", ".join(missing_streams)
            )

        baseline = self._validated_commands(self.baseline_commands)
        if self.initial_state.actuator_states != baseline:
            raise ValueError("initial actuator states must equal baseline commands")

        tower_splits: dict[str, Mapping[str, float]] = {}
        for component in ALL_COMPONENTS:
            gross_outputs = {
                outlet: steady.streams[outlet].component_flow_kg_s(component)
                for outlet in COLUMN_OUTLET_NAMES
            }
            total_output = sum(gross_outputs.values())
            if total_output <= 0.0:
                raise ValueError(
                    f"M2 tower has no gross flow for component {component!r}"
                )
            tower_splits[component] = MappingProxyType(
                {
                    outlet: gross_outputs[outlet] / total_output
                    for outlet in COLUMN_OUTLET_NAMES
                }
            )

        overhead = steady.streams["overhead"]
        oil = steady.streams["oil_condensate"]
        gas = steady.streams["offgas"]
        gas_fractions: dict[str, float] = {}
        for component in ALL_COMPONENTS:
            combined = (
                oil.component_flow_kg_s(component)
                + gas.component_flow_kg_s(component)
            )
            overhead_flow = overhead.component_flow_kg_s(component)
            if component == "water":
                gas_fractions[component] = 0.0
            elif combined <= 0.0:
                if overhead_flow > 1e-12:
                    raise ValueError(
                        f"M2 condenser loses component {component!r} across oil/gas boundary"
                    )
                gas_fractions[component] = 0.0
            else:
                gas_fractions[component] = gas.component_flow_kg_s(component) / combined

        settings = RecycleSettings.from_model(self.model)
        configured_pump_duties = settings.pump_around_duties_w
        configured_total = sum(configured_pump_duties)
        reported_total = steady.diagnostics.get(
            "pump_around_removed_duty_w",
            configured_total,
        )
        if reported_total < 0.0:
            raise ValueError("M2 reported pump-around duty cannot be negative")
        if configured_total == 0.0:
            if reported_total != 0.0:
                raise ValueError("cannot infer individual pump-around duties from M2 result")
            nominal_pump_duties = (0.0, 0.0, 0.0)
        else:
            scale = reported_total / configured_total
            nominal_pump_duties = cast(
                tuple[float, float, float],
                tuple(scale * value for value in configured_pump_duties),
            )

        object.__setattr__(self, "baseline_commands", baseline)
        object.__setattr__(self, "_steady", steady)
        object.__setattr__(self, "_tower_splits", MappingProxyType(tower_splits))
        object.__setattr__(
            self,
            "_nominal_gas_fractions",
            MappingProxyType(gas_fractions),
        )
        object.__setattr__(
            self,
            "_nominal_pump_around_duties_w",
            nominal_pump_duties,
        )

    @property
    def versions(self) -> Mapping[str, str]:
        return self._steady.versions

    @property
    def input_fingerprint(self) -> str:
        return self._steady.input_fingerprint

    def _validated_commands(
        self,
        commands: Mapping[str, float],
    ) -> Mapping[str, float]:
        actual = set(commands)
        expected = set(ACTUATOR_STATE_NAMES)
        if actual != expected:
            raise ValueError(
                "command keys differ; "
                f"missing={sorted(expected - actual)}, "
                f"unknown={sorted(actual - expected)}"
            )
        validated: dict[str, float] = {}
        for name in ACTUATOR_STATE_NAMES:
            value = _finite_number(commands[name], context=f"commands.{name}")
            if value < 0.0:
                raise ValueError(f"commands.{name} must be non-negative")
            validated[name] = value
        return MappingProxyType(validated)

    @staticmethod
    def _coerce_state(state_or_vector: DynamicState | Sequence[float]) -> DynamicState:
        if isinstance(state_or_vector, DynamicState):
            return state_or_vector
        if isinstance(state_or_vector, (str, bytes, bytearray)):
            raise TypeError("state vector must be a numeric sequence")
        return DynamicState.from_vector(state_or_vector)

    def top_pressure_pa(self, state: DynamicState) -> float:
        """Derive absolute top pressure from the gas inventory using ideal gas."""

        gas_temperature_k = state.thermal_states["tower_top_temperature_k"]
        gas_moles = sum(
            state.top_gas_component_masses_kg[component]
            / self.catalog.components[component].molecular_weight_kg_mol
            for component in ALL_COMPONENTS
        )
        volume_m3 = _positive_config_value(
            self.model.dynamic,
            "top_gas_volume_m3",
        )
        pressure = gas_moles * GAS_CONSTANT_J_MOL_K * gas_temperature_k / volume_m3
        if not math.isfinite(pressure) or pressure <= 0.0:
            raise ValueError("top gas inventory implies an invalid absolute pressure")
        return pressure

    def _condenser_split(
        self,
        overhead_flows: Mapping[str, float],
        cooling_duty_w: float,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        nominal_cooling = self.baseline_commands["condenser_cooling_duty_w"]
        if nominal_cooling <= 0.0:
            raise ValueError("nominal condenser cooling duty must be positive")
        relative_shift = cooling_duty_w / nominal_cooling - 1.0
        oil: dict[str, float] = {}
        gas: dict[str, float] = {}
        aqueous = {component: 0.0 for component in ALL_COMPONENTS}
        for component in ALL_COMPONENTS:
            total = overhead_flows[component]
            if component == "water":
                aqueous[component] = total
                oil[component] = 0.0
                gas[component] = 0.0
                continue
            nominal_fraction = self._nominal_gas_fractions[component]
            if nominal_fraction <= 0.0:
                gas_fraction = 0.0
            elif nominal_fraction >= 1.0:
                gas_fraction = 1.0
            else:
                nominal_logit = math.log(
                    nominal_fraction / (1.0 - nominal_fraction)
                )
                gas_fraction = _stable_logistic(
                    nominal_logit
                    - CONDENSER_LOGIT_COOLING_SENSITIVITY * relative_shift
                )
            gas[component] = total * gas_fraction
            oil[component] = total - gas[component]
        return oil, gas, aqueous

    def _thermal_derivatives(
        self,
        state: DynamicState,
        flash_outflow_components: Mapping[str, float],
    ) -> dict[str, float]:
        thermal = state.thermal_states
        actuators = state.actuator_states
        flash_outflow = actuators["flash_liquid_outflow_kg_s"]
        if flash_outflow <= 0.0:
            raise ValueError("flash liquid outflow must be positive for furnace dynamics")
        composition = {
            component: flash_outflow_components[component] / flash_outflow
            for component in ALL_COMPONENTS
        }
        thermo = ReducedThermo(self.catalog)
        cp_liquid = thermo.mixture_cp_liquid(composition)

        recycle = self.model.equipment["recycle"]
        recovery_efficiency = _nonnegative_config_value(
            recycle,
            "heat_recovery_efficiency",
        )
        recovery_cap = _nonnegative_config_value(
            recycle,
            "maximum_recovered_duty_w",
        )
        current_pump_duties = tuple(
            actuators[f"pump_around_{index}_duty_w"]
            for index in range(1, 4)
        )
        current_available_recovery = min(
            recovery_efficiency * sum(current_pump_duties),
            recovery_cap,
        )
        preheater = self.model.equipment["pre_furnace_preheater"]
        preheater_effectiveness = _nonnegative_config_value(
            preheater,
            "effectiveness",
        )
        if preheater_effectiveness > 1.0:
            raise ValueError("preheater effectiveness cannot exceed one")
        preheater_target_temperature = _positive_config_value(
            preheater,
            "target_temperature_k",
        )
        nominal_flash_temperature = self._steady.streams["flash_liquid"].temperature_k
        effective_preheater_target = nominal_flash_temperature + (
            preheater_effectiveness
            * max(preheater_target_temperature - nominal_flash_temperature, 0.0)
        )
        desired_preheater_duty = (
            flash_outflow
            * cp_liquid
            * (effective_preheater_target - nominal_flash_temperature)
        )
        preheater_target = min(
            current_available_recovery,
            desired_preheater_duty,
        )
        preheater_tau = _positive_config_value(
            self.model.dynamic,
            "preheater_time_constant_s",
        )

        preheated_temperature = nominal_flash_temperature + (
            thermal["preheater_duty_w"] / (flash_outflow * cp_liquid)
        )
        furnace = self.model.equipment["furnace"]
        furnace_efficiency = _positive_config_value(furnace, "efficiency")
        furnace_heat_loss = _nonnegative_config_value(furnace, "heat_loss_w")
        furnace_process_duty = (
            furnace_efficiency * actuators["furnace_fuel_duty_w"]
            - furnace_heat_loss
        )
        if furnace_process_duty < 0.0:
            raise ValueError("furnace fuel duty is below the heat-loss threshold")
        furnace_target = preheated_temperature + furnace_process_duty / (
            flash_outflow * cp_liquid
        )
        maximum_furnace_temperature = _positive_config_value(
            furnace,
            "maximum_outlet_temperature_k",
        )
        if furnace_target <= 0.0 or not math.isfinite(furnace_target):
            raise ValueError("furnace energy balance implies an invalid temperature")
        if furnace_target > maximum_furnace_temperature:
            raise ValueError("furnace equilibrium temperature exceeds configured maximum")
        if thermal["furnace_outlet_temperature_k"] > maximum_furnace_temperature:
            raise ValueError("furnace state exceeds configured maximum temperature")

        furnace_tau = _positive_config_value(
            self.model.dynamic,
            "furnace_time_constant_s",
        )
        tower_tau = _positive_config_value(
            self.model.dynamic,
            "tower_temperature_time_constant_s",
        )
        nominal_furnace_temperature = self.initial_state.thermal_states[
            "furnace_outlet_temperature_k"
        ]
        furnace_delta = (
            thermal["furnace_outlet_temperature_k"] - nominal_furnace_temperature
        )
        nominal_feed_cp = thermo.mixture_cp_liquid(self.case.feed.mass_fractions)
        temperature_duty_scale = self.case.feed.mass_flow_kg_s * nominal_feed_cp
        if temperature_duty_scale <= 0.0:
            raise ValueError("nominal feed heat-capacity flow must be positive")
        pump_deltas = tuple(
            current - nominal
            for current, nominal in zip(
                current_pump_duties,
                self._nominal_pump_around_duties_w,
                strict=True,
            )
        )
        tower_targets = {
            "tower_top_temperature_k": (
                self.initial_state.thermal_states["tower_top_temperature_k"]
                + TOWER_TOP_FURNACE_TEMPERATURE_GAIN * furnace_delta
                - pump_deltas[0] / temperature_duty_scale
            ),
            "kerosene_temperature_k": (
                self.initial_state.thermal_states["kerosene_temperature_k"]
                + KEROSENE_FURNACE_TEMPERATURE_GAIN * furnace_delta
                - pump_deltas[0] / temperature_duty_scale
            ),
            "light_diesel_temperature_k": (
                self.initial_state.thermal_states["light_diesel_temperature_k"]
                + LIGHT_DIESEL_FURNACE_TEMPERATURE_GAIN * furnace_delta
                - pump_deltas[1] / temperature_duty_scale
            ),
            "heavy_diesel_temperature_k": (
                self.initial_state.thermal_states["heavy_diesel_temperature_k"]
                + HEAVY_DIESEL_FURNACE_TEMPERATURE_GAIN * furnace_delta
                - pump_deltas[2] / temperature_duty_scale
            ),
        }
        if any(
            not math.isfinite(value) or value <= 0.0
            for value in tower_targets.values()
        ):
            raise ValueError("tower heat balance implies an invalid local temperature")

        return {
            "furnace_outlet_temperature_k": (
                furnace_target - thermal["furnace_outlet_temperature_k"]
            )
            / furnace_tau,
            "tower_top_temperature_k": (
                tower_targets["tower_top_temperature_k"]
                - thermal["tower_top_temperature_k"]
            )
            / tower_tau,
            "kerosene_temperature_k": (
                tower_targets["kerosene_temperature_k"]
                - thermal["kerosene_temperature_k"]
            )
            / tower_tau,
            "light_diesel_temperature_k": (
                tower_targets["light_diesel_temperature_k"]
                - thermal["light_diesel_temperature_k"]
            )
            / tower_tau,
            "heavy_diesel_temperature_k": (
                tower_targets["heavy_diesel_temperature_k"]
                - thermal["heavy_diesel_temperature_k"]
            )
            / tower_tau,
            "preheater_duty_w": (
                preheater_target - thermal["preheater_duty_w"]
            )
            / preheater_tau,
        }

    def evaluate(
        self,
        state_or_vector: DynamicState | Sequence[float],
        commands: Mapping[str, float] | None = None,
    ) -> DynamicEvaluation:
        """Evaluate signed state rates and an instantaneous net-boundary audit."""

        state = self._coerce_state(state_or_vector)
        selected_commands = self._validated_commands(
            self.baseline_commands if commands is None else commands
        )
        actuators = state.actuator_states
        nominal_feed_flow = self.case.feed.mass_flow_kg_s
        if nominal_feed_flow <= 0.0:
            raise ValueError("nominal fresh feed flow must be positive")
        feed_scale = actuators["fresh_feed_flow_kg_s"] / nominal_feed_flow

        wash = _scaled_component_flows(self._steady.streams["wash_water"], feed_scale)
        brine = _scaled_component_flows(self._steady.streams["brine"], feed_scale)
        flash_source = _scaled_component_flows(
            self._steady.streams["flash_liquid"],
            feed_scale,
        )
        flash_vapor = _scaled_component_flows(
            self._steady.streams["flash_vapor"],
            feed_scale,
        )
        flash_source_salt = (
            feed_scale * self._steady.streams["flash_liquid"].salt_mass_flow_kg_s
        )
        brine_salt = feed_scale * self._steady.streams["brine"].salt_mass_flow_kg_s

        flash_outflow, flash_outflow_salt = _inventory_outflow(
            state,
            "flash_drum",
            actuators["flash_liquid_outflow_kg_s"],
        )
        reflux_outflow, reflux_outflow_salt = _inventory_outflow(
            state,
            "reflux_drum",
            actuators["reflux_flow_kg_s"],
        )
        gasoline_outflow, gasoline_outflow_salt = _inventory_outflow(
            state,
            "reflux_drum",
            actuators["gasoline_draw_kg_s"],
        )
        residue_outflow, residue_outflow_salt = _inventory_outflow(
            state,
            "tower_bottom",
            actuators["residue_draw_kg_s"],
        )

        tower_in = {
            component: (
                flash_outflow[component]
                + flash_vapor[component]
                + reflux_outflow[component]
            )
            for component in ALL_COMPONENTS
        }
        column_outputs = {
            outlet: {
                component: (
                    tower_in[component] * self._tower_splits[component][outlet]
                )
                for component in ALL_COMPONENTS
            }
            for outlet in COLUMN_OUTLET_NAMES
        }
        column_residue_salt = flash_outflow_salt + reflux_outflow_salt

        oil_source, gas_source, aqueous_outflow = self._condenser_split(
            column_outputs["overhead"],
            actuators["condenser_cooling_duty_w"],
        )
        gas_total_mass = sum(state.top_gas_component_masses_kg.values())
        if gas_total_mass <= 0.0:
            raise ValueError("top gas inventory must be positive")
        gas_vent = {
            component: (
                actuators["top_gas_vent_kg_s"]
                * state.top_gas_component_masses_kg[component]
                / gas_total_mass
            )
            for component in ALL_COMPONENTS
        }

        inventory_component_derivatives: dict[str, dict[str, float]] = {
            "flash_drum": {
                component: flash_source[component] - flash_outflow[component]
                for component in ALL_COMPONENTS
            },
            "reflux_drum": {
                component: (
                    oil_source[component]
                    - reflux_outflow[component]
                    - gasoline_outflow[component]
                )
                for component in ALL_COMPONENTS
            },
            "tower_bottom": {
                component: (
                    column_outputs["residue"][component]
                    - residue_outflow[component]
                )
                for component in ALL_COMPONENTS
            },
        }
        inventory_salt_derivatives = {
            "flash_drum": flash_source_salt - flash_outflow_salt,
            "reflux_drum": -reflux_outflow_salt - gasoline_outflow_salt,
            "tower_bottom": column_residue_salt - residue_outflow_salt,
        }
        gas_derivatives = {
            component: gas_source[component] - gas_vent[component]
            for component in ALL_COMPONENTS
        }

        thermal_derivatives = self._thermal_derivatives(state, flash_outflow)
        actuator_tau = _positive_config_value(
            self.model.dynamic,
            "actuator_time_constant_s",
        )
        condenser_tau = _positive_config_value(
            self.model.dynamic,
            "condenser_time_constant_s",
        )
        actuator_derivatives: dict[str, float] = {}
        for name in ACTUATOR_STATE_NAMES:
            time_constant_s = (
                condenser_tau
                if name == "condenser_cooling_duty_w"
                else actuator_tau
            )
            # Only the defensible physical domain is enforced here: these flow
            # and positive-duty magnitudes cannot be negative.  No plant travel
            # or slew limits are inferred from the nominal M2 point, and commands
            # are therefore not silently clipped to an arbitrary nominal multiple.
            actuator_derivatives[name] = (
                selected_commands[name] - actuators[name]
            ) / time_constant_s

        pressure = self.top_pressure_pa(state)
        true_sensor_values = {
            "furnace_outlet_temperature_k": state.thermal_states[
                "furnace_outlet_temperature_k"
            ],
            "tower_top_pressure_pa": pressure,
            "tower_top_temperature_k": state.thermal_states[
                "tower_top_temperature_k"
            ],
            "flash_drum_inventory_kg": state.liquid_inventories[
                "flash_drum"
            ].total_mass_kg,
            "reflux_drum_inventory_kg": state.liquid_inventories[
                "reflux_drum"
            ].total_mass_kg,
            "tower_bottom_inventory_kg": state.liquid_inventories[
                "tower_bottom"
            ].total_mass_kg,
        }
        sensor_tau = _positive_config_value(
            self.model.dynamic,
            "sensor_time_constant_s",
        )
        sensor_spec = SensorSpec(sensor_tau)
        sensor_derivatives = {
            name: sensor_spec.derivative(
                state.sensor_states[name],
                true_sensor_values[name],
            )
            for name in SENSOR_STATE_NAMES
        }

        derivative_values: list[float] = []
        for inventory_name in LIQUID_INVENTORY_NAMES:
            derivative_values.extend(
                inventory_component_derivatives[inventory_name][component]
                for component in ALL_COMPONENTS
            )
            derivative_values.append(inventory_salt_derivatives[inventory_name])
        derivative_values.extend(gas_derivatives[component] for component in ALL_COMPONENTS)
        derivative_values.extend(
            thermal_derivatives[name] for name in THERMAL_STATE_NAMES
        )
        derivative_values.extend(
            actuator_derivatives[name] for name in ACTUATOR_STATE_NAMES
        )
        derivative_values.extend(sensor_derivatives[name] for name in SENSOR_STATE_NAMES)
        derivative_vector = tuple(derivative_values)

        feed_components = _scaled_component_flows(self.case.feed, feed_scale)
        boundary_component_in = {
            component: feed_components[component] + wash[component]
            for component in ALL_COMPONENTS
        }
        boundary_component_out = {
            component: (
                brine[component]
                + column_outputs["kerosene"][component]
                + column_outputs["light_diesel"][component]
                + column_outputs["heavy_diesel"][component]
                + gasoline_outflow[component]
                + residue_outflow[component]
                + gas_vent[component]
                + aqueous_outflow[component]
            )
            for component in ALL_COMPONENTS
        }
        component_accumulation = {
            component: (
                sum(
                    inventory_component_derivatives[inventory_name][component]
                    for inventory_name in LIQUID_INVENTORY_NAMES
                )
                + gas_derivatives[component]
            )
            for component in ALL_COMPONENTS
        }
        component_residuals = {
            component: (
                boundary_component_in[component]
                - boundary_component_out[component]
                - component_accumulation[component]
            )
            for component in ALL_COMPONENTS
        }
        boundary_mass_in = sum(boundary_component_in.values())
        boundary_mass_out = sum(boundary_component_out.values())
        total_accumulation = sum(component_accumulation.values())
        boundary_salt_in = feed_scale * self.case.feed.salt_mass_flow_kg_s
        boundary_salt_out = brine_salt + gasoline_outflow_salt + residue_outflow_salt
        salt_accumulation = sum(inventory_salt_derivatives.values())
        balance = BalanceReport(
            inlet_kg_s=boundary_mass_in,
            outlet_kg_s=boundary_mass_out,
            accumulation_kg_s=total_accumulation,
            component_residuals_kg_s=component_residuals,
            salt_residual_kg_s=(
                boundary_salt_in - boundary_salt_out - salt_accumulation
            ),
        )
        stream_mass_flows = {
            "fresh_crude": sum(feed_components.values()),
            "wash_water": sum(wash.values()),
            "brine": sum(brine.values()),
            "flash_vapor": sum(flash_vapor.values()),
            "flash_liquid_to_furnace": sum(flash_outflow.values()),
            "reflux": sum(reflux_outflow.values()),
            "column_overhead": sum(column_outputs["overhead"].values()),
            "oil_condensate": sum(oil_source.values()),
            "offgas_to_drum": sum(gas_source.values()),
            "aqueous": sum(aqueous_outflow.values()),
            "gasoline": sum(gasoline_outflow.values()),
            "kerosene": sum(column_outputs["kerosene"].values()),
            "light_diesel": sum(column_outputs["light_diesel"].values()),
            "heavy_diesel": sum(column_outputs["heavy_diesel"].values()),
            "residue_to_bottom": sum(column_outputs["residue"].values()),
            "residue_product": sum(residue_outflow.values()),
            "top_gas_vent": sum(gas_vent.values()),
        }
        product_component_flows = {
            "gasoline": gasoline_outflow,
            "kerosene": column_outputs["kerosene"],
            "light_diesel": column_outputs["light_diesel"],
            "heavy_diesel": column_outputs["heavy_diesel"],
            "residue": residue_outflow,
        }
        product_temperatures = {
            "gasoline": self._steady.streams["gasoline"].temperature_k,
            "kerosene": state.thermal_states["kerosene_temperature_k"],
            "light_diesel": state.thermal_states["light_diesel_temperature_k"],
            "heavy_diesel": state.thermal_states["heavy_diesel_temperature_k"],
            "residue": self._steady.streams["residue"].temperature_k,
        }
        product_fallback_compositions: Mapping[str, Mapping[str, float]] = {
            "gasoline": state.liquid_inventories["reflux_drum"].mass_fractions,
            "kerosene": self._steady.streams["kerosene"].mass_fractions,
            "light_diesel": self._steady.streams["light_diesel"].mass_fractions,
            "heavy_diesel": self._steady.streams["heavy_diesel"].mass_fractions,
            "residue": state.liquid_inventories["tower_bottom"].mass_fractions,
        }
        product_streams: dict[str, MaterialStream] = {}
        for product in NET_HYDROCARBON_PRODUCT_NAMES:
            component_flows = product_component_flows[product]
            total_flow = sum(component_flows.values())
            composition = (
                {
                    component: component_flows[component] / total_flow
                    for component in ALL_COMPONENTS
                }
                if total_flow > 0.0
                else dict(product_fallback_compositions[product])
            )
            product_streams[product] = MaterialStream(
                name=product,
                mass_flow_kg_s=total_flow,
                temperature_k=product_temperatures[product],
                pressure_pa=self._steady.streams[product].pressure_pa,
                mass_fractions=composition,
            )
        product_qualities = {
            product: quality_proxies(product_streams[product], self.catalog)
            for product in NET_HYDROCARBON_PRODUCT_NAMES
        }
        return DynamicEvaluation(
            derivative_vector=derivative_vector,
            top_pressure_pa=pressure,
            boundary_balance=balance,
            boundary_mass_in_kg_s=boundary_mass_in,
            boundary_mass_out_kg_s=boundary_mass_out,
            boundary_salt_in_kg_s=boundary_salt_in,
            boundary_salt_out_kg_s=boundary_salt_out,
            boundary_component_in_kg_s=boundary_component_in,
            boundary_component_out_kg_s=boundary_component_out,
            stream_mass_flows_kg_s=stream_mass_flows,
            product_component_flows_kg_s=product_component_flows,
            product_quality_proxies=product_qualities,
            diagnostics={
                "feed_scale": feed_scale,
                "flash_bulk_accumulation_kg_s": sum(
                    inventory_component_derivatives["flash_drum"].values()
                ),
                "reflux_bulk_accumulation_kg_s": sum(
                    inventory_component_derivatives["reflux_drum"].values()
                ),
                "tower_bottom_bulk_accumulation_kg_s": sum(
                    inventory_component_derivatives["tower_bottom"].values()
                ),
                "top_gas_bulk_accumulation_kg_s": sum(gas_derivatives.values()),
            },
        )

    def rhs(
        self,
        time_s: float,
        state_or_vector: DynamicState | Sequence[float],
        commands: Mapping[str, float] | None = None,
    ) -> tuple[float, ...]:
        """Return the deterministic signed derivative vector."""

        time_value = _finite_number(time_s, context="time_s")
        if time_value < 0.0:
            raise ValueError("time_s must be non-negative")
        return self.evaluate(state_or_vector, commands).derivative_vector
