"""Deterministic nominal-grid RK4 execution for the open-loop dynamic CDU model."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType
from typing import Protocol, cast

from ..core.config import canonical_fingerprint
from ..core.math_utils import rk4_step
from ..core.types import BalanceReport
from ..properties.components import ALL_COMPONENTS
from .schedule import CommandSchedule
from .state import DynamicState

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RESULT_STATUSES = frozenset({"success", "failed"})
_REQUIRED_SOURCE_VERSION_NAMES = frozenset(
    {
        "software_version",
        "model_version",
        "parameter_set_version",
        "config_version",
        "case_version",
    }
)
_REQUIRED_SCENARIO_METADATA_NAMES = frozenset(
    {"scenario_name", "scenario_version", "purpose"}
)
_SYNTHETIC_DATA_ORIGIN = "M3_open_loop_simulation"


class DynamicConservationError(RuntimeError):
    """Raised when an instantaneous or cumulative conservation gate fails."""


@dataclass(frozen=True)
class DynamicConservationTolerances:
    """Explicit numerical gates for dynamic material conservation."""

    instantaneous_mass_atol_kg_s: float = 1e-8
    instantaneous_component_atol_kg_s: float = 1e-8
    instantaneous_salt_atol_kg_s: float = 1e-10
    cumulative_relative_atol: float = 1e-6
    cumulative_flow_floor_kg: float = 1.0

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            object.__setattr__(
                self,
                name,
                _nonnegative_number(value, context=name),
            )
        if self.cumulative_flow_floor_kg <= 0.0:
            raise ValueError("cumulative_flow_floor_kg must be positive")

    @classmethod
    def from_dynamic_model(cls, dynamic_model: object) -> DynamicConservationTolerances:
        """Use configured M0 solver gates when the model exposes them."""

        defaults = cls()
        model_config = getattr(dynamic_model, "model", None)
        raw_solver = getattr(model_config, "solver", None)
        if not isinstance(raw_solver, Mapping):
            return defaults
        solver = cast(Mapping[object, object], raw_solver)

        def configured(name: str, fallback: float) -> float:
            if name not in solver:
                return fallback
            return _nonnegative_number(solver[name], context=f"model.solver.{name}")

        return cls(
            instantaneous_mass_atol_kg_s=configured(
                "mass_tolerance_kg_s",
                defaults.instantaneous_mass_atol_kg_s,
            ),
            instantaneous_component_atol_kg_s=configured(
                "component_tolerance_kg_s",
                defaults.instantaneous_component_atol_kg_s,
            ),
            instantaneous_salt_atol_kg_s=configured(
                "salt_tolerance_kg_s",
                defaults.instantaneous_salt_atol_kg_s,
            ),
            cumulative_relative_atol=defaults.cumulative_relative_atol,
            cumulative_flow_floor_kg=defaults.cumulative_flow_floor_kg,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "instantaneous_mass_atol_kg_s": self.instantaneous_mass_atol_kg_s,
            "instantaneous_component_atol_kg_s": (
                self.instantaneous_component_atol_kg_s
            ),
            "instantaneous_salt_atol_kg_s": self.instantaneous_salt_atol_kg_s,
            "cumulative_relative_atol": self.cumulative_relative_atol,
            "cumulative_flow_floor_kg": self.cumulative_flow_floor_kg,
        }


class DynamicEvaluationLike(Protocol):
    """Minimum evaluation contract needed by the integration engine."""

    derivative_vector: Sequence[float]
    boundary_balance: BalanceReport
    boundary_mass_in_kg_s: float
    boundary_mass_out_kg_s: float
    boundary_salt_in_kg_s: float
    boundary_salt_out_kg_s: float
    boundary_component_in_kg_s: Mapping[str, float]
    boundary_component_out_kg_s: Mapping[str, float]
    maximum_absolute_component_residual_kg_s: float
    maximum_absolute_material_residual_kg_s: float

    def as_dict(self) -> Mapping[str, object]: ...


class DynamicModelLike(Protocol):
    """Structural interface implemented by the reduced dynamic flowsheet."""

    @property
    def initial_state(self) -> DynamicState: ...

    @property
    def baseline_commands(self) -> Mapping[str, float]: ...

    def evaluate(
        self,
        state: DynamicState,
        commands: Mapping[str, float],
    ) -> object: ...


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{context} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{context} must be a finite number")
    return number


def _nonnegative_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number < 0.0:
        raise ValueError(f"{context} must be non-negative")
    return number


def _traceable_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    if metadata is None:
        raise ValueError(
            "metadata must identify scenario_name, scenario_version, and purpose"
        )
    copied = dict(metadata)
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in copied.items()
    ):
        raise TypeError("metadata must map strings to strings")
    missing = sorted(_REQUIRED_SCENARIO_METADATA_NAMES - set(copied))
    if missing:
        raise ValueError(
            "metadata is missing required scenario fields: " + ", ".join(missing)
        )
    blank = sorted(
        name
        for name in _REQUIRED_SCENARIO_METADATA_NAMES
        if not copied[name].strip()
    )
    if blank:
        raise ValueError(
            "metadata scenario fields cannot be blank: " + ", ".join(blank)
        )
    if copied.get("synthetic", "true") != "true":
        raise ValueError("M3 simulation metadata cannot claim non-synthetic data")
    if copied.get("data_origin", _SYNTHETIC_DATA_ORIGIN) != _SYNTHETIC_DATA_ORIGIN:
        raise ValueError(
            f"M3 simulation data_origin must be {_SYNTHETIC_DATA_ORIGIN}"
        )
    copied["synthetic"] = "true"
    copied["data_origin"] = _SYNTHETIC_DATA_ORIGIN
    return copied


def _traceable_versions(
    versions: Mapping[str, str],
    *,
    scenario_version: str,
) -> dict[str, str]:
    copied = dict(versions)
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in copied.items()
    ):
        raise TypeError("versions must map strings to strings")
    missing = sorted(_REQUIRED_SOURCE_VERSION_NAMES - set(copied))
    if missing:
        raise ValueError("versions is missing required fields: " + ", ".join(missing))
    blank = sorted(
        name for name in _REQUIRED_SOURCE_VERSION_NAMES if not copied[name].strip()
    )
    if blank:
        raise ValueError("required versions cannot be blank: " + ", ".join(blank))
    supplied_scenario_version = copied.get("scenario_version")
    if (
        supplied_scenario_version is not None
        and supplied_scenario_version != scenario_version
    ):
        raise ValueError(
            "versions.scenario_version must match metadata.scenario_version"
        )
    copied["scenario_version"] = scenario_version
    copied["simulation_stage"] = "M3"
    return copied


def _freeze_json_value(value: object, *, context: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{context} must not contain non-finite values")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        frozen: dict[str, object] = {}
        for key, nested in mapping.items():
            if not isinstance(key, str):
                raise TypeError(f"{context} keys must be strings")
            frozen[key] = _freeze_json_value(
                nested,
                context=f"{context}.{key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, context=f"{context}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{context} contains a non-JSON value of type {type(value).__name__}")


def _freeze_json_mapping(
    values: Mapping[str, object],
    *,
    context: str,
) -> Mapping[str, object]:
    frozen = _freeze_json_value(values, context=context)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guaranteed by input type
        raise TypeError(f"{context} must be a mapping")
    return cast(Mapping[str, object], frozen)


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _thaw_json_value(nested) for key, nested in mapping.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _finite_command_mapping(
    values: Mapping[str, float],
    *,
    context: str,
) -> Mapping[str, float]:
    copied: dict[str, float] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{context} names must be non-empty strings")
        copied[name] = _finite_number(value, context=f"{context}.{name}")
    if not copied:
        raise ValueError(f"{context} cannot be empty")
    return MappingProxyType(copied)


def _inventory_components(state: DynamicState) -> Mapping[str, float]:
    return MappingProxyType(
        {
            component: (
                sum(
                    inventory.component_masses_kg[component]
                    for inventory in state.liquid_inventories.values()
                )
                + state.top_gas_component_masses_kg[component]
            )
            for component in ALL_COMPONENTS
        }
    )


def _inventory_salt(state: DynamicState) -> float:
    salt_mass = sum(
        inventory.salt_mass_kg for inventory in state.liquid_inventories.values()
    )
    return salt_mass


def _component_mapping(
    values: Mapping[str, float],
    *,
    context: str,
    nonnegative: bool,
) -> Mapping[str, float]:
    if set(values) != set(ALL_COMPONENTS):
        raise ValueError(f"{context} must contain exactly the modeled components")
    copied: dict[str, float] = {}
    for component in ALL_COMPONENTS:
        value = _finite_number(values[component], context=f"{context}.{component}")
        if nonnegative and value < 0.0:
            raise ValueError(f"{context}.{component} must be non-negative")
        copied[component] = value
    return MappingProxyType(copied)


@dataclass(frozen=True)
class DynamicCumulativeBalance:
    """Run-to-date material and salt balance over the dynamic boundary."""

    initial_component_inventory_kg: Mapping[str, float]
    final_component_inventory_kg: Mapping[str, float]
    cumulative_component_in_kg: Mapping[str, float]
    cumulative_component_out_kg: Mapping[str, float]
    initial_inventory_salt_kg: float
    final_inventory_salt_kg: float
    cumulative_salt_in_kg: float
    cumulative_salt_out_kg: float

    def __post_init__(self) -> None:
        for name in (
            "initial_component_inventory_kg",
            "final_component_inventory_kg",
            "cumulative_component_in_kg",
            "cumulative_component_out_kg",
        ):
            object.__setattr__(
                self,
                name,
                _component_mapping(
                    getattr(self, name),
                    context=name,
                    nonnegative=True,
                ),
            )
        for name, value in (
            ("initial_inventory_salt_kg", self.initial_inventory_salt_kg),
            ("final_inventory_salt_kg", self.final_inventory_salt_kg),
            ("cumulative_salt_in_kg", self.cumulative_salt_in_kg),
            ("cumulative_salt_out_kg", self.cumulative_salt_out_kg),
        ):
            _nonnegative_number(value, context=name)

    @property
    def component_residuals_kg(self) -> Mapping[str, float]:
        return MappingProxyType(
            {
                component: (
                    self.initial_component_inventory_kg[component]
                    + self.cumulative_component_in_kg[component]
                    - self.cumulative_component_out_kg[component]
                    - self.final_component_inventory_kg[component]
                )
                for component in ALL_COMPONENTS
            }
        )

    @property
    def initial_inventory_mass_kg(self) -> float:
        return sum(self.initial_component_inventory_kg.values())

    @property
    def final_inventory_mass_kg(self) -> float:
        return sum(self.final_component_inventory_kg.values())

    @property
    def cumulative_mass_in_kg(self) -> float:
        return sum(self.cumulative_component_in_kg.values())

    @property
    def cumulative_mass_out_kg(self) -> float:
        return sum(self.cumulative_component_out_kg.values())

    @property
    def mass_residual_kg(self) -> float:
        return sum(self.component_residuals_kg.values())

    @property
    def maximum_absolute_component_residual_kg(self) -> float:
        return max(
            (abs(value) for value in self.component_residuals_kg.values()),
            default=0.0,
        )

    @property
    def salt_residual_kg(self) -> float:
        return (
            self.initial_inventory_salt_kg
            + self.cumulative_salt_in_kg
            - self.cumulative_salt_out_kg
            - self.final_inventory_salt_kg
        )

    def passed(self, *, mass_atol_kg: float, salt_atol_kg: float) -> bool:
        mass_tolerance = _nonnegative_number(mass_atol_kg, context="mass_atol_kg")
        salt_tolerance = _nonnegative_number(salt_atol_kg, context="salt_atol_kg")
        return (
            self.maximum_absolute_component_residual_kg <= mass_tolerance
            and abs(self.mass_residual_kg) <= mass_tolerance
            and abs(self.salt_residual_kg) <= salt_tolerance
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "initial_component_inventory_kg": dict(
                self.initial_component_inventory_kg
            ),
            "final_component_inventory_kg": dict(self.final_component_inventory_kg),
            "cumulative_component_in_kg": dict(self.cumulative_component_in_kg),
            "cumulative_component_out_kg": dict(self.cumulative_component_out_kg),
            "component_residuals_kg": dict(self.component_residuals_kg),
            "initial_inventory_mass_kg": self.initial_inventory_mass_kg,
            "final_inventory_mass_kg": self.final_inventory_mass_kg,
            "cumulative_mass_in_kg": self.cumulative_mass_in_kg,
            "cumulative_mass_out_kg": self.cumulative_mass_out_kg,
            "initial_inventory_salt_kg": self.initial_inventory_salt_kg,
            "final_inventory_salt_kg": self.final_inventory_salt_kg,
            "cumulative_salt_in_kg": self.cumulative_salt_in_kg,
            "cumulative_salt_out_kg": self.cumulative_salt_out_kg,
            "mass_residual_kg": self.mass_residual_kg,
            "maximum_absolute_component_residual_kg": (
                self.maximum_absolute_component_residual_kg
            ),
            "salt_residual_kg": self.salt_residual_kg,
        }


@dataclass(frozen=True)
class DynamicSample:
    """One immutable endpoint sample; state serialization is deferred until requested."""

    time_s: float
    state: DynamicState
    commands: Mapping[str, float]
    evaluation: Mapping[str, object]
    cumulative_component_in_kg: Mapping[str, float]
    cumulative_component_out_kg: Mapping[str, float]
    component_balance_residuals_kg: Mapping[str, float]
    cumulative_salt_in_kg: float = 0.0
    cumulative_salt_out_kg: float = 0.0
    mass_balance_residual_kg: float = 0.0
    salt_balance_residual_kg: float = 0.0
    instantaneous_mass_residual_kg_s: float = 0.0
    instantaneous_max_component_residual_kg_s: float = 0.0
    instantaneous_salt_residual_kg_s: float = 0.0

    def __post_init__(self) -> None:
        time_s = _nonnegative_number(self.time_s, context="sample time_s")
        if not isinstance(self.state, DynamicState):
            raise TypeError("sample state must be a DynamicState")
        for name in ("cumulative_salt_in_kg", "cumulative_salt_out_kg"):
            _nonnegative_number(getattr(self, name), context=f"sample {name}")
        for name in (
            "mass_balance_residual_kg",
            "salt_balance_residual_kg",
            "instantaneous_mass_residual_kg_s",
            "instantaneous_max_component_residual_kg_s",
            "instantaneous_salt_residual_kg_s",
        ):
            _finite_number(getattr(self, name), context=f"sample {name}")
        if self.instantaneous_max_component_residual_kg_s < 0.0:
            raise ValueError(
                "sample instantaneous_max_component_residual_kg_s must be non-negative"
            )
        object.__setattr__(self, "time_s", time_s)
        object.__setattr__(
            self,
            "commands",
            _finite_command_mapping(self.commands, context="sample commands"),
        )
        object.__setattr__(
            self,
            "evaluation",
            _freeze_json_mapping(self.evaluation, context="sample evaluation"),
        )
        object.__setattr__(
            self,
            "cumulative_component_in_kg",
            _component_mapping(
                self.cumulative_component_in_kg,
                context="sample cumulative_component_in_kg",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "cumulative_component_out_kg",
            _component_mapping(
                self.cumulative_component_out_kg,
                context="sample cumulative_component_out_kg",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "component_balance_residuals_kg",
            _component_mapping(
                self.component_balance_residuals_kg,
                context="sample component_balance_residuals_kg",
                nonnegative=False,
            ),
        )
        if not math.isclose(
            sum(self.component_balance_residuals_kg.values()),
            self.mass_balance_residual_kg,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "sample mass balance residual must equal the component residual sum"
            )

    @property
    def cumulative_mass_in_kg(self) -> float:
        return sum(self.cumulative_component_in_kg.values())

    @property
    def cumulative_mass_out_kg(self) -> float:
        return sum(self.cumulative_component_out_kg.values())

    @property
    def maximum_absolute_component_balance_residual_kg(self) -> float:
        return max(
            (abs(value) for value in self.component_balance_residuals_kg.values()),
            default=0.0,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "time_s": self.time_s,
            "state": self.state.as_dict(),
            "commands": dict(self.commands),
            "evaluation": _thaw_json_value(self.evaluation),
            "cumulative_component_in_kg": dict(self.cumulative_component_in_kg),
            "cumulative_component_out_kg": dict(self.cumulative_component_out_kg),
            "component_balance_residuals_kg": dict(
                self.component_balance_residuals_kg
            ),
            "cumulative_mass_in_kg": self.cumulative_mass_in_kg,
            "cumulative_mass_out_kg": self.cumulative_mass_out_kg,
            "cumulative_salt_in_kg": self.cumulative_salt_in_kg,
            "cumulative_salt_out_kg": self.cumulative_salt_out_kg,
            "mass_balance_residual_kg": self.mass_balance_residual_kg,
            "salt_balance_residual_kg": self.salt_balance_residual_kg,
            "instantaneous_mass_residual_kg_s": (
                self.instantaneous_mass_residual_kg_s
            ),
            "instantaneous_max_component_residual_kg_s": (
                self.instantaneous_max_component_residual_kg_s
            ),
            "instantaneous_salt_residual_kg_s": (
                self.instantaneous_salt_residual_kg_s
            ),
        }


@dataclass(frozen=True)
class DynamicSimulationResult:
    """Traceable dynamic time series with explicit partial-failure semantics."""

    status: str
    samples: tuple[DynamicSample, ...]
    balance: DynamicCumulativeBalance
    conservation_tolerances: DynamicConservationTolerances
    diagnostics: Mapping[str, float]
    versions: Mapping[str, str]
    metadata: Mapping[str, str]
    source_fingerprint: str
    input_fingerprint: str
    requested_duration_s: float
    time_step_s: float
    failure_reason: str | None = None
    failure_stage: str | None = None
    failure_time_s: float | None = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"unsupported dynamic simulation status: {self.status!r}")
        duration = _nonnegative_number(
            self.requested_duration_s,
            context="requested_duration_s",
        )
        step = _nonnegative_number(self.time_step_s, context="time_step_s")
        if duration <= 0.0 or step <= 0.0:
            raise ValueError("requested duration and time step must be positive")
        if not _SHA256_PATTERN.fullmatch(self.source_fingerprint):
            raise ValueError("source_fingerprint must be a lowercase SHA-256 digest")
        if not _SHA256_PATTERN.fullmatch(self.input_fingerprint):
            raise ValueError("input_fingerprint must be a lowercase SHA-256 digest")
        samples = tuple(self.samples)
        if any(not isinstance(sample, DynamicSample) for sample in samples):
            raise TypeError("dynamic samples must be DynamicSample instances")
        if not isinstance(self.balance, DynamicCumulativeBalance):
            raise TypeError("dynamic balance must be a DynamicCumulativeBalance")
        if not isinstance(
            self.conservation_tolerances,
            DynamicConservationTolerances,
        ):
            raise TypeError(
                "conservation_tolerances must be DynamicConservationTolerances"
            )
        if samples and samples[0].time_s != 0.0:
            raise ValueError("the first dynamic sample must be at t=0")
        if any(
            later.time_s <= earlier.time_s
            for earlier, later in pairwise(samples)
        ):
            raise ValueError("dynamic sample times must increase strictly")
        if samples and samples[-1].time_s > duration:
            raise ValueError("dynamic sample time exceeds the requested duration")
        if self.status == "success":
            if not samples or not math.isclose(
                samples[-1].time_s,
                duration,
                rel_tol=0.0,
                abs_tol=1e-12 * max(1.0, duration),
            ):
                raise ValueError("a successful run must reach the requested duration")
            if any(
                value is not None
                for value in (self.failure_reason, self.failure_stage, self.failure_time_s)
            ):
                raise ValueError("a successful run cannot contain failure metadata")
        else:
            if not self.failure_reason or not self.failure_stage:
                raise ValueError("a failed run must describe its reason and stage")
            if self.failure_time_s is not None:
                failure_time = _nonnegative_number(
                    self.failure_time_s,
                    context="failure_time_s",
                )
                if failure_time > duration:
                    raise ValueError("failure_time_s exceeds the requested duration")
        if any(not isinstance(name, str) for name in self.diagnostics):
            raise TypeError("dynamic diagnostic names must be strings")
        copied_diagnostics = {
            name: _finite_number(value, context=f"dynamic diagnostic {name!r}")
            for name, value in self.diagnostics.items()
        }
        if any(not isinstance(name, str) for name in self.versions):
            raise TypeError("dynamic version names must be strings")
        if any(not isinstance(value, str) for value in self.versions.values()):
            raise TypeError("dynamic version values must be strings")
        copied_metadata = _traceable_metadata(self.metadata)
        copied_versions = _traceable_versions(
            self.versions,
            scenario_version=copied_metadata["scenario_version"],
        )
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "diagnostics", MappingProxyType(copied_diagnostics))
        object.__setattr__(self, "versions", MappingProxyType(copied_versions))
        object.__setattr__(self, "metadata", MappingProxyType(copied_metadata))
        object.__setattr__(self, "requested_duration_s", duration)
        object.__setattr__(self, "time_step_s", step)

    @property
    def last_valid_state(self) -> DynamicState | None:
        return None if not self.samples else self.samples[-1].state

    @property
    def completed_time_s(self) -> float:
        return 0.0 if not self.samples else self.samples[-1].time_s

    def require_success(self) -> DynamicSimulationResult:
        if self.status != "success":
            raise RuntimeError(self.failure_reason or "dynamic simulation failed")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "samples": [sample.as_dict() for sample in self.samples],
            "balance": self.balance.as_dict(),
            "conservation_tolerances": self.conservation_tolerances.as_dict(),
            "diagnostics": dict(self.diagnostics),
            "versions": dict(self.versions),
            "metadata": dict(self.metadata),
            "source_fingerprint": self.source_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "requested_duration_s": self.requested_duration_s,
            "time_step_s": self.time_step_s,
            "completed_time_s": self.completed_time_s,
            "failure_reason": self.failure_reason,
            "failure_stage": self.failure_stage,
            "failure_time_s": self.failure_time_s,
        }


def _evaluation_and_rates(
    dynamic_model: DynamicModelLike,
    state: DynamicState,
    commands: Mapping[str, float],
    *,
    capture_payload: bool,
    tolerances: DynamicConservationTolerances,
) -> tuple[
    DynamicEvaluationLike,
    Mapping[str, object],
    tuple[float, ...],
    tuple[float, float, float],
]:
    evaluation = cast(
        DynamicEvaluationLike,
        dynamic_model.evaluate(state, commands),
    )
    if capture_payload:
        raw_payload = evaluation.as_dict()
        if not isinstance(raw_payload, Mapping):
            raise TypeError("dynamic evaluation as_dict() must return a mapping")
        evaluation_payload = {
            name: value
            for name, value in raw_payload.items()
            if name != "derivative_vector"
        }
    else:
        evaluation_payload = {}
    component_in = _component_mapping(
        evaluation.boundary_component_in_kg_s,
        context="boundary_component_in_kg_s",
        nonnegative=True,
    )
    component_out = _component_mapping(
        evaluation.boundary_component_out_kg_s,
        context="boundary_component_out_kg_s",
        nonnegative=True,
    )
    mass_in = _nonnegative_number(
        evaluation.boundary_mass_in_kg_s,
        context="boundary_mass_in_kg_s",
    )
    mass_out = _nonnegative_number(
        evaluation.boundary_mass_out_kg_s,
        context="boundary_mass_out_kg_s",
    )
    if not math.isclose(
        mass_in,
        sum(component_in.values()),
        rel_tol=1e-12,
        abs_tol=1e-9,
    ) or not math.isclose(
        mass_out,
        sum(component_out.values()),
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise ValueError("boundary total mass rates differ from component rates")
    boundary_rates = (
        *(component_in[component] for component in ALL_COMPONENTS),
        *(component_out[component] for component in ALL_COMPONENTS),
        _nonnegative_number(
            evaluation.boundary_salt_in_kg_s,
            context="boundary_salt_in_kg_s",
        ),
        _nonnegative_number(
            evaluation.boundary_salt_out_kg_s,
            context="boundary_salt_out_kg_s",
        ),
    )
    instantaneous_residuals = (
        _finite_number(
            evaluation.boundary_balance.residual_kg_s,
            context="instantaneous mass residual",
        ),
        _nonnegative_number(
            evaluation.maximum_absolute_component_residual_kg_s,
            context="instantaneous maximum component residual",
        ),
        _finite_number(
            evaluation.boundary_balance.salt_residual_kg_s,
            context="instantaneous salt residual",
        ),
    )
    material_maximum = _nonnegative_number(
        evaluation.maximum_absolute_material_residual_kg_s,
        context="instantaneous maximum material residual",
    )
    if not evaluation.boundary_balance.passed(
        mass_atol_kg_s=tolerances.instantaneous_mass_atol_kg_s,
        component_atol_kg_s=tolerances.instantaneous_component_atol_kg_s,
        salt_atol_kg_s=tolerances.instantaneous_salt_atol_kg_s,
    ):
        raise DynamicConservationError(
            "instantaneous boundary balance failed: "
            f"mass={instantaneous_residuals[0]:.16g} kg/s, "
            f"max_component={instantaneous_residuals[1]:.16g} kg/s, "
            f"salt={instantaneous_residuals[2]:.16g} kg/s, "
            f"reported_maximum={material_maximum:.16g} kg/s"
        )
    return evaluation, evaluation_payload, boundary_rates, instantaneous_residuals


def _make_sample(
    *,
    time_s: float,
    state: DynamicState,
    commands: Mapping[str, float],
    evaluation: Mapping[str, object],
    cumulative_rates: Sequence[float],
    initial_component_inventory_kg: Mapping[str, float],
    initial_inventory_salt_kg: float,
    instantaneous_residuals: tuple[float, float, float],
) -> DynamicSample:
    cumulative = tuple(
        _nonnegative_number(value, context=f"cumulative boundary amount {index}")
        for index, value in enumerate(cumulative_rates)
    )
    component_count = len(ALL_COMPONENTS)
    if len(cumulative) != 2 * component_count + 2:
        raise ValueError("cumulative boundary amounts have the wrong dimension")
    component_in = {
        component: cumulative[index]
        for index, component in enumerate(ALL_COMPONENTS)
    }
    component_out = {
        component: cumulative[component_count + index]
        for index, component in enumerate(ALL_COMPONENTS)
    }
    current_components = _inventory_components(state)
    component_residuals = {
        component: (
            initial_component_inventory_kg[component]
            + component_in[component]
            - component_out[component]
            - current_components[component]
        )
        for component in ALL_COMPONENTS
    }
    cumulative_salt_in = cumulative[-2]
    cumulative_salt_out = cumulative[-1]
    current_salt = _inventory_salt(state)
    salt_residual = (
        initial_inventory_salt_kg
        + cumulative_salt_in
        - cumulative_salt_out
        - current_salt
    )
    return DynamicSample(
        time_s=time_s,
        state=state,
        commands=commands,
        evaluation=evaluation,
        cumulative_component_in_kg=component_in,
        cumulative_component_out_kg=component_out,
        component_balance_residuals_kg=component_residuals,
        cumulative_salt_in_kg=cumulative_salt_in,
        cumulative_salt_out_kg=cumulative_salt_out,
        mass_balance_residual_kg=sum(component_residuals.values()),
        salt_balance_residual_kg=salt_residual,
        instantaneous_mass_residual_kg_s=instantaneous_residuals[0],
        instantaneous_max_component_residual_kg_s=instantaneous_residuals[1],
        instantaneous_salt_residual_kg_s=instantaneous_residuals[2],
    )


def _require_cumulative_conservation(
    sample: DynamicSample,
    tolerances: DynamicConservationTolerances,
) -> None:
    floor = tolerances.cumulative_flow_floor_kg
    component_relative_residuals = {
        component: (
            abs(sample.component_balance_residuals_kg[component])
            / max(sample.cumulative_component_in_kg[component], floor)
        )
        for component in ALL_COMPONENTS
    }
    maximum_component_relative = max(
        component_relative_residuals.values(),
        default=0.0,
    )
    mass_relative = abs(sample.mass_balance_residual_kg) / max(
        sample.cumulative_mass_in_kg,
        floor,
    )
    salt_relative = abs(sample.salt_balance_residual_kg) / max(
        sample.cumulative_salt_in_kg,
        floor,
    )
    if (
        mass_relative > tolerances.cumulative_relative_atol
        or maximum_component_relative > tolerances.cumulative_relative_atol
        or salt_relative > tolerances.cumulative_relative_atol
    ):
        raise DynamicConservationError(
            "cumulative boundary balance failed: "
            f"mass_relative={mass_relative:.16g}, "
            f"max_component_relative={maximum_component_relative:.16g}, "
            f"salt_relative={salt_relative:.16g}, "
            f"threshold={tolerances.cumulative_relative_atol:.16g}"
        )


def _schedule_discontinuities(
    schedule: CommandSchedule,
    *,
    duration_s: float,
) -> frozenset[float]:
    discontinuities: set[float] = set()
    for event in schedule.events:
        if 0.0 < event.time_s <= duration_s:
            discontinuities.add(event.time_s)
        if event.duration_s is not None:
            end_time = event.time_s + event.duration_s
            if 0.0 < end_time <= duration_s:
                discontinuities.add(end_time)
    return frozenset(discontinuities)


def _integration_endpoints(
    *,
    duration_s: float,
    dt_s: float,
    requested_steps: int,
    discontinuities: frozenset[float],
) -> tuple[float, ...]:
    endpoints = set(
        _nominal_endpoints(
            duration_s=duration_s,
            dt_s=dt_s,
            requested_steps=requested_steps,
        )
    )
    endpoints.update(discontinuities)
    return tuple(sorted(endpoints))


def _nominal_endpoints(
    *,
    duration_s: float,
    dt_s: float,
    requested_steps: int,
) -> tuple[float, ...]:
    return tuple(
        duration_s if step_index == requested_steps else step_index * dt_s
        for step_index in range(1, requested_steps + 1)
    )


def _commands_before(
    schedule: CommandSchedule,
    discontinuity_time_s: float,
) -> Mapping[str, float]:
    if discontinuity_time_s <= 0.0:
        raise ValueError("a positive discontinuity time is required")
    return schedule.values_at(math.nextafter(discontinuity_time_s, -math.inf))


def _schedule_payload(schedule: CommandSchedule) -> dict[str, object]:
    return {
        "baseline_commands": dict(schedule.baseline_commands),
        "events": [
            {
                "time_s": event.time_s,
                "target": event.target,
                "value": event.value,
                "duration_s": event.duration_s,
            }
            for event in schedule.events
        ],
    }


def _build_balance(
    initial_state: DynamicState,
    samples: Sequence[DynamicSample],
) -> DynamicCumulativeBalance:
    initial_components = _inventory_components(initial_state)
    initial_salt = _inventory_salt(initial_state)
    if samples:
        final_sample = samples[-1]
        final_state = final_sample.state
        component_in = final_sample.cumulative_component_in_kg
        component_out = final_sample.cumulative_component_out_kg
        salt_in = final_sample.cumulative_salt_in_kg
        salt_out = final_sample.cumulative_salt_out_kg
    else:
        final_state = initial_state
        component_in = {component: 0.0 for component in ALL_COMPONENTS}
        component_out = {component: 0.0 for component in ALL_COMPONENTS}
        salt_in = 0.0
        salt_out = 0.0
    final_components = _inventory_components(final_state)
    final_salt = _inventory_salt(final_state)
    return DynamicCumulativeBalance(
        initial_component_inventory_kg=initial_components,
        final_component_inventory_kg=final_components,
        cumulative_component_in_kg=component_in,
        cumulative_component_out_kg=component_out,
        initial_inventory_salt_kg=initial_salt,
        final_inventory_salt_kg=final_salt,
        cumulative_salt_in_kg=salt_in,
        cumulative_salt_out_kg=salt_out,
    )


def _result_diagnostics(
    samples: Sequence[DynamicSample],
    *,
    nominal_endpoints: Sequence[float],
    integration_endpoints: Sequence[float],
    tolerances: DynamicConservationTolerances,
) -> dict[str, float]:
    accepted_times = {sample.time_s for sample in samples}
    completed_nominal_steps = sum(
        endpoint in accepted_times for endpoint in nominal_endpoints
    )
    return {
        "requested_nominal_steps": float(len(nominal_endpoints)),
        "completed_nominal_steps": float(completed_nominal_steps),
        "requested_integration_substeps": float(len(integration_endpoints)),
        "completed_integration_substeps": float(max(0, len(samples) - 1)),
        "max_absolute_mass_balance_residual_kg": max(
            (abs(sample.mass_balance_residual_kg) for sample in samples),
            default=0.0,
        ),
        "max_absolute_salt_balance_residual_kg": max(
            (abs(sample.salt_balance_residual_kg) for sample in samples),
            default=0.0,
        ),
        "max_absolute_component_balance_residual_kg": max(
            (
                sample.maximum_absolute_component_balance_residual_kg
                for sample in samples
            ),
            default=0.0,
        ),
        "max_instantaneous_mass_residual_kg_s": max(
            (abs(sample.instantaneous_mass_residual_kg_s) for sample in samples),
            default=0.0,
        ),
        "max_instantaneous_component_residual_kg_s": max(
            (
                sample.instantaneous_max_component_residual_kg_s
                for sample in samples
            ),
            default=0.0,
        ),
        "max_instantaneous_salt_residual_kg_s": max(
            (abs(sample.instantaneous_salt_residual_kg_s) for sample in samples),
            default=0.0,
        ),
        **{
            f"conservation_{name}": value
            for name, value in tolerances.as_dict().items()
        },
    }


def simulate_dynamic(
    dynamic_model: DynamicModelLike,
    schedule: CommandSchedule,
    duration_s: float,
    dt_s: float,
    *,
    fingerprint: str,
    versions: Mapping[str, str],
    conservation_tolerances: DynamicConservationTolerances | None = None,
    metadata: Mapping[str, str] | None = None,
) -> DynamicSimulationResult:
    """Run RK4 on a fixed nominal output grid and retain valid endpoints.

    Event starts and pulse ends deterministically split that grid; each resulting
    RK4 substep uses one fixed substep length internally. Component inlet/outlet and
    salt rates are augmented states, keeping cumulative diagnostics on the same
    quadrature and command schedule as the physical state. Runtime model errors
    become an explicit failed result; invalid states are never clipped or committed.
    """

    duration = _finite_number(duration_s, context="duration_s")
    step_size = _finite_number(dt_s, context="dt_s")
    if duration <= 0.0 or step_size <= 0.0:
        raise ValueError("duration_s and dt_s must be positive")
    if step_size > duration:
        raise ValueError("dt_s cannot exceed duration_s")
    step_ratio = duration / step_size
    requested_steps = round(step_ratio)
    if not math.isclose(
        step_ratio,
        requested_steps,
        rel_tol=0.0,
        abs_tol=1e-12 * max(1.0, abs(step_ratio)),
    ):
        raise ValueError("duration_s must be an integer multiple of dt_s")
    if not _SHA256_PATTERN.fullmatch(fingerprint):
        raise ValueError("fingerprint must be a lowercase SHA-256 digest")
    if not isinstance(schedule, CommandSchedule):
        raise TypeError("schedule must be a CommandSchedule")
    initial_state = dynamic_model.initial_state
    if not isinstance(initial_state, DynamicState):
        raise TypeError("dynamic_model.initial_state must be a DynamicState")
    model_commands = _finite_command_mapping(
        dynamic_model.baseline_commands,
        context="dynamic model baseline commands",
    )
    if set(model_commands) != set(schedule.baseline_commands):
        raise ValueError("schedule targets must exactly match dynamic model commands")
    copied_metadata = _traceable_metadata(metadata)
    copied_versions = _traceable_versions(
        versions,
        scenario_version=copied_metadata["scenario_version"],
    )
    selected_tolerances = (
        DynamicConservationTolerances.from_dynamic_model(dynamic_model)
        if conservation_tolerances is None
        else conservation_tolerances
    )
    if not isinstance(selected_tolerances, DynamicConservationTolerances):
        raise TypeError(
            "conservation_tolerances must be DynamicConservationTolerances"
        )
    input_fingerprint = canonical_fingerprint(
        {
            "stage": "M3",
            "source_fingerprint": fingerprint,
            "versions": copied_versions,
            "duration_s": duration,
            "dt_s": step_size,
            "initial_state": initial_state.as_dict(),
            "schedule": _schedule_payload(schedule),
            "conservation_tolerances": selected_tolerances.as_dict(),
            "metadata": copied_metadata,
        }
    )
    state_length = len(initial_state.to_vector())
    initial_components = _inventory_components(initial_state)
    initial_salt = _inventory_salt(initial_state)
    samples: list[DynamicSample] = []
    discontinuities = _schedule_discontinuities(
        schedule,
        duration_s=duration,
    )
    nominal_endpoints = _nominal_endpoints(
        duration_s=duration,
        dt_s=step_size,
        requested_steps=requested_steps,
    )
    integration_endpoints = _integration_endpoints(
        duration_s=duration,
        dt_s=step_size,
        requested_steps=requested_steps,
        discontinuities=discontinuities,
    )

    def finish(
        status: str,
        *,
        failure_reason: str | None = None,
        failure_stage: str | None = None,
        failure_time_s: float | None = None,
    ) -> DynamicSimulationResult:
        frozen_samples = tuple(samples)
        return DynamicSimulationResult(
            status=status,
            samples=frozen_samples,
            balance=_build_balance(initial_state, frozen_samples),
            conservation_tolerances=selected_tolerances,
            diagnostics=_result_diagnostics(
                frozen_samples,
                nominal_endpoints=nominal_endpoints,
                integration_endpoints=integration_endpoints,
                tolerances=selected_tolerances,
            ),
            versions=copied_versions,
            metadata=copied_metadata,
            source_fingerprint=fingerprint,
            input_fingerprint=input_fingerprint,
            requested_duration_s=duration,
            time_step_s=step_size,
            failure_reason=failure_reason,
            failure_stage=failure_stage,
            failure_time_s=failure_time_s,
        )

    try:
        initial_commands = schedule.values_at(0.0)
        _, initial_evaluation, _, initial_residuals = _evaluation_and_rates(
            dynamic_model,
            initial_state,
            initial_commands,
            capture_payload=True,
            tolerances=selected_tolerances,
        )
        initial_sample = _make_sample(
            time_s=0.0,
            state=initial_state,
            commands=initial_commands,
            evaluation=initial_evaluation,
            cumulative_rates=(0.0,) * (2 * len(ALL_COMPONENTS) + 2),
            initial_component_inventory_kg=initial_components,
            initial_inventory_salt_kg=initial_salt,
            instantaneous_residuals=initial_residuals,
        )
        _require_cumulative_conservation(initial_sample, selected_tolerances)
        samples.append(initial_sample)
    except DynamicConservationError as exc:
        return finish(
            "failed",
            failure_reason=f"{type(exc).__name__}: {exc}",
            failure_stage="conservation",
            failure_time_s=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - runtime failures are result data
        return finish(
            "failed",
            failure_reason=f"{type(exc).__name__}: {exc}",
            failure_stage="initial_evaluation",
            failure_time_s=0.0,
        )

    augmented_state = initial_state.to_vector() + (0.0,) * (
        2 * len(ALL_COMPONENTS) + 2
    )
    active_stage_time = 0.0
    start_time = 0.0
    for end_time in integration_endpoints:
        substep_size = end_time - start_time
        endpoint_is_discontinuity = end_time in discontinuities
        stage_call_index = 0

        def augmented_rhs(
            stage_time_s: float,
            stage_values: Sequence[float],
            *,
            discontinuity_time_s: float = end_time,
            use_left_limit_at_endpoint: bool = endpoint_is_discontinuity,
        ) -> tuple[float, ...]:
            nonlocal active_stage_time, stage_call_index
            stage_call_index += 1
            active_stage_time = stage_time_s
            physical_vector = tuple(stage_values[:state_length])
            stage_state = DynamicState.from_vector(physical_vector)
            stage_commands = (
                _commands_before(schedule, discontinuity_time_s)
                if use_left_limit_at_endpoint and stage_call_index == 4
                else schedule.values_at(stage_time_s)
            )
            evaluation, _, boundary_rates, _ = _evaluation_and_rates(
                dynamic_model,
                stage_state,
                stage_commands,
                capture_payload=False,
                tolerances=selected_tolerances,
            )
            slopes = tuple(
                _finite_number(value, context=f"state derivative {index}")
                for index, value in enumerate(evaluation.derivative_vector)
            )
            if len(slopes) != state_length:
                raise ValueError(
                    f"dynamic RHS returned {len(slopes)} values; expected {state_length}"
                )
            return slopes + boundary_rates

        try:
            augmented_state = rk4_step(
                augmented_rhs,
                start_time,
                augmented_state,
                substep_size,
            )
            active_stage_time = end_time
            endpoint_state = DynamicState.from_vector(augmented_state[:state_length])
            endpoint_commands = schedule.values_at(end_time)
            (
                _,
                endpoint_evaluation,
                _,
                endpoint_residuals,
            ) = _evaluation_and_rates(
                dynamic_model,
                endpoint_state,
                endpoint_commands,
                capture_payload=True,
                tolerances=selected_tolerances,
            )
            endpoint_sample = _make_sample(
                time_s=end_time,
                state=endpoint_state,
                commands=endpoint_commands,
                evaluation=endpoint_evaluation,
                cumulative_rates=augmented_state[state_length:],
                initial_component_inventory_kg=initial_components,
                initial_inventory_salt_kg=initial_salt,
                instantaneous_residuals=endpoint_residuals,
            )
            _require_cumulative_conservation(endpoint_sample, selected_tolerances)
            samples.append(endpoint_sample)
            start_time = end_time
        except DynamicConservationError as exc:
            return finish(
                "failed",
                failure_reason=f"{type(exc).__name__}: {exc}",
                failure_stage="conservation",
                failure_time_s=active_stage_time,
            )
        except Exception as exc:  # noqa: BLE001 - retain the last valid endpoint
            return finish(
                "failed",
                failure_reason=f"{type(exc).__name__}: {exc}",
                failure_stage="integration",
                failure_time_s=active_stage_time,
            )
    return finish("success")
