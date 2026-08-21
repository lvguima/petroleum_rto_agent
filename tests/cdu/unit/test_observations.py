from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from petroleum_rto.cdu.calibration.observations import (
    Observation,
    ObservationContractError,
    load_observation_catalog,
    load_source_manifest,
    observation_catalog_jsonl,
    source_manifest_jsonl,
    validate_observation_sources,
)

OBSERVATION_CATALOG = Path("data/cdu/catalog/cdu_observations_v0.1.0.jsonl")
SOURCE_MANIFEST = Path("data/cdu/catalog/cdu_sources_v0.1.0.jsonl")
EXPECTED_OBSERVATION_IDS = (
    "obs-dcs-flash-bottom-temperature-ti-1013-review",
    "obs-dcs-flash-feed-temperature-ti-1011-review",
    "obs-dcs-flash-pressure-pi-1002",
    "obs-dcs-flash-temperature-ti-1012",
    "obs-dcs-fresh-feed-overview",
    "obs-dcs-furnace-feed-temperature-ti-1110",
    "obs-dcs-gasoline-product",
    "obs-dcs-heavy-diesel-product",
    "obs-dcs-kerosene-product",
    "obs-dcs-light-diesel-product",
    "obs-dcs-overhead-reflux-fi-1010",
    "obs-dcs-residue-product",
    "obs-dcs-top-circulation-fi-1003",
    "obs-dcs-tower-top-pressure-pi-1003",
    "obs-dcs-wash-ratio-feed-fi-1018",
    "obs-dcs-wash-water-fic-1107",
    "obs-lab-gasoline-final-boiling-20260604",
    "obs-lab-kerosene-final-boiling-20260604",
    "obs-lab-mixed-diesel-t95-20260604",
)


def _catalog_by_id(repo_root: Path) -> dict[str, Observation]:
    observations = load_observation_catalog(repo_root / OBSERVATION_CATALOG, repo_root=repo_root)
    return {observation.id: observation for observation in observations}


def test_versioned_catalog_and_source_manifest_verify_real_sources(repo_root: Path) -> None:
    observations = load_observation_catalog(repo_root / OBSERVATION_CATALOG, repo_root=repo_root)
    sources = load_source_manifest(repo_root / SOURCE_MANIFEST, repo_root=repo_root)

    assert tuple(observation.id for observation in observations) == EXPECTED_OBSERVATION_IDS
    assert len(sources) == 8
    assert all(source.read_only for source in sources)
    validate_observation_sources(observations, sources, repo_root=repo_root)


def test_shipped_jsonl_files_are_canonical_and_deterministic(repo_root: Path) -> None:
    observation_path = repo_root / OBSERVATION_CATALOG
    source_path = repo_root / SOURCE_MANIFEST
    observations = load_observation_catalog(observation_path)
    sources = load_source_manifest(source_path)

    assert observation_catalog_jsonl(reversed(observations)) == observation_path.read_text(
        encoding="utf-8"
    )
    assert source_manifest_jsonl(reversed(sources)) == source_path.read_text(encoding="utf-8")


def test_six_net_boundary_flows_are_one_same_screen_alignment_group(repo_root: Path) -> None:
    by_id = _catalog_by_id(repo_root)
    ids = (
        "obs-dcs-fresh-feed-overview",
        "obs-dcs-gasoline-product",
        "obs-dcs-kerosene-product",
        "obs-dcs-light-diesel-product",
        "obs-dcs-heavy-diesel-product",
        "obs-dcs-residue-product",
    )
    values = [by_id[observation_id] for observation_id in ids]

    assert {value.alignment_group for value in values} == {"dcs-20260604-0916-overview"}
    assert {value.alignment_quality for value in values} == {"same_screen"}
    assert {value.source_id for value in values} == {"src-dcs-overview"}
    assert {value.observed_at.isoformat() for value in values} == {
        "2026-06-04T09:16:00+08:00"
    }
    assert values[0].variable_role == "net_boundary_input"
    assert {value.variable_role for value in values[1:]} == {"net_boundary_output"}
    assert all(value.usage == "data_coordination" for value in values)
    assert all(value.status == "candidate" for value in values)


def test_internal_circulation_is_not_mislabeled_as_reflux(repo_root: Path) -> None:
    by_id = _catalog_by_id(repo_root)
    circulation = by_id["obs-dcs-top-circulation-fi-1003"]
    reflux = by_id["obs-dcs-overhead-reflux-fi-1010"]

    assert circulation.instrument_tag == "FIC-1003"
    assert circulation.variable_role == "internal_circulation"
    assert circulation.status == "excluded"
    assert circulation.usage == "do_not_use"
    assert circulation.exclusion_reason is not None
    assert "not tower overhead reflux" in circulation.exclusion_reason
    assert reflux.instrument_tag == "FI-1010"
    assert reflux.variable_role == "internal_reflux"
    assert reflux.status == "reference_only"
    assert reflux.usage == "diagnostic_reference"


def test_wash_water_and_different_screen_denominator_are_weakly_aligned(
    repo_root: Path,
) -> None:
    by_id = _catalog_by_id(repo_root)
    wash_water = by_id["obs-dcs-wash-water-fic-1107"]
    denominator = by_id["obs-dcs-wash-ratio-feed-fi-1018"]

    assert wash_water.raw_value == pytest.approx(18.97)
    assert denominator.raw_value == pytest.approx(407.60)
    assert wash_water.raw_value / denominator.raw_value == pytest.approx(0.0465407262)
    assert wash_water.source_id != denominator.source_id
    assert wash_water.alignment_quality == denominator.alignment_quality == "weak"
    assert wash_water.variable_role == "auxiliary_input"
    assert wash_water.status == "candidate"
    assert wash_water.usage == "data_coordination"
    assert denominator.status == "reference_only"
    assert denominator.usage == "diagnostic_reference"
    assert denominator.exclusion_reason is not None
    assert "different screen" in denominator.exclusion_reason


def test_flash_and_furnace_temperature_roles_are_explicit(repo_root: Path) -> None:
    by_id = _catalog_by_id(repo_root)
    candidate = by_id["obs-dcs-flash-temperature-ti-1012"]
    feed_review = by_id["obs-dcs-flash-feed-temperature-ti-1011-review"]
    bottom_review = by_id["obs-dcs-flash-bottom-temperature-ti-1013-review"]
    furnace_feed = by_id["obs-dcs-furnace-feed-temperature-ti-1110"]

    assert candidate.si_value == pytest.approx(473.75)
    assert candidate.instrument_tag == "TI-1012"
    assert candidate.status == "reference_only"
    assert candidate.usage == "diagnostic_reference"
    assert feed_review.si_value == pytest.approx(474.65)
    assert bottom_review.si_value == pytest.approx(474.25)
    assert feed_review.status == bottom_review.status == "reference_only"
    assert "do not average" in (feed_review.exclusion_reason or "")
    assert "do not average" in (bottom_review.exclusion_reason or "")
    assert furnace_feed.si_value == pytest.approx(542.95)
    assert furnace_feed.variable == "furnace_feed_temperature"
    assert furnace_feed.status == "reference_only"
    assert furnace_feed.usage == "diagnostic_reference"


def test_flash_and_tower_pressures_retain_gauge_pressure_semantics(
    repo_root: Path,
) -> None:
    by_id = _catalog_by_id(repo_root)
    flash = by_id["obs-dcs-flash-pressure-pi-1002"]
    tower_top = by_id["obs-dcs-tower-top-pressure-pi-1003"]

    assert flash.raw_value == pytest.approx(0.082)
    assert flash.raw_unit == "MPa(g)"
    assert flash.si_value == pytest.approx(82_000.0)
    assert flash.si_unit == "Pa(g)"
    assert tower_top.raw_value == pytest.approx(0.0514)
    assert tower_top.si_value == pytest.approx(51_400.0)
    assert tower_top.si_unit == "Pa(g)"
    assert flash.status == tower_top.status == "reference_only"
    assert flash.usage == tower_top.usage == "diagnostic_reference"
    assert "Gauge-pressure diagnostic" in (flash.exclusion_reason or "")
    assert "absolute-pressure" in (tower_top.exclusion_reason or "")


def test_three_lab_anchors_are_exact_cells_but_weakly_aligned_to_dcs(repo_root: Path) -> None:
    by_id = _catalog_by_id(repo_root)
    ids = (
        "obs-lab-gasoline-final-boiling-20260604",
        "obs-lab-kerosene-final-boiling-20260604",
        "obs-lab-mixed-diesel-t95-20260604",
    )
    values = [by_id[observation_id] for observation_id in ids]

    assert [value.raw_value for value in values] == pytest.approx([167.2, 230.0, 371.0])
    assert [value.si_value for value in values] == pytest.approx([440.35, 503.15, 644.15])
    assert {value.alignment_quality for value in values} == {"weak"}
    assert {value.observed_at.isoformat() for value in values} == {
        "2026-06-04T08:00:00+08:00"
    }
    assert all(value.source_locator.startswith("xl/worksheets/sheet1.xml!") for value in values)
    assert all(value.status == "reference_only" for value in values)
    assert all(value.usage == "diagnostic_reference" for value in values)


def test_resolution_uncertainty_is_not_claimed_as_statistical_sigma(repo_root: Path) -> None:
    observations = load_observation_catalog(repo_root / OBSERVATION_CATALOG)

    assert all("not statistical 1-sigma" in value.uncertainty_basis for value in observations)
    assert all(value.usage != "calibration_target" for value in observations)


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("raw_unit", "unknown", "unsupported observation unit"),
        ("observed_at", "2026-06-04T09:16:00", "timezone"),
        ("uncertainty_si", 0.0, "positive"),
        ("source_path", "../outside.jpg", "repository-relative"),
        ("source_locator", "", "non-empty"),
        ("source_sha256", "A" * 64, "lowercase SHA-256"),
        ("si_value", 1.0, "does not match"),
    ],
)
def test_invalid_unit_time_error_and_source_evidence_are_rejected(
    repo_root: Path,
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    observation = load_observation_catalog(repo_root / OBSERVATION_CATALOG)[0]
    raw = copy.deepcopy(observation.as_dict())
    raw[field] = invalid_value

    with pytest.raises(ObservationContractError, match=message):
        Observation.from_mapping(raw)


def test_unknown_observation_field_is_rejected(repo_root: Path) -> None:
    raw = load_observation_catalog(repo_root / OBSERVATION_CATALOG)[0].as_dict()
    raw["unexpected"] = True

    with pytest.raises(ObservationContractError, match="unknown"):
        Observation.from_mapping(raw)


def test_hash_mismatch_against_real_source_is_rejected(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    raw = load_observation_catalog(repo_root / OBSERVATION_CATALOG)[0].as_dict()
    raw["source_sha256"] = "0" * 64
    catalog = tmp_path / "observations.jsonl"
    catalog.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ObservationContractError, match="source hash mismatch"):
        load_observation_catalog(catalog, repo_root=repo_root)
