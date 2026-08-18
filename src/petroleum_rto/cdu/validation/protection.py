"""Deterministic synthetic protection supervisor for M6 model validation.

This module deliberately models validation-only supervisory behaviour.  It does
not contain plant SIS set-points, voting, hardware diagnostics, or field control
interfaces.  The state transition function is immutable: every input frame
returns a new :class:`ProtectionTrace` and never mutates prior evidence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from types import MappingProxyType
from typing import Final, Literal, cast

ProtectionCondition = Literal["high", "low", "invalid"]
ProtectionPhase = Literal[
    "normal",
    "pending_trip",
    "active",
    "pending_clear",
    "latched_clear",
]
ProtectionEventKind = Literal[
    "trip_pending",
    "trip_cancelled",
    "triggered",
    "clear_pending",
    "clear_cancelled",
    "cleared",
    "latched_clear",
    "reset_rejected",
    "reset",
]

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_CONDITIONS: Final[frozenset[str]] = frozenset({"high", "low", "invalid"})
_PHASES: Final[frozenset[str]] = frozenset(
    {"normal", "pending_trip", "active", "pending_clear", "latched_clear"}
)
_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "trip_pending",
        "trip_cancelled",
        "triggered",
        "clear_pending",
        "clear_cancelled",
        "cleared",
        "latched_clear",
        "reset_rejected",
        "reset",
    }
)
_ACTION_PHASES: Final[frozenset[str]] = frozenset(
    {"active", "pending_clear", "latched_clear"}
)
_EVENT_KIND_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {
        "trip_pending": 0,
        "trip_cancelled": 1,
        "triggered": 2,
        "clear_pending": 3,
        "clear_cancelled": 4,
        "cleared": 5,
        "latched_clear": 6,
        "reset_rejected": 7,
        "reset": 8,
    }
)


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def _nonnegative_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number < 0.0:
        raise ValueError(f"{context} must be non-negative")
    return number


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def _condition(value: object) -> ProtectionCondition:
    if value not in _CONDITIONS:
        raise ValueError("condition must be high, low, or invalid")
    return cast(ProtectionCondition, value)


def _phase(value: object, *, context: str) -> ProtectionPhase:
    if value not in _PHASES:
        raise ValueError(f"{context} has an unsupported protection phase")
    return cast(ProtectionPhase, value)


def _event_kind(value: object) -> ProtectionEventKind:
    if value not in _EVENT_KINDS:
        raise ValueError("unsupported protection event kind")
    return cast(ProtectionEventKind, value)


def _float_mapping(
    values: object,
    *,
    context: str,
    nonnegative: bool,
) -> Mapping[str, float]:
    if not isinstance(values, Mapping) or any(
        not isinstance(name, str) for name in values
    ):
        raise TypeError(f"{context} must be a mapping with string keys")
    copied: dict[str, float] = {}
    for raw_name in sorted(values):
        name = _identifier(raw_name, context=f"{context} key")
        raw_value = values[raw_name]
        number = (
            _nonnegative_number(raw_value, context=f"{context}.{name}")
            if nonnegative
            else _finite_number(raw_value, context=f"{context}.{name}")
        )
        copied[name] = number
    return MappingProxyType(copied)


def _bool_mapping(values: object, *, context: str) -> Mapping[str, bool]:
    if not isinstance(values, Mapping) or any(
        not isinstance(name, str) for name in values
    ):
        raise TypeError(f"{context} must be a mapping with string keys")
    copied: dict[str, bool] = {}
    for raw_name in sorted(values):
        name = _identifier(raw_name, context=f"{context} key")
        value = values[raw_name]
        if not isinstance(value, bool):
            raise TypeError(f"{context}.{name} must be a boolean")
        copied[name] = value
    return MappingProxyType(copied)


def _identifier_tuple(values: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        raise TypeError(f"{context} must be a sequence of identifiers")
    copied = tuple(
        _identifier(value, context=f"{context}[{index}]")
        for index, value in enumerate(values)
    )
    if len(set(copied)) != len(copied):
        raise ValueError(f"{context} cannot contain duplicates")
    return tuple(sorted(copied))


@dataclass(frozen=True)
class ProtectionAction:
    """One synthetic supervisory demand.

    ``command_ratio_overrides`` uses nominal-command ratios.  A ratio may be
    zero because a synthetic trip can demand zero duty.  The named controller
    loops in ``manual_tracking_loop_ids`` must track the applied override; the
    actual PI/manual hand-off is performed by the higher-level M6 runner.
    """

    command_ratio_overrides: Mapping[str, float] = field(default_factory=dict)
    manual_tracking_loop_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        overrides = _float_mapping(
            self.command_ratio_overrides,
            context="command_ratio_overrides",
            nonnegative=True,
        )
        loop_ids = _identifier_tuple(
            self.manual_tracking_loop_ids,
            context="manual_tracking_loop_ids",
        )
        if not overrides and not loop_ids:
            raise ValueError("a protection action must contain an override or tracking loop")
        object.__setattr__(self, "command_ratio_overrides", overrides)
        object.__setattr__(self, "manual_tracking_loop_ids", loop_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "command_ratio_overrides": dict(self.command_ratio_overrides),
            "manual_tracking_loop_ids": list(self.manual_tracking_loop_ids),
        }


@dataclass(frozen=True)
class ProtectionRule:
    """One high, low, or measurement-invalid synthetic protection rule."""

    rule_id: str
    priority: int
    condition: ProtectionCondition
    signal_name: str
    trigger_delay_s: float
    clear_delay_s: float
    latching: bool
    action: ProtectionAction
    trip_threshold: float | None = None
    clear_threshold: float | None = None

    def __post_init__(self) -> None:
        rule_id = _identifier(self.rule_id, context="rule_id")
        signal_name = _identifier(self.signal_name, context="signal_name")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("priority must be an integer")
        if self.priority < 0:
            raise ValueError("priority must be non-negative")
        condition = _condition(self.condition)
        trigger_delay = _nonnegative_number(
            self.trigger_delay_s,
            context="trigger_delay_s",
        )
        clear_delay = _nonnegative_number(
            self.clear_delay_s,
            context="clear_delay_s",
        )
        if not isinstance(self.latching, bool):
            raise TypeError("latching must be a boolean")
        if not isinstance(self.action, ProtectionAction):
            raise TypeError("action must be a ProtectionAction")

        trip: float | None
        clear: float | None
        if condition == "invalid":
            if self.trip_threshold is not None or self.clear_threshold is not None:
                raise ValueError("invalid rules cannot define numerical thresholds")
            trip = None
            clear = None
        else:
            if self.trip_threshold is None or self.clear_threshold is None:
                raise ValueError("high and low rules require trip and clear thresholds")
            trip = _finite_number(self.trip_threshold, context="trip_threshold")
            clear = _finite_number(self.clear_threshold, context="clear_threshold")
            if condition == "high" and clear >= trip:
                raise ValueError("a high rule clear threshold must be below its trip threshold")
            if condition == "low" and clear <= trip:
                raise ValueError("a low rule clear threshold must be above its trip threshold")

        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "signal_name", signal_name)
        object.__setattr__(self, "condition", condition)
        object.__setattr__(self, "trigger_delay_s", trigger_delay)
        object.__setattr__(self, "clear_delay_s", clear_delay)
        object.__setattr__(self, "trip_threshold", trip)
        object.__setattr__(self, "clear_threshold", clear)

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "priority": self.priority,
            "condition": self.condition,
            "signal_name": self.signal_name,
            "trip_threshold": self.trip_threshold,
            "clear_threshold": self.clear_threshold,
            "trigger_delay_s": self.trigger_delay_s,
            "clear_delay_s": self.clear_delay_s,
            "latching": self.latching,
            "action": self.action.as_dict(),
        }


@dataclass(frozen=True)
class ProtectionFrame:
    """One supervisor evaluation frame with explicit measurement validity."""

    time_s: float
    signals: Mapping[str, float]
    validity: Mapping[str, bool]
    reset_requests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        time_s = _nonnegative_number(self.time_s, context="frame time_s")
        signals = _float_mapping(
            self.signals,
            context="frame signals",
            nonnegative=False,
        )
        validity = _bool_mapping(self.validity, context="frame validity")
        if set(signals) != set(validity):
            raise ValueError("frame signals and validity must contain identical names")
        resets = _identifier_tuple(self.reset_requests, context="reset_requests")
        object.__setattr__(self, "time_s", time_s)
        object.__setattr__(self, "signals", signals)
        object.__setattr__(self, "validity", validity)
        object.__setattr__(self, "reset_requests", resets)

    def as_dict(self) -> dict[str, object]:
        return {
            "time_s": self.time_s,
            "signals": dict(self.signals),
            "validity": dict(self.validity),
            "reset_requests": list(self.reset_requests),
        }


@dataclass(frozen=True)
class ProtectionRuleState:
    """Minimal persistent state for one protection rule."""

    rule_id: str
    phase: ProtectionPhase
    phase_started_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _identifier(self.rule_id, context="rule_id"))
        object.__setattr__(self, "phase", _phase(self.phase, context="phase"))
        object.__setattr__(
            self,
            "phase_started_s",
            _nonnegative_number(self.phase_started_s, context="phase_started_s"),
        )

    @property
    def action_active(self) -> bool:
        return self.phase in _ACTION_PHASES

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "phase": self.phase,
            "phase_started_s": self.phase_started_s,
            "action_active": self.action_active,
        }


@dataclass(frozen=True)
class ProtectionEvent:
    """One immutable state transition or explicit reset rejection."""

    time_s: float
    priority: int
    rule_id: str
    event_kind: ProtectionEventKind
    previous_phase: ProtectionPhase
    new_phase: ProtectionPhase
    signal_name: str
    observed_value: float
    valid: bool
    reason: str
    action: ProtectionAction | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "time_s",
            _nonnegative_number(self.time_s, context="event time_s"),
        )
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("event priority must be an integer")
        if self.priority < 0:
            raise ValueError("event priority must be non-negative")
        object.__setattr__(self, "rule_id", _identifier(self.rule_id, context="rule_id"))
        object.__setattr__(self, "event_kind", _event_kind(self.event_kind))
        object.__setattr__(
            self,
            "previous_phase",
            _phase(self.previous_phase, context="previous_phase"),
        )
        object.__setattr__(
            self,
            "new_phase",
            _phase(self.new_phase, context="new_phase"),
        )
        object.__setattr__(
            self,
            "signal_name",
            _identifier(self.signal_name, context="signal_name"),
        )
        object.__setattr__(
            self,
            "observed_value",
            _finite_number(self.observed_value, context="observed_value"),
        )
        if not isinstance(self.valid, bool):
            raise TypeError("event valid must be a boolean")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("event reason must be non-empty")
        if self.action is not None and not isinstance(self.action, ProtectionAction):
            raise TypeError("event action must be a ProtectionAction or None")

    def as_dict(self) -> dict[str, object]:
        return {
            "time_s": self.time_s,
            "priority": self.priority,
            "rule_id": self.rule_id,
            "event_kind": self.event_kind,
            "previous_phase": self.previous_phase,
            "new_phase": self.new_phase,
            "signal_name": self.signal_name,
            "observed_value": self.observed_value,
            "valid": self.valid,
            "reason": self.reason,
            "action": None if self.action is None else self.action.as_dict(),
        }


def _event_sort_key(event: ProtectionEvent) -> tuple[float, int, str, int]:
    return (
        event.time_s,
        event.priority,
        event.rule_id,
        _EVENT_KIND_ORDER[event.event_kind],
    )


def _states_mapping(values: object) -> Mapping[str, ProtectionRuleState]:
    if not isinstance(values, Mapping) or any(
        not isinstance(name, str) for name in values
    ):
        raise TypeError("states must be a mapping with string keys")
    copied: dict[str, ProtectionRuleState] = {}
    for name in sorted(values):
        state = values[name]
        if not isinstance(state, ProtectionRuleState):
            raise TypeError("states must contain ProtectionRuleState values")
        if state.rule_id != name:
            raise ValueError("state mapping keys must match rule_id")
        copied[name] = state
    return MappingProxyType(copied)


@dataclass(frozen=True)
class ProtectionTrace:
    """Complete immutable protection evidence accumulated from input frames."""

    rules: tuple[ProtectionRule, ...]
    states: Mapping[str, ProtectionRuleState]
    start_time_s: float = 0.0
    frames: tuple[ProtectionFrame, ...] = ()
    events: tuple[ProtectionEvent, ...] = ()

    def __post_init__(self) -> None:
        start = _nonnegative_number(self.start_time_s, context="start_time_s")
        rules = tuple(self.rules)
        if not rules or any(not isinstance(rule, ProtectionRule) for rule in rules):
            raise ValueError("rules must contain at least one ProtectionRule")
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("protection rule ids must be unique")
        canonical_rules = tuple(sorted(rules, key=lambda rule: (rule.priority, rule.rule_id)))
        states = _states_mapping(self.states)
        rule_ids = {rule.rule_id for rule in canonical_rules}
        if set(states) != rule_ids:
            raise ValueError("states must cover exactly the configured protection rules")
        if any(state.phase_started_s < start for state in states.values()):
            raise ValueError("state phase start cannot precede trace start")

        frames = tuple(self.frames)
        if any(not isinstance(frame, ProtectionFrame) for frame in frames):
            raise TypeError("frames must contain ProtectionFrame values")
        if frames and frames[0].time_s < start:
            raise ValueError("the first frame cannot precede trace start")
        if any(
            later.time_s <= earlier.time_s
            for earlier, later in pairwise(frames)
        ):
            raise ValueError("protection frame times must increase strictly")

        events = tuple(self.events)
        if any(not isinstance(event, ProtectionEvent) for event in events):
            raise TypeError("events must contain ProtectionEvent values")
        if any(event.rule_id not in rule_ids for event in events):
            raise ValueError("protection events reference unknown rules")
        if events != tuple(sorted(events, key=_event_sort_key)):
            raise ValueError("protection events are not in deterministic order")
        last_time = start if not frames else frames[-1].time_s
        if any(state.phase_started_s > last_time for state in states.values()):
            raise ValueError("state phase start cannot follow the last trace frame")
        if not frames and (
            events
            or any(
                state.phase != "normal" or state.phase_started_s != start
                for state in states.values()
            )
        ):
            raise ValueError("a trace without frames must contain only initial normal states")
        if any(event.time_s < start or event.time_s > last_time for event in events):
            raise ValueError("protection event time lies outside the trace")

        object.__setattr__(self, "rules", canonical_rules)
        object.__setattr__(self, "states", states)
        object.__setattr__(self, "start_time_s", start)
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "events", events)

    @classmethod
    def initialize(
        cls,
        rules: Sequence[ProtectionRule],
        *,
        start_time_s: float = 0.0,
    ) -> ProtectionTrace:
        """Return a new all-normal trace at ``start_time_s``."""

        if isinstance(rules, (str, bytes, bytearray)):
            raise TypeError("rules must be a sequence of ProtectionRule values")
        start = _nonnegative_number(start_time_s, context="start_time_s")
        copied_rules = tuple(rules)
        states = {
            rule.rule_id: ProtectionRuleState(rule.rule_id, "normal", start)
            for rule in copied_rules
            if isinstance(rule, ProtectionRule)
        }
        return cls(copied_rules, states, start_time_s=start)

    @property
    def last_time_s(self) -> float:
        return self.start_time_s if not self.frames else self.frames[-1].time_s

    @property
    def active_actions(self) -> Mapping[str, ProtectionAction]:
        """Return active actions keyed by rule, in deterministic rule order."""

        return MappingProxyType(
            {
                rule.rule_id: rule.action
                for rule in self.rules
                if self.states[rule.rule_id].action_active
            }
        )

    @property
    def effective_action(self) -> ProtectionAction | None:
        """Merge active actions; lower priority number and rule id win conflicts."""

        active = self.active_actions
        if not active:
            return None
        overrides: dict[str, float] = {}
        tracking_ids: set[str] = set()
        for rule in self.rules:
            action = active.get(rule.rule_id)
            if action is None:
                continue
            for command, ratio in action.command_ratio_overrides.items():
                overrides.setdefault(command, ratio)
            tracking_ids.update(action.manual_tracking_loop_ids)
        return ProtectionAction(overrides, tuple(sorted(tracking_ids)))

    def advance(self, frame: ProtectionFrame) -> ProtectionTrace:
        """Pure convenience method equivalent to :func:`advance_protection`."""

        return advance_protection(self, frame)

    def as_dict(self) -> dict[str, object]:
        effective = self.effective_action
        return {
            "start_time_s": self.start_time_s,
            "last_time_s": self.last_time_s,
            "rules": [rule.as_dict() for rule in self.rules],
            "states": {
                rule_id: self.states[rule_id].as_dict()
                for rule_id in sorted(self.states)
            },
            "frames": [frame.as_dict() for frame in self.frames],
            "events": [event.as_dict() for event in self.events],
            "active_actions": {
                rule_id: action.as_dict()
                for rule_id, action in self.active_actions.items()
            },
            "effective_action": None if effective is None else effective.as_dict(),
        }


def _predicate_values(
    rule: ProtectionRule,
    frame: ProtectionFrame,
) -> tuple[bool | None, bool | None]:
    """Return trip/clear predicates, or two ``None`` values when unavailable."""

    valid = frame.validity[rule.signal_name]
    value = frame.signals[rule.signal_name]
    if rule.condition == "invalid":
        return not valid, valid
    if not valid:
        # An invalid analogue signal must neither trip nor clear its analogue
        # rule.  A separate ``invalid`` rule handles the measurement failure.
        return None, None
    if rule.trip_threshold is None or rule.clear_threshold is None:
        raise AssertionError("validated analogue rule omitted thresholds")
    if rule.condition == "high":
        return value >= rule.trip_threshold, value <= rule.clear_threshold
    return value <= rule.trip_threshold, value >= rule.clear_threshold


def _new_state(
    state: ProtectionRuleState,
    phase: ProtectionPhase,
    time_s: float,
) -> ProtectionRuleState:
    if phase == state.phase:
        return state
    return ProtectionRuleState(state.rule_id, phase, time_s)


def _transition_event(
    rule: ProtectionRule,
    frame: ProtectionFrame,
    *,
    event_kind: ProtectionEventKind,
    previous_phase: ProtectionPhase,
    new_phase: ProtectionPhase,
    reason: str,
) -> ProtectionEvent:
    return ProtectionEvent(
        time_s=frame.time_s,
        priority=rule.priority,
        rule_id=rule.rule_id,
        event_kind=event_kind,
        previous_phase=previous_phase,
        new_phase=new_phase,
        signal_name=rule.signal_name,
        observed_value=frame.signals[rule.signal_name],
        valid=frame.validity[rule.signal_name],
        reason=reason,
        action=(rule.action if new_phase in _ACTION_PHASES else None),
    )


def _advance_rule(
    rule: ProtectionRule,
    state: ProtectionRuleState,
    frame: ProtectionFrame,
) -> tuple[ProtectionRuleState, tuple[ProtectionEvent, ...]]:
    trip, clear = _predicate_values(rule, frame)
    previous_phase = state.phase
    next_state = state
    events: list[ProtectionEvent] = []

    if trip is not None and clear is not None:
        if state.phase == "normal" and trip:
            if rule.trigger_delay_s == 0.0:
                next_state = _new_state(state, "active", frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind="triggered",
                        previous_phase=previous_phase,
                        new_phase="active",
                        reason="trip condition satisfied with zero trigger delay",
                    )
                )
            else:
                next_state = _new_state(state, "pending_trip", frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind="trip_pending",
                        previous_phase=previous_phase,
                        new_phase="pending_trip",
                        reason="trip condition detected; trigger delay started",
                    )
                )
        elif state.phase == "pending_trip":
            if not trip:
                next_state = _new_state(state, "normal", frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind="trip_cancelled",
                        previous_phase=previous_phase,
                        new_phase="normal",
                        reason="trip condition ended before its delay elapsed",
                    )
                )
            elif frame.time_s - state.phase_started_s >= rule.trigger_delay_s:
                next_state = _new_state(state, "active", frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind="triggered",
                        previous_phase=previous_phase,
                        new_phase="active",
                        reason="trip condition remained active through its delay",
                    )
                )
        elif state.phase == "active" and clear:
            if rule.clear_delay_s == 0.0:
                target: ProtectionPhase = "latched_clear" if rule.latching else "normal"
                kind: ProtectionEventKind = (
                    "latched_clear" if rule.latching else "cleared"
                )
                next_state = _new_state(state, target, frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind=kind,
                        previous_phase=previous_phase,
                        new_phase=target,
                        reason="clear condition satisfied with zero clear delay",
                    )
                )
            else:
                next_state = _new_state(state, "pending_clear", frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind="clear_pending",
                        previous_phase=previous_phase,
                        new_phase="pending_clear",
                        reason="clear condition detected; clear delay started",
                    )
                )
        elif state.phase == "pending_clear":
            if not clear:
                next_state = _new_state(state, "active", frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind="clear_cancelled",
                        previous_phase=previous_phase,
                        new_phase="active",
                        reason="clear condition ended before its delay elapsed",
                    )
                )
            elif frame.time_s - state.phase_started_s >= rule.clear_delay_s:
                target = "latched_clear" if rule.latching else "normal"
                kind = "latched_clear" if rule.latching else "cleared"
                next_state = _new_state(state, target, frame.time_s)
                events.append(
                    _transition_event(
                        rule,
                        frame,
                        event_kind=kind,
                        previous_phase=previous_phase,
                        new_phase=target,
                        reason="clear condition remained active through its delay",
                    )
                )
        elif state.phase == "latched_clear" and not clear:
            next_state = _new_state(state, "active", frame.time_s)
            events.append(
                _transition_event(
                    rule,
                    frame,
                    event_kind="clear_cancelled",
                    previous_phase=previous_phase,
                    new_phase="active",
                    reason="safe reset condition was lost while the action remained latched",
                )
            )

    if rule.rule_id in frame.reset_requests:
        phase_before_reset = next_state.phase
        if rule.latching and phase_before_reset == "latched_clear" and clear is True:
            reset_state = _new_state(next_state, "normal", frame.time_s)
            events.append(
                _transition_event(
                    rule,
                    frame,
                    event_kind="reset",
                    previous_phase=phase_before_reset,
                    new_phase="normal",
                    reason="explicit reset accepted after the clear condition",
                )
            )
            next_state = reset_state
        elif phase_before_reset != "normal":
            reason = (
                "reset rejected because the rule is not latching"
                if not rule.latching
                else "reset rejected because the clear condition is not established"
            )
            events.append(
                _transition_event(
                    rule,
                    frame,
                    event_kind="reset_rejected",
                    previous_phase=phase_before_reset,
                    new_phase=phase_before_reset,
                    reason=reason,
                )
            )

    return next_state, tuple(events)


def advance_protection(
    trace: ProtectionTrace,
    frame: ProtectionFrame,
) -> ProtectionTrace:
    """Advance all rules once and return a new deterministically ordered trace."""

    if not isinstance(trace, ProtectionTrace):
        raise TypeError("trace must be a ProtectionTrace")
    if not isinstance(frame, ProtectionFrame):
        raise TypeError("frame must be a ProtectionFrame")
    if frame.time_s < trace.start_time_s:
        raise ValueError("frame time cannot precede trace start")
    if trace.frames and frame.time_s <= trace.last_time_s:
        raise ValueError("frame time must be strictly greater than the last frame time")
    required_signals = {rule.signal_name for rule in trace.rules}
    if set(frame.signals) != required_signals:
        missing = sorted(required_signals - set(frame.signals))
        unknown = sorted(set(frame.signals) - required_signals)
        raise ValueError(
            f"frame signal names differ; missing={missing}, unknown={unknown}"
        )
    unknown_resets = sorted(set(frame.reset_requests) - {rule.rule_id for rule in trace.rules})
    if unknown_resets:
        raise ValueError("reset requests reference unknown rules: " + ", ".join(unknown_resets))

    states: dict[str, ProtectionRuleState] = {}
    new_events: list[ProtectionEvent] = []
    for rule in trace.rules:
        state, events = _advance_rule(rule, trace.states[rule.rule_id], frame)
        states[rule.rule_id] = state
        new_events.extend(events)
    all_events = tuple(sorted((*trace.events, *new_events), key=_event_sort_key))
    return ProtectionTrace(
        rules=trace.rules,
        states=states,
        start_time_s=trace.start_time_s,
        frames=(*trace.frames, frame),
        events=all_events,
    )


def run_protection(
    rules: Sequence[ProtectionRule],
    frames: Sequence[ProtectionFrame],
    *,
    start_time_s: float = 0.0,
) -> ProtectionTrace:
    """Evaluate a finite frame sequence through the pure protection supervisor."""

    if isinstance(frames, (str, bytes, bytearray)):
        raise TypeError("frames must be a sequence of ProtectionFrame values")
    trace = ProtectionTrace.initialize(rules, start_time_s=start_time_s)
    for frame in frames:
        trace = advance_protection(trace, frame)
    return trace


__all__ = [
    "ProtectionAction",
    "ProtectionCondition",
    "ProtectionEvent",
    "ProtectionEventKind",
    "ProtectionFrame",
    "ProtectionPhase",
    "ProtectionRule",
    "ProtectionRuleState",
    "ProtectionTrace",
    "advance_protection",
    "run_protection",
]
