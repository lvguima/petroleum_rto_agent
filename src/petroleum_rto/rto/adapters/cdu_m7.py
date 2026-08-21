"""CDU M7 request compiler and strict evidence adapter for the neutral RTO port."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

from petroleum_rto.cdu.runtime import (
    RunRecord,
    RunRequest,
    RuntimeInputEvent,
    RuntimeScenarioRequest,
    load_preset,
    preview,
    read_run,
    run,
)

from ..contracts.common import JsonValue, thaw_json
from ..contracts.context import OperatingContext
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE
from ..contracts.reference import ContractRef
from ..contracts.simulation import (
    SIMULATION_SCHEMA_VERSION,
    SimulationEvaluationRequest,
    SimulationPreview,
    SimulationRunBundle,
)

ATMOSPHERIC_PRESSURE_PA: Final[float] = 101_325.0


class CduM7RequestFactory:
    """Translate canonical SI decisions into the public M7 RunRequest contract."""

    @property
    def provider_id(self) -> str:
        return "cdu-m7-v1"

    @property
    def compiler_version(self) -> str:
        return "cdu-m7-candidate-compiler-v1"

    @staticmethod
    def _validate_context(context: OperatingContext) -> None:
        if context.provider_id != "cdu-m7":
            raise ValueError("operating context belongs to another simulator provider")

    @staticmethod
    def _context_feed_mass_flow_kg_s(
        context: OperatingContext,
    ) -> float:
        value = context.facts.get("fresh_feed_load_kg_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("context requires numeric fresh_feed_load_kg_s")
        result = float(value)
        if not math.isfinite(result) or result <= 0.0:
            raise ValueError("fresh_feed_load_kg_s must be positive and finite")
        return result

    @staticmethod
    def _complete_decisions(
        context: OperatingContext,
        decision_values: Mapping[str, float],
    ) -> tuple[dict[str, float], frozenset[str]]:
        expected = {
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        }
        selected = frozenset(decision_values)
        if not selected or not selected.issubset(expected):
            raise ValueError("CDU M7 received an empty or unsupported decision vector")
        completed = dict(context.current_setpoints)
        if set(completed) != expected:
            raise ValueError("CDU M7 context must define both high-level setpoints")
        completed.update(decision_values)
        temperature_k = float(completed["furnace_temperature_target_k"])
        pressure_pa_a = float(completed["tower_top_pressure_target_pa_a"])
        if not math.isfinite(temperature_k) or not math.isfinite(pressure_pa_a):
            raise ValueError("CDU M7 decision values must be finite")
        if temperature_k <= 0.0 or pressure_pa_a <= ATMOSPHERIC_PRESSURE_PA:
            raise ValueError("CDU M7 decision values must be positive absolute quantities")
        return completed, selected

    @staticmethod
    def _parameters(
        context: OperatingContext,
        decision_values: Mapping[str, float],
    ) -> dict[str, float]:
        completed, _ = CduM7RequestFactory._complete_decisions(context, decision_values)
        temperature_k = completed["furnace_temperature_target_k"]
        pressure_pa_a = completed["tower_top_pressure_target_pa_a"]
        return {
            "feed.mass_flow_t_h": round(
                CduM7RequestFactory._context_feed_mass_flow_kg_s(context) * 3.6,
                12,
            ),
            "operating.furnace_outlet_temperature_c": round(temperature_k - 273.15, 12),
            "operating.tower_top_pressure_mpa_g": round(
                (pressure_pa_a - ATMOSPHERIC_PRESSURE_PA) / 1_000_000.0,
                12,
            ),
        }

    @staticmethod
    def _initial_state(
        context: OperatingContext,
    ) -> dict[str, float]:
        ratios = context.initial_state
        return {
            "inventory.flash_drum_ratio": ratios["flash_drum"],
            "inventory.reflux_drum_ratio": ratios["reflux_drum"],
            "inventory.tower_bottom_ratio": ratios["tower_bottom"],
        }

    def build_m2_request(
        self,
        context: OperatingContext,
        decision_values: Mapping[str, float],
    ) -> Mapping[str, JsonValue]:
        self._validate_context(context)
        base = load_preset("steady-baseline")
        request = replace(base, parameters=self._parameters(context, decision_values))
        return cast(Mapping[str, JsonValue], request.fingerprint_payload())

    def build_m4_request(
        self,
        context: OperatingContext,
        decision_values: Mapping[str, float],
        *,
        candidate: bool,
        event_time_s: float,
        duration_s: float,
        time_step_s: float,
    ) -> Mapping[str, JsonValue]:
        self._validate_context(context)
        completed, selected = self._complete_decisions(context, decision_values)
        baseline_values = context.current_setpoints
        baseline_completed, _ = self._complete_decisions(context, baseline_values)
        if candidate:
            events = tuple(
                RuntimeInputEvent(
                    time_s=event_time_s,
                    target=f"{loop_id}.setpoint_ratio",
                    value=completed[variable_id] / baseline_completed[variable_id],
                    value_basis="setpoint_ratio",
                    duration_s=None,
                )
                for variable_id, loop_id in (
                    ("furnace_temperature_target_k", "furnace_temperature"),
                    ("tower_top_pressure_target_pa_a", "top_pressure"),
                )
                if variable_id in selected
            )
        else:
            events = ()
        scenario = RuntimeScenarioRequest(
            duration_s=duration_s,
            time_step_s=time_step_s,
            events=events,
        )
        base = load_preset("closed-loop-feed-step")
        request = replace(
            base,
            parameters=self._parameters(context, baseline_values),
            initial_state=self._initial_state(context),
            scenario=scenario,
        )
        return cast(Mapping[str, JsonValue], request.fingerprint_payload())


class CduM7Simulator:
    """Synchronous, file-evidenced implementation of the neutral SimulatorPort."""

    def __init__(self, output_root: Path) -> None:
        if not isinstance(output_root, Path):
            raise TypeError("output_root must be a pathlib.Path")
        self._output_root = output_root

    @staticmethod
    def _provider_request(request: SimulationEvaluationRequest) -> RunRequest:
        if request.provider_id != "cdu-m7-v1":
            raise ValueError("simulation request targets another provider")
        raw = thaw_json(cast(JsonValue, request.provider_request))
        if not isinstance(raw, dict):  # pragma: no cover - contract guarantees mapping
            raise TypeError("provider request did not thaw to an object")
        provider_request = RunRequest.from_mapping(cast(Mapping[str, object], raw))
        expected = {
            "M2": ("steady_recycle", "steady-baseline"),
            "M4": ("closed_loop_dynamic", "closed-loop-feed-step"),
        }[request.stage]
        if (provider_request.run_type, provider_request.preset_id) != expected:
            raise ValueError("provider request run type or preset differs from its RTO stage")
        return provider_request

    @staticmethod
    def _request_ref(request: SimulationEvaluationRequest) -> ContractRef:
        return ContractRef(request.request_id, request.fingerprint)

    def preview(self, request: SimulationEvaluationRequest) -> SimulationPreview:
        if not isinstance(request, SimulationEvaluationRequest):
            raise TypeError("preview requires a SimulationEvaluationRequest")
        resolved = preview(self._provider_request(request))
        return SimulationPreview(
            schema_version=SIMULATION_SCHEMA_VERSION,
            preview_version="simulation-preview",
            simulation_request_ref=self._request_ref(request),
            provider_id="cdu-m7-v1",
            provider_preview_fingerprint=resolved.preview_fingerprint,
            effective_input_fingerprint=resolved.execution_input_fingerprint,
            base_object_fingerprints=resolved.base_object_fingerprints,
            effective_object_fingerprints=resolved.effective_object_fingerprints,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    def evaluate(
        self,
        request: SimulationEvaluationRequest,
        expected_preview_fingerprint: str,
    ) -> SimulationRunBundle:
        if not isinstance(request, SimulationEvaluationRequest):
            raise TypeError("evaluate requires a SimulationEvaluationRequest")
        provider_request = self._provider_request(request)
        current_preview = preview(provider_request)
        if expected_preview_fingerprint != current_preview.preview_fingerprint:
            raise ValueError("expected preview fingerprint differs from current provider inputs")
        record = run(
            provider_request,
            output_root=self._output_root,
            expected_preview_fingerprint=expected_preview_fingerprint,
        )
        verified = read_run(record.run_dir)
        return self._bundle(verified)

    def read_evidence(self, run_ref: Path) -> SimulationRunBundle:
        if not isinstance(run_ref, Path):
            raise TypeError("run_ref must be a pathlib.Path")
        return self._bundle(read_run(run_ref))

    @staticmethod
    def _bundle(record: RunRecord) -> SimulationRunBundle:
        return SimulationRunBundle(
            schema_version=SIMULATION_SCHEMA_VERSION,
            bundle_version="simulation-run-bundle",
            provider_id="cdu-m7-v1",
            provider_request_fingerprint=record.request.request_fingerprint,
            run_ref=str(record.run_dir.resolve()),
            runtime_status=record.payload.runtime_status,
            engine_status=record.payload.engine_status,
            summary=record.payload.summary,
            sample_count=len(record.payload.timeseries),
            event_count=len(record.payload.events),
            request_fingerprint=record.manifest.request_fingerprint,
            effective_input_fingerprint=record.manifest.effective_input_fingerprint,
            result_fingerprint=record.manifest.result_fingerprint,
            manifest_fingerprint=record.manifest.manifest_fingerprint,
            versions=record.manifest.versions,
            source_fingerprints=record.manifest.source_fingerprints,
            failure_stage=record.payload.failure_stage,
            failure_reason=record.payload.failure_reason,
            synthetic=record.manifest.synthetic,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
