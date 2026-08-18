"""Ordered, resumable and manifest-last M7 batch execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Final, cast

from .artifacts import RunRecord, read_run
from .contracts import RUNTIME_SCHEMA_VERSION, RunRequest

BATCH_REQUEST_VERSION: Final[str] = "cdu-mini-batch-request-v0.1.0"
BATCH_MANIFEST_VERSION: Final[str] = "cdu-mini-batch-manifest-v0.1.0"

type RunOne = Callable[[RunRequest, Path], RunRecord]

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_FAILED_STATUSES: Final[frozenset[str]] = frozenset({"failed", "not_converged"})
_NON_FAILURE_STATUSES: Final[frozenset[str]] = frozenset({"success", "limited", "rejected"})
_BATCH_STATUSES: Final[frozenset[str]] = frozenset({"success", "limited", "failed"})
_RUNTIME_STATUSES: Final[frozenset[str]] = _FAILED_STATUSES | _NON_FAILURE_STATUSES
_BATCH_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "batch_started",
        "batch_resumed",
        "item_started",
        "item_completed",
        "item_exception",
        "item_skipped",
        "batch_finished",
    }
)
_BATCH_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "sequence",
        "event_type",
        "item_index",
        "attempt_number",
        "runtime_status",
        "message",
    }
)
_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "manifest_version",
        "artifact_state",
        "batch_id",
        "batch_status",
        "batch_fingerprint",
        "item_count",
        "completed_items",
        "request_artifact",
        "events_artifact",
        "items",
        "manifest_fingerprint",
    }
)
_ITEM_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "item_index",
        "request_fingerprint",
        "run_dir",
        "runtime_status",
        "result_fingerprint",
        "run_manifest_fingerprint",
    }
)
_ARTIFACT_DESCRIPTOR_FIELDS: Final[frozenset[str]] = frozenset({"path", "size_bytes", "sha256"})


def _json_bytes(value: object) -> bytes:
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


def _fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_json_bytes(dict(value)).rstrip(b"\n")).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty identifier")
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"{context} must not contain path traversal")
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a supported identifier")
    return value


def _integer(value: object, *, context: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer and must not be boolean")
    if value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be a sequence")
    return cast(Sequence[object], value)


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _load_json(path: Path, *, context: str) -> Mapping[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    return _mapping(value, context=context)


def _safe_child(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("batch path leaves its batch directory")
    return target


def _verify_written_json(path: Path, data: bytes, *, json_lines: bool) -> None:
    verified = path.read_bytes()
    if (
        len(verified) != len(data)
        or hashlib.sha256(verified).digest() != hashlib.sha256(data).digest()
    ):
        raise OSError(f"staged batch artifact verification failed for {path.name}")
    if json_lines:
        for line_number, line in enumerate(verified.splitlines(), start=1):
            if not line.strip():
                raise ValueError(f"{path.name}:{line_number} must not be blank")
            value = json.loads(line)
            if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
                raise ValueError(f"{path.name}:{line_number} must contain a JSON object")
    else:
        json.loads(verified)


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
    _verify_written_json(path, data, json_lines=path.suffix == ".jsonl")


def _publish(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.stage")
    staged.unlink(missing_ok=True)
    with staged.open("xb") as handle:
        handle.write(data)
    _verify_written_json(staged, data, json_lines=False)
    staged.replace(path)


@dataclass(frozen=True)
class BatchRequest:
    """One strict ordered collection of semantic :class:`RunRequest` items."""

    schema_version: str
    request_version: str
    batch_id: str
    items: tuple[RunRequest, ...]

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ValueError("batch schema_version differs from the runtime contract")
        if self.request_version != BATCH_REQUEST_VERSION:
            raise ValueError("batch request_version differs from the runtime contract")
        object.__setattr__(
            self,
            "batch_id",
            _safe_identifier(self.batch_id, context="batch_id"),
        )
        items = tuple(self.items)
        if not items:
            raise ValueError("batch items cannot be empty")
        if any(not isinstance(item, RunRequest) for item in items):
            raise TypeError("batch items must contain RunRequest values")
        object.__setattr__(self, "items", items)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "batch_id": self.batch_id,
            "item_request_fingerprints": [item.request_fingerprint for item in self.items],
        }

    @property
    def batch_fingerprint(self) -> str:
        return _fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "batch_id": self.batch_id,
            "items": [item.as_dict() for item in self.items],
            "batch_fingerprint": self.batch_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BatchRequest:
        required = {"schema_version", "request_version", "batch_id", "items"}
        allowed = required | {"batch_fingerprint"}
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - allowed)
        if missing or unknown:
            raise ValueError(f"batch request fields differ; missing={missing}, unknown={unknown}")
        raw_items = _sequence(value["items"], context="batch items")
        request = cls(
            schema_version=cast(str, value["schema_version"]),
            request_version=cast(str, value["request_version"]),
            batch_id=_safe_identifier(value["batch_id"], context="batch_id"),
            items=tuple(
                RunRequest.from_mapping(_mapping(item, context=f"batch items[{index}]"))
                for index, item in enumerate(raw_items)
            ),
        )
        supplied = value.get("batch_fingerprint")
        if (
            supplied is not None
            and _digest(
                supplied,
                context="batch_fingerprint",
            )
            != request.batch_fingerprint
        ):
            raise ValueError("batch request fingerprint mismatch")
        return request


@dataclass(frozen=True)
class BatchRecord:
    """Verified current batch view with one selected run per ordered item."""

    batch_dir: Path
    request: BatchRequest
    batch_status: str
    item_records: tuple[RunRecord | None, ...]

    def __post_init__(self) -> None:
        if self.batch_status not in {"success", "limited", "failed"}:
            raise ValueError("batch_status must be success, limited or failed")
        records = tuple(self.item_records)
        if len(records) != len(self.request.items):
            raise ValueError("batch item records must align with request items")
        for index, record in enumerate(records):
            if record is not None and (
                not isinstance(record, RunRecord)
                or record.request.request_fingerprint
                != self.request.items[index].request_fingerprint
            ):
                raise ValueError("batch item record belongs to another request")
        object.__setattr__(self, "batch_dir", self.batch_dir.resolve())
        object.__setattr__(self, "item_records", records)

    @property
    def completed_items(self) -> int:
        return sum(record is not None for record in self.item_records)

    def as_summary_dict(self) -> dict[str, object]:
        return {
            "batch_dir": str(self.batch_dir),
            "batch_status": self.batch_status,
            "item_count": len(self.request.items),
            "completed_items": self.completed_items,
        }


def _event(
    events_path: Path,
    *,
    event_type: str,
    item_index: int | None = None,
    attempt_number: int | None = None,
    runtime_status: str | None = None,
    message: str,
) -> None:
    existing = _read_events(events_path)
    row = {
        "sequence": len(existing),
        "event_type": event_type,
        "item_index": item_index,
        "attempt_number": attempt_number,
        "runtime_status": runtime_status,
        "message": message,
    }
    _validate_event_row(row, expected_sequence=len(existing))
    with events_path.open("ab") as handle:
        handle.write(_json_bytes(row))


def _validate_event_row(
    row: Mapping[str, object],
    *,
    expected_sequence: int,
) -> None:
    if set(row) != _BATCH_EVENT_FIELDS:
        raise ValueError("batch event fields differ from the fixed contract")
    if _integer(row["sequence"], context="batch event sequence") != expected_sequence:
        raise ValueError("batch event sequences must be contiguous from zero")
    event_type = _safe_identifier(row["event_type"], context="batch event_type")
    if event_type not in _BATCH_EVENT_TYPES:
        raise ValueError(f"unsupported batch event_type: {event_type!r}")

    item_event = event_type.startswith("item_")
    attempt_event = event_type in {
        "item_started",
        "item_completed",
        "item_exception",
        "item_skipped",
    }
    if item_event:
        _integer(row["item_index"], context="batch event item_index")
    elif row["item_index"] is not None:
        raise ValueError(f"{event_type} item_index must be null")
    if attempt_event:
        _integer(
            row["attempt_number"],
            context="batch event attempt_number",
            minimum=1,
        )
    elif row["attempt_number"] is not None:
        raise ValueError(f"{event_type} attempt_number must be null")

    runtime_status = row["runtime_status"]
    if event_type in {"batch_started", "batch_resumed", "item_started"}:
        if runtime_status is not None:
            raise ValueError(f"{event_type} runtime_status must be null")
    elif event_type == "batch_finished":
        if not isinstance(runtime_status, str) or runtime_status not in _BATCH_STATUSES:
            raise ValueError("batch_finished runtime_status is invalid")
    elif event_type == "item_exception":
        if runtime_status != "failed":
            raise ValueError("item_exception runtime_status must be failed")
    elif not isinstance(runtime_status, str) or runtime_status not in _RUNTIME_STATUSES:
        raise ValueError(f"{event_type} runtime_status is invalid")

    if not isinstance(row["message"], str) or not row["message"].strip():
        raise ValueError("batch event message must be non-empty")


def _events_from_bytes(
    data: bytes,
    *,
    context: str,
) -> tuple[Mapping[str, object], ...]:
    if data and not data.endswith(b"\n"):
        raise ValueError(f"{context} must end at a complete JSONL record")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{context} must be valid UTF-8") from exc
    rows: list[Mapping[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"{context}:{line_number} must not be blank")
        row = _mapping(json.loads(line), context=f"{context}:{line_number}")
        _validate_event_row(row, expected_sequence=len(rows))
        rows.append(row)
    return tuple(rows)


def _read_events(path: Path) -> tuple[Mapping[str, object], ...]:
    if not path.is_file():
        return ()
    return _events_from_bytes(path.read_bytes(), context=path.name)


def _attempt_dirs(batch_dir: Path, item_index: int) -> tuple[Path, ...]:
    item_dir = _safe_child(batch_dir, f"items/{item_index:04d}")
    if not item_dir.is_dir():
        return ()
    attempts: list[tuple[int, Path]] = []
    for child in item_dir.iterdir():
        match = re.fullmatch(r"attempt-(\d{4})", child.name)
        if not child.is_dir() or match is None:
            raise ValueError("batch item directory contains an unsupported entry")
        attempt_number = int(match.group(1))
        if attempt_number < 1:
            raise ValueError("batch attempt numbers must start at one")
        attempts.append((attempt_number, child))
    numbers = tuple(number for number, _ in sorted(attempts))
    if numbers and numbers != tuple(range(1, numbers[-1] + 1)):
        raise ValueError("batch attempt numbers must be continuous from one")
    return tuple(path for _, path in sorted(attempts))


def _attempt_number(attempt_dir: Path) -> int:
    match = re.fullmatch(r"attempt-(\d{4})", attempt_dir.name)
    if match is None:
        raise ValueError("batch attempt directory name is invalid")
    return _integer(int(match.group(1)), context="batch attempt number", minimum=1)


def _validate_items_layout(
    batch_dir: Path,
    request: BatchRequest,
    *,
    require_all_items: bool,
) -> None:
    items_dir = batch_dir / "items"
    if not items_dir.exists():
        if require_all_items:
            raise ValueError("completed batch is missing its items directory")
        return
    if not items_dir.is_dir():
        raise ValueError("batch items must be a directory")
    observed: set[int] = set()
    for child in items_dir.iterdir():
        match = re.fullmatch(r"(\d{4})", child.name)
        if not child.is_dir() or match is None:
            raise ValueError("batch items directory contains an unsupported entry")
        item_index = int(match.group(1))
        if item_index >= len(request.items) or child.name != f"{item_index:04d}":
            raise ValueError("batch items directory contains an out-of-range item")
        observed.add(item_index)
        _attempt_dirs(batch_dir, item_index)
    if require_all_items and observed != set(range(len(request.items))):
        raise ValueError("completed batch items do not cover the request")


def _expected_attempt_request(
    expected: RunRequest,
    *,
    item_index: int,
    attempt_number: int,
) -> RunRequest:
    return replace(
        expected,
        run_id=f"item-{item_index:04d}-attempt-{attempt_number:04d}",
        requested_at_utc=None,
    )


def _load_attempt_request(
    attempt_dir: Path,
    expected: RunRequest,
) -> RunRequest:
    if re.fullmatch(r"\d{4}", attempt_dir.parent.name) is None:
        raise ValueError("batch attempt item directory name is invalid")
    item_index = int(attempt_dir.parent.name)
    attempt_number = _attempt_number(attempt_dir)
    request_path = attempt_dir / "request.json"
    if not request_path.is_file():
        raise ValueError("batch attempt request.json is missing")
    document = _load_json(request_path, context="batch attempt request")
    if "request_fingerprint" not in document:
        raise ValueError("batch attempt request fingerprint is missing")
    attempt_request = RunRequest.from_mapping(document)
    expected_request = _expected_attempt_request(
        expected,
        item_index=item_index,
        attempt_number=attempt_number,
    )
    if attempt_request != expected_request:
        raise ValueError("batch attempt request differs from its batch item")
    return attempt_request


def _record_in_attempt(attempt_dir: Path, expected: RunRequest) -> RunRecord | None:
    attempt_request = _load_attempt_request(attempt_dir, expected)
    expected_run_id = cast(str, attempt_request.run_id)
    children = tuple(child for child in attempt_dir.iterdir() if child.name != "request.json")
    if not children:
        return None
    if len(children) != 1 or not children[0].is_dir() or children[0].name != expected_run_id:
        raise ValueError("batch attempt contains an unsupported entry")
    run_dir = children[0]
    if not (run_dir / "manifest.json").is_file():
        return None
    record = read_run(run_dir)
    if record.request != attempt_request:
        raise ValueError("batch attempt run request differs from request.json")
    return record


def _event_attempt_dir(
    batch_dir: Path,
    *,
    item_index: int,
    attempt_number: int,
) -> Path:
    attempt_dir = _safe_child(
        batch_dir,
        f"items/{item_index:04d}/attempt-{attempt_number:04d}",
    )
    if not attempt_dir.is_dir():
        raise ValueError("batch event references a nonexistent attempt")
    return attempt_dir


def _record_attempt_number(batch_dir: Path, record: RunRecord) -> int:
    try:
        relative = record.run_dir.resolve().relative_to(batch_dir.resolve())
    except ValueError as exc:
        raise ValueError("batch run evidence leaves its batch directory") from exc
    parts = relative.parts
    if len(parts) != 4 or re.fullmatch(r"attempt-\d{4}", parts[2]) is None:
        raise ValueError("batch run evidence differs from the attempt layout")
    return _integer(
        int(parts[2].removeprefix("attempt-")),
        context="batch run attempt number",
        minimum=1,
    )


def _verify_event_timeline(
    batch_dir: Path,
    request: BatchRequest,
    events: tuple[Mapping[str, object], ...],
    *,
    final_records: tuple[RunRecord | None, ...] | None = None,
    final_status: str | None = None,
    require_finished: bool,
) -> None:
    if not events:
        raise ValueError("batch event timeline cannot be empty")
    if events[0]["event_type"] != "batch_started":
        raise ValueError("batch event timeline must start with batch_started")

    active_epoch = False
    saw_batch_started = False
    started_attempts: set[tuple[int, int]] = set()
    terminal_attempts: dict[tuple[int, int], str] = {}
    completed_attempts: dict[tuple[int, int], str] = {}

    for event in events:
        event_type = cast(str, event["event_type"])
        if event_type == "batch_started":
            if saw_batch_started or event["sequence"] != 0:
                raise ValueError("batch_started must occur exactly once at sequence zero")
            saw_batch_started = True
            active_epoch = True
            continue
        if event_type == "batch_resumed":
            if not saw_batch_started:
                raise ValueError("batch_resumed cannot precede batch_started")
            # A recovery may legitimately supersede an interrupted epoch whose
            # latest item_started event has no completed/exception event.
            active_epoch = True
            continue
        if event_type == "batch_finished":
            if not active_epoch:
                raise ValueError("batch_finished has no active execution epoch")
            active_epoch = False
            continue
        if not active_epoch:
            raise ValueError("batch item event occurs outside an execution epoch")

        item_index = _integer(
            event["item_index"],
            context="batch event item_index",
        )
        if item_index >= len(request.items):
            raise ValueError("batch event item_index is outside the request")
        attempt_number = _integer(
            event["attempt_number"],
            context="batch event attempt_number",
            minimum=1,
        )
        attempt_dir = _event_attempt_dir(
            batch_dir,
            item_index=item_index,
            attempt_number=attempt_number,
        )
        key = (item_index, attempt_number)

        if event_type == "item_started":
            if key in started_attempts:
                raise ValueError("batch attempt has duplicate item_started events")
            started_attempts.add(key)
            continue
        if event_type == "item_skipped":
            record = _record_in_attempt(attempt_dir, request.items[item_index])
            if record is None:
                raise ValueError("item_skipped must reference an existing valid run")
            if event["runtime_status"] != record.payload.runtime_status:
                raise ValueError("item_skipped runtime_status differs from its run")
            if completed_attempts.get(key) != record.payload.runtime_status:
                raise ValueError("item_skipped lacks prior item_completed evidence")
            continue

        if key not in started_attempts:
            raise ValueError(f"{event_type} has no matching item_started event")
        if key in terminal_attempts:
            raise ValueError("batch attempt has multiple terminal events")
        terminal_attempts[key] = event_type
        record = _record_in_attempt(attempt_dir, request.items[item_index])
        if event_type == "item_completed":
            if record is None:
                raise ValueError("item_completed must reference a valid run manifest")
            runtime_status = cast(str, event["runtime_status"])
            if runtime_status != record.payload.runtime_status:
                raise ValueError("item_completed runtime_status differs from its run")
            completed_attempts[key] = runtime_status
        elif record is not None:
            raise ValueError("item_exception cannot conceal a valid run manifest")

    if not saw_batch_started:
        raise ValueError("batch event timeline is missing batch_started")
    if not require_finished:
        return
    if events[-1]["event_type"] != "batch_finished" or active_epoch:
        raise ValueError("completed batch events must end with batch_finished")
    if final_status is None or final_records is None:
        raise ValueError("completed batch event verification needs manifest evidence")
    if events[-1]["runtime_status"] != final_status:
        raise ValueError("batch_finished status differs from the batch manifest")

    for item_index, record in enumerate(final_records):
        if record is None:
            continue
        key = (item_index, _record_attempt_number(batch_dir, record))
        if completed_attempts.get(key) != record.payload.runtime_status:
            raise ValueError("batch manifest run lacks matching item_completed evidence")


def _selected_records(batch_dir: Path, request: BatchRequest) -> tuple[RunRecord | None, ...]:
    selected: list[RunRecord | None] = []
    for index, item in enumerate(request.items):
        valid = tuple(
            record
            for record in (
                _record_in_attempt(attempt, item) for attempt in _attempt_dirs(batch_dir, index)
            )
            if record is not None
        )
        non_failed = tuple(
            record for record in valid if record.payload.runtime_status in _NON_FAILURE_STATUSES
        )
        selected.append(non_failed[-1] if non_failed else (valid[-1] if valid else None))
    return tuple(selected)


def _status(records: tuple[RunRecord | None, ...]) -> str:
    if any(record is None for record in records):
        return "failed"
    statuses = tuple(cast(RunRecord, record).payload.runtime_status for record in records)
    if any(status in _FAILED_STATUSES for status in statuses):
        return "failed"
    if any(status in {"limited", "rejected"} for status in statuses):
        return "limited"
    return "success"


def _manifest_payload(
    batch_dir: Path,
    request: BatchRequest,
    records: tuple[RunRecord | None, ...],
) -> dict[str, object]:
    request_data = (batch_dir / "request.json").read_bytes()
    events_data = (batch_dir / "events.jsonl").read_bytes()
    payload: dict[str, object] = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "manifest_version": BATCH_MANIFEST_VERSION,
        "artifact_state": "complete",
        "batch_id": request.batch_id,
        "batch_status": _status(records),
        "batch_fingerprint": request.batch_fingerprint,
        "item_count": len(request.items),
        "completed_items": sum(record is not None for record in records),
        "request_artifact": {
            "path": "request.json",
            "size_bytes": len(request_data),
            "sha256": _sha256(request_data),
        },
        "events_artifact": {
            "path": "events.jsonl",
            "size_bytes": len(events_data),
            "sha256": _sha256(events_data),
        },
        "items": [
            {
                "item_index": index,
                "request_fingerprint": request.items[index].request_fingerprint,
                "run_dir": (
                    None
                    if record is None
                    else record.run_dir.resolve().relative_to(batch_dir).as_posix()
                ),
                "runtime_status": (None if record is None else record.payload.runtime_status),
                "result_fingerprint": (
                    None if record is None else record.payload.result_fingerprint
                ),
                "run_manifest_fingerprint": (
                    None if record is None else record.manifest.manifest_fingerprint
                ),
            }
            for index, record in enumerate(records)
        ],
    }
    return payload


def _write_manifest(
    batch_dir: Path,
    request: BatchRequest,
    records: tuple[RunRecord | None, ...],
) -> None:
    payload = _manifest_payload(batch_dir, request, records)
    payload["manifest_fingerprint"] = _fingerprint(payload)
    _publish(batch_dir / "batch_manifest.json", _json_bytes(payload))


def _load_batch_request(batch_dir: Path) -> BatchRequest:
    request_path = batch_dir / "request.json"
    if not request_path.is_file():
        raise ValueError("batch request.json is missing")
    document = _load_json(request_path, context="batch request")
    if "batch_fingerprint" not in document:
        raise ValueError("persisted batch request fingerprint is missing")
    request = BatchRequest.from_mapping(document)
    if batch_dir.name != request.batch_id:
        raise ValueError("batch directory name differs from batch_id")
    return request


def _manifest_document(path: Path) -> tuple[dict[str, object], str, bytes]:
    if not path.is_file():
        raise ValueError("batch is incomplete because batch_manifest.json is absent")
    data = path.read_bytes()
    document = dict(_mapping(json.loads(data), context=f"batch manifest {path.name}"))
    if set(document) != _MANIFEST_FIELDS:
        missing = sorted(_MANIFEST_FIELDS - set(document))
        unknown = sorted(set(document) - _MANIFEST_FIELDS)
        raise ValueError(f"batch manifest fields differ; missing={missing}, unknown={unknown}")
    supplied = _digest(
        document.pop("manifest_fingerprint"),
        context="batch manifest fingerprint",
    )
    if _fingerprint(document) != supplied:
        raise ValueError("batch manifest fingerprint mismatch")
    return document, supplied, data


def _verify_artifact_descriptor(
    batch_dir: Path,
    payload: Mapping[str, object],
    *,
    field: str,
    filename: str,
    allow_prefix: bool,
) -> bytes:
    descriptor = _mapping(payload.get(field), context=field)
    if set(descriptor) != _ARTIFACT_DESCRIPTOR_FIELDS:
        raise ValueError(f"batch {field} fields differ from the fixed contract")
    if descriptor["path"] != filename:
        raise ValueError(f"batch {field} path mismatch")
    expected_size = _integer(
        descriptor["size_bytes"],
        context=f"batch {field} size_bytes",
    )
    expected_hash = _digest(
        descriptor["sha256"],
        context=f"batch {field} sha256",
    )
    artifact_path = batch_dir / filename
    if not artifact_path.is_file():
        raise ValueError(f"batch {field} is missing")
    data = artifact_path.read_bytes()
    if allow_prefix:
        if expected_size > len(data):
            raise ValueError(f"batch historical {field} size exceeds current artifact")
        data = data[:expected_size]
        if data and not data.endswith(b"\n"):
            raise ValueError(f"batch historical {field} ends inside a record")
    if len(data) != expected_size or _sha256(data) != expected_hash:
        raise ValueError(f"batch {field} hash/size mismatch")
    return data


def _manifest_run_dir(
    batch_dir: Path,
    raw_value: object,
    *,
    item_index: int,
) -> Path:
    if not isinstance(raw_value, str) or not raw_value or "\\" in raw_value:
        raise ValueError("batch item run_dir must be a relative POSIX path")
    relative = PurePosixPath(raw_value)
    parts = relative.parts
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_value
        or ".." in parts
        or len(parts) != 4
        or parts[0] != "items"
        or parts[1] != f"{item_index:04d}"
        or re.fullmatch(r"attempt-\d{4}", parts[2]) is None
    ):
        raise ValueError("batch item run_dir differs from the attempt layout")
    return _safe_child(batch_dir, raw_value)


def _records_from_manifest(
    batch_dir: Path,
    request: BatchRequest,
    payload: Mapping[str, object],
    *,
    historical: bool,
) -> tuple[RunRecord | None, ...]:
    if payload.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        raise ValueError("batch manifest schema_version differs from runtime")
    if payload.get("manifest_version") != BATCH_MANIFEST_VERSION:
        raise ValueError("batch manifest_version differs from runtime")
    if payload.get("artifact_state") != "complete":
        raise ValueError("batch manifest artifact_state must be complete")
    if payload.get("batch_id") != request.batch_id:
        raise ValueError("batch manifest batch_id differs from request")
    _validate_items_layout(batch_dir, request, require_all_items=True)
    if (
        _digest(
            payload.get("batch_fingerprint"),
            context="batch manifest batch_fingerprint",
        )
        != request.batch_fingerprint
    ):
        raise ValueError("batch manifest belongs to another request")
    batch_status = payload.get("batch_status")
    if not isinstance(batch_status, str) or batch_status not in _BATCH_STATUSES:
        raise ValueError("batch manifest batch_status is invalid")

    _verify_artifact_descriptor(
        batch_dir,
        payload,
        field="request_artifact",
        filename="request.json",
        allow_prefix=False,
    )
    events_data = _verify_artifact_descriptor(
        batch_dir,
        payload,
        field="events_artifact",
        filename="events.jsonl",
        allow_prefix=historical,
    )

    raw_items = _sequence(payload.get("items"), context="batch manifest items")
    if len(raw_items) != len(request.items):
        raise ValueError("batch manifest items do not cover the request")
    records: list[RunRecord | None] = []
    for index, raw_item in enumerate(raw_items):
        item = _mapping(raw_item, context=f"batch manifest items[{index}]")
        if set(item) != _ITEM_MANIFEST_FIELDS:
            raise ValueError("batch manifest item fields differ from the fixed contract")
        if (
            _integer(
                item["item_index"],
                context="batch manifest item_index",
            )
            != index
        ):
            raise ValueError("batch manifest items are not in request order")
        if (
            _digest(
                item["request_fingerprint"],
                context="batch manifest item request_fingerprint",
            )
            != request.items[index].request_fingerprint
        ):
            raise ValueError("batch manifest item belongs to another request")

        raw_run_dir = item["run_dir"]
        if raw_run_dir is None:
            if any(
                item[field] is not None
                for field in (
                    "runtime_status",
                    "result_fingerprint",
                    "run_manifest_fingerprint",
                )
            ):
                raise ValueError("batch manifest incomplete item has run metadata")
            records.append(None)
            continue

        run_dir = _manifest_run_dir(
            batch_dir,
            raw_run_dir,
            item_index=index,
        )
        record = read_run(run_dir)
        if record.request.request_fingerprint != request.items[index].request_fingerprint:
            raise ValueError("batch manifest run belongs to another request")
        runtime_status = item["runtime_status"]
        if (
            not isinstance(runtime_status, str)
            or runtime_status != record.payload.runtime_status
            or runtime_status != record.manifest.runtime_status
        ):
            raise ValueError("batch manifest run status mismatch")
        if (
            _digest(
                item["result_fingerprint"],
                context="batch manifest item result_fingerprint",
            )
            != record.payload.result_fingerprint
        ):
            raise ValueError("batch manifest run result fingerprint mismatch")
        if (
            _digest(
                item["run_manifest_fingerprint"],
                context="batch manifest item run_manifest_fingerprint",
            )
            != record.manifest.manifest_fingerprint
        ):
            raise ValueError("batch manifest run manifest fingerprint mismatch")
        records.append(record)

    result = tuple(records)
    if _integer(
        payload.get("item_count"),
        context="batch manifest item_count",
    ) != len(request.items):
        raise ValueError("batch manifest item_count mismatch")
    if _integer(
        payload.get("completed_items"),
        context="batch manifest completed_items",
    ) != sum(record is not None for record in result):
        raise ValueError("batch manifest completed_items mismatch")
    if batch_status != _status(result):
        raise ValueError("batch manifest batch_status mismatch")
    events = _events_from_bytes(events_data, context="events.jsonl")
    _verify_event_timeline(
        batch_dir,
        request,
        events,
        final_records=result,
        final_status=batch_status,
        require_finished=True,
    )
    return result


def _verify_manifest_file(
    path: Path,
    batch_dir: Path,
    request: BatchRequest,
    *,
    historical: bool,
) -> tuple[tuple[RunRecord | None, ...], str, bytes]:
    payload, fingerprint, data = _manifest_document(path)
    records = _records_from_manifest(
        batch_dir,
        request,
        payload,
        historical=historical,
    )
    return records, fingerprint, data


def _verify_history(batch_dir: Path, request: BatchRequest) -> None:
    history_dir = batch_dir / "history"
    if not history_dir.exists():
        return
    if not history_dir.is_dir():
        raise ValueError("batch history must be a directory")
    for path in sorted(history_dir.iterdir()):
        match = re.fullmatch(r"manifest-([0-9a-f]{64})\.json", path.name)
        if not path.is_file() or match is None:
            raise ValueError("batch history contains an unsupported entry")
        _, fingerprint, _ = _verify_manifest_file(
            path,
            batch_dir,
            request,
            historical=True,
        )
        if fingerprint != match.group(1):
            raise ValueError("batch history filename fingerprint mismatch")


def read_batch(batch_dir: Path) -> BatchRecord:
    """Strictly verify and reconstruct the current completed batch artifact."""

    root = batch_dir.resolve()
    if not root.is_dir():
        raise ValueError("batch directory does not exist")
    request = _load_batch_request(root)
    events_path = root / "events.jsonl"
    if not events_path.is_file():
        raise ValueError("batch events.jsonl is missing")
    _verify_history(root, request)
    records, _, _ = _verify_manifest_file(
        root / "batch_manifest.json",
        root,
        request,
        historical=False,
    )
    selected = _selected_records(root, request)
    if tuple(None if record is None else record.run_dir for record in records) != tuple(
        None if record is None else record.run_dir for record in selected
    ):
        raise ValueError("batch manifest does not select the current run evidence")
    return BatchRecord(root, request, _status(records), records)


def _archive_current_manifest(batch_dir: Path, request: BatchRequest) -> None:
    current = batch_dir / "batch_manifest.json"
    if not current.is_file():
        return
    _, fingerprint, data = _verify_manifest_file(
        current,
        batch_dir,
        request,
        historical=False,
    )
    history_dir = batch_dir / "history"
    history_dir.mkdir(exist_ok=True)
    archived = history_dir / f"manifest-{fingerprint}.json"
    if archived.exists():
        if not archived.is_file() or archived.read_bytes() != data:
            raise ValueError("batch history manifest collision")
        current.unlink()
        return
    current.rename(archived)


def _default_run_one(request: RunRequest, output_root: Path) -> RunRecord:
    from .api import run

    return run(request, output_root=output_root)


def _run_pending(
    batch_dir: Path,
    request: BatchRequest,
    *,
    retry_failed: bool,
    run_one: RunOne,
) -> tuple[RunRecord | None, ...]:
    for index, item in enumerate(request.items):
        attempts = _attempt_dirs(batch_dir, index)
        attempt_records = tuple(
            (_attempt_number(path), _record_in_attempt(path, item)) for path in attempts
        )
        records = tuple(
            (attempt_number, record)
            for attempt_number, record in attempt_records
            if record is not None
        )
        successful = tuple(
            (attempt_number, record)
            for attempt_number, record in records
            if record.payload.runtime_status in _NON_FAILURE_STATUSES
        )
        latest = successful[-1] if successful else (records[-1] if records else None)
        if successful:
            completed_attempt, completed = cast(tuple[int, RunRecord], latest)
            _event(
                batch_dir / "events.jsonl",
                event_type="item_skipped",
                item_index=index,
                attempt_number=completed_attempt,
                runtime_status=completed.payload.runtime_status,
                message="valid completed item retained",
            )
            continue
        latest_attempt_is_incomplete = bool(attempt_records) and attempt_records[-1][1] is None
        if latest is not None and not retry_failed and not latest_attempt_is_incomplete:
            retained_attempt, retained = latest
            _event(
                batch_dir / "events.jsonl",
                event_type="item_skipped",
                item_index=index,
                attempt_number=retained_attempt,
                runtime_status=retained.payload.runtime_status,
                message="failed item retained without retry_failed",
            )
            continue

        attempt_number = (
            max(
                (_attempt_number(path) for path in attempts),
                default=0,
            )
            + 1
        )
        attempt_dir = _safe_child(
            batch_dir,
            f"items/{index:04d}/attempt-{attempt_number:04d}",
        )
        attempt_dir.mkdir(parents=True, exist_ok=False)
        execution_request = _expected_attempt_request(
            item,
            item_index=index,
            attempt_number=attempt_number,
        )
        _write_new(attempt_dir / "request.json", _json_bytes(execution_request.as_dict()))
        _event(
            batch_dir / "events.jsonl",
            event_type="item_started",
            item_index=index,
            attempt_number=attempt_number,
            message="batch item attempt started",
        )
        try:
            record = run_one(execution_request, attempt_dir)
            if not isinstance(record, RunRecord):
                raise TypeError("run_one must return a RunRecord")
            if not record.run_dir.resolve().is_relative_to(attempt_dir.resolve()):
                raise ValueError("run_one returned evidence outside its attempt directory")
            verified = read_run(record.run_dir)
            if verified.request.request_fingerprint != item.request_fingerprint:
                raise ValueError("run_one returned evidence for another request")
            _event(
                batch_dir / "events.jsonl",
                event_type="item_completed",
                item_index=index,
                attempt_number=attempt_number,
                runtime_status=verified.payload.runtime_status,
                message="batch item attempt published a valid run manifest",
            )
        except Exception as exc:  # noqa: BLE001 - per-item failure isolation is required.
            detail = str(exc).strip() or type(exc).__name__
            _event(
                batch_dir / "events.jsonl",
                event_type="item_exception",
                item_index=index,
                attempt_number=attempt_number,
                runtime_status="failed",
                message=f"{type(exc).__name__}: {detail}",
            )
    return _selected_records(batch_dir, request)


def execute_batch(
    request: BatchRequest,
    *,
    output_root: Path,
    retry_failed: bool = False,
    run_one: RunOne | None = None,
) -> BatchRecord:
    """Execute a new ordered batch while isolating every item failure."""

    if not isinstance(request, BatchRequest):
        raise TypeError("request must be a BatchRequest")
    if not isinstance(retry_failed, bool):
        raise TypeError("retry_failed must be boolean")
    batch_dir = (output_root.resolve() / request.batch_id).resolve()
    if not batch_dir.is_relative_to(output_root.resolve()):
        raise ValueError("batch directory leaves output_root")
    batch_dir.mkdir(parents=True, exist_ok=False)
    _write_new(batch_dir / "request.json", _json_bytes(request.as_dict()))
    _write_new(batch_dir / "events.jsonl", b"")
    _event(
        batch_dir / "events.jsonl",
        event_type="batch_started",
        message="ordered batch execution started",
    )
    records = _run_pending(
        batch_dir,
        request,
        retry_failed=retry_failed,
        run_one=_default_run_one if run_one is None else run_one,
    )
    _event(
        batch_dir / "events.jsonl",
        event_type="batch_finished",
        runtime_status=_status(records),
        message="ordered batch execution finished",
    )
    _write_manifest(batch_dir, request, records)
    return read_batch(batch_dir)


def resume_batch(
    batch_dir: Path,
    *,
    retry_failed: bool = False,
    run_one: RunOne | None = None,
) -> BatchRecord:
    """Resume an existing batch without overwriting any prior attempt evidence."""

    if not isinstance(retry_failed, bool):
        raise TypeError("retry_failed must be boolean")
    root = batch_dir.resolve()
    if not root.is_dir():
        raise ValueError("batch directory does not exist")
    request = _load_batch_request(root)
    _validate_items_layout(root, request, require_all_items=False)
    events_path = root / "events.jsonl"
    if not events_path.is_file():
        raise ValueError("batch events.jsonl is missing")
    existing_events = _read_events(events_path)
    _verify_event_timeline(
        root,
        request,
        existing_events,
        require_finished=False,
    )
    _verify_history(root, request)
    if (root / "batch_manifest.json").is_file():
        # The current manifest is the completion marker.  Archive it before
        # appending recovery events so an interruption leaves an unambiguous
        # in-progress batch and never invalidates prior completion evidence.
        read_batch(root)
        _archive_current_manifest(root, request)
        _verify_history(root, request)
    _event(
        events_path,
        event_type="batch_resumed",
        message="ordered batch recovery started",
    )
    records = _run_pending(
        root,
        request,
        retry_failed=retry_failed,
        run_one=_default_run_one if run_one is None else run_one,
    )
    _event(
        root / "events.jsonl",
        event_type="batch_finished",
        runtime_status=_status(records),
        message="ordered batch recovery finished",
    )
    _write_manifest(root, request, records)
    return read_batch(root)


__all__ = [
    "BATCH_MANIFEST_VERSION",
    "BATCH_REQUEST_VERSION",
    "BatchRecord",
    "BatchRequest",
    "execute_batch",
    "read_batch",
    "resume_batch",
]
