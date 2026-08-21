from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.runtime import artifacts, executor
from petroleum_rto.cdu.runtime import resources as resources_module
from petroleum_rto.cdu.runtime.api import run, runtime_input_resources
from petroleum_rto.cdu.runtime.artifacts import read_run
from petroleum_rto.cdu.runtime.presets import RuntimePreset, get_preset, load_preset
from petroleum_rto.cdu.runtime.resources import (
    RuntimeResourceBundle,
    list_runtime_resource_ids,
    runtime_resource_ids_for_preset,
)


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
    } == set(runtime_resource_ids_for_preset(get_preset("steady-baseline")))


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


def test_run_reuses_prepared_resources_until_independent_strict_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_loader = executor.load_runtime_resource_bundle
    strict_loader = artifacts.load_runtime_resource_bundle
    calls: list[str] = []

    def load_for_prepare(preset: RuntimePreset | None = None) -> RuntimeResourceBundle:
        calls.append("prepare")
        return prepare_loader(preset)

    def load_for_strict_read(preset: RuntimePreset | None = None) -> RuntimeResourceBundle:
        calls.append("strict_read")
        return strict_loader(preset)

    monkeypatch.setattr(executor, "load_runtime_resource_bundle", load_for_prepare)
    monkeypatch.setattr(artifacts, "load_runtime_resource_bundle", load_for_strict_read)
    request = replace(load_preset("steady-baseline"), run_id="steady-prepared-once")

    record = run(request, output_root=tmp_path)

    assert calls == ["prepare"]
    reloaded = read_run(record.run_dir)
    assert calls == ["prepare", "strict_read"]
    assert reloaded.payload.result_fingerprint == record.payload.result_fingerprint


def test_run_reads_each_closure_resource_once_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = resources_module.read_runtime_resource_bytes
    calls: list[str] = []

    def counted(resource_id: str) -> bytes:
        calls.append(resource_id)
        return original(resource_id)

    monkeypatch.setattr(resources_module, "read_runtime_resource_bytes", counted)
    request = replace(load_preset("steady-baseline"), run_id="steady-one-read")

    run(request, output_root=tmp_path)

    assert tuple(calls) == runtime_resource_ids_for_preset(get_preset(request.preset_id))


def test_runtime_input_resources_are_complete_and_path_type_is_strict(
    tmp_path: Path,
) -> None:
    resources = runtime_input_resources()
    assert tuple(resources) == list_runtime_resource_ids()
    assert all(payload for payload in resources.values())
    steady = load_preset("steady-baseline")
    assert tuple(runtime_input_resources(steady)) == runtime_resource_ids_for_preset(
        get_preset(steady.preset_id)
    )

    with pytest.raises(TypeError, match="pathlib.Path"):
        run(steady, output_root=str(tmp_path))  # type: ignore[arg-type]
