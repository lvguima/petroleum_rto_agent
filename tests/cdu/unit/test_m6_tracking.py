from __future__ import annotations

from dataclasses import replace

import pytest

from petroleum_rto.cdu.control.controllers import (
    NormalizedPIController,
    PIControllerSpec,
)
from petroleum_rto.cdu.validation.tracking import verify_controller_tracking


def _controller() -> NormalizedPIController:
    return NormalizedPIController(
        PIControllerSpec(
            loop_id="furnace_temperature",
            action="direct",
            proportional_gain=4.0,
            integral_time_s=180.0,
            anti_windup_time_s=45.0,
            setpoint_rate_limit_fraction_per_s=0.001,
            output_min_ratio=0.8,
            output_max_ratio=1.15,
            output_rate_limit_fraction_per_s=0.01,
        ),
        pv_scale=600.0,
        output_scale=10_000_000.0,
    )


def test_manual_tracking_reaches_protected_output_and_returns_without_bump() -> None:
    controller = _controller()
    initial = controller.initialize(
        process_value=600.0,
        setpoint=600.0,
        output=10_000_000.0,
    )

    evidence = verify_controller_tracking(
        "furnace_temperature",
        controller,
        initial,
        process_value=600.0,
        target_setpoint=600.0,
        protected_output=8_000_000.0,
        control_interval_s=1.0,
        maximum_manual_steps=30,
        relative_tolerance=1e-12,
    )

    assert evidence.passed
    assert evidence.manual_steps == 20
    assert evidence.final_manual_output == pytest.approx(8_000_000.0)
    assert evidence.automatic_return_jump <= evidence.tolerance
    assert evidence.as_dict() == verify_controller_tracking(
        "furnace_temperature",
        controller,
        initial,
        process_value=600.0,
        target_setpoint=600.0,
        protected_output=8_000_000.0,
        control_interval_s=1.0,
        maximum_manual_steps=30,
        relative_tolerance=1e-12,
    ).as_dict()


def test_tracking_reports_failure_when_manual_window_is_too_short() -> None:
    controller = _controller()
    initial = controller.initialize(
        process_value=600.0,
        setpoint=600.0,
        output=10_000_000.0,
    )

    evidence = verify_controller_tracking(
        "furnace_temperature",
        controller,
        initial,
        process_value=600.0,
        target_setpoint=600.0,
        protected_output=8_000_000.0,
        control_interval_s=1.0,
        maximum_manual_steps=2,
        relative_tolerance=1e-12,
    )

    assert not evidence.passed
    with pytest.raises(ValueError, match="automatic mode"):
        verify_controller_tracking(
            "furnace_temperature",
            controller,
            replace(initial, mode="manual"),
            process_value=600.0,
            target_setpoint=600.0,
            protected_output=8_000_000.0,
            control_interval_s=1.0,
            maximum_manual_steps=2,
            relative_tolerance=1e-12,
        )
