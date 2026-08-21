"""Stable public execution API for one self-contained CDU Mini Loop run."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from .artifacts import RunRecord, _write_prepared_run, write_run
from .contracts import ExecutionPayload, RunRequest
from .custom_inputs import ResolvedRuntimeInputs, resolve_runtime_inputs
from .executor import _execute_prepared, _prepare_execution
from .presets import get_preset
from .resources import (
    list_runtime_resource_ids,
    read_runtime_resource_bytes,
    runtime_resource_ids_for_preset,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def runtime_input_resources(request: RunRequest | None = None) -> dict[str, bytes]:
    """Return all package inputs, or the exact closure for one request."""

    resource_ids = list_runtime_resource_ids()
    if request is not None:
        try:
            preset = get_preset(request.preset_id)
        except (KeyError, TypeError):
            pass
        else:
            if request.run_type == preset.run_type:
                resource_ids = runtime_resource_ids_for_preset(preset)
    return {resource_id: read_runtime_resource_bytes(resource_id) for resource_id in resource_ids}


def preview(request: RunRequest) -> ResolvedRuntimeInputs:
    """Resolve the exact effective inputs without running a model solver."""

    if not isinstance(request, RunRequest):
        raise TypeError("preview requires a RunRequest")
    return resolve_runtime_inputs(request)


def run(
    request: RunRequest,
    *,
    output_root: Path,
    expected_preview_fingerprint: str | None = None,
) -> RunRecord:
    """Execute and publish one request through the sole stable runtime path."""

    if not isinstance(request, RunRequest):
        raise TypeError("run requires a RunRequest")
    if not isinstance(output_root, Path):
        raise TypeError("output_root must be a pathlib.Path")
    custom_requested = bool(
        request.parameters
        or request.overrides
        or request.initial_state
        or request.scenario is not None
    )
    confirmation_required = custom_requested or expected_preview_fingerprint is not None
    started_at = _utc_now()
    started_clock = perf_counter()
    prepared = _prepare_execution(
        request,
        normalize_errors=not confirmation_required,
    )
    if isinstance(prepared, ExecutionPayload):
        payload = prepared
    else:
        resolved = prepared.resolved
        if confirmation_required:
            if custom_requested and expected_preview_fingerprint is None:
                raise ValueError(
                    "custom requests require a confirmed preview fingerprint before execution"
                )
            if expected_preview_fingerprint != resolved.preview_fingerprint:
                raise ValueError(
                    "confirmed preview fingerprint differs from the current resolved inputs"
                )
        payload = _execute_prepared(prepared)
    input_resources = (
        runtime_input_resources(request)
        if isinstance(prepared, ExecutionPayload)
        else dict(prepared.bundle.resource_bytes)
    )
    resource_fingerprints = {
        resource_id: hashlib.sha256(data).hexdigest()
        for resource_id, data in input_resources.items()
    }
    for resource_id, digest in resource_fingerprints.items():
        supplied = payload.source_fingerprints.get(resource_id)
        if supplied is not None and supplied != digest:
            raise ValueError(f"execution source fingerprint differs for {resource_id!r}")
    if any(resource_id not in payload.source_fingerprints for resource_id in input_resources):
        payload = replace(
            payload,
            source_fingerprints={
                **resource_fingerprints,
                **dict(payload.source_fingerprints),
            },
        )
    if isinstance(prepared, ExecutionPayload):
        return write_run(
            request,
            payload,
            output_root,
            input_resources=input_resources,
            started_at_utc=started_at,
            wall_clock_start_s=started_clock,
        )
    return _write_prepared_run(
        request,
        payload,
        output_root,
        input_resources=input_resources,
        prepared_bundle=prepared.bundle,
        prepared_resolved=prepared.resolved,
        started_at_utc=started_at,
        wall_clock_start_s=started_clock,
    )


__all__ = ["preview", "run", "runtime_input_resources"]
