"""Immutable, strict and deterministic M7 runtime data contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from types import MappingProxyType
from typing import Final, Literal, cast

RUNTIME_SCHEMA_VERSION: Final[str] = "1.0.0"
RUNTIME_VERSION: Final[str] = "cdu-mini-runtime-v0.1.0"
RUN_REQUEST_VERSION: Final[str] = "cdu-mini-run-request-v0.1.0"
CUSTOM_INPUT_VERSION: Final[str] = "cdu-mini-custom-input-v0.1.0"

RunType = Literal[
    "steady_recycle",
    "open_loop_dynamic",
    "closed_loop_dynamic",
    "validation_scenario",
]
RuntimeStatus = Literal[
    "success",
    "limited",
    "rejected",
    "failed",
    "not_converged",
]
EventValueBasis = Literal["absolute", "nominal_ratio", "setpoint_ratio"]

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Mapping[str, JsonValue] | tuple[JsonValue, ...]

_RUN_TYPES: Final[frozenset[str]] = frozenset(
    {
        "steady_recycle",
        "open_loop_dynamic",
        "closed_loop_dynamic",
        "validation_scenario",
    }
)
_RUNTIME_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "limited", "rejected", "failed", "not_converged"}
)
_EVENT_VALUE_BASES: Final[frozenset[str]] = frozenset(
    {"absolute", "nominal_ratio", "setpoint_ratio"}
)
_NON_FAILURE_STATUSES: Final[frozenset[str]] = frozenset({"success", "limited"})
_DOMAIN_STATUSES: Final[frozenset[str]] = frozenset(
    {"not_applicable", "passed", "limited", "rejected"}
)
_DATA_ORIGIN_BY_RUN_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "steady_recycle": "M2_steady_model_prediction",
        "open_loop_dynamic": "M3_open_loop_simulation",
        "closed_loop_dynamic": "M4_closed_loop_simulation",
        "validation_scenario": "M6_synthetic_validation",
    }
)
_CLAIM_SCOPE_BY_RUN_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "steady_recycle": "engineering_simulation_only",
        "open_loop_dynamic": "engineering_simulation_only",
        "closed_loop_dynamic": "engineering_simulation_only",
        "validation_scenario": "engineering_validation_only",
    }
)
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KEY: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


def _strict_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    context: str,
) -> None:
    allowed = required | (set() if optional is None else optional)
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise ValueError(f"{context} fields differ; missing={missing}, unknown={unknown}")


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be a sequence")
    return cast(Sequence[object], value)


def _safe_identifier(value: object, *, context: str) -> str:
    if isinstance(value, str) and (".." in value or "/" in value or "\\" in value):
        raise ValueError(f"{context} must not contain a path traversal")
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def _safe_key(value: object, *, context: str) -> str:
    if isinstance(value, str) and (".." in value or "/" in value or "\\" in value):
        raise ValueError(f"{context} must not contain a path traversal")
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError(f"{context} must be a supported key")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _optional_text(value: object, *, context: str) -> str | None:
    if value is None:
        return None
    return _text(value, context=context)


def _finite_number(
    value: object,
    *,
    context: str,
    minimum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be numeric and must not be boolean")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    if strictly_positive and number <= 0.0:
        raise ValueError(f"{context} must be positive")
    if minimum is not None and number < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return number


def _optional_time(value: object, *, context: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, context=context, minimum=0.0)


def _integer(value: object, *, context: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer and must not be boolean")
    if value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _validate_timestamp(value: str | None, *, context: str) -> str | None:
    if value is None:
        return None
    text = _text(value, context=context)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include a UTC offset")
    return text


def _freeze_json(value: object, *, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{context} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError(f"{context} object keys must be strings")
            frozen[raw_key] = _freeze_json(item, context=f"{context}.{raw_key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            _freeze_json(item, context=f"{context}[{index}]") for index, item in enumerate(value)
        )
    raise TypeError(f"{context} must contain only JSON-compatible values")


def _freeze_json_mapping(value: object, *, context: str) -> Mapping[str, JsonValue]:
    raw = _mapping(value, context=context)
    frozen = _freeze_json(raw, context=context)
    if not isinstance(frozen, Mapping):  # pragma: no cover - raw is a mapping
        raise TypeError("JSON mapping did not remain a mapping")
    return frozen


def _thaw_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(dict(value))).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _numeric_mapping(value: object, *, context: str) -> Mapping[str, float]:
    raw = _mapping(value, context=context)
    copied: dict[str, float] = {}
    for key, item in raw.items():
        safe_key = _safe_key(key, context=f"{context} key")
        copied[safe_key] = _finite_number(item, context=f"{context}.{safe_key}")
    return MappingProxyType(copied)


def _string_mapping(value: object, *, context: str) -> Mapping[str, str]:
    raw = _mapping(value, context=context)
    copied: dict[str, str] = {}
    for key, item in raw.items():
        safe_key = _safe_key(key, context=f"{context} key")
        copied[safe_key] = _text(item, context=f"{context}.{safe_key}")
    return MappingProxyType(copied)


@dataclass(frozen=True)
class RuntimeInputEvent:
    """One user-facing dynamic input event before model-specific resolution."""

    time_s: float
    target: str
    value: float
    value_basis: EventValueBasis = "absolute"
    duration_s: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "time_s",
            _finite_number(self.time_s, context="runtime event time_s", minimum=0.0),
        )
        object.__setattr__(
            self,
            "target",
            _safe_key(self.target, context="runtime event target"),
        )
        object.__setattr__(
            self,
            "value",
            _finite_number(self.value, context="runtime event value"),
        )
        if self.value_basis not in _EVENT_VALUE_BASES:
            raise ValueError(f"unsupported runtime event value_basis: {self.value_basis!r}")
        if self.duration_s is not None:
            object.__setattr__(
                self,
                "duration_s",
                _finite_number(
                    self.duration_s,
                    context="runtime event duration_s",
                    strictly_positive=True,
                ),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RuntimeInputEvent:
        _strict_keys(
            value,
            required={"time_s", "target", "value"},
            optional={"value_basis", "duration_s"},
            context="runtime input event",
        )
        raw_basis = value.get("value_basis", "absolute")
        if not isinstance(raw_basis, str) or raw_basis not in _EVENT_VALUE_BASES:
            raise ValueError(f"unsupported runtime event value_basis: {raw_basis!r}")
        return cls(
            time_s=_finite_number(value["time_s"], context="event time_s", minimum=0.0),
            target=_safe_key(value["target"], context="event target"),
            value=_finite_number(value["value"], context="event value"),
            value_basis=cast(EventValueBasis, raw_basis),
            duration_s=(
                None
                if value.get("duration_s") is None
                else _finite_number(
                    value["duration_s"],
                    context="event duration_s",
                    strictly_positive=True,
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "time_s": self.time_s,
            "target": self.target,
            "value": self.value,
            "value_basis": self.value_basis,
            "duration_s": self.duration_s,
        }


@dataclass(frozen=True)
class RuntimeScenarioRequest:
    """Optional overrides for a packaged dynamic scenario template."""

    duration_s: float | None = None
    time_step_s: float | None = None
    events: tuple[RuntimeInputEvent, ...] | None = None

    def __post_init__(self) -> None:
        if self.duration_s is not None:
            object.__setattr__(
                self,
                "duration_s",
                _finite_number(
                    self.duration_s,
                    context="runtime scenario duration_s",
                    strictly_positive=True,
                ),
            )
        if self.time_step_s is not None:
            object.__setattr__(
                self,
                "time_step_s",
                _finite_number(
                    self.time_step_s,
                    context="runtime scenario time_step_s",
                    strictly_positive=True,
                ),
            )
        if self.events is not None:
            events = tuple(self.events)
            if any(not isinstance(event, RuntimeInputEvent) for event in events):
                raise TypeError("runtime scenario events must be RuntimeInputEvent objects")
            if any(later.time_s < earlier.time_s for earlier, later in pairwise(events)):
                raise ValueError("runtime scenario events must be ordered by time")
            if self.duration_s is not None and any(
                event.time_s > self.duration_s for event in events
            ):
                raise ValueError("runtime scenario event exceeds requested duration")
            object.__setattr__(self, "events", events)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RuntimeScenarioRequest:
        _strict_keys(
            value,
            required=set(),
            optional={"duration_s", "time_step_s", "events"},
            context="runtime scenario request",
        )
        raw_events = value.get("events")
        events: tuple[RuntimeInputEvent, ...] | None
        if raw_events is None:
            events = None
        else:
            events = tuple(
                RuntimeInputEvent.from_mapping(
                    _mapping(item, context=f"runtime scenario event {index}")
                )
                for index, item in enumerate(
                    _sequence(raw_events, context="runtime scenario events")
                )
            )
        return cls(
            duration_s=(
                None
                if value.get("duration_s") is None
                else _finite_number(
                    value["duration_s"],
                    context="scenario duration_s",
                    strictly_positive=True,
                )
            ),
            time_step_s=(
                None
                if value.get("time_step_s") is None
                else _finite_number(
                    value["time_step_s"],
                    context="scenario time_step_s",
                    strictly_positive=True,
                )
            ),
            events=events,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "events": (None if self.events is None else [event.as_dict() for event in self.events]),
        }


@dataclass(frozen=True)
class RunRequest:
    """One semantic run request plus optional non-semantic execution identity."""

    schema_version: str
    request_version: str
    preset_id: str
    run_type: RunType
    random_seed: int
    parameters: Mapping[str, float]
    overrides: Mapping[str, float]
    metadata: Mapping[str, str]
    run_id: str | None = None
    requested_at_utc: str | None = None
    scenario: RuntimeScenarioRequest | None = None
    initial_state: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ValueError("run request schema_version differs from the runtime contract")
        if self.request_version != RUN_REQUEST_VERSION:
            raise ValueError("run request request_version differs from the runtime contract")
        object.__setattr__(
            self,
            "preset_id",
            _safe_identifier(self.preset_id, context="run request preset_id"),
        )
        if self.run_type not in _RUN_TYPES:
            raise ValueError(f"unsupported run_type: {self.run_type!r}")
        object.__setattr__(self, "run_type", self.run_type)
        object.__setattr__(
            self,
            "random_seed",
            _integer(self.random_seed, context="run request random_seed"),
        )
        object.__setattr__(
            self,
            "parameters",
            _numeric_mapping(self.parameters, context="run request parameters"),
        )
        object.__setattr__(
            self,
            "overrides",
            _numeric_mapping(self.overrides, context="run request overrides"),
        )
        object.__setattr__(
            self,
            "metadata",
            _string_mapping(self.metadata, context="run request metadata"),
        )
        if self.run_id is not None:
            object.__setattr__(
                self,
                "run_id",
                _safe_identifier(self.run_id, context="run request run_id"),
            )
        object.__setattr__(
            self,
            "requested_at_utc",
            _validate_timestamp(
                self.requested_at_utc,
                context="run request requested_at_utc",
            ),
        )
        if self.scenario is not None and not isinstance(
            self.scenario,
            RuntimeScenarioRequest,
        ):
            raise TypeError("run request scenario must be RuntimeScenarioRequest or None")
        object.__setattr__(
            self,
            "initial_state",
            _numeric_mapping(self.initial_state, context="run request initial_state"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RunRequest:
        _strict_keys(
            value,
            required={
                "schema_version",
                "request_version",
                "preset_id",
                "run_type",
                "random_seed",
                "parameters",
                "overrides",
                "metadata",
            },
            optional={
                "run_id",
                "requested_at_utc",
                "scenario",
                "initial_state",
                "request_fingerprint",
            },
            context="run request",
        )
        run_type = value["run_type"]
        if not isinstance(run_type, str) or run_type not in _RUN_TYPES:
            raise ValueError(f"unsupported run_type: {run_type!r}")
        request = cls(
            schema_version=_text(value["schema_version"], context="schema_version"),
            request_version=_text(value["request_version"], context="request_version"),
            preset_id=_safe_identifier(value["preset_id"], context="preset_id"),
            run_type=cast(RunType, run_type),
            random_seed=_integer(value["random_seed"], context="random_seed"),
            parameters=_numeric_mapping(value["parameters"], context="parameters"),
            overrides=_numeric_mapping(value["overrides"], context="overrides"),
            metadata=_string_mapping(value["metadata"], context="metadata"),
            run_id=(
                None
                if value.get("run_id") is None
                else _safe_identifier(value["run_id"], context="run_id")
            ),
            requested_at_utc=_optional_text(
                value.get("requested_at_utc"),
                context="requested_at_utc",
            ),
            scenario=(
                None
                if value.get("scenario") is None
                else RuntimeScenarioRequest.from_mapping(
                    _mapping(value["scenario"], context="scenario")
                )
            ),
            initial_state=_numeric_mapping(
                value.get("initial_state", {}),
                context="initial_state",
            ),
        )
        supplied_fingerprint = value.get("request_fingerprint")
        if (
            supplied_fingerprint is not None
            and _digest(
                supplied_fingerprint,
                context="request_fingerprint",
            )
            != request.request_fingerprint
        ):
            raise ValueError("run request fingerprint mismatch")
        return request

    def fingerprint_payload(self) -> dict[str, object]:
        """Return only semantic fields; execution identity and wall clock are excluded."""

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "preset_id": self.preset_id,
            "run_type": self.run_type,
            "random_seed": self.random_seed,
            "parameters": dict(self.parameters),
            "overrides": dict(self.overrides),
            "metadata": dict(self.metadata),
        }
        if self.scenario is not None:
            payload["scenario"] = self.scenario.as_dict()
        if self.initial_state:
            payload["initial_state"] = dict(self.initial_state)
        return payload

    @property
    def request_fingerprint(self) -> str:
        return _canonical_fingerprint(self.fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "run_id": self.run_id,
            "requested_at_utc": self.requested_at_utc,
            "request_fingerprint": self.request_fingerprint,
        }


@dataclass(frozen=True)
class EventRecord:
    """One ordered model, protection or runtime event."""

    sequence: int
    time_s: float | None
    event_type: str
    source: str
    stage: str
    message: str
    details: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _integer(self.sequence, context="event sequence"))
        object.__setattr__(self, "time_s", _optional_time(self.time_s, context="event time_s"))
        for name in ("event_type", "source", "stage"):
            object.__setattr__(
                self,
                name,
                _safe_identifier(getattr(self, name), context=f"event {name}"),
            )
        object.__setattr__(self, "message", _text(self.message, context="event message"))
        object.__setattr__(
            self,
            "details",
            _freeze_json_mapping(self.details, context="event details"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EventRecord:
        _strict_keys(
            value,
            required={
                "sequence",
                "time_s",
                "event_type",
                "source",
                "stage",
                "message",
                "details",
            },
            context="event record",
        )
        return cls(
            sequence=_integer(value["sequence"], context="event sequence"),
            time_s=_optional_time(value["time_s"], context="event time_s"),
            event_type=_safe_identifier(value["event_type"], context="event_type"),
            source=_safe_identifier(value["source"], context="event source"),
            stage=_safe_identifier(value["stage"], context="event stage"),
            message=_text(value["message"], context="event message"),
            details=_freeze_json_mapping(value["details"], context="event details"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "time_s": self.time_s,
            "event_type": self.event_type,
            "source": self.source,
            "stage": self.stage,
            "message": self.message,
            "details": _thaw_json(cast(JsonValue, self.details)),
        }


@dataclass(frozen=True)
class ErrorRecord:
    """One ordered failure with retry and last-valid evidence."""

    sequence: int
    error_type: str
    stage: str
    message: str
    time_s: float | None
    last_valid: Mapping[str, JsonValue] | None
    retryable: bool
    details: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _integer(self.sequence, context="error sequence"))
        object.__setattr__(
            self,
            "error_type",
            _safe_identifier(self.error_type, context="error_type"),
        )
        object.__setattr__(self, "stage", _safe_identifier(self.stage, context="error stage"))
        object.__setattr__(self, "message", _text(self.message, context="error message"))
        object.__setattr__(self, "time_s", _optional_time(self.time_s, context="error time_s"))
        if self.last_valid is not None:
            object.__setattr__(
                self,
                "last_valid",
                _freeze_json_mapping(self.last_valid, context="error last_valid"),
            )
        if not isinstance(self.retryable, bool):
            raise TypeError("error retryable must be boolean")
        object.__setattr__(
            self,
            "details",
            _freeze_json_mapping(self.details, context="error details"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ErrorRecord:
        _strict_keys(
            value,
            required={
                "sequence",
                "error_type",
                "stage",
                "message",
                "time_s",
                "last_valid",
                "retryable",
                "details",
            },
            context="error record",
        )
        retryable = value["retryable"]
        if not isinstance(retryable, bool):
            raise TypeError("error retryable must be boolean")
        raw_last_valid = value["last_valid"]
        return cls(
            sequence=_integer(value["sequence"], context="error sequence"),
            error_type=_safe_identifier(value["error_type"], context="error_type"),
            stage=_safe_identifier(value["stage"], context="error stage"),
            message=_text(value["message"], context="error message"),
            time_s=_optional_time(value["time_s"], context="error time_s"),
            last_valid=(
                None
                if raw_last_valid is None
                else _freeze_json_mapping(raw_last_valid, context="error last_valid")
            ),
            retryable=retryable,
            details=_freeze_json_mapping(value["details"], context="error details"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "error_type": self.error_type,
            "stage": self.stage,
            "message": self.message,
            "time_s": self.time_s,
            "last_valid": (
                None if self.last_valid is None else _thaw_json(cast(JsonValue, self.last_valid))
            ),
            "retryable": self.retryable,
            "details": _thaw_json(cast(JsonValue, self.details)),
        }


FailureRecord = ErrorRecord


@dataclass(frozen=True)
class ExecutionPayload:
    """Pure deterministic outcome before run-directory and wall-clock packaging."""

    schema_version: str
    runtime_version: str
    preset_id: str
    run_type: RunType
    runtime_status: RuntimeStatus
    request_fingerprint: str
    engine_status: str
    raw_result_type: str
    summary: Mapping[str, JsonValue]
    timeseries: tuple[Mapping[str, JsonValue], ...]
    events: tuple[EventRecord, ...]
    errors: tuple[ErrorRecord, ...]
    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    effective_input_fingerprint: str
    synthetic: bool
    data_origin: str
    claim_scope: str
    failure_stage: str | None
    failure_reason: str | None
    failure_time_s: float | None
    last_valid: Mapping[str, JsonValue] | None
    duration_s: float | None
    time_step_s: float | None
    diagnostics: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ValueError("execution schema_version differs from the runtime contract")
        if self.runtime_version != RUNTIME_VERSION:
            raise ValueError("execution runtime_version differs from the runtime contract")
        object.__setattr__(
            self,
            "preset_id",
            _safe_identifier(self.preset_id, context="execution preset_id"),
        )
        if self.run_type not in _RUN_TYPES:
            raise ValueError(f"unsupported run_type: {self.run_type!r}")
        object.__setattr__(self, "run_type", self.run_type)
        if self.runtime_status not in _RUNTIME_STATUSES:
            raise ValueError(f"unsupported runtime_status: {self.runtime_status!r}")
        object.__setattr__(
            self,
            "runtime_status",
            self.runtime_status,
        )
        object.__setattr__(
            self,
            "request_fingerprint",
            _digest(self.request_fingerprint, context="request_fingerprint"),
        )
        object.__setattr__(
            self,
            "engine_status",
            _safe_identifier(self.engine_status, context="engine_status"),
        )
        object.__setattr__(
            self,
            "raw_result_type",
            _safe_identifier(self.raw_result_type, context="raw_result_type"),
        )
        object.__setattr__(
            self,
            "summary",
            _freeze_json_mapping(self.summary, context="execution summary"),
        )
        frozen_timeseries = tuple(
            _freeze_json_mapping(sample, context=f"execution timeseries[{index}]")
            for index, sample in enumerate(self.timeseries)
        )
        object.__setattr__(self, "timeseries", frozen_timeseries)
        if any(not isinstance(event, EventRecord) for event in self.events):
            raise TypeError("execution events must contain EventRecord values")
        if any(not isinstance(error, ErrorRecord) for error in self.errors):
            raise TypeError("execution errors must contain ErrorRecord values")
        events = tuple(self.events)
        errors = tuple(self.errors)
        if tuple(event.sequence for event in events) != tuple(range(len(events))):
            raise ValueError("execution event sequences must be contiguous from zero")
        if tuple(error.sequence for error in errors) != tuple(range(len(errors))):
            raise ValueError("execution error sequences must be contiguous from zero")
        timed_events = tuple(event.time_s for event in events if event.time_s is not None)
        if any(later < earlier for earlier, later in pairwise(timed_events)):
            raise ValueError("execution event times must be nondecreasing")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "errors", errors)
        object.__setattr__(
            self,
            "versions",
            _string_mapping(self.versions, context="execution versions"),
        )
        fingerprints = _mapping(
            self.source_fingerprints,
            context="execution source_fingerprints",
        )
        object.__setattr__(
            self,
            "source_fingerprints",
            MappingProxyType(
                {
                    _safe_key(key, context="source fingerprint key"): _digest(
                        value,
                        context=f"source_fingerprints.{key}",
                    )
                    for key, value in fingerprints.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "effective_input_fingerprint",
            _digest(
                self.effective_input_fingerprint,
                context="effective_input_fingerprint",
            ),
        )
        if not isinstance(self.synthetic, bool):
            raise TypeError("execution synthetic must be boolean")
        object.__setattr__(
            self,
            "data_origin",
            _safe_identifier(self.data_origin, context="data_origin"),
        )
        object.__setattr__(
            self,
            "claim_scope",
            _safe_identifier(self.claim_scope, context="claim_scope"),
        )
        if (
            not self.synthetic
            or self.data_origin != _DATA_ORIGIN_BY_RUN_TYPE[self.run_type]
            or self.claim_scope != _CLAIM_SCOPE_BY_RUN_TYPE[self.run_type]
        ):
            raise ValueError("execution source contract differs from the fixed synthetic runtime")
        failure_stage = (
            None
            if self.failure_stage is None
            else _safe_identifier(self.failure_stage, context="failure_stage")
        )
        failure_reason = _optional_text(self.failure_reason, context="failure_reason")
        failure_time_s = _optional_time(self.failure_time_s, context="failure_time_s")
        object.__setattr__(self, "failure_stage", failure_stage)
        object.__setattr__(self, "failure_reason", failure_reason)
        object.__setattr__(self, "failure_time_s", failure_time_s)
        if self.last_valid is not None:
            object.__setattr__(
                self,
                "last_valid",
                _freeze_json_mapping(self.last_valid, context="execution last_valid"),
            )
        duration_s = (
            None
            if self.duration_s is None
            else _finite_number(self.duration_s, context="duration_s", strictly_positive=True)
        )
        time_step_s = (
            None
            if self.time_step_s is None
            else _finite_number(
                self.time_step_s,
                context="time_step_s",
                strictly_positive=True,
            )
        )
        if (duration_s is None) != (time_step_s is None):
            raise ValueError("duration_s and time_step_s must either both be set or both be null")
        if duration_s is not None and time_step_s is not None and time_step_s > duration_s:
            raise ValueError("time_step_s cannot exceed duration_s")
        if self.run_type in {"open_loop_dynamic", "closed_loop_dynamic"} and duration_s is None:
            raise ValueError("dynamic execution must record duration_s and time_step_s")
        if self.run_type == "steady_recycle" and duration_s is not None:
            raise ValueError("steady execution must not record dynamic duration")
        object.__setattr__(self, "duration_s", duration_s)
        object.__setattr__(self, "time_step_s", time_step_s)
        object.__setattr__(
            self,
            "diagnostics",
            _freeze_json_mapping(self.diagnostics, context="execution diagnostics"),
        )
        domain_status = self.diagnostics.get("domain_status", "not_applicable")
        if not isinstance(domain_status, str) or domain_status not in _DOMAIN_STATUSES:
            raise ValueError("execution diagnostics domain_status is invalid")
        if self.runtime_status in _NON_FAILURE_STATUSES:
            if errors:
                raise ValueError("successful or limited execution cannot contain errors")
            if any(
                event.event_type in {"execution_failure", "structural_rejection"}
                for event in events
            ):
                raise ValueError("successful or limited execution cannot contain failure events")
            if any(
                item is not None
                for item in (failure_stage, failure_reason, failure_time_s, self.last_valid)
            ):
                raise ValueError("successful or limited execution cannot contain failure fields")
            if self.engine_status != "success":
                raise ValueError("successful or limited execution requires engine_status='success'")
            if self.runtime_status == "success" and domain_status not in {
                "not_applicable",
                "passed",
            }:
                raise ValueError("successful execution cannot have a limited or rejected domain")
            if self.runtime_status == "limited" and domain_status != "limited":
                raise ValueError("limited execution requires domain_status='limited'")
        else:
            if self.engine_status == "success":
                raise ValueError(
                    "failed, rejected or not-converged execution cannot have a successful engine"
                )
            if self.runtime_status == "rejected" and self.engine_status not in {
                "exception",
                "not_called",
            }:
                raise ValueError(
                    "rejected execution requires an uncalled or exception engine status"
                )
            if self.runtime_status == "rejected" and domain_status not in {
                "not_applicable",
                "rejected",
            }:
                raise ValueError("rejected execution has an inconsistent domain_status")
            if not errors:
                raise ValueError("failed, rejected or not-converged execution requires an error")
            if failure_stage is None or failure_reason is None:
                raise ValueError("failed execution requires failure_stage and failure_reason")
            first_error = errors[0]
            if (
                first_error.stage != failure_stage
                or first_error.message != failure_reason
                or first_error.time_s != failure_time_s
                or first_error.last_valid != self.last_valid
            ):
                raise ValueError("failure fields differ from the first error record")
            if not events:
                raise ValueError("failed execution requires a terminal failure event")
            if failure_time_s is not None and any(
                event.time_s is not None and event.time_s > failure_time_s for event in events
            ):
                raise ValueError("execution events cannot occur after failure_time_s")
            terminal_event = events[-1]
            allowed_terminal_event_types = (
                {"execution_failure", "structural_rejection"}
                if self.runtime_status == "rejected"
                else {"execution_failure"}
            )
            if (
                terminal_event.event_type not in allowed_terminal_event_types
                or terminal_event.stage != failure_stage
                or terminal_event.message != failure_reason
                or terminal_event.time_s != failure_time_s
            ):
                raise ValueError("failure fields differ from the terminal failure event")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runtime_version": self.runtime_version,
            "preset_id": self.preset_id,
            "run_type": self.run_type,
            "runtime_status": self.runtime_status,
            "request_fingerprint": self.request_fingerprint,
            "engine_status": self.engine_status,
            "raw_result_type": self.raw_result_type,
            "summary": _thaw_json(cast(JsonValue, self.summary)),
            "timeseries": [_thaw_json(cast(JsonValue, sample)) for sample in self.timeseries],
            "events": [event.as_dict() for event in self.events],
            "errors": [error.as_dict() for error in self.errors],
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "synthetic": self.synthetic,
            "data_origin": self.data_origin,
            "claim_scope": self.claim_scope,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "failure_time_s": self.failure_time_s,
            "last_valid": (
                None if self.last_valid is None else _thaw_json(cast(JsonValue, self.last_valid))
            ),
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "diagnostics": _thaw_json(cast(JsonValue, self.diagnostics)),
        }

    @property
    def result_fingerprint(self) -> str:
        fields: dict[str, object] = {
            "schema_version": self.schema_version,
            "runtime_version": self.runtime_version,
            "preset_id": self.preset_id,
            "run_type": self.run_type,
            "runtime_status": self.runtime_status,
            "request_fingerprint": self.request_fingerprint,
            "engine_status": self.engine_status,
            "raw_result_type": self.raw_result_type,
            "summary": _thaw_json(cast(JsonValue, self.summary)),
            "events": [event.as_dict() for event in self.events],
            "errors": [error.as_dict() for error in self.errors],
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "synthetic": self.synthetic,
            "data_origin": self.data_origin,
            "claim_scope": self.claim_scope,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "failure_time_s": self.failure_time_s,
            "last_valid": (
                None if self.last_valid is None else _thaw_json(cast(JsonValue, self.last_valid))
            ),
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "diagnostics": _thaw_json(cast(JsonValue, self.diagnostics)),
        }
        digest = hashlib.sha256()
        digest.update(b"{")
        keys = sorted((*fields, "timeseries"))
        for key_index, key in enumerate(keys):
            if key_index:
                digest.update(b",")
            digest.update(_canonical_json_bytes(key))
            digest.update(b":")
            if key != "timeseries":
                digest.update(_canonical_json_bytes(fields[key]))
                continue
            digest.update(b"[")
            for sample_index, sample in enumerate(self.timeseries):
                if sample_index:
                    digest.update(b",")
                digest.update(_canonical_json_bytes(_thaw_json(cast(JsonValue, sample))))
            digest.update(b"]")
        digest.update(b"}")
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "result_fingerprint": self.result_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ExecutionPayload:
        fields = {
            "schema_version",
            "runtime_version",
            "preset_id",
            "run_type",
            "runtime_status",
            "request_fingerprint",
            "engine_status",
            "raw_result_type",
            "summary",
            "timeseries",
            "events",
            "errors",
            "versions",
            "source_fingerprints",
            "effective_input_fingerprint",
            "synthetic",
            "data_origin",
            "claim_scope",
            "failure_stage",
            "failure_reason",
            "failure_time_s",
            "last_valid",
            "duration_s",
            "time_step_s",
            "diagnostics",
        }
        _strict_keys(
            value,
            required=fields,
            optional={"result_fingerprint"},
            context="execution payload",
        )
        run_type = value["run_type"]
        runtime_status = value["runtime_status"]
        if not isinstance(run_type, str) or run_type not in _RUN_TYPES:
            raise ValueError(f"unsupported run_type: {run_type!r}")
        if not isinstance(runtime_status, str) or runtime_status not in _RUNTIME_STATUSES:
            raise ValueError(f"unsupported runtime_status: {runtime_status!r}")
        raw_events = _sequence(value["events"], context="execution events")
        raw_errors = _sequence(value["errors"], context="execution errors")
        raw_timeseries = _sequence(value["timeseries"], context="execution timeseries")
        synthetic = value["synthetic"]
        if not isinstance(synthetic, bool):
            raise TypeError("execution synthetic must be boolean")
        raw_last_valid = value["last_valid"]
        payload = cls(
            schema_version=_text(value["schema_version"], context="schema_version"),
            runtime_version=_text(value["runtime_version"], context="runtime_version"),
            preset_id=_safe_identifier(value["preset_id"], context="preset_id"),
            run_type=cast(RunType, run_type),
            runtime_status=cast(RuntimeStatus, runtime_status),
            request_fingerprint=_digest(
                value["request_fingerprint"],
                context="request_fingerprint",
            ),
            engine_status=_safe_identifier(value["engine_status"], context="engine_status"),
            raw_result_type=_safe_identifier(
                value["raw_result_type"],
                context="raw_result_type",
            ),
            summary=_freeze_json_mapping(value["summary"], context="summary"),
            timeseries=cast(
                tuple[Mapping[str, JsonValue], ...],
                raw_timeseries,
            ),
            events=tuple(
                EventRecord.from_mapping(_mapping(item, context=f"events[{index}]"))
                for index, item in enumerate(raw_events)
            ),
            errors=tuple(
                ErrorRecord.from_mapping(_mapping(item, context=f"errors[{index}]"))
                for index, item in enumerate(raw_errors)
            ),
            versions=_string_mapping(value["versions"], context="versions"),
            source_fingerprints=cast(
                Mapping[str, str],
                _mapping(value["source_fingerprints"], context="source_fingerprints"),
            ),
            effective_input_fingerprint=_digest(
                value["effective_input_fingerprint"],
                context="effective_input_fingerprint",
            ),
            synthetic=synthetic,
            data_origin=_safe_identifier(value["data_origin"], context="data_origin"),
            claim_scope=_safe_identifier(value["claim_scope"], context="claim_scope"),
            failure_stage=(
                None
                if value["failure_stage"] is None
                else _safe_identifier(value["failure_stage"], context="failure_stage")
            ),
            failure_reason=_optional_text(value["failure_reason"], context="failure_reason"),
            failure_time_s=_optional_time(value["failure_time_s"], context="failure_time_s"),
            last_valid=(
                None
                if raw_last_valid is None
                else _freeze_json_mapping(raw_last_valid, context="last_valid")
            ),
            duration_s=_optional_time(value["duration_s"], context="duration_s"),
            time_step_s=_optional_time(value["time_step_s"], context="time_step_s"),
            diagnostics=_freeze_json_mapping(value["diagnostics"], context="diagnostics"),
        )
        supplied_fingerprint = value.get("result_fingerprint")
        if (
            supplied_fingerprint is not None
            and _digest(
                supplied_fingerprint,
                context="result_fingerprint",
            )
            != payload.result_fingerprint
        ):
            raise ValueError("execution result fingerprint mismatch")
        return payload


RunOutcome = ExecutionPayload


__all__ = [
    "CUSTOM_INPUT_VERSION",
    "RUNTIME_SCHEMA_VERSION",
    "RUNTIME_VERSION",
    "RUN_REQUEST_VERSION",
    "ErrorRecord",
    "EventRecord",
    "EventValueBasis",
    "ExecutionPayload",
    "FailureRecord",
    "JsonValue",
    "RunOutcome",
    "RunRequest",
    "RunType",
    "RuntimeInputEvent",
    "RuntimeScenarioRequest",
    "RuntimeStatus",
]
