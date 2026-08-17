"""Stable data contracts shared by equipment, flowsheet and simulation layers."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..properties.components import ALL_COMPONENTS
from .math_utils import weighted_average

_COMPOSITION_TOLERANCE = 1e-9
_SIMULATION_STATUSES = frozenset({"success", "failed", "not_converged", "rejected"})


def _finite_mapping(values: Mapping[str, float], *, context: str) -> Mapping[str, float]:
    copied = dict(values)
    if any(not isinstance(key, str) for key in copied):
        raise TypeError(f"{context} keys must be strings")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        for value in copied.values()
    ):
        raise TypeError(f"{context} values must be numbers")
    if any(not math.isfinite(value) for value in copied.values()):
        raise ValueError(f"{context} values must be finite")
    return MappingProxyType(copied)


def _string_mapping(values: Mapping[str, str], *, context: str) -> Mapping[str, str]:
    copied = dict(values)
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in copied.items()):
        raise ValueError(f"{context} keys and values must be strings")
    return MappingProxyType(copied)


@dataclass(frozen=True)
class MaterialStream:
    """A bulk material stream plus an independently conserved salt tracer.

    mass_flow_kg_s includes the seven bulk pseudo-components but excludes
    salt_mass_flow_kg_s. Core calculations use SI units.
    """

    name: str
    mass_flow_kg_s: float
    temperature_k: float
    pressure_pa: float
    mass_fractions: Mapping[str, float]
    salt_mass_flow_kg_s: float = 0.0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("stream name cannot be empty")
        scalar_values = (
            self.mass_flow_kg_s,
            self.temperature_k,
            self.pressure_pa,
            self.salt_mass_flow_kg_s,
        )
        if any(not math.isfinite(value) for value in scalar_values):
            raise ValueError(f"stream {self.name!r} contains non-finite values")
        if self.mass_flow_kg_s < 0.0:
            raise ValueError("mass flow cannot be negative")
        if self.temperature_k <= 0.0 or self.pressure_pa <= 0.0:
            raise ValueError("absolute temperature and pressure must be positive")
        if self.salt_mass_flow_kg_s < 0.0:
            raise ValueError("salt mass flow cannot be negative")

        fractions = dict(self.mass_fractions)
        if any(not isinstance(key, str) for key in fractions):
            raise TypeError("mass-fraction keys must be strings")
        unknown = sorted(set(fractions) - set(ALL_COMPONENTS))
        if unknown:
            raise ValueError(f"unknown stream components: {', '.join(unknown)}")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0.0
            for value in fractions.values()
        ):
            raise ValueError("mass fractions must be finite and non-negative")
        total = sum(fractions.values())
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_COMPOSITION_TOLERANCE):
            raise ValueError(f"mass fractions must sum to one, got {total:.16g}")
        object.__setattr__(self, "mass_fractions", MappingProxyType(fractions))
        object.__setattr__(
            self,
            "metadata",
            _string_mapping(self.metadata, context="stream metadata"),
        )

    def component_flow_kg_s(self, component: str) -> float:
        """Return a component mass flow, or zero when the component is absent."""

        if component not in ALL_COMPONENTS:
            raise ValueError(f"unknown component: {component!r}")
        return self.mass_flow_kg_s * self.mass_fractions.get(component, 0.0)

    def renamed(self, name: str) -> MaterialStream:
        """Return an identical stream with a new name."""

        return replace(self, name=name)

    def at_conditions(
        self,
        *,
        name: str | None = None,
        temperature_k: float | None = None,
        pressure_pa: float | None = None,
        mass_flow_kg_s: float | None = None,
    ) -> MaterialStream:
        """Return a copy with selected scalar conditions replaced."""

        return replace(
            self,
            name=self.name if name is None else name,
            mass_flow_kg_s=self.mass_flow_kg_s if mass_flow_kg_s is None else mass_flow_kg_s,
            temperature_k=self.temperature_k if temperature_k is None else temperature_k,
            pressure_pa=self.pressure_pa if pressure_pa is None else pressure_pa,
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""

        return {
            "name": self.name,
            "mass_flow_kg_s": self.mass_flow_kg_s,
            "temperature_k": self.temperature_k,
            "pressure_pa": self.pressure_pa,
            "mass_fractions": dict(self.mass_fractions),
            "salt_mass_flow_kg_s": self.salt_mass_flow_kg_s,
            "metadata": dict(self.metadata),
        }


def stream_from_component_flows(
    name: str,
    component_flows_kg_s: Mapping[str, float],
    *,
    temperature_k: float,
    pressure_pa: float,
    salt_mass_flow_kg_s: float = 0.0,
    metadata: Mapping[str, str] | None = None,
) -> MaterialStream:
    """Construct a stream from explicit component mass flows."""

    unknown = sorted(set(component_flows_kg_s) - set(ALL_COMPONENTS))
    if unknown:
        raise ValueError(f"unknown stream components: {', '.join(unknown)}")
    if any(not math.isfinite(value) or value < 0.0 for value in component_flows_kg_s.values()):
        raise ValueError("component mass flows must be finite and non-negative")
    total = sum(component_flows_kg_s.values())
    if total <= 0.0:
        raise ValueError("total component flow must be positive")
    fractions = {
        component: flow / total
        for component, flow in component_flows_kg_s.items()
        if flow > 0.0
    }
    return MaterialStream(
        name=name,
        mass_flow_kg_s=total,
        temperature_k=temperature_k,
        pressure_pa=pressure_pa,
        mass_fractions=fractions,
        salt_mass_flow_kg_s=salt_mass_flow_kg_s,
        metadata={} if metadata is None else metadata,
    )


def merge_streams(
    name: str,
    streams: Iterable[MaterialStream],
    *,
    pressure_pa: float | None = None,
) -> MaterialStream:
    """Mix streams using a common constant-heat-capacity temperature approximation."""

    stream_list = tuple(streams)
    if not stream_list:
        raise ValueError("at least one stream is required")
    total_flow = sum(stream.mass_flow_kg_s for stream in stream_list)
    if total_flow <= 0.0:
        raise ValueError("combined stream flow must be positive")
    component_flows = {
        component: sum(stream.component_flow_kg_s(component) for stream in stream_list)
        for component in ALL_COMPONENTS
    }
    fractions = {
        component: flow / total_flow
        for component, flow in component_flows.items()
        if flow > 0.0
    }
    temperature_k = weighted_average(
        (stream.temperature_k, stream.mass_flow_kg_s) for stream in stream_list
    )
    mixed_pressure = (
        min(stream.pressure_pa for stream in stream_list)
        if pressure_pa is None
        else pressure_pa
    )
    return MaterialStream(
        name=name,
        mass_flow_kg_s=total_flow,
        temperature_k=temperature_k,
        pressure_pa=mixed_pressure,
        mass_fractions=fractions,
        salt_mass_flow_kg_s=sum(stream.salt_mass_flow_kg_s for stream in stream_list),
    )


@dataclass(frozen=True)
class BalanceReport:
    """Conservation diagnostics for one declared model boundary."""

    inlet_kg_s: float
    outlet_kg_s: float
    accumulation_kg_s: float = 0.0
    component_residuals_kg_s: Mapping[str, float] = field(default_factory=dict)
    salt_residual_kg_s: float = 0.0
    energy_residual_w: float | None = None

    def __post_init__(self) -> None:
        scalars = (
            self.inlet_kg_s,
            self.outlet_kg_s,
            self.accumulation_kg_s,
            self.salt_residual_kg_s,
        )
        if any(not math.isfinite(value) for value in scalars):
            raise ValueError("balance values must be finite")
        if self.energy_residual_w is not None and not math.isfinite(self.energy_residual_w):
            raise ValueError("energy residual must be finite when provided")
        unknown = sorted(set(self.component_residuals_kg_s) - set(ALL_COMPONENTS))
        if unknown:
            raise ValueError(f"unknown balance components: {', '.join(unknown)}")
        object.__setattr__(
            self,
            "component_residuals_kg_s",
            _finite_mapping(self.component_residuals_kg_s, context="component residual"),
        )

    @property
    def residual_kg_s(self) -> float:
        return self.inlet_kg_s - self.outlet_kg_s - self.accumulation_kg_s

    @property
    def relative_residual(self) -> float:
        scale = max(abs(self.inlet_kg_s), 1e-12)
        return self.residual_kg_s / scale

    def passed(
        self,
        *,
        mass_atol_kg_s: float = 1e-9,
        component_atol_kg_s: float = 1e-9,
        salt_atol_kg_s: float = 1e-12,
        energy_atol_w: float | None = None,
    ) -> bool:
        if any(
            not math.isfinite(tolerance) or tolerance < 0.0
            for tolerance in (mass_atol_kg_s, component_atol_kg_s, salt_atol_kg_s)
        ):
            raise ValueError("balance tolerances must be finite and non-negative")
        mass_ok = abs(self.residual_kg_s) <= mass_atol_kg_s
        components_ok = all(
            abs(value) <= component_atol_kg_s
            for value in self.component_residuals_kg_s.values()
        )
        salt_ok = abs(self.salt_residual_kg_s) <= salt_atol_kg_s
        if energy_atol_w is None:
            energy_ok = True
        else:
            if not math.isfinite(energy_atol_w) or energy_atol_w < 0.0:
                raise ValueError("energy tolerance must be finite and non-negative")
            energy_ok = (
                self.energy_residual_w is not None
                and abs(self.energy_residual_w) <= energy_atol_w
            )
        return mass_ok and components_ok and salt_ok and energy_ok

    def as_dict(self) -> dict[str, object]:
        return {
            "inlet_kg_s": self.inlet_kg_s,
            "outlet_kg_s": self.outlet_kg_s,
            "accumulation_kg_s": self.accumulation_kg_s,
            "residual_kg_s": self.residual_kg_s,
            "relative_residual": self.relative_residual,
            "component_residuals_kg_s": dict(self.component_residuals_kg_s),
            "salt_residual_kg_s": self.salt_residual_kg_s,
            "energy_residual_w": self.energy_residual_w,
        }


@dataclass(frozen=True)
class UnitResult:
    """Common result returned by every unit operation.

    duty_w is positive when heat enters the process material and negative
    when heat leaves it.
    """

    outlets: Mapping[str, MaterialStream]
    duty_w: float = 0.0
    diagnostics: Mapping[str, float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    balance: BalanceReport | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.duty_w):
            raise ValueError("unit duty must be finite")
        if any(
            not isinstance(key, str) or not isinstance(value, MaterialStream)
            for key, value in self.outlets.items()
        ):
            raise TypeError("unit outlets must map string names to MaterialStream values")
        if any(not isinstance(warning, str) for warning in self.warnings):
            raise TypeError("unit warnings must be strings")
        object.__setattr__(self, "outlets", MappingProxyType(dict(self.outlets)))
        object.__setattr__(
            self,
            "diagnostics",
            _finite_mapping(self.diagnostics, context="unit diagnostic"),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def as_dict(self) -> dict[str, object]:
        return {
            "outlets": {name: stream.as_dict() for name, stream in self.outlets.items()},
            "duty_w": self.duty_w,
            "diagnostics": dict(self.diagnostics),
            "warnings": list(self.warnings),
            "balance": None if self.balance is None else self.balance.as_dict(),
        }


@dataclass(frozen=True)
class EquipmentState:
    """Named numerical state for one equipment item."""

    equipment_id: str
    values: Mapping[str, float]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.equipment_id.strip():
            raise ValueError("equipment_id cannot be empty")
        object.__setattr__(
            self,
            "values",
            _finite_mapping(self.values, context="equipment state"),
        )
        object.__setattr__(
            self,
            "metadata",
            _string_mapping(self.metadata, context="equipment metadata"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "equipment_id": self.equipment_id,
            "values": dict(self.values),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ControlSignals:
    """External commands and measured values shared with later control stages."""

    values: Mapping[str, float]
    modes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            _finite_mapping(self.values, context="control signal"),
        )
        object.__setattr__(
            self,
            "modes",
            _string_mapping(self.modes, context="control mode"),
        )

    def as_dict(self) -> dict[str, object]:
        return {"values": dict(self.values), "modes": dict(self.modes)}


@dataclass(frozen=True)
class SimulationResult:
    """Serializable top-level result for steady or dynamic model execution."""

    status: str
    streams: Mapping[str, MaterialStream] = field(default_factory=dict)
    equipment_states: Mapping[str, EquipmentState] = field(default_factory=dict)
    control_signals: ControlSignals | None = None
    balance: BalanceReport | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)
    events: tuple[Mapping[str, object], ...] = ()
    versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _SIMULATION_STATUSES:
            raise ValueError(f"unsupported simulation status: {self.status!r}")
        if any(
            not isinstance(key, str) or not isinstance(value, MaterialStream)
            for key, value in self.streams.items()
        ):
            raise TypeError("simulation streams must map string names to MaterialStream values")
        if any(
            not isinstance(key, str) or not isinstance(value, EquipmentState)
            for key, value in self.equipment_states.items()
        ):
            raise TypeError(
                "simulation equipment_states must map string names to EquipmentState values"
            )
        object.__setattr__(self, "streams", MappingProxyType(dict(self.streams)))
        object.__setattr__(
            self,
            "equipment_states",
            MappingProxyType(dict(self.equipment_states)),
        )
        object.__setattr__(
            self,
            "metrics",
            _finite_mapping(self.metrics, context="simulation metric"),
        )
        object.__setattr__(
            self,
            "versions",
            _string_mapping(self.versions, context="simulation version"),
        )
        if any(any(not isinstance(key, str) for key in event) for event in self.events):
            raise TypeError("simulation event keys must be strings")
        plain_events = tuple(dict(event) for event in self.events)
        try:
            json.dumps(plain_events, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("simulation events must be JSON serializable") from exc
        copied_events = tuple(MappingProxyType(event) for event in plain_events)
        object.__setattr__(self, "events", copied_events)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "streams": {name: stream.as_dict() for name, stream in self.streams.items()},
            "equipment_states": {
                name: state.as_dict() for name, state in self.equipment_states.items()
            },
            "control_signals": (
                None if self.control_signals is None else self.control_signals.as_dict()
            ),
            "balance": None if self.balance is None else self.balance.as_dict(),
            "metrics": dict(self.metrics),
            "events": [dict(event) for event in self.events],
            "versions": dict(self.versions),
        }
