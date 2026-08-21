from __future__ import annotations

import copy
import json
import math
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

from petroleum_rto.cdu.control.config import (
    CONTROL_PAIRING_WHITELIST,
    load_control_config,
)
from petroleum_rto.cdu.core.config import ConfigurationError, load_json
from petroleum_rto.cdu.validation.config import (
    M6_ANALYSIS_BASIS_VERSION,
    M6_CLAIM_SCOPE,
    M6_SCHEMA_VERSION,
    M6_VALIDATION_VERSION,
    M6ValidationConfig,
    load_m6_validation_config,
)
from petroleum_rto.cdu.validation.domain import assess_applicability
from petroleum_rto.cdu.validation.metrics import STEADY_OUTPUT_IDS

_CONFIG_PATH = Path("configs/cdu/validation/m6_validation_v0.1.0.json")
_CONTROL_CONFIG_PATH = Path("configs/cdu/controllers/cdu_pi_v0.1.0.json")


def _path(repo_root: Path) -> Path:
    return repo_root / _CONFIG_PATH


def _raw(repo_root: Path) -> dict[str, Any]:
    return copy.deepcopy(load_json(_path(repo_root)))


def _scenarios(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], raw["scenarios"])


def _rules(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], raw["protection_rules"])


def _dimensions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], raw["domain_dimensions"])


def test_repository_config_loads_round_trips_and_is_deeply_immutable(
    repo_root: Path,
) -> None:
    first = load_m6_validation_config(_path(repo_root))
    second = load_m6_validation_config(_path(repo_root))
    round_trip = M6ValidationConfig.from_mapping(first.as_dict())

    assert first == second == round_trip
    assert first.as_dict() == second.as_dict() == round_trip.as_dict()
    assert first.input_fingerprint == second.input_fingerprint
    assert len(first.input_fingerprint) == 64
    json.dumps(first.as_dict(), allow_nan=False, sort_keys=True)
    assert first.versions["schema_version"] == M6_SCHEMA_VERSION
    assert first.validation_version == M6_VALIDATION_VERSION
    assert first.analysis_basis_version == M6_ANALYSIS_BASIS_VERSION
    assert first.claim_scope == M6_CLAIM_SCOPE

    with pytest.raises(TypeError):
        first.metadata["purpose"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        first.scenarios[0].inputs["feed_load_ratio"] = 99.0  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        first.validation_version = "changed"  # type: ignore[misc]


def test_scenario_suite_has_the_frozen_dispatch_contract(repo_root: Path) -> None:
    config = load_m6_validation_config(_path(repo_root))
    dispatch = {
        item.scenario_id: (item.execution_reference, dict(item.inputs))
        for item in config.scenarios
    }

    assert dispatch == {
        "limited_condenser_cooling_decline_10": (
            "m3.condenser_cooling_capacity_ratio",
            {"condenser_cooling_capacity_ratio": 0.9},
        ),
        "limited_feed_drop_30": (
            "m3.feed_load_ratio",
            {"feed_load_ratio": 0.7},
        ),
        "limited_furnace_duty_decline_10": (
            "m3.available_furnace_duty_ratio",
            {"available_furnace_duty_ratio": 0.9},
        ),
        "limited_furnace_fuel_saturation": (
            "m3.available_furnace_duty_ratio",
            {"available_furnace_duty_ratio": 0.8},
        ),
        "limited_furnace_temperature_sensor_bias": (
            "supervision.furnace_temperature_sensor_bias",
            {"furnace_temperature_sensor_bias_k": 5.0},
        ),
        "limited_furnace_temperature_sensor_freeze": (
            "supervision.furnace_temperature_measurement_invalid",
            {"furnace_temperature_sensor_health_ratio": 0.0},
        ),
        "limited_pump_around_1_trip": (
            "m3.pump_around_1_duty_ratio",
            {"pump_around_1_duty_ratio": 0.0, "pump_around_1_health_ratio": 0.0},
        ),
        "limited_residue_draw_valve_stuck": (
            "supervision.residue_draw_valve_stuck",
            {"residue_draw_valve_mobility_ratio": 0.0},
        ),
        "normal_crude_heavier_2": (
            "steady.crude_lightness_shift_fraction",
            {"crude_lightness_shift_fraction": -0.02},
        ),
        "normal_crude_lighter_2": (
            "steady.crude_lightness_shift_fraction",
            {"crude_lightness_shift_fraction": 0.02},
        ),
        "normal_feed_minus_5": (
            "steady.feed_load_ratio",
            {"feed_load_ratio": 0.95},
        ),
        "normal_feed_plus_5": (
            "steady.feed_load_ratio",
            {"feed_load_ratio": 1.05},
        ),
        "normal_feed_temperature_minus_5k": (
            "steady.feed_temperature_offset_k",
            {"feed_temperature_offset_k": -5.0},
        ),
        "normal_feed_temperature_plus_5k": (
            "steady.feed_temperature_offset_k",
            {"feed_temperature_offset_k": 5.0},
        ),
        "normal_m4_feed_setpoint_minus_5": (
            "m4.feed_flow",
            {"feed_load_ratio": 0.95},
        ),
        "normal_m4_feed_setpoint_plus_5": (
            "m4.feed_flow",
            {"feed_load_ratio": 1.05},
        ),
        "normal_pump_around_1_plus_5": (
            "steady.pump_around_1_duty_ratio",
            {"pump_around_1_duty_ratio": 1.05},
        ),
        "normal_pump_around_2_plus_5": (
            "steady.pump_around_2_duty_ratio",
            {"pump_around_2_duty_ratio": 1.05},
        ),
        "normal_pump_around_3_plus_5": (
            "steady.pump_around_3_duty_ratio",
            {"pump_around_3_duty_ratio": 1.05},
        ),
        "normal_reflux_plus_5": (
            "steady.reflux_ratio_factor",
            {"reflux_ratio_factor": 1.05},
        ),
        "rejected_dynamic_feed_water_pulse": (
            "unsupported.dynamic_water_composition",
            {"dynamic_feed_water_pulse_ratio": 1.2},
        ),
        "rejected_stripping_steam_request": (
            "unsupported.stripping_steam",
            {"stripping_steam_ratio": 1.05},
        ),
    }


def test_scenario_applicability_and_probe_backed_directions_are_frozen(
    repo_root: Path,
) -> None:
    config = load_m6_validation_config(_path(repo_root))
    for scenario in config.scenarios:
        result = assess_applicability(
            config.domain_dimensions,
            scenario.inputs,
            abnormal_verification=scenario.abnormal_verification,
        )
        assert result.status == scenario.expected_status

    assert config.scenario("normal_feed_temperature_plus_5k").expected_directions == {
        "energy.furnace_fuel_duty_w": 0
    }
    assert config.scenario("normal_reflux_plus_5").expected_directions == {
        "quality.gasoline.t90_k_proxy": -1
    }
    assert config.scenario(
        "normal_m4_feed_setpoint_plus_5"
    ).expected_directions == {"loop.feed_flow.final_output_ratio": 1}
    assert config.scenario("limited_feed_drop_30").expected_directions == {
        "final_inventory_ratio.flash_drum": -1
    }
    assert config.scenario("limited_pump_around_1_trip").expected_directions == {
        "maximum.kerosene_temperature_k": 1,
        "maximum.tower_top_temperature_k": 1,
    }
    fuel_saturation = config.scenario("limited_furnace_fuel_saturation")
    assert fuel_saturation.expected_directions == {
        "minimum.furnace_outlet_temperature_k": -1
    }
    assert "claim.proxy.limited" in fuel_saturation.claim_ids
    assert "available-duty command-proxy" in fuel_saturation.purpose
    assert "not an independent fuel-heating-value" in fuel_saturation.purpose
    assert config.scenario("rejected_dynamic_feed_water_pulse").expected_status == (
        "rejected"
    )


def test_all_domain_dimensions_disclose_layer_confidence_and_assumptions(
    repo_root: Path,
) -> None:
    config = load_m6_validation_config(_path(repo_root))
    disclosed = {
        item.dimension_id: (
            item.representation,
            item.input_layer,
            item.confidence,
            item.assumptions,
        )
        for item in config.domain_dimensions
    }
    assert disclosed == {
        "actuator_time_constant_ratio": (
            "direct",
            "M3_open_loop",
            "low_engineering",
            ("not_field_identified", "single_global_first_order_lag"),
        ),
        "available_furnace_duty_ratio": (
            "proxy",
            "M3_open_loop",
            "low_proxy",
            ("command_power_proxy_not_fuel_heating_value",),
        ),
        "column_cut_3_offset_k": (
            "direct",
            "M2_steady",
            "low_single_case_aligned",
            ("m5_single_case_two_parameter_alignment",),
        ),
        "column_cut_4_offset_k": (
            "direct",
            "M2_steady",
            "low_single_case_aligned",
            ("m5_single_case_two_parameter_alignment",),
        ),
        "condenser_cooling_capacity_ratio": (
            "proxy",
            "M3_open_loop",
            "low_proxy",
            ("cooling_command_proxy_not_equipment_capacity_curve",),
        ),
        "crude_lightness_shift_fraction": (
            "direct",
            "M2_steady",
            "low_engineering",
            ("equal_naphtha_residue_mass_shift", "other_components_fixed"),
        ),
        "dynamic_feed_water_pulse_ratio": (
            "unsupported",
            "structural_rejection",
            "not_applicable",
            (
                "no_dynamic_feed_water_composition_state",
                "pre_solver_rejection_required",
            ),
        ),
        "feed_load_ratio": (
            "direct",
            "M2_M3_M4_shared",
            "low_engineering",
            ("fixed_feed_composition", "nominal_ratio_basis"),
        ),
        "feed_temperature_offset_k": (
            "direct",
            "M2_steady",
            "low_case_observation",
            ("fixed_preheater_target_masks_raw_feed_temperature",),
        ),
        "flash_temperature_offset_k": (
            "direct",
            "M2_steady",
            "low_case_observation",
            ("case_flash_temperature_offset", "weak_time_alignment"),
        ),
        "furnace_temperature_sensor_bias_k": (
            "proxy",
            "M6_supervision",
            "synthetic_logic_only",
            ("finite_synthetic_bias", "not_field_transmitter_model"),
        ),
        "furnace_temperature_sensor_health_ratio": (
            "proxy",
            "M6_supervision",
            "synthetic_logic_only",
            ("synthetic_freeze_logic", "validity_sideband_not_nan"),
        ),
        "pump_around_1_duty_ratio": (
            "direct",
            "M2_M3_shared",
            "low_engineering",
            ("pump_around_duty_scales_nominal_heat_removal",),
        ),
        "pump_around_1_health_ratio": (
            "proxy",
            "M6_supervision",
            "synthetic_logic_only",
            ("binary_health_sideband", "not_field_trip_signal"),
        ),
        "pump_around_2_duty_ratio": (
            "direct",
            "M2_M3_shared",
            "low_engineering",
            ("pump_around_duty_scales_nominal_heat_removal",),
        ),
        "pump_around_3_duty_ratio": (
            "direct",
            "M2_M3_shared",
            "low_engineering",
            ("pump_around_duty_scales_nominal_heat_removal",),
        ),
        "reflux_flow_ratio": (
            "proxy",
            "M3_open_loop",
            "low_proxy",
            ("dynamic_command_proxy_not_steady_reflux_ratio",),
        ),
        "reflux_ratio_factor": (
            "direct",
            "M2_steady",
            "low_engineering",
            ("steady_reflux_ratio_factor_only",),
        ),
        "residue_draw_valve_mobility_ratio": (
            "proxy",
            "M6_supervision",
            "synthetic_logic_only",
            ("mobility_sideband_not_valve_mechanics", "synthetic_fault_logic"),
        ),
        "sensor_time_constant_ratio": (
            "direct",
            "M3_open_loop",
            "low_engineering",
            ("not_field_identified", "single_global_first_order_lag"),
        ),
        "stripping_steam_ratio": (
            "unsupported",
            "structural_rejection",
            "not_applicable",
            (
                "no_independent_steam_balance_or_separation_equation",
                "pre_solver_rejection_required",
            ),
        ),
        "wash_water_ratio_factor": (
            "direct",
            "M2_steady",
            "low_case_observation",
            (
                "wash_water_ratio_not_dynamic_feed_water_composition",
                "weak_time_alignment",
            ),
        ),
    }


def test_protection_rules_match_formal_thresholds_and_public_targets(
    repo_root: Path,
) -> None:
    config = load_m6_validation_config(_path(repo_root))
    assert len(config.protection_rules) == 11
    assert tuple(item.priority for item in config.protection_rules) == (
        0,
        1,
        2,
        3,
        4,
        10,
        11,
        12,
        13,
        14,
        15,
    )
    low_feed = config.protection_rule("low_furnace_feed")
    assert (low_feed.trip_threshold, low_feed.clear_threshold) == (0.75, 0.8)
    assert low_feed.trigger_delay_s == 10.0
    assert low_feed.latching
    assert low_feed.action.command_ratio_overrides == {
        "furnace_fuel_duty_w": 0.8
    }
    high_pressure = config.protection_rule("high_tower_top_pressure")
    assert (high_pressure.trip_threshold, high_pressure.clear_threshold) == (1.05, 1.03)
    assert high_pressure.trigger_delay_s == 15.0
    high_temperature = config.protection_rule("high_furnace_temperature")
    assert high_temperature.action.command_ratio_overrides == {
        "furnace_fuel_duty_w": 0.8
    }
    pump = config.protection_rule("pump_around_1_invalid")
    assert pump.condition == "invalid"
    assert pump.trigger_delay_s == 2.0
    assert pump.action.manual_tracking_loop_ids == (
        "feed_flow",
        "furnace_temperature",
    )
    assert pump.action.command_ratio_overrides == {
        "fresh_feed_flow_kg_s": 0.8,
        "furnace_fuel_duty_w": 0.8,
    }

    control = load_control_config(repo_root / _CONTROL_CONFIG_PATH)
    missing_override_loops: list[tuple[str, str]] = []
    for rule in config.protection_rules:
        for loop_id in rule.action.manual_tracking_loop_ids:
            command_id = CONTROL_PAIRING_WHITELIST[loop_id].manipulated_variable
            override = rule.action.command_ratio_overrides.get(command_id)
            if override is None:
                missing_override_loops.append((rule.rule_id, loop_id))
                continue
            loop = control.loop(loop_id)
            assert loop.output_min_ratio <= override <= loop.output_max_ratio
    assert missing_override_loops == [
        ("furnace_temperature_measurement_invalid", "furnace_temperature")
    ]
    measurement_invalid = config.protection_rule(
        "furnace_temperature_measurement_invalid"
    )
    assert measurement_invalid.trigger_delay_s == 5.0
    assert measurement_invalid.action.command_ratio_overrides == {}
    assert measurement_invalid.action.manual_tracking_loop_ids == (
        "furnace_temperature",
    )


def test_uncertainty_plans_have_exact_order_steps_outputs_and_sources(
    repo_root: Path,
) -> None:
    config = load_m6_validation_config(_path(repo_root))
    steady = config.steady_uncertainty
    dynamic = config.dynamic_uncertainty

    assert steady.plan_id == "m6_steady_local_envelope_v0.1.0"
    assert tuple(item.input_id for item in steady.inputs) == (
        "feed_load_ratio",
        "crude_lightness_shift_fraction",
        "feed_temperature_offset_k",
        "flash_temperature_offset_k",
        "wash_water_ratio_factor",
        "column_cut_3_offset_k",
        "column_cut_4_offset_k",
    )
    assert tuple(item.central_step for item in steady.inputs) == (
        0.01,
        0.005,
        1.0,
        1.0,
        0.01,
        1.0,
        1.0,
    )
    assert tuple(item.output_id for item in steady.outputs) == STEADY_OUTPUT_IDS
    assert tuple(item.input_id for item in steady.intervals) == tuple(
        item.input_id for item in steady.inputs
    )
    assert "crude_structure_error" in steady.unquantified_sources
    assert "field_time_alignment_error" in steady.unquantified_sources

    assert dynamic.plan_id == "m6_dynamic_lag_envelope_v0.1.0"
    assert tuple(item.input_id for item in dynamic.inputs) == (
        "actuator_time_constant_ratio",
        "sensor_time_constant_ratio",
    )
    assert tuple(item.central_step for item in dynamic.inputs) == (0.05, 0.05)
    assert tuple(item.output_id for item in dynamic.outputs) == (
        "maximum.furnace_outlet_temperature_k",
        "maximum.tower_top_pressure_pa",
        "maximum_abs_inventory_deviation.flash_drum",
        "maximum_abs_inventory_deviation.reflux_drum",
        "maximum_abs_inventory_deviation.tower_bottom",
        "final_inventory_ratio.flash_drum",
        "final_inventory_ratio.reflux_drum",
        "final_inventory_ratio.tower_bottom",
        "final.tower_top_pressure_pa",
        "tracking_iae.actuator.fresh_feed_flow_kg_s",
        "tracking_iae.sensor.flash_drum_inventory_kg",
        "response_t63_s.actuator.fresh_feed_flow_kg_s",
        "response_t63_s.sensor.flash_drum_inventory_kg",
    )
    units = {item.output_id: item.unit for item in dynamic.outputs}
    assert units["maximum.furnace_outlet_temperature_k"] == "K"
    assert units["maximum.tower_top_pressure_pa"] == "Pa"
    assert units["tracking_iae.actuator.fresh_feed_flow_kg_s"] == "kg"
    assert units["tracking_iae.sensor.flash_drum_inventory_kg"] == "kg*s"
    assert units["response_t63_s.actuator.fresh_feed_flow_kg_s"] == "s"
    assert units["response_t63_s.sensor.flash_drum_inventory_kg"] == "s"
    assert tuple(item.input_id for item in dynamic.intervals) == (
        "actuator_time_constant_ratio",
        "sensor_time_constant_ratio",
    )
    assert "field_dynamic_identification_gap" in dynamic.unquantified_sources


def test_report_acceptance_and_provenance_are_explicit(repo_root: Path) -> None:
    config = load_m6_validation_config(_path(repo_root))
    acceptance = config.report_acceptance

    assert acceptance.minimum_scenario_coverage_fraction == 1.0
    assert acceptance.maximum_failed_scenarios == 0
    assert acceptance.maximum_mass_residual_kg_s == 1e-8
    assert acceptance.maximum_component_residual_kg_s == 1e-8
    assert acceptance.maximum_salt_residual_kg_s == 1e-10
    assert acceptance.protection_timing_tolerance_s == 0.0
    assert acceptance.require_exact_reproduction
    assert config.metadata == {
        "claim_scope": "engineering_validation_only",
        "confidence": "low_engineering_confidence",
        "data_origin": "M6_synthetic_validation",
        "purpose": "M6_engineering_validation_protection_uncertainty_and_domain_evidence",
        "synthetic": "true",
    }


@pytest.mark.parametrize(
    "nested_section",
    ["domain_dimensions", "scenarios", "protection_rules"],
)
def test_unknown_top_and_nested_fields_are_rejected(
    repo_root: Path,
    nested_section: str,
) -> None:
    top = _raw(repo_root)
    top["unexpected"] = 1
    with pytest.raises(ConfigurationError, match="unknown"):
        M6ValidationConfig.from_mapping(top)

    nested = _raw(repo_root)
    values = cast(list[dict[str, Any]], nested[nested_section])
    values[0]["unexpected"] = 1
    with pytest.raises(ConfigurationError, match="unknown"):
        M6ValidationConfig.from_mapping(nested)


@pytest.mark.parametrize(
    ("version_field", "bad_value"),
    [
        ("schema_version", "2.0.0"),
        ("validation_version", "m6-validation-v9.9.9"),
        ("analysis_basis_version", "m6-basis-v9.9.9"),
        ("model_version", "cdu-reduced-9.9.9"),
        ("model_config_version", "cdu-mini-config-9.9.9"),
        ("base_parameter_set_version", "cdu-parameters-9.9.9"),
        (
            "derived_parameter_set_version",
            "cdu-parameters-m5-case20260604-v9.9.9",
        ),
        ("base_case_version", "case-20260604-v9.9.9"),
        ("derived_case_version", "case-20260604-m5-aligned-v9.9.9"),
        ("control_version", "cdu-pi-control-9.9.9"),
        ("claim_scope", "field_validated"),
    ],
)
def test_fixed_versions_and_claim_scope_reject_drift(
    repo_root: Path,
    version_field: str,
    bad_value: str,
) -> None:
    raw = _raw(repo_root)
    raw[version_field] = bad_value
    with pytest.raises(ConfigurationError):
        M6ValidationConfig.from_mapping(raw)


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf, True, "1.0"])
def test_scenario_inputs_require_finite_real_numbers(
    repo_root: Path,
    bad_value: object,
) -> None:
    raw = _raw(repo_root)
    _scenarios(raw)[0]["inputs"] = {"feed_load_ratio": bad_value}
    with pytest.raises(ConfigurationError, match="numeric|finite"):
        M6ValidationConfig.from_mapping(raw)


def test_nested_types_bounds_and_boolean_fields_are_strict(repo_root: Path) -> None:
    bad_bound = _raw(repo_root)
    _dimensions(bad_bound)[0]["limited_max"] = math.inf
    with pytest.raises(ConfigurationError, match="finite"):
        M6ValidationConfig.from_mapping(bad_bound)

    bad_flag = _raw(repo_root)
    _scenarios(bad_flag)[0]["abnormal_verification"] = 0
    with pytest.raises(ConfigurationError, match="boolean"):
        M6ValidationConfig.from_mapping(bad_flag)

    bad_priority = _raw(repo_root)
    _rules(bad_priority)[0]["priority"] = True
    with pytest.raises(ConfigurationError, match="integer"):
        M6ValidationConfig.from_mapping(bad_priority)

    bad_report = _raw(repo_root)
    cast(dict[str, Any], bad_report["report_acceptance"])[
        "direction_absolute_tolerance"
    ] = -1.0
    with pytest.raises(ConfigurationError, match="non-negative"):
        M6ValidationConfig.from_mapping(bad_report)

    bad_enum = _raw(repo_root)
    _scenarios(bad_enum)[0]["scenario_class"] = ["normal"]
    with pytest.raises(ConfigurationError, match="scenario_class"):
        M6ValidationConfig.from_mapping(bad_enum)

    bad_layer = _raw(repo_root)
    _dimensions(bad_layer)[0]["input_layer"] = "field_DCS"
    with pytest.raises(ConfigurationError, match="input_layer"):
        M6ValidationConfig.from_mapping(bad_layer)

    bad_confidence = _raw(repo_root)
    _dimensions(bad_confidence)[0]["confidence"] = "field_validated"
    with pytest.raises(ConfigurationError, match="confidence"):
        M6ValidationConfig.from_mapping(bad_confidence)

    bad_assumptions = _raw(repo_root)
    _dimensions(bad_assumptions)[0]["assumptions"] = []
    with pytest.raises(ConfigurationError, match="cannot be empty"):
        M6ValidationConfig.from_mapping(bad_assumptions)


def test_domain_provenance_fields_are_required(repo_root: Path) -> None:
    for field in ("input_layer", "confidence", "assumptions"):
        raw = _raw(repo_root)
        del _dimensions(raw)[0][field]
        with pytest.raises(ConfigurationError, match="missing"):
            M6ValidationConfig.from_mapping(raw)


@pytest.mark.parametrize(
    "duplicate_kind",
    ["dimension", "scenario_id", "scenario_version", "rule", "uncertainty_input"],
)
def test_duplicate_ids_are_rejected(repo_root: Path, duplicate_kind: str) -> None:
    raw = _raw(repo_root)
    if duplicate_kind == "dimension":
        _dimensions(raw).append(copy.deepcopy(_dimensions(raw)[0]))
    elif duplicate_kind == "scenario_id":
        _scenarios(raw)[1]["scenario_id"] = _scenarios(raw)[0]["scenario_id"]
    elif duplicate_kind == "scenario_version":
        _scenarios(raw)[1]["scenario_version"] = _scenarios(raw)[0][
            "scenario_version"
        ]
    elif duplicate_kind == "rule":
        _rules(raw).append(copy.deepcopy(_rules(raw)[0]))
    else:
        plan = cast(dict[str, Any], raw["steady_uncertainty"])
        inputs = cast(list[dict[str, Any]], plan["inputs"])
        inputs[1]["input_id"] = inputs[0]["input_id"]
    with pytest.raises(ConfigurationError, match="unique|exactly match"):
        M6ValidationConfig.from_mapping(raw)


def test_scenario_status_unknown_input_and_structural_pairing_are_rejected(
    repo_root: Path,
) -> None:
    mismatch = _raw(repo_root)
    _scenarios(mismatch)[0]["expected_status"] = "limited"
    with pytest.raises(ConfigurationError, match="requires expected_status"):
        M6ValidationConfig.from_mapping(mismatch)

    unknown = _raw(repo_root)
    _scenarios(unknown)[0]["inputs"] = {"unknown_factor": 1.0}
    with pytest.raises(ConfigurationError, match="applicability"):
        M6ValidationConfig.from_mapping(unknown)

    bad_layer = _raw(repo_root)
    structural = next(
        item
        for item in _scenarios(bad_layer)
        if item["scenario_id"] == "rejected_stripping_steam_request"
    )
    structural["execution_layer"] = "M3_open_loop"
    with pytest.raises(ConfigurationError, match="must be paired"):
        M6ValidationConfig.from_mapping(bad_layer)


def test_model_executed_scenario_expected_directions_cannot_be_empty(
    repo_root: Path,
) -> None:
    raw = _raw(repo_root)
    fuel_saturation = next(
        item
        for item in _scenarios(raw)
        if item["scenario_id"] == "limited_furnace_fuel_saturation"
    )
    fuel_saturation["expected_directions"] = {}
    with pytest.raises(ConfigurationError, match="expected_directions cannot be empty"):
        M6ValidationConfig.from_mapping(raw)


@pytest.mark.parametrize("bad_target", ["unknown_command", "unknown_loop"])
def test_protection_actions_must_use_public_commands_and_loops(
    repo_root: Path,
    bad_target: str,
) -> None:
    raw = _raw(repo_root)
    action = cast(dict[str, Any], _rules(raw)[0]["action"])
    if bad_target == "unknown_command":
        action["command_ratio_overrides"] = {"field_valve": 1.0}
    else:
        action["manual_tracking_loop_ids"] = ["field_loop"]
    with pytest.raises(ConfigurationError, match="unknown actions"):
        M6ValidationConfig.from_mapping(raw)


def test_uncertainty_order_intervals_and_domain_are_strict(repo_root: Path) -> None:
    reversed_intervals = _raw(repo_root)
    plan = cast(dict[str, Any], reversed_intervals["dynamic_uncertainty"])
    intervals = cast(list[dict[str, Any]], plan["intervals"])
    intervals.reverse()
    with pytest.raises(ConfigurationError, match="exactly match"):
        M6ValidationConfig.from_mapping(reversed_intervals)

    outside = _raw(repo_root)
    plan = cast(dict[str, Any], outside["dynamic_uncertainty"])
    intervals = cast(list[dict[str, Any]], plan["intervals"])
    intervals[0]["upper"] = 3.0
    with pytest.raises(ConfigurationError, match="leaves its limited domain"):
        M6ValidationConfig.from_mapping(outside)

    unsupported = _raw(repo_root)
    plan = cast(dict[str, Any], unsupported["dynamic_uncertainty"])
    inputs = cast(list[dict[str, Any]], plan["inputs"])
    intervals = cast(list[dict[str, Any]], plan["intervals"])
    inputs[0]["input_id"] = "stripping_steam_ratio"
    intervals[0]["input_id"] = "stripping_steam_ratio"
    with pytest.raises(ConfigurationError, match="cannot be unsupported"):
        M6ValidationConfig.from_mapping(unsupported)


def test_metadata_fields_and_values_are_exact(repo_root: Path) -> None:
    unknown = _raw(repo_root)
    cast(dict[str, Any], unknown["metadata"])["unexpected"] = "value"
    with pytest.raises(ConfigurationError, match="exactly"):
        M6ValidationConfig.from_mapping(unknown)

    not_synthetic = _raw(repo_root)
    cast(dict[str, Any], not_synthetic["metadata"])["synthetic"] = "false"
    with pytest.raises(ConfigurationError, match="synthetic"):
        M6ValidationConfig.from_mapping(not_synthetic)
