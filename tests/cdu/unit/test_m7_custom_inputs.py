from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.cdu.runtime.api import preview, run
from petroleum_rto.cdu.runtime.artifacts import derive_run_provenance
from petroleum_rto.cdu.runtime.contracts import (
    RuntimeInputEvent,
    RuntimeScenarioRequest,
)
from petroleum_rto.cdu.runtime.custom_inputs import (
    list_runtime_input_specs,
    resolve_runtime_inputs,
    runtime_request_from_mapping,
    runtime_request_template,
)
from petroleum_rto.cdu.runtime.presentation import build_result_summary
from petroleum_rto.cdu.runtime.presets import load_preset


@pytest.mark.parametrize(
    "preset_id",
    ("steady-baseline", "open-loop-feed-step", "closed-loop-feed-step"),
)
def test_default_preview_preserves_existing_effective_input_fingerprint(
    preset_id: str,
) -> None:
    request = load_preset(preset_id)
    resolved = resolve_runtime_inputs(request)

    assert not resolved.is_custom
    assert (
        resolved.execution_input_fingerprint
        == derive_run_provenance(request).effective_input_fingerprint
    )
    assert resolved.as_dict()["preview_fingerprint"] == resolved.preview_fingerprint


def test_custom_steady_inputs_are_converted_and_applied() -> None:
    request = replace(
        load_preset("steady-baseline"),
        parameters={
            "feed.mass_flow_t_h": 360.0,
            "feed.temperature_c": 45.0,
            "operation.reflux_ratio": 0.6,
            "operation.pump_around_1_duty_mw": 9.0,
        },
        overrides={
            "column.cut_3_temperature_c": 300.0,
            "column.cut_4_temperature_c": 370.0,
        },
    )

    resolved = resolve_runtime_inputs(request)

    assert resolved.case.feed.mass_flow_kg_s == pytest.approx(100.0)
    assert resolved.case.feed.temperature_k == pytest.approx(318.15)
    assert resolved.model.equipment["recycle"]["reflux_ratio"] == pytest.approx(0.6)
    assert resolved.model.equipment["recycle"]["pump_around_duties_w"] == pytest.approx(
        [9_000_000.0, 10_000_000.0, 8_000_000.0]
    )
    assert resolved.model.equipment["column"]["cut_points_k"] == pytest.approx(
        [448.15, 524.15, 573.15, 643.15]
    )
    applied = resolved.as_dict()["applied_inputs"]
    assert isinstance(applied, dict)
    assert applied["feed.mass_flow_t_h"]["normalized_value"] == pytest.approx(100.0)


def test_composition_change_must_still_sum_to_one() -> None:
    request = replace(
        load_preset("steady-baseline"),
        parameters={"feed.mass_fraction.naphtha": 0.2},
    )

    with pytest.raises(ValueError, match="sum to one"):
        resolve_runtime_inputs(request)


def test_unknown_and_misplaced_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown controlled"):
        resolve_runtime_inputs(
            replace(
                load_preset("steady-baseline"),
                parameters={"model.anything": 1.0},
            )
        )
    with pytest.raises(ValueError, match="belongs in override"):
        resolve_runtime_inputs(
            replace(
                load_preset("steady-baseline"),
                parameters={"column.cut_3_temperature_c": 300.0},
            )
        )


def test_invalid_custom_request_never_creates_a_run_directory(tmp_path: Path) -> None:
    request = replace(
        load_preset("steady-baseline"),
        run_id="invalid-custom-input",
        parameters={"forbidden": 1.0},
    )

    with pytest.raises(ValueError, match="unknown controlled custom input"):
        run(request, output_root=tmp_path, expected_preview_fingerprint="0" * 64)

    assert not (tmp_path / "invalid-custom-input").exists()


def test_open_loop_custom_grid_events_and_initial_inventory_preview() -> None:
    request = replace(
        load_preset("open-loop-feed-step"),
        overrides={"dynamic.sensor_time_constant_s": 20.0},
        initial_state={"inventory.flash_drum_ratio": 1.1},
        scenario=RuntimeScenarioRequest(
            duration_s=120.0,
            time_step_s=0.5,
            events=(
                RuntimeInputEvent(
                    30.0,
                    "fresh_feed_flow_kg_s",
                    1.03,
                    "nominal_ratio",
                ),
            ),
        ),
    )

    resolved = resolve_runtime_inputs(request)

    assert resolved.duration_s == 120.0
    assert resolved.time_step_s == 0.5
    assert resolved.initial_inventory_ratios == {"flash_drum": 1.1}
    assert resolved.event_requests is not None
    assert resolved.event_requests[0].value_basis == "nominal_ratio"
    assert resolved.model.dynamic["sensor_time_constant_s"] == 20.0


def test_closed_loop_custom_setpoint_event_is_typed() -> None:
    request = replace(
        load_preset("closed-loop-feed-step"),
        scenario=RuntimeScenarioRequest(
            duration_s=600.0,
            time_step_s=1.0,
            events=(
                RuntimeInputEvent(
                    60.0,
                    "feed_flow.setpoint_ratio",
                    0.98,
                    "setpoint_ratio",
                ),
            ),
        ),
    )

    resolved = resolve_runtime_inputs(request)

    assert resolved.closed_loop_scenario is not None
    assert resolved.closed_loop_scenario.events[0].setpoint_ratio == 0.98


def test_wrong_event_semantics_and_m6_customization_are_rejected() -> None:
    with pytest.raises(ValueError, match="setpoint_ratio"):
        resolve_runtime_inputs(
            replace(
                load_preset("closed-loop-feed-step"),
                scenario=RuntimeScenarioRequest(
                    events=(
                        RuntimeInputEvent(
                            10.0,
                            "feed_flow.setpoint_ratio",
                            1.01,
                            "absolute",
                        ),
                    )
                ),
            )
        )
    with pytest.raises(ValueError, match="do not accept custom"):
        resolve_runtime_inputs(
            replace(
                load_preset("m6-abnormal-pump-trip"),
                parameters={"feed.mass_flow_t_h": 400.0},
            )
        )


def test_input_catalog_and_request_template_are_versioned_and_filtered() -> None:
    steady_ids = {item.input_id for item in list_runtime_input_specs("steady-baseline")}
    dynamic_ids = {item.input_id for item in list_runtime_input_specs("open-loop-feed-step")}
    template = runtime_request_template("open-loop-feed-step")

    assert "dynamic.sensor_time_constant_s" not in steady_ids
    assert "dynamic.sensor_time_constant_s" in dynamic_ids
    assert "inventory.flash_drum_ratio" in dynamic_ids
    assert template["custom_input_version"] == "cdu-mini-custom-input-v0.1.0"
    request = template["request"]
    assert isinstance(request, dict)
    assert request == {
        "preset_id": "open-loop-feed-step",
        "parameters": {},
        "overrides": {},
        "initial_state": {},
    }


def test_sparse_request_inherits_preset_and_only_overlays_supplied_fields() -> None:
    base = load_preset("open-loop-feed-step")
    request = runtime_request_from_mapping(
        {
            "preset_id": "open-loop-feed-step",
            "parameters": {"feed.mass_flow_t_h": 360.0},
            "scenario": {"duration_s": 1200.0},
            "metadata": {"purpose": "sparse request test"},
        }
    )

    assert request.schema_version == base.schema_version
    assert request.request_version == base.request_version
    assert request.run_type == base.run_type
    assert request.random_seed == base.random_seed
    assert request.parameters == {"feed.mass_flow_t_h": 360.0}
    assert request.overrides == {}
    assert request.initial_state == {}
    assert request.metadata == {
        "preset.source": "M7_fixed_registry",
        "purpose": "sparse request test",
    }
    assert request.scenario is not None
    assert request.scenario.duration_s == 1200.0
    assert request.scenario.time_step_s is None
    assert request.scenario.events is None
    assert runtime_request_from_mapping(base.as_dict()) == base


def test_sparse_request_rejects_contract_and_preset_drift() -> None:
    with pytest.raises(ValueError, match="missing=.*preset_id"):
        runtime_request_from_mapping({"parameters": {}})
    with pytest.raises(ValueError, match="unknown"):
        runtime_request_from_mapping({"preset_id": "steady-baseline", "extra": 1.0})
    with pytest.raises(ValueError, match="run_type differs"):
        runtime_request_from_mapping(
            {"preset_id": "steady-baseline", "run_type": "open_loop_dynamic"}
        )
    with pytest.raises(ValueError, match="cannot be overridden"):
        runtime_request_from_mapping(
            {
                "preset_id": "steady-baseline",
                "metadata": {"preset.source": "another registry"},
            }
        )


def test_preview_confirmation_publishes_and_reloads_custom_steady_run(
    tmp_path: Path,
) -> None:
    request = replace(
        load_preset("steady-baseline"),
        run_id="custom-steady-confirmed",
        parameters={"feed.mass_flow_t_h": 360.0},
    )
    resolved = preview(request)

    with pytest.raises(ValueError, match="confirmed preview fingerprint"):
        run(request, output_root=tmp_path)

    record = run(
        request,
        output_root=tmp_path,
        expected_preview_fingerprint=resolved.preview_fingerprint,
    )

    assert record.payload.runtime_status == "success"
    assert record.payload.effective_input_fingerprint == resolved.execution_input_fingerprint
    assert record.payload.source_fingerprints["runtime_custom_input_preview"] == (
        resolved.preview_fingerprint
    )

    with pytest.raises(ValueError, match="confirmed preview fingerprint"):
        run(
            replace(request, run_id="custom-steady-stale-preview"),
            output_root=tmp_path,
            expected_preview_fingerprint="0" * 64,
        )


def test_custom_open_loop_executes_ratio_event_and_inventory_initialization(
    tmp_path: Path,
) -> None:
    request = replace(
        load_preset("open-loop-feed-step"),
        run_id="custom-open-short",
        initial_state={"inventory.flash_drum_ratio": 1.02},
        scenario=RuntimeScenarioRequest(
            duration_s=6.0,
            time_step_s=1.0,
            events=(
                RuntimeInputEvent(
                    2.0,
                    "fresh_feed_flow_kg_s",
                    1.01,
                    "nominal_ratio",
                ),
            ),
        ),
    )
    resolved = preview(request)

    record = run(
        request,
        output_root=tmp_path,
        expected_preview_fingerprint=resolved.preview_fingerprint,
    )

    assert record.payload.runtime_status == "success"
    assert len(record.payload.timeseries) == 7
    first = cast(Mapping[str, object], record.payload.timeseries[0])
    third = cast(Mapping[str, object], record.payload.timeseries[2])
    first_commands = cast(Mapping[str, float], first["commands"])
    third_commands = cast(Mapping[str, float], third["commands"])
    baseline = first_commands["fresh_feed_flow_kg_s"]
    stepped = third_commands["fresh_feed_flow_kg_s"]
    assert stepped == pytest.approx(baseline * 1.01)
    first_state = cast(Mapping[str, object], first["state"])
    sensors = cast(Mapping[str, float], first_state["sensor_states"])
    inventories = cast(Mapping[str, object], first_state["liquid_inventories"])
    flash = cast(Mapping[str, float], inventories["flash_drum"])
    assert sensors["flash_drum_inventory_kg"] == pytest.approx(flash["total_mass_kg"])
    summary = cast(Mapping[str, object], build_result_summary(record)["key_results"])
    products = cast(Mapping[str, object], summary["products"])
    assert all(
        cast(Mapping[str, float], row)["final_mass_flow_t_h"] > 0.0 for row in products.values()
    )


def test_custom_closed_loop_executes_with_resolved_operating_inputs(
    tmp_path: Path,
) -> None:
    request = replace(
        load_preset("closed-loop-feed-step"),
        run_id="custom-closed-operating-inputs",
        parameters={"feed.mass_flow_t_h": 400.0},
        scenario=RuntimeScenarioRequest(
            duration_s=703.5,
            time_step_s=3.5,
            events=(),
        ),
    )
    resolved = preview(request)

    record = run(
        request,
        output_root=tmp_path,
        expected_preview_fingerprint=resolved.preview_fingerprint,
    )

    assert record.payload.runtime_status == "success"
    assert record.payload.duration_s == 703.5
    assert record.payload.time_step_s == 3.5
    assert len(record.payload.timeseries) == 805
    assert record.payload.source_fingerprints["runtime_custom_input_preview"] == (
        resolved.preview_fingerprint
    )
    summary = cast(Mapping[str, object], build_result_summary(record)["key_results"])
    controls = cast(Mapping[str, object], summary["control_loops"])
    assert len(controls) == 7
    assert summary["acceptance_passed"] is True


def test_custom_closed_loop_initial_inventory_reaches_honest_acceptance_failure(
    tmp_path: Path,
) -> None:
    request = replace(
        load_preset("closed-loop-feed-step"),
        run_id="custom-closed-initial-inventory",
        initial_state={"inventory.flash_drum_ratio": 1.01},
        scenario=RuntimeScenarioRequest(
            duration_s=10.0,
            time_step_s=1.0,
            events=(),
        ),
    )
    resolved = preview(request)

    record = run(
        request,
        output_root=tmp_path,
        expected_preview_fingerprint=resolved.preview_fingerprint,
    )

    assert record.payload.runtime_status == "failed"
    assert record.payload.failure_stage == "performance_evaluation"
    assert len(record.payload.timeseries) == 11
    first = cast(Mapping[str, object], record.payload.timeseries[0])
    controls = cast(Mapping[str, object], first["controls"])
    flash_control = cast(Mapping[str, float], controls["flash_inventory"])
    assert flash_control["error_normalized"] == pytest.approx(-0.01)
