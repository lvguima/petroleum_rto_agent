"""Directional product-quality proxies for the six-component model."""

from __future__ import annotations

import math

from ..core.types import MaterialStream
from ..properties.components import HYDROCARBON_COMPONENTS, ComponentCatalog


def _boiling_quantile_k(
    stream: MaterialStream,
    catalog: ComponentCatalog,
    quantile: float,
) -> float:
    hydrocarbon_fractions = {
        name: stream.mass_fractions.get(name, 0.0) for name in HYDROCARBON_COMPONENTS
    }
    total = sum(hydrocarbon_fractions.values())
    if total <= 0.0:
        raise ValueError("quality proxy requires hydrocarbon content")
    cumulative = 0.0
    previous_bp = catalog.components[HYDROCARBON_COMPONENTS[0]].normal_boiling_point_k
    previous_cumulative = 0.0
    for name in HYDROCARBON_COMPONENTS:
        boiling_point = catalog.components[name].normal_boiling_point_k
        fraction = hydrocarbon_fractions[name] / total
        cumulative += fraction
        if cumulative >= quantile:
            if cumulative == previous_cumulative:
                return boiling_point
            interpolation = (quantile - previous_cumulative) / (
                cumulative - previous_cumulative
            )
            return previous_bp + interpolation * (boiling_point - previous_bp)
        previous_bp = boiling_point
        previous_cumulative = cumulative
    return catalog.components[HYDROCARBON_COMPONENTS[-1]].normal_boiling_point_k


def quality_proxies(
    stream: MaterialStream,
    catalog: ComponentCatalog,
) -> dict[str, float]:
    """Return explicitly approximate density and boiling/flash-point indicators."""

    hydrocarbon_total = sum(
        stream.mass_fractions.get(name, 0.0) for name in HYDROCARBON_COMPONENTS
    )
    if hydrocarbon_total <= 0.0:
        raise ValueError("quality proxy requires hydrocarbon content")
    inverse_density = sum(
        (stream.mass_fractions.get(name, 0.0) / hydrocarbon_total)
        / catalog.components[name].liquid_density_kg_m3
        for name in HYDROCARBON_COMPONENTS
    )
    density = 1.0 / inverse_density
    t10 = _boiling_quantile_k(stream, catalog, 0.10)
    t50 = _boiling_quantile_k(stream, catalog, 0.50)
    t90 = _boiling_quantile_k(stream, catalog, 0.90)
    light_fraction = (
        stream.mass_fractions.get("light_ends", 0.0)
        + stream.mass_fractions.get("naphtha", 0.0)
    ) / hydrocarbon_total
    flash_point_proxy_k = max(150.0, 0.35 * t10 + 0.65 * t50 - 45.0 * light_fraction)
    maximum_boiling_point = catalog.components[
        HYDROCARBON_COMPONENTS[-1]
    ].normal_boiling_point_k
    values = {
        "density_kg_m3_proxy": density,
        "t10_k_proxy": t10,
        "t50_k_proxy": t50,
        "t90_k_proxy": t90,
        "dry_point_k_proxy": min(t90 + 0.25 * (t90 - t50), maximum_boiling_point),
        "flash_point_k_proxy": flash_point_proxy_k,
    }
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("quality proxy produced a non-finite value")
    return values
