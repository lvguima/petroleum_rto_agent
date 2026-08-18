"""Stable public execution API for one self-contained CDU Mini Loop run."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from .artifacts import RunRecord, write_run
from .contracts import RunRequest
from .custom_inputs import ResolvedRuntimeInputs, resolve_runtime_inputs
from .executor import execute
from .resources import list_runtime_resource_ids, read_runtime_resource_bytes


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def runtime_input_resources() -> dict[str, bytes]:
    """Return verified package inputs in stable registry order."""

    return {
        resource_id: read_runtime_resource_bytes(resource_id)
        for resource_id in list_runtime_resource_ids()
    }


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
    if custom_requested or expected_preview_fingerprint is not None:
        resolved = preview(request)
        if custom_requested and expected_preview_fingerprint is None:
            raise ValueError(
                "custom requests require a confirmed preview fingerprint before execution"
            )
        if expected_preview_fingerprint != resolved.preview_fingerprint:
            raise ValueError(
                "confirmed preview fingerprint differs from the current resolved inputs"
            )
    started_at = _utc_now()
    started_clock = perf_counter()
    payload = execute(request)
    input_resources = runtime_input_resources()
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
    return write_run(
        request,
        payload,
        output_root,
        input_resources=input_resources,
        started_at_utc=started_at,
        wall_clock_start_s=started_clock,
    )


__all__ = ["preview", "run", "runtime_input_resources"]
