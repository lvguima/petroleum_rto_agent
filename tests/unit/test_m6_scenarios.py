from __future__ import annotations

import math
from pathlib import Path

import pytest

from petroleum_rto.cdu.core.config import (
    canonical_fingerprint,
    load_case_config,
    load_model_config,
)
from petroleum_rto.cdu.validation.scenarios import (
    apply_steady_factor,
    dynamic_command_for_factor,
)


def _inputs(repo_root: Path):  # type: ignore[no-untyped-def]
    return (
        load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json"),
        load_case_config(repo_root / "configs/cases/case_20260604.json"),
    )


@pytest.mark.parametrize(
    ("factor_id", "value", "expected_path"),
    [
        ("feed_load_ratio", 1.05, "case.feed.mass_flow_kg_s"),
        (
            "crude_lightness_shift_fraction",
            0.02,
            "case.feed.mass_fractions.naphtha",
        ),
        ("feed_temperature_offset_k", 5.0, "case.feed.temperature_k"),
        (
            "flash_temperature_offset_k",
            -0.1,
            "case.operating_conditions.flash_temperature_k",
        ),
        ("reflux_ratio_factor", 1.05, "model.equipment.recycle.reflux_ratio"),
        (
            "pump_around_1_duty_ratio",
            0.95,
            "model.equipment.recycle.pump_around_duties_w[0]",
        ),
        (
            "pump_around_2_duty_ratio",
            1.05,
            "model.equipment.recycle.pump_around_duties_w[1]",
        ),
        (
            "pump_around_3_duty_ratio",
            0.0,
            "model.equipment.recycle.pump_around_duties_w[2]",
        ),
        (
            "wash_water_ratio_factor",
            1.01,
            "model.equipment.desalter.wash_water_ratio",
        ),
        ("column_cut_3_offset_k", 1.0, "model.equipment.column.cut_points_k[2]"),
        ("column_cut_4_offset_k", -1.0, "model.equipment.column.cut_points_k[3]"),
    ],
)
def test_single_factor_changes_only_declared_paths(
    repo_root: Path,
    factor_id: str,
    value: float,
    expected_path: str,
) -> None:
    model, case = _inputs(repo_root)
    original_model_fingerprint = canonical_fingerprint(model.as_dict())
    original_case_fingerprint = canonical_fingerprint(case.as_dict())

    result = apply_steady_factor(model, case, factor_id, value)

    assert expected_path in result.modified_paths
    assert canonical_fingerprint(model.as_dict()) == original_model_fingerprint
    assert canonical_fingerprint(case.as_dict()) == original_case_fingerprint
    assert result.as_dict() == apply_steady_factor(
        model, case, factor_id, value
    ).as_dict()


def test_lightness_shift_is_closed_and_preserves_water_and_salt(repo_root: Path) -> None:
    model, case = _inputs(repo_root)
    result = apply_steady_factor(model, case, "crude_lightness_shift_fraction", 0.02)

    assert math.fsum(result.case.feed.mass_fractions.values()) == pytest.approx(1.0)
    assert result.case.feed.mass_fractions["naphtha"] == pytest.approx(
        case.feed.mass_fractions["naphtha"] + 0.02
    )
    assert result.case.feed.mass_fractions["residue"] == pytest.approx(
        case.feed.mass_fractions["residue"] - 0.02
    )
    assert result.case.feed.mass_fractions["water"] == case.feed.mass_fractions["water"]
    assert result.case.feed.salt_mass_flow_kg_s == case.feed.salt_mass_flow_kg_s


def test_invalid_factor_inputs_are_rejected_before_model_execution(repo_root: Path) -> None:
    model, case = _inputs(repo_root)

    with pytest.raises(ValueError, match="unsupported steady factor"):
        apply_steady_factor(model, case, "stripping_steam_ratio", 1.0)
    with pytest.raises(ValueError, match="component negative"):
        apply_steady_factor(model, case, "crude_lightness_shift_fraction", 0.9)
    with pytest.raises((TypeError, ValueError), match="finite|numeric"):
        apply_steady_factor(model, case, "feed_load_ratio", float("nan"))


@pytest.mark.parametrize(
    ("factor_id", "target"),
    [
        ("feed_load_ratio", "fresh_feed_flow_kg_s"),
        ("available_furnace_duty_ratio", "furnace_fuel_duty_w"),
        ("condenser_cooling_capacity_ratio", "condenser_cooling_duty_w"),
        ("reflux_flow_ratio", "reflux_flow_kg_s"),
        ("pump_around_1_duty_ratio", "pump_around_1_duty_w"),
        ("pump_around_2_duty_ratio", "pump_around_2_duty_w"),
        ("pump_around_3_duty_ratio", "pump_around_3_duty_w"),
    ],
)
def test_dynamic_factor_maps_to_one_absolute_command(
    factor_id: str,
    target: str,
) -> None:
    commands = {
        "fresh_feed_flow_kg_s": 100.0,
        "furnace_fuel_duty_w": 10.0,
        "condenser_cooling_duty_w": 20.0,
        "reflux_flow_kg_s": 5.0,
        "pump_around_1_duty_w": 30.0,
        "pump_around_2_duty_w": 40.0,
        "pump_around_3_duty_w": 50.0,
    }

    actual_target, value = dynamic_command_for_factor(commands, factor_id, 0.95)

    assert actual_target == target
    assert value == pytest.approx(commands[target] * 0.95)
