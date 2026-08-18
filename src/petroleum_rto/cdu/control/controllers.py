"""Immutable normalized digital PI controller contracts for the M4 control layer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

ControlAction = Literal["direct", "reverse"]
ControlMode = Literal["automatic", "manual"]

DIRECT: Final[ControlAction] = "direct"
REVERSE: Final[ControlAction] = "reverse"
AUTOMATIC: Final[ControlMode] = "automatic"
MANUAL: Final[ControlMode] = "manual"


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


def _control_action(value: object) -> ControlAction:
    if value not in (DIRECT, REVERSE):
        raise ValueError("action must be 'direct' or 'reverse'")
    return value


def _control_mode(value: object, *, context: str = "mode") -> ControlMode:
    if value not in (AUTOMATIC, MANUAL):
        raise ValueError(f"{context} must be 'automatic' or 'manual'")
    return value


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


@dataclass(frozen=True)
class PIControllerSpec:
    """Dimensionless tuning and limiter settings for one digital PI controller."""

    loop_id: str
    action: ControlAction
    proportional_gain: float
    integral_time_s: float
    anti_windup_time_s: float
    setpoint_rate_limit_fraction_per_s: float
    output_min_ratio: float
    output_max_ratio: float
    output_rate_limit_fraction_per_s: float
    initial_mode: ControlMode = AUTOMATIC

    def __post_init__(self) -> None:
        if not isinstance(self.loop_id, str) or not self.loop_id.strip():
            raise ValueError("loop_id must be a non-empty string")
        action = _control_action(self.action)
        mode = _control_mode(self.initial_mode, context="initial_mode")
        proportional_gain = _positive_number(
            self.proportional_gain,
            context="proportional_gain",
        )
        integral_time = _positive_number(
            self.integral_time_s,
            context="integral_time_s",
        )
        anti_windup_time = _positive_number(
            self.anti_windup_time_s,
            context="anti_windup_time_s",
        )
        setpoint_rate = _positive_number(
            self.setpoint_rate_limit_fraction_per_s,
            context="setpoint_rate_limit_fraction_per_s",
        )
        output_min = _finite_number(self.output_min_ratio, context="output_min_ratio")
        output_max = _finite_number(self.output_max_ratio, context="output_max_ratio")
        output_rate = _positive_number(
            self.output_rate_limit_fraction_per_s,
            context="output_rate_limit_fraction_per_s",
        )
        if output_min < 0.0:
            raise ValueError("output_min_ratio must be non-negative")
        if output_min >= output_max:
            raise ValueError("output_min_ratio must be less than output_max_ratio")
        if not output_min < 1.0 < output_max:
            raise ValueError(
                "normalized output limits must strictly bracket the nominal ratio 1.0"
            )
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "initial_mode", mode)
        object.__setattr__(self, "proportional_gain", proportional_gain)
        object.__setattr__(self, "integral_time_s", integral_time)
        object.__setattr__(self, "anti_windup_time_s", anti_windup_time)
        object.__setattr__(
            self,
            "setpoint_rate_limit_fraction_per_s",
            setpoint_rate,
        )
        object.__setattr__(self, "output_min_ratio", output_min)
        object.__setattr__(self, "output_max_ratio", output_max)
        object.__setattr__(self, "output_rate_limit_fraction_per_s", output_rate)

    @property
    def signed_proportional_gain(self) -> float:
        """Return the gain sign for the convention ``error = setpoint - PV``."""

        return self.proportional_gain if self.action == DIRECT else -self.proportional_gain


@dataclass(frozen=True)
class PIControllerState:
    """Minimal persistent state advanced exactly once per digital control interval."""

    ramped_setpoint: float
    previous_target_setpoint: float
    integral_term_normalized: float
    output_normalized: float
    previous_error_normalized: float
    previous_tracking_error_normalized: float
    mode: ControlMode

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ramped_setpoint",
            _finite_number(self.ramped_setpoint, context="ramped_setpoint"),
        )
        object.__setattr__(
            self,
            "previous_target_setpoint",
            _finite_number(
                self.previous_target_setpoint,
                context="previous_target_setpoint",
            ),
        )
        object.__setattr__(
            self,
            "integral_term_normalized",
            _finite_number(
                self.integral_term_normalized,
                context="integral_term_normalized",
            ),
        )
        object.__setattr__(
            self,
            "output_normalized",
            _finite_number(self.output_normalized, context="output_normalized"),
        )
        object.__setattr__(
            self,
            "previous_error_normalized",
            _finite_number(
                self.previous_error_normalized,
                context="previous_error_normalized",
            ),
        )
        object.__setattr__(
            self,
            "previous_tracking_error_normalized",
            _finite_number(
                self.previous_tracking_error_normalized,
                context="previous_tracking_error_normalized",
            ),
        )
        object.__setattr__(self, "mode", _control_mode(self.mode))


@dataclass(frozen=True)
class PIControllerUpdate:
    """One immutable PI decision and the state to use at the next control tick."""

    state: PIControllerState
    process_value: float
    target_setpoint: float
    error_normalized: float
    proportional_term_normalized: float
    feedforward_normalized: float
    unconstrained_output_normalized: float
    magnitude_limited_output_normalized: float
    output_normalized: float
    output: float
    integral_rate_normalized_per_s: float
    limited_by_magnitude: bool
    limited_by_rate: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state, PIControllerState):
            raise TypeError("state must be a PIControllerState")
        for name in (
            "process_value",
            "target_setpoint",
            "error_normalized",
            "proportional_term_normalized",
            "feedforward_normalized",
            "unconstrained_output_normalized",
            "magnitude_limited_output_normalized",
            "output_normalized",
            "output",
            "integral_rate_normalized_per_s",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(getattr(self, name), context=name),
            )
        if not isinstance(self.limited_by_magnitude, bool):
            raise TypeError("limited_by_magnitude must be a boolean")
        if not isinstance(self.limited_by_rate, bool):
            raise TypeError("limited_by_rate must be a boolean")

    @property
    def saturated(self) -> bool:
        return self.limited_by_magnitude or self.limited_by_rate


@dataclass(frozen=True)
class NormalizedPIController:
    """Pure digital PI controller using nominal PV and output scales.

    ``update`` advances the integral and setpoint-ramp states once. The caller must
    hold the returned output between control ticks and must not invoke this method
    from RK4 stage evaluations.
    """

    spec: PIControllerSpec
    pv_scale: float
    output_scale: float
    bias_normalized: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.spec, PIControllerSpec):
            raise TypeError("spec must be a PIControllerSpec")
        object.__setattr__(
            self,
            "pv_scale",
            _positive_number(self.pv_scale, context="pv_scale"),
        )
        object.__setattr__(
            self,
            "output_scale",
            _positive_number(self.output_scale, context="output_scale"),
        )
        object.__setattr__(
            self,
            "bias_normalized",
            _finite_number(self.bias_normalized, context="bias_normalized"),
        )

    def initialize(
        self,
        *,
        process_value: float,
        output: float,
        setpoint: float | None = None,
        mode: ControlMode | None = None,
        feedforward_output: float = 0.0,
    ) -> PIControllerState:
        """Return a state whose first automatic output exactly matches ``output``."""

        pv = _finite_number(process_value, context="process_value")
        selected_setpoint = (
            pv if setpoint is None else _finite_number(setpoint, context="setpoint")
        )
        selected_mode = self.spec.initial_mode if mode is None else _control_mode(mode)
        output_value = _finite_number(output, context="output")
        output_normalized = output_value / self.output_scale
        if not self.spec.output_min_ratio <= output_normalized <= self.spec.output_max_ratio:
            raise ValueError("initial output is outside the configured normalized limits")
        feedforward_normalized = (
            _finite_number(feedforward_output, context="feedforward_output")
            / self.output_scale
        )
        error_normalized = (selected_setpoint - pv) / self.pv_scale
        proportional = self.spec.signed_proportional_gain * error_normalized
        integral = (
            output_normalized
            - self.bias_normalized
            - feedforward_normalized
            - proportional
        )
        return PIControllerState(
            ramped_setpoint=selected_setpoint,
            previous_target_setpoint=selected_setpoint,
            integral_term_normalized=integral,
            output_normalized=output_normalized,
            previous_error_normalized=0.0,
            previous_tracking_error_normalized=0.0,
            mode=selected_mode,
        )

    def update(
        self,
        state: PIControllerState,
        *,
        process_value: float,
        target_setpoint: float,
        dt_s: float,
        requested_mode: ControlMode | None = None,
        manual_output: float | None = None,
        feedforward_output: float = 0.0,
    ) -> PIControllerUpdate:
        """Advance one control interval with final-output back-calculation."""

        if not isinstance(state, PIControllerState):
            raise TypeError("state must be a PIControllerState")
        if not self.spec.output_min_ratio <= state.output_normalized <= self.spec.output_max_ratio:
            raise ValueError("controller state output is outside configured limits")
        pv = _finite_number(process_value, context="process_value")
        target = _finite_number(target_setpoint, context="target_setpoint")
        dt = _finite_number(dt_s, context="dt_s")
        if dt < 0.0:
            raise ValueError("dt_s must be non-negative")
        selected_mode = (
            state.mode if requested_mode is None else _control_mode(requested_mode)
        )
        if selected_mode == AUTOMATIC and manual_output is not None:
            raise ValueError("manual_output can only be supplied in manual mode")
        feedforward_normalized = (
            _finite_number(feedforward_output, context="feedforward_output")
            / self.output_scale
        )

        # Advance states over the interval that has already elapsed using only
        # information known at its beginning.  A target arriving at this call is
        # stored for the *next* interval; it cannot leak into the past interval.
        elapsed_integral_rate = (
            self.spec.signed_proportional_gain
            * state.previous_error_normalized
            / self.spec.integral_time_s
            + state.previous_tracking_error_normalized
            / self.spec.anti_windup_time_s
        )
        integral_after_elapsed_interval = (
            state.integral_term_normalized + dt * elapsed_integral_rate
        )
        maximum_setpoint_change = (
            self.spec.setpoint_rate_limit_fraction_per_s * self.pv_scale * dt
        )
        ramped_setpoint = state.ramped_setpoint + _clamp(
            state.previous_target_setpoint - state.ramped_setpoint,
            -maximum_setpoint_change,
            maximum_setpoint_change,
        )
        error_normalized = (ramped_setpoint - pv) / self.pv_scale
        proportional = self.spec.signed_proportional_gain * error_normalized

        if selected_mode == AUTOMATIC:
            integral_before_update = integral_after_elapsed_interval
            if state.mode == MANUAL:
                integral_before_update = (
                    state.output_normalized
                    - self.bias_normalized
                    - feedforward_normalized
                    - proportional
                )
            unconstrained = (
                self.bias_normalized
                + feedforward_normalized
                + proportional
                + integral_before_update
            )
            magnitude_limited, output_normalized, magnitude_flag, rate_flag = (
                self._limited_output(
                    unconstrained,
                    previous_output_normalized=state.output_normalized,
                    dt_s=dt,
                )
            )
            integral_after_update = integral_before_update
        else:
            requested_manual_output = (
                state.output_normalized
                if manual_output is None
                else _finite_number(manual_output, context="manual_output")
                / self.output_scale
            )
            unconstrained = requested_manual_output
            magnitude_limited, output_normalized, magnitude_flag, rate_flag = (
                self._limited_output(
                    unconstrained,
                    previous_output_normalized=state.output_normalized,
                    dt_s=dt,
                )
            )
            integral_after_update = (
                output_normalized
                - self.bias_normalized
                - feedforward_normalized
                - proportional
            )

        next_state = PIControllerState(
            ramped_setpoint=ramped_setpoint,
            previous_target_setpoint=target,
            integral_term_normalized=integral_after_update,
            output_normalized=output_normalized,
            previous_error_normalized=error_normalized,
            previous_tracking_error_normalized=(
                output_normalized - unconstrained
                if selected_mode == AUTOMATIC
                else 0.0
            ),
            mode=selected_mode,
        )
        return PIControllerUpdate(
            state=next_state,
            process_value=pv,
            target_setpoint=target,
            error_normalized=error_normalized,
            proportional_term_normalized=proportional,
            feedforward_normalized=feedforward_normalized,
            unconstrained_output_normalized=unconstrained,
            magnitude_limited_output_normalized=magnitude_limited,
            output_normalized=output_normalized,
            output=output_normalized * self.output_scale,
            integral_rate_normalized_per_s=elapsed_integral_rate,
            limited_by_magnitude=magnitude_flag,
            limited_by_rate=rate_flag,
        )

    def _limited_output(
        self,
        requested_output_normalized: float,
        *,
        previous_output_normalized: float,
        dt_s: float,
    ) -> tuple[float, float, bool, bool]:
        magnitude_limited = _clamp(
            requested_output_normalized,
            self.spec.output_min_ratio,
            self.spec.output_max_ratio,
        )
        maximum_output_change = self.spec.output_rate_limit_fraction_per_s * dt_s
        output_normalized = _clamp(
            magnitude_limited,
            previous_output_normalized - maximum_output_change,
            previous_output_normalized + maximum_output_change,
        )
        return (
            magnitude_limited,
            output_normalized,
            magnitude_limited != requested_output_normalized,
            output_normalized != magnitude_limited,
        )
