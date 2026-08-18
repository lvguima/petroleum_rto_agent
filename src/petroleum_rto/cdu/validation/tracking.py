"""Deterministic M6 evidence for protection-to-PI manual tracking.

The helper deliberately operates on the already validated M4 digital PI
controller.  It does not alter the M4 closed-loop success contract: the manual
segment and the return to automatic are recorded as separate M6 synthetic
protection evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

from ..control.controllers import (
    AUTOMATIC,
    MANUAL,
    NormalizedPIController,
    PIControllerState,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _finite(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ControllerTrackingEvidence:
    """One bounded manual-tracking and automatic-return verification trace."""

    loop_id: str
    initial_output: float
    protected_output: float
    final_manual_output: float
    return_automatic_output: float
    manual_steps: int
    maximum_manual_output_change: float
    final_tracking_error: float
    automatic_return_jump: float
    tolerance: float
    passed: bool
    evidence_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.loop_id, str) or not self.loop_id.strip():
            raise ValueError("tracking loop_id must be non-empty")
        for name in (
            "initial_output",
            "protected_output",
            "final_manual_output",
            "return_automatic_output",
            "maximum_manual_output_change",
            "final_tracking_error",
            "automatic_return_jump",
            "tolerance",
        ):
            number = _finite(getattr(self, name), context=name)
            if name in {
                "maximum_manual_output_change",
                "final_tracking_error",
                "automatic_return_jump",
                "tolerance",
            } and number < 0.0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, number)
        if not isinstance(self.manual_steps, int) or isinstance(self.manual_steps, bool):
            raise TypeError("manual_steps must be an integer")
        if self.manual_steps <= 0:
            raise ValueError("manual_steps must be positive")
        if not isinstance(self.passed, bool):
            raise TypeError("tracking passed must be boolean")
        if not _SHA256.fullmatch(self.evidence_fingerprint):
            raise ValueError("tracking evidence fingerprint must be SHA-256")
        if self.evidence_fingerprint != _fingerprint(self._payload()):
            raise ValueError("tracking evidence fingerprint differs from content")

    def _payload(self) -> dict[str, object]:
        return {
            "loop_id": self.loop_id,
            "initial_output": self.initial_output,
            "protected_output": self.protected_output,
            "final_manual_output": self.final_manual_output,
            "return_automatic_output": self.return_automatic_output,
            "manual_steps": self.manual_steps,
            "maximum_manual_output_change": self.maximum_manual_output_change,
            "final_tracking_error": self.final_tracking_error,
            "automatic_return_jump": self.automatic_return_jump,
            "tolerance": self.tolerance,
            "passed": self.passed,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._payload(), "evidence_fingerprint": self.evidence_fingerprint}


def verify_controller_tracking(
    loop_id: str,
    controller: NormalizedPIController,
    initial_state: PIControllerState,
    *,
    process_value: float,
    target_setpoint: float,
    protected_output: float,
    control_interval_s: float,
    maximum_manual_steps: int,
    relative_tolerance: float,
    feedforward_output: float = 0.0,
) -> ControllerTrackingEvidence:
    """Track a protection command in manual and verify a no-bump auto return."""

    if not isinstance(loop_id, str) or not loop_id.strip():
        raise ValueError("tracking loop_id must be non-empty")
    if not isinstance(controller, NormalizedPIController):
        raise TypeError("controller must be a NormalizedPIController")
    if not isinstance(initial_state, PIControllerState):
        raise TypeError("initial_state must be a PIControllerState")
    if initial_state.mode != AUTOMATIC:
        raise ValueError("tracking verification must start in automatic mode")
    pv = _finite(process_value, context="process_value")
    target = _finite(target_setpoint, context="target_setpoint")
    protected = _finite(protected_output, context="protected_output")
    interval = _finite(control_interval_s, context="control_interval_s")
    tolerance = _finite(relative_tolerance, context="relative_tolerance")
    feedforward = _finite(feedforward_output, context="feedforward_output")
    if interval <= 0.0:
        raise ValueError("control_interval_s must be positive")
    if tolerance <= 0.0:
        raise ValueError("relative_tolerance must be positive")
    if (
        not isinstance(maximum_manual_steps, int)
        or isinstance(maximum_manual_steps, bool)
        or maximum_manual_steps <= 0
    ):
        raise ValueError("maximum_manual_steps must be a positive integer")

    absolute_tolerance = tolerance * max(abs(protected), controller.output_scale, 1.0)
    state = initial_state
    prior_output = initial_state.output_normalized * controller.output_scale
    maximum_change = 0.0
    final_output = prior_output
    used_steps = 0
    for step in range(1, maximum_manual_steps + 1):
        update = controller.update(
            state,
            process_value=pv,
            target_setpoint=target,
            dt_s=interval,
            requested_mode=MANUAL,
            manual_output=protected,
            feedforward_output=feedforward,
        )
        final_output = update.output
        maximum_change = max(maximum_change, abs(final_output - prior_output))
        prior_output = final_output
        state = update.state
        used_steps = step
        if abs(final_output - protected) <= absolute_tolerance:
            break

    automatic = controller.update(
        state,
        process_value=pv,
        target_setpoint=target,
        dt_s=0.0,
        requested_mode=AUTOMATIC,
        feedforward_output=feedforward,
    )
    tracking_error = abs(final_output - protected)
    return_jump = abs(automatic.output - final_output)
    passed = (
        tracking_error <= absolute_tolerance
        and return_jump <= absolute_tolerance
        and automatic.state.mode == AUTOMATIC
        and state.mode == MANUAL
    )
    initial_output = initial_state.output_normalized * controller.output_scale
    payload: dict[str, object] = {
        "loop_id": loop_id,
        "initial_output": initial_output,
        "protected_output": protected,
        "final_manual_output": final_output,
        "return_automatic_output": automatic.output,
        "manual_steps": used_steps,
        "maximum_manual_output_change": maximum_change,
        "final_tracking_error": tracking_error,
        "automatic_return_jump": return_jump,
        "tolerance": absolute_tolerance,
        "passed": passed,
    }
    return ControllerTrackingEvidence(
        loop_id=loop_id,
        initial_output=initial_output,
        protected_output=protected,
        final_manual_output=final_output,
        return_automatic_output=automatic.output,
        manual_steps=used_steps,
        maximum_manual_output_change=maximum_change,
        final_tracking_error=tracking_error,
        automatic_return_jump=return_jump,
        tolerance=absolute_tolerance,
        passed=passed,
        evidence_fingerprint=_fingerprint(payload),
    )
