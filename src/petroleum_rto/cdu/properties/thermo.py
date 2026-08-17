"""Reduced thermophysical relations for the first CDU model."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..core.math_utils import clamp
from ..core.types import MaterialStream, stream_from_component_flows
from .components import ALL_COMPONENTS, HYDROCARBON_COMPONENTS, ComponentCatalog

REFERENCE_TEMPERATURE_K = 273.15
REFERENCE_PRESSURE_PA = 101325.0
GAS_CONSTANT_J_MOL_K = 8.314462618


@dataclass(frozen=True)
class ReducedThermo:
    """Constant-heat-capacity and ideal low-pressure property approximation."""

    catalog: ComponentCatalog

    def _validate_composition(self, composition: Mapping[str, float]) -> None:
        unknown = sorted(set(composition) - set(ALL_COMPONENTS))
        if unknown:
            raise ValueError(f"unknown components: {', '.join(unknown)}")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0.0
            for value in composition.values()
        ):
            raise ValueError("composition values must be finite and non-negative")
        if not math.isclose(sum(composition.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("composition must sum to one")

    def mixture_cp_liquid(self, composition: Mapping[str, float]) -> float:
        self._validate_composition(composition)
        return sum(
            fraction * self.catalog.components[name].cp_liquid_j_kg_k
            for name, fraction in composition.items()
        )

    def liquid_specific_enthalpy(
        self,
        composition: Mapping[str, float],
        temperature_k: float,
    ) -> float:
        if not math.isfinite(temperature_k) or temperature_k <= 0.0:
            raise ValueError("temperature must be finite and positive")
        return self.mixture_cp_liquid(composition) * (
            temperature_k - REFERENCE_TEMPERATURE_K
        )

    def vapor_specific_enthalpy(
        self,
        composition: Mapping[str, float],
        temperature_k: float,
    ) -> float:
        self._validate_composition(composition)
        if not math.isfinite(temperature_k) or temperature_k <= 0.0:
            raise ValueError("temperature must be finite and positive")
        enthalpy = 0.0
        for name, fraction in composition.items():
            component = self.catalog.components[name]
            sensible_to_boil = component.cp_liquid_j_kg_k * (
                component.normal_boiling_point_k - REFERENCE_TEMPERATURE_K
            )
            vapor_sensible = component.cp_vapor_j_kg_k * (
                temperature_k - component.normal_boiling_point_k
            )
            enthalpy += fraction * (
                sensible_to_boil + component.latent_heat_j_kg + vapor_sensible
            )
        return enthalpy

    def stream_enthalpy_w(self, stream: MaterialStream, *, phase: str = "liquid") -> float:
        if phase == "liquid":
            specific = self.liquid_specific_enthalpy(
                stream.mass_fractions,
                stream.temperature_k,
            )
        elif phase == "vapor":
            specific = self.vapor_specific_enthalpy(
                stream.mass_fractions,
                stream.temperature_k,
            )
        else:
            raise ValueError(f"unsupported phase: {phase!r}")
        return stream.mass_flow_kg_s * specific

    def temperature_from_liquid_enthalpy(
        self,
        composition: Mapping[str, float],
        specific_enthalpy_j_kg: float,
    ) -> float:
        if not math.isfinite(specific_enthalpy_j_kg):
            raise ValueError("specific enthalpy must be finite")
        temperature = (
            REFERENCE_TEMPERATURE_K
            + specific_enthalpy_j_kg / self.mixture_cp_liquid(composition)
        )
        if temperature <= 0.0:
            raise ValueError("enthalpy implies a non-positive absolute temperature")
        return temperature

    def mix_by_enthalpy(
        self,
        name: str,
        streams: Iterable[MaterialStream],
        *,
        pressure_pa: float | None = None,
    ) -> MaterialStream:
        """Mix liquid streams while conserving reduced-model sensible enthalpy."""

        stream_values = tuple(streams)
        if not stream_values:
            raise ValueError("at least one stream is required")
        total_flow = sum(stream.mass_flow_kg_s for stream in stream_values)
        if total_flow <= 0.0:
            raise ValueError("combined stream flow must be positive")
        minimum_inlet_pressure = min(stream.pressure_pa for stream in stream_values)
        if pressure_pa is not None and pressure_pa > minimum_inlet_pressure:
            raise ValueError("a passive mixer cannot raise pressure above an inlet pressure")
        component_flows = {
            component: sum(
                stream.component_flow_kg_s(component) for stream in stream_values
            )
            for component in ALL_COMPONENTS
        }
        composition = {
            component: flow / total_flow
            for component, flow in component_flows.items()
            if flow > 0.0
        }
        total_enthalpy = sum(self.stream_enthalpy_w(stream) for stream in stream_values)
        temperature_k = self.temperature_from_liquid_enthalpy(
            composition,
            total_enthalpy / total_flow,
        )
        return stream_from_component_flows(
            name,
            component_flows,
            temperature_k=temperature_k,
            pressure_pa=(
                minimum_inlet_pressure
                if pressure_pa is None
                else pressure_pa
            ),
            salt_mass_flow_kg_s=sum(
                stream.salt_mass_flow_kg_s for stream in stream_values
            ),
        )

    def hydrocarbon_mole_fractions(
        self,
        mass_fractions: Mapping[str, float],
    ) -> dict[str, float]:
        self._validate_composition(mass_fractions)
        mole_flows = {
            name: mass_fractions.get(name, 0.0)
            / self.catalog.components[name].molecular_weight_kg_mol
            for name in HYDROCARBON_COMPONENTS
        }
        total = sum(mole_flows.values())
        if total <= 0.0:
            return {name: 0.0 for name in HYDROCARBON_COMPONENTS}
        return {name: value / total for name, value in mole_flows.items()}

    def vapor_pressure_pa(self, component_name: str, temperature_k: float) -> float:
        if component_name not in HYDROCARBON_COMPONENTS:
            raise ValueError("vapor-pressure relation is only defined for hydrocarbons")
        if not math.isfinite(temperature_k) or temperature_k <= 0.0:
            raise ValueError("temperature must be finite and positive")
        component = self.catalog.components[component_name]
        latent_heat_j_mol = (
            component.latent_heat_j_kg * component.molecular_weight_kg_mol
        )
        exponent = -latent_heat_j_mol / GAS_CONSTANT_J_MOL_K * (
            1.0 / temperature_k - 1.0 / component.normal_boiling_point_k
        )
        return REFERENCE_PRESSURE_PA * math.exp(clamp(exponent, -80.0, 80.0))

    def saturation_temperature_k(self, component_name: str, pressure_pa: float) -> float:
        """Invert the reduced Clausius-Clapeyron relation at a given pressure."""

        if component_name not in HYDROCARBON_COMPONENTS:
            raise ValueError("saturation relation is only defined for hydrocarbons")
        if not math.isfinite(pressure_pa) or pressure_pa <= 0.0:
            raise ValueError("pressure must be finite and positive")
        component = self.catalog.components[component_name]
        latent_heat_j_mol = (
            component.latent_heat_j_kg * component.molecular_weight_kg_mol
        )
        inverse_temperature = (
            1.0 / component.normal_boiling_point_k
            - GAS_CONSTANT_J_MOL_K
            / latent_heat_j_mol
            * math.log(pressure_pa / REFERENCE_PRESSURE_PA)
        )
        if inverse_temperature <= 0.0:
            raise ValueError("pressure is outside the reduced saturation relation")
        return 1.0 / inverse_temperature

    def k_values(
        self,
        temperature_k: float,
        pressure_pa: float,
    ) -> dict[str, float]:
        if not math.isfinite(pressure_pa) or pressure_pa <= 0.0:
            raise ValueError("pressure must be finite and positive")
        return {
            name: self.vapor_pressure_pa(name, temperature_k) / pressure_pa
            for name in HYDROCARBON_COMPONENTS
        }
