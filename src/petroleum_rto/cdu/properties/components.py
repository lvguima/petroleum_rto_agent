"""Strict pseudo-component definitions for the reduced CDU model."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

HYDROCARBON_COMPONENTS: tuple[str, ...] = (
    "light_ends",
    "naphtha",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
)
ALL_COMPONENTS: tuple[str, ...] = (*HYDROCARBON_COMPONENTS, "water")
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _numeric_field(value: Mapping[str, object], key: str, *, component: str) -> float:
    raw = value[key]
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise TypeError(f"component {component!r} field {key!r} must be numeric")
    return float(raw)


@dataclass(frozen=True)
class PseudoComponent:
    """Minimal property set used by the reduced-order equipment models."""

    name: str
    normal_boiling_point_k: float
    molecular_weight_kg_mol: float
    liquid_density_kg_m3: float
    cp_liquid_j_kg_k: float
    cp_vapor_j_kg_k: float
    latent_heat_j_kg: float
    source: str
    confidence: str

    def __post_init__(self) -> None:
        numeric_values = (
            self.normal_boiling_point_k,
            self.molecular_weight_kg_mol,
            self.liquid_density_kg_m3,
            self.cp_liquid_j_kg_k,
            self.cp_vapor_j_kg_k,
            self.latent_heat_j_kg,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in numeric_values):
            raise ValueError(f"component {self.name!r} properties must be finite and positive")
        if not self.source.strip():
            raise ValueError(f"component {self.name!r} requires a source")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"component {self.name!r} has invalid confidence")

    @classmethod
    def from_mapping(cls, name: str, value: Mapping[str, object]) -> PseudoComponent:
        expected = {
            "normal_boiling_point_k",
            "molecular_weight_kg_mol",
            "liquid_density_kg_m3",
            "cp_liquid_j_kg_k",
            "cp_vapor_j_kg_k",
            "latent_heat_j_kg",
            "source",
            "confidence",
        }
        if set(value) != expected:
            missing = sorted(expected - set(value))
            unknown = sorted(set(value) - expected)
            raise ValueError(
                f"component {name!r} fields differ; missing={missing}, unknown={unknown}"
            )
        source = value["source"]
        confidence = value["confidence"]
        if not isinstance(source, str) or not isinstance(confidence, str):
            raise TypeError(f"component {name!r} source and confidence must be strings")
        return cls(
            name=name,
            normal_boiling_point_k=_numeric_field(
                value, "normal_boiling_point_k", component=name
            ),
            molecular_weight_kg_mol=_numeric_field(
                value, "molecular_weight_kg_mol", component=name
            ),
            liquid_density_kg_m3=_numeric_field(
                value, "liquid_density_kg_m3", component=name
            ),
            cp_liquid_j_kg_k=_numeric_field(value, "cp_liquid_j_kg_k", component=name),
            cp_vapor_j_kg_k=_numeric_field(value, "cp_vapor_j_kg_k", component=name),
            latent_heat_j_kg=_numeric_field(value, "latent_heat_j_kg", component=name),
            source=source,
            confidence=confidence,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "normal_boiling_point_k": self.normal_boiling_point_k,
            "molecular_weight_kg_mol": self.molecular_weight_kg_mol,
            "liquid_density_kg_m3": self.liquid_density_kg_m3,
            "cp_liquid_j_kg_k": self.cp_liquid_j_kg_k,
            "cp_vapor_j_kg_k": self.cp_vapor_j_kg_k,
            "latent_heat_j_kg": self.latent_heat_j_kg,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ComponentCatalog:
    """Complete and immutable component catalog for one parameter set."""

    schema_version: str
    parameter_set_version: str
    components: Mapping[str, PseudoComponent]

    def __post_init__(self) -> None:
        if not _VERSION_PATTERN.fullmatch(self.schema_version):
            raise ValueError("component catalog schema_version is invalid")
        if not _VERSION_PATTERN.fullmatch(self.parameter_set_version):
            raise ValueError("component catalog parameter_set_version is invalid")
        names = set(self.components)
        expected = set(ALL_COMPONENTS)
        if names != expected:
            raise ValueError(
                f"component catalog differs; missing={sorted(expected - names)}, "
                f"unknown={sorted(names - expected)}"
            )
        for name, component in self.components.items():
            if name != component.name:
                raise ValueError("component mapping key must match component name")
        boiling_points = [
            self.components[name].normal_boiling_point_k for name in HYDROCARBON_COMPONENTS
        ]
        if any(left >= right for left, right in pairwise(boiling_points)):
            raise ValueError("hydrocarbon normal boiling points must be strictly increasing")
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ComponentCatalog:
        expected = {"schema_version", "parameter_set_version", "components"}
        if set(value) != expected:
            raise ValueError("component catalog must contain only schema, parameter and component fields")
        schema_version = value["schema_version"]
        parameter_set_version = value["parameter_set_version"]
        raw_components = value["components"]
        if not isinstance(schema_version, str) or not isinstance(parameter_set_version, str):
            raise TypeError("catalog versions must be strings")
        if not isinstance(raw_components, Mapping):
            raise TypeError("catalog components must be an object")
        components: dict[str, PseudoComponent] = {}
        for name, raw_component in raw_components.items():
            if not isinstance(name, str) or not isinstance(raw_component, Mapping):
                raise TypeError("component entries must map names to objects")
            components[name] = PseudoComponent.from_mapping(name, raw_component)
        return cls(schema_version, parameter_set_version, components)

    def as_dict(self) -> dict[str, object]:
        component_values: dict[str, dict[str, object]] = {}
        for name, component in self.components.items():
            raw = component.as_dict()
            del raw["name"]
            component_values[name] = raw
        return {
            "schema_version": self.schema_version,
            "parameter_set_version": self.parameter_set_version,
            "components": component_values,
        }
