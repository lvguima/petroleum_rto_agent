"""Unit acceptance for the immutable M6 synthetic protection supervisor."""

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from petroleum_rto.cdu.validation.protection import (
    ProtectionAction,
    ProtectionFrame,
    ProtectionRule,
    ProtectionTrace,
    advance_protection,
    run_protection,
)


def _action(
    ratio: float = 0.8,
    *,
    command: str = "furnace_fuel_duty_w",
    loop_id: str = "furnace_temperature",
) -> ProtectionAction:
    return ProtectionAction({command: ratio}, (loop_id,))


def _high_rule(
    *,
    rule_id: str = "high_temperature",
    signal_name: str = "furnace_outlet_temperature_k",
    priority: int = 10,
    trigger_delay_s: float = 3.0,
    clear_delay_s: float = 2.0,
    latching: bool = False,
    action: ProtectionAction | None = None,
) -> ProtectionRule:
    return ProtectionRule(
        rule_id=rule_id,
        priority=priority,
        condition="high",
        signal_name=signal_name,
        trip_threshold=10.0,
        clear_threshold=8.0,
        trigger_delay_s=trigger_delay_s,
        clear_delay_s=clear_delay_s,
        latching=latching,
        action=_action() if action is None else action,
    )


def _one_frame(
    time_s: float,
    value: float,
    *,
    valid: bool = True,
    resets: tuple[str, ...] = (),
    signal_name: str = "furnace_outlet_temperature_k",
) -> ProtectionFrame:
    return ProtectionFrame(
        time_s,
        {signal_name: value},
        {signal_name: valid},
        resets,
    )


def test_trigger_delay_uses_the_exact_boundary_and_short_pulse_cancels() -> None:
    rule = _high_rule(trigger_delay_s=3.0)
    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 9.0),
            _one_frame(1.0, 10.0),
            _one_frame(3.999, 10.5),
            # Dropping below the trip threshold cancels a pending trip even
            # though the value remains inside the active-state hysteresis band.
            _one_frame(4.0, 9.0),
            _one_frame(5.0, 11.0),
            _one_frame(7.999, 11.0),
            _one_frame(8.0, 11.0),
        ),
    )

    assert [event.event_kind for event in trace.events] == [
        "trip_pending",
        "trip_cancelled",
        "trip_pending",
        "triggered",
    ]
    assert [event.time_s for event in trace.events] == [1.0, 4.0, 5.0, 8.0]
    assert trace.states[rule.rule_id].phase == "active"
    assert trace.active_actions[rule.rule_id] == rule.action


def test_hysteresis_and_clear_delay_do_not_chatter_or_drop_the_action() -> None:
    rule = _high_rule(trigger_delay_s=0.0, clear_delay_s=2.0)
    trace = ProtectionTrace.initialize((rule,))
    trace = trace.advance(_one_frame(0.0, 11.0))
    assert trace.states[rule.rule_id].phase == "active"

    # Values within (clear, trip) leave an active rule active.
    trace = trace.advance(_one_frame(1.0, 9.0))
    assert trace.states[rule.rule_id].phase == "active"
    assert [event.event_kind for event in trace.events] == ["triggered"]

    trace = trace.advance(_one_frame(2.0, 8.0))
    assert trace.states[rule.rule_id].phase == "pending_clear"
    assert rule.rule_id in trace.active_actions

    # Leaving the clear side before its delay expires restores active without
    # ever removing the protective action.
    trace = trace.advance(_one_frame(3.0, 9.0))
    assert trace.states[rule.rule_id].phase == "active"
    assert rule.rule_id in trace.active_actions
    trace = trace.advance(_one_frame(4.0, 8.0))
    trace = trace.advance(_one_frame(5.999, 7.0))
    assert trace.states[rule.rule_id].phase == "pending_clear"
    trace = trace.advance(_one_frame(6.0, 8.0))

    assert trace.states[rule.rule_id].phase == "normal"
    assert not trace.active_actions
    assert [event.event_kind for event in trace.events] == [
        "triggered",
        "clear_pending",
        "clear_cancelled",
        "clear_pending",
        "cleared",
    ]


def test_low_rule_reverses_trip_and_clear_comparisons() -> None:
    signal = "flash_liquid_outflow_ratio"
    rule = ProtectionRule(
        rule_id="low_furnace_feed",
        priority=0,
        condition="low",
        signal_name=signal,
        trip_threshold=0.75,
        clear_threshold=0.80,
        trigger_delay_s=1.0,
        clear_delay_s=1.0,
        latching=False,
        action=_action(),
    )

    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 0.79, signal_name=signal),
            _one_frame(1.0, 0.75, signal_name=signal),
            _one_frame(2.0, 0.74, signal_name=signal),
            # Inside the low-rule hysteresis band, the active action stays on.
            _one_frame(3.0, 0.78, signal_name=signal),
            _one_frame(4.0, 0.80, signal_name=signal),
            _one_frame(5.0, 0.81, signal_name=signal),
        ),
    )

    assert [event.event_kind for event in trace.events] == [
        "trip_pending",
        "triggered",
        "clear_pending",
        "cleared",
    ]
    assert trace.states[rule.rule_id].phase == "normal"


def test_latched_rule_rejects_unsafe_reset_and_accepts_explicit_safe_reset() -> None:
    rule = _high_rule(
        trigger_delay_s=0.0,
        clear_delay_s=1.0,
        latching=True,
    )
    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 11.0),
            _one_frame(1.0, 9.0, resets=(rule.rule_id,)),
            _one_frame(2.0, 8.0),
            _one_frame(3.0, 7.0),
        ),
    )
    assert trace.states[rule.rule_id].phase == "latched_clear"
    assert rule.rule_id in trace.active_actions
    rejected = [event for event in trace.events if event.event_kind == "reset_rejected"]
    assert len(rejected) == 1
    assert rejected[0].time_s == 1.0
    assert rejected[0].previous_phase == rejected[0].new_phase == "active"

    released = advance_protection(
        trace,
        _one_frame(4.0, 7.5, resets=(rule.rule_id,)),
    )
    assert released.states[rule.rule_id].phase == "normal"
    assert not released.active_actions
    assert released.events[-1].event_kind == "reset"
    assert released.events[-1].previous_phase == "latched_clear"
    # Pure advancement leaves the previous evidence unchanged.
    assert trace.states[rule.rule_id].phase == "latched_clear"
    assert len(released.frames) == len(trace.frames) + 1


def test_safe_reset_at_clear_boundary_has_stable_event_kind_order() -> None:
    rule = _high_rule(
        trigger_delay_s=0.0,
        clear_delay_s=0.0,
        latching=True,
    )
    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 11.0),
            _one_frame(1.0, 8.0, resets=(rule.rule_id,)),
        ),
    )

    assert trace.states[rule.rule_id].phase == "normal"
    assert [event.event_kind for event in trace.events] == [
        "triggered",
        "latched_clear",
        "reset",
    ]
    assert [event.time_s for event in trace.events[-2:]] == [1.0, 1.0]


def test_latched_clear_readiness_is_lost_inside_the_hysteresis_band() -> None:
    rule = _high_rule(
        trigger_delay_s=0.0,
        clear_delay_s=0.0,
        latching=True,
    )
    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 11.0),
            _one_frame(1.0, 8.0),
            _one_frame(2.0, 9.0, resets=(rule.rule_id,)),
        ),
    )

    assert trace.states[rule.rule_id].phase == "active"
    assert [event.event_kind for event in trace.events[-2:]] == [
        "clear_cancelled",
        "reset_rejected",
    ]
    assert rule.rule_id in trace.active_actions


def test_invalid_rule_uses_validity_sideband_and_not_nonfinite_values() -> None:
    signal = "tower_top_pressure_pa"
    rule = ProtectionRule(
        rule_id="pressure_measurement_invalid",
        priority=1,
        condition="invalid",
        signal_name=signal,
        trigger_delay_s=2.0,
        clear_delay_s=1.0,
        latching=False,
        action=ProtectionAction(
            {"top_gas_vent_kg_s": 1.0},
            ("top_pressure",),
        ),
    )
    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 152_325.0, signal_name=signal),
            _one_frame(1.0, 152_325.0, valid=False, signal_name=signal),
            _one_frame(2.999, 152_325.0, valid=False, signal_name=signal),
            _one_frame(3.0, 152_325.0, valid=False, signal_name=signal),
            _one_frame(4.0, 152_325.0, valid=True, signal_name=signal),
            _one_frame(5.0, 152_325.0, valid=True, signal_name=signal),
        ),
    )

    assert [event.event_kind for event in trace.events] == [
        "trip_pending",
        "triggered",
        "clear_pending",
        "cleared",
    ]
    assert trace.states[rule.rule_id].phase == "normal"
    assert all(math.isfinite(event.observed_value) for event in trace.events)
    with pytest.raises(ValueError, match="finite"):
        ProtectionFrame(6.0, {signal: math.nan}, {signal: False})


def test_invalid_analogue_signal_neither_trips_nor_clears_analogue_rule() -> None:
    rule = _high_rule(trigger_delay_s=0.0, clear_delay_s=0.0)
    trace = run_protection(
        (rule,),
        (
            _one_frame(0.0, 11.0),
            _one_frame(1.0, 7.0, valid=False),
        ),
    )
    assert trace.states[rule.rule_id].phase == "active"
    assert [event.event_kind for event in trace.events] == ["triggered"]


def test_simultaneous_events_sort_by_priority_rule_id_then_event_kind() -> None:
    rules = (
        _high_rule(
            rule_id="z_rule",
            signal_name="z_signal",
            priority=2,
            trigger_delay_s=0.0,
        ),
        _high_rule(
            rule_id="b_rule",
            signal_name="b_signal",
            priority=1,
            trigger_delay_s=0.0,
        ),
        _high_rule(
            rule_id="a_rule",
            signal_name="a_signal",
            priority=1,
            trigger_delay_s=0.0,
        ),
    )
    frame = ProtectionFrame(
        0.0,
        {"z_signal": 11.0, "b_signal": 11.0, "a_signal": 11.0},
        {"z_signal": True, "b_signal": True, "a_signal": True},
    )
    trace = run_protection(rules, (frame,))

    assert [
        (event.priority, event.rule_id, event.event_kind)
        for event in trace.events
    ] == [
        (1, "a_rule", "triggered"),
        (1, "b_rule", "triggered"),
        (2, "z_rule", "triggered"),
    ]
    assert [rule.rule_id for rule in trace.rules] == ["a_rule", "b_rule", "z_rule"]


def test_effective_action_uses_priority_and_deterministically_unions_tracking() -> None:
    higher = _high_rule(
        rule_id="higher",
        signal_name="higher_signal",
        priority=0,
        trigger_delay_s=0.0,
        action=ProtectionAction(
            {"furnace_fuel_duty_w": 0.8},
            ("furnace_temperature",),
        ),
    )
    lower = _high_rule(
        rule_id="lower",
        signal_name="lower_signal",
        priority=1,
        trigger_delay_s=0.0,
        action=ProtectionAction(
            {
                "furnace_fuel_duty_w": 1.1,
                "top_gas_vent_kg_s": 1.5,
            },
            ("top_pressure",),
        ),
    )
    trace = run_protection(
        (lower, higher),
        (
            ProtectionFrame(
                0.0,
                {"higher_signal": 11.0, "lower_signal": 11.0},
                {"higher_signal": True, "lower_signal": True},
            ),
        ),
    )

    assert trace.effective_action is not None
    assert trace.effective_action.command_ratio_overrides == {
        "furnace_fuel_duty_w": 0.8,
        "top_gas_vent_kg_s": 1.5,
    }
    assert trace.effective_action.manual_tracking_loop_ids == (
        "furnace_temperature",
        "top_pressure",
    )


@pytest.mark.parametrize(
    "factory, message",
    (
        (
            lambda: ProtectionAction({}, ()),
            "must contain",
        ),
        (
            lambda: ProtectionAction({"fuel": -0.1}, ("loop",)),
            "non-negative",
        ),
        (
            lambda: ProtectionAction({"fuel": 1.0}, ("loop", "loop")),
            "duplicates",
        ),
        (
            lambda: _high_rule(trigger_delay_s=-1.0),
            "non-negative",
        ),
        (
            lambda: ProtectionRule(
                "bad_high",
                0,
                "high",
                "signal",
                0.0,
                0.0,
                False,
                _action(),
                trip_threshold=10.0,
                clear_threshold=10.0,
            ),
            "below",
        ),
        (
            lambda: ProtectionRule(
                "bad_low",
                0,
                "low",
                "signal",
                0.0,
                0.0,
                False,
                _action(),
                trip_threshold=5.0,
                clear_threshold=5.0,
            ),
            "above",
        ),
        (
            lambda: ProtectionRule(
                "bad_invalid",
                0,
                "invalid",
                "signal",
                0.0,
                0.0,
                False,
                _action(),
                trip_threshold=1.0,
            ),
            "cannot define",
        ),
        (
            lambda: ProtectionFrame(0.0, {"signal": 1.0}, {"other": True}),
            "identical names",
        ),
        (
            lambda: ProtectionFrame(
                0.0,
                {"signal": 1.0},
                {"signal": cast(bool, 1)},
            ),
            "boolean",
        ),
        (
            lambda: ProtectionFrame(-1.0, {"signal": 1.0}, {"signal": True}),
            "non-negative",
        ),
    ),
)
def test_strict_contracts_reject_invalid_thresholds_times_and_mappings(
    factory: object,
    message: str,
) -> None:
    callable_factory = factory
    assert callable(callable_factory)
    with pytest.raises((TypeError, ValueError), match=message):
        callable_factory()


def test_frame_coverage_reset_targets_and_time_order_are_strict() -> None:
    rule = _high_rule()
    trace = ProtectionTrace.initialize((rule,))
    with pytest.raises(ValueError, match="signal names differ"):
        trace.advance(
            ProtectionFrame(
                0.0,
                {rule.signal_name: 9.0, "unknown": 1.0},
                {rule.signal_name: True, "unknown": True},
            )
        )
    with pytest.raises(ValueError, match="unknown rules"):
        trace.advance(_one_frame(0.0, 9.0, resets=("unknown_rule",)))
    trace = trace.advance(_one_frame(0.0, 9.0))
    with pytest.raises(ValueError, match="strictly greater"):
        trace.advance(_one_frame(0.0, 9.0))


def test_mappings_are_frozen_and_serialization_is_repeatable() -> None:
    raw_overrides = {"furnace_fuel_duty_w": 0.8}
    raw_signals = {"furnace_outlet_temperature_k": 11.0}
    raw_validity = {"furnace_outlet_temperature_k": True}
    action = ProtectionAction(raw_overrides, ("furnace_temperature",))
    rule = _high_rule(trigger_delay_s=0.0, action=action)
    frame = ProtectionFrame(0.0, raw_signals, raw_validity)
    raw_overrides["furnace_fuel_duty_w"] = 1.2
    raw_signals["furnace_outlet_temperature_k"] = 0.0
    raw_validity["furnace_outlet_temperature_k"] = False

    assert action.command_ratio_overrides["furnace_fuel_duty_w"] == 0.8
    assert frame.signals["furnace_outlet_temperature_k"] == 11.0
    assert frame.validity["furnace_outlet_temperature_k"]
    with pytest.raises(TypeError):
        action.command_ratio_overrides["furnace_fuel_duty_w"] = 1.0  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        frame.time_s = 1.0  # type: ignore[misc]

    first = run_protection((rule,), (frame,))
    second = run_protection((rule,), (frame,))
    first_payload = first.as_dict()
    assert first_payload == second.as_dict()
    assert json.dumps(first_payload, sort_keys=True, allow_nan=False) == json.dumps(
        second.as_dict(),
        sort_keys=True,
        allow_nan=False,
    )
    # Returned dictionaries are detached from the immutable evidence.
    first_payload["states"] = {}
    assert first.as_dict()["states"]
