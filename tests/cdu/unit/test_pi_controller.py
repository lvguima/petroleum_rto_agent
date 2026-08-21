from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from petroleum_rto.cdu.control.controllers import (
    AUTOMATIC,
    MANUAL,
    NormalizedPIController,
    PIControllerSpec,
)


def make_spec(**overrides: object) -> PIControllerSpec:
    values: dict[str, object] = {
        "loop_id": "test_loop",
        "action": "direct",
        "proportional_gain": 2.0,
        "integral_time_s": 10.0,
        "anti_windup_time_s": 5.0,
        "setpoint_rate_limit_fraction_per_s": 0.1,
        "output_min_ratio": 0.0,
        "output_max_ratio": 2.0,
        "output_rate_limit_fraction_per_s": 1.0,
        "initial_mode": AUTOMATIC,
    }
    values.update(overrides)
    return PIControllerSpec(**values)  # type: ignore[arg-type]


def test_initialization_is_normalized_immutable_and_bumpless() -> None:
    controller = NormalizedPIController(make_spec(), pv_scale=100.0, output_scale=10.0)
    state = controller.initialize(
        process_value=90.0,
        setpoint=100.0,
        output=12.0,
    )

    assert state.ramped_setpoint == 100.0
    assert state.previous_target_setpoint == 100.0
    assert state.output_normalized == 1.2
    assert state.integral_term_normalized == pytest.approx(0.0)
    assert state.previous_error_normalized == 0.0
    assert state.previous_tracking_error_normalized == 0.0

    first_decision = controller.update(
        state,
        process_value=90.0,
        target_setpoint=100.0,
        dt_s=0.0,
    )
    assert first_decision.output == pytest.approx(12.0)
    assert not first_decision.saturated
    with pytest.raises(FrozenInstanceError):
        state.output_normalized = 0.0  # type: ignore[misc]


def test_new_target_at_non_aligned_event_cannot_leak_into_elapsed_interval() -> None:
    controller = NormalizedPIController(
        make_spec(
            proportional_gain=30.0,
            integral_time_s=100.0,
            anti_windup_time_s=2.0,
            setpoint_rate_limit_fraction_per_s=0.01,
            output_min_ratio=0.8,
            output_max_ratio=1.1,
            output_rate_limit_fraction_per_s=0.02,
        ),
        pv_scale=100.0,
        output_scale=10.0,
    )
    state = controller.initialize(process_value=100.0, setpoint=100.0, output=10.0)

    event_decision = controller.update(
        state,
        process_value=100.0,
        target_setpoint=200.0,
        dt_s=0.4,
    )
    assert event_decision.state.ramped_setpoint == 100.0
    assert event_decision.state.integral_term_normalized == 0.0
    assert event_decision.output == 10.0

    future_decision = controller.update(
        event_decision.state,
        process_value=100.0,
        target_setpoint=200.0,
        dt_s=1.0,
    )
    assert future_decision.state.ramped_setpoint == 101.0
    assert future_decision.unconstrained_output_normalized == pytest.approx(1.3)
    assert future_decision.magnitude_limited_output_normalized == 1.1
    assert future_decision.output_normalized == 1.02
    assert future_decision.limited_by_magnitude
    assert future_decision.limited_by_rate
    assert future_decision.state.previous_tracking_error_normalized == pytest.approx(-0.28)

    back_calculated = controller.update(
        future_decision.state,
        process_value=100.0,
        target_setpoint=200.0,
        dt_s=1.0,
    )
    expected_integral_change = 30.0 * 0.01 / 100.0 + (-0.28) / 2.0
    assert back_calculated.state.integral_term_normalized == pytest.approx(
        expected_integral_change
    )


def test_constant_error_is_integrated_once_on_the_following_tick() -> None:
    controller = NormalizedPIController(make_spec(), pv_scale=100.0, output_scale=10.0)
    state = controller.initialize(process_value=100.0, setpoint=100.0, output=10.0)
    event = controller.update(
        state,
        process_value=100.0,
        target_setpoint=110.0,
        dt_s=0.0,
    )
    error_established = controller.update(
        event.state,
        process_value=100.0,
        target_setpoint=110.0,
        dt_s=1.0,
    )
    assert error_established.error_normalized == pytest.approx(0.1)
    assert error_established.state.integral_term_normalized == 0.0

    integrated = controller.update(
        error_established.state,
        process_value=100.0,
        target_setpoint=110.0,
        dt_s=1.0,
    )
    assert integrated.state.integral_term_normalized == pytest.approx(2.0 / 10.0 * 0.1)


def test_back_calculation_recovers_after_long_magnitude_saturation() -> None:
    controller = NormalizedPIController(
        make_spec(
            proportional_gain=1.0,
            integral_time_s=10.0,
            anti_windup_time_s=1.0,
            setpoint_rate_limit_fraction_per_s=10.0,
            output_min_ratio=0.5,
            output_max_ratio=1.2,
            output_rate_limit_fraction_per_s=10.0,
        ),
        pv_scale=100.0,
        output_scale=10.0,
    )
    state = controller.initialize(process_value=100.0, setpoint=100.0, output=10.0)
    state = controller.update(
        state,
        process_value=100.0,
        target_setpoint=150.0,
        dt_s=0.0,
    ).state

    maximum_integral_magnitude = 0.0
    for _ in range(240):
        decision = controller.update(
            state,
            process_value=100.0,
            target_setpoint=150.0,
            dt_s=1.0,
        )
        assert decision.limited_by_magnitude
        assert not decision.limited_by_rate
        assert 0.5 <= decision.output_normalized <= 1.2
        maximum_integral_magnitude = max(
            maximum_integral_magnitude,
            abs(decision.state.integral_term_normalized),
        )
        state = decision.state

    assert maximum_integral_magnitude < 0.5

    state = controller.update(
        state,
        process_value=100.0,
        target_setpoint=90.0,
        dt_s=0.0,
    ).state
    desaturated_step: int | None = None
    for step in range(1, 6):
        decision = controller.update(
            state,
            process_value=100.0,
            target_setpoint=90.0,
            dt_s=1.0,
        )
        assert decision.error_normalized < 0.0
        assert not decision.limited_by_rate
        assert 0.5 <= decision.output_normalized <= 1.2
        assert abs(decision.state.integral_term_normalized) < 0.5
        state = decision.state
        if not decision.limited_by_magnitude:
            desaturated_step = step
            break

    assert desaturated_step is not None
    assert desaturated_step <= 3


def test_back_calculation_recovers_from_rate_only_saturation() -> None:
    controller = NormalizedPIController(
        make_spec(
            proportional_gain=2.0,
            integral_time_s=10.0,
            anti_windup_time_s=2.0,
            setpoint_rate_limit_fraction_per_s=10.0,
            output_min_ratio=0.1,
            output_max_ratio=3.0,
            output_rate_limit_fraction_per_s=0.02,
        ),
        pv_scale=100.0,
        output_scale=10.0,
    )
    state = controller.initialize(process_value=100.0, setpoint=100.0, output=10.0)
    state = controller.update(
        state,
        process_value=100.0,
        target_setpoint=150.0,
        dt_s=0.0,
    ).state

    maximum_integral_magnitude = 0.0
    for _ in range(20):
        decision = controller.update(
            state,
            process_value=100.0,
            target_setpoint=150.0,
            dt_s=1.0,
        )
        assert decision.limited_by_rate
        assert not decision.limited_by_magnitude
        assert 0.1 <= decision.output_normalized <= 3.0
        maximum_integral_magnitude = max(
            maximum_integral_magnitude,
            abs(decision.state.integral_term_normalized),
        )
        state = decision.state

    assert maximum_integral_magnitude < 1.0
    assert state.integral_term_normalized < 0.0
    assert state.previous_tracking_error_normalized < 0.0
    output_before_reversal = state.output_normalized

    state = controller.update(
        state,
        process_value=100.0,
        target_setpoint=99.0,
        dt_s=0.0,
    ).state
    desaturated_step = None
    for step in range(1, 13):
        decision = controller.update(
            state,
            process_value=100.0,
            target_setpoint=99.0,
            dt_s=1.0,
        )
        assert not decision.limited_by_magnitude
        assert 0.1 <= decision.output_normalized <= 3.0
        assert abs(decision.state.integral_term_normalized) < 1.0
        if step == 1:
            assert decision.error_normalized < 0.0
            assert decision.limited_by_rate
            assert decision.output_normalized < output_before_reversal
        state = decision.state
        if not decision.limited_by_rate:
            desaturated_step = step
            break

    assert desaturated_step is not None
    assert desaturated_step <= 8


def test_manual_tracking_and_manual_to_auto_transfer_are_bumpless() -> None:
    controller = NormalizedPIController(make_spec(), pv_scale=100.0, output_scale=10.0)
    initial = controller.initialize(process_value=100.0, output=10.0)
    manual = controller.update(
        initial,
        process_value=100.0,
        target_setpoint=100.0,
        dt_s=1.0,
        requested_mode=MANUAL,
        manual_output=12.0,
    )
    assert manual.output == pytest.approx(12.0)
    assert manual.state.mode == MANUAL
    assert manual.state.integral_term_normalized == pytest.approx(0.2)

    automatic = controller.update(
        manual.state,
        process_value=90.0,
        target_setpoint=100.0,
        dt_s=0.0,
        requested_mode=AUTOMATIC,
    )
    assert automatic.output == pytest.approx(manual.output)
    assert automatic.state.mode == AUTOMATIC
    assert automatic.state.integral_term_normalized == pytest.approx(0.0)

    held_manual = controller.update(
        automatic.state,
        process_value=90.0,
        target_setpoint=100.0,
        dt_s=0.0,
        requested_mode=MANUAL,
    )
    assert held_manual.output == automatic.output


def test_reverse_action_changes_output_in_the_opposite_direction() -> None:
    direct = NormalizedPIController(make_spec(), pv_scale=100.0, output_scale=10.0)
    reverse = NormalizedPIController(
        make_spec(action="reverse"),
        pv_scale=100.0,
        output_scale=10.0,
    )
    assert direct.spec.signed_proportional_gain == 2.0
    assert reverse.spec.signed_proportional_gain == -2.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"proportional_gain": 0.0},
        {"integral_time_s": -1.0},
        {"action": "sideways"},
        {"output_min_ratio": 1.0},
        {"output_max_ratio": 1.0},
        {"output_min_ratio": 1.1},
        {"output_max_ratio": 0.9},
        {"output_rate_limit_fraction_per_s": float("nan")},
    ],
)
def test_invalid_controller_specs_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_spec(**overrides)


def test_invalid_runtime_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="pv_scale"):
        NormalizedPIController(make_spec(), pv_scale=0.0, output_scale=1.0)
    controller = NormalizedPIController(make_spec(), pv_scale=100.0, output_scale=10.0)
    with pytest.raises(ValueError, match="initial output"):
        controller.initialize(process_value=100.0, output=30.0)
    state = controller.initialize(process_value=100.0, output=10.0)
    with pytest.raises(ValueError, match="dt_s"):
        controller.update(
            state,
            process_value=100.0,
            target_setpoint=100.0,
            dt_s=-1.0,
        )
    with pytest.raises(ValueError, match="manual_output"):
        controller.update(
            state,
            process_value=100.0,
            target_setpoint=100.0,
            dt_s=1.0,
            manual_output=10.0,
        )
