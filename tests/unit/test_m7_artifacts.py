from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.runtime.api import run
from petroleum_rto.cdu.runtime.artifacts import (
    derive_run_provenance,
    read_run,
    write_run,
)
from petroleum_rto.cdu.runtime.contracts import (
    RUN_REQUEST_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
    ErrorRecord,
    EventRecord,
    ExecutionPayload,
    JsonValue,
    RunRequest,
)
from petroleum_rto.cdu.runtime.presets import list_presets, load_preset
from petroleum_rto.cdu.runtime.provenance import installed_source_tree_sha256
from petroleum_rto.cdu.runtime.resources import (
    list_runtime_resource_ids,
    read_runtime_resource_bytes,
)


def _inputs() -> dict[str, bytes]:
    return {
        resource_id: read_runtime_resource_bytes(resource_id)
        for resource_id in list_runtime_resource_ids()
    }


def _request(*, run_id: str | None = None) -> RunRequest:
    return RunRequest(
        schema_version=RUNTIME_SCHEMA_VERSION,
        request_version=RUN_REQUEST_VERSION,
        preset_id="steady-baseline",
        run_type="steady_recycle",
        random_seed=0,
        parameters={},
        overrides={},
        metadata={"purpose": "test"},
        run_id=run_id,
    )


def _success(request: RunRequest) -> ExecutionPayload:
    provenance = derive_run_provenance(request)
    source_fingerprints = dict(provenance.source_fingerprints)
    summary: dict[str, JsonValue] = {"mass_residual_kg_s": 0.0}
    origins = {
        "steady_recycle": "M2_steady_model_prediction",
        "open_loop_dynamic": "M3_open_loop_simulation",
        "closed_loop_dynamic": "M4_closed_loop_simulation",
        "validation_scenario": "M6_synthetic_validation",
    }
    claim_scope = (
        "engineering_validation_only"
        if request.run_type == "validation_scenario"
        else "engineering_simulation_only"
    )
    duration_by_preset = {
        "open-loop-feed-step": 7200.0,
        "closed-loop-feed-step": 7200.0,
        "m6-abnormal-pump-trip": 600.0,
    }
    duration_s = duration_by_preset.get(request.preset_id)
    time_step_s = None if duration_s is None else 1.0
    timeseries = (
        ()
        if duration_s is None
        else tuple({"time_s": float(index)} for index in range(int(duration_s) + 1))
    )
    if request.run_type in {"open_loop_dynamic", "closed_loop_dynamic"}:
        engine_source = "e" * 64
        source_fingerprints["engine_source"] = engine_source
        summary["source_fingerprint"] = engine_source
    if request.run_type == "closed_loop_dynamic":
        summary["control_fingerprint"] = source_fingerprints["control_input"]
    if request.run_type == "validation_scenario":
        summary["formal_m6_result_fingerprint"] = source_fingerprints["formal_m6_result"]
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status="success",
        request_fingerprint=request.request_fingerprint,
        engine_status="success",
        raw_result_type="RecycleSolveResult",
        summary=summary,
        timeseries=timeseries,
        events=(),
        errors=(),
        versions=provenance.versions,
        source_fingerprints=source_fingerprints,
        effective_input_fingerprint=provenance.effective_input_fingerprint,
        synthetic=True,
        data_origin=origins[request.run_type],
        claim_scope=claim_scope,
        failure_stage=None,
        failure_reason=None,
        failure_time_s=None,
        last_valid=None,
        duration_s=duration_s,
        time_step_s=time_step_s,
        diagnostics={},
    )


def _rejected(request: RunRequest) -> ExecutionPayload:
    provenance = derive_run_provenance(request)
    error = ErrorRecord(
        sequence=0,
        error_type="UnsupportedInput",
        stage="domain_preflight",
        message="unsupported request",
        time_s=0.0,
        last_valid=None,
        retryable=False,
        details={},
    )
    origins = {
        "steady_recycle": "M2_steady_model_prediction",
        "open_loop_dynamic": "M3_open_loop_simulation",
        "closed_loop_dynamic": "M4_closed_loop_simulation",
        "validation_scenario": "M6_synthetic_validation",
    }
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status="rejected",
        request_fingerprint=request.request_fingerprint,
        engine_status="not_called",
        raw_result_type="StructuralRejection",
        summary={
            "formal_m6_result_fingerprint": provenance.source_fingerprints.get(
                "formal_m6_result",
                "0" * 64,
            )
        },
        timeseries=(),
        events=(
            EventRecord(
                sequence=0,
                time_s=0.0,
                event_type="structural_rejection",
                source="M7_runtime",
                stage="domain_preflight",
                message="unsupported request",
                details={"error_type": "UnsupportedInput"},
            ),
        ),
        errors=(error,),
        versions=provenance.versions,
        source_fingerprints=provenance.source_fingerprints,
        effective_input_fingerprint=provenance.effective_input_fingerprint,
        synthetic=True,
        data_origin=origins[request.run_type],
        claim_scope=(
            "engineering_validation_only"
            if request.run_type == "validation_scenario"
            else "engineering_simulation_only"
        ),
        failure_stage="domain_preflight",
        failure_reason="unsupported request",
        failure_time_s=0.0,
        last_valid=None,
        duration_s=None,
        time_step_s=None,
        diagnostics={
            "domain_status": (
                "rejected" if request.run_type == "validation_scenario" else "not_applicable"
            )
        },
    )


def _forged_request_and_payload(
    attack: str,
    *,
    run_id: str,
) -> tuple[RunRequest, ExecutionPayload]:
    request = _request(run_id=run_id)
    payload = _success(request)
    if attack == "effective_input":
        return request, replace(payload, effective_input_fingerprint="0" * 64)
    if attack == "model_version":
        return request, replace(
            payload,
            versions={**dict(payload.versions), "model_version": "forged-model-v9"},
        )
    if attack == "m5_pipeline":
        return request, replace(
            payload,
            source_fingerprints={
                **dict(payload.source_fingerprints),
                "m5_pipeline_result": "0" * 64,
            },
        )
    if attack == "effective_object":
        return request, replace(
            payload,
            source_fingerprints={
                **dict(payload.source_fingerprints),
                "effective_object.calibrated_model_object": "0" * 64,
            },
        )
    if attack == "unknown_preset":
        forged_request = replace(request, preset_id="unknown-fixed-preset")
        return forged_request, replace(
            payload,
            preset_id=forged_request.preset_id,
            request_fingerprint=forged_request.request_fingerprint,
        )
    if attack == "forbidden_parameters":
        forged_request = replace(request, parameters={"forbidden": 1.0})
        return forged_request, replace(
            payload,
            request_fingerprint=forged_request.request_fingerprint,
        )
    raise AssertionError(f"unknown test attack: {attack}")


def _canonical_json_data(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def test_manifest_last_run_round_trip_and_source_tree_hash(tmp_path: Path) -> None:
    request = _request(run_id="fixed-run")
    payload = _success(request)

    written = write_run(
        request,
        payload,
        tmp_path,
        input_resources=_inputs(),
        started_at_utc="2026-08-18T00:00:00Z",
        finished_at_utc="2026-08-18T00:00:01Z",
        wall_time_s=1.0,
    )
    loaded = read_run(written.run_dir)

    assert loaded.request.as_dict() == request.as_dict()
    assert loaded.payload.as_dict() == payload.as_dict()
    assert loaded.manifest.as_dict() == written.manifest.as_dict()
    assert loaded.manifest.installed_source_tree_sha256 == installed_source_tree_sha256()
    assert (written.run_dir / "manifest.json").is_file()
    assert not tuple(written.run_dir.rglob("*.stage"))
    with pytest.raises(ValueError, match="required provenance"):
        replace(written.manifest, environment={})


def test_derived_provenance_covers_all_fixed_presets_without_model_solve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_solve(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("provenance derivation must not solve the model")

    monkeypatch.setattr(
        "petroleum_rto.cdu.flowsheet.recycle.solve_recycle",
        reject_solve,
    )
    evidence = {
        preset.preset_id: derive_run_provenance(load_preset(preset.preset_id))
        for preset in list_presets()
    }

    assert set(evidence) == {
        "steady-baseline",
        "open-loop-feed-step",
        "closed-loop-feed-step",
        "m6-abnormal-pump-trip",
        "m6-structural-rejection",
    }
    assert all(item.effective_input_fingerprint != "0" * 64 for item in evidence.values())
    assert evidence["steady-baseline"].versions["simulation_stage"] == "M2"
    assert evidence["open-loop-feed-step"].versions["simulation_stage"] == "M3"
    assert evidence["closed-loop-feed-step"].versions["simulation_stage"] == "M4"
    assert (
        evidence["open-loop-feed-step"].versions["config_version"]
        == (evidence["open-loop-feed-step"].versions["model_config_version"])
    )
    assert (
        evidence["closed-loop-feed-step"].versions["config_version"]
        == (evidence["closed-loop-feed-step"].versions["model_config_version"])
    )
    assert evidence["m6-abnormal-pump-trip"].versions["simulation_stage"] == "M6"
    assert "m6_portable_candidate" in evidence["m6-abnormal-pump-trip"].source_fingerprints


def test_writer_rejects_fixed_preset_status_grid_and_sample_forgery(
    tmp_path: Path,
) -> None:
    steady = _request(run_id="forged-steady-rejection")
    with pytest.raises(ValueError, match="status differs from fixed preset"):
        write_run(steady, _rejected(steady), tmp_path, input_resources=_inputs())

    structural = replace(
        load_preset("m6-structural-rejection"),
        run_id="forged-structural-success",
    )
    with pytest.raises(ValueError, match="status differs from fixed preset"):
        write_run(
            structural,
            _success(structural),
            tmp_path,
            input_resources=_inputs(),
        )

    dynamic = replace(
        load_preset("open-loop-feed-step"),
        run_id="forged-dynamic-grid",
    )
    valid_shape = _success(dynamic)
    with pytest.raises(ValueError, match="time grid differs from fixed preset"):
        write_run(
            dynamic,
            replace(
                valid_shape,
                timeseries=({"time_s": 0.0}, {"time_s": 1.0}),
                duration_s=1.0,
            ),
            tmp_path,
            input_resources=_inputs(),
        )
    with pytest.raises(ValueError, match="sample count differs from fixed preset"):
        write_run(
            replace(dynamic, run_id="forged-dynamic-samples"),
            replace(
                _success(replace(dynamic, run_id="forged-dynamic-samples")),
                timeseries=(),
            ),
            tmp_path,
            input_resources=_inputs(),
        )


@pytest.mark.parametrize(
    "attack",
    [
        "effective_input",
        "model_version",
        "m5_pipeline",
        "effective_object",
        "unknown_preset",
        "forbidden_parameters",
    ],
)
def test_writer_rejects_forged_or_nonexecutable_success_evidence(
    tmp_path: Path,
    attack: str,
) -> None:
    run_id = f"write-{attack.replace('_', '-')}-run"
    request, payload = _forged_request_and_payload(attack, run_id=run_id)

    with pytest.raises(ValueError, match="derived provenance|non-executable"):
        write_run(request, payload, tmp_path, input_resources=_inputs())
    assert not (tmp_path / run_id).exists()


def test_honest_preflight_rejection_remains_publishable(tmp_path: Path) -> None:
    request = replace(
        _request(run_id="honest-preflight-rejection"),
        run_type="open_loop_dynamic",
    )

    written = run(request, output_root=tmp_path)
    loaded = read_run(written.run_dir)

    assert loaded.payload.runtime_status == "rejected"
    assert loaded.payload.failure_stage == "request_preflight"
    assert loaded.payload.versions == {}
    assert loaded.payload.effective_input_fingerprint == request.request_fingerprint


def test_writer_and_reader_reject_reverse_manifest_timestamps(tmp_path: Path) -> None:
    request = _request(run_id="reverse-write-time")
    with pytest.raises(ValueError, match="must not precede"):
        write_run(
            request,
            _success(request),
            tmp_path,
            input_resources=_inputs(),
            started_at_utc="2026-08-18T00:00:01Z",
            finished_at_utc="2026-08-18T00:00:00Z",
        )
    assert not (tmp_path / "reverse-write-time").exists()

    readable_request = _request(run_id="reverse-read-time")
    written = write_run(
        readable_request,
        _success(readable_request),
        tmp_path,
        input_resources=_inputs(),
        started_at_utc="2026-08-18T00:00:00Z",
        finished_at_utc="2026-08-18T00:00:01Z",
    )
    _resign_manifest(
        written.run_dir,
        lambda manifest: manifest.__setitem__(
            "finished_at_utc",
            "2026-08-17T23:59:59Z",
        ),
    )
    with pytest.raises(ValueError, match="must not precede"):
        read_run(written.run_dir)


def test_reader_rejects_incomplete_or_tampered_run(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        read_run(incomplete)

    request = _request(run_id="tampered-run")
    written = write_run(
        request,
        _success(request),
        tmp_path,
        input_resources=_inputs(),
    )
    result_path = written.run_dir / "result.json"
    result_path.write_bytes(result_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash/size mismatch"):
        read_run(written.run_dir)


def test_run_ids_never_overwrite_and_rejected_evidence_round_trips(
    tmp_path: Path,
) -> None:
    request = replace(load_preset("m6-structural-rejection"), run_id="same-run")
    payload = _rejected(request)
    first = write_run(
        request,
        payload,
        tmp_path,
        input_resources=_inputs(),
    )
    assert read_run(first.run_dir).payload.runtime_status == "rejected"
    with pytest.raises(FileExistsError):
        write_run(
            request,
            payload,
            tmp_path,
            input_resources=_inputs(),
        )


def test_nested_immutable_dynamic_samples_are_serialized_as_standard_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steady_request = _request()
    request = replace(
        steady_request,
        preset_id="open-loop-feed-step",
        run_type="open_loop_dynamic",
        run_id="nested-dynamic",
    )
    base_payload = _success(request)
    payload = replace(
        base_payload,
        timeseries=(
            {"time_s": 0.0, "plant": {"commands": {"feed": 1.0}}},
            *base_payload.timeseries[1:],
        ),
    )

    def reject_full_payload_expansion(_payload: ExecutionPayload) -> dict[str, object]:
        raise AssertionError("writer must not call ExecutionPayload.as_dict")

    with monkeypatch.context() as patcher:
        patcher.setattr(ExecutionPayload, "as_dict", reject_full_payload_expansion)
        written = write_run(
            request,
            payload,
            tmp_path,
            input_resources=_inputs(),
        )

    assert read_run(written.run_dir).payload.as_dict() == payload.as_dict()


def _resign_manifest(
    run_dir: Path,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    mutate(manifest)
    fingerprint_payload = dict(manifest)
    fingerprint_payload.pop("manifest_fingerprint")
    manifest["manifest_fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _replace_published_request_and_payload(
    run_dir: Path,
    request: RunRequest,
    payload: ExecutionPayload,
) -> None:
    request_data = _canonical_json_data(request.as_dict())
    request_path = run_dir / "request.json"
    request_path.write_bytes(request_data)

    result_path = run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    for key, value in payload.as_dict().items():
        if key not in {"timeseries", "events", "errors"}:
            result[key] = value
    result_data = _canonical_json_data(result)
    result_path.write_bytes(result_data)

    def mutate(manifest: dict[str, object]) -> None:
        manifest.update(
            {
                "request_fingerprint": request.request_fingerprint,
                "result_fingerprint": payload.result_fingerprint,
                "effective_input_fingerprint": payload.effective_input_fingerprint,
                "versions": dict(payload.versions),
                "source_fingerprints": dict(payload.source_fingerprints),
            }
        )
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        request_descriptor = artifacts["request"]
        result_descriptor = artifacts["result"]
        assert isinstance(request_descriptor, dict)
        assert isinstance(result_descriptor, dict)
        request_descriptor.update(
            {
                "size_bytes": len(request_data),
                "sha256": hashlib.sha256(request_data).hexdigest(),
            }
        )
        result_descriptor.update(
            {
                "size_bytes": len(result_data),
                "sha256": hashlib.sha256(result_data).hexdigest(),
            }
        )

    _resign_manifest(run_dir, mutate)


def test_reader_rejects_resigned_manifest_input_and_status_contradictions(
    tmp_path: Path,
) -> None:
    request = _request(run_id="resigned-run")
    written = write_run(request, _success(request), tmp_path, input_resources=_inputs())

    input_id = list_runtime_resource_ids()[0]
    input_path = written.run_dir / f"inputs/{input_id.replace('.', '__')}.json"
    input_path.write_bytes(b"X")

    def mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        descriptor = artifacts[f"input.{input_id}"]
        assert isinstance(descriptor, dict)
        descriptor["size_bytes"] = 1
        descriptor["sha256"] = hashlib.sha256(b"X").hexdigest()
        manifest["runtime_status"] = "failed"

    _resign_manifest(written.run_dir, mutate)
    with pytest.raises(ValueError, match="installed package|runtime_status"):
        read_run(written.run_dir)

    status_request = _request(run_id="resigned-status-run")
    status_run = write_run(
        status_request,
        _success(status_request),
        tmp_path,
        input_resources=_inputs(),
    )
    _resign_manifest(
        status_run.run_dir,
        lambda manifest: manifest.__setitem__("runtime_status", "failed"),
    )
    with pytest.raises(ValueError, match="runtime_status"):
        read_run(status_run.run_dir)


def test_reader_rejects_missing_input_and_staged_or_orphan_files(tmp_path: Path) -> None:
    request = _request(run_id="layout-run")
    written = write_run(request, _success(request), tmp_path, input_resources=_inputs())
    resource_id = list_runtime_resource_ids()[0]
    (written.run_dir / f"inputs/{resource_id.replace('.', '__')}.json").unlink()
    with pytest.raises(ValueError, match="file set|missing"):
        read_run(written.run_dir)

    second = write_run(
        replace(request, run_id="staged-run"),
        replace(
            _success(request),
            request_fingerprint=replace(request, run_id="staged-run").request_fingerprint,
        ),
        tmp_path,
        input_resources=_inputs(),
    )
    (second.run_dir / "orphan.stage").write_text("partial", encoding="utf-8")
    with pytest.raises(ValueError, match="staged"):
        read_run(second.run_dir)


def test_reader_rejects_resigned_result_source_fingerprint_forgery(
    tmp_path: Path,
) -> None:
    request = _request(run_id="source-forgery-run")
    payload = _success(request)
    written = write_run(request, payload, tmp_path, input_resources=_inputs())
    resource_id = list_runtime_resource_ids()[0]
    forged = replace(
        payload,
        source_fingerprints={
            **dict(payload.source_fingerprints),
            resource_id: "0" * 64,
        },
    )
    result_path = written.run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    result["source_fingerprints"] = dict(forged.source_fingerprints)
    result["result_fingerprint"] = forged.result_fingerprint
    result_data = (
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    result_path.write_bytes(result_data)

    def mutate(manifest: dict[str, object]) -> None:
        manifest["result_fingerprint"] = forged.result_fingerprint
        manifest["source_fingerprints"] = dict(forged.source_fingerprints)
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        descriptor = artifacts["result"]
        assert isinstance(descriptor, dict)
        descriptor["size_bytes"] = len(result_data)
        descriptor["sha256"] = hashlib.sha256(result_data).hexdigest()

    _resign_manifest(written.run_dir, mutate)
    with pytest.raises(ValueError, match="source_fingerprints|input resource"):
        read_run(written.run_dir)


def test_reader_streams_timeseries_without_path_read_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steady = _request()
    request = replace(
        steady,
        preset_id="open-loop-feed-step",
        run_type="open_loop_dynamic",
        run_id="stream-reader-run",
    )
    payload = _success(request)
    written = write_run(request, payload, tmp_path, input_resources=_inputs())
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "timeseries.jsonl":
            raise AssertionError("reader must not materialize the JSONL text")
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    loaded = read_run(written.run_dir)
    samples = tuple(loaded.iter_samples())
    assert len(samples) == 7201
    assert samples[0]["time_s"] == 0.0
    assert samples[-1]["time_s"] == 7200.0


@pytest.mark.parametrize("field", ["timeseries", "events", "errors"])
def test_reader_rejects_resigned_conflicting_embedded_externalized_data(
    tmp_path: Path,
    field: str,
) -> None:
    request = _request(run_id=f"embedded-{field}-run")
    written = write_run(request, _success(request), tmp_path, input_resources=_inputs())
    result_path = written.run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    result[field] = [{"forged": True, "time_s": 999.0}]
    result_data = (
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    result_path.write_bytes(result_data)

    def mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        descriptor = artifacts["result"]
        assert isinstance(descriptor, dict)
        descriptor["size_bytes"] = len(result_data)
        descriptor["sha256"] = hashlib.sha256(result_data).hexdigest()

    _resign_manifest(written.run_dir, mutate)
    with pytest.raises(ValueError, match=f"result {field} must be empty"):
        read_run(written.run_dir)


@pytest.mark.parametrize(
    ("artifact_id", "file_name", "fingerprint_field", "error_match"),
    [
        (
            "request",
            "request.json",
            "request_fingerprint",
            "published request fingerprint is missing",
        ),
        (
            "result",
            "result.json",
            "result_fingerprint",
            "published result fingerprint is missing",
        ),
    ],
)
def test_reader_requires_explicit_fingerprint_in_published_documents(
    tmp_path: Path,
    artifact_id: str,
    file_name: str,
    fingerprint_field: str,
    error_match: str,
) -> None:
    request = _request(run_id=f"missing-{artifact_id}-fingerprint-run")
    written = write_run(request, _success(request), tmp_path, input_resources=_inputs())
    artifact_path = written.run_dir / file_name
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document.pop(fingerprint_field)
    artifact_data = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    artifact_path.write_bytes(artifact_data)

    def mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        descriptor = artifacts[artifact_id]
        assert isinstance(descriptor, dict)
        descriptor["size_bytes"] = len(artifact_data)
        descriptor["sha256"] = hashlib.sha256(artifact_data).hexdigest()

    _resign_manifest(written.run_dir, mutate)
    with pytest.raises(ValueError, match=error_match):
        read_run(written.run_dir)


@pytest.mark.parametrize(
    "attack",
    [
        "effective_input",
        "model_version",
        "m5_pipeline",
        "effective_object",
        "unknown_preset",
        "forbidden_parameters",
    ],
)
def test_reader_rejects_fully_resigned_derived_provenance_forgery(
    tmp_path: Path,
    attack: str,
) -> None:
    run_id = f"read-{attack.replace('_', '-')}-run"
    original_request = _request(run_id=run_id)
    written = write_run(
        original_request,
        _success(original_request),
        tmp_path,
        input_resources=_inputs(),
    )
    forged_request, forged_payload = _forged_request_and_payload(
        attack,
        run_id=run_id,
    )
    _replace_published_request_and_payload(
        written.run_dir,
        forged_request,
        forged_payload,
    )

    with pytest.raises(ValueError, match="derived provenance|non-executable"):
        read_run(written.run_dir)
