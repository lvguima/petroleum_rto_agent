from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.runtime.artifacts import (
    RunRecord,
    derive_run_provenance,
    write_run,
)
from petroleum_rto.cdu.runtime.batch import (
    BATCH_MANIFEST_VERSION,
    BATCH_REQUEST_VERSION,
    BatchRequest,
    execute_batch,
    read_batch,
    resume_batch,
)
from petroleum_rto.cdu.runtime.contracts import (
    RUN_REQUEST_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
    ErrorRecord,
    EventRecord,
    ExecutionPayload,
    RunRequest,
    RuntimeStatus,
)
from petroleum_rto.cdu.runtime.presets import get_preset
from petroleum_rto.cdu.runtime.resources import (
    read_runtime_resource_bytes,
    runtime_resource_ids_for_preset,
)

RunOne = Callable[[RunRequest, Path], RunRecord]


def _resign_manifest(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document.pop("manifest_fingerprint")
    mutate(document)
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    document["manifest_fingerprint"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _rewrite_events_and_resign(
    batch_dir: Path,
    mutate: Callable[[list[dict[str, object]]], None],
) -> None:
    events_path = batch_dir / "events.jsonl"
    rows: list[dict[str, object]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        decoded: object = json.loads(line)
        assert isinstance(decoded, dict)
        assert all(isinstance(key, str) for key in decoded)
        rows.append(decoded)
    mutate(rows)
    for sequence, row in enumerate(rows):
        row["sequence"] = sequence
    events_data = b"".join(
        (
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )
    events_path.write_bytes(events_data)

    def update_manifest(document: dict[str, object]) -> None:
        descriptor = document["events_artifact"]
        assert isinstance(descriptor, dict)
        descriptor["size_bytes"] = len(events_data)
        descriptor["sha256"] = hashlib.sha256(events_data).hexdigest()

    _resign_manifest(batch_dir / "batch_manifest.json", update_manifest)


def _request(item_label: str) -> RunRequest:
    return RunRequest(
        schema_version=RUNTIME_SCHEMA_VERSION,
        request_version=RUN_REQUEST_VERSION,
        preset_id="steady-baseline",
        run_type="steady_recycle",
        random_seed=0,
        parameters={},
        overrides={},
        metadata={"purpose": "batch_test", "batch.item_label": item_label},
    )


def _batch(*preset_ids: str, batch_id: str = "batch-test") -> BatchRequest:
    return BatchRequest(
        schema_version=RUNTIME_SCHEMA_VERSION,
        request_version=BATCH_REQUEST_VERSION,
        batch_id=batch_id,
        items=tuple(_request(preset_id) for preset_id in preset_ids),
    )


def _payload(request: RunRequest, status: RuntimeStatus) -> ExecutionPayload:
    failed = status in {"failed", "not_converged", "rejected"}
    errors = (
        (
            ErrorRecord(
                sequence=0,
                error_type="fixture_failure",
                stage="fixture",
                message="injected batch-item failure",
                time_s=None,
                last_valid={"preset_id": request.preset_id},
                retryable=True,
                details={},
            ),
        )
        if failed
        else ()
    )
    provenance = derive_run_provenance(request)
    return ExecutionPayload(
        schema_version=RUNTIME_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        preset_id=request.preset_id,
        run_type=request.run_type,
        runtime_status=status,
        request_fingerprint=request.request_fingerprint,
        engine_status="failed" if failed else "success",
        raw_result_type="batch_fixture",
        summary={"value": 1.0},
        timeseries=(),
        events=(
            EventRecord(
                sequence=0,
                time_s=None,
                event_type="execution_failure",
                source="M7_runtime",
                stage="fixture",
                message="injected batch-item failure",
                details={"error_type": "fixture_failure"},
            ),
        )
        if failed
        else (),
        errors=errors,
        versions=provenance.versions,
        source_fingerprints=provenance.source_fingerprints,
        effective_input_fingerprint=provenance.effective_input_fingerprint,
        synthetic=True,
        data_origin="M2_steady_model_prediction",
        claim_scope="engineering_simulation_only",
        failure_stage="fixture" if failed else None,
        failure_reason="injected batch-item failure" if failed else None,
        failure_time_s=None,
        last_valid={"preset_id": request.preset_id} if failed else None,
        duration_s=None,
        time_step_s=None,
        diagnostics={},
    )


def _publisher(
    calls: list[str],
    statuses: dict[str, RuntimeStatus] | None = None,
    raising: set[str] | None = None,
) -> RunOne:
    selected_statuses = {} if statuses is None else statuses
    selected_raising = set() if raising is None else raising

    def run_one(request: RunRequest, output_root: Path) -> RunRecord:
        item_label = request.metadata["batch.item_label"]
        assert isinstance(item_label, str)
        calls.append(item_label)
        if item_label in selected_raising:
            raise RuntimeError(f"injected exception for {item_label}")
        return write_run(
            request,
            _payload(request, selected_statuses.get(item_label, "success")),
            output_root,
            input_resources={
                resource_id: read_runtime_resource_bytes(resource_id)
                for resource_id in runtime_resource_ids_for_preset(get_preset(request.preset_id))
            },
        )

    return run_one


def test_batch_request_is_strict_immutable_ordered_and_deterministic() -> None:
    request = _batch("first", "second")
    decoded = BatchRequest.from_mapping(request.as_dict())

    assert decoded == request
    assert decoded.batch_fingerprint == request.batch_fingerprint
    assert request.batch_fingerprint != _batch("second", "first").batch_fingerprint
    assert request.as_dict()["batch_fingerprint"] == request.batch_fingerprint

    with pytest.raises(FrozenInstanceError):
        request.batch_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown"):
        BatchRequest.from_mapping({**request.as_dict(), "unknown": 1})
    with pytest.raises(ValueError, match="path traversal"):
        replace(request, batch_id="../escape")

    bool_numeric = request.as_dict()
    first = bool_numeric["items"][0]  # type: ignore[index]
    first["random_seed"] = True
    with pytest.raises(TypeError, match="must not be boolean"):
        BatchRequest.from_mapping(bool_numeric)

    wrong_fingerprint = request.as_dict()
    wrong_fingerprint["batch_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        BatchRequest.from_mapping(wrong_fingerprint)


def test_execute_is_ordered_isolates_exceptions_and_resume_skips_valid_runs(
    tmp_path: Path,
) -> None:
    request = _batch("first", "raises", "third", batch_id="isolation")
    initial_calls: list[str] = []
    initial = execute_batch(
        request,
        output_root=tmp_path,
        run_one=_publisher(initial_calls, raising={"raises"}),
    )

    assert initial_calls == ["first", "raises", "third"]
    assert initial.batch_status == "failed"
    assert initial.completed_items == 2
    assert initial.as_summary_dict() == {
        "batch_dir": str(initial.batch_dir),
        "batch_status": "failed",
        "item_count": 3,
        "completed_items": 2,
    }
    assert (initial.batch_dir / "request.json").is_file()
    assert (initial.batch_dir / "events.jsonl").is_file()
    manifest_path = initial.batch_dir / "batch_manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == BATCH_MANIFEST_VERSION
    assert manifest["batch_fingerprint"] == request.batch_fingerprint
    old_events = (initial.batch_dir / "events.jsonl").read_bytes()
    retained = (initial.item_records[0].run_dir, initial.item_records[2].run_dir)  # type: ignore[union-attr]

    resumed_calls: list[str] = []
    resumed = resume_batch(
        initial.batch_dir,
        run_one=_publisher(resumed_calls),
    )

    assert resumed_calls == ["raises"]
    assert resumed.batch_status == "success"
    assert resumed.completed_items == 3
    assert resumed.item_records[0] is not None
    assert resumed.item_records[2] is not None
    assert (resumed.item_records[0].run_dir, resumed.item_records[2].run_dir) == retained
    assert (resumed.batch_dir / "events.jsonl").read_bytes().startswith(old_events)
    assert len(tuple((resumed.batch_dir / "items/0001").glob("attempt-*"))) == 2


def test_failed_run_is_retained_until_explicit_retry_and_never_overwritten(
    tmp_path: Path,
) -> None:
    request = _batch("fails", batch_id="retry")
    initial_calls: list[str] = []
    initial = execute_batch(
        request,
        output_root=tmp_path,
        run_one=_publisher(initial_calls, {"fails": "failed"}),
    )
    assert initial.batch_status == "failed"
    assert initial.completed_items == 1
    assert initial.item_records[0] is not None
    failed_run_dir = initial.item_records[0].run_dir
    failed_manifest = (failed_run_dir / "manifest.json").read_bytes()

    default_calls: list[str] = []
    retained = resume_batch(
        initial.batch_dir,
        run_one=_publisher(default_calls),
    )
    assert default_calls == []
    assert retained.batch_status == "failed"
    assert retained.item_records[0] is not None
    assert retained.item_records[0].run_dir == failed_run_dir

    retry_calls: list[str] = []
    retried = resume_batch(
        initial.batch_dir,
        retry_failed=True,
        run_one=_publisher(retry_calls),
    )
    assert retry_calls == ["fails"]
    assert retried.batch_status == "success"
    assert retried.item_records[0] is not None
    assert retried.item_records[0].run_dir != failed_run_dir
    assert (failed_run_dir / "manifest.json").read_bytes() == failed_manifest
    assert len(tuple((retried.batch_dir / "items/0000").glob("attempt-*"))) == 2


def test_resume_rejects_request_tampering_before_running_items(tmp_path: Path) -> None:
    request = _batch("only", batch_id="tamper")
    record = execute_batch(
        request,
        output_root=tmp_path,
        run_one=_publisher([]),
    )
    request_path = record.batch_dir / "request.json"
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    payload["batch_id"] = "different"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        resume_batch(record.batch_dir, run_one=_publisher(calls))
    assert calls == []


def test_retry_failed_flag_must_be_boolean(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="retry_failed"):
        execute_batch(
            _batch("only"),
            output_root=tmp_path,
            retry_failed=1,  # type: ignore[arg-type]
            run_one=_publisher([]),
        )


def test_interrupted_resume_can_resume_again_and_history_is_append_only(
    tmp_path: Path,
) -> None:
    request = _batch("fails", batch_id="interrupt-recovery")
    initial = execute_batch(
        request,
        output_root=tmp_path,
        run_one=_publisher([], {"fails": "failed"}),
    )
    current_manifest = initial.batch_dir / "batch_manifest.json"
    initial_manifest_bytes = current_manifest.read_bytes()
    initial_manifest = json.loads(initial_manifest_bytes)
    initial_fingerprint = initial_manifest["manifest_fingerprint"]
    interrupt_calls: list[str] = []

    def interrupt(request: RunRequest, output_root: Path) -> RunRecord:
        del output_root
        item_label = request.metadata["batch.item_label"]
        assert isinstance(item_label, str)
        interrupt_calls.append(item_label)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        resume_batch(
            initial.batch_dir,
            retry_failed=True,
            run_one=interrupt,
        )

    archived = initial.batch_dir / "history" / f"manifest-{initial_fingerprint}.json"
    assert interrupt_calls == ["fails"]
    assert not current_manifest.exists()
    assert archived.read_bytes() == initial_manifest_bytes

    retry_calls: list[str] = []
    recovered = resume_batch(
        initial.batch_dir,
        run_one=_publisher(retry_calls),
    )
    assert retry_calls == ["fails"]
    assert recovered.batch_status == "success"
    assert read_batch(recovered.batch_dir).batch_status == "success"
    assert archived.read_bytes() == initial_manifest_bytes
    assert len(tuple((recovered.batch_dir / "items/0000").glob("attempt-*"))) == 3

    final_calls: list[str] = []
    resumed_again = resume_batch(
        recovered.batch_dir,
        run_one=_publisher(final_calls),
    )
    assert final_calls == []
    assert resumed_again.batch_status == "success"
    assert archived.read_bytes() == initial_manifest_bytes
    assert len(tuple((recovered.batch_dir / "history").glob("manifest-*.json"))) == 2


@pytest.mark.parametrize("forgery", ["items", "count", "status"])
def test_read_batch_rejects_resigned_semantic_manifest_forgery(
    tmp_path: Path,
    forgery: str,
) -> None:
    record = execute_batch(
        _batch("first", "second", batch_id="resigned-forgery"),
        output_root=tmp_path,
        run_one=_publisher([]),
    )
    manifest_path = record.batch_dir / "batch_manifest.json"

    def mutate(document: dict[str, object]) -> None:
        if forgery == "items":
            items = document["items"]
            assert isinstance(items, list)
            items.reverse()
        elif forgery == "count":
            document["item_count"] = 3
        else:
            document["batch_status"] = "failed"

    _resign_manifest(manifest_path, mutate)

    with pytest.raises(ValueError):
        read_batch(record.batch_dir)


def test_event_log_uses_all_seven_frozen_event_types_with_typed_fields(
    tmp_path: Path,
) -> None:
    initial = execute_batch(
        _batch("first", "raises", batch_id="event-types"),
        output_root=tmp_path,
        run_one=_publisher([], raising={"raises"}),
    )
    recovered = resume_batch(initial.batch_dir, run_one=_publisher([]))
    rows = [
        json.loads(line)
        for line in (recovered.batch_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["event_type"] for row in rows} == {
        "batch_started",
        "batch_resumed",
        "item_started",
        "item_completed",
        "item_exception",
        "item_skipped",
        "batch_finished",
    }
    for row in rows:
        event_type = row["event_type"]
        if event_type.startswith("batch_"):
            assert row["item_index"] is None
            assert row["attempt_number"] is None
        else:
            assert isinstance(row["item_index"], int)
            assert isinstance(row["attempt_number"], int)
            assert row["attempt_number"] >= 1
        if event_type in {"batch_started", "batch_resumed", "item_started"}:
            assert row["runtime_status"] is None
        elif event_type == "item_exception":
            assert row["runtime_status"] == "failed"
        else:
            assert isinstance(row["runtime_status"], str)


@pytest.mark.parametrize(
    "forgery",
    (
        "fabricated_event",
        "item_index_999",
        "fake_finished",
        "nonexistent_attempt",
    ),
)
def test_read_batch_rejects_resigned_event_evidence_forgery(
    tmp_path: Path,
    forgery: str,
) -> None:
    record = execute_batch(
        _batch("only", batch_id=f"event-forgery-{forgery}"),
        output_root=tmp_path,
        run_one=_publisher([]),
    )

    def mutate(rows: list[dict[str, object]]) -> None:
        if forgery == "fabricated_event":
            started = next(row for row in rows if row["event_type"] == "item_started")
            started["event_type"] = "fabricated_event"
        elif forgery == "item_index_999":
            started = next(row for row in rows if row["event_type"] == "item_started")
            started["item_index"] = 999
        elif forgery == "fake_finished":
            assert rows[-1]["event_type"] == "batch_finished"
            rows[-1]["runtime_status"] = "failed"
        else:
            for row in rows:
                if row["event_type"] in {"item_started", "item_completed"}:
                    row["attempt_number"] = 999

    _rewrite_events_and_resign(record.batch_dir, mutate)

    with pytest.raises(ValueError):
        read_batch(record.batch_dir)


@pytest.mark.parametrize(
    "event_type",
    (
        "batch_started",
        "batch_resumed",
        "item_started",
        "item_completed",
        "item_exception",
        "item_skipped",
        "batch_finished",
    ),
)
def test_each_event_type_rejects_invalid_nullable_or_status_semantics(
    tmp_path: Path,
    event_type: str,
) -> None:
    initial = execute_batch(
        _batch("first", "raises", batch_id=f"event-fields-{event_type}"),
        output_root=tmp_path,
        run_one=_publisher([], raising={"raises"}),
    )
    record = (
        resume_batch(initial.batch_dir, run_one=_publisher([]))
        if event_type in {"batch_resumed", "item_skipped"}
        else initial
    )

    def mutate(rows: list[dict[str, object]]) -> None:
        selected = next(row for row in rows if row["event_type"] == event_type)
        if event_type in {"batch_started", "batch_finished"}:
            selected["item_index"] = 0
        elif event_type in {"batch_resumed", "item_started"}:
            selected["runtime_status"] = "success"
        elif event_type == "item_completed":
            selected["runtime_status"] = None
        elif event_type == "item_exception":
            selected["runtime_status"] = "not_converged"
        else:
            selected["attempt_number"] = None

    _rewrite_events_and_resign(record.batch_dir, mutate)

    with pytest.raises((TypeError, ValueError)):
        read_batch(record.batch_dir)


@pytest.mark.parametrize("mutation", ("empty", "wrong_run_id"))
def test_read_batch_strictly_reloads_attempt_request_and_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    record = execute_batch(
        _batch("only", batch_id=f"attempt-request-{mutation}"),
        output_root=tmp_path,
        run_one=_publisher([]),
    )
    request_path = record.batch_dir / "items/0000/attempt-0001/request.json"
    if mutation == "empty":
        request_path.write_text("{}\n", encoding="utf-8")
    else:
        document = json.loads(request_path.read_text(encoding="utf-8"))
        document["run_id"] = "forged-attempt-identity"
        request_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="attempt request"):
        read_batch(record.batch_dir)


@pytest.mark.parametrize(
    "forgery",
    ("out_of_range_item", "non_contiguous_attempt", "orphan_file"),
)
def test_read_batch_rejects_contract_external_items_layout(
    tmp_path: Path,
    forgery: str,
) -> None:
    record = execute_batch(
        _batch("only", batch_id=f"layout-{forgery}"),
        output_root=tmp_path,
        run_one=_publisher([]),
    )
    if forgery == "out_of_range_item":
        orphan = record.batch_dir / "items/9999/attempt-0001/orphan.txt"
        orphan.parent.mkdir(parents=True)
        orphan.write_text("orphan", encoding="utf-8")
    elif forgery == "non_contiguous_attempt":
        (record.batch_dir / "items/0000/attempt-0003").mkdir()
    else:
        (record.batch_dir / "items/0000/attempt-0001/orphan.txt").write_text(
            "orphan",
            encoding="utf-8",
        )

    with pytest.raises(ValueError):
        read_batch(record.batch_dir)


def test_historical_manifest_rejects_resigned_unfinished_event_prefix(
    tmp_path: Path,
) -> None:
    initial = execute_batch(
        _batch("only", batch_id="history-event-tail"),
        output_root=tmp_path,
        run_one=_publisher([]),
    )
    resumed = resume_batch(initial.batch_dir, run_one=_publisher([]))
    archived = next((resumed.batch_dir / "history").glob("manifest-*.json"))

    def truncate_finished(document: dict[str, object]) -> None:
        descriptor = document["events_artifact"]
        assert isinstance(descriptor, dict)
        original_size = descriptor["size_bytes"]
        assert isinstance(original_size, int)
        prefix = (resumed.batch_dir / "events.jsonl").read_bytes()[:original_size]
        lines = prefix.splitlines(keepends=True)
        assert len(lines) >= 2
        truncated = b"".join(lines[:-1])
        descriptor["size_bytes"] = len(truncated)
        descriptor["sha256"] = hashlib.sha256(truncated).hexdigest()

    _resign_manifest(archived, truncate_finished)

    with pytest.raises(ValueError, match="end with batch_finished"):
        read_batch(resumed.batch_dir)


def test_default_batch_path_does_not_bypass_custom_preview_confirmation(
    tmp_path: Path,
) -> None:
    custom = replace(
        _request("custom-steady-batch"),
        parameters={"feed.mass_flow_t_h": 360.0},
    )
    batch = BatchRequest(
        schema_version=RUNTIME_SCHEMA_VERSION,
        request_version=BATCH_REQUEST_VERSION,
        batch_id="custom-input-batch",
        items=(custom,),
    )

    record = execute_batch(batch, output_root=tmp_path)
    reloaded = read_batch(record.batch_dir)

    assert reloaded.batch_status == "failed"
    assert reloaded.completed_items == 0
    assert reloaded.item_records == (None,)
    events = (record.batch_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "confirmed preview fingerprint" in events
