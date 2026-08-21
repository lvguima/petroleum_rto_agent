"""Strict, versioned observation and source-manifest contracts for M5.

The catalog is intentionally evidence-oriented: every usable number keeps its
raw transcription, canonical SI value, immutable source digest, source
locator, timezone-aware observation time, alignment semantics, uncertainty,
and explicit usage/exclusion decision.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from ..repository import resolve_cdu_repository_path
from .etl import file_sha256, parse_lab_value


class ObservationContractError(ValueError):
    """Raised when observation evidence is incomplete or internally inconsistent."""


OBSERVATION_SCHEMA_VERSION = "1.0.0"
OBSERVATION_CATALOG_VERSION = "cdu-observations-v0.1.0"
SOURCE_MANIFEST_SCHEMA_VERSION = "1.0.0"
SOURCE_MANIFEST_VERSION = "cdu-sources-v0.1.0"

ValueQualifier = Literal["exact", "explicit_positive", "upper_bound"]
VariableRole = Literal[
    "net_boundary_input",
    "net_boundary_output",
    "internal_circulation",
    "internal_reflux",
    "internal_transfer",
    "auxiliary_input",
    "operating_condition",
    "quality_anchor",
]
TimeSemantics = Literal["operator_screen_instantaneous", "laboratory_sample_reported_at"]
AlignmentQuality = Literal["same_screen", "weak"]
ExtractionMethod = Literal["manual_visual_transcription", "ooxml_shared_string_lookup"]
Quality = Literal["manual_screen_read", "laboratory_report_export"]
Confidence = Literal["low", "medium", "high"]
Usage = Literal["data_coordination", "calibration_target", "diagnostic_reference", "do_not_use"]
Status = Literal["candidate", "reference_only", "excluded"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VARIABLE_ROLES = frozenset(
    {
        "net_boundary_input",
        "net_boundary_output",
        "internal_circulation",
        "internal_reflux",
        "internal_transfer",
        "auxiliary_input",
        "operating_condition",
        "quality_anchor",
    }
)
_TIME_SEMANTICS = frozenset(
    {"operator_screen_instantaneous", "laboratory_sample_reported_at"}
)
_ALIGNMENT_QUALITIES = frozenset({"same_screen", "weak"})
_EXTRACTION_METHODS = frozenset(
    {"manual_visual_transcription", "ooxml_shared_string_lookup"}
)
_QUALITIES = frozenset({"manual_screen_read", "laboratory_report_export"})
_CONFIDENCES = frozenset({"low", "medium", "high"})
_USAGES = frozenset(
    {"data_coordination", "calibration_target", "diagnostic_reference", "do_not_use"}
)
_STATUSES = frozenset({"candidate", "reference_only", "excluded"})
_VALUE_QUALIFIERS = frozenset({"exact", "explicit_positive", "upper_bound"})
_UNIT_RULES: Mapping[str, tuple[str, float, float]] = {
    "t/h": ("kg/s", 1000.0 / 3600.0, 0.0),
    "degC": ("K", 1.0, 273.15),
    "MPa(g)": ("Pa(g)", 1_000_000.0, 0.0),
}
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "catalog_version",
        "id",
        "variable",
        "variable_role",
        "instrument_tag",
        "raw_text",
        "raw_value",
        "raw_unit",
        "value_qualifier",
        "si_value",
        "si_unit",
        "source_id",
        "source_path",
        "source_locator",
        "source_sha256",
        "observed_at",
        "time_semantics",
        "alignment_group",
        "alignment_quality",
        "extraction_method",
        "quality",
        "confidence",
        "uncertainty_si",
        "uncertainty_unit",
        "uncertainty_basis",
        "usage",
        "status",
        "exclusion_reason",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_version",
        "source_id",
        "source_path",
        "media_type",
        "byte_size",
        "sha256",
        "extraction_method",
        "observation_scope",
        "read_only",
    }
)


def _strict_keys(value: Mapping[str, object], expected: frozenset[str], *, context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ObservationContractError(
            f"{context} fields differ; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservationContractError(f"{field_name} must be a non-empty string")
    return value


def _identifier(value: object, *, field_name: str) -> str:
    result = _text(value, field_name=field_name)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ObservationContractError(f"{field_name} must be a stable identifier")
    return result


def _number(value: object, *, field_name: str, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ObservationContractError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ObservationContractError(f"{field_name} must be finite")
    if positive and result <= 0.0:
        raise ObservationContractError(f"{field_name} must be positive")
    return result


def _choice(value: object, choices: frozenset[str], *, field_name: str) -> str:
    result = _text(value, field_name=field_name)
    if result not in choices:
        raise ObservationContractError(
            f"{field_name} must be one of {sorted(choices)}, got {result!r}"
        )
    return result


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name=field_name)


def _source_path(value: object, *, field_name: str = "source_path") -> str:
    result = _text(value, field_name=field_name)
    if "\\" in result:
        raise ObservationContractError(f"{field_name} must use repository-relative POSIX separators")
    path = PurePosixPath(result)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise ObservationContractError(f"{field_name} must be a safe repository-relative path")
    if not path.parts or path.parts[0] != "base_files":
        raise ObservationContractError(f"{field_name} must point into base_files")
    return result


def _sha256(value: object, *, field_name: str) -> str:
    result = _text(value, field_name=field_name)
    if _SHA256.fullmatch(result) is None:
        raise ObservationContractError(f"{field_name} must be a lowercase SHA-256 digest")
    return result


def _aware_datetime(value: object, *, field_name: str) -> datetime:
    raw = _text(value, field_name=field_name)
    if "T" not in raw:
        raise ObservationContractError(f"{field_name} must be an ISO-8601 timestamp with timezone")
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ObservationContractError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ObservationContractError(f"{field_name} must include a timezone")
    if result.microsecond != 0:
        raise ObservationContractError(f"{field_name} must use whole-second precision")
    return result


def convert_to_si(raw_value: float, raw_unit: str) -> tuple[float, str]:
    """Convert one supported raw observation value to its canonical SI unit."""

    if raw_unit not in _UNIT_RULES:
        raise ObservationContractError(f"unsupported observation unit {raw_unit!r}")
    if not math.isfinite(raw_value):
        raise ObservationContractError("raw_value must be finite")
    si_unit, scale, offset = _UNIT_RULES[raw_unit]
    return raw_value * scale + offset, si_unit


def _verify_raw_text(raw_text: str, raw_value: float, qualifier: str) -> None:
    parsed = parse_lab_value(raw_text)
    if parsed.kind != qualifier or parsed.value is None:
        raise ObservationContractError(
            f"raw_text {raw_text!r} is inconsistent with value_qualifier {qualifier!r}"
        )
    if not math.isclose(parsed.value, raw_value, rel_tol=0.0, abs_tol=1e-12):
        raise ObservationContractError("raw_text numeric content differs from raw_value")


@dataclass(frozen=True)
class Observation:
    """One fully traceable observation in canonical SI form."""

    schema_version: str
    catalog_version: str
    id: str
    variable: str
    variable_role: VariableRole
    instrument_tag: str | None
    raw_text: str
    raw_value: float
    raw_unit: str
    value_qualifier: ValueQualifier
    si_value: float
    si_unit: str
    source_id: str
    source_path: str
    source_locator: str
    source_sha256: str
    observed_at: datetime
    time_semantics: TimeSemantics
    alignment_group: str
    alignment_quality: AlignmentQuality
    extraction_method: ExtractionMethod
    quality: Quality
    confidence: Confidence
    uncertainty_si: float
    uncertainty_unit: str
    uncertainty_basis: str
    usage: Usage
    status: Status
    exclusion_reason: str | None

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ObservationContractError("unsupported observation schema_version")
        if self.catalog_version != OBSERVATION_CATALOG_VERSION:
            raise ObservationContractError("unsupported observation catalog_version")
        _identifier(self.id, field_name="id")
        _identifier(self.variable, field_name="variable")
        _choice(self.variable_role, _VARIABLE_ROLES, field_name="variable_role")
        if self.instrument_tag is not None:
            _text(self.instrument_tag, field_name="instrument_tag")
        _text(self.raw_text, field_name="raw_text")
        raw_value = _number(self.raw_value, field_name="raw_value", positive=True)
        qualifier = _choice(
            self.value_qualifier,
            _VALUE_QUALIFIERS,
            field_name="value_qualifier",
        )
        _verify_raw_text(self.raw_text, raw_value, qualifier)
        expected_si, expected_unit = convert_to_si(raw_value, self.raw_unit)
        si_value = _number(self.si_value, field_name="si_value", positive=True)
        if self.si_unit != expected_unit:
            raise ObservationContractError(
                f"si_unit must be {expected_unit!r} for raw_unit {self.raw_unit!r}"
            )
        if not math.isclose(si_value, expected_si, rel_tol=1e-12, abs_tol=1e-10):
            raise ObservationContractError("si_value does not match the declared raw value and unit")
        _identifier(self.source_id, field_name="source_id")
        _source_path(self.source_path)
        _text(self.source_locator, field_name="source_locator")
        _sha256(self.source_sha256, field_name="source_sha256")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ObservationContractError("observed_at must include a timezone")
        if self.observed_at.microsecond != 0:
            raise ObservationContractError("observed_at must use whole-second precision")
        _choice(self.time_semantics, _TIME_SEMANTICS, field_name="time_semantics")
        _identifier(self.alignment_group, field_name="alignment_group")
        _choice(
            self.alignment_quality,
            _ALIGNMENT_QUALITIES,
            field_name="alignment_quality",
        )
        _choice(self.extraction_method, _EXTRACTION_METHODS, field_name="extraction_method")
        _choice(self.quality, _QUALITIES, field_name="quality")
        _choice(self.confidence, _CONFIDENCES, field_name="confidence")
        _number(self.uncertainty_si, field_name="uncertainty_si", positive=True)
        if self.uncertainty_unit != self.si_unit:
            raise ObservationContractError("uncertainty_unit must equal si_unit")
        _text(self.uncertainty_basis, field_name="uncertainty_basis")
        _choice(self.usage, _USAGES, field_name="usage")
        _choice(self.status, _STATUSES, field_name="status")
        if self.status == "candidate" and self.exclusion_reason is not None:
            raise ObservationContractError("candidate observations cannot have an exclusion_reason")
        if self.status != "candidate" and (
            self.exclusion_reason is None or not self.exclusion_reason.strip()
        ):
            raise ObservationContractError(
                "reference_only and excluded observations require an exclusion_reason"
            )
        if self.usage == "do_not_use" and self.status != "excluded":
            raise ObservationContractError("do_not_use observations must have excluded status")
        if self.status == "excluded" and self.usage != "do_not_use":
            raise ObservationContractError("excluded observations must use do_not_use")

        suffix = PurePosixPath(self.source_path).suffix.lower()
        if self.extraction_method == "manual_visual_transcription" and suffix not in {
            ".jpg",
            ".jpeg",
            ".png",
        }:
            raise ObservationContractError("manual_visual_transcription requires an image source")
        if self.extraction_method == "ooxml_shared_string_lookup" and suffix != ".xlsx":
            raise ObservationContractError("ooxml_shared_string_lookup requires an .xlsx source")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Observation:
        """Construct from one strict JSON object."""

        _strict_keys(value, _OBSERVATION_FIELDS, context="observation")
        instrument_tag = _optional_text(value["instrument_tag"], field_name="instrument_tag")
        exclusion_reason = _optional_text(
            value["exclusion_reason"],
            field_name="exclusion_reason",
        )
        return cls(
            schema_version=_text(value["schema_version"], field_name="schema_version"),
            catalog_version=_text(value["catalog_version"], field_name="catalog_version"),
            id=_identifier(value["id"], field_name="id"),
            variable=_identifier(value["variable"], field_name="variable"),
            variable_role=cast(
                VariableRole,
                _choice(value["variable_role"], _VARIABLE_ROLES, field_name="variable_role"),
            ),
            instrument_tag=instrument_tag,
            raw_text=_text(value["raw_text"], field_name="raw_text"),
            raw_value=_number(value["raw_value"], field_name="raw_value", positive=True),
            raw_unit=_text(value["raw_unit"], field_name="raw_unit"),
            value_qualifier=cast(
                ValueQualifier,
                _choice(
                    value["value_qualifier"],
                    _VALUE_QUALIFIERS,
                    field_name="value_qualifier",
                ),
            ),
            si_value=_number(value["si_value"], field_name="si_value", positive=True),
            si_unit=_text(value["si_unit"], field_name="si_unit"),
            source_id=_identifier(value["source_id"], field_name="source_id"),
            source_path=_source_path(value["source_path"]),
            source_locator=_text(value["source_locator"], field_name="source_locator"),
            source_sha256=_sha256(value["source_sha256"], field_name="source_sha256"),
            observed_at=_aware_datetime(value["observed_at"], field_name="observed_at"),
            time_semantics=cast(
                TimeSemantics,
                _choice(
                    value["time_semantics"],
                    _TIME_SEMANTICS,
                    field_name="time_semantics",
                ),
            ),
            alignment_group=_identifier(value["alignment_group"], field_name="alignment_group"),
            alignment_quality=cast(
                AlignmentQuality,
                _choice(
                    value["alignment_quality"],
                    _ALIGNMENT_QUALITIES,
                    field_name="alignment_quality",
                ),
            ),
            extraction_method=cast(
                ExtractionMethod,
                _choice(
                    value["extraction_method"],
                    _EXTRACTION_METHODS,
                    field_name="extraction_method",
                ),
            ),
            quality=cast(
                Quality,
                _choice(value["quality"], _QUALITIES, field_name="quality"),
            ),
            confidence=cast(
                Confidence,
                _choice(value["confidence"], _CONFIDENCES, field_name="confidence"),
            ),
            uncertainty_si=_number(
                value["uncertainty_si"],
                field_name="uncertainty_si",
                positive=True,
            ),
            uncertainty_unit=_text(
                value["uncertainty_unit"],
                field_name="uncertainty_unit",
            ),
            uncertainty_basis=_text(
                value["uncertainty_basis"],
                field_name="uncertainty_basis",
            ),
            usage=cast(
                Usage,
                _choice(value["usage"], _USAGES, field_name="usage"),
            ),
            status=cast(
                Status,
                _choice(value["status"], _STATUSES, field_name="status"),
            ),
            exclusion_reason=exclusion_reason,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the deterministic JSON representation."""

        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "id": self.id,
            "variable": self.variable,
            "variable_role": self.variable_role,
            "instrument_tag": self.instrument_tag,
            "raw_text": self.raw_text,
            "raw_value": self.raw_value,
            "raw_unit": self.raw_unit,
            "value_qualifier": self.value_qualifier,
            "si_value": self.si_value,
            "si_unit": self.si_unit,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "source_locator": self.source_locator,
            "source_sha256": self.source_sha256,
            "observed_at": self.observed_at.isoformat(timespec="seconds"),
            "time_semantics": self.time_semantics,
            "alignment_group": self.alignment_group,
            "alignment_quality": self.alignment_quality,
            "extraction_method": self.extraction_method,
            "quality": self.quality,
            "confidence": self.confidence,
            "uncertainty_si": self.uncertainty_si,
            "uncertainty_unit": self.uncertainty_unit,
            "uncertainty_basis": self.uncertainty_basis,
            "usage": self.usage,
            "status": self.status,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class SourceManifestRecord:
    """Immutable identity and permitted extraction scope for one raw source."""

    schema_version: str
    manifest_version: str
    source_id: str
    source_path: str
    media_type: str
    byte_size: int
    sha256: str
    extraction_method: ExtractionMethod
    observation_scope: str
    read_only: bool

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_MANIFEST_SCHEMA_VERSION:
            raise ObservationContractError("unsupported source manifest schema_version")
        if self.manifest_version != SOURCE_MANIFEST_VERSION:
            raise ObservationContractError("unsupported source manifest_version")
        _identifier(self.source_id, field_name="source_id")
        _source_path(self.source_path)
        _text(self.media_type, field_name="media_type")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size <= 0:
            raise ObservationContractError("byte_size must be a positive integer")
        _sha256(self.sha256, field_name="sha256")
        _choice(self.extraction_method, _EXTRACTION_METHODS, field_name="extraction_method")
        _text(self.observation_scope, field_name="observation_scope")
        if self.read_only is not True:
            raise ObservationContractError("raw sources must be declared read_only")
        suffix = PurePosixPath(self.source_path).suffix.lower()
        expected_media = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }.get(suffix)
        if expected_media is None or self.media_type != expected_media:
            raise ObservationContractError("media_type does not match source_path")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SourceManifestRecord:
        """Construct one strict source manifest record."""

        _strict_keys(value, _SOURCE_FIELDS, context="source manifest record")
        byte_size = value["byte_size"]
        if isinstance(byte_size, bool) or not isinstance(byte_size, int):
            raise ObservationContractError("byte_size must be a positive integer")
        read_only = value["read_only"]
        if not isinstance(read_only, bool):
            raise ObservationContractError("read_only must be boolean")
        return cls(
            schema_version=_text(value["schema_version"], field_name="schema_version"),
            manifest_version=_text(value["manifest_version"], field_name="manifest_version"),
            source_id=_identifier(value["source_id"], field_name="source_id"),
            source_path=_source_path(value["source_path"]),
            media_type=_text(value["media_type"], field_name="media_type"),
            byte_size=byte_size,
            sha256=_sha256(value["sha256"], field_name="sha256"),
            extraction_method=cast(
                ExtractionMethod,
                _choice(
                    value["extraction_method"],
                    _EXTRACTION_METHODS,
                    field_name="extraction_method",
                ),
            ),
            observation_scope=_text(
                value["observation_scope"],
                field_name="observation_scope",
            ),
            read_only=read_only,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the deterministic JSON representation."""

        return {
            "schema_version": self.schema_version,
            "manifest_version": self.manifest_version,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "extraction_method": self.extraction_method,
            "observation_scope": self.observation_scope,
            "read_only": self.read_only,
        }


def _decode_json_line(raw_line: str, *, line_number: int, path: Path) -> Mapping[str, object]:
    try:
        decoded: object = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise ObservationContractError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise ObservationContractError(f"JSONL record at {path}:{line_number} must be an object")
    return cast(Mapping[str, object], decoded)


def _load_jsonl(path: Path) -> tuple[Mapping[str, object], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ObservationContractError(f"cannot read catalog {path}: {exc}") from exc
    lines = text.splitlines()
    if not lines:
        raise ObservationContractError(f"catalog is empty: {path}")
    records: list[Mapping[str, object]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            raise ObservationContractError(f"blank JSONL record at {path}:{line_number}")
        records.append(_decode_json_line(raw_line, line_number=line_number, path=path))
    return tuple(records)


def _verify_order_and_uniqueness(ids: list[str], *, context: str) -> None:
    if len(ids) != len(set(ids)):
        raise ObservationContractError(f"{context} contains duplicate ids")
    if ids != sorted(ids):
        raise ObservationContractError(f"{context} records must be sorted by id")


def _verified_source_path(repo_root: Path, source_path: str) -> Path:
    try:
        candidate = resolve_cdu_repository_path(repo_root, source_path)
    except (TypeError, ValueError) as exc:
        raise ObservationContractError(str(exc)) from exc
    if not candidate.is_file():
        raise ObservationContractError(f"source does not exist: {source_path}")
    return candidate


def load_observation_catalog(
    path: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[Observation, ...]:
    """Load, strictly validate, and optionally hash-check a JSONL catalog."""

    observations = tuple(Observation.from_mapping(value) for value in _load_jsonl(path))
    _verify_order_and_uniqueness(
        [observation.id for observation in observations],
        context="observation catalog",
    )
    if repo_root is not None:
        for observation in observations:
            source = _verified_source_path(repo_root, observation.source_path)
            if file_sha256(source) != observation.source_sha256:
                raise ObservationContractError(
                    f"source hash mismatch for observation {observation.id}"
                )
    return observations


def load_source_manifest(
    path: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[SourceManifestRecord, ...]:
    """Load a strict manifest and optionally verify source sizes and hashes."""

    records = tuple(SourceManifestRecord.from_mapping(value) for value in _load_jsonl(path))
    _verify_order_and_uniqueness(
        [record.source_id for record in records],
        context="source manifest",
    )
    if repo_root is not None:
        for record in records:
            source = _verified_source_path(repo_root, record.source_path)
            if source.stat().st_size != record.byte_size:
                raise ObservationContractError(f"source size mismatch for {record.source_id}")
            if file_sha256(source) != record.sha256:
                raise ObservationContractError(f"source hash mismatch for {record.source_id}")
    return records


def validate_observation_sources(
    observations: Iterable[Observation],
    sources: Iterable[SourceManifestRecord],
    *,
    repo_root: Path | None = None,
) -> None:
    """Cross-check every observation against the versioned source manifest."""

    observation_values = tuple(observations)
    source_values = tuple(sources)
    by_id = {source.source_id: source for source in source_values}
    if len(by_id) != len(source_values):
        raise ObservationContractError("source manifest contains duplicate ids")
    referenced_ids = {observation.source_id for observation in observation_values}
    if referenced_ids != set(by_id):
        raise ObservationContractError(
            "observation/source ids differ; "
            f"missing={sorted(referenced_ids - set(by_id))}, "
            f"unused={sorted(set(by_id) - referenced_ids)}"
        )
    for observation in observation_values:
        source = by_id[observation.source_id]
        if observation.source_path != source.source_path:
            raise ObservationContractError(f"source path mismatch for {observation.id}")
        if observation.source_sha256 != source.sha256:
            raise ObservationContractError(f"source hash mismatch for {observation.id}")
        if observation.extraction_method != source.extraction_method:
            raise ObservationContractError(f"extraction method mismatch for {observation.id}")
    if repo_root is not None:
        for source in source_values:
            actual = _verified_source_path(repo_root, source.source_path)
            if actual.stat().st_size != source.byte_size or file_sha256(actual) != source.sha256:
                raise ObservationContractError(f"source identity mismatch for {source.source_id}")


def observation_catalog_jsonl(observations: Iterable[Observation]) -> str:
    """Serialize observations in canonical id order with stable JSON formatting."""

    ordered = sorted(observations, key=lambda item: item.id)
    _verify_order_and_uniqueness(
        [observation.id for observation in ordered],
        context="observation catalog",
    )
    return "".join(
        json.dumps(
            observation.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for observation in ordered
    )


def source_manifest_jsonl(sources: Iterable[SourceManifestRecord]) -> str:
    """Serialize source records in canonical id order with stable formatting."""

    ordered = sorted(sources, key=lambda item: item.source_id)
    _verify_order_and_uniqueness(
        [source.source_id for source in ordered],
        context="source manifest",
    )
    return "".join(
        json.dumps(
            source.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for source in ordered
    )
