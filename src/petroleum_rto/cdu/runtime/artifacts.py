"""Manifest-last, tamper-evident M7 run directories and reader."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Final, TextIO, cast, overload

from petroleum_rto import __version__ as SOFTWARE_VERSION

from ..control.scenario import ClosedLoopScenarioConfig
from ..core.config import ScenarioConfig, canonical_fingerprint
from ..validation.config import ValidationScenarioSpec
from ..validation.domain import assess_applicability
from .contracts import (
    RUN_REQUEST_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
    ExecutionPayload,
    JsonValue,
    RunRequest,
)
from .custom_inputs import (
    ResolvedRuntimeInputs,
    resolve_runtime_inputs,
    validate_runtime_request_shape,
)
from .presets import RuntimePreset, get_preset
from .provenance import installed_source_tree_sha256, runtime_environment
from .resources import (
    RuntimeResourceBundle,
    list_runtime_resource_ids,
    load_runtime_resource_bundle,
    read_runtime_resource_bytes,
    runtime_resource_ids_for_preset,
)

RUN_MANIFEST_VERSION: Final[str] = "cdu-mini-run-manifest-v0.3.0"
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RUNTIME_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "limited", "rejected", "failed", "not_converged"}
)
_DOMAIN_STATUSES: Final[frozenset[str]] = frozenset(
    {"not_applicable", "passed", "limited", "rejected"}
)
_REQUIRED_NON_FAILURE_VERSION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "base_case_version",
        "base_parameter_set_version",
        "case_version",
        "derived_case_version",
        "derived_parameter_set_version",
        "m5_overlay_version",
        "model_config_version",
        "model_version",
        "parameter_set_version",
        "scenario_version",
        "software_version",
    }
)
_REQUIRED_EFFECTIVE_FINGERPRINT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "effective_object.calibrated_model_object",
        "effective_object.component_catalog_object",
        "effective_object.effective_case_object",
        "m5_pipeline_result",
    }
)
_REQUIRED_ENVIRONMENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "distribution_version",
        "git_commit",
        "git_dirty",
        "machine",
        "operating_system",
        "os_release",
        "python_full_version",
        "python_implementation",
        "python_version",
    }
)
_M6_PUMP_PRESET_ID: Final[str] = "m6-abnormal-pump-trip"
_M6_REJECTION_PRESET_ID: Final[str] = "m6-structural-rejection"
_M6_PUMP_RULE_ID: Final[str] = "pump_around_1_invalid"
_M6_DURATION_S: Final[float] = 600.0
_M6_EVENT_TIME_S: Final[float] = 60.0
_M6_TIME_STEP_S: Final[float] = 1.0
_EXPECTED_PRESET_STATUS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "steady-baseline": "success",
        "open-loop-feed-step": "success",
        "closed-loop-feed-step": "success",
        "m6-abnormal-pump-trip": "limited",
        "m6-structural-rejection": "rejected",
    }
)
_EXPECTED_PRESET_SAMPLE_COUNT: Final[Mapping[str, int]] = MappingProxyType(
    {
        "steady-baseline": 0,
        "open-loop-feed-step": 7201,
        "closed-loop-feed-step": 7201,
        "m6-abnormal-pump-trip": 601,
        "m6-structural-rejection": 0,
    }
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _plain_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_json_bytes(dict(value)).rstrip(b"\n")).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_keys(
    value: Mapping[str, object],
    expected: set[str],
    *,
    context: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{context} fields differ from the fixed contract")


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def _identifier(value: object, *, context: str) -> str:
    text = _text(value, context=context)
    if not _IDENTIFIER.fullmatch(text) or ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"{context} must be a safe identifier")
    return text


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, *, context: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{context} must be an integer >= {minimum}")
    return value


def _finite(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{context} must be finite and non-negative")
    return number


def _timestamp(value: object, *, context: str) -> str:
    text = _text(value, context=context)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{context} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must contain a UTC offset")
    return text


def _string_mapping(value: object, *, context: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be an object with string keys")
    copied: dict[str, str] = {}
    for key, item in value.items():
        copied[_identifier(key, context=f"{context} key")] = _text(
            item,
            context=f"{context}.{key}",
        )
    return MappingProxyType(copied)


def _digest_mapping(value: object, *, context: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be an object with string keys")
    copied: dict[str, str] = {}
    for key, item in value.items():
        safe_key = _identifier(key, context=f"{context} key")
        copied[safe_key] = _digest(item, context=f"{context}.{key}")
    return MappingProxyType(copied)


def _domain_status(payload: ExecutionPayload) -> str:
    value = payload.diagnostics.get("domain_status", "not_applicable")
    if not isinstance(value, str) or value not in _DOMAIN_STATUSES:
        raise ValueError("execution domain_status differs from the manifest contract")
    return value


@dataclass(frozen=True)
class DerivedRunProvenance:
    """Source-closed evidence derivable from a fixed request without solving models."""

    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    effective_input_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "versions",
            _string_mapping(self.versions, context="derived versions"),
        )
        object.__setattr__(
            self,
            "source_fingerprints",
            _digest_mapping(
                self.source_fingerprints,
                context="derived source_fingerprints",
            ),
        )
        object.__setattr__(
            self,
            "effective_input_fingerprint",
            _digest(
                self.effective_input_fingerprint,
                context="derived effective_input_fingerprint",
            ),
        )


def _resolve_executable_preset(request: RunRequest) -> RuntimePreset:
    try:
        preset = get_preset(request.preset_id)
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"request references an unknown fixed preset: {request.preset_id!r}"
        ) from exc
    if request.run_type != preset.run_type:
        raise ValueError("request run_type differs from its fixed preset")
    validate_runtime_request_shape(request)
    return preset


def _resource_ids_for_request(request: RunRequest) -> tuple[str, ...]:
    try:
        preset = get_preset(request.preset_id)
    except (KeyError, TypeError):
        return list_runtime_resource_ids()
    if request.run_type != preset.run_type:
        return list_runtime_resource_ids()
    return runtime_resource_ids_for_preset(preset)


def _common_versions(bundle: RuntimeResourceBundle) -> dict[str, str]:
    overlay = bundle.m5_overlay
    versions = {
        "base_case_version": overlay.base_case_version,
        "base_parameter_set_version": overlay.base_parameter_set_version,
        "case_version": bundle.effective_case.case_version,
        "derived_case_version": overlay.derived_case_version,
        "derived_parameter_set_version": overlay.derived_parameter_set_version,
        "m5_overlay_version": overlay.overlay_version,
        "model_config_version": bundle.effective_model.config_version,
        "model_version": bundle.effective_model.model_version,
        "parameter_set_version": bundle.effective_model.parameter_set_version,
        "scenario_version": "not_applicable",
        "software_version": SOFTWARE_VERSION,
    }
    if bundle.control is not None:
        versions["control_version"] = bundle.control.control_version
    if bundle.validation_config is not None:
        versions.update(
            {
                "analysis_basis_version": bundle.validation_config.analysis_basis_version,
                "validation_version": bundle.validation_config.validation_version,
            }
        )
    return versions


def _base_source_fingerprints(bundle: RuntimeResourceBundle) -> dict[str, str]:
    fingerprints = dict(bundle.resource_fingerprints)
    fingerprints.update(
        {
            "m5_manifest": bundle.m5_overlay.m5_manifest_sha256,
            "m5_pipeline_result": bundle.m5_overlay.pipeline_result_fingerprint,
        }
    )
    fingerprints.update(
        {
            f"m5_artifact.{name}": digest
            for name, digest in bundle.m5_overlay.m5_artifact_sha256.items()
        }
    )
    fingerprints.update(
        {
            "effective_object.calibrated_model_object": (
                bundle.m5_overlay.calibrated_model_object_fingerprint
            ),
            "effective_object.effective_case_object": (
                bundle.m5_overlay.effective_case_object_fingerprint
            ),
            "effective_object.component_catalog_object": (
                bundle.m5_overlay.component_catalog_object_fingerprint
            ),
        }
    )
    if bundle.m6_result_fingerprint is not None:
        fingerprints["m6_formal_result"] = bundle.m6_result_fingerprint
    return {name: fingerprints[name] for name in sorted(fingerprints)}


def _effective_fingerprint(
    bundle: RuntimeResourceBundle,
    request: RunRequest,
    *,
    extra: Mapping[str, object] | None = None,
) -> str:
    value: dict[str, object] = {
        "request": request.fingerprint_payload(),
        "effective_model": bundle.effective_model.as_dict(),
        "effective_case": bundle.effective_case.as_dict(),
        "component_catalog": bundle.catalog.as_dict(),
        "m5_analysis_basis": bundle.m5_overlay.analysis_basis_fingerprint,
        "resource_fingerprints": dict(bundle.resource_fingerprints),
    }
    if extra is not None:
        value.update(extra)
    return canonical_fingerprint(value)


def _open_loop_scenario(
    bundle: RuntimeResourceBundle,
    preset: RuntimePreset,
) -> ScenarioConfig:
    try:
        return next(
            scenario
            for scenario in bundle.open_loop_scenarios.values()
            if scenario.scenario_version == preset.scenario_id
        )
    except StopIteration as exc:
        raise ValueError("fixed preset has no matching packaged open-loop scenario") from exc


def _closed_loop_scenario(
    bundle: RuntimeResourceBundle,
    preset: RuntimePreset,
) -> ClosedLoopScenarioConfig:
    try:
        return next(
            scenario
            for scenario in bundle.closed_loop_scenarios.values()
            if scenario.scenario_version == preset.scenario_id
        )
    except StopIteration as exc:
        raise ValueError("fixed preset has no matching packaged closed-loop scenario") from exc


def _validation_scenario(
    bundle: RuntimeResourceBundle,
    preset: RuntimePreset,
) -> ValidationScenarioSpec:
    config = bundle.require_validation_config()
    try:
        return next(
            scenario for scenario in config.scenarios if scenario.scenario_id == preset.scenario_id
        )
    except StopIteration as exc:
        raise ValueError("fixed preset has no matching packaged M6 scenario") from exc


def _resolved_run_provenance(
    resolved: ResolvedRuntimeInputs,
    bundle: RuntimeResourceBundle,
) -> DerivedRunProvenance:
    versions = _common_versions(bundle)
    sources = _base_source_fingerprints(bundle)
    sources.update(
        {
            "runtime_custom_input_preview": resolved.preview_fingerprint,
            **{
                f"runtime_effective_object.{name}": digest
                for name, digest in resolved.effective_object_fingerprints.items()
            },
        }
    )
    if resolved.preset.engine_layer == "M2":
        versions["simulation_stage"] = "M2"
    elif resolved.preset.engine_layer == "M3":
        if resolved.open_loop_scenario is None:
            raise ValueError("custom M3 provenance lacks its resolved scenario")
        versions.update(
            {
                "config_version": resolved.model.config_version,
                "scenario_version": resolved.open_loop_scenario.scenario_version,
                "simulation_stage": "M3",
            }
        )
    elif resolved.preset.engine_layer == "M4":
        if resolved.closed_loop_scenario is None:
            raise ValueError("custom M4 provenance lacks its resolved scenario")
        control = bundle.require_control()
        versions.update(
            {
                "config_version": resolved.model.config_version,
                "control_version": control.control_version,
                "scenario_version": resolved.closed_loop_scenario.scenario_version,
                "simulation_stage": "M4",
            }
        )
        sources["control_input"] = control.input_fingerprint
    else:  # pragma: no cover - custom M6 is rejected during shape validation
        raise ValueError("portable M6 provenance does not accept custom inputs")
    return DerivedRunProvenance(
        versions=versions,
        source_fingerprints={name: sources[name] for name in sorted(sources)},
        effective_input_fingerprint=resolved.execution_input_fingerprint,
    )


def _normal_run_provenance(
    request: RunRequest,
    preset: RuntimePreset,
    bundle: RuntimeResourceBundle,
    *,
    resolved: ResolvedRuntimeInputs | None = None,
) -> DerivedRunProvenance:
    actual_resolved = (
        resolve_runtime_inputs(request, bundle=bundle) if resolved is None else resolved
    )
    if actual_resolved.request != request or actual_resolved.preset != preset:
        raise ValueError("resolved provenance inputs differ from the request or preset")
    if actual_resolved.is_custom:
        return _resolved_run_provenance(actual_resolved, bundle)
    versions = _common_versions(bundle)
    source_fingerprints = _base_source_fingerprints(bundle)
    extra: dict[str, object] | None = None
    if preset.engine_layer == "M2":
        versions["simulation_stage"] = "M2"
    elif preset.engine_layer == "M3":
        open_scenario = _open_loop_scenario(bundle, preset)
        versions["config_version"] = bundle.effective_model.config_version
        versions["scenario_version"] = open_scenario.scenario_version
        versions["simulation_stage"] = "M3"
        extra = {"scenario": open_scenario.as_dict()}
    elif preset.engine_layer == "M4":
        control = bundle.require_control()
        closed_scenario = _closed_loop_scenario(bundle, preset)
        versions["config_version"] = bundle.effective_model.config_version
        versions["control_version"] = control.control_version
        versions["scenario_version"] = closed_scenario.scenario_version
        versions["simulation_stage"] = "M4"
        source_fingerprints["control_input"] = control.input_fingerprint
        extra = {
            "control": control.as_dict(),
            "scenario": closed_scenario.as_dict(),
        }
    else:
        control = bundle.require_control()
        config = bundle.require_validation_config()
        formal_m6_result = bundle.require_m6_result_fingerprint()
        validation_scenario = _validation_scenario(bundle, preset)
        versions.update(
            {
                "control_version": control.control_version,
                "execution_profile": "portable_selected_scenario_replay",
                "scenario_version": validation_scenario.scenario_version,
                "simulation_stage": "M6",
                "validation_version": config.validation_version,
            }
        )
        source_fingerprints["formal_m6_result"] = formal_m6_result
        if preset.preset_id == _M6_PUMP_PRESET_ID:
            if (
                preset.duration_s != _M6_DURATION_S
                or preset.time_step_s != _M6_TIME_STEP_S
                or validation_scenario.execution_reference != "m3.pump_around_1_duty_ratio"
                or validation_scenario.inputs.get("pump_around_1_duty_ratio") != 0.0
            ):
                raise ValueError("portable M6 pump-trip preset differs from its frozen grid")
            try:
                rule = next(
                    item for item in config.protection_rules if item.rule_id == _M6_PUMP_RULE_ID
                )
            except StopIteration as exc:
                raise ValueError("portable M6 pump-trip protection rule is unavailable") from exc
            extra = {
                "control": control.as_dict(),
                "m6_config": config.as_dict(),
                "scenario": validation_scenario.as_dict(),
                "protection_rule": rule.as_dict(),
                "execution_profile": "portable_selected_scenario_replay",
            }
        elif preset.preset_id == _M6_REJECTION_PRESET_ID:
            domain = assess_applicability(
                config.domain_dimensions,
                validation_scenario.inputs,
                abnormal_verification=validation_scenario.abnormal_verification,
            )
            if domain.status != "rejected":
                raise ValueError("portable M6 structural preset is not rejected")
            extra = {
                "m6_config": config.as_dict(),
                "scenario": validation_scenario.as_dict(),
                "domain": domain.as_dict(),
                "execution_profile": "portable_selected_scenario_replay",
            }
        else:  # pragma: no cover - fixed registry guarded by tests
            raise ValueError("unsupported portable M6 fixed preset")
    effective = _effective_fingerprint(bundle, request, extra=extra)
    if preset.preset_id == _M6_PUMP_PRESET_ID:
        validation_scenario = _validation_scenario(bundle, preset)
        baseline_fingerprint = canonical_fingerprint(
            {
                "effective_input": effective,
                "scenario": validation_scenario.as_dict(),
                "variant": "baseline",
                "duration_s": _M6_DURATION_S,
                "time_step_s": _M6_TIME_STEP_S,
            }
        )
        candidate_fingerprint = canonical_fingerprint(
            {
                "effective_input": effective,
                "scenario": validation_scenario.as_dict(),
                "target": "pump_around_1_duty_w",
                "value": 0.0,
                "event_time_s": _M6_EVENT_TIME_S,
                "duration_s": _M6_DURATION_S,
                "time_step_s": _M6_TIME_STEP_S,
            }
        )
        source_fingerprints.update(
            {
                "m6_portable_baseline": baseline_fingerprint,
                "m6_portable_candidate": candidate_fingerprint,
            }
        )
    return DerivedRunProvenance(
        versions=versions,
        source_fingerprints={
            name: source_fingerprints[name] for name in sorted(source_fingerprints)
        },
        effective_input_fingerprint=effective,
    )


def derive_run_provenance(request: RunRequest) -> DerivedRunProvenance:
    """Derive the exact normal-run lineage from bundled inputs without a solve."""

    if not isinstance(request, RunRequest):
        raise TypeError("derive_run_provenance requires a RunRequest")
    preset = _resolve_executable_preset(request)
    return _normal_run_provenance(
        request,
        preset,
        load_runtime_resource_bundle(preset),
    )


def _assert_mapping_equal(
    actual: Mapping[str, str],
    expected: Mapping[str, str],
    *,
    context: str,
) -> None:
    if dict(actual) != dict(expected):
        raise ValueError(f"execution {context} differs from packaged derived provenance")


def _validate_fixed_preset_shape(
    preset: RuntimePreset,
    payload: ExecutionPayload,
    *,
    resolved: ResolvedRuntimeInputs | None = None,
) -> None:
    if resolved is not None and resolved.is_custom:
        if payload.duration_s != resolved.duration_s or payload.time_step_s != resolved.time_step_s:
            raise ValueError("execution time grid differs from resolved custom inputs")
        if resolved.time_step_s is None or resolved.duration_s is None:
            if payload.timeseries:
                raise ValueError("steady custom execution cannot contain a timeseries")
            return
        sample_times: list[float] = []
        for sample in payload.timeseries:
            time_value = sample.get("time_s")
            if (
                isinstance(time_value, bool)
                or not isinstance(time_value, (int, float))
                or not math.isfinite(float(time_value))
            ):
                raise TypeError("custom-grid timeseries samples require finite numeric time_s")
            sample_times.append(float(time_value))
        if any(later <= earlier for earlier, later in pairwise(sample_times)):
            raise ValueError("custom execution sample times must strictly increase")
        if sample_times and (
            sample_times[0] != 0.0 or sample_times[-1] > resolved.duration_s + 1.0e-10
        ):
            raise ValueError("custom execution sample times exceed the resolved grid")
        output_times = tuple(
            index * resolved.time_step_s
            for index in range(round(resolved.duration_s / resolved.time_step_s) + 1)
        )
        if preset.engine_layer == "M3":
            if payload.runtime_status == "success" and len(sample_times) != len(output_times):
                raise ValueError("custom M3 sample count differs from its output grid")
            for actual, expected_time in zip(
                sample_times,
                output_times,
                strict=False,
            ):
                if not math.isclose(
                    actual,
                    expected_time,
                    rel_tol=0.0,
                    abs_tol=1.0e-12 * max(abs(expected_time), 1.0),
                ):
                    raise ValueError("custom M3 sample times differ from its output grid")
        elif (
            preset.engine_layer == "M4"
            and sample_times
            and math.isclose(
                sample_times[-1],
                resolved.duration_s,
                rel_tol=0.0,
                abs_tol=1.0e-10 * max(resolved.duration_s, 1.0),
            )
        ):
            position = 0
            for expected_time in output_times:
                while position < len(sample_times) and sample_times[position] < (
                    expected_time - 1.0e-10
                ):
                    position += 1
                if position == len(sample_times) or not math.isclose(
                    sample_times[position],
                    expected_time,
                    rel_tol=0.0,
                    abs_tol=1.0e-10 * max(abs(expected_time), 1.0),
                ):
                    raise ValueError("custom M4 samples omit an output-grid endpoint")
        return
    expected_status = _EXPECTED_PRESET_STATUS[preset.preset_id]
    if payload.runtime_status in {"success", "limited", "rejected"} and (
        payload.runtime_status != expected_status
    ):
        raise ValueError("execution status differs from fixed preset semantics")
    if payload.duration_s != preset.duration_s or payload.time_step_s != preset.time_step_s:
        raise ValueError("execution time grid differs from fixed preset semantics")

    expected_count = _EXPECTED_PRESET_SAMPLE_COUNT[preset.preset_id]
    actual_count = len(payload.timeseries)
    if payload.runtime_status == expected_status:
        if actual_count != expected_count:
            raise ValueError("execution sample count differs from fixed preset semantics")
    elif actual_count > expected_count:
        raise ValueError("failed execution contains more samples than its fixed grid")

    if payload.runtime_status != expected_status or preset.time_step_s is None:
        return
    for index, sample in enumerate(payload.timeseries):
        time_value = sample.get("time_s")
        if (
            isinstance(time_value, bool)
            or not isinstance(time_value, (int, float))
            or not math.isfinite(float(time_value))
        ):
            raise TypeError("fixed-grid timeseries samples require finite numeric time_s")
        expected_time = index * preset.time_step_s
        if not math.isclose(
            float(time_value),
            expected_time,
            rel_tol=0.0,
            abs_tol=1.0e-12 * max(abs(expected_time), 1.0),
        ):
            raise ValueError("execution sample times differ from fixed preset grid")


def _validate_source_fingerprints(
    payload: ExecutionPayload,
    expected: Mapping[str, str],
    *,
    preset: RuntimePreset,
) -> None:
    actual = dict(payload.source_fingerprints)
    expected_values = dict(expected)
    if preset.engine_layer in {"M3", "M4"}:
        engine_source = actual.pop("engine_source", None)
        if engine_source is not None:
            if payload.summary.get("source_fingerprint") != engine_source:
                raise ValueError("execution engine source differs from its engine result summary")
        elif payload.runtime_status in {"success", "limited"}:
            raise ValueError("successful dynamic execution lacks its engine source")
    if (
        preset.engine_layer == "M4"
        and "control_input" in expected_values
        and payload.summary.get("control_fingerprint") != expected_values["control_input"]
    ):
        raise ValueError("execution control source differs from its engine result summary")
    if (
        preset.engine_layer == "M6_portable"
        and "formal_m6_result" in expected_values
        and payload.summary.get("formal_m6_result_fingerprint")
        != expected_values["formal_m6_result"]
    ):
        raise ValueError("execution formal M6 source differs from its scenario result summary")
    _assert_mapping_equal(
        actual,
        expected_values,
        context="source_fingerprints",
    )


def _validate_payload_provenance(
    request: RunRequest,
    payload: ExecutionPayload,
    *,
    prepared_bundle: RuntimeResourceBundle | None = None,
    prepared_resolved: ResolvedRuntimeInputs | None = None,
) -> None:
    def validate_preflight_rejection(
        exception: ValueError,
        *,
        known_resource_fingerprints: Mapping[str, str] | None = None,
    ) -> None:
        if payload.runtime_status != "rejected" or payload.failure_stage != "request_preflight":
            raise ValueError(
                "a non-executable request may only publish an honest preflight rejection"
            ) from exception
        if payload.versions:
            raise ValueError("preflight rejection must not claim model version lineage")
        if payload.effective_input_fingerprint != request.request_fingerprint:
            raise ValueError("preflight rejection effective input must equal the request")
        resource_fingerprints = (
            {
                resource_id: _sha256(read_runtime_resource_bytes(resource_id))
                for resource_id in _resource_ids_for_request(request)
            }
            if known_resource_fingerprints is None
            else dict(known_resource_fingerprints)
        )
        _assert_mapping_equal(
            payload.source_fingerprints,
            resource_fingerprints,
            context="preflight source_fingerprints",
        )

    try:
        preset = _resolve_executable_preset(request)
    except ValueError as exc:
        validate_preflight_rejection(exc)
        return

    if (prepared_bundle is None) != (prepared_resolved is None):
        raise ValueError("prepared provenance requires both bundle and resolved inputs")
    if prepared_bundle is None or prepared_resolved is None:
        bundle = load_runtime_resource_bundle(preset)
        try:
            resolved = resolve_runtime_inputs(request, bundle=bundle)
        except ValueError as exc:
            validate_preflight_rejection(
                exc,
                known_resource_fingerprints=bundle.resource_fingerprints,
            )
            return
    else:
        bundle = prepared_bundle
        resolved = prepared_resolved
        if resolved.request != request or resolved.preset != preset:
            raise ValueError("prepared provenance inputs differ from the request or preset")
    if payload.failure_stage == "request_preflight":
        raise ValueError("an executable request contradicts request_preflight rejection")
    _validate_fixed_preset_shape(preset, payload, resolved=resolved)
    normal = _normal_run_provenance(request, preset, bundle, resolved=resolved)
    if payload.runtime_status == _EXPECTED_PRESET_STATUS[preset.preset_id]:
        expected = normal
    elif payload.failure_stage == "model_execution":
        failure_sources = _base_source_fingerprints(bundle)
        failure_effective = _effective_fingerprint(bundle, request)
        if resolved.is_custom:
            failure_effective = resolved.execution_input_fingerprint
            failure_sources.update(
                {
                    "runtime_custom_input_preview": resolved.preview_fingerprint,
                    **{
                        f"runtime_effective_object.{name}": digest
                        for name, digest in resolved.effective_object_fingerprints.items()
                    },
                }
            )
        expected = DerivedRunProvenance(
            versions=_common_versions(bundle),
            source_fingerprints={name: failure_sources[name] for name in sorted(failure_sources)},
            effective_input_fingerprint=failure_effective,
        )
    elif preset.preset_id == _M6_PUMP_PRESET_ID and payload.runtime_status in {
        "failed",
        "not_converged",
    }:
        base_sources = _base_source_fingerprints(bundle)
        normal_sources = dict(normal.source_fingerprints)
        baseline_sources = {
            **base_sources,
            "formal_m6_result": normal_sources["formal_m6_result"],
            "m6_portable_baseline": normal_sources["m6_portable_baseline"],
        }
        allowed_sources = (base_sources, baseline_sources, normal_sources)
        if not any(dict(payload.source_fingerprints) == candidate for candidate in allowed_sources):
            raise ValueError(
                "execution source_fingerprints differ from packaged derived provenance"
            )
        prerequisite_versions = {
            **_common_versions(bundle),
            "simulation_stage": "M6",
        }
        if dict(payload.versions) not in (
            prerequisite_versions,
            dict(normal.versions),
        ):
            raise ValueError("execution versions differ from packaged derived provenance")
        if payload.effective_input_fingerprint != normal.effective_input_fingerprint:
            raise ValueError(
                "execution effective_input_fingerprint differs from packaged derived provenance"
            )
        return
    else:
        expected = normal
    _assert_mapping_equal(
        payload.versions,
        expected.versions,
        context="versions",
    )
    _validate_source_fingerprints(
        payload,
        expected.source_fingerprints,
        preset=preset,
    )
    if payload.effective_input_fingerprint != expected.effective_input_fingerprint:
        raise ValueError(
            "execution effective_input_fingerprint differs from packaged derived provenance"
        )


@dataclass(frozen=True)
class ArtifactDescriptor:
    path: str
    size_bytes: int
    sha256: str
    media_type: str
    schema: str

    def __post_init__(self) -> None:
        path = _text(self.path, context="artifact path").replace("\\", "/")
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact path must stay inside its run directory")
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "size_bytes",
            _integer(self.size_bytes, context="artifact size_bytes"),
        )
        object.__setattr__(self, "sha256", _digest(self.sha256, context="artifact sha256"))
        object.__setattr__(self, "media_type", _text(self.media_type, context="media_type"))
        object.__setattr__(self, "schema", _text(self.schema, context="artifact schema"))

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "schema": self.schema,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArtifactDescriptor:
        _strict_keys(
            value,
            {"path", "size_bytes", "sha256", "media_type", "schema"},
            context="artifact descriptor",
        )
        return cls(
            path=_text(value["path"], context="artifact path"),
            size_bytes=_integer(value["size_bytes"], context="artifact size_bytes"),
            sha256=_digest(value["sha256"], context="artifact sha256"),
            media_type=_text(value["media_type"], context="artifact media_type"),
            schema=_text(value["schema"], context="artifact schema"),
        )


@dataclass(frozen=True)
class RunManifest:
    schema_version: str
    manifest_version: str
    artifact_state: str
    run_id: str
    runtime_status: str
    engine_status: str
    domain_status: str
    request_fingerprint: str
    result_fingerprint: str
    effective_input_fingerprint: str
    installed_source_tree_sha256: str
    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    environment: Mapping[str, str]
    started_at_utc: str
    finished_at_utc: str
    wall_time_s: float
    random_seed: int
    randomness_used: bool
    synthetic: bool
    data_origin: str
    claim_scope: str
    artifacts: Mapping[str, ArtifactDescriptor]
    manifest_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ValueError("manifest schema_version differs from runtime")
        if self.manifest_version != RUN_MANIFEST_VERSION:
            raise ValueError("manifest_version differs from runtime")
        if self.artifact_state != "complete":
            raise ValueError("a published manifest must be complete")
        object.__setattr__(self, "run_id", _identifier(self.run_id, context="run_id"))
        if self.runtime_status not in _RUNTIME_STATUSES:
            raise ValueError("manifest runtime_status differs from the runtime contract")
        object.__setattr__(
            self,
            "engine_status",
            _identifier(self.engine_status, context="manifest engine_status"),
        )
        if self.domain_status not in _DOMAIN_STATUSES:
            raise ValueError("manifest domain_status differs from the runtime contract")
        for name in (
            "request_fingerprint",
            "result_fingerprint",
            "effective_input_fingerprint",
            "installed_source_tree_sha256",
            "manifest_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), context=name),
            )
        object.__setattr__(self, "versions", _string_mapping(self.versions, context="versions"))
        object.__setattr__(
            self,
            "source_fingerprints",
            _digest_mapping(self.source_fingerprints, context="source_fingerprints"),
        )
        if self.runtime_status in {"success", "limited"}:
            if not _REQUIRED_NON_FAILURE_VERSION_KEYS.issubset(self.versions):
                raise ValueError("non-failure manifest is missing required version lineage")
            if not _REQUIRED_EFFECTIVE_FINGERPRINT_KEYS.issubset(self.source_fingerprints):
                raise ValueError("non-failure manifest is missing required source lineage")
        object.__setattr__(
            self,
            "environment",
            _string_mapping(self.environment, context="environment"),
        )
        if not _REQUIRED_ENVIRONMENT_KEYS.issubset(self.environment):
            raise ValueError("manifest environment is missing required provenance fields")
        object.__setattr__(
            self,
            "started_at_utc",
            _timestamp(self.started_at_utc, context="started_at_utc"),
        )
        object.__setattr__(
            self,
            "finished_at_utc",
            _timestamp(self.finished_at_utc, context="finished_at_utc"),
        )
        if datetime.fromisoformat(self.finished_at_utc) < datetime.fromisoformat(
            self.started_at_utc
        ):
            raise ValueError("finished_at_utc must not precede started_at_utc")
        object.__setattr__(self, "wall_time_s", _finite(self.wall_time_s, context="wall_time_s"))
        object.__setattr__(self, "random_seed", _integer(self.random_seed, context="random_seed"))
        if not isinstance(self.randomness_used, bool) or not isinstance(self.synthetic, bool):
            raise TypeError("manifest boolean fields must be boolean")
        object.__setattr__(
            self, "data_origin", _identifier(self.data_origin, context="data_origin")
        )
        object.__setattr__(
            self, "claim_scope", _identifier(self.claim_scope, context="claim_scope")
        )
        raw_artifacts = dict(self.artifacts)
        if not raw_artifacts:
            raise ValueError("manifest must contain artifacts")
        artifacts: dict[str, ArtifactDescriptor] = {}
        for artifact_id, descriptor in raw_artifacts.items():
            safe_id = _identifier(artifact_id, context="artifact id")
            if not isinstance(descriptor, ArtifactDescriptor):
                raise TypeError("manifest artifacts must contain ArtifactDescriptor values")
            artifacts[safe_id] = descriptor
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))
        input_resource_ids = {
            artifact_id.removeprefix("input.")
            for artifact_id in artifacts
            if artifact_id.startswith("input.")
        }
        if self.runtime_status in {"success", "limited"} and not input_resource_ids.issubset(
            self.source_fingerprints
        ):
            raise ValueError("non-failure manifest is missing input resource lineage")
        if self.manifest_fingerprint != _fingerprint(self.fingerprint_payload()):
            raise ValueError("manifest fingerprint differs from content")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_version": self.manifest_version,
            "artifact_state": self.artifact_state,
            "run_id": self.run_id,
            "runtime_status": self.runtime_status,
            "engine_status": self.engine_status,
            "domain_status": self.domain_status,
            "request_fingerprint": self.request_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "installed_source_tree_sha256": self.installed_source_tree_sha256,
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "environment": dict(self.environment),
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "wall_time_s": self.wall_time_s,
            "random_seed": self.random_seed,
            "randomness_used": self.randomness_used,
            "synthetic": self.synthetic,
            "data_origin": self.data_origin,
            "claim_scope": self.claim_scope,
            "artifacts": {key: value.as_dict() for key, value in self.artifacts.items()},
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.fingerprint_payload(), "manifest_fingerprint": self.manifest_fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RunManifest:
        expected = {
            "schema_version",
            "manifest_version",
            "artifact_state",
            "run_id",
            "runtime_status",
            "engine_status",
            "domain_status",
            "request_fingerprint",
            "result_fingerprint",
            "effective_input_fingerprint",
            "installed_source_tree_sha256",
            "versions",
            "source_fingerprints",
            "environment",
            "started_at_utc",
            "finished_at_utc",
            "wall_time_s",
            "random_seed",
            "randomness_used",
            "synthetic",
            "data_origin",
            "claim_scope",
            "artifacts",
            "manifest_fingerprint",
        }
        _strict_keys(value, expected, context="run manifest")
        raw_artifacts = value["artifacts"]
        if not isinstance(raw_artifacts, Mapping) or any(
            not isinstance(key, str) for key in raw_artifacts
        ):
            raise TypeError("manifest artifacts must be an object")
        randomness_used = value["randomness_used"]
        synthetic = value["synthetic"]
        if not isinstance(randomness_used, bool) or not isinstance(synthetic, bool):
            raise TypeError("manifest boolean fields must be boolean")
        return cls(
            schema_version=_text(value["schema_version"], context="schema_version"),
            manifest_version=_text(value["manifest_version"], context="manifest_version"),
            artifact_state=_text(value["artifact_state"], context="artifact_state"),
            run_id=_identifier(value["run_id"], context="run_id"),
            runtime_status=_identifier(value["runtime_status"], context="runtime_status"),
            engine_status=_identifier(value["engine_status"], context="engine_status"),
            domain_status=_identifier(value["domain_status"], context="domain_status"),
            request_fingerprint=_digest(
                value["request_fingerprint"], context="request_fingerprint"
            ),
            result_fingerprint=_digest(value["result_fingerprint"], context="result_fingerprint"),
            effective_input_fingerprint=_digest(
                value["effective_input_fingerprint"],
                context="effective_input_fingerprint",
            ),
            installed_source_tree_sha256=_digest(
                value["installed_source_tree_sha256"],
                context="installed_source_tree_sha256",
            ),
            versions=cast(Mapping[str, str], value["versions"]),
            source_fingerprints=cast(
                Mapping[str, str],
                value["source_fingerprints"],
            ),
            environment=cast(Mapping[str, str], value["environment"]),
            started_at_utc=_timestamp(value["started_at_utc"], context="started_at_utc"),
            finished_at_utc=_timestamp(value["finished_at_utc"], context="finished_at_utc"),
            wall_time_s=_finite(value["wall_time_s"], context="wall_time_s"),
            random_seed=_integer(value["random_seed"], context="random_seed"),
            randomness_used=randomness_used,
            synthetic=synthetic,
            data_origin=_identifier(value["data_origin"], context="data_origin"),
            claim_scope=_identifier(value["claim_scope"], context="claim_scope"),
            artifacts={
                key: ArtifactDescriptor.from_mapping(cast(Mapping[str, object], item))
                for key, item in raw_artifacts.items()
            },
            manifest_fingerprint=_digest(
                value["manifest_fingerprint"],
                context="manifest_fingerprint",
            ),
        )


@dataclass(frozen=True)
class RunRecord:
    run_dir: Path
    request: RunRequest
    payload: ExecutionPayload
    manifest: RunManifest

    def iter_samples(self) -> Iterator[Mapping[str, JsonValue]]:
        yield from self.payload.timeseries


def _new_run_id(request: RunRequest) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{request.request_fingerprint[:12]}-{uuid.uuid4().hex[:8]}"


def _descriptor(path: str, data: bytes, *, media_type: str, schema: str) -> ArtifactDescriptor:
    return ArtifactDescriptor(path, len(data), _sha256(data), media_type, schema)


def _safe_path(run_dir: Path, relative: str) -> Path:
    target = (run_dir / relative).resolve()
    if not target.is_relative_to(run_dir.resolve()):
        raise ValueError("artifact target leaves the run directory")
    return target


def _publish(run_dir: Path, relative: str, data: bytes) -> None:
    target = _safe_path(run_dir, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f"{target.name}.stage")
    with staged.open("xb") as handle:
        handle.write(data)
    verified = staged.read_bytes()
    if len(verified) != len(data) or _sha256(verified) != _sha256(data):
        raise OSError(f"staged JSON verification failed for {relative}")
    try:
        json.loads(verified)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"staged JSON parsing failed for {relative}") from exc
    staged.replace(target)


def _publish_jsonl(
    run_dir: Path,
    relative: str,
    rows: Iterable[Mapping[str, object]],
    *,
    schema: str,
) -> ArtifactDescriptor:
    target = _safe_path(run_dir, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f"{target.name}.stage")
    digest = hashlib.sha256()
    size_bytes = 0
    with staged.open("xb") as handle:
        for row in rows:
            data = _json_bytes(row)
            handle.write(data)
            digest.update(data)
            size_bytes += len(data)
    verification = hashlib.sha256()
    verified_size = 0
    with staged.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            verification.update(chunk)
            verified_size += len(chunk)
    if verified_size != size_bytes or verification.hexdigest() != digest.hexdigest():
        raise OSError(f"staged JSONL verification failed for {relative}")
    with staged.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ValueError(f"{relative}:{line_number} must not be blank")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"staged JSONL parsing failed for {relative}:{line_number}"
                ) from exc
            if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
                raise ValueError(f"{relative}:{line_number} must contain a JSON object")
    staged.replace(target)
    return ArtifactDescriptor(
        relative,
        size_bytes,
        digest.hexdigest(),
        "application/x-ndjson",
        schema,
    )


def write_run(
    request: RunRequest,
    payload: ExecutionPayload,
    output_root: Path,
    *,
    input_resources: Mapping[str, bytes],
    started_at_utc: str | None = None,
    finished_at_utc: str | None = None,
    wall_time_s: float = 0.0,
    wall_clock_start_s: float | None = None,
) -> RunRecord:
    """Publish one complete run; manifest.json is always the last file."""

    return _write_run(
        request,
        payload,
        output_root,
        input_resources=input_resources,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        wall_time_s=wall_time_s,
        wall_clock_start_s=wall_clock_start_s,
        prepared_bundle=None,
        prepared_resolved=None,
    )


def _write_prepared_run(
    request: RunRequest,
    payload: ExecutionPayload,
    output_root: Path,
    *,
    input_resources: Mapping[str, bytes],
    prepared_bundle: RuntimeResourceBundle,
    prepared_resolved: ResolvedRuntimeInputs,
    started_at_utc: str | None = None,
    finished_at_utc: str | None = None,
    wall_time_s: float = 0.0,
    wall_clock_start_s: float | None = None,
) -> RunRecord:
    """Publish from one package-private context while retaining full validation."""

    return _write_run(
        request,
        payload,
        output_root,
        input_resources=input_resources,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        wall_time_s=wall_time_s,
        wall_clock_start_s=wall_clock_start_s,
        prepared_bundle=prepared_bundle,
        prepared_resolved=prepared_resolved,
    )


def _write_run(
    request: RunRequest,
    payload: ExecutionPayload,
    output_root: Path,
    *,
    input_resources: Mapping[str, bytes],
    started_at_utc: str | None,
    finished_at_utc: str | None,
    wall_time_s: float,
    wall_clock_start_s: float | None,
    prepared_bundle: RuntimeResourceBundle | None,
    prepared_resolved: ResolvedRuntimeInputs | None,
) -> RunRecord:
    if payload.request_fingerprint != request.request_fingerprint:
        raise ValueError("execution payload belongs to another request")
    if payload.preset_id != request.preset_id or payload.run_type != request.run_type:
        raise ValueError("execution payload identity differs from request")
    resource_ids = _resource_ids_for_request(request)
    if tuple(input_resources) != resource_ids:
        raise ValueError("run publication resources differ from the request closure")
    if prepared_bundle is not None and tuple(prepared_bundle.resource_bytes) != resource_ids:
        raise ValueError("prepared resources differ from the request closure")
    for resource_id in resource_ids:
        data = input_resources[resource_id]
        if not isinstance(data, bytes):
            raise TypeError("input resource content must be bytes")
        expected_data = (
            read_runtime_resource_bytes(resource_id)
            if prepared_bundle is None
            else prepared_bundle.resource_bytes[resource_id]
        )
        if data != expected_data:
            raise ValueError(f"input resource {resource_id!r} differs from the package")
        if payload.source_fingerprints.get(resource_id) != _sha256(data):
            raise ValueError(
                f"execution source fingerprint differs for input resource {resource_id!r}"
            )
    _validate_payload_provenance(
        request,
        payload,
        prepared_bundle=prepared_bundle,
        prepared_resolved=prepared_resolved,
    )
    if started_at_utc is not None and finished_at_utc is not None:
        started_check = _timestamp(started_at_utc, context="started_at_utc")
        finished_check = _timestamp(finished_at_utc, context="finished_at_utc")
        if datetime.fromisoformat(finished_check) < datetime.fromisoformat(started_check):
            raise ValueError("finished_at_utc must not precede started_at_utc")
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_id = request.run_id or _new_run_id(request)
    run_dir = root / run_id
    run_dir.mkdir(parents=False, exist_ok=False)

    started = started_at_utc or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    request_data = _json_bytes(request.as_dict())
    result_fingerprint = payload.result_fingerprint
    result_mapping: dict[str, object] = {
        "schema_version": payload.schema_version,
        "runtime_version": payload.runtime_version,
        "preset_id": payload.preset_id,
        "run_type": payload.run_type,
        "runtime_status": payload.runtime_status,
        "request_fingerprint": payload.request_fingerprint,
        "engine_status": payload.engine_status,
        "raw_result_type": payload.raw_result_type,
        "summary": payload.summary,
        "timeseries": [],
        "events": [],
        "errors": [],
        "versions": payload.versions,
        "source_fingerprints": payload.source_fingerprints,
        "effective_input_fingerprint": payload.effective_input_fingerprint,
        "synthetic": payload.synthetic,
        "data_origin": payload.data_origin,
        "claim_scope": payload.claim_scope,
        "failure_stage": payload.failure_stage,
        "failure_reason": payload.failure_reason,
        "failure_time_s": payload.failure_time_s,
        "last_valid": payload.last_valid,
        "duration_s": payload.duration_s,
        "time_step_s": payload.time_step_s,
        "diagnostics": payload.diagnostics,
        "result_fingerprint": result_fingerprint,
        "externalized": {
            "timeseries_count": len(payload.timeseries),
            "event_count": len(payload.events),
            "error_count": len(payload.errors),
        },
    }
    result_data = _json_bytes(result_mapping)
    errors_data = _json_bytes({"errors": [error.as_dict() for error in payload.errors]})

    files: dict[str, tuple[str, bytes, str, str]] = {
        "request": ("request.json", request_data, "application/json", RUN_REQUEST_VERSION),
        "result": ("result.json", result_data, "application/json", RUNTIME_VERSION),
        "errors": (
            "error.json",
            errors_data,
            "application/json",
            "cdu-mini-errors-v0.1.0",
        ),
    }
    for resource_id, data in input_resources.items():
        safe_id = _identifier(resource_id, context="input resource id")
        files[f"input.{safe_id}"] = (
            f"inputs/{safe_id.replace('.', '__')}.json",
            data,
            "application/json",
            "source-resource",
        )

    descriptors: dict[str, ArtifactDescriptor] = {}
    for artifact_id, (relative, data, media_type, schema) in files.items():
        _publish(run_dir, relative, data)
        descriptors[artifact_id] = _descriptor(
            relative,
            data,
            media_type=media_type,
            schema=schema,
        )
    descriptors["timeseries"] = _publish_jsonl(
        run_dir,
        "timeseries.jsonl",
        cast(tuple[Mapping[str, object], ...], payload.timeseries),
        schema="cdu-mini-timeseries-v0.1.0",
    )
    descriptors["events"] = _publish_jsonl(
        run_dir,
        "events.jsonl",
        (event.as_dict() for event in payload.events),
        schema="cdu-mini-events-v0.1.0",
    )
    finished = finished_at_utc or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    elapsed = (
        _finite(wall_time_s, context="wall_time_s")
        if wall_clock_start_s is None
        else _finite(perf_counter() - wall_clock_start_s, context="wall_time_s")
    )
    domain_status = _domain_status(payload)
    manifest_payload: dict[str, object] = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "manifest_version": RUN_MANIFEST_VERSION,
        "artifact_state": "complete",
        "run_id": run_id,
        "runtime_status": payload.runtime_status,
        "engine_status": payload.engine_status,
        "domain_status": domain_status,
        "request_fingerprint": request.request_fingerprint,
        "result_fingerprint": result_fingerprint,
        "effective_input_fingerprint": payload.effective_input_fingerprint,
        "installed_source_tree_sha256": installed_source_tree_sha256(),
        "versions": dict(payload.versions),
        "source_fingerprints": dict(payload.source_fingerprints),
        "environment": dict(runtime_environment()),
        "started_at_utc": started,
        "finished_at_utc": finished,
        "wall_time_s": elapsed,
        "random_seed": request.random_seed,
        "randomness_used": False,
        "synthetic": payload.synthetic,
        "data_origin": payload.data_origin,
        "claim_scope": payload.claim_scope,
        "artifacts": {key: descriptor.as_dict() for key, descriptor in descriptors.items()},
    }
    manifest = RunManifest(
        schema_version=RUNTIME_SCHEMA_VERSION,
        manifest_version=RUN_MANIFEST_VERSION,
        artifact_state="complete",
        run_id=run_id,
        runtime_status=payload.runtime_status,
        engine_status=payload.engine_status,
        domain_status=domain_status,
        request_fingerprint=request.request_fingerprint,
        result_fingerprint=result_fingerprint,
        effective_input_fingerprint=payload.effective_input_fingerprint,
        installed_source_tree_sha256=cast(
            str,
            manifest_payload["installed_source_tree_sha256"],
        ),
        versions=payload.versions,
        source_fingerprints=payload.source_fingerprints,
        environment=cast(Mapping[str, str], manifest_payload["environment"]),
        started_at_utc=started,
        finished_at_utc=finished,
        wall_time_s=cast(float, manifest_payload["wall_time_s"]),
        random_seed=request.random_seed,
        randomness_used=False,
        synthetic=payload.synthetic,
        data_origin=payload.data_origin,
        claim_scope=payload.claim_scope,
        artifacts=descriptors,
        manifest_fingerprint=_fingerprint(manifest_payload),
    )
    _publish(run_dir, "manifest.json", _json_bytes(manifest.as_dict()))
    return RunRecord(run_dir, request, payload, manifest)


def _load_json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(Mapping[str, object], value)


def _load_jsonl_rows(path: Path) -> Iterator[Mapping[str, object]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ValueError(f"{path.name}:{line_number} must not be blank")
            value = json.loads(line)
            if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
                raise ValueError(f"{path.name}:{line_number} must contain a JSON object")
            yield cast(Mapping[str, object], value)


class _OnePassJsonlSequence(Sequence[object]):
    """Sequence adapter consumed once by ``ExecutionPayload.from_mapping``."""

    def __init__(self, path: Path, expected_count: int) -> None:
        self._path = path
        self._expected_count = expected_count
        self._next_index = 0
        self._handle: TextIO | None = path.open("r", encoding="utf-8")

    def __len__(self) -> int:
        return self._expected_count

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        if isinstance(index, slice):
            raise TypeError("one-pass JSONL sequence does not support slicing")
        if index != self._next_index:
            if index >= self._expected_count:
                self.finish()
                raise IndexError(index)
            raise RuntimeError("one-pass JSONL sequence was accessed out of order")
        if index >= self._expected_count:
            self.finish()
            raise IndexError(index)
        if self._handle is None:
            raise RuntimeError("one-pass JSONL sequence is already closed")
        line = self._handle.readline()
        if line == "":
            raise ValueError(f"{self._path.name} has fewer than {self._expected_count} rows")
        if not line.strip():
            raise ValueError(f"{self._path.name}:{index + 1} must not be blank")
        value = json.loads(line)
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError(f"{self._path.name}:{index + 1} must contain a JSON object")
        self._next_index += 1
        return value

    def finish(self) -> None:
        if self._handle is None:
            return
        try:
            if self._next_index != self._expected_count:
                raise ValueError(f"{self._path.name} has fewer than {self._expected_count} rows")
            extra = self._handle.readline()
            if extra != "":
                raise ValueError(f"{self._path.name} has more than {self._expected_count} rows")
        finally:
            self._handle.close()
            self._handle = None

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def _artifact_layout(
    resource_ids: Iterable[str],
) -> Mapping[str, tuple[str, str, str]]:
    layout: dict[str, tuple[str, str, str]] = {
        "request": ("request.json", "application/json", RUN_REQUEST_VERSION),
        "result": ("result.json", "application/json", RUNTIME_VERSION),
        "timeseries": (
            "timeseries.jsonl",
            "application/x-ndjson",
            "cdu-mini-timeseries-v0.1.0",
        ),
        "events": (
            "events.jsonl",
            "application/x-ndjson",
            "cdu-mini-events-v0.1.0",
        ),
        "errors": ("error.json", "application/json", "cdu-mini-errors-v0.1.0"),
    }
    for resource_id in resource_ids:
        layout[f"input.{resource_id}"] = (
            f"inputs/{resource_id.replace('.', '__')}.json",
            "application/json",
            "source-resource",
        )
    return MappingProxyType(layout)


def _file_size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
    return size_bytes, digest.hexdigest()


def read_run(run_dir: Path) -> RunRecord:
    """Verify every listed byte before reconstructing a stable run record."""

    root = run_dir.resolve()
    if not root.is_dir():
        raise ValueError("run directory does not exist")
    if any(path.is_file() and path.name.endswith((".stage", ".tmp")) for path in root.rglob("*")):
        raise ValueError("run directory contains an unpublished staged file")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("run directory is incomplete because manifest.json is absent")
    manifest = RunManifest.from_mapping(_load_json(manifest_path))
    if root.name != manifest.run_id:
        raise ValueError("run directory name differs from manifest run_id")
    manifest_resource_ids = {
        artifact_id.removeprefix("input.")
        for artifact_id in manifest.artifacts
        if artifact_id.startswith("input.")
    }
    resource_ids = tuple(
        resource_id
        for resource_id in list_runtime_resource_ids()
        if resource_id in manifest_resource_ids
    )
    if set(resource_ids) != manifest_resource_ids:
        raise ValueError("manifest references an unknown input resource")
    layout = _artifact_layout(resource_ids)
    if set(manifest.artifacts) != set(layout):
        raise ValueError("manifest artifact set differs from the runtime contract")
    expected_files = {
        "manifest.json",
        *(descriptor.path for descriptor in manifest.artifacts.values()),
    }
    actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_files != expected_files:
        raise ValueError("run directory file set differs from the manifest contract")
    for artifact_id, descriptor in manifest.artifacts.items():
        expected_path, expected_media_type, expected_schema = layout[artifact_id]
        if (
            descriptor.path != expected_path
            or descriptor.media_type != expected_media_type
            or descriptor.schema != expected_schema
        ):
            raise ValueError(f"manifest artifact {artifact_id} descriptor differs")
        target = _safe_path(root, descriptor.path)
        if not target.is_file():
            raise ValueError(f"manifest artifact {artifact_id} is missing")
        size_bytes, digest = _file_size_and_sha256(target)
        if size_bytes != descriptor.size_bytes or digest != descriptor.sha256:
            raise ValueError(f"manifest artifact {artifact_id} hash/size mismatch")
        if artifact_id.startswith("input."):
            resource_id = artifact_id.removeprefix("input.")
            package_data = read_runtime_resource_bytes(resource_id)
            if descriptor.sha256 != _sha256(package_data) or target.read_bytes() != package_data:
                raise ValueError(
                    f"input resource {resource_id!r} differs from the installed package"
                )

    request_document = _load_json(root / manifest.artifacts["request"].path)
    if "request_fingerprint" not in request_document:
        raise ValueError("published request fingerprint is missing")
    request = RunRequest.from_mapping(request_document)
    if resource_ids != _resource_ids_for_request(request):
        raise ValueError("manifest input resources differ from the request closure")
    result = dict(_load_json(root / manifest.artifacts["result"].path))
    if "result_fingerprint" not in result:
        raise ValueError("published result fingerprint is missing")
    for externalized_field in ("timeseries", "events", "errors"):
        if result.get(externalized_field) != []:
            raise ValueError(f"result {externalized_field} must be empty when externalized")
    externalized = result.pop("externalized", None)
    if not isinstance(externalized, dict):
        raise TypeError("result externalized counts are missing")
    _strict_keys(
        externalized,
        {"timeseries_count", "event_count", "error_count"},
        context="result externalized counts",
    )
    raw_timeseries_count = externalized.get("timeseries_count")
    raw_event_count = externalized.get("event_count")
    raw_error_count = externalized.get("error_count")
    timeseries_count = _integer(raw_timeseries_count, context="timeseries_count")
    event_count = _integer(raw_event_count, context="event_count")
    error_count = _integer(raw_error_count, context="error_count")
    timeseries = _OnePassJsonlSequence(
        root / manifest.artifacts["timeseries"].path,
        timeseries_count,
    )
    events = tuple(_load_jsonl_rows(root / manifest.artifacts["events"].path))
    error_payload = _load_json(root / manifest.artifacts["errors"].path)
    _strict_keys(error_payload, {"errors"}, context="error artifact")
    raw_errors = error_payload.get("errors")
    if not isinstance(raw_errors, list):
        raise TypeError("error.json must contain an errors list")
    if (event_count, error_count) != (len(events), len(raw_errors)):
        raise ValueError("externalized artifact counts differ from result.json")
    result["timeseries"] = timeseries
    result["events"] = list(events)
    result["errors"] = raw_errors
    try:
        payload = ExecutionPayload.from_mapping(result)
        timeseries.finish()
    finally:
        timeseries.close()
    _validate_payload_provenance(request, payload)
    if request.request_fingerprint != manifest.request_fingerprint:
        raise ValueError("request fingerprint differs from manifest")
    if request.random_seed != manifest.random_seed:
        raise ValueError("request random_seed differs from manifest")
    if request.run_id is not None and request.run_id != manifest.run_id:
        raise ValueError("request run_id differs from manifest")
    if payload.request_fingerprint != request.request_fingerprint:
        raise ValueError("execution request fingerprint differs from request")
    if payload.preset_id != request.preset_id or payload.run_type != request.run_type:
        raise ValueError("execution identity differs from request")
    if payload.result_fingerprint != manifest.result_fingerprint:
        raise ValueError("result fingerprint differs from manifest")
    if payload.effective_input_fingerprint != manifest.effective_input_fingerprint:
        raise ValueError("effective input fingerprint differs from manifest")
    if payload.runtime_status != manifest.runtime_status:
        raise ValueError("execution runtime_status differs from manifest")
    if payload.engine_status != manifest.engine_status:
        raise ValueError("execution engine_status differs from manifest")
    if _domain_status(payload) != manifest.domain_status:
        raise ValueError("execution domain_status differs from manifest")
    if dict(payload.versions) != dict(manifest.versions):
        raise ValueError("execution versions differ from manifest")
    if dict(payload.source_fingerprints) != dict(manifest.source_fingerprints):
        raise ValueError("execution source fingerprints differ from manifest")
    if (
        payload.synthetic != manifest.synthetic
        or payload.data_origin != manifest.data_origin
        or payload.claim_scope != manifest.claim_scope
    ):
        raise ValueError("execution source contract differs from manifest")
    for resource_id in resource_ids:
        descriptor = manifest.artifacts[f"input.{resource_id}"]
        if payload.source_fingerprints.get(resource_id) != descriptor.sha256:
            raise ValueError(
                f"execution source fingerprint differs for input resource {resource_id!r}"
            )
    return RunRecord(root, request, payload, manifest)


__all__ = [
    "RUN_MANIFEST_VERSION",
    "ArtifactDescriptor",
    "DerivedRunProvenance",
    "RunManifest",
    "RunRecord",
    "derive_run_provenance",
    "read_run",
    "write_run",
]
