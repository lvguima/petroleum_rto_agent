"""Source-closed M6 analysis basis reconstructed from the accepted M5 suite."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from ... import __version__ as SOFTWARE_VERSION
from ..calibration.etl import file_sha256
from ..calibration.pipeline import M5PipelineResult, run_m5_pipeline
from ..core.config import (
    CaseConfig,
    ModelConfig,
    canonical_fingerprint,
    load_case_config,
    load_component_catalog,
    validate_config_compatibility,
)
from ..properties.components import ComponentCatalog

_SCHEMA_VERSION: Final[str] = "1.0.0"
_ANALYSIS_VERSION: Final[str] = "m6-basis-v0.1.0"
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_NAMES: Final[tuple[str, ...]] = (
    "reconciled_case",
    "calibrated_parameters",
    "report_json",
    "report_markdown",
)
_OBJECT_FINGERPRINT_NAMES: Final[tuple[str, ...]] = (
    "calibrated_model_object",
    "effective_case_object",
    "component_catalog_object",
)
_METADATA: Final[Mapping[str, str]] = MappingProxyType(
    {
        "synthetic": "true",
        "data_origin": "M6_synthetic_validation",
        "claim_scope": "engineering_validation_only",
    }
)


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _json_object(path: Path, *, context: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {context}: {exc}") from exc
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) for key in decoded
    ):
        raise ValueError(f"{context} must be a JSON object with string keys")
    return cast(dict[str, object], decoded)


def _object_field(
    value: Mapping[str, object],
    name: str,
    *,
    context: str,
) -> dict[str, object]:
    raw = value.get(name)
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{context}.{name} must be a JSON object")
    return cast(dict[str, object], raw)


def _fingerprinted_json(
    path: Path,
    *,
    expected_type: str,
    context: str,
) -> dict[str, object]:
    payload = _json_object(path, context=context)
    if payload.get("artifact_type") != expected_type:
        raise ValueError(f"{context} artifact_type differs from {expected_type}")
    fingerprint = _digest(
        payload.get("artifact_fingerprint"),
        context=f"{context}.artifact_fingerprint",
    )
    unsigned = dict(payload)
    del unsigned["artifact_fingerprint"]
    if canonical_fingerprint(unsigned) != fingerprint:
        raise ValueError(f"{context} artifact fingerprint mismatch")
    return payload


def _repo_file(repo_root: Path, relative_path: str, *, context: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        raise ValueError(f"{context} must be a non-empty repository-relative POSIX path")
    parsed = PurePosixPath(relative_path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or "." in parsed.parts
        or ".." in parsed.parts
    ):
        raise ValueError(f"{context} must stay inside the repository")
    root = repo_root.resolve()
    resolved = (root / parsed).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{context} escapes the repository") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"{context} does not exist: {relative_path}")
    return resolved


@dataclass(frozen=True)
class _M5ArtifactEvidence:
    manifest_sha256: str
    manifest_fingerprint: str
    artifact_sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        _digest(self.manifest_sha256, context="M5 manifest SHA-256")
        _digest(self.manifest_fingerprint, context="M5 manifest fingerprint")
        hashes = dict(self.artifact_sha256)
        if tuple(hashes) != _ARTIFACT_NAMES:
            raise ValueError("M5 artifact hashes differ from the fixed suite")
        for name, digest in hashes.items():
            _digest(digest, context=f"M5 artifact {name} SHA-256")
        object.__setattr__(self, "artifact_sha256", MappingProxyType(hashes))


def _validate_parameter_artifact(
    payload: Mapping[str, object],
    result: M5PipelineResult,
) -> None:
    expected_top_level = {
        "schema_version": result.alignment.schema_version,
        "claim_scope": "case_alignment_only",
        "base_model_version": result.versions["model_version"],
        "base_model_config_version": result.versions["model_config_version"],
        "base_parameter_set_version": result.versions[
            "base_parameter_set_version"
        ],
        "parameter_set_version": result.versions[
            "calibrated_parameter_set_version"
        ],
        "base_case_version": result.versions["base_case_version"],
        "derived_case_version": result.versions["derived_case_version"],
        "pipeline_result_fingerprint": result.result_fingerprint,
        "calibrated_model_fingerprint": result.fingerprints[
            "calibrated_model_object"
        ],
        "effective_model_fingerprint": result.fingerprints[
            "effective_model_object"
        ],
        "reconciliation_result_fingerprint": result.fingerprints[
            "reconciliation_result"
        ],
        "calibration_result_fingerprint": result.fingerprints[
            "calibration_result"
        ],
    }
    mismatches = sorted(
        name for name, expected in expected_top_level.items()
        if payload.get(name) != expected
    )
    if mismatches:
        raise ValueError(
            "M5 parameter artifact fields differ from the source-verified pipeline: "
            + ", ".join(mismatches)
        )

    if _object_field(payload, "versions", context="M5 parameter artifact") != dict(
        result.versions
    ):
        raise ValueError("M5 parameter artifact versions differ from the pipeline")
    if _object_field(
        payload,
        "calibration_versions",
        context="M5 parameter artifact",
    ) != dict(result.calibration.versions):
        raise ValueError("M5 parameter calibration versions differ from the pipeline")
    if _object_field(
        payload,
        "calibration_fingerprints",
        context="M5 parameter artifact",
    ) != dict(result.calibration.fingerprints):
        raise ValueError("M5 parameter calibration fingerprints differ from the pipeline")

    expected_file_fingerprints = {
        name: digest
        for name, digest in result.fingerprints.items()
        if name.endswith("_file")
    }
    if _object_field(
        payload,
        "input_file_fingerprints",
        context="M5 parameter artifact",
    ) != expected_file_fingerprints:
        raise ValueError("M5 parameter input-file fingerprints differ from the pipeline")
    expected_object_fingerprints = {
        name: digest
        for name, digest in result.fingerprints.items()
        if not name.endswith("_file")
    }
    if _object_field(
        payload,
        "input_object_fingerprints",
        context="M5 parameter artifact",
    ) != expected_object_fingerprints:
        raise ValueError("M5 parameter object fingerprints differ from the pipeline")

    parameter_overlays = payload.get("parameter_overlays")
    if not isinstance(parameter_overlays, list) or len(parameter_overlays) != len(
        result.calibration.parameters
    ):
        raise ValueError("M5 parameter overlays differ from the calibrated result")
    for raw_overlay, parameter in zip(
        parameter_overlays,
        result.calibration.parameters,
        strict=True,
    ):
        if not isinstance(raw_overlay, dict) or any(
            not isinstance(key, str) for key in raw_overlay
        ):
            raise ValueError("M5 parameter overlay must be a JSON object")
        overlay = cast(dict[str, object], raw_overlay)
        expected = {
            **parameter.as_dict(),
            "distance_to_lower_bound_k": (
                parameter.calibrated_k - parameter.lower_bound_k
            ),
            "distance_to_upper_bound_k": (
                parameter.upper_bound_k - parameter.calibrated_k
            ),
        }
        if overlay != expected:
            raise ValueError("M5 parameter overlays differ from the calibrated result")


def _validate_m5_artifact_suite(
    repo_root: Path,
    result: M5PipelineResult,
) -> _M5ArtifactEvidence:
    """Validate the published M5 commit marker and all referenced files."""

    expected_paths = result.alignment.artifacts.as_dict()
    manifest_relative = expected_paths["artifact_manifest"]
    manifest_path = _repo_file(
        repo_root,
        manifest_relative,
        context="M5 artifact manifest path",
    )
    manifest = _fingerprinted_json(
        manifest_path,
        expected_type="M5_artifact_suite_manifest",
        context="M5 artifact manifest",
    )
    if manifest.get("schema_version") != result.alignment.schema_version:
        raise ValueError("M5 manifest schema version differs from the pipeline")
    if manifest.get("status") != "valid":
        raise ValueError("M5 artifact manifest is not valid")
    if manifest.get("claim_scope") != "case_alignment_only":
        raise ValueError("M5 artifact manifest claim scope differs")
    if manifest.get("manifest_path") != manifest_relative:
        raise ValueError("M5 artifact manifest path differs from the alignment")
    if manifest.get("pipeline_result_fingerprint") != result.result_fingerprint:
        raise ValueError("M5 manifest pipeline fingerprint differs")
    if _object_field(manifest, "versions", context="M5 manifest") != dict(
        result.versions
    ):
        raise ValueError("M5 manifest versions differ from the pipeline")

    expected_result_fingerprints = {
        "reconciliation_result": result.fingerprints["reconciliation_result"],
        "calibration_result": result.fingerprints["calibration_result"],
        "calibrated_model_object": result.fingerprints[
            "calibrated_model_object"
        ],
    }
    if _object_field(
        manifest,
        "result_fingerprints",
        context="M5 manifest",
    ) != expected_result_fingerprints:
        raise ValueError("M5 manifest result fingerprints differ from the pipeline")
    expected_config_fingerprints = {
        name: result.fingerprints[name]
        for name in (
            "alignment_file",
            "alignment_object",
            "calibration_config_file",
            "model_file",
            "case_file",
            "component_catalog_file",
            "observation_catalog_file",
            "source_manifest_file",
        )
    }
    if _object_field(
        manifest,
        "config_fingerprints",
        context="M5 manifest",
    ) != expected_config_fingerprints:
        raise ValueError("M5 manifest input fingerprints differ from the pipeline")

    artifact_entries = _object_field(manifest, "artifacts", context="M5 manifest")
    if set(artifact_entries) != set(_ARTIFACT_NAMES):
        raise ValueError("M5 manifest artifacts differ from the fixed suite")
    artifact_hashes: dict[str, str] = {}
    artifact_paths: dict[str, Path] = {}
    for name in _ARTIFACT_NAMES:
        item = artifact_entries[name]
        if not isinstance(item, dict) or any(
            not isinstance(key, str) for key in item
        ):
            raise ValueError(f"M5 manifest artifact {name} must be an object")
        entry = cast(dict[str, object], item)
        if set(entry) != {"path", "sha256"}:
            raise ValueError(f"M5 manifest artifact {name} fields differ")
        relative_path = _text(entry["path"], context=f"M5 artifact {name} path")
        if relative_path != expected_paths[name]:
            raise ValueError(f"M5 artifact {name} path differs from the alignment")
        expected_sha256 = _digest(
            entry["sha256"],
            context=f"M5 artifact {name} manifest SHA-256",
        )
        path = _repo_file(
            repo_root,
            relative_path,
            context=f"M5 artifact {name} path",
        )
        actual_sha256 = file_sha256(path)
        if actual_sha256 != expected_sha256:
            raise ValueError(f"M5 artifact {name} file hash mismatch")
        artifact_hashes[name] = actual_sha256
        artifact_paths[name] = path

    _fingerprinted_json(
        artifact_paths["reconciled_case"],
        expected_type="M5_reconciled_case",
        context="M5 reconciled-case artifact",
    )
    parameter_payload = _fingerprinted_json(
        artifact_paths["calibrated_parameters"],
        expected_type="M5_calibrated_parameter_set",
        context="M5 calibrated-parameter artifact",
    )
    _validate_parameter_artifact(parameter_payload, result)
    _fingerprinted_json(
        artifact_paths["report_json"],
        expected_type="M5_calibration_review",
        context="M5 calibration report artifact",
    )

    return _M5ArtifactEvidence(
        manifest_sha256=file_sha256(manifest_path),
        manifest_fingerprint=_digest(
            manifest["artifact_fingerprint"],
            context="M5 manifest artifact fingerprint",
        ),
        artifact_sha256=artifact_hashes,
    )


@dataclass(frozen=True)
class M6Basis:
    """Immutable effective model inputs and source-closed M5 evidence for M6."""

    schema_version: str
    analysis_version: str
    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    base_parameter_set_version: str
    derived_parameter_set_version: str
    base_case_version: str
    derived_case_version: str
    m5_pipeline_fingerprint: str
    m5_manifest_sha256: str
    m5_manifest_fingerprint: str
    m5_artifact_sha256: Mapping[str, str]
    effective_object_fingerprints: Mapping[str, str]
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("M6 basis schema_version differs from the fixed contract")
        if self.analysis_version != _ANALYSIS_VERSION:
            raise ValueError("M6 basis analysis_version differs from the fixed contract")
        if not isinstance(self.model, ModelConfig):
            raise TypeError("M6 basis model must be a ModelConfig")
        if not isinstance(self.case, CaseConfig):
            raise TypeError("M6 basis case must be a CaseConfig")
        if not isinstance(self.catalog, ComponentCatalog):
            raise TypeError("M6 basis catalog must be a ComponentCatalog")

        for name in (
            "base_parameter_set_version",
            "derived_parameter_set_version",
            "base_case_version",
            "derived_case_version",
        ):
            _text(getattr(self, name), context=f"M6 basis {name}")
        if self.derived_parameter_set_version == self.base_parameter_set_version:
            raise ValueError("M6 derived and base parameter-set versions must differ")
        if self.derived_case_version == self.base_case_version:
            raise ValueError("M6 derived and base case versions must differ")
        if (
            self.model.parameter_set_version != self.base_parameter_set_version
            or self.case.parameter_set_version != self.base_parameter_set_version
            or self.catalog.parameter_set_version != self.base_parameter_set_version
        ):
            raise ValueError(
                "M6 effective core objects must retain the base parameter-set version"
            )
        if self.case.case_version != self.base_case_version:
            raise ValueError("M6 effective case must retain the base core case version")
        validate_config_compatibility(
            self.model,
            self.case,
            software_version=SOFTWARE_VERSION,
            catalog=self.catalog,
        )

        _digest(self.m5_pipeline_fingerprint, context="M5 pipeline fingerprint")
        _digest(self.m5_manifest_sha256, context="M5 manifest SHA-256")
        _digest(self.m5_manifest_fingerprint, context="M5 manifest fingerprint")

        artifact_hashes = dict(self.m5_artifact_sha256)
        if tuple(artifact_hashes) != _ARTIFACT_NAMES:
            raise ValueError("M6 basis M5 artifact hashes differ from the fixed suite")
        for name, digest in artifact_hashes.items():
            _digest(digest, context=f"M6 basis artifact {name} SHA-256")

        object_fingerprints = dict(self.effective_object_fingerprints)
        if tuple(object_fingerprints) != _OBJECT_FINGERPRINT_NAMES:
            raise ValueError("M6 effective object fingerprint keys differ")
        for name, digest in object_fingerprints.items():
            _digest(digest, context=f"M6 basis {name}")
        expected_object_fingerprints = {
            "calibrated_model_object": canonical_fingerprint(self.model.as_dict()),
            "effective_case_object": canonical_fingerprint(self.case.as_dict()),
            "component_catalog_object": canonical_fingerprint(self.catalog.as_dict()),
        }
        if object_fingerprints != expected_object_fingerprints:
            raise ValueError("M6 effective object fingerprints do not match the objects")

        metadata = dict(self.metadata)
        if metadata != dict(_METADATA):
            raise ValueError("M6 basis metadata differs from the fixed source contract")
        object.__setattr__(
            self,
            "m5_artifact_sha256",
            MappingProxyType(artifact_hashes),
        )
        object.__setattr__(
            self,
            "effective_object_fingerprints",
            MappingProxyType(object_fingerprints),
        )
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "analysis_version": self.analysis_version,
            "model": self.model.as_dict(),
            "case": self.case.as_dict(),
            "component_catalog": self.catalog.as_dict(),
            "base_parameter_set_version": self.base_parameter_set_version,
            "derived_parameter_set_version": self.derived_parameter_set_version,
            "base_case_version": self.base_case_version,
            "derived_case_version": self.derived_case_version,
            "m5_pipeline_fingerprint": self.m5_pipeline_fingerprint,
            "m5_manifest_sha256": self.m5_manifest_sha256,
            "m5_manifest_fingerprint": self.m5_manifest_fingerprint,
            "m5_artifact_sha256": dict(self.m5_artifact_sha256),
            "effective_object_fingerprints": dict(
                self.effective_object_fingerprints
            ),
            "metadata": dict(self.metadata),
        }

    @property
    def analysis_basis_fingerprint(self) -> str:
        return canonical_fingerprint(self._fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {
            **self._fingerprint_payload(),
            "analysis_basis_fingerprint": self.analysis_basis_fingerprint,
        }


def _effective_case(result: M5PipelineResult, base_case: CaseConfig) -> CaseConfig:
    flash_overlay = result.overlays.flash_temperature_k
    raw_effective = flash_overlay["effective"]
    raw_baseline = flash_overlay["baseline"]
    if (
        isinstance(raw_effective, bool)
        or not isinstance(raw_effective, (int, float))
        or not math.isfinite(raw_effective)
        or raw_effective <= 0.0
    ):
        raise ValueError("M5 flash-temperature overlay is not finite and positive")
    if (
        isinstance(raw_baseline, bool)
        or not isinstance(raw_baseline, (int, float))
        or not math.isfinite(raw_baseline)
        or raw_baseline <= 0.0
    ):
        raise ValueError("M5 flash-temperature baseline is not finite and positive")
    if base_case.operating_conditions["flash_temperature_k"] != float(raw_baseline):
        raise ValueError("M5 flash-temperature baseline differs from the base case")
    conditions = dict(base_case.operating_conditions)
    conditions["flash_temperature_k"] = float(raw_effective)
    return replace(
        base_case,
        operating_conditions=MappingProxyType(conditions),
    )


def load_m6_basis(repo_root: Path) -> M6Basis:
    """Run M5 once, validate its formal suite, and build the effective M6 basis."""

    if not isinstance(repo_root, Path):
        raise TypeError("repo_root must be a pathlib.Path")
    root = repo_root.resolve()
    result = run_m5_pipeline(root)
    artifacts = _validate_m5_artifact_suite(root, result)

    case_path = _repo_file(
        root,
        result.alignment.paths.case_config,
        context="M6 base case path",
    )
    catalog_path = _repo_file(
        root,
        result.calibrated_model.component_catalog_path,
        context="M6 component catalog path",
    )
    if file_sha256(case_path) != result.fingerprints["case_file"]:
        raise ValueError("M6 base case file hash differs from the M5 pipeline")
    if file_sha256(catalog_path) != result.fingerprints["component_catalog_file"]:
        raise ValueError("M6 component catalog file hash differs from the M5 pipeline")

    base_case = load_case_config(case_path)
    catalog = load_component_catalog(catalog_path)
    if canonical_fingerprint(base_case.as_dict()) != result.fingerprints[
        "baseline_case_object"
    ]:
        raise ValueError("M6 base case object differs from the M5 pipeline")
    case = _effective_case(result, base_case)
    model = result.calibrated_model
    calibrated_model_fingerprint = canonical_fingerprint(model.as_dict())
    effective_case_fingerprint = canonical_fingerprint(case.as_dict())
    catalog_fingerprint = canonical_fingerprint(catalog.as_dict())
    if calibrated_model_fingerprint != result.fingerprints[
        "calibrated_model_object"
    ]:
        raise ValueError("M6 calibrated model differs from the M5 pipeline")
    if effective_case_fingerprint != result.fingerprints["effective_case_object"]:
        raise ValueError("M6 effective case differs from the M5 pipeline")
    if catalog_fingerprint != result.calibration.fingerprints[
        "component_catalog"
    ]:
        raise ValueError("M6 component catalog object differs from the M5 pipeline")
    validate_config_compatibility(
        model,
        case,
        software_version=SOFTWARE_VERSION,
        catalog=catalog,
    )

    return M6Basis(
        schema_version=_SCHEMA_VERSION,
        analysis_version=_ANALYSIS_VERSION,
        model=model,
        case=case,
        catalog=catalog,
        base_parameter_set_version=result.versions["base_parameter_set_version"],
        derived_parameter_set_version=result.versions[
            "calibrated_parameter_set_version"
        ],
        base_case_version=result.versions["base_case_version"],
        derived_case_version=result.versions["derived_case_version"],
        m5_pipeline_fingerprint=result.result_fingerprint,
        m5_manifest_sha256=artifacts.manifest_sha256,
        m5_manifest_fingerprint=artifacts.manifest_fingerprint,
        m5_artifact_sha256=artifacts.artifact_sha256,
        effective_object_fingerprints={
            "calibrated_model_object": calibrated_model_fingerprint,
            "effective_case_object": effective_case_fingerprint,
            "component_catalog_object": catalog_fingerprint,
        },
        metadata=_METADATA,
    )
