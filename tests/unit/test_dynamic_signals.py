from __future__ import annotations

import math
from collections.abc import MutableMapping
from typing import cast

import pytest

from petroleum_rto.cdu.core.math_utils import rk4_step
from petroleum_rto.cdu.dynamics.actuators import ActuatorSpec
from petroleum_rto.cdu.dynamics.schedule import CommandEvent, CommandSchedule
from petroleum_rto.cdu.dynamics.sensors import SensorSpec


def test_actuator_direction_target_clamp_rate_limit_and_failure() -> None:
    unrestricted = ActuatorSpec(time_constant_s=2.0, lower=0.0, upper=10.0)
    limited = ActuatorSpec(
        time_constant_s=1.0,
        lower=0.0,
        upper=10.0,
        rate_limit_per_s=2.0,
    )

    assert unrestricted.derivative(2.0, 8.0) == pytest.approx(3.0)
    assert unrestricted.derivative(8.0, 2.0) == pytest.approx(-3.0)
    assert unrestricted.derivative(5.0, 20.0) == pytest.approx(2.5)
    assert limited.derivative(0.0, 10.0) == pytest.approx(2.0)
    assert limited.derivative(5.0, 9.0, failure_position=-5.0) == pytest.approx(-2.0)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ActuatorSpec(0.0, 0.0, 1.0),
        lambda: ActuatorSpec(1.0, 2.0, 1.0),
        lambda: ActuatorSpec(1.0, 0.0, 1.0, 0.0),
        lambda: ActuatorSpec(math.nan, 0.0, 1.0),
    ],
)
def test_actuator_rejects_invalid_specifications(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_actuator_rejects_invalid_actual_and_nonfinite_inputs() -> None:
    actuator = ActuatorSpec(1.0, 0.0, 1.0)

    with pytest.raises(ValueError, match="outside"):
        actuator.derivative(1.1, 0.5)
    with pytest.raises(ValueError, match="finite"):
        actuator.derivative(math.nan, 0.5)
    with pytest.raises(ValueError, match="finite"):
        actuator.derivative(0.5, math.inf)
    with pytest.raises(ValueError, match="finite"):
        actuator.derivative(0.5, 0.5, failure_position=math.nan)


def test_sensor_direction_hold_and_invalid_values() -> None:
    sensor = SensorSpec(time_constant_s=5.0)

    assert sensor.derivative(0.0, 10.0) == pytest.approx(2.0)
    assert sensor.derivative(10.0, 0.0) == pytest.approx(-2.0)
    assert sensor.derivative(1.0, 10.0, held=True) == 0.0
    with pytest.raises(ValueError, match="finite"):
        sensor.derivative(math.nan, 1.0)
    with pytest.raises(ValueError, match="finite"):
        sensor.derivative(1.0, math.inf, held=True)
    with pytest.raises(ValueError, match="positive"):
        SensorSpec(0.0)


def test_first_order_sensor_reaches_sixty_three_percent_after_one_time_constant() -> None:
    sensor = SensorSpec(time_constant_s=10.0)
    state: tuple[float, ...] = (0.0,)
    time_s = 0.0
    dt_s = 0.1

    for _ in range(100):
        state = rk4_step(
            lambda _time, values: (sensor.derivative(values[0], 1.0),),
            time_s,
            state,
            dt_s,
        )
        time_s += dt_s

    assert state[0] == pytest.approx(1.0 - math.exp(-1.0), abs=1e-9)


def test_command_schedule_steps_pulses_order_and_freezing() -> None:
    schedule = CommandSchedule(
        baseline_commands={"feed": 1.0, "duty": 2.0},
        events=(
            CommandEvent(40.0, "feed", 1.3),
            CommandEvent(10.0, "feed", 1.2),
            CommandEvent(20.0, "feed", 0.8, duration_s=5.0),
            CommandEvent(40.0, "feed", 1.4),
        ),
    )

    assert tuple(event.time_s for event in schedule.events) == (10.0, 20.0, 40.0, 40.0)
    assert dict(schedule.values_at(0.0)) == {"feed": 1.0, "duty": 2.0}
    assert schedule.values_at(10.0)["feed"] == 1.2
    assert schedule.values_at(20.0)["feed"] == 0.8
    assert schedule.values_at(24.999)["feed"] == 0.8
    assert schedule.values_at(25.0)["feed"] == 1.2
    assert schedule.values_at(40.0)["feed"] == 1.4
    assert schedule.values_at(40.0) == schedule.values_at(40.0)
    frozen_baseline = cast(MutableMapping[str, float], schedule.baseline_commands)
    frozen_values = cast(MutableMapping[str, float], schedule.values_at(0.0))
    with pytest.raises(TypeError):
        frozen_baseline["feed"] = 9.0
    with pytest.raises(TypeError):
        frozen_values["feed"] = 9.0


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CommandEvent(-1.0, "feed", 1.0),
        lambda: CommandEvent(0.0, "", 1.0),
        lambda: CommandEvent(0.0, "feed", math.nan),
        lambda: CommandEvent(0.0, "feed", -1.0),
        lambda: CommandEvent(0.0, "feed", 1.0, duration_s=0.0),
        lambda: CommandSchedule({}),
        lambda: CommandSchedule({"feed": math.inf}),
        lambda: CommandSchedule({"feed": -1.0}),
        lambda: CommandSchedule(
            {"feed": 1.0},
            (CommandEvent(0.0, "unknown", 1.0),),
        ),
    ],
)
def test_command_contracts_reject_invalid_inputs(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_schedule_rejects_invalid_query_time() -> None:
    schedule = CommandSchedule({"feed": 1.0})

    with pytest.raises(ValueError):
        schedule.values_at(-1.0)
    with pytest.raises(ValueError):
        schedule.values_at(math.nan)
