from __future__ import annotations

import json
from importlib import resources as importlib_resources
from pathlib import Path
from types import MappingProxyType

import pytest

from petroleum_rto.cdu.core.config import ConfigurationError, ModelConfig
from petroleum_rto.cdu.repository import (
    canonicalize_cdu_resource_bytes,
    resolve_cdu_repository_path,
)
from petroleum_rto.cdu.runtime.resources import (
    M5RuntimeOverlay,
    RuntimeResourceError,
    RuntimeResourceSpec,
    get_runtime_resource_spec,
    list_runtime_resource_ids,
    load_m5_runtime_overlay,
    load_runtime_resource_bundle,
    read_runtime_resource_bytes,
    read_runtime_resource_json,
    read_runtime_resource_text,
    runtime_resource_sha256,
)

_RESOURCE_IDS = (
    "model.base",
    "catalog.components",
    "case.base",
    "control.pi",
    "scenario.open_loop.baseline",
    "scenario.open_loop.feed_step",
    "scenario.closed_loop.baseline",
    "scenario.closed_loop.feed_step",
    "validation.m6",
    "validation.m6_manifest",
    "overlay.m5",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _mutable_overlay() -> dict[str, object]:
    decoded: object = json.loads(read_runtime_resource_text("overlay.m5"))
    assert isinstance(decoded, dict)
    return decoded


def test_resource_registry_is_fixed_and_rejects_unknown_or_path_like_ids() -> None:
    assert list_runtime_resource_ids() == _RESOURCE_IDS
    assert get_runtime_resource_spec("model.base").kind == "model"
    with pytest.raises(KeyError, match="unknown runtime resource"):
        get_runtime_resource_spec("unknown.resource")
    with pytest.raises(RuntimeResourceError, match="path traversal"):
        get_runtime_resource_spec("../model.base")
    with pytest.raises(RuntimeResourceError, match="stay inside"):
        RuntimeResourceSpec(
            "bad.resource",
            "model",
            "bad.json",
            "../configs/model.json",
            "a" * 64,
        )
    with pytest.raises(RuntimeResourceError, match="POSIX separators"):
        RuntimeResourceSpec(
            "bad.resource",
            "model",
            "bad.json",
            "C:/outside/model.json",
            "a" * 64,
        )


def test_every_package_resource_is_byte_identical_to_its_repository_source() -> None:
    root = _repo_root()
    for resource_id in _RESOURCE_IDS:
        spec = get_runtime_resource_spec(resource_id)
        source_bytes = canonicalize_cdu_resource_bytes(
            resolve_cdu_repository_path(root, spec.source_path).read_bytes(),
            spec.source_path,
        )
        package_bytes = read_runtime_resource_bytes(resource_id)
        assert package_bytes == source_bytes
        assert runtime_resource_sha256(resource_id) == spec.expected_sha256


def test_resource_reader_rejects_package_byte_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BadPackageResource:
        def joinpath(self, *descendants: str) -> BadPackageResource:
            assert descendants
            return self

        def read_bytes(self) -> bytes:
            return b"{}"

    monkeypatch.setattr(
        importlib_resources,
        "files",
        lambda package: BadPackageResource(),
    )
    with pytest.raises(RuntimeResourceError, match="SHA-256 mismatch"):
        read_runtime_resource_bytes("model.base")


def test_json_resources_are_deeply_immutable() -> None:
    payload = read_runtime_resource_json("scenario.open_loop.feed_step")
    assert isinstance(payload, MappingProxyType)
    events = payload["events"]
    assert isinstance(events, tuple)
    with pytest.raises(TypeError):
        payload["name"] = "changed"  # type: ignore[index]


def test_m5_overlay_is_strict_and_is_not_a_model_config() -> None:
    overlay = load_m5_runtime_overlay()

    assert overlay.claim_scope == "case_alignment_only"
    assert overlay.wash_water_ratio_effective == pytest.approx(0.04654072620215898)
    assert overlay.flash_temperature_effective_k == 473.75
    assert overlay.column_cut_3_effective_k == 571.7046875
    assert overlay.column_cut_4_effective_k == 647.9546875
    assert overlay.overlay_fingerprint == overlay.declared_overlay_fingerprint
    assert M5RuntimeOverlay.from_mapping(overlay.as_dict()) == overlay
    assert overlay.m5_manifest_fingerprint == (
        "01c9cd02442e62da12f7263ce881dc566fdb25a721b0174ee4955544c5b124b1"
    )
    assert overlay.parameter_artifact_fingerprint == (
        "2b0309ca3142bdd9a7d5e641df723e40238f7537b394c1d86b08b7a1b6cdbeda"
    )
    with pytest.raises(ConfigurationError):
        ModelConfig.from_mapping(_mutable_overlay())


@pytest.mark.parametrize("mutation", ["unknown", "boolean", "traversal", "fingerprint"])
def test_m5_overlay_rejects_unknown_bool_path_traversal_and_tampering(
    mutation: str,
) -> None:
    payload = _mutable_overlay()
    if mutation == "unknown":
        payload["unknown"] = 1
    elif mutation == "boolean":
        operating = payload["case_operating_overrides"]
        assert isinstance(operating, dict)
        flash = operating["flash_temperature_k"]
        assert isinstance(flash, dict)
        flash["baseline"] = True
    elif mutation == "traversal":
        source = payload["source_evidence"]
        assert isinstance(source, dict)
        manifest = source["manifest"]
        assert isinstance(manifest, dict)
        manifest["path"] = "../manifest.json"
    else:
        payload["overlay_fingerprint"] = "f" * 64

    with pytest.raises(RuntimeResourceError):
        M5RuntimeOverlay.from_mapping(payload)


def test_bundle_reconstructs_the_exact_m5_effective_basis_and_m6_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    bundle = load_runtime_resource_bundle()

    assert bundle.base_model.equipment["desalter"]["wash_water_ratio"] == 0.04
    assert bundle.effective_model.equipment["desalter"]["wash_water_ratio"] == pytest.approx(
        0.04654072620215898
    )
    assert bundle.base_model.equipment["column"]["cut_points_k"] == (
        448.15,
        524.15,
        583.15,
        638.15,
    )
    assert bundle.effective_model.equipment["column"]["cut_points_k"] == (
        448.15,
        524.15,
        571.7046875,
        647.9546875,
    )
    assert bundle.base_case.operating_conditions["flash_temperature_k"] == 493.15
    assert bundle.effective_case.operating_conditions["flash_temperature_k"] == 473.75
    assert bundle.m6_basis.analysis_basis_fingerprint == (
        "4c12146b6fb14cb033b0e05f64e68093f28087482f55128aed5aa56c37dfffed"
    )
    assert bundle.validation_config.input_fingerprint == (
        "ccf4eeeb4eeb79fa0a0b9ea707c37dc36d3b8cc7886ba94c0b1a729c01b9d0ed"
    )
    assert bundle.m6_result_fingerprint == (
        "76c8e86262f96e517c76083677500621bcf777e3e7d2a6e3dd84b4a94e3370ba"
    )
    assert bundle.m6_manifest["status"] == "valid"
    assert tuple(bundle.resource_fingerprints) == _RESOURCE_IDS


def test_bundle_collections_and_m5_evidence_are_immutable() -> None:
    bundle = load_runtime_resource_bundle()

    assert isinstance(bundle.open_loop_scenarios, MappingProxyType)
    assert isinstance(bundle.closed_loop_scenarios, MappingProxyType)
    assert isinstance(bundle.resource_fingerprints, MappingProxyType)
    assert isinstance(bundle.m5_overlay.m5_artifact_sha256, MappingProxyType)
    with pytest.raises(TypeError):
        bundle.open_loop_scenarios["extra"] = bundle.open_loop_scenarios["baseline"]  # type: ignore[index]
