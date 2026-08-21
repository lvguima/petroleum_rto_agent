from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.runtime.api import run
from petroleum_rto.cdu.runtime.artifacts import read_run
from petroleum_rto.cdu.runtime.presets import get_preset, load_preset
from petroleum_rto.cdu.runtime.resources import runtime_resource_ids_for_preset


@pytest.mark.parametrize(
    ("preset_id", "expected_status", "expected_samples"),
    (
        ("steady-baseline", "success", 0),
        ("open-loop-feed-step", "success", 7201),
        ("closed-loop-feed-step", "success", 7201),
        ("m6-abnormal-pump-trip", "limited", 601),
        ("m6-structural-rejection", "rejected", 0),
    ),
)
def test_all_public_presets_execute_publish_and_reload(
    tmp_path: Path,
    preset_id: str,
    expected_status: str,
    expected_samples: int,
) -> None:
    request = replace(load_preset(preset_id), run_id=f"integration-{preset_id}")

    written = run(request, output_root=tmp_path)
    reloaded = read_run(written.run_dir)

    assert reloaded.payload.runtime_status == expected_status
    assert len(reloaded.payload.timeseries) == expected_samples
    assert reloaded.payload.result_fingerprint == written.payload.result_fingerprint
    assert reloaded.manifest.manifest_fingerprint == written.manifest.manifest_fingerprint
    assert reloaded.manifest.artifact_state == "complete"
    assert sum(key.startswith("input.") for key in reloaded.manifest.artifacts) == len(
        runtime_resource_ids_for_preset(get_preset(preset_id))
    )

    if preset_id in {"open-loop-feed-step", "closed-loop-feed-step"}:
        assert any(event.time_s == 600.0 for event in reloaded.payload.events)
    if preset_id == "m6-abnormal-pump-trip":
        assert any(event.time_s == 60.0 for event in reloaded.payload.events)
        assert any(event.time_s == 62.0 for event in reloaded.payload.events)
    if preset_id == "m6-structural-rejection":
        assert reloaded.payload.engine_status == "not_called"
