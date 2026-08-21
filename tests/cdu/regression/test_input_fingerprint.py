from __future__ import annotations

from pathlib import Path

from petroleum_rto import __version__
from petroleum_rto.cdu.core.config import (
    input_bundle_fingerprint,
    load_case_config,
    load_component_catalog,
    load_model_config,
    load_scenario_config,
    validate_config_compatibility,
)
from petroleum_rto.cdu.repository import resolve_cdu_repository_path


def test_baseline_input_fingerprint_is_stable(repo_root: Path) -> None:
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
    fingerprint = input_bundle_fingerprint(
        model,
        case,
        versions,
        catalog=catalog,
        scenario=scenario,
    )
    assert fingerprint == "38227d4711c00576b636b39b5322379004bf70994569581685b3ef6c669a22c2"
