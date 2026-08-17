"""M2 physical reflux and equivalent heat-recovery closure."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import (
    CaseConfig,
    ModelConfig,
    canonical_fingerprint,
    validate_config_compatibility,
)
from ..core.conservation import material_balance
from ..core.types import BalanceReport, MaterialStream, UnitResult, stream_from_component_flows
from ..equipment import (
    EquivalentPreheater,
    Furnace,
    OverheadCondenser,
    ReducedColumn,
    quality_proxies,
)
from ..properties.components import ALL_COMPONENTS, ComponentCatalog
from ..properties.thermo import ReducedThermo
from .open_loop import (
    MAIN_PRODUCT_NAMES,
    _float_mapping_parameter,
    _float_parameter,
    _four_float_parameters,
    run_open_loop,
)
from .results import SteadyFlowsheetResult

_RECYCLE_STATUSES = frozenset({"success", "not_converged", "failed", "rejected"})
_ENERGY_TOLERANCE_W = 1e-5
_UPSTREAM_UNIT_NAMES = (
    "pre_desalter_preheater",
    "desalter",
    "post_desalter_preheater",
    "flash",
    "pre_furnace_preheater",
)
_UPSTREAM_STREAM_NAMES = (
    "wash_water",
    "pre_desalter_crude",
    "desalted_crude",
    "brine",
    "flash_feed",
    "flash_vapor",
    "flash_liquid",
    "furnace_feed",
)


def _three_float_parameters(
    parameters: Mapping[str, object],
    name: str,
) -> tuple[float, float, float]:
    value = parameters[name]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"equipment parameter {name!r} must be a sequence")
    if len(value) != 3:
        raise ValueError(f"equipment parameter {name!r} must contain three values")
    converted = tuple(_float_parameter({name: item}, name) for item in value)
    return cast(tuple[float, float, float], converted)


@dataclass(frozen=True)
class RecycleSettings:
    """All numerical settings that define one M2 recycle solve."""

    reflux_ratio: float
    reflux_sharpness_gain: float
    pump_around_duties_w: tuple[float, float, float]
    heat_recovery_efficiency: float
    maximum_recovered_duty_w: float
    tolerance: float
    maximum_iterations: int
    relaxation_factor: float

    def __post_init__(self) -> None:
        if len(self.pump_around_duties_w) != 3:
            raise ValueError("pump-around duties must contain exactly three values")
        if not isinstance(self.maximum_iterations, int) or isinstance(
            self.maximum_iterations,
            bool,
        ):
            raise TypeError("maximum iterations must be a non-boolean integer")
        scalar_values = (
            self.reflux_ratio,
            self.reflux_sharpness_gain,
            *self.pump_around_duties_w,
            self.heat_recovery_efficiency,
            self.maximum_recovered_duty_w,
            self.tolerance,
            self.relaxation_factor,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in scalar_values
        ):
            raise TypeError("recycle numeric settings must be non-boolean numbers")
        if any(not math.isfinite(value) for value in scalar_values):
            raise ValueError("recycle settings must be finite")
        if self.reflux_ratio < 0.0 or self.reflux_sharpness_gain < 0.0:
            raise ValueError("reflux ratio and sharpness gain must be non-negative")
        if any(value < 0.0 for value in self.pump_around_duties_w):
            raise ValueError("pump-around duties must be non-negative")
        if not 0.0 <= self.heat_recovery_efficiency <= 1.0:
            raise ValueError("heat-recovery efficiency must be between zero and one")
        if self.maximum_recovered_duty_w < 0.0:
            raise ValueError("maximum recovered duty must be non-negative")
        if self.tolerance <= 0.0:
            raise ValueError("recycle tolerance must be positive")
        if self.maximum_iterations < 1:
            raise ValueError("maximum iterations must be at least one")
        if not 0.0 < self.relaxation_factor <= 1.0:
            raise ValueError("relaxation factor must be in the interval (0, 1]")

    @classmethod
    def from_model(cls, model: ModelConfig) -> RecycleSettings:
        recycle = model.equipment["recycle"]
        solver = model.solver
        maximum_iterations = solver["maximum_iterations"]
        if not isinstance(maximum_iterations, int) or isinstance(maximum_iterations, bool):
            raise TypeError("maximum_iterations must be an integer")
        return cls(
            reflux_ratio=_float_parameter(recycle, "reflux_ratio"),
            reflux_sharpness_gain=_float_parameter(recycle, "reflux_sharpness_gain"),
            pump_around_duties_w=_three_float_parameters(
                recycle,
                "pump_around_duties_w",
            ),
            heat_recovery_efficiency=_float_parameter(
                recycle,
                "heat_recovery_efficiency",
            ),
            maximum_recovered_duty_w=_float_parameter(
                recycle,
                "maximum_recovered_duty_w",
            ),
            tolerance=_float_parameter(solver, "recycle_tolerance"),
            maximum_iterations=maximum_iterations,
            relaxation_factor=_float_parameter(solver, "relaxation_factor"),
        )

    @property
    def potential_recovered_duty_w(self) -> float:
        return self.heat_recovery_efficiency * sum(self.pump_around_duties_w)

    @property
    def available_recovered_duty_w(self) -> float:
        return min(self.potential_recovered_duty_w, self.maximum_recovered_duty_w)

    @property
    def separation_width_scale(self) -> float:
        return 1.0 / (1.0 + self.reflux_sharpness_gain * self.reflux_ratio)

    def as_dict(self) -> dict[str, object]:
        return {
            "reflux_ratio": self.reflux_ratio,
            "reflux_sharpness_gain": self.reflux_sharpness_gain,
            "pump_around_duties_w": list(self.pump_around_duties_w),
            "heat_recovery_efficiency": self.heat_recovery_efficiency,
            "maximum_recovered_duty_w": self.maximum_recovered_duty_w,
            "tolerance": self.tolerance,
            "maximum_iterations": self.maximum_iterations,
            "relaxation_factor": self.relaxation_factor,
        }


@dataclass(frozen=True)
class RecycleSolveResult:
    """Convergence envelope around the last physically valid steady result."""

    status: str
    flowsheet: SteadyFlowsheetResult | None
    iterations: int
    final_residual: float | None
    residual_history: tuple[float, ...]
    reflux: MaterialStream | None
    failure_reason: str | None = None
    failure_stage: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _RECYCLE_STATUSES:
            raise ValueError(f"unsupported recycle status: {self.status!r}")
        if self.iterations < 0:
            raise ValueError("iteration count cannot be negative")
        if self.final_residual is not None and (
            not math.isfinite(self.final_residual) or self.final_residual < 0.0
        ):
            raise ValueError("final residual must be finite and non-negative")
        if any(not math.isfinite(value) or value < 0.0 for value in self.residual_history):
            raise ValueError("residual history must be finite and non-negative")
        if self.status == "success" and (
            self.flowsheet is None or self.flowsheet.status != "success"
        ):
            raise ValueError("a successful recycle solve requires a successful flowsheet")
        if self.status != "success" and not self.failure_reason:
            raise ValueError("an unsuccessful recycle solve requires a failure reason")
        if self.status != "success" and not self.failure_stage:
            raise ValueError("an unsuccessful recycle solve requires a failure stage")
        if self.status == "success" and (
            self.failure_reason is not None or self.failure_stage is not None
        ):
            raise ValueError("a successful recycle solve cannot contain failure details")

    @property
    def converged(self) -> bool:
        return self.status == "success"

    def require_converged(self) -> SteadyFlowsheetResult:
        if self.flowsheet is None or not self.converged:
            raise RuntimeError(self.failure_reason or "recycle solve did not converge")
        return self.flowsheet

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "flowsheet": None if self.flowsheet is None else self.flowsheet.as_dict(),
            "iterations": self.iterations,
            "final_residual": self.final_residual,
            "residual_history": list(self.residual_history),
            "reflux": None if self.reflux is None else self.reflux.as_dict(),
            "failure_reason": self.failure_reason,
            "failure_stage": self.failure_stage,
        }


def _zero_reflux(*, temperature_k: float, pressure_pa: float) -> MaterialStream:
    return MaterialStream(
        "reflux",
        0.0,
        temperature_k,
        pressure_pa,
        {"naphtha": 1.0},
        metadata={"role": "tear stream"},
    )


def _scaled_reflux(stream: MaterialStream, fraction: float) -> MaterialStream:
    return stream.at_conditions(
        name="reflux",
        mass_flow_kg_s=stream.mass_flow_kg_s * fraction,
    )


def _relax_reflux(
    current: MaterialStream,
    target: MaterialStream,
    relaxation_factor: float,
) -> MaterialStream:
    component_flows = {
        component: (
            current.component_flow_kg_s(component)
            + relaxation_factor
            * (
                target.component_flow_kg_s(component)
                - current.component_flow_kg_s(component)
            )
        )
        for component in ALL_COMPONENTS
    }
    total = sum(component_flows.values())
    if total <= 0.0:
        return _zero_reflux(
            temperature_k=target.temperature_k,
            pressure_pa=target.pressure_pa,
        )
    return stream_from_component_flows(
        "reflux",
        component_flows,
        temperature_k=target.temperature_k,
        pressure_pa=target.pressure_pa,
        metadata={"role": "tear stream"},
    )


def _recycle_residual(
    current: MaterialStream,
    target: MaterialStream,
    feed_scale_kg_s: float,
) -> float:
    component_residual = max(
        abs(
            current.component_flow_kg_s(component)
            - target.component_flow_kg_s(component)
        )
        / max(feed_scale_kg_s, 1.0)
        for component in ALL_COMPONENTS
    )
    flow_residual = abs(current.mass_flow_kg_s - target.mass_flow_kg_s) / max(
        feed_scale_kg_s,
        1.0,
    )
    temperature_residual = abs(current.temperature_k - target.temperature_k) / max(
        target.temperature_k,
        1.0,
    )
    pressure_residual = abs(current.pressure_pa - target.pressure_pa) / max(
        target.pressure_pa,
        1.0,
    )
    return max(
        component_residual,
        flow_residual,
        temperature_residual,
        pressure_residual,
    )


def _net_gasoline(
    oil_condensate: MaterialStream,
    reflux: MaterialStream,
) -> MaterialStream:
    differences = {
        component: (
            oil_condensate.component_flow_kg_s(component)
            - reflux.component_flow_kg_s(component)
        )
        for component in ALL_COMPONENTS
    }
    tolerance = 1e-10 * max(oil_condensate.mass_flow_kg_s, 1.0)
    if any(value < -tolerance for value in differences.values()):
        raise ValueError("reflux tear stream exceeds current oil condensate")
    corrected = {component: max(value, 0.0) for component, value in differences.items()}
    total = sum(corrected.values())
    if total <= 0.0:
        return MaterialStream(
            "gasoline",
            0.0,
            oil_condensate.temperature_k,
            oil_condensate.pressure_pa,
            {"naphtha": 1.0},
        )
    return stream_from_component_flows(
        "gasoline",
        corrected,
        temperature_k=oil_condensate.temperature_k,
        pressure_pa=oil_condensate.pressure_pa,
    )


def _validated_initial_reflux(
    initial_reflux: MaterialStream | None,
    *,
    case: CaseConfig,
    condenser_temperature_k: float,
    column_pressure_pa: float,
) -> MaterialStream:
    if initial_reflux is None:
        return _zero_reflux(
            temperature_k=condenser_temperature_k,
            pressure_pa=column_pressure_pa,
        )
    if initial_reflux.salt_mass_flow_kg_s > 1e-12:
        raise ValueError("initial reflux cannot contain salt")
    if initial_reflux.component_flow_kg_s("water") > 1e-9:
        raise ValueError("initial reflux must be an oil stream without water")
    if initial_reflux.mass_flow_kg_s > 5.0 * case.feed.mass_flow_kg_s:
        raise ValueError("initial reflux exceeds the allowed engineering bound")
    if initial_reflux.pressure_pa < column_pressure_pa:
        raise ValueError("initial reflux pressure is below column pressure")
    return initial_reflux.at_conditions(
        name="reflux",
        pressure_pa=column_pressure_pa,
    )


def _conservation_gate(
    model: ModelConfig,
    balance: BalanceReport,
    unit_results: Mapping[str, UnitResult],
) -> bool:
    tolerances = {
        "mass_atol_kg_s": _float_parameter(model.solver, "mass_tolerance_kg_s"),
        "component_atol_kg_s": _float_parameter(
            model.solver,
            "component_tolerance_kg_s",
        ),
        "salt_atol_kg_s": _float_parameter(model.solver, "salt_tolerance_kg_s"),
        "energy_atol_w": _ENERGY_TOLERANCE_W,
    }
    if not balance.passed(**tolerances):
        return False
    return all(
        result.balance is not None and result.balance.passed(**tolerances)
        for result in unit_results.values()
    )


@dataclass(frozen=True)
class RecycleCDU:
    """Configuration-driven M2 solver for reflux and recovered heat."""

    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    settings: RecycleSettings | None = None
    software_version: str = SOFTWARE_VERSION

    def __post_init__(self) -> None:
        validate_config_compatibility(
            self.model,
            self.case,
            software_version=self.software_version,
            catalog=self.catalog,
        )

    def _effective_settings(self) -> RecycleSettings:
        return RecycleSettings.from_model(self.model) if self.settings is None else self.settings

    def _prepare_upstream(
        self,
        settings: RecycleSettings,
        thermo: ReducedThermo,
    ) -> tuple[
        SteadyFlowsheetResult,
        dict[str, MaterialStream],
        dict[str, UnitResult],
        MaterialStream,
        MaterialStream,
    ]:
        base = run_open_loop(
            self.model,
            self.case,
            self.catalog,
            software_version=self.software_version,
        )
        streams = {self.case.feed.name: self.case.feed}
        streams.update(
            {
                name: base.streams[name]
                for name in _UPSTREAM_STREAM_NAMES
            }
        )
        unit_results = {
            name: base.unit_results[name]
            for name in _UPSTREAM_UNIT_NAMES
        }
        streams.pop("furnace_feed")
        pre_recovery_feed = streams["flash_liquid"].renamed(
            "pre_recovery_furnace_feed"
        )
        streams[pre_recovery_feed.name] = pre_recovery_feed
        pre_furnace_parameters = self.model.equipment["pre_furnace_preheater"]
        recovered_preheat = EquivalentPreheater(
            thermo=thermo,
            effectiveness=_float_parameter(pre_furnace_parameters, "effectiveness"),
            target_temperature_k=_float_parameter(
                pre_furnace_parameters,
                "target_temperature_k",
            ),
            pressure_drop_pa=_float_parameter(
                pre_furnace_parameters,
                "pressure_drop_pa",
            ),
        ).solve(
            pre_recovery_feed,
            available_duty_w=settings.available_recovered_duty_w,
            outlet_name="furnace_feed",
        )
        unit_results["pre_furnace_preheater"] = recovered_preheat
        furnace_feed = recovered_preheat.outlets["furnace_feed"]
        streams[furnace_feed.name] = furnace_feed

        furnace_parameters = self.model.equipment["furnace"]
        furnace = Furnace(
            thermo=thermo,
            efficiency=_float_parameter(furnace_parameters, "efficiency"),
            heat_loss_w=_float_parameter(furnace_parameters, "heat_loss_w"),
            maximum_outlet_temperature_k=_float_parameter(
                furnace_parameters,
                "maximum_outlet_temperature_k",
            ),
            pressure_drop_pa=_float_parameter(furnace_parameters, "pressure_drop_pa"),
        ).solve(
            furnace_feed,
            outlet_temperature_k=self.case.operating_conditions[
                "furnace_outlet_temperature_k"
            ],
            outlet_name="furnace_outlet",
        )
        unit_results["furnace"] = furnace
        furnace_outlet = furnace.outlets["furnace_outlet"]
        streams[furnace_outlet.name] = furnace_outlet
        return base, streams, unit_results, furnace_outlet, streams["flash_vapor"]

    def solve(self, *, initial_reflux: MaterialStream | None = None) -> RecycleSolveResult:
        """Iterate the reflux tear stream and retain the last valid state on failure."""

        try:
            settings = self._effective_settings()
        except (TypeError, ValueError) as exc:
            return RecycleSolveResult(
                status="rejected",
                flowsheet=None,
                iterations=0,
                final_residual=None,
                residual_history=(),
                reflux=None,
                failure_reason=str(exc),
                failure_stage="settings",
            )
        thermo = ReducedThermo(self.catalog)
        column_pressure = self.case.operating_conditions["tower_top_pressure_pa"]
        condenser_temperature = self.case.operating_conditions["condenser_temperature_k"]
        try:
            reflux = _validated_initial_reflux(
                initial_reflux,
                case=self.case,
                condenser_temperature_k=condenser_temperature,
                column_pressure_pa=column_pressure,
            )
        except ValueError as exc:
            return RecycleSolveResult(
                status="rejected",
                flowsheet=None,
                iterations=0,
                final_residual=None,
                residual_history=(),
                reflux=initial_reflux,
                failure_reason=str(exc),
                failure_stage="initial_reflux",
            )

        try:
            base, upstream_streams, upstream_units, furnace_outlet, flash_vapor = (
                self._prepare_upstream(settings, thermo)
            )
            column_parameters = self.model.equipment["column"]
            column_model = ReducedColumn(
                thermo=thermo,
                pressure_pa=column_pressure,
                cut_points_k=_four_float_parameters(column_parameters, "cut_points_k"),
                separation_widths_k=_four_float_parameters(
                    column_parameters,
                    "separation_widths_k",
                ),
                product_temperatures_k=_float_mapping_parameter(
                    column_parameters,
                    "product_temperatures_k",
                ),
            )
            condenser_parameters = self.model.equipment["condenser"]
            condenser_model = OverheadCondenser(
                thermo=thermo,
                temperature_k=condenser_temperature,
                pressure_pa=column_pressure,
                condensation_width_k=_float_parameter(
                    condenser_parameters,
                    "condensation_width_k",
                ),
            )
            versions = validate_config_compatibility(
                self.model,
                self.case,
                software_version=self.software_version,
                catalog=self.catalog,
            )
            fingerprint = canonical_fingerprint(
                {
                    "stage": "M2",
                    "base_input_fingerprint": base.input_fingerprint,
                    "settings": settings.as_dict(),
                }
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            return RecycleSolveResult(
                status="failed",
                flowsheet=None,
                iterations=0,
                final_residual=None,
                residual_history=(),
                reflux=None,
                failure_reason=str(exc),
                failure_stage="preparation",
            )
        residual_history: list[float] = []
        last_valid: SteadyFlowsheetResult | None = None
        last_valid_reflux: MaterialStream | None = None
        last_valid_history: tuple[float, ...] = ()
        last_valid_iteration = 0

        for iteration in range(1, settings.maximum_iterations + 1):
            try:
                column_result = column_model.solve(
                    furnace_outlet,
                    flash_vapor,
                    reflux=(None if reflux.mass_flow_kg_s == 0.0 else reflux),
                    separation_width_scale=settings.separation_width_scale,
                )
                column = column_result.unit_result
                condenser = condenser_model.solve(column.outlets["overhead"])
                oil_condensate = condenser.outlets["oil_condensate"]
                target_reflux = _scaled_reflux(
                    oil_condensate,
                    settings.reflux_ratio / (1.0 + settings.reflux_ratio),
                )
                residual = _recycle_residual(
                    reflux,
                    target_reflux,
                    self.case.feed.mass_flow_kg_s,
                )
                residual_history.append(residual)
                try:
                    gasoline = _net_gasoline(oil_condensate, reflux)
                except ValueError:
                    if residual <= settings.tolerance:
                        raise
                    if iteration < settings.maximum_iterations:
                        reflux = _relax_reflux(
                            reflux,
                            target_reflux,
                            settings.relaxation_factor,
                        )
                    continue

                streams = dict(upstream_streams)
                streams[reflux.name] = reflux
                streams.update({stream.name: stream for stream in column.outlets.values()})
                streams.update({stream.name: stream for stream in condenser.outlets.values()})
                streams[gasoline.name] = gasoline
                unit_results = dict(upstream_units)
                unit_results["column"] = column
                unit_results["condenser"] = condenser
                splitter_energy_residual = (
                    thermo.stream_enthalpy_w(oil_condensate)
                    - thermo.stream_enthalpy_w(gasoline)
                    - thermo.stream_enthalpy_w(reflux)
                )
                unit_results["reflux_splitter"] = UnitResult(
                    outlets={"gasoline": gasoline, "reflux": reflux},
                    diagnostics={
                        "configured_reflux_ratio": settings.reflux_ratio,
                        "realized_reflux_ratio": (
                            reflux.mass_flow_kg_s
                            / max(gasoline.mass_flow_kg_s, 1e-30)
                        ),
                    },
                    balance=material_balance(
                        [oil_condensate],
                        [gasoline, reflux],
                        energy_residual_w=splitter_energy_residual,
                    ),
                )
                products = {
                    "gasoline": gasoline,
                    "kerosene": column.outlets["kerosene"],
                    "light_diesel": column.outlets["light_diesel"],
                    "heavy_diesel": column.outlets["heavy_diesel"],
                    "residue": column.outlets["residue"],
                    "offgas": condenser.outlets["offgas"],
                    "aqueous": condenser.outlets["aqueous"],
                    "brine": streams["brine"],
                }
                inlet_enthalpy = thermo.stream_enthalpy_w(
                    self.case.feed
                ) + thermo.stream_enthalpy_w(streams["wash_water"])
                outlet_enthalpy = thermo.stream_enthalpy_w(
                    products["offgas"],
                    phase="vapor",
                ) + sum(
                    thermo.stream_enthalpy_w(stream)
                    for name, stream in products.items()
                    if name != "offgas"
                )
                total_process_duty = sum(
                    result.duty_w for result in unit_results.values()
                )
                balance = material_balance(
                    [self.case.feed, streams["wash_water"]],
                    products.values(),
                    energy_residual_w=(
                        inlet_enthalpy + total_process_duty - outlet_enthalpy
                    ),
                )
                fixed_point_converged = residual <= settings.tolerance
                conservation_passed = _conservation_gate(
                    self.model,
                    balance,
                    unit_results,
                )
                actual_recovered_duty = unit_results[
                    "pre_furnace_preheater"
                ].duty_w
                diagnostics = {
                    "iterations": float(iteration),
                    "recycle_residual": residual,
                    "configured_reflux_ratio": settings.reflux_ratio,
                    "realized_reflux_ratio": (
                        reflux.mass_flow_kg_s / max(gasoline.mass_flow_kg_s, 1e-30)
                    ),
                    "separation_width_scale": settings.separation_width_scale,
                    "potential_recovered_duty_w": settings.potential_recovered_duty_w,
                    "available_recovered_duty_w": settings.available_recovered_duty_w,
                    "actual_recovered_duty_w": actual_recovered_duty,
                    "unused_available_recovery_w": (
                        settings.available_recovered_duty_w
                        - actual_recovered_duty
                    ),
                    "pump_around_removed_duty_w": sum(
                        settings.pump_around_duties_w
                    ),
                    "heat_recovery_hot_side_w": -actual_recovered_duty,
                    "heat_recovery_cold_side_w": actual_recovered_duty,
                    "heat_recovery_internal_net_w": 0.0,
                    "furnace_process_duty_w": unit_results["furnace"].duty_w,
                    "furnace_fuel_duty_w": unit_results["furnace"].diagnostics[
                        "fuel_duty_w"
                    ],
                    "overall_mass_residual_kg_s": balance.residual_kg_s,
                    "energy_residual_w": cast(float, balance.energy_residual_w),
                    "conservation_gate_passed": float(conservation_passed),
                }
                diagnostics.update(
                    {
                        f"{name}_yield_mass_fraction": (
                            products[name].mass_flow_kg_s / self.case.feed.mass_flow_kg_s
                        )
                        for name in MAIN_PRODUCT_NAMES
                    }
                )
                last_valid = SteadyFlowsheetResult(
                    status=(
                        "success"
                        if fixed_point_converged and conservation_passed
                        else (
                            "failed"
                            if fixed_point_converged
                            else "not_converged"
                        )
                    ),
                    streams=streams,
                    products=products,
                    unit_results=unit_results,
                    qualities={
                        name: quality_proxies(products[name], self.catalog)
                        for name in MAIN_PRODUCT_NAMES
                    },
                    balance=balance,
                    diagnostics=diagnostics,
                    versions={
                        **{
                            name: value
                            for name, value in versions.as_dict().items()
                            if value is not None
                        },
                        "simulation_stage": "M2",
                    },
                    input_fingerprint=fingerprint,
                    warnings=(
                        *base.warnings,
                        "Pump-around heat recovery is an equivalent internal-duty model.",
                        "Configured recovered heat is capped by the existing pre-furnace preheater target; it is not added beyond that target.",
                        "The column duty remains the total net tower duty, so pump-around removal is reported but not subtracted a second time.",
                    ),
                )
                last_valid_reflux = reflux
                last_valid_history = tuple(residual_history)
                last_valid_iteration = iteration
                if fixed_point_converged and not conservation_passed:
                    return RecycleSolveResult(
                        status="failed",
                        flowsheet=last_valid,
                        iterations=iteration,
                        final_residual=residual,
                        residual_history=tuple(residual_history),
                        reflux=reflux,
                        failure_reason=(
                            "fixed point converged but a required conservation boundary failed"
                        ),
                        failure_stage="conservation",
                    )
                if fixed_point_converged:
                    return RecycleSolveResult(
                        status="success",
                        flowsheet=last_valid,
                        iterations=iteration,
                        final_residual=residual,
                        residual_history=tuple(residual_history),
                        reflux=reflux,
                    )
                if iteration < settings.maximum_iterations:
                    reflux = _relax_reflux(
                        reflux,
                        target_reflux,
                        settings.relaxation_factor,
                    )
            except (ArithmeticError, ValueError) as exc:
                failed_flowsheet = (
                    None
                    if last_valid is None
                    else replace(
                        last_valid,
                        status="failed",
                        warnings=(*last_valid.warnings, f"Recycle iteration failed: {exc}"),
                    )
                )
                return RecycleSolveResult(
                    status="failed",
                    flowsheet=failed_flowsheet,
                    iterations=(
                        iteration if last_valid is None else last_valid_iteration
                    ),
                    final_residual=(
                        None
                        if last_valid is None or not last_valid_history
                        else last_valid_history[-1]
                    ),
                    residual_history=(
                        tuple(residual_history)
                        if last_valid is None
                        else last_valid_history
                    ),
                    reflux=last_valid_reflux,
                    failure_reason=str(exc),
                    failure_stage="iteration",
                )

        nonconverged = (
            None
            if last_valid is None
            else replace(
                last_valid,
                status="not_converged",
                warnings=(
                    *last_valid.warnings,
                    "Recycle solver reached the maximum iteration count.",
                ),
            )
        )
        return RecycleSolveResult(
            status="not_converged",
            flowsheet=nonconverged,
            iterations=(
                settings.maximum_iterations
                if last_valid is None
                else last_valid_iteration
            ),
            final_residual=(
                None
                if last_valid is None or not last_valid_history
                else last_valid_history[-1]
            ),
            residual_history=(
                tuple(residual_history)
                if last_valid is None
                else last_valid_history
            ),
            reflux=last_valid_reflux,
            failure_reason="maximum iteration count reached before recycle convergence",
            failure_stage="convergence",
        )


def solve_recycle(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    *,
    settings: RecycleSettings | None = None,
    initial_reflux: MaterialStream | None = None,
    software_version: str = SOFTWARE_VERSION,
) -> RecycleSolveResult:
    """Convenience entry point for one deterministic M2 recycle solve."""

    try:
        solver = RecycleCDU(
            model,
            case,
            catalog,
            settings=settings,
            software_version=software_version,
        )
    except (TypeError, ValueError) as exc:
        return RecycleSolveResult(
            status="rejected",
            flowsheet=None,
            iterations=0,
            final_residual=None,
            residual_history=(),
            reflux=None,
            failure_reason=str(exc),
            failure_stage="configuration",
        )
    return solver.solve(initial_reflux=initial_reflux)
