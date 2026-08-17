"""Reusable material-balance construction for declared process boundaries."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from ..properties.components import ALL_COMPONENTS
from .types import BalanceReport, MaterialStream


def material_balance(
    inlets: Iterable[MaterialStream],
    outlets: Iterable[MaterialStream],
    *,
    component_accumulation_kg_s: Mapping[str, float] | None = None,
    salt_accumulation_kg_s: float = 0.0,
    energy_residual_w: float | None = None,
) -> BalanceReport:
    """Build total, component and salt residuals for one boundary."""

    inlet_streams = tuple(inlets)
    outlet_streams = tuple(outlets)
    accumulation = (
        {} if component_accumulation_kg_s is None else dict(component_accumulation_kg_s)
    )
    unknown = sorted(set(accumulation) - set(ALL_COMPONENTS))
    if unknown:
        raise ValueError(f"unknown accumulation components: {', '.join(unknown)}")
    if any(not math.isfinite(value) for value in accumulation.values()):
        raise ValueError("component accumulation values must be finite")
    if not math.isfinite(salt_accumulation_kg_s):
        raise ValueError("salt accumulation must be finite")
    component_residuals = {
        component: (
            sum(stream.component_flow_kg_s(component) for stream in inlet_streams)
            - sum(stream.component_flow_kg_s(component) for stream in outlet_streams)
            - accumulation.get(component, 0.0)
        )
        for component in ALL_COMPONENTS
    }
    return BalanceReport(
        inlet_kg_s=sum(stream.mass_flow_kg_s for stream in inlet_streams),
        outlet_kg_s=sum(stream.mass_flow_kg_s for stream in outlet_streams),
        accumulation_kg_s=sum(accumulation.values()),
        component_residuals_kg_s=component_residuals,
        salt_residual_kg_s=(
            sum(stream.salt_mass_flow_kg_s for stream in inlet_streams)
            - sum(stream.salt_mass_flow_kg_s for stream in outlet_streams)
            - salt_accumulation_kg_s
        ),
        energy_residual_w=energy_residual_w,
    )
