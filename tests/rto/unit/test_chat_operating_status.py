from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.rto import load_operating_context
from petroleum_rto.rto.runtime import build_chat_operating_status

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTEXT_PATH = _REPO_ROOT / "configs" / "rto" / "contexts" / "case_20260604.json"


def test_operating_status_projects_only_the_trusted_simulation_snapshot() -> None:
    summary = build_chat_operating_status(load_operating_context(_CONTEXT_PATH))

    assert summary == {
        "state_kind": "configured_simulation_context",
        "simulator_mode": "on_demand_offline",
        "simulator_state": "idle",
        "simulation_executed_for_this_query": False,
        "operating_mode": "normal-steady",
        "fresh_feed_load": {
            "kg_per_s": 113.1388888888889,
            "t_per_h": 407.3,
        },
        "current_setpoints": [
            {
                "variable_id": "furnace_temperature_target_k",
                "value_k": 628.35,
                "value_deg_c": 355.2,
            },
            {
                "variable_id": "tower_top_pressure_target_pa_a",
                "value_pa_a": 152325.0,
                "value_mpa_a": 0.152325,
                "value_mpa_g": 0.051,
            },
        ],
        "initial_inventory_ratios": {
            "flash_drum": 1.0,
            "reflux_drum": 1.0,
            "tower_bottom": 1.0,
        },
        "data_timestamp": "2026-06-04T09:16:00+08:00",
        "data_quality": "weak-time-alignment",
        "claim_scope": "engineering_simulation_only",
        "live_plant_data": False,
        "field_validated": False,
        "control_authority": "none",
    }
    serialized = repr(summary)
    for forbidden in (
        "feed_composition",
        "model_ref",
        "case_ref",
        "fingerprint",
        "solver",
        "run_dir",
    ):
        assert forbidden not in serialized


def test_operating_status_rejects_an_incomplete_setpoint_snapshot() -> None:
    context = load_operating_context(_CONTEXT_PATH)
    incomplete = replace(
        context,
        current_setpoints={"furnace_temperature_target_k": 628.35},
    )

    with pytest.raises(ValueError, match="two supported CDU setpoints"):
        build_chat_operating_status(incomplete)
