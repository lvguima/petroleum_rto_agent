"""Reduced sensible-heating and fired-heater equipment models."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.conservation import material_balance
from ..core.types import MaterialStream, UnitResult
from ..properties.thermo import ReducedThermo


def _outlet_pressure(feed: MaterialStream, pressure_drop_pa: float) -> float:
    pressure = feed.pressure_pa - pressure_drop_pa
    if pressure <= 0.0:
        raise ValueError("pressure drop produces a non-positive outlet pressure")
    return pressure


@dataclass(frozen=True)
class EquivalentPreheater:
    """Equivalent heat-recovery section with a bounded target temperature."""

    thermo: ReducedThermo
    effectiveness: float
    target_temperature_k: float
    pressure_drop_pa: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.effectiveness) or not 0.0 <= self.effectiveness <= 1.0:
            raise ValueError("preheater effectiveness must be between zero and one")
        if not math.isfinite(self.target_temperature_k) or self.target_temperature_k <= 0.0:
            raise ValueError("preheater target temperature must be finite and positive")
        if not math.isfinite(self.pressure_drop_pa) or self.pressure_drop_pa < 0.0:
            raise ValueError("preheater pressure drop must be finite and non-negative")

    def solve(
        self,
        feed: MaterialStream,
        *,
        available_duty_w: float | None = None,
        outlet_name: str = "heated",
    ) -> UnitResult:
        if available_duty_w is not None and (
            not math.isfinite(available_duty_w) or available_duty_w < 0.0
        ):
            raise ValueError("available preheater duty must be finite and non-negative")
        desired_temperature = feed.temperature_k + self.effectiveness * max(
            self.target_temperature_k - feed.temperature_k,
            0.0,
        )
        cp = self.thermo.mixture_cp_liquid(feed.mass_fractions)
        desired_duty = (
            feed.mass_flow_kg_s * cp * (desired_temperature - feed.temperature_k)
        )
        duty = desired_duty if available_duty_w is None else min(desired_duty, available_duty_w)
        if feed.mass_flow_kg_s == 0.0:
            outlet_temperature = feed.temperature_k
            duty = 0.0
        else:
            outlet_temperature = feed.temperature_k + duty / (feed.mass_flow_kg_s * cp)
        outlet = feed.at_conditions(
            name=outlet_name,
            temperature_k=outlet_temperature,
            pressure_pa=_outlet_pressure(feed, self.pressure_drop_pa),
        )
        energy_residual = (
            self.thermo.stream_enthalpy_w(feed)
            + duty
            - self.thermo.stream_enthalpy_w(outlet)
        )
        balance = material_balance([feed], [outlet], energy_residual_w=energy_residual)
        return UnitResult(
            outlets={outlet_name: outlet},
            duty_w=duty,
            diagnostics={
                "effectiveness": self.effectiveness,
                "desired_duty_w": desired_duty,
                "outlet_temperature_k": outlet_temperature,
            },
            balance=balance,
        )


@dataclass(frozen=True)
class Furnace:
    """Single-zone fired heater with one identifiable effective efficiency."""

    thermo: ReducedThermo
    efficiency: float
    heat_loss_w: float
    maximum_outlet_temperature_k: float
    pressure_drop_pa: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.efficiency) or not 0.0 < self.efficiency <= 1.0:
            raise ValueError("furnace efficiency must be in the interval (0, 1]")
        if not math.isfinite(self.heat_loss_w) or self.heat_loss_w < 0.0:
            raise ValueError("furnace heat loss must be finite and non-negative")
        if (
            not math.isfinite(self.maximum_outlet_temperature_k)
            or self.maximum_outlet_temperature_k <= 0.0
        ):
            raise ValueError("maximum furnace outlet temperature must be positive")
        if not math.isfinite(self.pressure_drop_pa) or self.pressure_drop_pa < 0.0:
            raise ValueError("furnace pressure drop must be finite and non-negative")

    def solve(
        self,
        feed: MaterialStream,
        *,
        fuel_duty_w: float | None = None,
        outlet_temperature_k: float | None = None,
        outlet_name: str = "furnace_outlet",
    ) -> UnitResult:
        if (fuel_duty_w is None) == (outlet_temperature_k is None):
            raise ValueError(
                "provide exactly one of fuel_duty_w or outlet_temperature_k"
            )
        cp = self.thermo.mixture_cp_liquid(feed.mass_fractions)
        if outlet_temperature_k is not None:
            if (
                not math.isfinite(outlet_temperature_k)
                or outlet_temperature_k < feed.temperature_k
                or outlet_temperature_k > self.maximum_outlet_temperature_k
            ):
                raise ValueError("requested furnace outlet temperature is outside bounds")
            process_duty = (
                feed.mass_flow_kg_s
                * cp
                * (outlet_temperature_k - feed.temperature_k)
            )
            required_fuel_duty = (process_duty + self.heat_loss_w) / self.efficiency
            actual_outlet_temperature = outlet_temperature_k
        else:
            if fuel_duty_w is None or not math.isfinite(fuel_duty_w) or fuel_duty_w < 0.0:
                raise ValueError("fuel duty must be finite and non-negative")
            required_fuel_duty = fuel_duty_w
            process_duty = self.efficiency * fuel_duty_w - self.heat_loss_w
            if process_duty < 0.0:
                raise ValueError("fuel duty is below the furnace heat-loss threshold")
            if feed.mass_flow_kg_s <= 0.0:
                raise ValueError("positive furnace feed flow is required in fuel-duty mode")
            actual_outlet_temperature = (
                feed.temperature_k + process_duty / (feed.mass_flow_kg_s * cp)
            )
            if (
                actual_outlet_temperature <= 0.0
                or actual_outlet_temperature > self.maximum_outlet_temperature_k
            ):
                raise ValueError("fuel duty implies an outlet temperature outside bounds")
        outlet = feed.at_conditions(
            name=outlet_name,
            temperature_k=actual_outlet_temperature,
            pressure_pa=_outlet_pressure(feed, self.pressure_drop_pa),
        )
        energy_residual = (
            self.thermo.stream_enthalpy_w(feed)
            + process_duty
            - self.thermo.stream_enthalpy_w(outlet)
        )
        balance = material_balance([feed], [outlet], energy_residual_w=energy_residual)
        return UnitResult(
            outlets={outlet_name: outlet},
            duty_w=process_duty,
            diagnostics={
                "fuel_duty_w": required_fuel_duty,
                "heat_loss_w": self.heat_loss_w,
                "efficiency": self.efficiency,
                "outlet_temperature_k": actual_outlet_temperature,
            },
            balance=balance,
        )
