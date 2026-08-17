from __future__ import annotations

import json
from pathlib import Path

from petroleum_rto import __version__
from petroleum_rto.cdu.core.config import (
    canonical_fingerprint,
    input_bundle_fingerprint,
    load_case_config,
    load_component_catalog,
    load_model_config,
    load_scenario_config,
    validate_config_compatibility,
)


def test_complete_input_bundle_is_serializable_and_reproducible(repo_root: Path) -> None:
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")
    scenario = load_scenario_config(
        repo_root / "configs/scenarios/open_loop_baseline_v0.1.0.json"
    )
    catalog = load_component_catalog(repo_root / model.component_catalog_path)
    versions = validate_config_compatibility(
        model,
        case,
        software_version=__version__,
        catalog=catalog,
        scenario=scenario,
    )
    payloads = (model.as_dict(), case.as_dict(), scenario.as_dict(), catalog.as_dict())
    first = canonical_fingerprint(*payloads)
    second = canonical_fingerprint(*payloads)
    assert first == second
    assert len(first) == 64
    run_fingerprint = input_bundle_fingerprint(
        model,
        case,
        versions,
        catalog=catalog,
        scenario=scenario,
    )
    assert len(run_fingerprint) == 64
    json.dumps(
        {
            "versions": versions.as_dict(),
            "inputs": payloads,
            "fingerprint": first,
            "run_fingerprint": run_fingerprint,
        },
        ensure_ascii=False,
        allow_nan=False,
    )


def test_feed_step_scenario_is_version_compatible_and_relative_to_case(
    repo_root: Path,
) -> None:
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")
    catalog = load_component_catalog(repo_root / model.component_catalog_path)
    scenario = load_scenario_config(
        repo_root / "configs/scenarios/open_loop_feed_step_v0.1.0.json"
    )

    validate_config_compatibility(
        model,
        case,
        software_version=__version__,
        catalog=catalog,
        scenario=scenario,
    )

    assert scenario.duration_s == 7200.0
    assert scenario.time_step_s == 1.0
    assert len(scenario.events) == 1
    event = scenario.events[0]
    assert event["time_s"] == 600.0
    assert event["target"] == "fresh_feed_flow_kg_s"
    assert event["value"] == 1.05 * case.feed.mass_flow_kg_s
