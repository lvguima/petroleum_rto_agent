"""Deterministic, source-explicit and transactional M5 artifact writers."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast

from ..core.config import canonical_fingerprint
from .etl import file_sha256
from .observations import Observation, load_observation_catalog
from .pipeline import M5PipelineResult

_FITTED_STREAM_IDS = frozenset({"light_diesel", "heavy_diesel", "residue"})
_MIXED_RECONCILIATION_ORIGIN = "mixed_field_measurements_and_M2_latent_priors"
_PRODUCT_VARIABLES: Mapping[str, str] = MappingProxyType(
    {
        "gasoline_product_mass_flow": "gasoline",
        "kerosene_product_mass_flow": "kerosene",
    }
)
_OUTPUT_ROOTS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "reconciled_case": ("data/gold", ".json"),
        "calibrated_parameters": ("configs/parameters", ".json"),
        "report_json": ("reports/modeling", ".json"),
        "report_markdown": ("reports/modeling", ".md"),
        "artifact_manifest": ("reports/modeling", ".json"),
    }
)
_PUBLISH_ORDER = (
    "reconciled_case",
    "calibrated_parameters",
    "report_json",
    "report_markdown",
    "artifact_manifest",
)


def _artifact_fingerprint(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["artifact_fingerprint"] = canonical_fingerprint(payload)
    return result


def _json_text(payload: dict[str, object]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_repo_input(repo_root: Path, relative_path: str) -> Path:
    if "\\" in relative_path:
        raise ValueError("source paths must use repository-relative POSIX separators")
    parsed = PurePosixPath(relative_path)
    if parsed.is_absolute() or not parsed.parts or "." in parsed.parts or ".." in parsed.parts:
        raise ValueError(f"unsafe repository-relative source path: {relative_path}")
    root = repo_root.resolve()
    result = (root / parsed).resolve()
    try:
        result.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source path escapes repository: {relative_path}") from exc
    return result


def _load_result_observations(
    result: M5PipelineResult,
    source_repo_root: Path | None,
) -> tuple[Observation, ...]:
    observations = tuple(
        Observation.from_mapping(payload)
        for payload in result.observation_catalog_evidence.values()
    )
    observed_fingerprint = canonical_fingerprint(
        {"observations": [item.as_dict() for item in observations]}
    )
    if observed_fingerprint != result.fingerprints["observation_objects"]:
        raise ValueError("observation evidence differs from the successful pipeline result")
    if source_repo_root is not None:
        root = source_repo_root.resolve()
        catalog_path = _safe_repo_input(root, result.alignment.paths.observation_catalog)
        source_observations = load_observation_catalog(catalog_path, repo_root=root)
        if tuple(item.as_dict() for item in source_observations) != tuple(
            item.as_dict() for item in observations
        ):
            raise ValueError("source catalog differs from pipeline observation evidence")
    return observations


def _fitted_observation_ids(result: M5PipelineResult) -> frozenset[str]:
    if set(result.calibration_targets_kg_s) != _FITTED_STREAM_IDS:
        raise ValueError("calibration targets differ from the fixed three-stream contract")
    fitted = frozenset(
        item.observation_id
        for item in result.alignment.boundary_measurements
        if item.stream_id in _FITTED_STREAM_IDS
    )
    if len(fitted) != len(_FITTED_STREAM_IDS):
        raise ValueError("each fixed calibration target must map to one observation")
    return fitted


def _case_input_reason(observation: Observation) -> str | None:
    if observation.variable == "fresh_feed_mass_flow":
        return (
            "Observed net-boundary feed used for case data coordination; it is a case "
            "input, not an independent model prediction."
        )
    if observation.variable == "desalter_wash_water_mass_flow":
        return (
            "Observed wash-water inlet used for data coordination and the case wash-ratio "
            "overlay; it is a case input, not an independent model prediction."
        )
    if observation.variable == "wash_water_ratio_feed_denominator_mass_flow":
        return (
            "Reference denominator used only to construct the case wash-ratio overlay; it "
            "is a case input, not an independent model prediction."
        )
    if observation.variable == "flash_temperature":
        return (
            "TI-1012 defines the case flash-temperature overlay; it is a case input, not "
            "an independent model prediction."
        )
    return None


def _no_model_output_reason(observation: Observation) -> str:
    case_reason = _case_input_reason(observation)
    if case_reason is not None:
        return case_reason
    if observation.variable in {"flash_pressure_gauge", "tower_top_pressure_gauge"}:
        return (
            "The stored M2 evidence does not expose a location-matched gauge-pressure "
            "observable; gauge-to-absolute assumptions also require field confirmation."
        )
    if observation.variable in {
        "flash_feed_temperature",
        "flash_bottom_temperature",
        "furnace_feed_temperature",
    }:
        return (
            "The stored M2 evidence does not expose a location-matched temperature "
            "observable, so no defensible like-for-like prediction is available."
        )
    if observation.variable in {
        "tower_overhead_reflux_mass_flow",
        "tower_top_circulation_mass_flow",
    }:
        return (
            "The stored M2 evidence exposes net product flows only, not this internal "
            "circulation flow; it must not be compared with a net-boundary prediction."
        )
    if observation.variable_role == "quality_anchor":
        return (
            "The current M2 evidence does not expose a matching laboratory FBP/T95 "
            "observable under the reported test method."
        )
    return "The current M2 evidence does not expose a compatible observable for this record."


def _not_fitted_reason(observation: Observation) -> str:
    case_reason = _case_input_reason(observation)
    if case_reason is not None:
        return case_reason
    if observation.variable in _PRODUCT_VARIABLES:
        return (
            "Measured product flow retained for data coordination and out-of-objective "
            "diagnosis; the fixed calibration objective uses only light diesel, heavy "
            "diesel and residue."
        )
    if observation.exclusion_reason is not None:
        return observation.exclusion_reason
    return (
        "This observation is outside the fixed light-diesel/heavy-diesel/residue "
        "calibration target set."
    )


@dataclass(frozen=True)
class _ModelComparison:
    status: str
    initial_prediction_si: float | None
    calibrated_prediction_si: float | None
    initial_bias_si: float | None
    calibrated_bias_si: float | None
    si_unit: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "initial_prediction_si": self.initial_prediction_si,
            "calibrated_prediction_si": self.calibrated_prediction_si,
            "initial_bias_si": self.initial_bias_si,
            "calibrated_bias_si": self.calibrated_bias_si,
            "si_unit": self.si_unit,
            "bias_definition": "model_prediction_minus_observation",
            "reason": self.reason,
        }


def _model_comparison(
    result: M5PipelineResult,
    observation: Observation,
) -> _ModelComparison:
    product = _PRODUCT_VARIABLES.get(observation.variable)
    if product is None:
        return _ModelComparison(
            status="no_compatible_model_output",
            initial_prediction_si=None,
            calibrated_prediction_si=None,
            initial_bias_si=None,
            calibrated_bias_si=None,
            si_unit=observation.si_unit,
            reason=_no_model_output_reason(observation),
        )
    if observation.si_unit != "kg/s":
        raise ValueError(f"comparable product observation {observation.id} must use kg/s")
    initial = result.prior_m2.product_flows_kg_s[product]
    calibrated = result.final_m2.product_flows_kg_s[product]
    return _ModelComparison(
        status="comparable",
        initial_prediction_si=initial,
        calibrated_prediction_si=calibrated,
        initial_bias_si=initial - observation.si_value,
        calibrated_bias_si=calibrated - observation.si_value,
        si_unit=observation.si_unit,
        reason=(
            "The catalog value and both M2 evidence objects expose the same net-product "
            "mass-flow basis."
        ),
    )


@dataclass(frozen=True)
class _ObservationDisclosure:
    observation: Observation
    offset_s: float
    not_fitted_reason: str
    comparison: _ModelComparison

    def as_dict(self) -> dict[str, object]:
        item = self.observation
        return {
            "observation_id": item.id,
            "variable": item.variable,
            "variable_role": item.variable_role,
            "raw_value": item.raw_value,
            "raw_unit": item.raw_unit,
            "si_value": item.si_value,
            "si_unit": item.si_unit,
            "observed_at": item.observed_at.isoformat(timespec="seconds"),
            "offset_s": self.offset_s,
            "time_semantics": item.time_semantics,
            "alignment_group": item.alignment_group,
            "alignment_quality": item.alignment_quality,
            "source": {
                "source_id": item.source_id,
                "source_path": item.source_path,
                "source_locator": item.source_locator,
                "source_sha256": item.source_sha256,
            },
            "usage": item.usage,
            "status": item.status,
            "catalog_exclusion_reason": item.exclusion_reason,
            "not_fitted_reason": self.not_fitted_reason,
            "uncertainty": {
                "value_si": item.uncertainty_si,
                "unit": item.uncertainty_unit,
                "basis": item.uncertainty_basis,
            },
            "model_comparison": self.comparison.as_dict(),
        }


def _unfitted_disclosures(
    result: M5PipelineResult,
    observations: tuple[Observation, ...],
) -> tuple[_ObservationDisclosure, ...]:
    fitted_ids = _fitted_observation_ids(result)
    catalog_ids = {item.id for item in observations}
    if not fitted_ids.issubset(catalog_ids):
        raise ValueError("fixed calibration target observations are absent from the catalog")
    return tuple(
        _ObservationDisclosure(
            observation=item,
            offset_s=result.observation_offsets_s[item.id],
            not_fitted_reason=_not_fitted_reason(item),
            comparison=_model_comparison(result, item),
        )
        for item in observations
        if item.id not in fitted_ids
    )


def _origin_contract(
    result: M5PipelineResult,
    *,
    observation_count: int,
) -> dict[str, object]:
    return {
        "artifact_origin": "mixed",
        "top_level_synthetic_flag": "not_applicable_to_mixed_evidence",
        "section_rule": (
            "Each evidence section declares its own origin; field observations must not "
            "be conflated with M2 latent priors or model predictions."
        ),
        "sections": {
            "field_observations": {
                "origin": "source_traced_field_observation_catalog",
                "synthetic": False,
                "record_count": observation_count,
            },
            "m2_latent_priors": {
                "origin": "effective_baseline_M2_model_prediction",
                "synthetic": True,
                "streams": ["offgas", "aqueous", "brine"],
            },
            "m2_predictions": {
                "origin": "M2_steady_model_prediction",
                "synthetic": True,
                "stages": ["effective_baseline", "calibrated"],
            },
            "reconciliation": {
                "origin": _MIXED_RECONCILIATION_ORIGIN,
                "synthetic": None,
            },
        },
        "claim_scope": result.versions["claim_scope"],
    }


def _artifact_reconciliation_evidence(result: M5PipelineResult) -> dict[str, object]:
    """Normalize the low-level result's legacy field-only label for mixed M5 evidence."""

    evidence = result.reconciliation.as_dict()
    legacy_origin = evidence.pop("data_origin", None)
    legacy_synthetic = evidence.pop("synthetic", None)
    if (
        legacy_origin != "M5_reconciled_field_observations"
        or legacy_synthetic is not False
    ):
        raise ValueError("low-level reconciliation origin contract changed unexpectedly")
    evidence.update(
        {
            "data_origin": _MIXED_RECONCILIATION_ORIGIN,
            "synthetic": None,
            "legacy_entry_origin": {
                "data_origin": legacy_origin,
                "synthetic": legacy_synthetic,
                "scope": (
                    "Preserved for low-level lineage only; superseded for this artifact "
                    "because offgas, aqueous and brine are M2 latent priors."
                ),
            },
            "result_fingerprint_scope": (
                "Original low-level ReconciliationResult before artifact origin "
                "normalization."
            ),
        }
    )
    return evidence


def reconciled_case_payload(
    result: M5PipelineResult,
    *,
    source_repo_root: Path | None = None,
) -> dict[str, object]:
    """Build the versioned gold reconciliation artifact with section-level origins."""

    observations = _load_result_observations(result, source_repo_root)
    reconciliation_evidence = _artifact_reconciliation_evidence(result)
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M5_reconciled_case",
        "artifact_version": result.alignment.derived_case_version,
        "claim_scope": "case_alignment_only",
        "origin_contract": _origin_contract(
            result,
            observation_count=len(observations),
        ),
        "evidence_sections": {
            "field_observations": {
                "origin": "source_traced_field_observation_catalog",
                "synthetic": False,
                "observations": [item.as_dict() for item in observations],
            },
            "m2_latent_priors": {
                "origin": "effective_baseline_M2_model_prediction",
                "synthetic": True,
                "product_flows_kg_s": {
                    name: result.prior_m2.product_flows_kg_s[name]
                    for name in ("offgas", "aqueous", "brine")
                },
                "evidence": result.prior_m2.as_dict(),
            },
            "m2_predictions": {
                "origin": "M2_steady_model_prediction",
                "synthetic": True,
                "effective_baseline": result.prior_m2.as_dict(),
                "calibrated": result.final_m2.as_dict(),
            },
            "reconciliation": {
                "origin": _MIXED_RECONCILIATION_ORIGIN,
                "synthetic": None,
                "evidence": reconciliation_evidence,
            },
        },
        "versions": dict(result.versions),
        "source_evidence": {
            "paths": result.alignment.paths.as_dict(),
            "file_fingerprints": {
                key: value
                for key, value in result.fingerprints.items()
                if key.endswith("_file")
            },
            "object_fingerprints": {
                "alignment": result.fingerprints["alignment_object"],
                "observations": result.fingerprints["observation_objects"],
                "sources": result.fingerprints["source_objects"],
                "baseline_model": result.fingerprints["baseline_model_object"],
                "baseline_case": result.fingerprints["baseline_case_object"],
                "effective_model": result.fingerprints["effective_model_object"],
                "effective_case": result.fingerprints["effective_case_object"],
            },
        },
        "case_reference_time": result.alignment.case_reference_time.isoformat(
            timespec="seconds"
        ),
        "observation_offsets_s": dict(result.observation_offsets_s),
        "selected_observations": {
            key: dict(value) for key, value in result.selected_observations.items()
        },
        "review_only_observation_ids": list(result.review_only_observation_ids),
        "case_overlays": result.overlays.as_dict(),
        "apparent_hydrocarbon_residual_fraction": (
            result.apparent_hydrocarbon_residual_fraction
        ),
        "apparent_hydrocarbon_residual_basis": (
            "same-screen fresh feed minus five oil products; excludes wash water, "
            "offgas, aqueous and brine"
        ),
        "latent_prior_m2": result.prior_m2.as_dict(),
        "reconciliation": reconciliation_evidence,
        "calibration_targets_kg_s": dict(result.calibration_targets_kg_s),
    }
    return _artifact_fingerprint(payload)


def calibrated_parameter_payload(result: M5PipelineResult) -> dict[str, object]:
    """Build a two-overlay parameter artifact without rewriting the base model."""

    parameters = [
        {
            **item.as_dict(),
            "distance_to_lower_bound_k": item.calibrated_k - item.lower_bound_k,
            "distance_to_upper_bound_k": item.upper_bound_k - item.calibrated_k,
        }
        for item in result.calibration.parameters
    ]
    file_fingerprints = {
        key: value for key, value in result.fingerprints.items() if key.endswith("_file")
    }
    object_fingerprints = {
        key: value for key, value in result.fingerprints.items() if not key.endswith("_file")
    }
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M5_calibrated_parameter_set",
        "parameter_set_version": result.calibration.versions[
            "calibrated_parameter_set_version"
        ],
        "claim_scope": "case_alignment_only",
        "versions": dict(result.versions),
        "calibration_versions": dict(result.calibration.versions),
        "base_model_version": result.versions["model_version"],
        "base_model_config_version": result.versions["model_config_version"],
        "base_parameter_set_version": result.versions["base_parameter_set_version"],
        "base_case_version": result.versions["base_case_version"],
        "derived_case_version": result.versions["derived_case_version"],
        "alignment_version": result.versions["alignment_version"],
        "alignment_config_sha256": result.fingerprints["alignment_file"],
        "alignment_config_fingerprint": result.fingerprints["alignment_object"],
        "calibration_config_sha256": result.fingerprints["calibration_config_file"],
        "calibration_input_fingerprint": result.calibration.fingerprints["input_bundle"],
        "input_file_fingerprints": file_fingerprints,
        "input_object_fingerprints": object_fingerprints,
        "calibration_fingerprints": dict(result.calibration.fingerprints),
        "pipeline_result_fingerprint": result.result_fingerprint,
        "reconciliation_result_fingerprint": result.reconciliation.result_fingerprint,
        "calibration_result_fingerprint": result.calibration.result_fingerprint,
        "effective_model_fingerprint": result.fingerprints["effective_model_object"],
        "calibrated_model_fingerprint": result.fingerprints["calibrated_model_object"],
        "parameter_overlays": parameters,
        "boundary_hits": list(result.calibration.boundary_hits),
        "frozen_parameter_statement": (
            "All model fields except column.cut_points_k[2] and [3] remain fixed; "
            "case flash temperature and wash ratio are operating overlays, not calibrated "
            "parameters."
        ),
    }
    return _artifact_fingerprint(payload)


def calibration_report_payload(
    result: M5PipelineResult,
    *,
    reconciled_case_sha256: str,
    parameter_set_sha256: str,
    source_repo_root: Path | None = None,
) -> dict[str, object]:
    """Build the complete machine-readable M5 review report."""

    observations = _load_result_observations(result, source_repo_root)
    disclosures = _unfitted_disclosures(result, observations)
    initial = result.calibration.initial
    calibrated = result.calibration.calibrated
    improvement_fraction = (
        initial.total_objective - calibrated.total_objective
    ) / initial.total_objective
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M5_calibration_review",
        "report_version": result.alignment.calibration_version,
        "claim_scope": "case_alignment_only",
        "status": "success",
        "origin_contract": _origin_contract(
            result,
            observation_count=len(observations),
        ),
        "evidence_sections": {
            "field_observations": {
                "origin": "source_traced_field_observation_catalog",
                "synthetic": False,
                "catalog_fingerprint": result.fingerprints["observation_objects"],
                "record_count": len(observations),
            },
            "m2_latent_priors": {
                "origin": "effective_baseline_M2_model_prediction",
                "synthetic": True,
                "streams": ["offgas", "aqueous", "brine"],
            },
            "m2_predictions": {
                "origin": "M2_steady_model_prediction",
                "synthetic": True,
                "effective_baseline": result.prior_m2.as_dict(),
                "calibrated": result.final_m2.as_dict(),
            },
        },
        "versions": dict(result.versions),
        "artifact_links": {
            "reconciled_case": result.alignment.artifacts.reconciled_case,
            "reconciled_case_sha256": reconciled_case_sha256,
            "calibrated_parameters": result.alignment.artifacts.calibrated_parameters,
            "calibrated_parameters_sha256": parameter_set_sha256,
            "artifact_manifest": result.alignment.artifacts.artifact_manifest,
        },
        "completion_checks": {
            "source_identity_verified": True,
            "raw_sources_read_only": True,
            "all_unfitted_observations_disclosed": (
                len(disclosures) == len(observations) - len(_fitted_observation_ids(result))
            ),
            "reconciliation_closed": abs(
                result.reconciliation.post_reconciliation_residual_kg_s
            )
            <= 1e-10,
            "same_screen_apparent_residual_within_3_percent": (
                result.apparent_hydrocarbon_residual_fraction <= 0.03
            ),
            "m2_prior_conserving": result.prior_m2.unit_balances_passed,
            "objective_strictly_improved": (
                calibrated.total_objective < initial.total_objective
            ),
            "initial_sensitivity_full_rank": (
                result.calibration.initial_sensitivity.numerical_rank == 2
            ),
            "final_sensitivity_full_rank": (
                result.calibration.calibrated_sensitivity.numerical_rank == 2
            ),
            "no_parameter_bound_hit": not result.calibration.boundary_hits,
            "final_m2_conserving": result.final_m2.unit_balances_passed,
        },
        "objective": {
            "initial": initial.as_dict(),
            "calibrated": calibrated.as_dict(),
            "improvement_fraction": improvement_fraction,
        },
        "parameters": [item.as_dict() for item in result.calibration.parameters],
        "initial_sensitivity": result.calibration.initial_sensitivity.as_dict(),
        "calibrated_sensitivity": result.calibration.calibrated_sensitivity.as_dict(),
        "case_alignment": reconciled_case_payload(
            result,
            source_repo_root=source_repo_root,
        ),
        "final_m2": result.final_m2.as_dict(),
        "fitted_calibration_observation_ids": sorted(_fitted_observation_ids(result)),
        "unfitted_observation_ids": [item.observation.id for item in disclosures],
        "unfitted_observations": [item.as_dict() for item in disclosures],
        "limitations": [
            "One weakly time-aligned case only; no independent validation case.",
            "Six pseudo-components and all non-whitelisted equipment/dynamic/control parameters remain low-confidence engineering values.",
            "DCS screenshot display resolution is not treated as instrument one-sigma uncertainty.",
            "Offgas, aqueous and brine are broad M2 priors, not field measurements.",
            "The result is case alignment only, not field-validated prediction or deployable control advice.",
        ],
        "pipeline_result_fingerprint": result.result_fingerprint,
    }
    return _artifact_fingerprint(payload)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _comparison_markdown(comparison: _ModelComparison, *, initial: bool) -> str:
    prediction = (
        comparison.initial_prediction_si if initial else comparison.calibrated_prediction_si
    )
    bias = comparison.initial_bias_si if initial else comparison.calibrated_bias_si
    if comparison.status == "comparable":
        if prediction is None or bias is None:
            raise ValueError("comparable model disclosure omitted prediction or bias")
        return f"{prediction:.9g} / {bias:+.9g} {comparison.si_unit}"
    return "no_compatible_model_output: " + comparison.reason


def calibration_report_markdown(
    result: M5PipelineResult,
    *,
    reconciled_case_sha256: str,
    parameter_set_sha256: str,
    source_repo_root: Path | None = None,
) -> str:
    """Render the human review report from the exact result and observation catalog."""

    observations = _load_result_observations(result, source_repo_root)
    disclosures = _unfitted_disclosures(result, observations)
    initial = result.calibration.initial
    final = result.calibration.calibrated
    improvement = (
        100.0 * (initial.total_objective - final.total_objective) / initial.total_objective
    )
    lines = [
        "# M5 case_20260604 数据协调与基准校正报告",
        "",
        "_结论范围：`case_alignment_only`。本报告不构成现场验证、动态验证或可投用控制建议。_",
        "",
        "## 证据来源分层",
        "",
        "- `field_observations`：来自可追溯现场截图/化验目录，`synthetic=false`；只对这些记录作现场观测声明。",
        "- `m2_latent_priors`：offgas、aqueous、brine 来自有效基线 M2 预测，`synthetic=true`，不是现场测量。",
        "- `m2_predictions`：初始与校正后产品流均为 M2 稳态模型预测，`synthetic=true`。",
        "- `reconciliation`：由现场边界观测与 M2 潜变量先验混合形成，不使用单一顶层 synthetic 标志。",
        "",
        "## 结论",
        "",
        f"- 同屏五油品相对原油的原始表观偏差为 `{100.0 * result.apparent_hydrocarbon_residual_fraction:.4f}%`，低于 `3%` 门槛；该口径不包含洗水、不凝气和水相。",
        f"- 完整十流边界协调后残差为 `{result.reconciliation.post_reconciliation_residual_kg_s:.6g} kg/s`。",
        f"- 总加权目标由 `{initial.total_objective:.6f}` 降至 `{final.total_objective:.6f}`，改善 `{improvement:.3f}%`。",
        f"- 初始/最终灵敏度秩均为 `2`，条件数分别为 `{result.calibration.initial_sensitivity.condition_number:.6f}` 与 `{result.calibration.calibrated_sensitivity.condition_number:.6f}`。",
        f"- 参数边界命中：`{list(result.calibration.boundary_hits)}`；最终 M2 总流程与逐设备守恒通过。",
        "",
        "## 案例操作覆盖",
        "",
        "| 项目 | 基线 | M5有效值 | 来源 |",
        "| --- | ---: | ---: | --- |",
        f"| 闪蒸温度 | {result.overlays.flash_temperature_k['baseline']:.2f} K | {result.overlays.flash_temperature_k['effective']:.2f} K | TI-1012，弱时间对齐；案例输入而非独立预测 |",
        f"| 洗水比 | {result.overlays.wash_water_ratio['baseline']:.6f} | {result.overlays.wash_water_ratio['effective']:.9f} | 18.97/407.60 t/h，跨画面弱对齐；案例输入而非独立预测 |",
        "",
        "## 净边界协调",
        "",
        "| 物流 | 基础 | 原值/先验 kg/s | 协调值 kg/s | 调整 kg/s | pull |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name, entry in result.reconciliation.entries.items():
        basis = "实测" if entry.basis == "measurement" else "M2先验"
        lines.append(
            f"| {name} | {basis} | {entry.reference_kg_s:.9f} | "
            f"{entry.reconciled_kg_s:.9f} | {entry.adjustment_kg_s:.9f} | "
            f"{entry.pull:.6f} |"
        )
    lines.extend(
        [
            "",
            "回流和顶循仅保留作内部物流证据，未进入净边界；泵循环没有现场观测，不造假补值。截图末位只表示显示/抄录分辨率，协调权重来自独立版本化的保守工程尺度。",
            "",
            "## 校正参数",
            "",
            "| 参数 | 初值 K | 校正值 K | 下界 K | 上界 K |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for parameter in result.calibration.parameters:
        lines.append(
            f"| `{parameter.path}` | {parameter.initial_k:.6f} | "
            f"{parameter.calibrated_k:.6f} | {parameter.lower_bound_k:.6f} | "
            f"{parameter.upper_bound_k:.6f} |"
        )
    lines.extend(
        [
            "",
            "仅上述两个切割温度进入优化。前两个切割点、分离宽度、回流影响、六伪组分、预热/炉/闪蒸/冷凝参数、质量代理、全部动态和控制参数均冻结。",
            "",
            "## 目标与灵敏度",
            "",
            "| 指标 | 初始 | 校正后 |",
            "| --- | ---: | ---: |",
            f"| 数据失配 | {initial.data_misfit:.6f} | {final.data_misfit:.6f} |",
            f"| 正则惩罚 | {initial.regularization_penalty:.6f} | {final.regularization_penalty:.6f} |",
            f"| 总目标 | {initial.total_objective:.6f} | {final.total_objective:.6f} |",
            f"| 灵敏度条件数 | {result.calibration.initial_sensitivity.condition_number:.6f} | {result.calibration.calibrated_sensitivity.condition_number:.6f} |",
            f"| 灵敏度列余弦 | {result.calibration.initial_sensitivity.column_cosine:.6f} | {result.calibration.calibrated_sensitivity.column_cosine:.6f} |",
            "",
            "有量纲和归一化 3×2 灵敏度矩阵、奇异值、秩、逐目标误差及完整守恒证据见同名 JSON 报告。",
            "",
            "## 未进入固定校正目标的观测（完整披露）",
            "",
            f"目录共 `{len(observations)}` 条；仅轻柴油、重柴油、渣油 3 条进入固定校正目标，以下 `{len(disclosures)}` 条全部披露。偏差定义为模型预测减现场观测。",
            "",
            "| ID | SI值 | 时间/offset | 来源 | 用途/状态 | 未拟合原因 | 初始M2/偏差 | 校正M2/偏差 |",
            "| --- | ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in disclosures:
        observation = item.observation
        source = (
            f"{observation.source_path} :: {observation.source_locator} :: "
            f"sha256={observation.source_sha256}"
        )
        timestamp = (
            f"{observation.observed_at.isoformat(timespec='seconds')} "
            f"({item.offset_s:+.0f} s)"
        )
        usage = (
            f"{observation.usage}/{observation.status}; "
            f"catalog_exclusion={observation.exclusion_reason or 'none'}"
        )
        lines.append(
            f"| `{observation.id}` | {observation.si_value:.12g} "
            f"{_markdown_cell(observation.si_unit)} | {_markdown_cell(timestamp)} | "
            f"{_markdown_cell(source)} | {_markdown_cell(usage)} | "
            f"{_markdown_cell(item.not_fitted_reason)} | "
            f"{_markdown_cell(_comparison_markdown(item.comparison, initial=True))} | "
            f"{_markdown_cell(_comparison_markdown(item.comparison, initial=False))} |"
        )
    lines.extend(
        [
            "",
            "## 追溯与限制",
            "",
            f"- 协调案例 SHA-256：`{reconciled_case_sha256}`",
            f"- 参数集 SHA-256：`{parameter_set_sha256}`",
            f"- pipeline 结果指纹：`{result.result_fingerprint}`",
            "- 当前只有一套弱时间对齐案例；没有独立验证案例、连续 DCS 或动态参数辨识证据。",
            "- 缺测的 offgas、aqueous、brine 是宽松模型先验，不是现场测量。",
            "- 结果不能外推为现场精度、跨原油能力、在线优化或控制指令。",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_output(
    repo_root: Path,
    relative_path: str,
    *,
    artifact_name: str,
) -> Path:
    try:
        allowed_relative, suffix = _OUTPUT_ROOTS[artifact_name]
    except KeyError as exc:  # pragma: no cover - internal fixed map
        raise ValueError(f"unknown artifact class: {artifact_name}") from exc
    if "\\" in relative_path:
        raise ValueError("artifact paths must use repository-relative POSIX separators")
    parsed = PurePosixPath(relative_path)
    allowed = PurePosixPath(allowed_relative)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or "." in parsed.parts
        or ".." in parsed.parts
        or parsed.suffix.lower() != suffix
        or parsed.parts[: len(allowed.parts)] != allowed.parts
    ):
        raise ValueError(
            f"{artifact_name} must be a {suffix} file under {allowed_relative}"
        )
    root = repo_root.resolve()
    allowed_root = (root / allowed).resolve()
    output = (root / parsed).resolve()
    try:
        output.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(
            f"{artifact_name} path escapes its allowed output root: {relative_path}"
        ) from exc
    return output


def _validated_fingerprinted_json(path: Path, expected_type: str) -> dict[str, object]:
    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise ValueError(f"staged {expected_type} artifact must be a JSON object")
    payload = cast(dict[str, object], decoded)
    if payload.get("artifact_type") != expected_type:
        raise ValueError(f"staged artifact type differs from {expected_type}")
    fingerprint = payload.get("artifact_fingerprint")
    if not isinstance(fingerprint, str):
        raise TypeError(f"staged {expected_type} omitted artifact_fingerprint")
    unsigned = dict(payload)
    del unsigned["artifact_fingerprint"]
    if canonical_fingerprint(unsigned) != fingerprint:
        raise ValueError(f"staged {expected_type} artifact fingerprint mismatch")
    return payload


def _json_object_field(
    parent: Mapping[str, object],
    name: str,
    *,
    context: str,
) -> dict[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{context}.{name} must be a JSON object")
    return cast(dict[str, object], value)


def _validate_mixed_reconciliation_origin(gold: Mapping[str, object]) -> None:
    origin_contract = _json_object_field(gold, "origin_contract", context="gold")
    contract_sections = _json_object_field(
        origin_contract,
        "sections",
        context="gold.origin_contract",
    )
    contract_reconciliation = _json_object_field(
        contract_sections,
        "reconciliation",
        context="gold.origin_contract.sections",
    )
    evidence_sections = _json_object_field(gold, "evidence_sections", context="gold")
    reconciliation_section = _json_object_field(
        evidence_sections,
        "reconciliation",
        context="gold.evidence_sections",
    )
    nested_evidence = _json_object_field(
        reconciliation_section,
        "evidence",
        context="gold.evidence_sections.reconciliation",
    )
    top_level_evidence = _json_object_field(gold, "reconciliation", context="gold")
    for context, value, origin_key in (
        (
            "gold.origin_contract.sections.reconciliation",
            contract_reconciliation,
            "origin",
        ),
        ("gold.evidence_sections.reconciliation", reconciliation_section, "origin"),
        (
            "gold.evidence_sections.reconciliation.evidence",
            nested_evidence,
            "data_origin",
        ),
        ("gold.reconciliation", top_level_evidence, "data_origin"),
    ):
        if value.get(origin_key) != _MIXED_RECONCILIATION_ORIGIN:
            raise ValueError(f"{context} must declare mixed reconciliation origin")
        if "synthetic" not in value or value["synthetic"] is not None:
            raise ValueError(f"{context}.synthetic must be null for mixed evidence")
    for context, value in (
        ("gold.evidence_sections.reconciliation.evidence", nested_evidence),
        ("gold.reconciliation", top_level_evidence),
    ):
        legacy = _json_object_field(value, "legacy_entry_origin", context=context)
        if (
            legacy.get("data_origin") != "M5_reconciled_field_observations"
            or legacy.get("synthetic") is not False
            or "superseded" not in str(legacy.get("scope", ""))
        ):
            raise ValueError(f"{context} must scope the legacy field-only origin")


def _validate_staged_bundle(
    staged: Mapping[str, Path],
    *,
    disclosure_ids: tuple[str, ...],
) -> None:
    gold = _validated_fingerprinted_json(staged["reconciled_case"], "M5_reconciled_case")
    parameters = _validated_fingerprinted_json(
        staged["calibrated_parameters"],
        "M5_calibrated_parameter_set",
    )
    report = _validated_fingerprinted_json(
        staged["report_json"],
        "M5_calibration_review",
    )
    manifest = _validated_fingerprinted_json(
        staged["artifact_manifest"],
        "M5_artifact_suite_manifest",
    )
    origin_contract = gold.get("origin_contract")
    if (
        "synthetic" in gold
        or not isinstance(origin_contract, dict)
        or origin_contract.get("artifact_origin") != "mixed"
    ):
        raise ValueError("gold artifact must use section-level mixed-origin semantics")
    _validate_mixed_reconciliation_origin(gold)
    report_case_alignment = _json_object_field(
        report,
        "case_alignment",
        context="report",
    )
    _validate_mixed_reconciliation_origin(report_case_alignment)
    if parameters.get("versions") is None or parameters.get("input_file_fingerprints") is None:
        raise ValueError("parameter artifact omitted versions or input fingerprints")
    reported_ids = report.get("unfitted_observation_ids")
    if reported_ids != list(disclosure_ids):
        raise ValueError("JSON report does not disclose the complete unfitted observation set")
    disclosures = report.get("unfitted_observations")
    if not isinstance(disclosures, list) or len(disclosures) != len(disclosure_ids):
        raise ValueError("JSON report disclosure records are incomplete")
    markdown = staged["report_markdown"].read_text(encoding="utf-8")
    for observation_id in disclosure_ids:
        if markdown.count(f"`{observation_id}`") != 1:
            raise ValueError(
                f"Markdown report must disclose observation {observation_id} exactly once"
            )
    manifest_artifacts = manifest.get("artifacts")
    if not isinstance(manifest_artifacts, dict):
        raise TypeError("artifact manifest omitted artifacts")
    for name in _PUBLISH_ORDER[:-1]:
        item = manifest_artifacts.get(name)
        if not isinstance(item, dict) or item.get("sha256") != file_sha256(staged[name]):
            raise ValueError(f"artifact manifest hash mismatch for {name}")


def _artifact_manifest_payload(
    result: M5PipelineResult,
    *,
    paths: Mapping[str, str],
    sha256: Mapping[str, str],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M5_artifact_suite_manifest",
        "manifest_version": result.alignment.calibration_version,
        "manifest_path": paths["artifact_manifest"],
        "claim_scope": "case_alignment_only",
        "status": "valid",
        "artifacts": {
            name: {"path": paths[name], "sha256": sha256[name]}
            for name in _PUBLISH_ORDER[:-1]
        },
        "versions": dict(result.versions),
        "pipeline_result_fingerprint": result.result_fingerprint,
        "result_fingerprints": {
            "reconciliation_result": result.fingerprints["reconciliation_result"],
            "calibration_result": result.fingerprints["calibration_result"],
            "calibrated_model_object": result.fingerprints["calibrated_model_object"],
        },
        "config_fingerprints": {
            "alignment_file": result.fingerprints["alignment_file"],
            "alignment_object": result.fingerprints["alignment_object"],
            "calibration_config_file": result.fingerprints["calibration_config_file"],
            "model_file": result.fingerprints["model_file"],
            "case_file": result.fingerprints["case_file"],
            "component_catalog_file": result.fingerprints["component_catalog_file"],
            "observation_catalog_file": result.fingerprints["observation_catalog_file"],
            "source_manifest_file": result.fingerprints["source_manifest_file"],
        },
    }
    return _artifact_fingerprint(payload)


def _stage_bundle(
    targets: Mapping[str, Path],
    texts: Mapping[str, str],
    *,
    token: str,
) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    try:
        for name in _PUBLISH_ORDER:
            target = targets[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_name(f".{target.name}.{token}.stage")
            stage.write_text(texts[name], encoding="utf-8", newline="\n")
            staged[name] = stage
    except Exception:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    return staged


def _publish_staged_bundle(
    targets: Mapping[str, Path],
    staged: Mapping[str, Path],
    *,
    token: str,
) -> None:
    backups: dict[str, Path] = {}
    published: list[str] = []
    current: str | None = None
    try:
        for name in _PUBLISH_ORDER:
            current = name
            target = targets[name]
            if target.exists():
                backup = target.with_name(f".{target.name}.{token}.backup")
                target.replace(backup)
                backups[name] = backup
            staged[name].replace(target)
            published.append(name)
            current = None
    except Exception:
        if current is not None and current in backups:
            target = targets[current]
            if target.exists():
                target.unlink()
            backups[current].replace(target)
        for name in reversed(published):
            target = targets[name]
            target.unlink(missing_ok=True)
            prior_backup = backups.get(name)
            if prior_backup is not None:
                prior_backup.replace(target)
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        for path in backups.values():
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class M5ArtifactManifest:
    """Paths and SHA-256 hashes for the published M5 artifact suite."""

    paths: MappingProxyType[str, str]
    sha256: MappingProxyType[str, str]

    def as_dict(self) -> dict[str, object]:
        return {"paths": dict(self.paths), "sha256": dict(self.sha256)}


def write_m5_artifacts(
    result: M5PipelineResult,
    repo_root: Path,
    *,
    source_repo_root: Path | None = None,
) -> M5ArtifactManifest:
    """Validate, stage and transactionally publish the complete M5 artifact suite."""

    paths = result.alignment.artifacts
    relative_paths = paths.as_dict()
    targets = {
        name: _safe_output(repo_root, relative_paths[name], artifact_name=name)
        for name in _PUBLISH_ORDER
    }
    if len(set(targets.values())) != len(targets):
        raise ValueError("M5 artifact output paths must be distinct")
    resolved_source_root = source_repo_root
    if resolved_source_root is None:
        candidate_catalog = _safe_repo_input(
            repo_root,
            result.alignment.paths.observation_catalog,
        )
        if candidate_catalog.is_file():
            resolved_source_root = repo_root
    observations = _load_result_observations(result, resolved_source_root)
    disclosures = _unfitted_disclosures(result, observations)
    gold_text = _json_text(
        reconciled_case_payload(result, source_repo_root=resolved_source_root)
    )
    parameter_text = _json_text(calibrated_parameter_payload(result))
    artifact_sha = {
        "reconciled_case": _text_sha256(gold_text),
        "calibrated_parameters": _text_sha256(parameter_text),
    }
    report_json_text = _json_text(
        calibration_report_payload(
            result,
            reconciled_case_sha256=artifact_sha["reconciled_case"],
            parameter_set_sha256=artifact_sha["calibrated_parameters"],
            source_repo_root=resolved_source_root,
        )
    )
    report_markdown_text = calibration_report_markdown(
        result,
        reconciled_case_sha256=artifact_sha["reconciled_case"],
        parameter_set_sha256=artifact_sha["calibrated_parameters"],
        source_repo_root=resolved_source_root,
    )
    artifact_sha.update(
        {
            "report_json": _text_sha256(report_json_text),
            "report_markdown": _text_sha256(report_markdown_text),
        }
    )
    manifest_text = _json_text(
        _artifact_manifest_payload(
            result,
            paths=relative_paths,
            sha256=artifact_sha,
        )
    )
    artifact_sha["artifact_manifest"] = _text_sha256(manifest_text)
    texts = {
        "reconciled_case": gold_text,
        "calibrated_parameters": parameter_text,
        "report_json": report_json_text,
        "report_markdown": report_markdown_text,
        "artifact_manifest": manifest_text,
    }
    token = uuid.uuid4().hex
    staged = _stage_bundle(targets, texts, token=token)
    try:
        _validate_staged_bundle(
            staged,
            disclosure_ids=tuple(item.observation.id for item in disclosures),
        )
        _publish_staged_bundle(targets, staged, token=token)
    except Exception:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    return M5ArtifactManifest(
        paths=MappingProxyType(dict(relative_paths)),
        sha256=MappingProxyType(dict(artifact_sha)),
    )
