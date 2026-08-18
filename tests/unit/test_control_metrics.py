from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.cdu.control.config import (
    REQUIRED_CONTROL_LOOP_IDS,
    ControlConfig,
    load_control_config,
)
from petroleum_rto.cdu.control.metrics import (
    _left_held_saturation_metrics,
    _time_weighted_mean,
    _window_points,
    evaluate_closed_loop_acceptance,
)
from petroleum_rto.cdu.control.results import ClosedLoopSample


@dataclass(frozen=True)
class _MetricPoint:
    time_s: float
    value: float


@dataclass(frozen=True)
class _MetricControlRecord:
    target_setpoint: float
    ramped_setpoint: float
    process_value: float
    error_normalized: float
    output: float
    mode: str
    saturated: bool


@dataclass(frozen=True)
class _MetricInventory:
    total_mass_kg: float


@dataclass(frozen=True)
class _MetricState:
    liquid_inventories: Mapping[str, _MetricInventory]


@dataclass(frozen=True)
class _MetricPlantSample:
    commands: Mapping[str, float]
    state: _MetricState


@dataclass(frozen=True)
class _MetricClosedLoopSample:
    time_s: float
    plant: _MetricPlantSample
    controls: Mapping[str, _MetricControlRecord]


@pytest.fixture
def control_config(repo_root: Path) -> ControlConfig:
    return load_control_config(
        repo_root / "configs/controllers/cdu_pi_v0.1.0.json"
    )


def _acceptance_samples(
    control_config: ControlConfig,
    feed_states: Sequence[tuple[float, bool, str]],
) -> tuple[ClosedLoopSample, ...]:
    commands = {
        control_config.loops[loop_id].manipulated_variable: 100.0
        for loop_id in REQUIRED_CONTROL_LOOP_IDS
    }
    state = _MetricState(
        {
            "flash_drum": _MetricInventory(100.0),
            "reflux_drum": _MetricInventory(100.0),
            "tower_bottom": _MetricInventory(100.0),
        }
    )
    samples: list[_MetricClosedLoopSample] = []
    for time_s, feed_saturated, feed_mode in feed_states:
        controls = {
            loop_id: _MetricControlRecord(
                target_setpoint=100.0,
                ramped_setpoint=100.0,
                process_value=100.0,
                error_normalized=0.0,
                output=commands[
                    control_config.loops[loop_id].manipulated_variable
                ],
                mode=feed_mode if loop_id == "feed_flow" else "automatic",
                saturated=feed_saturated if loop_id == "feed_flow" else False,
            )
            for loop_id in REQUIRED_CONTROL_LOOP_IDS
        }
        samples.append(
            _MetricClosedLoopSample(
                time_s=time_s,
                plant=_MetricPlantSample(commands, state),
                controls=controls,
            )
        )
    return cast(tuple[ClosedLoopSample, ...], tuple(samples))


def test_nonuniform_tail_window_is_interpolated_and_time_weighted() -> None:
    samples = (
        _MetricPoint(0.0, 0.0),
        _MetricPoint(1.0, 2.0),
        _MetricPoint(3.0, 2.0),
    )

    points = _window_points(
        samples,
        start_time_s=0.5,
        value=lambda sample: sample.value,
    )

    assert points == ((0.5, 1.0), (1.0, 2.0), (3.0, 2.0))
    assert _time_weighted_mean(points) == pytest.approx(1.9)


def test_tail_saturation_uses_left_held_interval_crossing_window_start(
    control_config: ControlConfig,
) -> None:
    feed_states = (
        (0.0, False, "automatic"),
        (103.0, True, "automatic"),
        (104.0, False, "automatic"),
        (703.5, False, "automatic"),
    )
    assert not any(
        saturated for time_s, saturated, _ in feed_states if time_s >= 103.5
    )
    tail_total, tail_longest, tail_contains_saturation = (
        _left_held_saturation_metrics(
            tuple((time_s, saturated) for time_s, saturated, _ in feed_states),
            start_time_s=103.5,
            duration_s=703.5,
        )
    )
    assert tail_total == pytest.approx(0.5)
    assert tail_longest == pytest.approx(0.5)
    assert tail_contains_saturation

    performance, checks, _ = evaluate_closed_loop_acceptance(
        _acceptance_samples(control_config, feed_states),
        control_config,
        disturbance_time_s=None,
    )

    feed = performance["feed_flow"]
    assert feed.saturation_time_s == pytest.approx(1.0)
    assert feed.longest_continuous_saturation_s == pytest.approx(1.0)
    assert feed.failure_reasons == ("tail window contains saturation",)
    assert not checks["loop_performance"]


def test_continuous_saturation_gate_uses_aggregated_left_held_duration(
    control_config: ControlConfig,
) -> None:
    limit = control_config.acceptance.max_continuous_saturation_s
    saturation_end = 1.0 + limit + 1.0
    duration = saturation_end + control_config.acceptance.tail_window_s + 1.0
    samples = _acceptance_samples(
        control_config,
        (
            (0.0, False, "automatic"),
            (1.0, True, "automatic"),
            (saturation_end, False, "automatic"),
            (duration, False, "automatic"),
        ),
    )

    performance, checks, _ = evaluate_closed_loop_acceptance(
        samples,
        control_config,
        disturbance_time_s=None,
    )

    feed = performance["feed_flow"]
    assert feed.saturation_time_s == pytest.approx(limit + 1.0)
    assert feed.longest_continuous_saturation_s == pytest.approx(limit + 1.0)
    assert feed.failure_reasons == ("continuous saturation exceeded",)
    assert not checks["loop_performance"]


@pytest.mark.parametrize("manual_time_s", [0.0, 1.0])
def test_no_bump_gate_requires_every_sample_to_remain_automatic(
    control_config: ControlConfig,
    manual_time_s: float,
) -> None:
    samples = _acceptance_samples(
        control_config,
        tuple(
            (
                time_s,
                False,
                "manual" if time_s == manual_time_s else "automatic",
            )
            for time_s in (0.0, 1.0, 600.0)
        ),
    )

    performance, checks, diagnostics = evaluate_closed_loop_acceptance(
        samples,
        control_config,
        disturbance_time_s=None,
    )

    assert diagnostics["maximum_initial_command_delta"] == 0.0
    assert all(item.passed for item in performance.values())
    assert checks["loop_performance"]
    assert not checks["automatic_initialization_no_bump"]
