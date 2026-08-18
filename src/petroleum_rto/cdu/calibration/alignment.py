"""Strict M5 case-alignment and reconciliation configuration."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from ..core.config import ConfigurationError, canonical_fingerprint, load_json, strict_keys
from .reconciliation import INTERNAL_STREAM_IDS

MEASURED_BOUNDARY_STREAM_IDS: Final[tuple[str, ...]] = (
    "fresh_feed",
    "wash_water",
    "gasoline",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
)
LATENT_BOUNDARY_STREAM_IDS: Final[tuple[str, ...]] = ("offgas", "aqueous", "brine")
EXCLUDED_OBSERVED_INTERNAL_IDS: Final[tuple[str, ...]] = (
    "reflux",
    "top_circulation",
)


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, context: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ConfigurationError(f"{context} must be a sequence")
    return cast(Sequence[object], value)


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be a non-empty string")
    return value


def _number(value: object, *, context: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualification = "finite and positive" if positive else "finite"
        raise ConfigurationError(f"{context} must be {qualification}")
    return number


def _relative_path(value: object, *, context: str) -> str:
    text = _text(value, context=context)
    if "\\" in text:
        raise ConfigurationError(f"{context} must use forward slashes")
    parsed = PurePosixPath(text)
    if parsed.is_absolute() or not parsed.parts or "." in parsed.parts or ".." in parsed.parts:
        raise ConfigurationError(f"{context} must be a safe repository-relative path")
    return text


def _aware_datetime(value: object, *, context: str) -> datetime:
    raw = _text(value, context=context)
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{context} must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None or result.microsecond != 0:
        raise ConfigurationError(f"{context} must include timezone and whole seconds")
    return result


@dataclass(frozen=True)
class EngineeringScaleSpec:
    """One explicit non-statistical reconciliation weight."""

    stream_id: str
    relative_engineering_scale: float
    floor_kg_s: float

    def __post_init__(self) -> None:
        _text(self.stream_id, context="stream_id")
        relative = _number(
            self.relative_engineering_scale,
            context=f"{self.stream_id}.relative_engineering_scale",
            positive=True,
        )
        floor = _number(
            self.floor_kg_s,
            context=f"{self.stream_id}.floor_kg_s",
            positive=True,
        )
        object.__setattr__(self, "relative_engineering_scale", relative)
        object.__setattr__(self, "floor_kg_s", floor)

    def scale_for(self, value_kg_s: float) -> float:
        value = _number(value_kg_s, context=f"{self.stream_id} value", positive=True)
        return max(self.relative_engineering_scale * value, self.floor_kg_s)

    def as_dict(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "relative_engineering_scale": self.relative_engineering_scale,
            "floor_kg_s": self.floor_kg_s,
        }


@dataclass(frozen=True)
class BoundaryMeasurementSpec(EngineeringScaleSpec):
    """Fixed observation identity for one measured net-boundary flow."""

    observation_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.stream_id not in MEASURED_BOUNDARY_STREAM_IDS:
            raise ConfigurationError(f"unsupported measured boundary stream {self.stream_id!r}")
        _text(self.observation_id, context=f"{self.stream_id}.observation_id")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BoundaryMeasurementSpec:
        strict_keys(
            value,
            required={
                "stream_id",
                "observation_id",
                "relative_engineering_scale",
                "floor_kg_s",
            },
            context="boundary measurement",
        )
        return cls(
            stream_id=_text(value["stream_id"], context="boundary stream_id"),
            observation_id=_text(
                value["observation_id"], context="boundary observation_id"
            ),
            relative_engineering_scale=_number(
                value["relative_engineering_scale"],
                context="boundary relative engineering scale",
                positive=True,
            ),
            floor_kg_s=_number(
                value["floor_kg_s"], context="boundary scale floor", positive=True
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "observation_id": self.observation_id}


@dataclass(frozen=True)
class LatentPriorSpec(EngineeringScaleSpec):
    """M2-derived prior contract for one unmeasured net outlet."""

    source: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.stream_id not in LATENT_BOUNDARY_STREAM_IDS:
            raise ConfigurationError(f"unsupported latent boundary stream {self.stream_id!r}")
        if self.source != "effective_baseline_m2":
            raise ConfigurationError("latent prior source must be effective_baseline_m2")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LatentPriorSpec:
        strict_keys(
            value,
            required={
                "stream_id",
                "source",
                "relative_engineering_scale",
                "floor_kg_s",
            },
            context="latent prior",
        )
        return cls(
            stream_id=_text(value["stream_id"], context="latent stream_id"),
            source=_text(value["source"], context="latent source"),
            relative_engineering_scale=_number(
                value["relative_engineering_scale"],
                context="latent relative engineering scale",
                positive=True,
            ),
            floor_kg_s=_number(
                value["floor_kg_s"], context="latent scale floor", positive=True
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "source": self.source}


@dataclass(frozen=True)
class ExcludedInternalSpec(EngineeringScaleSpec):
    """Observed internal circulation retained but excluded from net balance."""

    observation_id: str
    exclusion_reason: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.stream_id not in INTERNAL_STREAM_IDS:
            raise ConfigurationError(f"unsupported internal stream {self.stream_id!r}")
        _text(self.observation_id, context=f"{self.stream_id}.observation_id")
        _text(self.exclusion_reason, context=f"{self.stream_id}.exclusion_reason")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ExcludedInternalSpec:
        strict_keys(
            value,
            required={
                "stream_id",
                "observation_id",
                "relative_engineering_scale",
                "floor_kg_s",
                "exclusion_reason",
            },
            context="excluded internal flow",
        )
        return cls(
            stream_id=_text(value["stream_id"], context="internal stream_id"),
            observation_id=_text(
                value["observation_id"], context="internal observation_id"
            ),
            relative_engineering_scale=_number(
                value["relative_engineering_scale"],
                context="internal relative engineering scale",
                positive=True,
            ),
            floor_kg_s=_number(
                value["floor_kg_s"], context="internal scale floor", positive=True
            ),
            exclusion_reason=_text(
                value["exclusion_reason"], context="internal exclusion reason"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "observation_id": self.observation_id,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class AlignmentPaths:
    """Repository-relative M5 inputs."""

    model_config: str
    case_config: str
    observation_catalog: str
    source_manifest: str
    calibration_config: str

    def __post_init__(self) -> None:
        for name in (
            "model_config",
            "case_config",
            "observation_catalog",
            "source_manifest",
            "calibration_config",
        ):
            object.__setattr__(
                self,
                name,
                _relative_path(getattr(self, name), context=f"paths.{name}"),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AlignmentPaths:
        required = {
            "model_config",
            "case_config",
            "observation_catalog",
            "source_manifest",
            "calibration_config",
        }
        strict_keys(value, required=required, context="alignment paths")
        return cls(**{name: _relative_path(value[name], context=f"paths.{name}") for name in required})

    def as_dict(self) -> dict[str, str]:
        return {
            "model_config": self.model_config,
            "case_config": self.case_config,
            "observation_catalog": self.observation_catalog,
            "source_manifest": self.source_manifest,
            "calibration_config": self.calibration_config,
        }


@dataclass(frozen=True)
class CaseOverlaySpec:
    """Observation identities used for the two effective operating overlays."""

    flash_temperature_observation_id: str
    wash_water_observation_id: str
    wash_ratio_feed_observation_id: str

    def __post_init__(self) -> None:
        for name in (
            "flash_temperature_observation_id",
            "wash_water_observation_id",
            "wash_ratio_feed_observation_id",
        ):
            _text(getattr(self, name), context=f"case_overlays.{name}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CaseOverlaySpec:
        required = {
            "flash_temperature_observation_id",
            "wash_water_observation_id",
            "wash_ratio_feed_observation_id",
        }
        strict_keys(value, required=required, context="case overlays")
        return cls(**{name: _text(value[name], context=f"case_overlays.{name}") for name in required})

    def as_dict(self) -> dict[str, str]:
        return {
            "flash_temperature_observation_id": self.flash_temperature_observation_id,
            "wash_water_observation_id": self.wash_water_observation_id,
            "wash_ratio_feed_observation_id": self.wash_ratio_feed_observation_id,
        }


@dataclass(frozen=True)
class ArtifactPaths:
    """Versioned M5 output locations."""

    reconciled_case: str
    calibrated_parameters: str
    report_json: str
    report_markdown: str
    artifact_manifest: str

    def __post_init__(self) -> None:
        for name in (
            "reconciled_case",
            "calibrated_parameters",
            "report_json",
            "report_markdown",
            "artifact_manifest",
        ):
            object.__setattr__(
                self,
                name,
                _relative_path(getattr(self, name), context=f"artifacts.{name}"),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArtifactPaths:
        required = {
            "reconciled_case",
            "calibrated_parameters",
            "report_json",
            "report_markdown",
            "artifact_manifest",
        }
        strict_keys(value, required=required, context="alignment artifacts")
        return cls(**{name: _relative_path(value[name], context=f"artifacts.{name}") for name in required})

    def as_dict(self) -> dict[str, str]:
        return {
            "reconciled_case": self.reconciled_case,
            "calibrated_parameters": self.calibrated_parameters,
            "report_json": self.report_json,
            "report_markdown": self.report_markdown,
            "artifact_manifest": self.artifact_manifest,
        }


@dataclass(frozen=True)
class AlignmentConfig:
    """Complete versioned M5 observation-to-calibration alignment contract."""

    schema_version: str
    alignment_version: str
    reconciliation_config_version: str
    derived_case_version: str
    model_version: str
    model_config_version: str
    base_parameter_set_version: str
    base_case_version: str
    observation_catalog_version: str
    source_manifest_version: str
    calibration_version: str
    case_reference_time: datetime
    maximum_alignment_offset_s: float
    paths: AlignmentPaths
    case_overlays: CaseOverlaySpec
    boundary_measurements: tuple[BoundaryMeasurementSpec, ...]
    latent_priors: tuple[LatentPriorSpec, ...]
    excluded_internal: tuple[ExcludedInternalSpec, ...]
    artifacts: ArtifactPaths
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in (
            "schema_version",
            "alignment_version",
            "reconciliation_config_version",
            "derived_case_version",
            "model_version",
            "model_config_version",
            "base_parameter_set_version",
            "base_case_version",
            "observation_catalog_version",
            "source_manifest_version",
            "calibration_version",
        ):
            _text(getattr(self, name), context=name)
        if self.case_reference_time.tzinfo is None or self.case_reference_time.utcoffset() is None:
            raise ConfigurationError("case_reference_time must include timezone")
        if self.case_reference_time.microsecond != 0:
            raise ConfigurationError("case_reference_time must use whole seconds")
        offset = _number(
            self.maximum_alignment_offset_s,
            context="maximum_alignment_offset_s",
            positive=True,
        )
        if tuple(item.stream_id for item in self.boundary_measurements) != MEASURED_BOUNDARY_STREAM_IDS:
            raise ConfigurationError("boundary measurements differ from the fixed seven-stream order")
        if tuple(item.stream_id for item in self.latent_priors) != LATENT_BOUNDARY_STREAM_IDS:
            raise ConfigurationError("latent priors differ from the fixed three-stream order")
        if tuple(item.stream_id for item in self.excluded_internal) != EXCLUDED_OBSERVED_INTERNAL_IDS:
            raise ConfigurationError("excluded internal observations differ from the fixed order")
        observation_ids = [item.observation_id for item in self.boundary_measurements]
        observation_ids.extend(item.observation_id for item in self.excluded_internal)
        if len(observation_ids) != len(set(observation_ids)):
            raise ConfigurationError("alignment observation mappings cannot contain duplicates")
        required_metadata = {
            "claim_scope",
            "confidence",
            "engineering_scale_basis",
            "latent_prior_basis",
        }
        if set(self.metadata) != required_metadata or any(
            not isinstance(key, str) or not isinstance(value, str) or not value.strip()
            for key, value in self.metadata.items()
        ):
            raise ConfigurationError("alignment metadata differs from the fixed contract")
        if self.metadata["claim_scope"] != "case_alignment_only":
            raise ConfigurationError("alignment claim_scope must be case_alignment_only")
        object.__setattr__(self, "maximum_alignment_offset_s", offset)
        object.__setattr__(self, "boundary_measurements", tuple(self.boundary_measurements))
        object.__setattr__(self, "latent_priors", tuple(self.latent_priors))
        object.__setattr__(self, "excluded_internal", tuple(self.excluded_internal))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AlignmentConfig:
        required = {
            "schema_version",
            "alignment_version",
            "reconciliation_config_version",
            "derived_case_version",
            "model_version",
            "model_config_version",
            "base_parameter_set_version",
            "base_case_version",
            "observation_catalog_version",
            "source_manifest_version",
            "calibration_version",
            "case_reference_time",
            "maximum_alignment_offset_s",
            "paths",
            "case_overlays",
            "boundary_measurements",
            "latent_priors",
            "excluded_internal",
            "artifacts",
            "metadata",
        }
        strict_keys(value, required=required, context="M5 alignment configuration")
        measurements = tuple(
            BoundaryMeasurementSpec.from_mapping(
                _mapping(item, context=f"boundary measurement {index}")
            )
            for index, item in enumerate(
                _sequence(value["boundary_measurements"], context="boundary measurements")
            )
        )
        priors = tuple(
            LatentPriorSpec.from_mapping(_mapping(item, context=f"latent prior {index}"))
            for index, item in enumerate(
                _sequence(value["latent_priors"], context="latent priors")
            )
        )
        internal = tuple(
            ExcludedInternalSpec.from_mapping(
                _mapping(item, context=f"excluded internal {index}")
            )
            for index, item in enumerate(
                _sequence(value["excluded_internal"], context="excluded internal")
            )
        )
        metadata = _mapping(value["metadata"], context="alignment metadata")
        if any(not isinstance(item, str) for item in metadata.values()):
            raise ConfigurationError("alignment metadata values must be strings")
        return cls(
            schema_version=_text(value["schema_version"], context="schema_version"),
            alignment_version=_text(value["alignment_version"], context="alignment_version"),
            reconciliation_config_version=_text(
                value["reconciliation_config_version"],
                context="reconciliation_config_version",
            ),
            derived_case_version=_text(
                value["derived_case_version"], context="derived_case_version"
            ),
            model_version=_text(value["model_version"], context="model_version"),
            model_config_version=_text(
                value["model_config_version"], context="model_config_version"
            ),
            base_parameter_set_version=_text(
                value["base_parameter_set_version"],
                context="base_parameter_set_version",
            ),
            base_case_version=_text(
                value["base_case_version"], context="base_case_version"
            ),
            observation_catalog_version=_text(
                value["observation_catalog_version"],
                context="observation_catalog_version",
            ),
            source_manifest_version=_text(
                value["source_manifest_version"], context="source_manifest_version"
            ),
            calibration_version=_text(
                value["calibration_version"], context="calibration_version"
            ),
            case_reference_time=_aware_datetime(
                value["case_reference_time"], context="case_reference_time"
            ),
            maximum_alignment_offset_s=_number(
                value["maximum_alignment_offset_s"],
                context="maximum_alignment_offset_s",
                positive=True,
            ),
            paths=AlignmentPaths.from_mapping(
                _mapping(value["paths"], context="alignment paths")
            ),
            case_overlays=CaseOverlaySpec.from_mapping(
                _mapping(value["case_overlays"], context="case overlays")
            ),
            boundary_measurements=measurements,
            latent_priors=priors,
            excluded_internal=internal,
            artifacts=ArtifactPaths.from_mapping(
                _mapping(value["artifacts"], context="alignment artifacts")
            ),
            metadata=MappingProxyType(
                {key: cast(str, item) for key, item in metadata.items()}
            ),
        )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "alignment_version": self.alignment_version,
            "reconciliation_config_version": self.reconciliation_config_version,
            "derived_case_version": self.derived_case_version,
            "model_version": self.model_version,
            "model_config_version": self.model_config_version,
            "base_parameter_set_version": self.base_parameter_set_version,
            "base_case_version": self.base_case_version,
            "observation_catalog_version": self.observation_catalog_version,
            "source_manifest_version": self.source_manifest_version,
            "calibration_version": self.calibration_version,
            "case_reference_time": self.case_reference_time.isoformat(timespec="seconds"),
            "maximum_alignment_offset_s": self.maximum_alignment_offset_s,
            "paths": self.paths.as_dict(),
            "case_overlays": self.case_overlays.as_dict(),
            "boundary_measurements": [item.as_dict() for item in self.boundary_measurements],
            "latent_priors": [item.as_dict() for item in self.latent_priors],
            "excluded_internal": [item.as_dict() for item in self.excluded_internal],
            "artifacts": self.artifacts.as_dict(),
            "metadata": dict(self.metadata),
        }


def load_alignment_config(path: Path) -> AlignmentConfig:
    """Load one strict versioned M5 alignment configuration."""

    try:
        return AlignmentConfig.from_mapping(load_json(path))
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid M5 alignment configuration {path}: {exc}") from exc
