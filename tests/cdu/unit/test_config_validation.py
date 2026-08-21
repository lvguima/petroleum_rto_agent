from __future__ import annotations

import copy
from pathlib import Path

import pytest

from petroleum_rto import __version__
from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ConfigurationError,
    ModelConfig,
    ScenarioConfig,
    canonical_fingerprint,
    input_bundle_fingerprint,
    load_case_config,
    load_component_catalog,
    load_json,
    load_model_config,
    load_scenario_config,
    validate_config_compatibility,
)
from petroleum_rto.cdu.repository import resolve_cdu_repository_path


def test_repository_configs_construct_and_match(repo_root: Path) -> None:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    scenario = load_scenario_config(
        repo_root / "configs/cdu/scenarios/open_loop_baseline_v0.1.0.json"
    )
    catalog = load_component_catalog(
        resolve_cdu_repository_path(repo_root, model.component_catalog_path)
    )
    versions = validate_config_compatibility(
        model,
        case,
        software_version=__version__,
        catalog=catalog,
        scenario=scenario,
    )
    assert versions.model_version == model.model_version
    assert case.feed.mass_flow_kg_s == pytest.approx(407.3 * 1000.0 / 3600.0)
    assert case.operating_conditions["tower_top_pressure_pa"] == pytest.approx(152325.0)
    assert scenario.duration_s == pytest.approx(14400.0)
    assert len(
        input_bundle_fingerprint(
            model,
            case,
            versions,
            catalog=catalog,
            scenario=scenario,
        )
    ) == 64


def test_unknown_model_field_is_rejected(repo_root: Path) -> None:
    raw = load_json(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    raw["unexpected"] = 1
    with pytest.raises(ConfigurationError, match="unknown"):
        ModelConfig.from_mapping(raw)


def test_invalid_case_unit_is_rejected(repo_root: Path) -> None:
    raw = copy.deepcopy(load_json(repo_root / "configs/cdu/cases/case_20260604.json"))
    raw["feed"]["mass_flow"]["unit"] = "unknown"
    with pytest.raises(ValueError, match="unit"):
        CaseConfig.from_mapping(raw)


def test_invalid_nested_model_range_is_rejected(repo_root: Path) -> None:
    raw = copy.deepcopy(load_json(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json"))
    raw["equipment"]["pre_desalter_preheater"]["effectiveness"] = 1.5
    with pytest.raises(ConfigurationError, match="at most"):
        ModelConfig.from_mapping(raw)


def test_config_nested_values_are_immutable(repo_root: Path) -> None:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    with pytest.raises(TypeError):
        model.equipment["column"]["cut_points_k"][0] = 1.0  # type: ignore[index]
    with pytest.raises(TypeError):
        case.observations["product_mass_flows_t_h"]["gasoline"] = 0.0  # type: ignore[index]


def test_scenario_event_schema_is_strict(repo_root: Path) -> None:
    raw = load_json(repo_root / "configs/cdu/scenarios/open_loop_baseline_v0.1.0.json")
    raw["events"] = [
        {
            "time": {"value": 60.0, "unit": "s"},
            "target": "feed_multiplier",
            "value": 1.05,
            "unexpected": True,
        }
    ]
    with pytest.raises(ConfigurationError, match="unknown"):
        ScenarioConfig.from_mapping(raw)


def test_scenario_rejects_negative_command_value(repo_root: Path) -> None:
    raw = load_json(repo_root / "configs/cdu/scenarios/open_loop_baseline_v0.1.0.json")
    raw["events"] = [
        {
            "time": {"value": 60.0, "unit": "s"},
            "target": "fresh_feed_flow_kg_s",
            "value": -1.0,
        }
    ]

    with pytest.raises(ConfigurationError, match="value must be non-negative"):
        ScenarioConfig.from_mapping(raw)


@pytest.mark.parametrize(
    "metadata, message",
    [
        ({"synthetic": "false", "purpose": "test"}, "synthetic"),
        ({"purpose": "test"}, "synthetic"),
        ({"synthetic": "true"}, "purpose"),
        ({"synthetic": "true", "purpose": "   "}, "purpose"),
    ],
)
def test_scenario_requires_synthetic_origin_and_nonempty_purpose(
    repo_root: Path,
    metadata: dict[str, str],
    message: str,
) -> None:
    raw = load_json(repo_root / "configs/cdu/scenarios/open_loop_baseline_v0.1.0.json")
    raw["metadata"] = metadata

    with pytest.raises(ConfigurationError, match=message):
        ScenarioConfig.from_mapping(raw)


def test_version_mismatch_is_rejected(repo_root: Path) -> None:
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    raw = load_json(repo_root / "configs/cdu/cases/case_20260604.json")
    raw["model_version"] = "different-0.1.0"
    case = CaseConfig.from_mapping(raw)
    with pytest.raises(ConfigurationError, match="do not match"):
        validate_config_compatibility(model, case, software_version=__version__)


def test_canonical_fingerprint_is_order_independent() -> None:
    first = {"a": 1, "b": {"x": 2, "y": 3}}
    second = {"b": {"y": 3, "x": 2}, "a": 1}
    assert canonical_fingerprint(first) == canonical_fingerprint(second)
