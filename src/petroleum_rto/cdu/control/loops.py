"""Pure assembly and signal routing for the seven M4 primary PI loops."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ..dynamics.state import ACTUATOR_STATE_NAMES, DynamicState
from .config import (
    REQUIRED_CONTROL_LOOP_IDS,
    ControlConfig,
    ControlledVariableSource,
    FeedforwardKind,
)
from .controllers import (
    NormalizedPIController,
    PIControllerState,
    PIControllerUpdate,
)

_INVENTORY_LOOP_TO_STATE_NAME: Final[Mapping[str, str]] = MappingProxyType(
    {
        "flash_inventory": "flash_drum",
        "reflux_inventory": "reflux_drum",
        "bottom_inventory": "tower_bottom",
    }
)


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def _positive_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number <= 0.0:
        raise ValueError(f"{context} must be positive")
    return number


def _nonnegative_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number < 0.0:
        raise ValueError(f"{context} must be non-negative")
    return number


def _exact_numeric_mapping(
    values: Mapping[str, float],
    expected_names: tuple[str, ...],
    *,
    context: str,
    strictly_positive: bool,
) -> Mapping[str, float]:
    if any(not isinstance(name, str) for name in values):
        raise TypeError(f"{context} keys must be strings")
    actual = set(values)
    expected = set(expected_names)
    if actual != expected:
        raise ValueError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    copied: dict[str, float] = {}
    for name in expected_names:
        copied[name] = (
            _positive_number(values[name], context=f"{context}.{name}")
            if strictly_positive
            else _nonnegative_number(values[name], context=f"{context}.{name}")
        )
    return MappingProxyType(copied)


@dataclass(frozen=True)
class FurnaceFeedforward:
    """Nominally unbiased furnace-duty feedforward from actual furnace feed."""

    nominal_feed_flow_kg_s: float
    nominal_fuel_duty_w: float
    efficiency: float
    heat_loss_w: float

    def __post_init__(self) -> None:
        nominal_feed = _positive_number(
            self.nominal_feed_flow_kg_s,
            context="nominal_feed_flow_kg_s",
        )
        nominal_fuel = _positive_number(
            self.nominal_fuel_duty_w,
            context="nominal_fuel_duty_w",
        )
        efficiency = _positive_number(self.efficiency, context="efficiency")
        if efficiency > 1.0:
            raise ValueError("efficiency cannot exceed one")
        heat_loss = _nonnegative_number(self.heat_loss_w, context="heat_loss_w")
        if efficiency * nominal_fuel < heat_loss:
            raise ValueError("nominal fuel duty is below the furnace heat-loss threshold")
        object.__setattr__(self, "nominal_feed_flow_kg_s", nominal_feed)
        object.__setattr__(self, "nominal_fuel_duty_w", nominal_fuel)
        object.__setattr__(self, "efficiency", efficiency)
        object.__setattr__(self, "heat_loss_w", heat_loss)

    def delta_duty_w(self, actual_feed_flow_kg_s: float) -> float:
        """Return ``Qff - Q0`` using the actual flash-liquid outlet flow."""

        actual_feed = _positive_number(
            actual_feed_flow_kg_s,
            context="actual_feed_flow_kg_s",
        )
        nominal_process_duty = (
            self.efficiency * self.nominal_fuel_duty_w - self.heat_loss_w
        )
        feed_ratio = actual_feed / self.nominal_feed_flow_kg_s
        feedforward_duty = (
            self.heat_loss_w + nominal_process_duty * feed_ratio
        ) / self.efficiency
        delta = feedforward_duty - self.nominal_fuel_duty_w
        if not math.isfinite(delta):  # pragma: no cover - guarded operands above
            raise ValueError("furnace feedforward duty must be finite")
        return delta

    def as_dict(self) -> dict[str, float]:
        return {
            "nominal_feed_flow_kg_s": self.nominal_feed_flow_kg_s,
            "nominal_fuel_duty_w": self.nominal_fuel_duty_w,
            "efficiency": self.efficiency,
            "heat_loss_w": self.heat_loss_w,
        }


@dataclass(frozen=True)
class LoopInitializationDiagnostic:
    """Auditable nominal signal values used to initialize one PI controller."""

    loop_id: str
    process_value: float
    target_setpoint: float
    target_setpoint_ratio: float
    output: float
    output_ratio: float
    feedforward_output: float
    initial_command_delta: float

    def __post_init__(self) -> None:
        if not isinstance(self.loop_id, str) or not self.loop_id.strip():
            raise ValueError("loop_id must be a non-empty string")
        for name in (
            "process_value",
            "target_setpoint",
            "target_setpoint_ratio",
            "output",
            "output_ratio",
            "feedforward_output",
            "initial_command_delta",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(getattr(self, name), context=name),
            )
        if self.process_value <= 0.0 or self.target_setpoint <= 0.0:
            raise ValueError("initial process value and setpoint must be positive")
        if self.target_setpoint_ratio <= 0.0 or self.output_ratio <= 0.0:
            raise ValueError("initial setpoint and output ratios must be positive")
        if self.output <= 0.0:
            raise ValueError("initial output must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "loop_id": self.loop_id,
            "process_value": self.process_value,
            "target_setpoint": self.target_setpoint,
            "target_setpoint_ratio": self.target_setpoint_ratio,
            "output": self.output,
            "output_ratio": self.output_ratio,
            "feedforward_output": self.feedforward_output,
            "initial_command_delta": self.initial_command_delta,
        }


@dataclass(frozen=True)
class AssembledControlLoop:
    """One configured controller plus its frozen M3 nominal basis."""

    loop_id: str
    controlled_variable_source: ControlledVariableSource
    controlled_variable_name: str
    manipulated_variable: str
    feedforward: FeedforwardKind
    controller: NormalizedPIController
    nominal_process_value: float
    nominal_output: float
    initial_state: PIControllerState
    initial_diagnostic: LoopInitializationDiagnostic

    def __post_init__(self) -> None:
        for name in ("loop_id", "controlled_variable_name", "manipulated_variable"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.controlled_variable_source not in ("actuator", "sensor"):
            raise ValueError("controlled_variable_source must be actuator or sensor")
        if self.feedforward not in ("none", "furnace_feed_flow"):
            raise ValueError("unsupported feedforward kind")
        if not isinstance(self.controller, NormalizedPIController):
            raise TypeError("controller must be a NormalizedPIController")
        if self.controller.spec.loop_id != self.loop_id:
            raise ValueError("controller spec loop_id must match the assembled loop")
        nominal_process_value = _positive_number(
            self.nominal_process_value,
            context=f"{self.loop_id}.nominal_process_value",
        )
        nominal_output = _positive_number(
            self.nominal_output,
            context=f"{self.loop_id}.nominal_output",
        )
        if not isinstance(self.initial_state, PIControllerState):
            raise TypeError("initial_state must be a PIControllerState")
        if not isinstance(self.initial_diagnostic, LoopInitializationDiagnostic):
            raise TypeError(
                "initial_diagnostic must be a LoopInitializationDiagnostic"
            )
        if self.initial_diagnostic.loop_id != self.loop_id:
            raise ValueError("initial diagnostic loop_id must match the assembled loop")
        object.__setattr__(self, "nominal_process_value", nominal_process_value)
        object.__setattr__(self, "nominal_output", nominal_output)


@dataclass(frozen=True)
class ControlLoopAssembly:
    """Immutable M4 signal-routing layer assembled from one M3 nominal point."""

    loops: Mapping[str, AssembledControlLoop]
    baseline_commands: Mapping[str, float]
    manipulated_variable_owners: Mapping[str, str]
    nominal_inventory_masses_kg: Mapping[str, float]
    furnace_feedforward: FurnaceFeedforward

    def __post_init__(self) -> None:
        if set(self.loops) != set(REQUIRED_CONTROL_LOOP_IDS):
            raise ValueError("assembled loops must contain exactly the seven M4 loop ids")
        frozen_loops: dict[str, AssembledControlLoop] = {}
        for loop_id in REQUIRED_CONTROL_LOOP_IDS:
            loop = self.loops[loop_id]
            if not isinstance(loop, AssembledControlLoop):
                raise TypeError("loop values must be AssembledControlLoop instances")
            if loop.loop_id != loop_id:
                raise ValueError("assembled loop key must match loop_id")
            frozen_loops[loop_id] = loop

        baseline = _exact_numeric_mapping(
            self.baseline_commands,
            ACTUATOR_STATE_NAMES,
            context="baseline_commands",
            strictly_positive=False,
        )
        expected_owners = {
            loop.manipulated_variable: loop_id
            for loop_id, loop in frozen_loops.items()
        }
        if len(expected_owners) != len(frozen_loops):
            raise ValueError("each primary loop must own a unique manipulated variable")
        if dict(self.manipulated_variable_owners) != expected_owners:
            raise ValueError("manipulated_variable_owners is inconsistent with loops")
        inventories = _exact_numeric_mapping(
            self.nominal_inventory_masses_kg,
            tuple(_INVENTORY_LOOP_TO_STATE_NAME),
            context="nominal_inventory_masses_kg",
            strictly_positive=True,
        )
        if not isinstance(self.furnace_feedforward, FurnaceFeedforward):
            raise TypeError("furnace_feedforward must be a FurnaceFeedforward")
        object.__setattr__(self, "loops", MappingProxyType(frozen_loops))
        object.__setattr__(self, "baseline_commands", baseline)
        object.__setattr__(
            self,
            "manipulated_variable_owners",
            MappingProxyType(expected_owners),
        )
        object.__setattr__(self, "nominal_inventory_masses_kg", inventories)

    @property
    def initial_controller_states(self) -> Mapping[str, PIControllerState]:
        return MappingProxyType(
            {
                loop_id: self.loops[loop_id].initial_state
                for loop_id in REQUIRED_CONTROL_LOOP_IDS
            }
        )

    @property
    def initial_diagnostics(self) -> Mapping[str, LoopInitializationDiagnostic]:
        return MappingProxyType(
            {
                loop_id: self.loops[loop_id].initial_diagnostic
                for loop_id in REQUIRED_CONTROL_LOOP_IDS
            }
        )

    @property
    def initial_target_setpoint_ratios(self) -> Mapping[str, float]:
        """Return the nominal all-one ratio schedule for a new closed-loop run."""

        return MappingProxyType(
            {loop_id: 1.0 for loop_id in REQUIRED_CONTROL_LOOP_IDS}
        )

    def process_values(self, state: DynamicState) -> Mapping[str, float]:
        """Extract seven measured PVs without modifying the M3 state."""

        if not isinstance(state, DynamicState):
            raise TypeError("state must be a DynamicState")
        values: dict[str, float] = {}
        for loop_id in REQUIRED_CONTROL_LOOP_IDS:
            loop = self.loops[loop_id]
            source = (
                state.actuator_states
                if loop.controlled_variable_source == "actuator"
                else state.sensor_states
            )
            try:
                value = source[loop.controlled_variable_name]
            except KeyError as exc:  # pragma: no cover - frozen whitelist and state contract
                raise ValueError(
                    f"state does not contain PV {loop.controlled_variable_name!r}"
                ) from exc
            values[loop_id] = _finite_number(value, context=f"PV.{loop_id}")
        return MappingProxyType(values)

    def target_setpoints(
        self,
        target_setpoint_ratios: Mapping[str, float],
    ) -> Mapping[str, float]:
        """Convert one complete ratio schedule into absolute loop setpoints."""

        ratios = _exact_numeric_mapping(
            target_setpoint_ratios,
            REQUIRED_CONTROL_LOOP_IDS,
            context="target_setpoint_ratios",
            strictly_positive=True,
        )
        return MappingProxyType(
            {
                loop_id: ratios[loop_id]
                * self.loops[loop_id].nominal_process_value
                for loop_id in REQUIRED_CONTROL_LOOP_IDS
            }
        )

    def feedforward_outputs(self, state: DynamicState) -> Mapping[str, float]:
        """Return absolute additive controller feedforward outputs for all loops."""

        if not isinstance(state, DynamicState):
            raise TypeError("state must be a DynamicState")
        actual_furnace_feed = state.actuator_states["flash_liquid_outflow_kg_s"]
        furnace_delta = self.furnace_feedforward.delta_duty_w(actual_furnace_feed)
        return MappingProxyType(
            {
                loop_id: (
                    furnace_delta
                    if self.loops[loop_id].feedforward == "furnace_feed_flow"
                    else 0.0
                )
                for loop_id in REQUIRED_CONTROL_LOOP_IDS
            }
        )

    def commands_from_updates(
        self,
        updates: Mapping[str, PIControllerUpdate],
    ) -> Mapping[str, float]:
        """Apply seven unique PI outputs over the independent baseline commands."""

        if set(updates) != set(REQUIRED_CONTROL_LOOP_IDS):
            raise ValueError("updates must contain exactly the seven M4 loop ids")
        commands = dict(self.baseline_commands)
        for loop_id in REQUIRED_CONTROL_LOOP_IDS:
            update = updates[loop_id]
            if not isinstance(update, PIControllerUpdate):
                raise TypeError("updates must contain PIControllerUpdate values")
            loop = self.loops[loop_id]
            output = _nonnegative_number(
                update.output,
                context=f"updates.{loop_id}.output",
            )
            if not math.isclose(
                output,
                update.output_normalized * loop.nominal_output,
                rel_tol=1e-12,
                abs_tol=1e-12 * max(loop.nominal_output, 1.0),
            ):
                raise ValueError(
                    f"update for {loop_id!r} does not use its assembled output scale"
                )
            if not (
                loop.controller.spec.output_min_ratio
                <= update.output_normalized
                <= loop.controller.spec.output_max_ratio
            ):
                raise ValueError(f"update for {loop_id!r} is outside its output envelope")
            commands[loop.manipulated_variable] = output
        return MappingProxyType(commands)

    def true_inventory_ratios(self, state: DynamicState) -> Mapping[str, float]:
        """Return physical inventory ratios for safety gates, not lagged sensors."""

        if not isinstance(state, DynamicState):
            raise TypeError("state must be a DynamicState")
        return MappingProxyType(
            {
                loop_id: (
                    state.liquid_inventories[inventory_name].total_mass_kg
                    / self.nominal_inventory_masses_kg[loop_id]
                )
                for loop_id, inventory_name in _INVENTORY_LOOP_TO_STATE_NAME.items()
            }
        )


def assemble_control_loops(
    control_config: ControlConfig,
    initial_state: DynamicState,
    baseline_commands: Mapping[str, float],
    *,
    furnace_efficiency: float,
    furnace_heat_loss_w: float,
) -> ControlLoopAssembly:
    """Assemble seven PI controllers from one steady-consistent M3 nominal point."""

    if not isinstance(control_config, ControlConfig):
        raise TypeError("control_config must be a ControlConfig")
    if not isinstance(initial_state, DynamicState):
        raise TypeError("initial_state must be a DynamicState")
    baseline = _exact_numeric_mapping(
        baseline_commands,
        ACTUATOR_STATE_NAMES,
        context="baseline_commands",
        strictly_positive=False,
    )
    furnace_feedforward = FurnaceFeedforward(
        nominal_feed_flow_kg_s=baseline["flash_liquid_outflow_kg_s"],
        nominal_fuel_duty_w=baseline["furnace_fuel_duty_w"],
        efficiency=furnace_efficiency,
        heat_loss_w=furnace_heat_loss_w,
    )
    initial_furnace_delta = furnace_feedforward.delta_duty_w(
        initial_state.actuator_states["flash_liquid_outflow_kg_s"]
    )

    loops: dict[str, AssembledControlLoop] = {}
    owners: dict[str, str] = {}
    for loop_id in REQUIRED_CONTROL_LOOP_IDS:
        configured = control_config.loop(loop_id)
        manipulated_variable = configured.manipulated_variable
        nominal_output = _positive_number(
            baseline[manipulated_variable],
            context=f"baseline_commands.{manipulated_variable}",
        )
        actual_output = initial_state.actuator_states[manipulated_variable]
        if not math.isclose(
            actual_output,
            nominal_output,
            rel_tol=1e-12,
            abs_tol=1e-12 * max(nominal_output, 1.0),
        ):
            raise ValueError(
                f"initial actuator {manipulated_variable!r} must equal its baseline command"
            )
        if not (
            configured.output_min_ratio < 1.0 < configured.output_max_ratio
        ):
            raise ValueError(
                f"control loop {loop_id!r} must place nominal output strictly "
                "inside its output envelope"
            )

        source = (
            initial_state.actuator_states
            if configured.controlled_variable.source == "actuator"
            else initial_state.sensor_states
        )
        nominal_process_value = _positive_number(
            source[configured.controlled_variable.name],
            context=f"nominal_process_value.{loop_id}",
        )
        controller = NormalizedPIController(
            spec=configured.controller_spec(),
            pv_scale=nominal_process_value,
            output_scale=nominal_output,
        )
        feedforward_output = (
            initial_furnace_delta
            if configured.feedforward == "furnace_feed_flow"
            else 0.0
        )
        controller_state = controller.initialize(
            process_value=nominal_process_value,
            output=nominal_output,
            setpoint=nominal_process_value,
            feedforward_output=feedforward_output,
        )
        command_delta = nominal_output - actual_output
        diagnostic = LoopInitializationDiagnostic(
            loop_id=loop_id,
            process_value=nominal_process_value,
            target_setpoint=nominal_process_value,
            target_setpoint_ratio=1.0,
            output=nominal_output,
            output_ratio=controller_state.output_normalized,
            feedforward_output=feedforward_output,
            initial_command_delta=command_delta,
        )
        loops[loop_id] = AssembledControlLoop(
            loop_id=loop_id,
            controlled_variable_source=configured.controlled_variable.source,
            controlled_variable_name=configured.controlled_variable.name,
            manipulated_variable=manipulated_variable,
            feedforward=configured.feedforward,
            controller=controller,
            nominal_process_value=nominal_process_value,
            nominal_output=nominal_output,
            initial_state=controller_state,
            initial_diagnostic=diagnostic,
        )
        if manipulated_variable in owners:  # pragma: no cover - ControlConfig gate
            raise ValueError(
                f"manipulated variable {manipulated_variable!r} has multiple owners"
            )
        owners[manipulated_variable] = loop_id

    nominal_inventories = {
        loop_id: initial_state.liquid_inventories[inventory_name].total_mass_kg
        for loop_id, inventory_name in _INVENTORY_LOOP_TO_STATE_NAME.items()
    }
    return ControlLoopAssembly(
        loops=loops,
        baseline_commands=baseline,
        manipulated_variable_owners=owners,
        nominal_inventory_masses_kg=nominal_inventories,
        furnace_feedforward=furnace_feedforward,
    )
