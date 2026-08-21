"""Protocols separating RTO core logic from a concrete simulator provider."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ..contracts import (
    JsonValue,
    OperatingContextV1,
    SimulationEvaluationRequestV1,
    SimulationPreviewV1,
    SimulationRunBundleV1,
)


class ProviderRequestFactory(Protocol):
    """Compile canonical RTO values into one provider's public request mapping."""

    @property
    def provider_id(self) -> str: ...

    @property
    def compiler_version(self) -> str: ...

    def build_m2_request(
        self,
        context: OperatingContextV1,
        decision_values: Mapping[str, float],
    ) -> Mapping[str, JsonValue]: ...

    def build_m4_request(
        self,
        context: OperatingContextV1,
        decision_values: Mapping[str, float],
        *,
        candidate: bool,
        event_time_s: float,
        duration_s: float,
        time_step_s: float,
    ) -> Mapping[str, JsonValue]: ...


class SimulatorPort(Protocol):
    """Stable preview, evaluate and strict evidence-read boundary."""

    def preview(self, request: SimulationEvaluationRequestV1) -> SimulationPreviewV1: ...

    def evaluate(
        self,
        request: SimulationEvaluationRequestV1,
        expected_preview_fingerprint: str,
    ) -> SimulationRunBundleV1: ...

    def read_evidence(self, run_ref: Path) -> SimulationRunBundleV1: ...
