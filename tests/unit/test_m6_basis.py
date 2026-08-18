from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.cdu.calibration import run_m5_pipeline
from petroleum_rto.cdu.calibration.etl import file_sha256
from petroleum_rto.cdu.core.config import canonical_fingerprint
from petroleum_rto.cdu.validation import basis as basis_module
from petroleum_rto.cdu.validation.basis import M6Basis, load_m6_basis

_ANALYSIS_BASIS_FINGERPRINT = (
    "4c12146b6fb14cb033b0e05f64e68093f28087482f55128aed5aa56c37dfffed"
)
_PIPELINE_FINGERPRINT = (
    "9e7bbda6a4f534008d847c49a42b2ee6526fb7132a5ca5db52a112ccf56941b7"
)
_MANIFEST_SHA256 = (
    "948f400bf265855aa7a3c928307f6486660f12cbbada6f031c52544ed71e562b"
)
_MANIFEST_FINGERPRINT = (
    "01c9cd02442e62da12f7263ce881dc566fdb25a721b0174ee4955544c5b124b1"
)
_ARTIFACT_SHA256 = {
    "reconciled_case": (
        "ab3f4e60c88b4f11d450ca7bcf0dd32c0860ba7e866eace17f0eca35a58bf2e5"
    ),
    "calibrated_parameters": (
        "d837f32c321c5ba7d5fbe82828b0d4b5112c926b57c01460866c93fdef66f816"
    ),
    "report_json": (
        "e730080b10396a25d292c0e0c220984f8c0cd5416e6f7e1d3f6a471e1565fcd2"
    ),
    "report_markdown": (
        "1ae81105c8adcc59bb7008e1969031ab276aaabe3a4eaa63a9e8a061febb7484"
    ),
}
_OBJECT_FINGERPRINTS = {
    "calibrated_model_object": (
        "49c04b222ab817ca2b27922171c832d562adcd55e80cda480c94b3142e0d2473"
    ),
    "effective_case_object": (
        "6687e0637c0cfbef46f6020b52751058325363f2e175ced59b3ba4469658585e"
    ),
    "component_catalog_object": (
        "e4d719fb1e0dbd71f5465644fbd051494661c480bd06a22c467a5c66410c81ec"
    ),
}


@pytest.fixture(scope="module")
def m6_basis(repo_root: Path) -> Iterator[M6Basis]:
    yield load_m6_basis(repo_root)


def test_load_m6_basis_reconstructs_the_source_closed_effective_inputs(
    m6_basis: M6Basis,
) -> None:
    assert m6_basis.schema_version == "1.0.0"
    assert m6_basis.analysis_version == "m6-basis-v0.1.0"
    assert m6_basis.base_parameter_set_version == "cdu-parameters-0.1.0"
    assert (
        m6_basis.derived_parameter_set_version
        == "cdu-parameters-m5-case20260604-v0.1.0"
    )
    assert m6_basis.base_case_version == "case-20260604-v0.1.0"
    assert m6_basis.derived_case_version == "case-20260604-m5-aligned-v0.1.0"

    assert m6_basis.model.parameter_set_version == m6_basis.base_parameter_set_version
    assert m6_basis.case.parameter_set_version == m6_basis.base_parameter_set_version
    assert m6_basis.catalog.parameter_set_version == m6_basis.base_parameter_set_version
    assert m6_basis.case.case_version == m6_basis.base_case_version
    assert m6_basis.case.operating_conditions["flash_temperature_k"] == 473.75
    assert m6_basis.model.equipment["desalter"][
        "wash_water_ratio"
    ] == pytest.approx(0.04654072620215898)
    assert m6_basis.model.equipment["column"]["cut_points_k"] == (
        448.15,
        524.15,
        571.7046875,
        647.9546875,
    )
    assert dict(m6_basis.metadata) == {
        "synthetic": "true",
        "data_origin": "M6_synthetic_validation",
        "claim_scope": "engineering_validation_only",
    }


def test_m6_basis_freezes_all_m5_and_effective_object_evidence(
    m6_basis: M6Basis,
) -> None:
    assert m6_basis.m5_pipeline_fingerprint == _PIPELINE_FINGERPRINT
    assert m6_basis.m5_manifest_sha256 == _MANIFEST_SHA256
    assert m6_basis.m5_manifest_fingerprint == _MANIFEST_FINGERPRINT
    assert dict(m6_basis.m5_artifact_sha256) == _ARTIFACT_SHA256
    assert dict(m6_basis.effective_object_fingerprints) == _OBJECT_FINGERPRINTS
    assert m6_basis.analysis_basis_fingerprint == _ANALYSIS_BASIS_FINGERPRINT


def test_m6_basis_is_deeply_immutable_and_serializes_deterministically(
    m6_basis: M6Basis,
) -> None:
    first = m6_basis.as_dict()
    second = m6_basis.as_dict()
    assert first == second
    assert first["analysis_basis_fingerprint"] == _ANALYSIS_BASIS_FINGERPRINT
    assert canonical_fingerprint(
        {key: value for key, value in first.items() if key != "analysis_basis_fingerprint"}
    ) == _ANALYSIS_BASIS_FINGERPRINT

    with pytest.raises(TypeError):
        cast(dict[str, str], m6_basis.metadata)["synthetic"] = "false"
    with pytest.raises(TypeError):
        cast(dict[str, str], m6_basis.m5_artifact_sha256)["report_json"] = "0" * 64
    with pytest.raises(TypeError):
        cast(dict[str, object], m6_basis.model.equipment)["new_unit"] = {}

    serialized_metadata = cast(dict[str, str], first["metadata"])
    serialized_metadata["synthetic"] = "changed-copy"
    assert m6_basis.metadata["synthetic"] == "true"
    assert m6_basis.as_dict() == second


def _rewrite_fingerprinted_json(path: Path, payload: dict[str, object]) -> None:
    unsigned = dict(payload)
    unsigned.pop("artifact_fingerprint", None)
    payload["artifact_fingerprint"] = canonical_fingerprint(unsigned)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_m5_parameter_tampering_is_rejected_even_with_rehashed_artifacts(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    result = run_m5_pipeline(repo_root)
    artifact_paths = result.alignment.artifacts.as_dict()
    for relative_path in artifact_paths.values():
        source = repo_root / relative_path
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    parameter_path = tmp_path / artifact_paths["calibrated_parameters"]
    parameter_payload = cast(
        dict[str, object],
        json.loads(parameter_path.read_text(encoding="utf-8")),
    )
    parameter_payload["pipeline_result_fingerprint"] = "0" * 64
    _rewrite_fingerprinted_json(parameter_path, parameter_payload)

    manifest_path = tmp_path / artifact_paths["artifact_manifest"]
    manifest_payload = cast(
        dict[str, object],
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )
    manifest_artifacts = cast(
        dict[str, dict[str, object]],
        manifest_payload["artifacts"],
    )
    manifest_artifacts["calibrated_parameters"]["sha256"] = file_sha256(
        parameter_path
    )
    _rewrite_fingerprinted_json(manifest_path, manifest_payload)

    with pytest.raises(
        ValueError,
        match="parameter artifact fields differ.*pipeline_result_fingerprint",
    ):
        basis_module._validate_m5_artifact_suite(tmp_path, result)
