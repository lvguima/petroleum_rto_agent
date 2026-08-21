"""Tamper-evident local file repository for immutable R5 strategies."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from ..contracts import CLAIM_SCOPE, RTO_SCHEMA_VERSION, ContractRef
from ..contracts.common import canonical_json_bytes
from .models import (
    StrategyEntryV1,
    StrategyEventType,
    StrategyLifecycleEventV1,
    StrategyQueryV1,
    StrategyRecordV1,
    StrategyReleaseManifestV1,
    StrategyState,
)

_REF: Final[re.Pattern[str]] = re.compile(r"^(?P<strategy>.+)-r(?P<revision>[1-9][0-9]*)$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StrategyRepository:
    """Single-process local repository with immutable payloads and event hash chains."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("strategy repository root must be a pathlib.Path")
        self.root = root
        self.entries_root = root / "entries"
        self.releases_root = root / "releases"

    def create_draft(
        self,
        entry: StrategyEntryV1,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "evidence-complete-offline-draft",
    ) -> StrategyRecordV1:
        if not isinstance(entry, StrategyEntryV1):
            raise TypeError("create_draft requires a StrategyEntryV1")
        with self._lock():
            path = self._entry_path(entry.strategy_id, entry.revision)
            if path.exists():
                existing = self.read(entry.strategy_id, entry.revision)
                if existing.entry != entry:
                    raise FileExistsError("strategy revision already exists with different content")
                return existing
            self._write_immutable(path, canonical_json_bytes(entry.as_dict()))
            event = StrategyLifecycleEventV1(
                schema_version=RTO_SCHEMA_VERSION,
                event_version="strategy-lifecycle-event-v1",
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
            )
            self._append_event(self._events_path(entry.strategy_id, entry.revision), event)
        return self.read(entry.strategy_id, entry.revision)

    def approve(
        self,
        strategy_id: str,
        revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "offline-review-approved",
    ) -> StrategyRecordV1:
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
    ) -> StrategyReleaseManifestV1:
        with self._lock():
            record = self.read(strategy_id, revision)
            if record.current_state == "published" and record.release_ref is not None:
                return self.read_release(record.release_ref.object_id)
            if record.current_state != "approved":
                raise ValueError("only an approved strategy may be published")
            instant = occurred_at or utc_now()
            release_id = f"release-{record.entry.fingerprint[:16]}"
            release = StrategyReleaseManifestV1(
                schema_version=RTO_SCHEMA_VERSION,
                release_version="strategy-library-release-v1",
                release_id=release_id,
                entry_refs=(record.entry.ref,),
                created_by=actor,
                created_at=instant,
                claim_scope=CLAIM_SCOPE,
            )
            self._write_immutable(
                self.releases_root / f"{release.release_id}.json",
                canonical_json_bytes(release.as_dict()),
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
            self._append_event(self._events_path(strategy_id, revision), event)
        verified = self.read(strategy_id, revision)
        if verified.release_ref != release.ref:
            raise RuntimeError("published event does not reference the written release")
        return self.read_release(release.release_id)

    def request_revalidation(
        self,
        strategy_id: str,
        revision: int,
        *,
        actor: str,
        occurred_at: str | None = None,
        reason: str = "dependency-version-changed",
    ) -> StrategyRecordV1:
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
    ) -> StrategyRecordV1:
        with self._lock():
            record = self.read(strategy_id, revision)
            replacement = self.read(replacement_strategy_id, replacement_revision)
            if record.current_state != "pending_revalidation":
                raise ValueError("only pending_revalidation strategy may be superseded")
            if replacement.current_state != "published":
                raise ValueError("replacement strategy must already be published")
            if replacement.entry.supersedes != record.entry.ref:
                raise ValueError("replacement payload does not supersede the old revision")
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
    ) -> StrategyRecordV1:
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

    def read(self, strategy_id: str, revision: int) -> StrategyRecordV1:
        entry_path = self._entry_path(strategy_id, revision)
        entry = StrategyEntryV1.from_mapping(self._read_json(entry_path))
        if entry.strategy_id != strategy_id or entry.revision != revision:
            raise ValueError("strategy path identity differs from immutable entry")
        events = self._read_events(self._events_path(strategy_id, revision))
        return StrategyRecordV1(entry=entry, events=events)

    def read_ref(self, ref: ContractRef) -> StrategyRecordV1:
        match = _REF.fullmatch(ref.object_id)
        if match is None:
            raise ValueError("strategy ref object_id has an invalid revision suffix")
        record = self.read(match.group("strategy"), int(match.group("revision")))
        if record.entry.ref != ref:
            raise ValueError("strategy ref fingerprint differs from repository entry")
        return record

    def read_release(self, release_id: str) -> StrategyReleaseManifestV1:
        release = StrategyReleaseManifestV1.from_mapping(
            self._read_json(self.releases_root / f"{release_id}.json")
        )
        for ref in release.entry_refs:
            record = self.read_ref(ref)
            if not any(
                event.event_type == "published" and event.release_ref == release.ref
                for event in record.events
            ):
                raise ValueError("release is not referenced by the strategy lifecycle")
        return release

    def query(self, query: StrategyQueryV1) -> tuple[StrategyRecordV1, ...]:
        if not isinstance(query, StrategyQueryV1):
            raise TypeError("query requires a StrategyQueryV1")
        matched: list[tuple[float, float, str, StrategyRecordV1]] = []
        if not self.entries_root.exists():
            return ()
        for path in sorted(self.entries_root.glob("*/r*/entry.json")):
            entry = StrategyEntryV1.from_mapping(self._read_json(path))
            record = self.read(entry.strategy_id, entry.revision)
            if record.current_state != "published":
                continue
            if entry.case_ref != query.case_ref or entry.operating_mode != query.operating_mode:
                continue
            if query.required_dependency_refs and not set(query.required_dependency_refs).issubset(
                entry.dependency_refs
            ):
                continue
            anchors = tuple(
                item
                for item in entry.anchors
                if abs(item.feed_mass_flow_kg_s - query.feed_mass_flow_kg_s)
                <= query.measurement_tolerance_kg_s
            )
            if not anchors:
                continue
            anchor = min(
                anchors,
                key=lambda item: abs(item.feed_mass_flow_kg_s - query.feed_mass_flow_kg_s),
            )
            matched.append(
                (
                    -anchor.relative_improvement,
                    -anchor.minimum_normalized_margin,
                    entry.fingerprint,
                    record,
                )
            )
        return tuple(item[3] for item in sorted(matched, key=lambda item: item[:3]))

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
    ) -> StrategyRecordV1:
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
        record: StrategyRecordV1,
        *,
        event_type: StrategyEventType,
        target: StrategyState,
        actor: str,
        occurred_at: str,
        reason: str,
        release_ref: ContractRef | None = None,
        related_strategy_ref: ContractRef | None = None,
    ) -> StrategyLifecycleEventV1:
        return StrategyLifecycleEventV1(
            schema_version=RTO_SCHEMA_VERSION,
            event_version="strategy-lifecycle-event-v1",
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
        )

    def _entry_path(self, strategy_id: str, revision: int) -> Path:
        return self.entries_root / strategy_id / f"r{revision}" / "entry.json"

    def _events_path(self, strategy_id: str, revision: int) -> Path:
        return self.entries_root / strategy_id / f"r{revision}" / "events.jsonl"

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read strict strategy JSON: {path}") from exc
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError(f"strategy JSON must be an object: {path}")
        return cast(dict[str, object], value)

    @staticmethod
    def _read_events(path: Path) -> tuple[StrategyLifecycleEventV1, ...]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"cannot read strategy events: {path}") from exc
        if not lines or any(not line.strip() for line in lines):
            raise ValueError("strategy event log must contain non-empty JSON lines")
        result: list[StrategyLifecycleEventV1] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("strategy event log contains invalid JSON") from exc
            if not isinstance(value, dict):
                raise TypeError("strategy event must be a JSON object")
            result.append(StrategyLifecycleEventV1.from_mapping(cast(dict[str, object], value)))
        return tuple(result)

    @staticmethod
    def _write_immutable(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError(f"immutable file already exists with different bytes: {path}")

    @staticmethod
    def _append_event(path: Path, event: StrategyLifecycleEventV1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write(canonical_json_bytes(event.as_dict()) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".strategy-repository.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("strategy repository is locked by another writer") from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.fsync(descriptor)
            yield
        finally:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)
