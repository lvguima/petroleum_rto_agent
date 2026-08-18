from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from petroleum_rto.cdu.runtime.contracts import RunRequest
from petroleum_rto.cdu.runtime.presets import (
    PRESET_IDS,
    PRESET_REGISTRY,
    RuntimePreset,
    get_preset,
    list_presets,
    load_preset,
)


def test_fixed_preset_registry_has_stable_complete_order() -> None:
    assert PRESET_IDS == (
        "steady-baseline",
        "open-loop-feed-step",
        "closed-loop-feed-step",
        "m6-abnormal-pump-trip",
        "m6-structural-rejection",
    )
    assert tuple(item.preset_id for item in list_presets()) == PRESET_IDS
    assert tuple(PRESET_REGISTRY) == PRESET_IDS
    assert {item.run_type for item in list_presets()} == {
        "steady_recycle",
        "open_loop_dynamic",
        "closed_loop_dynamic",
        "validation_scenario",
    }


def test_preset_registry_is_immutable_and_does_not_scan_unknown_ids() -> None:
    mutable_view = cast(dict[str, RuntimePreset], PRESET_REGISTRY)
    with pytest.raises(TypeError):
        mutable_view["unexpected"] = get_preset("steady-baseline")
    with pytest.raises(KeyError, match="unknown runtime preset"):
        get_preset("../outside")


def test_load_preset_returns_strict_deterministic_request() -> None:
    request = load_preset("closed-loop-feed-step")
    assert isinstance(request, RunRequest)
    assert request.run_type == "closed_loop_dynamic"
    assert not request.parameters
    assert not request.overrides
    identified = replace(
        request,
        run_id="run-0001",
        requested_at_utc="2026-08-18T00:00:00Z",
    )
    assert identified.request_fingerprint == request.request_fingerprint
    assert identified.as_dict()["run_id"] == "run-0001"


def test_runtime_preset_rejects_invalid_dynamic_grid() -> None:
    with pytest.raises(ValueError, match="requires duration"):
        RuntimePreset(
            preset_id="invalid-dynamic",
            run_type="open_loop_dynamic",
            engine_layer="M3",
            scenario_id="scenario-v1",
            description="invalid",
        )
    with pytest.raises(TypeError, match="must be numeric"):
        RuntimePreset(
            preset_id="invalid-grid",
            run_type="validation_scenario",
            engine_layer="M6_portable",
            scenario_id="scenario-v1",
            duration_s=True,
            time_step_s=1.0,
            description="invalid",
        )
