from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from petroleum_rto.cdu.calibration import run_m5_pipeline, write_m5_artifacts
from petroleum_rto.cdu.calibration.etl import file_sha256
from petroleum_rto.cdu.repository import resolve_cdu_repository_path

_PIPELINE_FINGERPRINT = "9e7bbda6a4f534008d847c49a42b2ee6526fb7132a5ca5db52a112ccf56941b7"
_EXPECTED_SHA256 = {
    "reconciled_case": "ab3f4e60c88b4f11d450ca7bcf0dd32c0860ba7e866eace17f0eca35a58bf2e5",
    "calibrated_parameters": "d837f32c321c5ba7d5fbe82828b0d4b5112c926b57c01460866c93fdef66f816",
    "report_json": "e730080b10396a25d292c0e0c220984f8c0cd5416e6f7e1d3f6a471e1565fcd2",
    "report_markdown": "1ae81105c8adcc59bb7008e1969031ab276aaabe3a4eaa63a9e8a061febb7484",
    "artifact_manifest": "948f400bf265855aa7a3c928307f6486660f12cbbada6f031c52544ed71e562b",
}


def _json_object(path: Path) -> dict[str, object]:
    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    assert all(isinstance(key, str) for key in decoded)
    return cast(dict[str, object], decoded)


def test_delivered_m5_suite_matches_a_fresh_source_verified_run(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    result = run_m5_pipeline(repo_root)
    regenerated = write_m5_artifacts(
        result,
        tmp_path,
        source_repo_root=repo_root,
    )

    assert result.result_fingerprint == _PIPELINE_FINGERPRINT
    assert dict(regenerated.sha256) == _EXPECTED_SHA256
    for name, relative_path in regenerated.paths.items():
        delivered = resolve_cdu_repository_path(repo_root, relative_path)
        replayed = resolve_cdu_repository_path(tmp_path, relative_path)
        assert delivered.is_file()
        assert replayed.is_file()
        assert file_sha256(delivered) == _EXPECTED_SHA256[name]
        assert delivered.read_bytes() == replayed.read_bytes()

    gold = _json_object(
        resolve_cdu_repository_path(repo_root, regenerated.paths["reconciled_case"])
    )
    parameters = _json_object(
        resolve_cdu_repository_path(
            repo_root,
            regenerated.paths["calibrated_parameters"],
        )
    )
    report = _json_object(
        resolve_cdu_repository_path(repo_root, regenerated.paths["report_json"])
    )
    manifest = _json_object(
        resolve_cdu_repository_path(repo_root, regenerated.paths["artifact_manifest"])
    )

    assert gold["origin_contract"]["artifact_origin"] == "mixed"  # type: ignore[index]
    offsets = cast(dict[str, float], gold["observation_offsets_s"])
    assert len(offsets) == 19
    overlays = cast(list[dict[str, object]], parameters["parameter_overlays"])
    assert [item["path"] for item in overlays] == [
        "column.cut_points_k[2]",
        "column.cut_points_k[3]",
    ]
    checks = cast(dict[str, bool], report["completion_checks"])
    assert checks and all(checks.values())
    disclosures = cast(list[dict[str, object]], report["unfitted_observations"])
    assert len(disclosures) == 16
    assert {item["observation_id"] for item in disclosures} == set(
        cast(list[str], report["unfitted_observation_ids"])
    )
    assert manifest["status"] == "valid"
    assert manifest["pipeline_result_fingerprint"] == _PIPELINE_FINGERPRINT

    for directory in (repo_root / "data/cdu/gold", repo_root / "configs/cdu/parameters", repo_root / "reports/cdu"):
        assert not tuple(directory.glob(".*.stage"))
        assert not tuple(directory.glob(".*.backup"))
