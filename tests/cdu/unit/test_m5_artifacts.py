from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.cdu.calibration import artifacts as artifacts_module
from petroleum_rto.cdu.calibration.artifacts import (
    calibrated_parameter_payload,
    calibration_report_payload,
    reconciled_case_payload,
    write_m5_artifacts,
)
from petroleum_rto.cdu.calibration.pipeline import M5PipelineResult, run_m5_pipeline
from petroleum_rto.cdu.core.config import canonical_fingerprint
from petroleum_rto.cdu.repository import resolve_cdu_repository_path


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _json_object(path: Path) -> dict[str, object]:
    return _mapping(json.loads(path.read_text(encoding="utf-8")))


def _write_fingerprinted_json(path: Path, payload: dict[str, object]) -> None:
    unsigned = dict(payload)
    unsigned.pop("artifact_fingerprint", None)
    payload["artifact_fingerprint"] = canonical_fingerprint(unsigned)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _fitted_observation_ids(result: M5PipelineResult) -> set[str]:
    return {
        item.observation_id
        for item in result.alignment.boundary_measurements
        if item.stream_id in {"light_diesel", "heavy_diesel", "residue"}
    }


def _unfitted_observation_ids(result: M5PipelineResult) -> set[str]:
    return set(result.observation_catalog_evidence) - _fitted_observation_ids(result)


def _report_disclosures(report: dict[str, object]) -> dict[str, dict[str, object]]:
    disclosures = _sequence(report["unfitted_observations"])
    result: dict[str, dict[str, object]] = {}
    for value in disclosures:
        item = _mapping(value)
        observation_id = item["observation_id"]
        assert isinstance(observation_id, str)
        result[observation_id] = item
    return result


def test_artifact_payloads_separate_origins_and_only_calibrate_two_parameters(
    repo_root: Path,
) -> None:
    result = run_m5_pipeline(repo_root)
    gold = reconciled_case_payload(result, source_repo_root=repo_root)
    parameters = calibrated_parameter_payload(result)

    assert gold["claim_scope"] == "case_alignment_only"
    assert "synthetic" not in gold
    origin = _mapping(gold["origin_contract"])
    assert origin["artifact_origin"] == "mixed"
    sections = _mapping(gold["evidence_sections"])
    field = _mapping(sections["field_observations"])
    latent = _mapping(sections["m2_latent_priors"])
    predictions = _mapping(sections["m2_predictions"])
    assert field["synthetic"] is False
    assert len(_sequence(field["observations"])) == len(result.observation_catalog_evidence)
    assert latent["synthetic"] is True
    assert predictions["synthetic"] is True
    reconciliation_section = _mapping(sections["reconciliation"])
    reconciliation_evidence = _mapping(reconciliation_section["evidence"])
    assert reconciliation_section["origin"] == ("mixed_field_measurements_and_M2_latent_priors")
    assert reconciliation_section["synthetic"] is None
    assert reconciliation_evidence["data_origin"] == (
        "mixed_field_measurements_and_M2_latent_priors"
    )
    assert reconciliation_evidence["synthetic"] is None
    legacy_origin = _mapping(reconciliation_evidence["legacy_entry_origin"])
    assert legacy_origin["data_origin"] == "M5_reconciled_field_observations"
    assert legacy_origin["synthetic"] is False
    assert "superseded" in cast(str, legacy_origin["scope"])
    reconciliation = _mapping(gold["reconciliation"])
    assert reconciliation["result_fingerprint"] == (result.reconciliation.result_fingerprint)
    assert reconciliation["data_origin"] == ("mixed_field_measurements_and_M2_latent_priors")
    assert reconciliation["synthetic"] is None

    overlays = _sequence(parameters["parameter_overlays"])
    assert [_mapping(item)["path"] for item in overlays] == [
        "column.cut_points_k[2]",
        "column.cut_points_k[3]",
    ]
    assert parameters["boundary_hits"] == []
    assert parameters["versions"] == dict(result.versions)
    assert parameters["alignment_config_sha256"] == result.fingerprints["alignment_file"]
    assert parameters["alignment_config_fingerprint"] == (result.fingerprints["alignment_object"])
    assert (
        parameters["calibration_config_sha256"] == (result.fingerprints["calibration_config_file"])
    )
    assert (
        parameters["calibration_input_fingerprint"]
        == (result.calibration.fingerprints["input_bundle"])
    )
    assert len(cast(str, parameters["artifact_fingerprint"])) == 64


def test_report_discloses_catalog_complement_not_just_review_only(
    repo_root: Path,
) -> None:
    result = run_m5_pipeline(repo_root)
    report = calibration_report_payload(
        result,
        reconciled_case_sha256="a" * 64,
        parameter_set_sha256="b" * 64,
        source_repo_root=repo_root,
    )
    expected = _unfitted_observation_ids(result)
    reported = set(cast(list[str], report["unfitted_observation_ids"]))
    disclosures = _report_disclosures(report)

    assert len(_fitted_observation_ids(result)) == 3
    assert len(expected) == (
        len(result.observation_catalog_evidence) - len(_fitted_observation_ids(result))
    )
    assert reported == expected == set(disclosures)
    assert set(result.review_only_observation_ids) < expected
    assert "obs-dcs-fresh-feed-overview" in reported
    assert "obs-dcs-wash-water-fic-1107" in reported
    assert "obs-dcs-gasoline-product" in reported
    assert "obs-dcs-kerosene-product" in reported

    for item in disclosures.values():
        assert isinstance(item["si_value"], (int, float))
        assert isinstance(item["si_unit"], str)
        observed_at = cast(str, item["observed_at"])
        assert observed_at.endswith("+08:00")
        assert isinstance(item["offset_s"], (int, float))
        source = _mapping(item["source"])
        assert cast(str, source["source_path"]).startswith("base_files/")
        assert len(cast(str, source["source_sha256"])) == 64
        assert isinstance(item["usage"], str)
        assert isinstance(item["status"], str)
        assert cast(str, item["not_fitted_reason"]).strip()

    for observation_id, product in (
        ("obs-dcs-gasoline-product", "gasoline"),
        ("obs-dcs-kerosene-product", "kerosene"),
    ):
        item = disclosures[observation_id]
        comparison = _mapping(item["model_comparison"])
        observed = cast(float, item["si_value"])
        assert comparison["status"] == "comparable"
        assert comparison["initial_prediction_si"] == pytest.approx(
            result.prior_m2.product_flows_kg_s[product]
        )
        assert comparison["calibrated_prediction_si"] == pytest.approx(
            result.final_m2.product_flows_kg_s[product]
        )
        assert comparison["initial_bias_si"] == pytest.approx(
            result.prior_m2.product_flows_kg_s[product] - observed
        )
        assert comparison["calibrated_bias_si"] == pytest.approx(
            result.final_m2.product_flows_kg_s[product] - observed
        )

    comparable_ids = {
        observation_id
        for observation_id, item in disclosures.items()
        if _mapping(item["model_comparison"])["status"] == "comparable"
    }
    assert comparable_ids == {
        "obs-dcs-gasoline-product",
        "obs-dcs-kerosene-product",
    }
    for observation_id in expected - comparable_ids:
        comparison = _mapping(disclosures[observation_id]["model_comparison"])
        assert comparison["status"] == "no_compatible_model_output"
        assert cast(str, comparison["reason"]).strip()
    for observation_id in (
        "obs-dcs-fresh-feed-overview",
        "obs-dcs-flash-temperature-ti-1012",
        "obs-dcs-wash-ratio-feed-fi-1018",
        "obs-dcs-wash-water-fic-1107",
    ):
        reason = cast(
            str,
            _mapping(disclosures[observation_id]["model_comparison"])["reason"],
        )
        assert "not an independent model prediction" in reason


def test_writer_is_repeatable_and_json_markdown_disclose_the_same_full_set(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    result = run_m5_pipeline(repo_root)
    first = write_m5_artifacts(result, tmp_path, source_repo_root=repo_root)
    second = write_m5_artifacts(result, tmp_path, source_repo_root=repo_root)

    assert first.as_dict() == second.as_dict()
    assert set(first.paths) == {
        "reconciled_case",
        "calibrated_parameters",
        "report_json",
        "report_markdown",
        "artifact_manifest",
    }
    for name, relative_path in first.paths.items():
        artifact_path = resolve_cdu_repository_path(tmp_path, relative_path)
        assert artifact_path.is_file()
        assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == first.sha256[name]

    gold = _json_object(resolve_cdu_repository_path(tmp_path, first.paths["reconciled_case"]))
    parameters = _json_object(
        resolve_cdu_repository_path(tmp_path, first.paths["calibrated_parameters"])
    )
    report = _json_object(resolve_cdu_repository_path(tmp_path, first.paths["report_json"]))
    manifest = _json_object(resolve_cdu_repository_path(tmp_path, first.paths["artifact_manifest"]))
    markdown = (resolve_cdu_repository_path(tmp_path, first.paths["report_markdown"])).read_text(
        encoding="utf-8"
    )
    expected = _unfitted_observation_ids(result)
    report_ids = set(cast(list[str], report["unfitted_observation_ids"]))

    assert gold["artifact_type"] == "M5_reconciled_case"
    assert parameters["artifact_type"] == "M5_calibrated_parameter_set"
    assert _mapping(report["completion_checks"])["objective_strictly_improved"] is True
    assert _mapping(report["completion_checks"])["all_unfitted_observations_disclosed"] is True
    assert report_ids == expected
    assert "证据来源分层" in markdown
    assert "不构成现场验证" in markdown
    for observation_id in expected:
        assert markdown.count(f"`{observation_id}`") == 1
    assert "no_compatible_model_output" in markdown

    assert manifest["artifact_type"] == "M5_artifact_suite_manifest"
    assert manifest["manifest_path"] == first.paths["artifact_manifest"]
    manifest_artifacts = _mapping(manifest["artifacts"])
    assert set(manifest_artifacts) == set(first.paths) - {"artifact_manifest"}
    for name, value in manifest_artifacts.items():
        item = _mapping(value)
        assert item["path"] == first.paths[name]
        assert item["sha256"] == first.sha256[name]
    unsigned_manifest = dict(manifest)
    manifest_fingerprint = cast(str, unsigned_manifest.pop("artifact_fingerprint"))
    assert canonical_fingerprint(unsigned_manifest) == manifest_fingerprint
    assert manifest["pipeline_result_fingerprint"] == result.result_fingerprint


def test_validation_failure_publishes_no_new_parameter_set(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_m5_pipeline(repo_root)

    def reject_staged_bundle(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected staged validation failure")

    monkeypatch.setattr(
        artifacts_module,
        "_validate_staged_bundle",
        reject_staged_bundle,
    )
    with pytest.raises(RuntimeError, match="injected staged validation failure"):
        write_m5_artifacts(result, tmp_path, source_repo_root=repo_root)

    parameter_path = tmp_path / result.alignment.artifacts.calibrated_parameters
    assert not parameter_path.exists()
    assert not any(tmp_path.rglob("*.stage"))
    assert not any(tmp_path.rglob("*.backup"))


def test_staged_validation_rejects_nested_field_only_reconciliation_claim(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    result = run_m5_pipeline(repo_root)
    artifact_manifest = write_m5_artifacts(
        result,
        tmp_path,
        source_repo_root=repo_root,
    )
    staged = {
        name: resolve_cdu_repository_path(tmp_path, relative_path)
        for name, relative_path in artifact_manifest.paths.items()
    }
    gold_path = staged["reconciled_case"]
    gold = _json_object(gold_path)
    sections = _mapping(gold["evidence_sections"])
    reconciliation_section = _mapping(sections["reconciliation"])
    nested_evidence = _mapping(reconciliation_section["evidence"])
    nested_evidence["data_origin"] = "M5_reconciled_field_observations"
    nested_evidence["synthetic"] = False
    _write_fingerprinted_json(gold_path, gold)

    persistent_manifest_path = staged["artifact_manifest"]
    persistent_manifest = _json_object(persistent_manifest_path)
    manifest_artifacts = _mapping(persistent_manifest["artifacts"])
    gold_entry = _mapping(manifest_artifacts["reconciled_case"])
    gold_entry["sha256"] = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    _write_fingerprinted_json(persistent_manifest_path, persistent_manifest)

    report = _json_object(staged["report_json"])
    disclosure_ids = tuple(cast(list[str], report["unfitted_observation_ids"]))
    with pytest.raises(ValueError, match="must declare mixed reconciliation origin"):
        artifacts_module._validate_staged_bundle(
            staged,
            disclosure_ids=disclosure_ids,
        )


def test_mid_publish_failure_restores_every_previous_artifact(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_m5_pipeline(repo_root)
    relative_paths = result.alignment.artifacts.as_dict()
    targets = {name: tmp_path / value for name, value in relative_paths.items()}
    for name, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"previous-{name}\n", encoding="utf-8")
    report_target = targets["report_json"].resolve()
    original_replace = Path.replace
    failure_injected = False

    def fail_report_publication(self: Path, target: str | Path) -> Path:
        nonlocal failure_injected
        resolved_target = Path(target).resolve()
        if (
            not failure_injected
            and self.name.endswith(".stage")
            and resolved_target == report_target
        ):
            failure_injected = True
            raise OSError("injected report publication failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_report_publication)
    with pytest.raises(OSError, match="injected report publication failure"):
        write_m5_artifacts(result, tmp_path, source_repo_root=repo_root)

    assert failure_injected
    for name, target in targets.items():
        assert target.read_text(encoding="utf-8") == f"previous-{name}\n"
    assert not any(tmp_path.rglob("*.stage"))
    assert not any(tmp_path.rglob("*.backup"))


@pytest.mark.parametrize(
    ("artifact_name", "relative_path"),
    [
        ("reconciled_case", "base_files/forbidden.json"),
        ("reconciled_case", "data/gold/../forbidden.json"),
        ("calibrated_parameters", "configs/model/cdu_model_v0.1.0.json"),
        ("calibrated_parameters", "base_files/forbidden.json"),
        ("report_json", "data/gold/forbidden.json"),
        ("report_markdown", "reports/modeling/forbidden.json"),
        ("artifact_manifest", "reports/forbidden.json"),
    ],
)
def test_artifact_classes_are_confined_to_their_fixed_output_roots(
    tmp_path: Path,
    artifact_name: str,
    relative_path: str,
) -> None:
    with pytest.raises(ValueError):
        artifacts_module._safe_output(
            tmp_path,
            relative_path,
            artifact_name=artifact_name,
        )
