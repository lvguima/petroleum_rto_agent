from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.cdu.calibration.calibration import apply_calibration_parameters
from petroleum_rto.cdu.calibration.config import (
    CALIBRATION_PARAMETER_PATHS,
    CALIBRATION_TARGETS,
    CalibrationConfig,
    load_calibration_config,
    validate_calibration_compatibility,
)
from petroleum_rto.cdu.core.config import (
    ConfigurationError,
    ModelConfig,
    load_case_config,
    load_component_catalog,
    load_json,
    load_model_config,
)


def _paths(repo_root: Path) -> tuple[Path, Path, Path]:
    return (
        repo_root / "configs/calibration/m5_case_20260604_v0.1.0.json",
        repo_root / "configs/models/cdu_mini_v0.1.0.json",
        repo_root / "configs/cases/case_20260604.json",
    )


def test_repository_calibration_config_freezes_identifiable_subset(
    repo_root: Path,
) -> None:
    config_path, model_path, case_path = _paths(repo_root)
    config = load_calibration_config(config_path)
    model = load_model_config(model_path)
    case = load_case_config(case_path)
    catalog = load_component_catalog(repo_root / model.component_catalog_path)

    assert tuple(item.path for item in config.parameters) == CALIBRATION_PARAMETER_PATHS
    assert tuple(item.product for item in config.targets) == CALIBRATION_TARGETS
    assert [item.lower_bound_k for item in config.parameters] == [568.15, 623.15]
    assert [item.upper_bound_k for item in config.parameters] == [598.15, 653.15]
    assert [item.prior_k for item in config.parameters] == [583.15, 638.15]
    assert [item.prior_scale_k for item in config.parameters] == [10.0, 10.0]
    assert config.data_reference.target_basis == "reconciled_net_boundary_flows"
    assert config.data_reference.target_unit == "kg/s"
    assert config.metadata["claim_scope"] == "case_alignment_only"
    assert config.fingerprint == load_calibration_config(config_path).fingerprint
    validate_calibration_compatibility(config, model, case, catalog)


@pytest.mark.parametrize("mutation", ["third_path", "wider_bound", "wrong_target"])
def test_calibration_config_rejects_scope_expansion(
    repo_root: Path,
    mutation: str,
) -> None:
    config_path, _, _ = _paths(repo_root)
    raw = load_json(config_path)
    parameters = raw["parameters"]
    targets = raw["targets"]
    assert isinstance(parameters, list)
    assert isinstance(targets, list)
    if mutation == "third_path":
        parameters.append(
            {
                "path": "column.separation_widths_k[2]",
                "lower_bound_k": 1.0,
                "upper_bound_k": 30.0,
                "prior_k": 16.0,
                "prior_scale_k": 5.0,
            }
        )
    elif mutation == "wider_bound":
        assert isinstance(parameters[0], dict)
        parameters[0]["lower_bound_k"] = 560.0
    else:
        assert isinstance(targets[0], dict)
        targets[0]["product"] = "gasoline"

    with pytest.raises(ConfigurationError):
        CalibrationConfig.from_mapping(raw)


def test_parameter_application_changes_only_two_cut_points_and_not_dynamics(
    repo_root: Path,
) -> None:
    config_path, model_path, _ = _paths(repo_root)
    config = load_calibration_config(config_path)
    model = load_model_config(model_path)
    original = model.as_dict()

    calibrated = apply_calibration_parameters(
        model,
        config,
        {
            "column.cut_points_k[2]": 575.0,
            "column.cut_points_k[3]": 646.0,
        },
    )

    original_column = model.equipment["column"]["cut_points_k"]
    calibrated_column = calibrated.equipment["column"]["cut_points_k"]
    assert original_column == (448.15, 524.15, 583.15, 638.15)
    assert calibrated_column == (448.15, 524.15, 575.0, 646.0)
    assert calibrated.dynamic == model.dynamic
    assert model.as_dict() == original
    assert calibrated.parameter_set_version == model.parameter_set_version

    with pytest.raises(ValueError, match="parameter keys differ"):
        apply_calibration_parameters(
            model,
            config,
            {
                "column.cut_points_k[2]": 575.0,
                "column.separation_widths_k[2]": 16.0,
            },
        )


def test_compatibility_rejects_base_model_prior_drift(repo_root: Path) -> None:
    config_path, model_path, case_path = _paths(repo_root)
    config = load_calibration_config(config_path)
    model_data = load_json(model_path)
    equipment = model_data["equipment"]
    assert isinstance(equipment, dict)
    column = equipment["column"]
    assert isinstance(column, dict)
    cut_points = column["cut_points_k"]
    assert isinstance(cut_points, list)
    cut_points[2] = 584.15
    drifted_model = ModelConfig.from_mapping(model_data)
    case = load_case_config(case_path)
    catalog = load_component_catalog(repo_root / drifted_model.component_catalog_path)

    with pytest.raises(ConfigurationError, match="no longer matches"):
        validate_calibration_compatibility(config, drifted_model, case, catalog)
