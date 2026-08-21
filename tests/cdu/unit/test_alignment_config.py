from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.calibration.alignment import (
    AlignmentConfig,
    BoundaryMeasurementSpec,
    load_alignment_config,
)
from petroleum_rto.cdu.core.config import ConfigurationError


def _load(repo_root: Path) -> AlignmentConfig:
    return load_alignment_config(
        repo_root / "configs/cdu/reconciliation/m5_case_20260604_v0.1.0.json"
    )


def test_repository_alignment_config_is_strict_and_deterministic(repo_root: Path) -> None:
    first = _load(repo_root)
    second = _load(repo_root)

    assert first.as_dict() == second.as_dict()
    assert first.fingerprint == second.fingerprint
    assert [item.stream_id for item in first.boundary_measurements] == [
        "fresh_feed",
        "wash_water",
        "gasoline",
        "kerosene",
        "light_diesel",
        "heavy_diesel",
        "residue",
    ]
    assert first.case_overlays.flash_temperature_observation_id.endswith("ti-1012")
    assert first.metadata["claim_scope"] == "case_alignment_only"


def test_engineering_scale_is_separate_from_catalog_display_resolution(
    repo_root: Path,
) -> None:
    config = _load(repo_root)
    fresh = config.boundary_measurements[0]
    wash = config.boundary_measurements[1]

    assert fresh.scale_for(407.3 / 3.6) == pytest.approx((407.3 / 3.6) * 0.01)
    assert wash.scale_for(18.97 / 3.6) == pytest.approx((18.97 / 3.6) * 0.05)
    assert "not instrument one-sigma" in config.metadata["engineering_scale_basis"]


def test_missing_reordered_or_duplicate_boundary_mapping_is_rejected(
    repo_root: Path,
) -> None:
    config = _load(repo_root)
    with pytest.raises(ConfigurationError, match="fixed seven-stream order"):
        replace(config, boundary_measurements=config.boundary_measurements[:-1])
    with pytest.raises(ConfigurationError, match="fixed seven-stream order"):
        replace(
            config,
            boundary_measurements=(
                config.boundary_measurements[1],
                config.boundary_measurements[0],
                *config.boundary_measurements[2:],
            ),
        )
    duplicate = replace(
        config.boundary_measurements[1],
        observation_id=config.boundary_measurements[0].observation_id,
    )
    with pytest.raises(ConfigurationError, match="duplicates"):
        replace(
            config,
            boundary_measurements=(
                config.boundary_measurements[0],
                duplicate,
                *config.boundary_measurements[2:],
            ),
        )


def test_unknown_stream_and_unsafe_path_are_rejected(repo_root: Path) -> None:
    config = _load(repo_root)
    with pytest.raises(ConfigurationError, match="unsupported measured"):
        BoundaryMeasurementSpec(
            stream_id="top_circulation",
            observation_id="obs-dcs-top-circulation-fi-1003",
            relative_engineering_scale=0.01,
            floor_kg_s=0.1,
        )
    with pytest.raises(ConfigurationError, match="repository-relative"):
        replace(config.paths, model_config="../outside.json")


def test_calibration_gold_path_and_claim_scope_are_frozen(repo_root: Path) -> None:
    config = _load(repo_root)
    assert config.artifacts.reconciled_case == (
        "data/gold/case_20260604_reconciled_v0.1.0.json"
    )
    assert config.artifacts.artifact_manifest == (
        "reports/modeling/M5_case_20260604_artifact_manifest_v0.1.0.json"
    )
    with pytest.raises(ConfigurationError, match="claim_scope"):
        replace(config, metadata={**config.metadata, "claim_scope": "field_validated"})
