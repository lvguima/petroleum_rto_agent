"""Tamper-evident append-only strategy repository."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from .._file_lock import exclusive_file_lock
from ..contracts.common import canonical_json_bytes, identifier, integer
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE
from ..contracts.reference import ContractRef
from .models import (
    STRATEGY_SCHEMA_VERSION,
    StrategyEntry,
    StrategyEventType,
    StrategyLifecycleEvent,
    StrategyQuery,
    StrategyRecord,
    StrategyReleaseManifest,
    StrategyState,
)

_REF: Final[re.Pattern[str]] = re.compile(
    r"^(?P<strategy>[A-Za-z0-9][A-Za-z0-9._-]*)-r(?P<revision>[1-9][0-9]*)$"
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"strict strategy JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _loads(payload: str, *, context: str) -> dict[str, object]:
    try:
        value = json.loads(payload, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot parse strict {context}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"strict {context} must be a JSON object")
    return cast(dict[str, object], value)


class StrategyRepository:
    """Local single-writer repository with immutable payloads and hash-chained events."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("strategy repository root must be pathlib.Path")
        self.root = root.resolve()
        self.entries_root = self.root / "entries"
        self.releases_root = self.root / "releases"

    def create_draft(
        self,
        entry: StrategyEntry,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "evidence-complete-offline-draft",
    ) -> StrategyRecord:
        if not isinstance(entry, StrategyEntry):
            raise TypeError("create_draft requires StrategyEntry")
        with self._lock():
            path = self._entry_path(entry.strategy_id, entry.revision)
            events_path = self._events_path(entry.strategy_id, entry.revision)
            if path.exists() or events_path.exists():
                existing = self.read(entry.strategy_id, entry.revision)
                if existing.entry != entry:
                    raise FileExistsError("strategy revision already exists with different content")
                return existing
            if entry.supersedes is not None:
                prior = self.read(entry.strategy_id, entry.revision - 1)
                if entry.supersedes != prior.entry.ref:
                    raise ValueError("supersedes differs from the stored direct prior revision")
                if prior.current_state != "pending_revalidation":
                    raise ValueError(
                        "prior revision must be pending_revalidation before a new draft"
                    )
            event = StrategyLifecycleEvent(
                schema_version=STRATEGY_SCHEMA_VERSION,
                event_version="strategy-lifecycle-event",
                strategy_ref=entry.ref,
                sequence=0,
                event_type="created",
                from_state=None,
                to_state="draft",
                actor=actor,
                occurred_at=occurred_at or utc_now(),
                reason=reason,
                release_ref=None,
                related_strategy_ref=None,
                previous_event_fingerprint=None,
                claim_scope=ENGINEERING_CLAIM_SCOPE,
            )
            self._write_draft_atomically(path.parent, entry, event)
        return self.read(entry.strategy_id, entry.revision)

    def approve(
        self,
        strategy_id: str,
        revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "offline-human-review-approved",
    ) -> StrategyRecord:
        return self._transition(
            strategy_id,
            revision,
            event_type="approved",
            expected="draft",
            target="approved",
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
        )

    def publish(
        self,
        strategy_id: str,
        revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "offline-library-release",
    ) -> StrategyReleaseManifest:
        with self._lock():
            record = self.read(strategy_id, revision)
            if record.current_state == "published" and record.release_ref is not None:
                return self.read_release(record.release_ref.object_id)
            if record.current_state != "approved":
                raise ValueError("only an approved strategy may be published")
            release_id = f"release-{record.entry.fingerprint[:16]}"
            release_path = self._release_path(release_id)
            if release_path.exists():
                release = self._read_release_payload(release_id)
                if release.entry_refs != (record.entry.ref,) or release.created_by != actor:
                    raise ValueError("orphan release differs from the requested publication")
                if occurred_at is not None and release.created_at != occurred_at:
                    raise ValueError("orphan release timestamp differs from publication request")
                instant = release.created_at
            else:
                instant = occurred_at or utc_now()
                release = StrategyReleaseManifest(
                    schema_version=STRATEGY_SCHEMA_VERSION,
                    release_version="strategy-library-release",
                    release_id=release_id,
                    entry_refs=(record.entry.ref,),
                    created_by=actor,
                    created_at=instant,
                    review_scope="offline-human-review",
                    execution_scope="offline_simulation_only",
                    claim_scope=ENGINEERING_CLAIM_SCOPE,
                )
            event = self._next_event(
                record,
                event_type="published",
                target="published",
                actor=actor,
                occurred_at=instant,
                reason=reason,
                release_ref=release.ref,
            )
            self._write_immutable(
                release_path,
                canonical_json_bytes(release.as_dict()),
            )
            self._append_event(self._events_path(strategy_id, revision), event)
        verified = self.read(strategy_id, revision)
        if verified.release_ref != release.ref:
            raise RuntimeError("published lifecycle differs from immutable release")
        return self.read_release(release.release_id)

    def request_revalidation(
        self,
        strategy_id: str,
        revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "dependency-version-changed",
    ) -> StrategyRecord:
        return self._transition(
            strategy_id,
            revision,
            event_type="revalidation_requested",
            expected="published",
            target="pending_revalidation",
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
        )

    def supersede(
        self,
        strategy_id: str,
        revision: int,
        replacement_strategy_id: str,
        replacement_revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "validated-replacement-published",
    ) -> StrategyRecord:
        with self._lock():
            record = self.read(strategy_id, revision)
            replacement = self.read(replacement_strategy_id, replacement_revision)
            if record.current_state != "pending_revalidation":
                raise ValueError("only a pending_revalidation strategy may be superseded")
            if replacement.current_state != "published":
                raise ValueError("replacement strategy must already be published")
            if (
                replacement.entry.strategy_id != record.entry.strategy_id
                or replacement.entry.revision != record.entry.revision + 1
                or replacement.entry.supersedes != record.entry.ref
            ):
                raise ValueError("replacement is not the published direct next revision")
            event = self._next_event(
                record,
                event_type="superseded",
                target="superseded",
                actor=actor,
                occurred_at=occurred_at or utc_now(),
                reason=reason,
                related_strategy_ref=replacement.entry.ref,
            )
            self._append_event(self._events_path(strategy_id, revision), event)
        return self.read(strategy_id, revision)

    def retire(
        self,
        strategy_id: str,
        revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "offline-strategy-retired",
    ) -> StrategyRecord:
        with self._lock():
            record = self.read(strategy_id, revision)
            if record.current_state not in {
                "draft",
                "approved",
                "published",
                "pending_revalidation",
            }:
                raise ValueError("strategy cannot be retired from its current state")
            event = self._next_event(
                record,
                event_type="retired",
                target="retired",
                actor=actor,
                occurred_at=occurred_at or utc_now(),
                reason=reason,
            )
            self._append_event(self._events_path(strategy_id, revision), event)
        return self.read(strategy_id, revision)

    def read(self, strategy_id: str, revision: int) -> StrategyRecord:
        entry_path = self._entry_path(strategy_id, revision)
        entry = StrategyEntry.from_mapping(self._read_json(entry_path))
        if entry.strategy_id != strategy_id or entry.revision != revision:
            raise ValueError("strategy path identity differs from immutable entry")
        events = self._read_events(self._events_path(strategy_id, revision))
        record = StrategyRecord(entry=entry, events=events)
        if revision > 1:
            prior = self.read(strategy_id, revision - 1)
            if entry.supersedes != prior.entry.ref:
                raise ValueError("strategy revision does not reference its stored predecessor")
        return record

    def read_ref(self, ref: ContractRef) -> StrategyRecord:
        if not isinstance(ref, ContractRef):
            raise TypeError("strategy ref must be ContractRef")
        match = _REF.fullmatch(ref.object_id)
        if match is None:
            raise ValueError("strategy ref object_id has an invalid revision suffix")
        record = self.read(
            match.group("strategy"),
            int(match.group("revision")),
        )
        if record.entry.ref != ref:
            raise ValueError("strategy ref fingerprint differs from repository entry")
        return record

    def read_release(self, release_id: str) -> StrategyReleaseManifest:
        release = self._read_release_payload(release_id)
        for ref in release.entry_refs:
            record = self.read_ref(ref)
            if not any(
                event.event_type == "published" and event.release_ref == release.ref
                for event in record.events
            ):
                raise ValueError("release is not referenced by strategy lifecycle")
        return release

    def query(self, query: StrategyQuery) -> tuple[StrategyRecord, ...]:
        """Return published records matching an explicitly evaluated anchor only."""

        if not isinstance(query, StrategyQuery):
            raise TypeError("query requires StrategyQuery")
        if not self.entries_root.exists():
            return ()
        records: list[StrategyRecord] = []
        for strategy_dir in sorted(self.entries_root.iterdir(), key=lambda item: item.name):
            if not strategy_dir.is_dir():
                continue
            identifier(strategy_dir.name, context="stored strategy_id")
            for revision_dir in sorted(strategy_dir.iterdir(), key=lambda item: item.name):
                match = re.fullmatch(r"r([1-9][0-9]*)", revision_dir.name)
                if match is None or not revision_dir.is_dir():
                    continue
                record = self.read(strategy_dir.name, int(match.group(1)))
                if record.current_state != "published" or not self._matches(query, record.entry):
                    continue
                records.append(record)
        return tuple(
            sorted(
                records,
                key=lambda item: (item.entry.strategy_id, item.entry.revision),
            )
        )

    @staticmethod
    def _matches(query: StrategyQuery, entry: StrategyEntry) -> bool:
        if query.case_ref != entry.case_ref or query.operating_mode != entry.operating_mode:
            return False
        if not set(query.required_dependency_refs).issubset(entry.dependency_refs):
            return False
        for anchor in entry.anchors:
            if set(anchor.applicability_values) != set(query.applicability_values):
                continue
            if all(
                abs(anchor.applicability_values[key] - value) <= query.measurement_tolerances[key]
                for key, value in query.applicability_values.items()
            ):
                return True
        return False

    def _read_release_payload(self, release_id: str) -> StrategyReleaseManifest:
        release = StrategyReleaseManifest.from_mapping(
            self._read_json(self._release_path(release_id))
        )
        if release.release_id != release_id:
            raise ValueError("release path identity differs from immutable release")
        return release

    def _transition(
        self,
        strategy_id: str,
        revision: int,
        *,
        event_type: StrategyEventType,
        expected: StrategyState,
        target: StrategyState,
        actor: str,
        occurred_at: str | None,
        reason: str,
    ) -> StrategyRecord:
        with self._lock():
            record = self.read(strategy_id, revision)
            if record.current_state != expected:
                raise ValueError(f"strategy must be {expected} before {event_type}")
            event = self._next_event(
                record,
                event_type=event_type,
                target=target,
                actor=actor,
                occurred_at=occurred_at or utc_now(),
                reason=reason,
            )
            self._append_event(self._events_path(strategy_id, revision), event)
        return self.read(strategy_id, revision)

    @staticmethod
    def _next_event(
        record: StrategyRecord,
        *,
        event_type: StrategyEventType,
        target: StrategyState,
        actor: str,
        occurred_at: str,
        reason: str,
        release_ref: ContractRef | None = None,
        related_strategy_ref: ContractRef | None = None,
    ) -> StrategyLifecycleEvent:
        event = StrategyLifecycleEvent(
            schema_version=STRATEGY_SCHEMA_VERSION,
            event_version="strategy-lifecycle-event",
            strategy_ref=record.entry.ref,
            sequence=len(record.events),
            event_type=event_type,
            from_state=record.current_state,
            to_state=target,
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
            release_ref=release_ref,
            related_strategy_ref=related_strategy_ref,
            previous_event_fingerprint=record.events[-1].fingerprint,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )
        StrategyRecord(entry=record.entry, events=(*record.events, event))
        return event

    def _entry_path(self, strategy_id: str, revision: int) -> Path:
        strategy = identifier(strategy_id, context="strategy_id")
        revision_value = integer(revision, context="revision", minimum=1)
        return self._safe_path(self.entries_root / strategy / f"r{revision_value}" / "entry.json")

    def _events_path(self, strategy_id: str, revision: int) -> Path:
        return self._entry_path(strategy_id, revision).with_name("events.jsonl")

    def _release_path(self, release_id: str) -> Path:
        release = identifier(release_id, context="release_id")
        return self._safe_path(self.releases_root / f"{release}.json")

    def _safe_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("strategy path escapes the repository root")
        return resolved

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            payload = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read strict strategy JSON: {path}") from exc
        return _loads(payload, context="strategy JSON")

    @staticmethod
    def _read_events(path: Path) -> tuple[StrategyLifecycleEvent, ...]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"cannot read strategy events: {path}") from exc
        if not lines or any(not line.strip() for line in lines):
            raise ValueError("strategy event log must contain non-empty JSON lines")
        return tuple(
            StrategyLifecycleEvent.from_mapping(_loads(line, context="strategy lifecycle event"))
            for line in lines
        )

    @staticmethod
    def _write_immutable(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise FileExistsError(
                        f"immutable file already exists with different bytes: {path}"
                    )
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _append_event(path: Path, event: StrategyLifecycleEvent) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            prior = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read strategy event log before append: {path}") from exc
        if not prior or not prior.endswith(b"\n"):
            raise ValueError("strategy event log has an incomplete final line")
        payload = prior + canonical_json_bytes(event.as_dict()) + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_draft_atomically(
        target: Path,
        entry: StrategyEntry,
        event: StrategyLifecycleEvent,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
        )
        entry_path = temporary / "entry.json"
        events_path = temporary / "events.jsonl"
        try:
            for path, payload in (
                (entry_path, canonical_json_bytes(entry.as_dict())),
                (events_path, canonical_json_bytes(event.as_dict()) + b"\n"),
            ):
                with path.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.rename(temporary, target)
        finally:
            entry_path.unlink(missing_ok=True)
            events_path.unlink(missing_ok=True)
            try:
                temporary.rmdir()
            except FileNotFoundError:
                pass

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with exclusive_file_lock(
            self.root / ".strategy-repository.lock",
            label="strategy repository",
        ):
            yield


__all__ = ["StrategyRepository", "utc_now"]
