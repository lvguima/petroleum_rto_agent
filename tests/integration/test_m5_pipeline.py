from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.calibration import pipeline as pipeline_module
from petroleum_rto.cdu.calibration.pipeline import (
    CaseOverlayEvidence,
    M5PipelineError,
    M5PipelineResult,
    run_m5_pipeline,
)


@pytest.fixture(scope="module")
def result(repo_root: Path) -> M5PipelineResult:
    return run_m5_pipeline(repo_root)


def test_m5_pipeline_closes_sources_overlays_reconciliation_and_calibration(
    result: M5PipelineResult,
) -> None:
    assert result.versions["simulation_stage"] == "M5"
    assert result.versions["claim_scope"] == "case_alignment_only"
    assert result.overlays.flash_temperature_k["effective"] == pytest.approx(473.75)
    assert result.overlays.wash_water_ratio["effective"] == pytest.approx(18.97 / 407.60)
    derived_uncertainty = result.overlays.wash_water_ratio[
        "derived_uncertainty_kg_per_kg"
    ]
    uncertainty_basis = result.overlays.wash_water_ratio["uncertainty_basis"]
    assert isinstance(derived_uncertainty, (int, float))
    assert not isinstance(derived_uncertainty, bool) and derived_uncertainty > 0.0
    assert isinstance(uncertainty_basis, str)
    assert "not statistical one-sigma" in uncertainty_basis
    assert result.overlays.wash_water_ratio["revision_reason"]
    assert result.prior_m2.product_flows_kg_s["offgas"] == pytest.approx(
        0.375830750463,
        rel=1e-9,
    )
    assert abs(result.reconciliation.post_reconciliation_residual_kg_s) < 1e-10
    assert result.apparent_hydrocarbon_residual_fraction == pytest.approx(
        (407.3 - 403.67) / 407.3
    )
    assert result.calibration.calibrated.total_objective < (
        result.calibration.initial.total_objective
    )
    assert result.calibration.initial_sensitivity.numerical_rank == 2
    assert result.calibration.calibrated_sensitivity.numerical_rank == 2
    assert not result.calibration.boundary_hits
    assert result.final_m2.unit_balances_passed


def test_final_m2_is_the_exact_calibrated_evaluation(result: M5PipelineResult) -> None:
    assert result.prior_m2.input_fingerprint == (
        result.calibration.initial.m2_input_fingerprint
    )
    assert result.final_m2.input_fingerprint == (
        result.calibration.calibrated.m2_input_fingerprint
    )
    for name in ("light_diesel", "heavy_diesel", "residue"):
        assert result.final_m2.product_flows_kg_s[name] == (
            result.calibration.calibrated.predictions_kg_s[name]
        )
    assert result.fingerprints["calibrated_model_object"] == (
        result.calibration.fingerprints["calibrated_model"]
    )
    assert result.fingerprints["effective_model_object"] == (
        result.calibration.fingerprints["base_model"]
    )
    assert result.fingerprints["effective_case_object"] == (
        result.calibration.fingerprints["case"]
    )


def test_targets_are_reconciled_not_raw_or_model_prior(result: M5PipelineResult) -> None:
    for name in ("light_diesel", "heavy_diesel", "residue"):
        assert result.calibration_targets_kg_s[name] == (
            result.reconciliation.entries[name].reconciled_kg_s
        )
        assert result.calibration.initial.targets_kg_s[name] == (
            result.reconciliation.entries[name].reconciled_kg_s
        )
    assert all(
        item.basis == "latent_prior"
        for name, item in result.reconciliation.entries.items()
        if name in {"offgas", "aqueous", "brine"}
    )


def test_observation_sets_partition_the_dynamic_catalog_and_all_offsets_are_recorded(
    result: M5PipelineResult,
) -> None:
    catalog_ids = tuple(result.observation_catalog_evidence)
    selected_ids = tuple(result.selected_observations)
    review_ids = result.review_only_observation_ids

    assert len(selected_ids) == 11
    assert set(selected_ids).isdisjoint(review_ids)
    assert set(selected_ids) | set(review_ids) == set(catalog_ids)
    assert tuple(result.observation_offsets_s) == catalog_ids
    assert review_ids == tuple(item for item in catalog_ids if item not in selected_ids)
    assert all(
        abs(result.observation_offsets_s[item])
        <= result.alignment.maximum_alignment_offset_s
        for item in selected_ids
    )
    assert any(
        abs(result.observation_offsets_s[item])
        > result.alignment.maximum_alignment_offset_s
        for item in review_ids
    )
    assert {
        "obs-dcs-flash-pressure-pi-1002",
        "obs-dcs-tower-top-pressure-pi-1003",
    }.issubset(review_ids)


def test_same_screen_and_internal_flow_semantics_are_fixed(
    result: M5PipelineResult,
) -> None:
    measurements = {
        item.stream_id: result.selected_observations[item.observation_id]
        for item in result.alignment.boundary_measurements
    }
    same_screen = [
        measurements[name]
        for name in (
            "fresh_feed",
            "gasoline",
            "kerosene",
            "light_diesel",
            "heavy_diesel",
            "residue",
        )
    ]
    for field in ("source_id", "alignment_group", "observed_at", "alignment_quality"):
        assert len({item[field] for item in same_screen}) == 1

    reflux = result.selected_observations["obs-dcs-overhead-reflux-fi-1010"]
    top_circulation = result.selected_observations[
        "obs-dcs-top-circulation-fi-1003"
    ]
    assert (reflux["variable_role"], reflux["usage"], reflux["status"]) == (
        "internal_reflux",
        "diagnostic_reference",
        "reference_only",
    )
    assert (
        top_circulation["variable_role"],
        top_circulation["usage"],
        top_circulation["status"],
    ) == ("internal_circulation", "do_not_use", "excluded")
    assert set(result.reconciliation.excluded_internal) == {
        "reflux",
        "top_circulation",
    }


def test_pipeline_result_serializes_mixed_sources_without_embedding_base_version_model(
    result: M5PipelineResult,
) -> None:
    payload = result.as_dict()
    assert payload["data_origin"] == "mixed_sources"
    assert "synthetic" not in payload
    prior_m2 = payload["prior_m2"]
    assert isinstance(prior_m2, dict)
    assert prior_m2["synthetic"] is True
    assert prior_m2["data_origin"] == "M2_steady_model_prediction"
    parameter_set = payload["calibrated_parameter_set"]
    assert isinstance(parameter_set, dict)
    assert "model" not in parameter_set
    assert "base_model_reference" in parameter_set
    assert "operating_overlays" in parameter_set
    assert "parameter_overrides" in parameter_set
    assert "calibrated_model_fingerprint" in parameter_set


def test_result_rejects_forged_final_m2_and_predictions(result: M5PipelineResult) -> None:
    forged_fingerprint = replace(result.final_m2, input_fingerprint="0" * 64)
    with pytest.raises(ValueError, match="final M2 differs"):
        replace(result, final_m2=forged_fingerprint)

    forged_flows = dict(result.final_m2.product_flows_kg_s)
    forged_flows["light_diesel"] += 1.0
    forged_prediction = replace(result.final_m2, product_flows_kg_s=forged_flows)
    with pytest.raises(ValueError, match="predictions differ"):
        replace(result, final_m2=forged_prediction)


def test_result_rejects_incomplete_or_inconsistent_observation_sets(
    result: M5PipelineResult,
) -> None:
    selected = dict(result.selected_observations)
    selected.pop(next(iter(selected)))
    with pytest.raises(ValueError, match="selected observations differ"):
        replace(result, selected_observations=selected)

    with pytest.raises(ValueError, match="complete catalog complement"):
        replace(
            result,
            review_only_observation_ids=result.review_only_observation_ids[:-1],
        )

    offsets = dict(result.observation_offsets_s)
    offsets.pop(result.review_only_observation_ids[0])
    with pytest.raises(ValueError, match="complete catalog"):
        replace(result, observation_offsets_s=offsets)

    offsets = dict(result.observation_offsets_s)
    offsets[result.review_only_observation_ids[0]] += 1.0
    with pytest.raises(ValueError, match="does not match observed_at"):
        replace(result, observation_offsets_s=offsets)


def test_result_rejects_forged_same_screen_or_internal_semantics(
    result: M5PipelineResult,
) -> None:
    catalog = {
        key: dict(value) for key, value in result.observation_catalog_evidence.items()
    }
    selected = {key: dict(value) for key, value in result.selected_observations.items()}
    gasoline_id = "obs-dcs-gasoline-product"
    catalog[gasoline_id]["source_id"] = "src-forged"
    selected[gasoline_id]["source_id"] = "src-forged"
    with pytest.raises(ValueError, match="same-screen observations differ"):
        replace(
            result,
            observation_catalog_evidence=catalog,
            selected_observations=selected,
        )

    catalog = {
        key: dict(value) for key, value in result.observation_catalog_evidence.items()
    }
    selected = {key: dict(value) for key, value in result.selected_observations.items()}
    reflux_id = "obs-dcs-overhead-reflux-fi-1010"
    for payload in (catalog[reflux_id], selected[reflux_id]):
        payload["variable_role"] = "internal_circulation"
        payload["usage"] = "do_not_use"
        payload["status"] = "excluded"
    with pytest.raises(ValueError, match="reflux observation semantics"):
        replace(
            result,
            observation_catalog_evidence=catalog,
            selected_observations=selected,
        )


def test_result_rejects_fingerprint_and_version_tampering(
    result: M5PipelineResult,
) -> None:
    fingerprints = dict(result.fingerprints)
    fingerprints.pop("calibration_result")
    with pytest.raises(ValueError, match="fingerprint keys"):
        replace(result, fingerprints=fingerprints)

    fingerprints = dict(result.fingerprints)
    fingerprints["alignment_file"] = fingerprints["alignment_file"].upper()
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(result, fingerprints=fingerprints)

    fingerprints = dict(result.fingerprints)
    fingerprints["calibration_result"] = "0" * 64
    with pytest.raises(ValueError, match="object fingerprints differ"):
        replace(result, fingerprints=fingerprints)

    fingerprints = dict(result.fingerprints)
    fingerprints["baseline_model_object"] = "0" * 64
    with pytest.raises(ValueError, match="object fingerprints differ"):
        replace(result, fingerprints=fingerprints)

    versions = dict(result.versions)
    versions["simulation_stage"] = "M4"
    with pytest.raises(ValueError, match="versions differ"):
        replace(result, versions=versions)


def test_overlay_contract_rejects_uncertainty_or_revision_forgery(
    result: M5PipelineResult,
) -> None:
    wash = dict(result.overlays.wash_water_ratio)
    raw_uncertainty = wash["derived_uncertainty_kg_per_kg"]
    assert isinstance(raw_uncertainty, (int, float)) and not isinstance(
        raw_uncertainty, bool
    )
    wash["derived_uncertainty_kg_per_kg"] = float(raw_uncertainty) + 1.0
    with pytest.raises(ValueError, match="derived uncertainty"):
        replace(result.overlays, wash_water_ratio=wash)

    wash = dict(result.overlays.wash_water_ratio)
    wash["revision_reason"] = ""
    with pytest.raises(ValueError, match="revision_reason"):
        CaseOverlayEvidence(
            flash_temperature_k=result.overlays.flash_temperature_k,
            wash_water_ratio=wash,
        )


def test_pipeline_wraps_overlay_reconciliation_and_calibration_failures(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_overlay(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("forced overlay failure")

    with monkeypatch.context() as patch:
        patch.setattr(pipeline_module, "_effective_inputs", fail_overlay)
        with pytest.raises(M5PipelineError) as captured:
            run_m5_pipeline(repo_root)
        assert captured.value.stage == "case_overlay"

    def fail_reconciliation(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("forced reconciliation failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            pipeline_module,
            "reconcile_boundary_flows",
            fail_reconciliation,
        )
        with pytest.raises(M5PipelineError) as captured:
            run_m5_pipeline(repo_root)
        assert captured.value.stage == "reconciliation"

    def fail_calibration(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("forced calibration failure")

    with monkeypatch.context() as patch:
        patch.setattr(pipeline_module, "run_calibration", fail_calibration)
        with pytest.raises(M5PipelineError) as captured:
            run_m5_pipeline(repo_root)
        assert captured.value.stage == "calibration"


def test_pipeline_wraps_prior_and_final_m2_failures(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_solve = pipeline_module.__dict__["solve_recycle"]
    assert callable(original_solve)

    def fail_prior(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("forced prior failure")

    with monkeypatch.context() as patch:
        patch.setattr(pipeline_module, "solve_recycle", fail_prior)
        with pytest.raises(M5PipelineError) as captured:
            run_m5_pipeline(repo_root)
        assert captured.value.stage == "effective_baseline_m2"

    call_count = 0

    def fail_final(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ValueError("forced final failure")
        return original_solve(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline_module, "solve_recycle", fail_final)
        with pytest.raises(M5PipelineError) as captured:
            run_m5_pipeline(repo_root)
        assert captured.value.stage == "final_m2"


def test_pipeline_result_is_exactly_repeatable(
    repo_root: Path,
    result: M5PipelineResult,
) -> None:
    repeated = run_m5_pipeline(repo_root)
    assert repeated.as_dict() == result.as_dict()
    assert repeated.result_fingerprint == result.result_fingerprint
