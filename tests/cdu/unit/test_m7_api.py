from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.runtime.api import run, runtime_input_resources
from petroleum_rto.cdu.runtime.artifacts import read_run
from petroleum_rto.cdu.runtime.presets import load_preset
from petroleum_rto.cdu.runtime.resources import list_runtime_resource_ids


def test_stable_api_executes_publishes_and_reloads_steady_run(tmp_path: Path) -> None:
    request = replace(load_preset("steady-baseline"), run_id="steady-api-smoke")

    record = run(request, output_root=tmp_path)
    reloaded = read_run(record.run_dir)

    assert reloaded.request == request
    assert reloaded.payload.runtime_status == "success"
    assert reloaded.payload.raw_result_type == "RecycleSolveResult"
    assert reloaded.payload.result_fingerprint == record.payload.result_fingerprint
    assert reloaded.manifest.installed_source_tree_sha256
    assert reloaded.manifest.engine_status == reloaded.payload.engine_status
    assert reloaded.manifest.domain_status == "not_applicable"
    assert reloaded.manifest.source_fingerprints == reloaded.payload.source_fingerprints
    assert reloaded.manifest.versions["m5_overlay_version"].startswith("m5-")
    assert reloaded.manifest.environment["git_commit"] == "unavailable"
    assert reloaded.manifest.environment["git_dirty"] == "unavailable"
    assert reloaded.manifest.wall_time_s > 0.0
    assert {
        artifact_id.removeprefix("input.")
        for artifact_id in reloaded.manifest.artifacts
        if artifact_id.startswith("input.")
    } == set(list_runtime_resource_ids())


def test_stable_api_keeps_semantic_fingerprints_outside_run_identity(
    tmp_path: Path,
) -> None:
    first_request = replace(load_preset("steady-baseline"), run_id="steady-repeat-a")
    second_request = replace(
        first_request,
        run_id="steady-repeat-b",
        requested_at_utc="2026-08-18T00:00:00Z",
    )

    first = run(first_request, output_root=tmp_path)
    second = run(second_request, output_root=tmp_path)

    assert first_request.request_fingerprint == second_request.request_fingerprint
    assert first.payload.result_fingerprint == second.payload.result_fingerprint
    assert first.run_dir != second.run_dir


def test_runtime_input_resources_are_complete_and_path_type_is_strict(
    tmp_path: Path,
) -> None:
    resources = runtime_input_resources()
    assert tuple(resources) == list_runtime_resource_ids()
    assert all(payload for payload in resources.values())

    with pytest.raises(TypeError, match="pathlib.Path"):
        run(load_preset("steady-baseline"), output_root=str(tmp_path))  # type: ignore[arg-type]
