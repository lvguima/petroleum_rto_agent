"""Self-contained, hash-checked runtime resources for installed M7 packages."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final, Literal, cast

from ..control.config import (
    ControlConfig,
    validate_control_compatibility,
)
from ..control.scenario import ClosedLoopScenarioConfig
from ..control.simulation import validate_closed_loop_scenario_compatibility
from ..core.config import (
    CaseConfig,
    ModelConfig,
    ScenarioConfig,
    canonical_fingerprint,
    validate_config_compatibility,
)
from ..properties.components import ComponentCatalog
from ..repository import canonicalize_cdu_resource_bytes, cdu_resource_bytes_sha256
from ..validation.basis import M6Basis
from ..validation.config import M6ValidationConfig
from .contracts import JsonValue

RuntimeResourceKind = Literal[
    "model",
    "component_catalog",
    "case",
    "control",
    "open_loop_scenario",
    "closed_loop_scenario",
    "validation_config",
    "validation_manifest",
    "runtime_overlay",
]

_DATA_PACKAGE: Final[str] = "petroleum_rto.cdu.runtime.data"
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "model",
        "component_catalog",
        "case",
        "control",
        "open_loop_scenario",
        "closed_loop_scenario",
        "validation_config",
        "validation_manifest",
        "runtime_overlay",
    }
)
_M5_ARTIFACT_NAMES: Final[tuple[str, ...]] = (
    "reconciled_case",
    "calibrated_parameters",
    "report_json",
    "report_markdown",
)
_M5_OVERLAY_VERSION: Final[str] = "m5-case-20260604-runtime-overlay-v0.1.0"
_M5_CLAIM_SCOPE: Final[str] = "case_alignment_only"
_M6_CONFIG_FINGERPRINT: Final[str] = (
    "ccf4eeeb4eeb79fa0a0b9ea707c37dc36d3b8cc7886ba94c0b1a729c01b9d0ed"
)


class RuntimeResourceError(ValueError):
    """Raised when a bundled resource is absent, malformed or not the frozen file."""


def _strict_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    context: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing or unknown:
        raise RuntimeResourceError(f"{context} fields differ; missing={missing}, unknown={unknown}")


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RuntimeResourceError(f"{context} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeResourceError(f"{context} must be a non-empty string")
    return value


def _identifier(value: object, *, context: str) -> str:
    if isinstance(value, str) and (".." in value or "/" in value or "\\" in value):
        raise RuntimeResourceError(f"{context} must not contain a path traversal")
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise RuntimeResourceError(f"{context} must be a non-empty identifier")
    return value


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RuntimeResourceError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _finite(value: object, *, context: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeResourceError(f"{context} must be numeric and must not be boolean")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeResourceError(f"{context} must be finite")
    if positive and number <= 0.0:
        raise RuntimeResourceError(f"{context} must be positive")
    return number


def _repo_relative_path(value: object, *, context: str) -> str:
    text = _text(value, context=context)
    if "\\" in text or ":" in text or text.startswith("~"):
        raise RuntimeResourceError(f"{context} must use POSIX separators")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise RuntimeResourceError(f"{context} must stay inside the repository")
    return text


def _deep_freeze_json(value: object, *, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeResourceError(f"{context} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeResourceError(f"{context} keys must be strings")
            copied[key] = _deep_freeze_json(item, context=f"{context}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, list):
        return tuple(
            _deep_freeze_json(item, context=f"{context}[{index}]")
            for index, item in enumerate(value)
        )
    raise RuntimeResourceError(f"{context} must contain only JSON-compatible values")


def _deep_thaw_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _deep_thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw_json(item) for item in value]
    return value


def _json_object(value: object, *, context: str) -> Mapping[str, JsonValue]:
    frozen = _deep_freeze_json(_mapping(value, context=context), context=context)
    if not isinstance(frozen, Mapping):  # pragma: no cover - mapping input
        raise RuntimeResourceError(f"{context} did not remain an object")
    return frozen


@dataclass(frozen=True)
class RuntimeResourceSpec:
    """One public logical resource mapped to one immutable package file."""

    resource_id: str
    kind: RuntimeResourceKind
    package_name: str
    source_path: str
    expected_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _identifier(self.resource_id, context="resource_id"),
        )
        if self.kind not in _RESOURCE_KINDS:
            raise RuntimeResourceError(f"unsupported runtime resource kind: {self.kind!r}")
        object.__setattr__(self, "kind", self.kind)
        package_name = _text(self.package_name, context="package_name")
        if (
            PurePosixPath(package_name).name != package_name
            or package_name in {".", ".."}
            or not _IDENTIFIER.fullmatch(package_name)
            or ".." in package_name
        ):
            raise RuntimeResourceError("package_name must be a plain file name")
        object.__setattr__(self, "package_name", package_name)
        object.__setattr__(
            self,
            "source_path",
            _repo_relative_path(self.source_path, context="source_path"),
        )
        object.__setattr__(
            self,
            "expected_sha256",
            _digest(self.expected_sha256, context="expected_sha256"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "package_name": self.package_name,
            "source_path": self.source_path,
            "expected_sha256": self.expected_sha256,
        }


_RESOURCE_SPECS: Final[tuple[RuntimeResourceSpec, ...]] = (
    RuntimeResourceSpec(
        "model.base",
        "model",
        "model_base.json",
        "configs/models/cdu_mini_v0.1.0.json",
        "0cb9ad6a62844b2e0cd26ab426e4f33e31cc71e6bfd0b674c34a002c1cb3af3d",
    ),
    RuntimeResourceSpec(
        "catalog.components",
        "component_catalog",
        "catalog_components.json",
        "configs/models/components_v0.1.0.json",
        "12d192eaeb7f3b30a6227ad5a3e908ec6f0e27c42bc173005dc050e34662918e",
    ),
    RuntimeResourceSpec(
        "case.base",
        "case",
        "case_base.json",
        "configs/cases/case_20260604.json",
        "0527a977baa6ca81c4d91c76299727dfd08556663d963269650166794426559c",
    ),
    RuntimeResourceSpec(
        "control.pi",
        "control",
        "control_pi.json",
        "configs/controllers/cdu_pi_v0.1.0.json",
        "ec09915607fa488ecce229aedb867cca6751573a7c7fe75cac27649da52f3ca7",
    ),
    RuntimeResourceSpec(
        "scenario.open_loop.baseline",
        "open_loop_scenario",
        "scenario_open_loop_baseline.json",
        "configs/scenarios/open_loop_baseline_v0.1.0.json",
        "6fb4f5d75e31a70a4ea5dd92453a5d5b8aa258bfa7169101b831ef28e300f4c0",
    ),
    RuntimeResourceSpec(
        "scenario.open_loop.feed_step",
        "open_loop_scenario",
        "scenario_open_loop_feed_step.json",
        "configs/scenarios/open_loop_feed_step_v0.1.0.json",
        "7a5bbff3a989a78cbb2402072802ed94cfda02b1020855b108ac67377feacb30",
    ),
    RuntimeResourceSpec(
        "scenario.closed_loop.baseline",
        "closed_loop_scenario",
        "scenario_closed_loop_baseline.json",
        "configs/scenarios/closed_loop_baseline_v0.1.0.json",
        "96eee3e422631a8aa2259a3608a2ab8cefbaf68f23b483330635e49158b026a9",
    ),
    RuntimeResourceSpec(
        "scenario.closed_loop.feed_step",
        "closed_loop_scenario",
        "scenario_closed_loop_feed_step.json",
        "configs/scenarios/closed_loop_feed_step_v0.1.0.json",
        "de60d0a2227dae283d6de0ade913c5d99058c025257cf3fabac114dca3c6b517",
    ),
    RuntimeResourceSpec(
        "validation.m6",
        "validation_config",
        "validation_m6.json",
        "configs/validation/m6_validation_v0.1.0.json",
        "3b5c329e345d5826a1980e897a64c595ed2607ffb1af74130a2e2bb75590a066",
    ),
    RuntimeResourceSpec(
        "validation.m6_manifest",
        "validation_manifest",
        "validation_m6_manifest.json",
        "reports/modeling/m6_validation_manifest_v0.1.0.json",
        "298934c7a34a2a1a807860019cc7c912471e362cdaac2242cc713062f7f79a6d",
    ),
    RuntimeResourceSpec(
        "overlay.m5",
        "runtime_overlay",
        "overlay_m5.json",
        "configs/runtime/m5_runtime_overlay_v0.1.0.json",
        "4f5d5f2cdf1a2196359389ae7284f6ffc7e0231fd7070635b2200664e9d5ce21",
    ),
)
_RESOURCE_BY_ID: Final[Mapping[str, RuntimeResourceSpec]] = MappingProxyType(
    {spec.resource_id: spec for spec in _RESOURCE_SPECS}
)


def list_runtime_resource_ids() -> tuple[str, ...]:
    """Return the stable logical resource IDs in deterministic registry order."""

    return tuple(spec.resource_id for spec in _RESOURCE_SPECS)


def get_runtime_resource_spec(resource_id: str) -> RuntimeResourceSpec:
    """Return a frozen resource descriptor and reject path-like IDs."""

    selected = _identifier(resource_id, context="resource_id")
    try:
        return _RESOURCE_BY_ID[selected]
    except KeyError as exc:
        raise KeyError(f"unknown runtime resource: {selected}") from exc


def read_runtime_resource_bytes(resource_id: str) -> bytes:
    """Read one bundled file and verify its frozen source-file SHA-256."""

    spec = get_runtime_resource_spec(resource_id)
    try:
        payload = resources.files(_DATA_PACKAGE).joinpath(spec.package_name).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise RuntimeResourceError(
            f"cannot read bundled runtime resource {resource_id!r}: {exc}"
        ) from exc
    payload = canonicalize_cdu_resource_bytes(payload, spec.source_path)
    actual = cdu_resource_bytes_sha256(payload, spec.source_path)
    if actual != spec.expected_sha256:
        raise RuntimeResourceError(f"bundled runtime resource {resource_id!r} SHA-256 mismatch")
    return payload


def read_runtime_resource_text(resource_id: str) -> str:
    """Read and verify one bundled UTF-8 text resource."""

    try:
        return read_runtime_resource_bytes(resource_id).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeResourceError(f"runtime resource {resource_id!r} is not valid UTF-8") from exc


def read_runtime_resource_json(resource_id: str) -> Mapping[str, JsonValue]:
    """Decode one verified JSON object and reject NaN/Infinity and non-object roots."""

    def reject_constant(value: str) -> None:
        raise RuntimeResourceError(f"runtime resource contains non-finite JSON value {value}")

    try:
        value: object = json.loads(
            read_runtime_resource_text(resource_id),
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise RuntimeResourceError(
            f"runtime resource {resource_id!r} is not valid JSON: {exc}"
        ) from exc
    return _json_object(value, context=f"runtime resource {resource_id}")


def _loader_mapping(resource_id: str) -> Mapping[str, object]:
    frozen = read_runtime_resource_json(resource_id)
    thawed = _deep_thaw_json(cast(JsonValue, frozen))
    return _mapping(thawed, context=f"runtime loader input {resource_id}")


def runtime_resource_sha256(resource_id: str) -> str:
    """Return the verified package bytes SHA-256 for one logical resource."""

    spec = get_runtime_resource_spec(resource_id)
    return cdu_resource_bytes_sha256(
        read_runtime_resource_bytes(resource_id),
        spec.source_path,
    )


@dataclass(frozen=True)
class M5RuntimeOverlay:
    """Strict portable form of M5's wash, flash and two-cut effective basis."""

    schema_version: str
    overlay_version: str
    claim_scope: str
    base_model_config_version: str
    model_version: str
    base_parameter_set_version: str
    derived_parameter_set_version: str
    base_case_version: str
    derived_case_version: str
    m5_manifest_path: str
    m5_manifest_sha256: str
    m5_manifest_fingerprint: str
    m5_artifact_paths: Mapping[str, str]
    m5_artifact_sha256: Mapping[str, str]
    parameter_artifact_fingerprint: str
    pipeline_result_fingerprint: str
    calibrated_model_object_fingerprint: str
    effective_case_object_fingerprint: str
    component_catalog_object_fingerprint: str
    analysis_basis_fingerprint: str
    flash_temperature_baseline_k: float
    flash_temperature_effective_k: float
    wash_water_ratio_baseline: float
    wash_water_ratio_effective: float
    column_cut_3_baseline_k: float
    column_cut_3_effective_k: float
    column_cut_4_baseline_k: float
    column_cut_4_effective_k: float
    metadata: Mapping[str, str]
    declared_overlay_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise RuntimeResourceError("M5 runtime overlay schema_version differs")
        if self.overlay_version != _M5_OVERLAY_VERSION:
            raise RuntimeResourceError("M5 runtime overlay version differs")
        if self.claim_scope != _M5_CLAIM_SCOPE:
            raise RuntimeResourceError("M5 runtime overlay claim_scope differs")
        expected_versions = {
            "base_model_config_version": "cdu-mini-config-0.1.0",
            "model_version": "cdu-reduced-0.1.0",
            "base_parameter_set_version": "cdu-parameters-0.1.0",
            "derived_parameter_set_version": "cdu-parameters-m5-case20260604-v0.1.0",
            "base_case_version": "case-20260604-v0.1.0",
            "derived_case_version": "case-20260604-m5-aligned-v0.1.0",
        }
        if any(getattr(self, name) != expected for name, expected in expected_versions.items()):
            raise RuntimeResourceError("M5 runtime overlay lineage versions differ")
        object.__setattr__(
            self,
            "m5_manifest_path",
            _repo_relative_path(self.m5_manifest_path, context="M5 manifest path"),
        )
        for name in (
            "m5_manifest_sha256",
            "m5_manifest_fingerprint",
            "parameter_artifact_fingerprint",
            "pipeline_result_fingerprint",
            "calibrated_model_object_fingerprint",
            "effective_case_object_fingerprint",
            "component_catalog_object_fingerprint",
            "analysis_basis_fingerprint",
            "declared_overlay_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), context=name),
            )
        paths = dict(self.m5_artifact_paths)
        hashes = dict(self.m5_artifact_sha256)
        if tuple(paths) != _M5_ARTIFACT_NAMES or tuple(hashes) != _M5_ARTIFACT_NAMES:
            raise RuntimeResourceError("M5 runtime overlay artifact set differs")
        object.__setattr__(
            self,
            "m5_artifact_paths",
            MappingProxyType(
                {
                    name: _repo_relative_path(paths[name], context=f"M5 artifact {name} path")
                    for name in _M5_ARTIFACT_NAMES
                }
            ),
        )
        object.__setattr__(
            self,
            "m5_artifact_sha256",
            MappingProxyType(
                {
                    name: _digest(hashes[name], context=f"M5 artifact {name} SHA-256")
                    for name in _M5_ARTIFACT_NAMES
                }
            ),
        )
        for name in (
            "flash_temperature_baseline_k",
            "flash_temperature_effective_k",
            "wash_water_ratio_baseline",
            "wash_water_ratio_effective",
            "column_cut_3_baseline_k",
            "column_cut_3_effective_k",
            "column_cut_4_baseline_k",
            "column_cut_4_effective_k",
        ):
            object.__setattr__(
                self, name, _finite(getattr(self, name), context=name, positive=True)
            )
        metadata = dict(self.metadata)
        expected_metadata_keys = {
            "purpose",
            "source_basis",
            "precision_claim",
            "prohibited_claim",
        }
        if set(metadata) != expected_metadata_keys or any(
            not isinstance(key, str) or not isinstance(value, str) or not value.strip()
            for key, value in metadata.items()
        ):
            raise RuntimeResourceError("M5 runtime overlay metadata differs")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))
        if self.declared_overlay_fingerprint != self.overlay_fingerprint:
            raise RuntimeResourceError("M5 runtime overlay fingerprint mismatch")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> M5RuntimeOverlay:
        _strict_keys(
            value,
            required={
                "artifact_type",
                "schema_version",
                "overlay_version",
                "claim_scope",
                "versions",
                "source_evidence",
                "case_operating_overrides",
                "calibrated_parameter_overrides",
                "metadata",
                "overlay_fingerprint",
            },
            context="M5 runtime overlay",
        )
        if value["artifact_type"] != "M5_runtime_overlay":
            raise RuntimeResourceError("M5 runtime overlay artifact_type differs")
        versions = _mapping(value["versions"], context="overlay versions")
        _strict_keys(
            versions,
            required={
                "base_model_config_version",
                "model_version",
                "base_parameter_set_version",
                "derived_parameter_set_version",
                "base_case_version",
                "derived_case_version",
            },
            context="overlay versions",
        )
        source = _mapping(value["source_evidence"], context="source_evidence")
        _strict_keys(
            source,
            required={
                "manifest",
                "artifacts",
                "pipeline_result_fingerprint",
                "calibrated_model_object_fingerprint",
                "effective_case_object_fingerprint",
                "component_catalog_object_fingerprint",
                "analysis_basis_fingerprint",
            },
            context="source_evidence",
        )
        manifest = _mapping(source["manifest"], context="source_evidence.manifest")
        _strict_keys(
            manifest,
            required={"path", "sha256", "artifact_fingerprint"},
            context="source_evidence.manifest",
        )
        artifacts = _mapping(source["artifacts"], context="source_evidence.artifacts")
        if tuple(artifacts) != _M5_ARTIFACT_NAMES:
            raise RuntimeResourceError("source_evidence.artifacts order or set differs")
        paths: dict[str, str] = {}
        hashes: dict[str, str] = {}
        parameter_fingerprint: str | None = None
        for name in _M5_ARTIFACT_NAMES:
            entry = _mapping(artifacts[name], context=f"artifact {name}")
            required = {"path", "sha256"}
            if name == "calibrated_parameters":
                required.add("artifact_fingerprint")
            _strict_keys(entry, required=required, context=f"artifact {name}")
            paths[name] = _repo_relative_path(entry["path"], context=f"artifact {name} path")
            hashes[name] = _digest(entry["sha256"], context=f"artifact {name} SHA-256")
            if name == "calibrated_parameters":
                parameter_fingerprint = _digest(
                    entry["artifact_fingerprint"],
                    context="parameter artifact fingerprint",
                )
        if parameter_fingerprint is None:  # pragma: no cover - fixed artifact set
            raise RuntimeResourceError("parameter artifact fingerprint is missing")

        operating = _mapping(
            value["case_operating_overrides"],
            context="case_operating_overrides",
        )
        _strict_keys(
            operating,
            required={"flash_temperature_k", "wash_water_ratio"},
            context="case_operating_overrides",
        )
        flash = _mapping(operating["flash_temperature_k"], context="flash override")
        wash = _mapping(operating["wash_water_ratio"], context="wash override")
        for name, item in (("flash", flash), ("wash", wash)):
            _strict_keys(
                item,
                required={"baseline", "effective", "classification"},
                context=f"{name} override",
            )
            if item["classification"] != "case_input_weak_time_alignment":
                raise RuntimeResourceError(f"{name} override classification differs")
        calibrated = _mapping(
            value["calibrated_parameter_overrides"],
            context="calibrated_parameter_overrides",
        )
        cut_paths = ("column.cut_points_k[2]", "column.cut_points_k[3]")
        if tuple(calibrated) != cut_paths:
            raise RuntimeResourceError("calibrated parameter whitelist differs")
        cuts: list[tuple[float, float]] = []
        for path in cut_paths:
            item = _mapping(calibrated[path], context=f"calibrated override {path}")
            _strict_keys(
                item,
                required={"baseline", "effective", "classification"},
                context=f"calibrated override {path}",
            )
            if item["classification"] != "M5_calibrated_parameter":
                raise RuntimeResourceError(f"calibrated override {path} classification differs")
            cuts.append(
                (
                    _finite(item["baseline"], context=f"{path} baseline", positive=True),
                    _finite(item["effective"], context=f"{path} effective", positive=True),
                )
            )
        metadata_raw = _mapping(value["metadata"], context="overlay metadata")
        metadata = {
            key: _text(item, context=f"overlay metadata.{key}")
            for key, item in metadata_raw.items()
        }
        return cls(
            schema_version=_text(value["schema_version"], context="schema_version"),
            overlay_version=_identifier(value["overlay_version"], context="overlay_version"),
            claim_scope=_identifier(value["claim_scope"], context="claim_scope"),
            base_model_config_version=_identifier(
                versions["base_model_config_version"],
                context="base_model_config_version",
            ),
            model_version=_identifier(versions["model_version"], context="model_version"),
            base_parameter_set_version=_identifier(
                versions["base_parameter_set_version"],
                context="base_parameter_set_version",
            ),
            derived_parameter_set_version=_identifier(
                versions["derived_parameter_set_version"],
                context="derived_parameter_set_version",
            ),
            base_case_version=_identifier(
                versions["base_case_version"], context="base_case_version"
            ),
            derived_case_version=_identifier(
                versions["derived_case_version"], context="derived_case_version"
            ),
            m5_manifest_path=_repo_relative_path(manifest["path"], context="M5 manifest path"),
            m5_manifest_sha256=_digest(manifest["sha256"], context="M5 manifest SHA-256"),
            m5_manifest_fingerprint=_digest(
                manifest["artifact_fingerprint"],
                context="M5 manifest fingerprint",
            ),
            m5_artifact_paths=paths,
            m5_artifact_sha256=hashes,
            parameter_artifact_fingerprint=parameter_fingerprint,
            pipeline_result_fingerprint=_digest(
                source["pipeline_result_fingerprint"],
                context="pipeline_result_fingerprint",
            ),
            calibrated_model_object_fingerprint=_digest(
                source["calibrated_model_object_fingerprint"],
                context="calibrated_model_object_fingerprint",
            ),
            effective_case_object_fingerprint=_digest(
                source["effective_case_object_fingerprint"],
                context="effective_case_object_fingerprint",
            ),
            component_catalog_object_fingerprint=_digest(
                source["component_catalog_object_fingerprint"],
                context="component_catalog_object_fingerprint",
            ),
            analysis_basis_fingerprint=_digest(
                source["analysis_basis_fingerprint"],
                context="analysis_basis_fingerprint",
            ),
            flash_temperature_baseline_k=_finite(
                flash["baseline"], context="flash baseline", positive=True
            ),
            flash_temperature_effective_k=_finite(
                flash["effective"], context="flash effective", positive=True
            ),
            wash_water_ratio_baseline=_finite(
                wash["baseline"], context="wash baseline", positive=True
            ),
            wash_water_ratio_effective=_finite(
                wash["effective"], context="wash effective", positive=True
            ),
            column_cut_3_baseline_k=cuts[0][0],
            column_cut_3_effective_k=cuts[0][1],
            column_cut_4_baseline_k=cuts[1][0],
            column_cut_4_effective_k=cuts[1][1],
            metadata=metadata,
            declared_overlay_fingerprint=_digest(
                value["overlay_fingerprint"], context="overlay_fingerprint"
            ),
        )

    def _payload(self) -> dict[str, object]:
        return {
            "artifact_type": "M5_runtime_overlay",
            "schema_version": self.schema_version,
            "overlay_version": self.overlay_version,
            "claim_scope": self.claim_scope,
            "versions": {
                "base_model_config_version": self.base_model_config_version,
                "model_version": self.model_version,
                "base_parameter_set_version": self.base_parameter_set_version,
                "derived_parameter_set_version": self.derived_parameter_set_version,
                "base_case_version": self.base_case_version,
                "derived_case_version": self.derived_case_version,
            },
            "source_evidence": {
                "manifest": {
                    "path": self.m5_manifest_path,
                    "sha256": self.m5_manifest_sha256,
                    "artifact_fingerprint": self.m5_manifest_fingerprint,
                },
                "artifacts": {
                    name: {
                        "path": self.m5_artifact_paths[name],
                        "sha256": self.m5_artifact_sha256[name],
                        **(
                            {"artifact_fingerprint": (self.parameter_artifact_fingerprint)}
                            if name == "calibrated_parameters"
                            else {}
                        ),
                    }
                    for name in _M5_ARTIFACT_NAMES
                },
                "pipeline_result_fingerprint": self.pipeline_result_fingerprint,
                "calibrated_model_object_fingerprint": (self.calibrated_model_object_fingerprint),
                "effective_case_object_fingerprint": (self.effective_case_object_fingerprint),
                "component_catalog_object_fingerprint": (self.component_catalog_object_fingerprint),
                "analysis_basis_fingerprint": self.analysis_basis_fingerprint,
            },
            "case_operating_overrides": {
                "flash_temperature_k": {
                    "baseline": self.flash_temperature_baseline_k,
                    "effective": self.flash_temperature_effective_k,
                    "classification": "case_input_weak_time_alignment",
                },
                "wash_water_ratio": {
                    "baseline": self.wash_water_ratio_baseline,
                    "effective": self.wash_water_ratio_effective,
                    "classification": "case_input_weak_time_alignment",
                },
            },
            "calibrated_parameter_overrides": {
                "column.cut_points_k[2]": {
                    "baseline": self.column_cut_3_baseline_k,
                    "effective": self.column_cut_3_effective_k,
                    "classification": "M5_calibrated_parameter",
                },
                "column.cut_points_k[3]": {
                    "baseline": self.column_cut_4_baseline_k,
                    "effective": self.column_cut_4_effective_k,
                    "classification": "M5_calibrated_parameter",
                },
            },
            "metadata": dict(self.metadata),
        }

    @property
    def overlay_fingerprint(self) -> str:
        return canonical_fingerprint(self._payload())

    def as_dict(self) -> dict[str, object]:
        return {**self._payload(), "overlay_fingerprint": self.overlay_fingerprint}

    def apply(self, model: ModelConfig, case: CaseConfig) -> tuple[ModelConfig, CaseConfig]:
        """Apply only the four frozen values after checking every baseline and lineage."""

        if model.config_version != self.base_model_config_version:
            raise RuntimeResourceError("base model config version differs from M5 overlay")
        if model.model_version != self.model_version or case.model_version != self.model_version:
            raise RuntimeResourceError("base model version differs from M5 overlay")
        if (
            model.parameter_set_version != self.base_parameter_set_version
            or case.parameter_set_version != self.base_parameter_set_version
        ):
            raise RuntimeResourceError("base parameter-set version differs from M5 overlay")
        if case.case_version != self.base_case_version:
            raise RuntimeResourceError("base case version differs from M5 overlay")

        model_data = model.as_dict()
        equipment = cast(dict[str, object], model_data["equipment"])
        desalter = cast(dict[str, object], equipment["desalter"])
        column = cast(dict[str, object], equipment["column"])
        cuts = cast(list[object], column["cut_points_k"])
        if desalter["wash_water_ratio"] != self.wash_water_ratio_baseline:
            raise RuntimeResourceError("base wash-water ratio differs from M5 overlay")
        if cuts[2] != self.column_cut_3_baseline_k or cuts[3] != self.column_cut_4_baseline_k:
            raise RuntimeResourceError("base column cut points differ from M5 overlay")
        desalter["wash_water_ratio"] = self.wash_water_ratio_effective
        cuts[2] = self.column_cut_3_effective_k
        cuts[3] = self.column_cut_4_effective_k
        effective_model = ModelConfig.from_mapping(model_data)

        conditions = dict(case.operating_conditions)
        if conditions["flash_temperature_k"] != self.flash_temperature_baseline_k:
            raise RuntimeResourceError("base flash temperature differs from M5 overlay")
        conditions["flash_temperature_k"] = self.flash_temperature_effective_k
        effective_case = replace(case, operating_conditions=MappingProxyType(conditions))
        if (
            canonical_fingerprint(effective_model.as_dict())
            != self.calibrated_model_object_fingerprint
        ):
            raise RuntimeResourceError("effective model fingerprint differs from M5 overlay")
        if (
            canonical_fingerprint(effective_case.as_dict())
            != self.effective_case_object_fingerprint
        ):
            raise RuntimeResourceError("effective case fingerprint differs from M5 overlay")
        return effective_model, effective_case


def load_m5_runtime_overlay() -> M5RuntimeOverlay:
    """Load the packaged M5 runtime overlay; it is not a ModelConfig artifact."""

    return M5RuntimeOverlay.from_mapping(_loader_mapping("overlay.m5"))


@dataclass(frozen=True)
class RuntimeResourceBundle:
    """Validated base/effective inputs and all built-in runtime scenarios."""

    base_model: ModelConfig
    effective_model: ModelConfig
    base_case: CaseConfig
    effective_case: CaseConfig
    catalog: ComponentCatalog
    control: ControlConfig
    open_loop_scenarios: Mapping[str, ScenarioConfig]
    closed_loop_scenarios: Mapping[str, ClosedLoopScenarioConfig]
    validation_config: M6ValidationConfig
    m6_manifest: Mapping[str, JsonValue]
    m6_result_fingerprint: str
    m6_basis: M6Basis
    m5_overlay: M5RuntimeOverlay
    resource_fingerprints: Mapping[str, str]

    def __post_init__(self) -> None:
        expected_types = (
            (self.base_model, ModelConfig, "base_model"),
            (self.effective_model, ModelConfig, "effective_model"),
            (self.base_case, CaseConfig, "base_case"),
            (self.effective_case, CaseConfig, "effective_case"),
            (self.catalog, ComponentCatalog, "catalog"),
            (self.control, ControlConfig, "control"),
            (self.validation_config, M6ValidationConfig, "validation_config"),
            (self.m6_basis, M6Basis, "m6_basis"),
            (self.m5_overlay, M5RuntimeOverlay, "m5_overlay"),
        )
        for value, expected_type, name in expected_types:
            if not isinstance(value, expected_type):
                raise TypeError(f"runtime bundle {name} has the wrong type")
        open_scenarios = dict(self.open_loop_scenarios)
        closed_scenarios = dict(self.closed_loop_scenarios)
        if tuple(open_scenarios) != ("baseline", "feed_step") or any(
            not isinstance(value, ScenarioConfig) for value in open_scenarios.values()
        ):
            raise RuntimeResourceError("runtime open-loop scenario set differs")
        if tuple(closed_scenarios) != ("baseline", "feed_step") or any(
            not isinstance(value, ClosedLoopScenarioConfig) for value in closed_scenarios.values()
        ):
            raise RuntimeResourceError("runtime closed-loop scenario set differs")
        object.__setattr__(
            self,
            "open_loop_scenarios",
            MappingProxyType(open_scenarios),
        )
        object.__setattr__(
            self,
            "closed_loop_scenarios",
            MappingProxyType(closed_scenarios),
        )
        object.__setattr__(
            self,
            "m6_manifest",
            _json_object(self.m6_manifest, context="M6 manifest"),
        )
        object.__setattr__(
            self,
            "m6_result_fingerprint",
            _digest(self.m6_result_fingerprint, context="M6 result fingerprint"),
        )
        fingerprints = dict(self.resource_fingerprints)
        if tuple(fingerprints) != list_runtime_resource_ids():
            raise RuntimeResourceError("runtime bundle resource fingerprint set differs")
        object.__setattr__(
            self,
            "resource_fingerprints",
            MappingProxyType(
                {
                    name: _digest(value, context=f"resource fingerprint {name}")
                    for name, value in fingerprints.items()
                }
            ),
        )


def _validate_m6_manifest(value: Mapping[str, JsonValue]) -> str:
    raw = cast(Mapping[str, object], value)
    required = {
        "artifact_type",
        "artifacts",
        "claim_scope",
        "completion_passed",
        "manifest_fingerprint",
        "manifest_path",
        "manifest_version",
        "metadata",
        "result_fingerprint",
        "schema_version",
        "status",
        "validation_status",
    }
    _strict_keys(raw, required=required, context="M6 manifest")
    expected = {
        "artifact_type": "M6_validation_artifact_suite_manifest",
        "claim_scope": "engineering_validation_only",
        "completion_passed": True,
        "manifest_path": "reports/modeling/m6_validation_manifest_v0.1.0.json",
        "manifest_version": "m6-validation-artifacts-v0.1.0",
        "schema_version": "1.0.0",
        "status": "valid",
        "validation_status": "success",
    }
    mismatches = sorted(
        name for name, expected_value in expected.items() if raw[name] != expected_value
    )
    if mismatches:
        raise RuntimeResourceError("M6 manifest fields differ: " + ", ".join(mismatches))
    declared = _digest(raw["manifest_fingerprint"], context="M6 manifest fingerprint")
    unsigned = {
        key: _deep_thaw_json(item) for key, item in value.items() if key != "manifest_fingerprint"
    }
    if canonical_fingerprint(unsigned) != declared:
        raise RuntimeResourceError("M6 manifest fingerprint mismatch")
    return _digest(raw["result_fingerprint"], context="M6 result fingerprint")


def load_runtime_resource_bundle() -> RuntimeResourceBundle:
    """Load and cross-check every built-in input without requiring a repository root."""

    base_model = ModelConfig.from_mapping(_loader_mapping("model.base"))
    catalog = ComponentCatalog.from_mapping(_loader_mapping("catalog.components"))
    base_case = CaseConfig.from_mapping(_loader_mapping("case.base"))
    control = ControlConfig.from_mapping(_loader_mapping("control.pi"))
    overlay = load_m5_runtime_overlay()
    effective_model, effective_case = overlay.apply(base_model, base_case)
    if canonical_fingerprint(catalog.as_dict()) != overlay.component_catalog_object_fingerprint:
        raise RuntimeResourceError("component catalog fingerprint differs from M5 overlay")
    validate_config_compatibility(
        effective_model,
        effective_case,
        software_version="0.1.0",
        catalog=catalog,
    )
    validate_control_compatibility(control, effective_model, effective_case)

    open_scenarios = {
        "baseline": ScenarioConfig.from_mapping(_loader_mapping("scenario.open_loop.baseline")),
        "feed_step": ScenarioConfig.from_mapping(_loader_mapping("scenario.open_loop.feed_step")),
    }
    for open_scenario in open_scenarios.values():
        validate_config_compatibility(
            effective_model,
            effective_case,
            software_version="0.1.0",
            catalog=catalog,
            scenario=open_scenario,
        )
    closed_scenarios = {
        "baseline": ClosedLoopScenarioConfig.from_mapping(
            _loader_mapping("scenario.closed_loop.baseline")
        ),
        "feed_step": ClosedLoopScenarioConfig.from_mapping(
            _loader_mapping("scenario.closed_loop.feed_step")
        ),
    }
    for closed_scenario in closed_scenarios.values():
        validate_closed_loop_scenario_compatibility(control, closed_scenario)

    validation_config = M6ValidationConfig.from_mapping(_loader_mapping("validation.m6"))
    if validation_config.input_fingerprint != _M6_CONFIG_FINGERPRINT:
        raise RuntimeResourceError("M6 validation config fingerprint differs")
    if (
        validation_config.model_version != overlay.model_version
        or validation_config.model_config_version != overlay.base_model_config_version
        or validation_config.base_parameter_set_version != overlay.base_parameter_set_version
        or validation_config.derived_parameter_set_version != overlay.derived_parameter_set_version
        or validation_config.base_case_version != overlay.base_case_version
        or validation_config.derived_case_version != overlay.derived_case_version
        or validation_config.control_version != control.control_version
    ):
        raise RuntimeResourceError("M6 validation lineage differs from runtime resources")

    m6_basis = M6Basis(
        schema_version="1.0.0",
        analysis_version=validation_config.analysis_basis_version,
        model=effective_model,
        case=effective_case,
        catalog=catalog,
        base_parameter_set_version=overlay.base_parameter_set_version,
        derived_parameter_set_version=overlay.derived_parameter_set_version,
        base_case_version=overlay.base_case_version,
        derived_case_version=overlay.derived_case_version,
        m5_pipeline_fingerprint=overlay.pipeline_result_fingerprint,
        m5_manifest_sha256=overlay.m5_manifest_sha256,
        m5_manifest_fingerprint=overlay.m5_manifest_fingerprint,
        m5_artifact_sha256=overlay.m5_artifact_sha256,
        effective_object_fingerprints={
            "calibrated_model_object": overlay.calibrated_model_object_fingerprint,
            "effective_case_object": overlay.effective_case_object_fingerprint,
            "component_catalog_object": overlay.component_catalog_object_fingerprint,
        },
        metadata={
            "synthetic": "true",
            "data_origin": "M6_synthetic_validation",
            "claim_scope": "engineering_validation_only",
        },
    )
    if m6_basis.analysis_basis_fingerprint != overlay.analysis_basis_fingerprint:
        raise RuntimeResourceError("portable M6 basis fingerprint differs from M5 overlay")

    m6_manifest = read_runtime_resource_json("validation.m6_manifest")
    m6_result_fingerprint = _validate_m6_manifest(m6_manifest)
    resource_fingerprints = {
        resource_id: runtime_resource_sha256(resource_id)
        for resource_id in list_runtime_resource_ids()
    }
    return RuntimeResourceBundle(
        base_model=base_model,
        effective_model=effective_model,
        base_case=base_case,
        effective_case=effective_case,
        catalog=catalog,
        control=control,
        open_loop_scenarios=open_scenarios,
        closed_loop_scenarios=closed_scenarios,
        validation_config=validation_config,
        m6_manifest=m6_manifest,
        m6_result_fingerprint=m6_result_fingerprint,
        m6_basis=m6_basis,
        m5_overlay=overlay,
        resource_fingerprints=resource_fingerprints,
    )


__all__ = [
    "M5RuntimeOverlay",
    "RuntimeResourceBundle",
    "RuntimeResourceError",
    "RuntimeResourceKind",
    "RuntimeResourceSpec",
    "get_runtime_resource_spec",
    "list_runtime_resource_ids",
    "load_m5_runtime_overlay",
    "load_runtime_resource_bundle",
    "read_runtime_resource_bytes",
    "read_runtime_resource_json",
    "read_runtime_resource_text",
    "runtime_resource_sha256",
]
