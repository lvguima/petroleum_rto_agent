"""Reduced desalting, flash, fractionation and overhead-condensation models."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

from ..core.conservation import material_balance
from ..core.math_utils import bisect_root, logistic
from ..core.types import MaterialStream, UnitResult, stream_from_component_flows
from ..properties.components import ALL_COMPONENTS, HYDROCARBON_COMPONENTS
from ..properties.thermo import ReducedThermo

PRODUCT_NAMES = ("overhead", "kerosene", "light_diesel", "heavy_diesel", "residue")


def _validate_fraction(value: float, *, name: str, upper: float = 1.0) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= upper:
        raise ValueError(f"{name} must be between zero and {upper}")


def _stream_allow_zero(
    name: str,
    component_flows_kg_s: Mapping[str, float],
    *,
    temperature_k: float,
    pressure_pa: float,
    salt_mass_flow_kg_s: float = 0.0,
    fallback_component: str,
) -> MaterialStream:
    total = sum(component_flows_kg_s.values())
    if total > 0.0:
        return stream_from_component_flows(
            name,
            component_flows_kg_s,
            temperature_k=temperature_k,
            pressure_pa=pressure_pa,
            salt_mass_flow_kg_s=salt_mass_flow_kg_s,
        )
    return MaterialStream(
        name,
        0.0,
        temperature_k,
        pressure_pa,
        {fallback_component: 1.0},
        salt_mass_flow_kg_s=salt_mass_flow_kg_s,
    )


def _hydrocarbon_phase_split(
    component_flows_kg_s: Mapping[str, float],
    *,
    temperature_k: float,
    pressure_pa: float,
    thermo: ReducedThermo,
) -> tuple[dict[str, float], dict[str, float], float]:
    mole_flows = {
        name: component_flows_kg_s.get(name, 0.0)
        / thermo.catalog.components[name].molecular_weight_kg_mol
        for name in HYDROCARBON_COMPONENTS
    }
    total_moles = sum(mole_flows.values())
    if total_moles <= 0.0:
        empty = {name: 0.0 for name in HYDROCARBON_COMPONENTS}
        return empty.copy(), empty.copy(), 0.0
    z = {name: value / total_moles for name, value in mole_flows.items()}
    k_values = thermo.k_values(temperature_k, pressure_pa)

    def rachford_rice(beta: float) -> float:
        return sum(
            z[name] * (k_values[name] - 1.0)
            / (1.0 + beta * (k_values[name] - 1.0))
            for name in HYDROCARBON_COMPONENTS
        )

    f_zero = rachford_rice(0.0)
    f_one = rachford_rice(1.0)
    if f_zero <= 0.0:
        beta = 0.0
    elif f_one >= 0.0:
        beta = 1.0
    else:
        beta = bisect_root(rachford_rice, 0.0, 1.0, tolerance=1e-12)

    liquid_mass: dict[str, float] = {}
    vapor_mass: dict[str, float] = {}
    for name in HYDROCARBON_COMPONENTS:
        denominator = 1.0 + beta * (k_values[name] - 1.0)
        liquid_moles = (1.0 - beta) * total_moles * z[name] / denominator
        vapor_moles = beta * total_moles * k_values[name] * z[name] / denominator
        molecular_weight = thermo.catalog.components[name].molecular_weight_kg_mol
        liquid_mass[name] = max(liquid_moles * molecular_weight, 0.0)
        vapor_mass[name] = max(vapor_moles * molecular_weight, 0.0)
    return liquid_mass, vapor_mass, beta


@dataclass(frozen=True)
class Desalter:
    """Equivalent desalting block with explicit brine and entrained-oil outlet."""

    thermo: ReducedThermo
    water_removal_efficiency: float
    salt_removal_efficiency: float
    oil_entrainment_fraction: float
    pressure_drop_pa: float = 0.0

    def __post_init__(self) -> None:
        _validate_fraction(
            self.water_removal_efficiency,
            name="water_removal_efficiency",
        )
        _validate_fraction(self.salt_removal_efficiency, name="salt_removal_efficiency")
        _validate_fraction(
            self.oil_entrainment_fraction,
            name="oil_entrainment_fraction",
            upper=0.1,
        )
        if not math.isfinite(self.pressure_drop_pa) or self.pressure_drop_pa < 0.0:
            raise ValueError("desalter pressure drop must be finite and non-negative")

    def solve(self, crude: MaterialStream, wash_water: MaterialStream) -> UnitResult:
        if wash_water.mass_fractions.get("water", 0.0) < 1.0 - 1e-9:
            raise ValueError("wash-water stream must contain only water")
        mixed = self.thermo.mix_by_enthalpy(
            "desalter_mixed_feed",
            [crude, wash_water],
        )
        outlet_pressure = min(crude.pressure_pa, wash_water.pressure_pa) - self.pressure_drop_pa
        if outlet_pressure <= 0.0:
            raise ValueError("desalter pressure drop produces non-positive outlet pressure")
        inlet_component_flows = {
            component: crude.component_flow_kg_s(component)
            + wash_water.component_flow_kg_s(component)
            for component in ALL_COMPONENTS
        }
        hydrocarbon_total = sum(
            inlet_component_flows[name] for name in HYDROCARBON_COMPONENTS
        )
        oil_to_brine = self.oil_entrainment_fraction * hydrocarbon_total
        brine_components: dict[str, float] = {}
        crude_components: dict[str, float] = {}
        for name in HYDROCARBON_COMPONENTS:
            inlet_flow = inlet_component_flows[name]
            entrained = (
                0.0
                if hydrocarbon_total == 0.0
                else oil_to_brine * inlet_flow / hydrocarbon_total
            )
            brine_components[name] = entrained
            crude_components[name] = inlet_flow - entrained
        total_water = inlet_component_flows["water"]
        brine_components["water"] = self.water_removal_efficiency * total_water
        crude_components["water"] = total_water - brine_components["water"]
        salt_to_brine = self.salt_removal_efficiency * (
            crude.salt_mass_flow_kg_s + wash_water.salt_mass_flow_kg_s
        )
        salt_to_crude = (
            crude.salt_mass_flow_kg_s
            + wash_water.salt_mass_flow_kg_s
            - salt_to_brine
        )
        desalted = _stream_allow_zero(
            "desalted_crude",
            crude_components,
            temperature_k=mixed.temperature_k,
            pressure_pa=outlet_pressure,
            salt_mass_flow_kg_s=salt_to_crude,
            fallback_component="residue",
        )
        brine = _stream_allow_zero(
            "brine",
            brine_components,
            temperature_k=mixed.temperature_k,
            pressure_pa=outlet_pressure,
            salt_mass_flow_kg_s=salt_to_brine,
            fallback_component="water",
        )
        inlet_enthalpy = self.thermo.stream_enthalpy_w(crude) + self.thermo.stream_enthalpy_w(
            wash_water
        )
        outlet_enthalpy = self.thermo.stream_enthalpy_w(
            desalted
        ) + self.thermo.stream_enthalpy_w(brine)
        balance = material_balance(
            [crude, wash_water],
            [desalted, brine],
            energy_residual_w=inlet_enthalpy - outlet_enthalpy,
        )
        return UnitResult(
            outlets={"desalted_crude": desalted, "brine": brine},
            diagnostics={
                "water_removal_efficiency": self.water_removal_efficiency,
                "salt_removal_efficiency": self.salt_removal_efficiency,
                "oil_entrainment_kg_s": oil_to_brine,
            },
            balance=balance,
        )


@dataclass(frozen=True)
class IsothermalFlash:
    """Low-pressure ideal K-value flash with water retained in the liquid phase."""

    thermo: ReducedThermo
    temperature_k: float
    pressure_pa: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.temperature_k) or self.temperature_k <= 0.0:
            raise ValueError("flash temperature must be finite and positive")
        if not math.isfinite(self.pressure_pa) or self.pressure_pa <= 0.0:
            raise ValueError("flash pressure must be finite and positive")

    def solve(self, feed: MaterialStream) -> UnitResult:
        if self.pressure_pa > feed.pressure_pa:
            raise ValueError("flash pressure cannot exceed feed pressure without a pump")
        feed_component_flows = {
            component: feed.component_flow_kg_s(component) for component in ALL_COMPONENTS
        }
        liquid_hc, vapor_hc, beta = _hydrocarbon_phase_split(
            feed_component_flows,
            temperature_k=self.temperature_k,
            pressure_pa=self.pressure_pa,
            thermo=self.thermo,
        )
        liquid_components = dict(liquid_hc)
        liquid_components["water"] = feed_component_flows["water"]
        vapor_components = dict(vapor_hc)
        vapor = _stream_allow_zero(
            "flash_vapor",
            vapor_components,
            temperature_k=self.temperature_k,
            pressure_pa=self.pressure_pa,
            fallback_component="light_ends",
        )
        liquid = _stream_allow_zero(
            "flash_liquid",
            liquid_components,
            temperature_k=self.temperature_k,
            pressure_pa=self.pressure_pa,
            salt_mass_flow_kg_s=feed.salt_mass_flow_kg_s,
            fallback_component="residue",
        )
        inlet_enthalpy = self.thermo.stream_enthalpy_w(feed)
        outlet_enthalpy = self.thermo.stream_enthalpy_w(
            vapor, phase="vapor"
        ) + self.thermo.stream_enthalpy_w(liquid)
        duty = outlet_enthalpy - inlet_enthalpy
        balance = material_balance(
            [feed],
            [vapor, liquid],
            energy_residual_w=inlet_enthalpy + duty - outlet_enthalpy,
        )
        return UnitResult(
            outlets={"vapor": vapor, "liquid": liquid},
            duty_w=duty,
            diagnostics={
                "hydrocarbon_molar_vapor_fraction": beta,
                "mass_vapor_fraction": vapor.mass_flow_kg_s / max(feed.mass_flow_kg_s, 1e-12),
            },
            balance=balance,
        )


@dataclass(frozen=True)
class ColumnResult:
    """Fractionation result with the exact component split matrix."""

    unit_result: UnitResult
    split_matrix: Mapping[str, Mapping[str, float]]

    def __post_init__(self) -> None:
        frozen = {
            product: MappingProxyType(dict(values))
            for product, values in self.split_matrix.items()
        }
        object.__setattr__(self, "split_matrix", MappingProxyType(frozen))


@dataclass(frozen=True)
class ReducedColumn:
    """Conservative five-product smooth-cut atmospheric column."""

    thermo: ReducedThermo
    pressure_pa: float
    cut_points_k: tuple[float, float, float, float]
    separation_widths_k: tuple[float, float, float, float]
    product_temperatures_k: Mapping[str, float]

    def __post_init__(self) -> None:
        if not math.isfinite(self.pressure_pa) or self.pressure_pa <= 0.0:
            raise ValueError("column pressure must be finite and positive")
        if (
            len(self.cut_points_k) != 4
            or any(not math.isfinite(value) or value <= 0.0 for value in self.cut_points_k)
            or any(
            left >= right for left, right in pairwise(self.cut_points_k)
            )
        ):
            raise ValueError("column cut points must contain four increasing values")
        if len(self.separation_widths_k) != 4 or any(
            not math.isfinite(value) or value <= 0.0
            for value in self.separation_widths_k
        ):
            raise ValueError("column separation widths must contain four positive values")
        if set(self.product_temperatures_k) != set(PRODUCT_NAMES):
            raise ValueError("column product temperatures must cover all products")
        if any(
            not math.isfinite(value) or value <= 0.0
            for value in self.product_temperatures_k.values()
        ):
            raise ValueError("column product temperatures must be finite and positive")
        self._split_matrix(1.0)

    def _split_matrix(self, width_scale: float) -> dict[str, dict[str, float]]:
        if not math.isfinite(width_scale) or width_scale <= 0.0:
            raise ValueError("separation width scale must be finite and positive")
        matrix: dict[str, dict[str, float]] = {
            product: {} for product in PRODUCT_NAMES
        }
        for component_name in HYDROCARBON_COMPONENTS:
            boiling_point = self.thermo.catalog.components[
                component_name
            ].normal_boiling_point_k
            cumulative = [
                logistic(
                    (cut_point - boiling_point) / (width * width_scale)
                )
                for cut_point, width in zip(
                    self.cut_points_k,
                    self.separation_widths_k,
                )
            ]
            probabilities = (
                cumulative[0],
                cumulative[1] - cumulative[0],
                cumulative[2] - cumulative[1],
                cumulative[3] - cumulative[2],
                1.0 - cumulative[3],
            )
            if any(value < -1e-12 for value in probabilities):
                raise ValueError("column cut settings produced a negative split")
            corrected = [max(value, 0.0) for value in probabilities]
            total = sum(corrected)
            for product, probability in zip(PRODUCT_NAMES, corrected):
                matrix[product][component_name] = probability / total
        for product in PRODUCT_NAMES:
            matrix[product]["water"] = 1.0 if product == "overhead" else 0.0
        return matrix

    def solve(
        self,
        hot_liquid: MaterialStream,
        flash_vapor: MaterialStream,
        *,
        reflux: MaterialStream | None = None,
        separation_width_scale: float = 1.0,
    ) -> ColumnResult:
        inlets = [hot_liquid, flash_vapor]
        if reflux is not None:
            inlets.append(reflux)
        total_inlet_flow = sum(stream.mass_flow_kg_s for stream in inlets)
        if total_inlet_flow <= 0.0:
            raise ValueError("column requires a positive total inlet flow")
        if self.pressure_pa > min(stream.pressure_pa for stream in inlets):
            raise ValueError("column pressure cannot exceed the lowest feed pressure")
        matrix = self._split_matrix(separation_width_scale)
        total_component_flows = {
            component: sum(stream.component_flow_kg_s(component) for stream in inlets)
            for component in ALL_COMPONENTS
        }
        product_streams: dict[str, MaterialStream] = {}
        for product in PRODUCT_NAMES:
            component_flows = {
                component: total_component_flows[component] * matrix[product][component]
                for component in ALL_COMPONENTS
            }
            product_streams[product] = _stream_allow_zero(
                product,
                component_flows,
                temperature_k=self.product_temperatures_k[product],
                pressure_pa=self.pressure_pa,
                salt_mass_flow_kg_s=(
                    sum(stream.salt_mass_flow_kg_s for stream in inlets)
                    if product == "residue"
                    else 0.0
                ),
                fallback_component=("light_ends" if product == "overhead" else "residue"),
            )
        inlet_enthalpy = (
            self.thermo.stream_enthalpy_w(hot_liquid)
            + self.thermo.stream_enthalpy_w(flash_vapor, phase="vapor")
            + (
                0.0
                if reflux is None
                else self.thermo.stream_enthalpy_w(reflux)
            )
        )
        outlet_enthalpy = self.thermo.stream_enthalpy_w(
            product_streams["overhead"],
            phase="vapor",
        ) + sum(
            self.thermo.stream_enthalpy_w(product_streams[name])
            for name in PRODUCT_NAMES
            if name != "overhead"
        )
        duty = outlet_enthalpy - inlet_enthalpy
        balance = material_balance(
            inlets,
            product_streams.values(),
            energy_residual_w=inlet_enthalpy + duty - outlet_enthalpy,
        )
        return ColumnResult(
            unit_result=UnitResult(
                outlets=product_streams,
                duty_w=duty,
                diagnostics={
                    "separation_width_scale": separation_width_scale,
                    "overhead_mass_fraction": (
                        product_streams["overhead"].mass_flow_kg_s
                        / total_inlet_flow
                    ),
                },
                balance=balance,
            ),
            split_matrix=matrix,
        )


@dataclass(frozen=True)
class OverheadCondenser:
    """Smooth reduced condenser with explicit gas, oil and water outlets."""

    thermo: ReducedThermo
    temperature_k: float
    pressure_pa: float
    condensation_width_k: float = 18.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.temperature_k) or self.temperature_k <= 0.0:
            raise ValueError("condenser temperature must be finite and positive")
        if not math.isfinite(self.pressure_pa) or self.pressure_pa <= 0.0:
            raise ValueError("condenser pressure must be finite and positive")
        if not math.isfinite(self.condensation_width_k) or self.condensation_width_k <= 0.0:
            raise ValueError("condensation width must be finite and positive")

    def solve(self, overhead_vapor: MaterialStream) -> UnitResult:
        if overhead_vapor.salt_mass_flow_kg_s > 1e-15:
            raise ValueError("overhead vapor cannot carry the salt tracer")
        if self.temperature_k > overhead_vapor.temperature_k:
            raise ValueError("condenser temperature cannot exceed inlet temperature")
        if self.pressure_pa > overhead_vapor.pressure_pa:
            raise ValueError("condenser cannot raise pressure without a compressor")
        feed_component_flows = {
            component: overhead_vapor.component_flow_kg_s(component)
            for component in ALL_COMPONENTS
        }
        liquid_hc: dict[str, float] = {}
        vapor_hc: dict[str, float] = {}
        feed_hydrocarbon_moles = 0.0
        vapor_hydrocarbon_moles = 0.0
        for name in HYDROCARBON_COMPONENTS:
            saturation_temperature = self.thermo.saturation_temperature_k(
                name,
                self.pressure_pa,
            )
            vapor_fraction = logistic(
                (self.temperature_k - saturation_temperature)
                / self.condensation_width_k
            )
            feed_flow = feed_component_flows[name]
            vapor_hc[name] = feed_flow * vapor_fraction
            liquid_hc[name] = feed_flow - vapor_hc[name]
            molecular_weight = self.thermo.catalog.components[name].molecular_weight_kg_mol
            feed_hydrocarbon_moles += feed_flow / molecular_weight
            vapor_hydrocarbon_moles += vapor_hc[name] / molecular_weight
        beta = vapor_hydrocarbon_moles / max(feed_hydrocarbon_moles, 1e-30)
        offgas = _stream_allow_zero(
            "offgas",
            vapor_hc,
            temperature_k=self.temperature_k,
            pressure_pa=self.pressure_pa,
            fallback_component="light_ends",
        )
        oil = _stream_allow_zero(
            "oil_condensate",
            liquid_hc,
            temperature_k=self.temperature_k,
            pressure_pa=self.pressure_pa,
            fallback_component="naphtha",
        )
        aqueous = _stream_allow_zero(
            "aqueous",
            {"water": feed_component_flows["water"]},
            temperature_k=self.temperature_k,
            pressure_pa=self.pressure_pa,
            fallback_component="water",
        )
        inlet_enthalpy = self.thermo.stream_enthalpy_w(overhead_vapor, phase="vapor")
        outlet_enthalpy = (
            self.thermo.stream_enthalpy_w(offgas, phase="vapor")
            + self.thermo.stream_enthalpy_w(oil)
            + self.thermo.stream_enthalpy_w(aqueous)
        )
        duty = outlet_enthalpy - inlet_enthalpy
        balance = material_balance(
            [overhead_vapor],
            [offgas, oil, aqueous],
            energy_residual_w=inlet_enthalpy + duty - outlet_enthalpy,
        )
        return UnitResult(
            outlets={"offgas": offgas, "oil_condensate": oil, "aqueous": aqueous},
            duty_w=duty,
            diagnostics={
                "hydrocarbon_molar_vapor_fraction": beta,
                "hydrocarbon_condensed_kg_s": oil.mass_flow_kg_s,
                "condensation_width_k": self.condensation_width_k,
            },
            balance=balance,
        )
