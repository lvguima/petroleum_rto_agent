"""Neutral ports used by the objective-count-independent RTO pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ..contracts.common import JsonValue
from ..contracts.context import OperatingContext
from ..contracts.simulation import (
    SimulationEvaluationRequest,
    SimulationPreview,
    SimulationRunBundle,
)


class UnifiedProviderRequestFactory(Protocol):
    """Compile canonical decisions without exposing provider-internal paths."""

    @property
    def provider_id(self) -> str: ...

    @property
    def compiler_version(self) -> str: ...

    def build_m2_request(
        self,
        context: OperatingContext,
        decision_values: Mapping[str, float],
    ) -> Mapping[str, JsonValue]: ...

    def build_m4_request(
        self,
        context: OperatingContext,
        decision_values: Mapping[str, float],
        *,
        candidate: bool,
        event_time_s: float,
        duration_s: float,
        time_step_s: float,
    ) -> Mapping[str, JsonValue]: ...


class UnifiedSimulatorPort(Protocol):
    """Preview, execute, and strictly reload one simulator request."""

    def preview(self, request: SimulationEvaluationRequest) -> SimulationPreview: ...

    def evaluate(
        self,
        request: SimulationEvaluationRequest,
        expected_preview_fingerprint: str,
    ) -> SimulationRunBundle: ...

    def read_evidence(self, run_ref: Path) -> SimulationRunBundle: ...


__all__ = ["UnifiedProviderRequestFactory", "UnifiedSimulatorPort"]
