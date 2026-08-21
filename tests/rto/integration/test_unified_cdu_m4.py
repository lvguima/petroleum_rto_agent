from __future__ import annotations

from pathlib import Path

from petroleum_rto.rto.adapters import CduM7Simulator
from tests.rto.unit.test_unified_m4_evaluation import _m4_basis


def test_unified_m4_pair_previews_against_real_cdu_without_running_dynamic_simulation(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _, context, _, _, pair = _m4_basis(repo_root, multi=False)
    simulator = CduM7Simulator(tmp_path / "runs")

    baseline = simulator.preview(pair.baseline)
    candidate = simulator.preview(pair.candidate)

    assert baseline.base_object_fingerprints["model"] == context.model_ref.fingerprint
    assert candidate.base_object_fingerprints["case"] == context.case_ref.fingerprint
    assert baseline.provider_preview_fingerprint != candidate.provider_preview_fingerprint
    assert baseline.effective_input_fingerprint != candidate.effective_input_fingerprint
    assert not (tmp_path / "runs").exists()
