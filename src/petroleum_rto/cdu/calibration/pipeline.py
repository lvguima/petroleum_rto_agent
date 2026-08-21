"""End-to-end M5 observation, reconciliation and steady calibration pipeline."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import (
    CaseConfig,
    ConfigurationError,
    ModelConfig,
    canonical_fingerprint,
    load_case_config,
    load_component_catalog,
    load_model_config,
    validate_config_compatibility,
)
from ..core.types import BalanceReport
from ..flowsheet.recycle import RecycleSolveResult, solve_recycle
from ..properties.components import ComponentCatalog
from ..repository import cdu_resource_file_sha256, resolve_cdu_repository_path
from .alignment import AlignmentConfig, load_alignment_config
from .calibration import CalibrationResult, apply_calibration_parameters, run_calibration
from .config import (
    CALIBRATION_PARAMETER_DEFINITIONS,
    CALIBRATION_TARGETS,
    CalibrationConfig,
    load_calibration_config,
)
from .etl import file_sha256
from .observations import (
    OBSERVATION_CATALOG_VERSION,
    SOURCE_MANIFEST_VERSION,
    Observation,
    SourceManifestRecord,
    load_observation_catalog,
    load_source_manifest,
    validate_observation_sources,
)
from .reconciliation import (
    FlowEstimateInput,
    ReconciliationResult,
    reconcile_boundary_flows,
)

_ENERGY_TOLERANCE_W: Final[float] = 1e-5
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_SAME_SCREEN_RESIDUAL_FRACTION: Final[float] = (407.3 - 403.67) / 407.3
_M2_PRODUCT_NAMES: Final[tuple[str, ...]] = (
    "offgas",
    "gasoline",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
    "aqueous",
    "brine",
)
_MEASUREMENT_ROLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fresh_feed": "net_boundary_input",
        "wash_water": "auxiliary_input",
        "gasoline": "net_boundary_output",
        "kerosene": "net_boundary_output",
        "light_diesel": "net_boundary_output",
        "heavy_diesel": "net_boundary_output",
        "residue": "net_boundary_output",
    }
)
_INTERNAL_OBSERVATION_CONTRACT: Final[
    Mapping[str, tuple[str, str, str, str]]
] = MappingProxyType(
    {
        "reflux": (
            "obs-dcs-overhead-reflux-fi-1010",
            "internal_reflux",
            "diagnostic_reference",
            "reference_only",
        ),
        "top_circulation": (
            "obs-dcs-top-circulation-fi-1003",
            "internal_circulation",
            "do_not_use",
            "excluded",
        ),
    }
)
_PIPELINE_VERSION_KEYS: Final[tuple[str, ...]] = (
    "software_version",
    "simulation_stage",
    "claim_scope",
    "alignment_version",
    "reconciliation_config_version",
    "calibration_version",
    "model_version",
    "model_config_version",
    "base_parameter_set_version",
    "calibrated_parameter_set_version",
    "base_case_version",
    "derived_case_version",
    "observation_catalog_version",
    "source_manifest_version",
)
_PIPELINE_FINGERPRINT_KEYS: Final[tuple[str, ...]] = (
    "alignment_file",
    "model_file",
    "case_file",
    "component_catalog_file",
    "observation_catalog_file",
    "source_manifest_file",
    "calibration_config_file",
    "alignment_object",
    "observation_objects",
    "source_objects",
    "baseline_model_object",
    "baseline_case_object",
    "effective_model_object",
    "effective_case_object",
    "reconciliation_result",
    "calibration_result",
    "calibrated_model_object",
)


class M5PipelineError(RuntimeError):
    """Stage-labelled M5 failure that never carries a valid parameter set."""

    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(f"M5 failed at {stage}: {reason}")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "failed",
            "simulation_stage": "M5",
            "failure_stage": self.stage,
            "failure_reason": self.reason,
            "calibrated_parameter_set": None,
        }


def _repo_path(repo_root: Path, relative_path: str) -> Path:
    try:
        return resolve_cdu_repository_path(repo_root, relative_path)
    except (TypeError, ValueError) as exc:  # pragma: no cover - config blocks traversal
        raise ConfigurationError(str(exc)) from exc


def _float_parameter(values: Mapping[str, object], name: str) -> float:
    raw = values[name]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise M5PipelineError("configuration", f"model value {name} is not numeric")
    result = float(raw)
    if not math.isfinite(result):
        raise M5PipelineError("configuration", f"model value {name} is not finite")
    return result


def _balance_tolerances(model: ModelConfig) -> dict[str, float]:
    return {
        "mass_atol_kg_s": _float_parameter(model.solver, "mass_tolerance_kg_s"),
        "component_atol_kg_s": _float_parameter(
            model.solver, "component_tolerance_kg_s"
        ),
        "salt_atol_kg_s": _float_parameter(model.solver, "salt_tolerance_kg_s"),
        "energy_atol_w": _ENERGY_TOLERANCE_W,
    }


def _maximum_component_residual(balance: BalanceReport) -> float:
    return max((abs(value) for value in balance.component_residuals_kg_s.values()), default=0.0)


@dataclass(frozen=True)
class M2Evidence:
    """Compact conserving M2 evidence used for priors or the final model."""

    input_fingerprint: str
    iterations: int
    final_recycle_residual: float
    product_flows_kg_s: Mapping[str, float]
    balance: Mapping[str, object]
    maximum_component_residual_kg_s: float
    unit_balances_passed: bool
    tolerances: Mapping[str, float]

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.input_fingerprint):
            raise ValueError("M2 evidence requires a SHA-256 input fingerprint")
        if self.iterations < 1:
            raise ValueError("M2 evidence requires positive iterations")
        if not math.isfinite(self.final_recycle_residual) or self.final_recycle_residual < 0.0:
            raise ValueError("M2 final recycle residual must be finite and non-negative")
        flows = dict(self.product_flows_kg_s)
        if tuple(flows) != _M2_PRODUCT_NAMES or any(
            not math.isfinite(value) or value < 0.0 for value in flows.values()
        ):
            raise ValueError("M2 evidence must contain the fixed finite product-flow order")
        if self.unit_balances_passed is not True:
            raise ValueError("successful M2 evidence requires every unit balance to pass")
        object.__setattr__(self, "product_flows_kg_s", MappingProxyType(flows))
        object.__setattr__(self, "balance", MappingProxyType(dict(self.balance)))
        object.__setattr__(self, "tolerances", MappingProxyType(dict(self.tolerances)))

    def as_dict(self) -> dict[str, object]:
        return {
            "synthetic": True,
            "data_origin": "M2_steady_model_prediction",
            "input_fingerprint": self.input_fingerprint,
            "iterations": self.iterations,
            "final_recycle_residual": self.final_recycle_residual,
            "product_flows_kg_s": dict(self.product_flows_kg_s),
            "balance": dict(self.balance),
            "maximum_component_residual_kg_s": self.maximum_component_residual_kg_s,
            "unit_balances_passed": self.unit_balances_passed,
            "tolerances": dict(self.tolerances),
        }


def _m2_evidence(
    result: RecycleSolveResult,
    model: ModelConfig,
    *,
    stage: str,
) -> M2Evidence:
    if not result.converged or result.flowsheet is None:
        reason = result.failure_reason or "M2 recycle did not converge"
        raise M5PipelineError(stage, reason)
    flowsheet = result.require_converged()
    tolerances = _balance_tolerances(model)
    if not flowsheet.balance.passed(**tolerances):
        raise M5PipelineError(stage, "M2 net boundary conservation failed")
    failed_units = tuple(
        name
        for name, unit in flowsheet.unit_results.items()
        if unit.balance is None or not unit.balance.passed(**tolerances)
    )
    if failed_units:
        raise M5PipelineError(
            stage,
            "M2 unit conservation failed: " + ", ".join(failed_units),
        )
    if result.final_residual is None:
        raise M5PipelineError(stage, "M2 success omitted final recycle residual")
    return M2Evidence(
        input_fingerprint=flowsheet.input_fingerprint,
        iterations=result.iterations,
        final_recycle_residual=result.final_residual,
        product_flows_kg_s={
            name: flowsheet.products[name].mass_flow_kg_s for name in _M2_PRODUCT_NAMES
        },
        balance=flowsheet.balance.as_dict(),
        maximum_component_residual_kg_s=_maximum_component_residual(flowsheet.balance),
        unit_balances_passed=True,
        tolerances=tolerances,
    )


@dataclass(frozen=True)
class CaseOverlayEvidence:
    """The two auditable operating-input changes applied only in memory."""

    flash_temperature_k: Mapping[str, object]
    wash_water_ratio: Mapping[str, object]

    def __post_init__(self) -> None:
        flash = dict(self.flash_temperature_k)
        wash = dict(self.wash_water_ratio)
        expected_flash_fields = {
            "baseline",
            "effective",
            "unit",
            "observation_id",
            "instrument_tag",
            "offset_s",
            "effective_uncertainty_k",
            "uncertainty_basis",
            "revision_reason",
        }
        expected_wash_fields = {
            "baseline",
            "effective",
            "unit",
            "numerator_observation_id",
            "denominator_observation_id",
            "numerator_kg_s",
            "denominator_kg_s",
            "formula",
            "numerator_offset_s",
            "denominator_offset_s",
            "numerator_uncertainty_kg_s",
            "denominator_uncertainty_kg_s",
            "derived_uncertainty_kg_per_kg",
            "propagation_formula",
            "uncertainty_basis",
            "revision_reason",
        }
        if set(flash) != expected_flash_fields:
            raise ValueError("flash overlay evidence fields differ from the fixed contract")
        if set(wash) != expected_wash_fields:
            raise ValueError("wash overlay evidence fields differ from the fixed contract")

        def number(values: Mapping[str, object], name: str, *, positive: bool = False) -> float:
            raw = values[name]
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(raw)
                or (positive and raw <= 0.0)
            ):
                raise ValueError(f"overlay {name} must be finite")
            return float(raw)

        def text(values: Mapping[str, object], name: str) -> str:
            raw = values[name]
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"overlay {name} must be a non-empty string")
            return raw

        if text(flash, "unit") != "K" or text(flash, "instrument_tag") != "TI-1012":
            raise ValueError("flash overlay must retain TI-1012 in kelvin")
        for name in ("observation_id", "uncertainty_basis", "revision_reason"):
            text(flash, name)
        number(flash, "baseline", positive=True)
        number(flash, "effective", positive=True)
        number(flash, "offset_s")
        number(flash, "effective_uncertainty_k", positive=True)

        if text(wash, "unit") != "kg/kg fresh feed":
            raise ValueError("wash overlay must be a mass ratio")
        for name in (
            "numerator_observation_id",
            "denominator_observation_id",
            "formula",
            "uncertainty_basis",
            "revision_reason",
        ):
            text(wash, name)
        expected_propagation = (
            "r*sqrt((u_numerator/numerator)^2+(u_denominator/denominator)^2)"
        )
        if text(wash, "propagation_formula") != expected_propagation:
            raise ValueError("wash overlay uncertainty propagation formula differs")
        numerator = number(wash, "numerator_kg_s", positive=True)
        denominator = number(wash, "denominator_kg_s", positive=True)
        effective = number(wash, "effective", positive=True)
        numerator_uncertainty = number(
            wash, "numerator_uncertainty_kg_s", positive=True
        )
        denominator_uncertainty = number(
            wash, "denominator_uncertainty_kg_s", positive=True
        )
        derived_uncertainty = number(
            wash, "derived_uncertainty_kg_per_kg", positive=True
        )
        number(wash, "baseline", positive=True)
        number(wash, "numerator_offset_s")
        number(wash, "denominator_offset_s")
        if not math.isclose(effective, numerator / denominator, rel_tol=1e-12):
            raise ValueError("wash overlay ratio does not match its numerator/denominator")
        expected_uncertainty = effective * math.sqrt(
            (numerator_uncertainty / numerator) ** 2
            + (denominator_uncertainty / denominator) ** 2
        )
        if not math.isclose(
            derived_uncertainty,
            expected_uncertainty,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("wash overlay derived uncertainty does not match propagation")
        object.__setattr__(
            self,
            "flash_temperature_k",
            MappingProxyType(flash),
        )
        object.__setattr__(
            self,
            "wash_water_ratio",
            MappingProxyType(wash),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "flash_temperature_k": dict(self.flash_temperature_k),
            "wash_water_ratio": dict(self.wash_water_ratio),
        }


@dataclass(frozen=True)
class M5PipelineResult:
    """Successful, source-closed M5 case-alignment result."""

    alignment: AlignmentConfig
    overlays: CaseOverlayEvidence
    observation_catalog_evidence: Mapping[str, Mapping[str, object]]
    selected_observations: Mapping[str, Mapping[str, object]]
    observation_offsets_s: Mapping[str, float]
    review_only_observation_ids: tuple[str, ...]
    apparent_hydrocarbon_residual_fraction: float
    prior_m2: M2Evidence
    reconciliation: ReconciliationResult
    calibration_targets_kg_s: Mapping[str, float]
    calibration: CalibrationResult
    final_m2: M2Evidence
    calibrated_model: ModelConfig
    versions: Mapping[str, str]
    fingerprints: Mapping[str, str]

    def __post_init__(self) -> None:
        catalog: dict[str, Mapping[str, object]] = {}
        for observation_id, payload in self.observation_catalog_evidence.items():
            if not isinstance(observation_id, str) or not isinstance(payload, Mapping):
                raise TypeError("observation catalog evidence must map ids to objects")
            copied = dict(payload)
            if copied.get("id") != observation_id:
                raise ValueError("observation catalog evidence id differs from its key")
            if copied.get("catalog_version") != self.alignment.observation_catalog_version:
                raise ValueError("observation catalog evidence version drift")
            _observation_payload_time(copied, observation_id)
            catalog[observation_id] = MappingProxyType(copied)
        if not catalog:
            raise ValueError("pipeline result requires complete observation catalog evidence")
        catalog_ids = tuple(catalog)

        selected: dict[str, Mapping[str, object]] = {}
        for observation_id, payload in self.selected_observations.items():
            if not isinstance(observation_id, str) or not isinstance(payload, Mapping):
                raise TypeError("selected observations must map ids to objects")
            selected[observation_id] = MappingProxyType(dict(payload))
        expected_selected_ids = _expected_selected_observation_ids(self.alignment)
        if tuple(selected) != expected_selected_ids:
            raise ValueError("selected observations differ from the alignment mapping")
        for observation_id in expected_selected_ids:
            if observation_id not in catalog or selected[observation_id] != catalog[observation_id]:
                raise ValueError("selected observation differs from catalog evidence")

        review_only = tuple(self.review_only_observation_ids)
        expected_review_only = tuple(
            observation_id
            for observation_id in catalog_ids
            if observation_id not in selected
        )
        if review_only != expected_review_only:
            raise ValueError("review-only observations must be the complete catalog complement")
        if set(selected) & set(review_only) or set(selected) | set(review_only) != set(catalog):
            raise ValueError("selected and review-only observations do not partition the catalog")

        offsets = dict(self.observation_offsets_s)
        if tuple(offsets) != catalog_ids:
            raise ValueError("observation offsets must cover the complete catalog in order")
        for observation_id, payload in catalog.items():
            observed_at = _observation_payload_time(payload, observation_id)
            expected_offset = (
                observed_at - self.alignment.case_reference_time
            ).total_seconds()
            offset = offsets[observation_id]
            if (
                isinstance(offset, bool)
                or not isinstance(offset, (int, float))
                or not math.isfinite(offset)
                or float(offset) != expected_offset
            ):
                raise ValueError("observation offset does not match observed_at")
            if (
                observation_id in selected
                and abs(expected_offset) > self.alignment.maximum_alignment_offset_s
            ):
                raise ValueError("selected observation exceeds the alignment window")

        measurement_ids = {
            item.stream_id: item.observation_id
            for item in self.alignment.boundary_measurements
        }
        same_screen_streams = (
            "fresh_feed",
            "gasoline",
            "kerosene",
            "light_diesel",
            "heavy_diesel",
            "residue",
        )
        same_screen = [catalog[measurement_ids[name]] for name in same_screen_streams]
        for field in ("source_id", "alignment_group", "observed_at", "alignment_quality"):
            values = [payload.get(field) for payload in same_screen]
            if any(not isinstance(value, str) or not value for value in values) or len(
                set(values)
            ) != 1:
                raise ValueError(f"same-screen observations differ in {field}")
        if same_screen[0]["observed_at"] != self.alignment.case_reference_time.isoformat(
            timespec="seconds"
        ):
            raise ValueError("same-screen observations differ from the case reference time")
        feed = _observation_payload_number(
            catalog[measurement_ids["fresh_feed"]],
            measurement_ids["fresh_feed"],
            "si_value",
        )
        product_sum = math.fsum(
            _observation_payload_number(
                catalog[measurement_ids[name]], measurement_ids[name], "si_value"
            )
            for name in same_screen_streams[1:]
        )
        apparent_residual = abs(feed - product_sum) / feed
        if not math.isclose(
            apparent_residual,
            _EXPECTED_SAME_SCREEN_RESIDUAL_FRACTION,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("same-screen apparent residual differs from the fixed evidence")
        if self.apparent_hydrocarbon_residual_fraction != apparent_residual:
            raise ValueError("reported apparent residual does not match the same-screen values")

        internal_specs = {
            item.stream_id: item for item in self.alignment.excluded_internal
        }
        for stream_id, contract in _INTERNAL_OBSERVATION_CONTRACT.items():
            expected_id, expected_role, expected_usage, expected_status = contract
            spec = internal_specs[stream_id]
            if spec.observation_id != expected_id:
                raise ValueError(f"{stream_id} alignment observation was swapped")
            payload = catalog[expected_id]
            if (
                payload.get("variable_role") != expected_role
                or payload.get("usage") != expected_usage
                or payload.get("status") != expected_status
            ):
                raise ValueError(f"{stream_id} observation semantics were swapped")
            reconciled_internal = self.reconciliation.excluded_internal[stream_id]
            if (
                f"observation:{expected_id}" not in reconciled_internal.source_refs
                or reconciled_internal.z_kg_s
                != _observation_payload_number(payload, expected_id, "si_value")
            ):
                raise ValueError(f"{stream_id} reconciliation source differs from observation")

        flash_id = self.alignment.case_overlays.flash_temperature_observation_id
        wash_id = self.alignment.case_overlays.wash_water_observation_id
        wash_feed_id = self.alignment.case_overlays.wash_ratio_feed_observation_id
        if (flash_id, wash_id, wash_feed_id) != (
            "obs-dcs-flash-temperature-ti-1012",
            "obs-dcs-wash-water-fic-1107",
            "obs-dcs-wash-ratio-feed-fi-1018",
        ):
            raise ValueError("case overlay observation identities drifted")
        flash_payload = catalog[flash_id]
        wash_payload = catalog[wash_id]
        wash_feed_payload = catalog[wash_feed_id]
        if (
            self.overlays.flash_temperature_k["observation_id"] != flash_id
            or self.overlays.flash_temperature_k["effective"]
            != _observation_payload_number(flash_payload, flash_id, "si_value")
            or self.overlays.flash_temperature_k["effective_uncertainty_k"]
            != _observation_payload_number(flash_payload, flash_id, "uncertainty_si")
            or self.overlays.flash_temperature_k["offset_s"] != offsets[flash_id]
        ):
            raise ValueError("flash overlay differs from TI-1012 catalog evidence")
        if (
            self.overlays.wash_water_ratio["numerator_observation_id"] != wash_id
            or self.overlays.wash_water_ratio["denominator_observation_id"]
            != wash_feed_id
            or self.overlays.wash_water_ratio["numerator_kg_s"]
            != _observation_payload_number(wash_payload, wash_id, "si_value")
            or self.overlays.wash_water_ratio["denominator_kg_s"]
            != _observation_payload_number(wash_feed_payload, wash_feed_id, "si_value")
            or self.overlays.wash_water_ratio["numerator_uncertainty_kg_s"]
            != _observation_payload_number(wash_payload, wash_id, "uncertainty_si")
            or self.overlays.wash_water_ratio["denominator_uncertainty_kg_s"]
            != _observation_payload_number(wash_feed_payload, wash_feed_id, "uncertainty_si")
            or self.overlays.wash_water_ratio["numerator_offset_s"] != offsets[wash_id]
            or self.overlays.wash_water_ratio["denominator_offset_s"]
            != offsets[wash_feed_id]
        ):
            raise ValueError("wash overlay differs from catalog numerator/denominator")

        targets = dict(self.calibration_targets_kg_s)
        if tuple(targets) != CALIBRATION_TARGETS:
            raise ValueError("pipeline calibration targets differ from the fixed order")
        expected = {
            name: self.reconciliation.entries[name].reconciled_kg_s
            for name in CALIBRATION_TARGETS
        }
        if targets != expected:
            raise ValueError("pipeline targets must come directly from reconciliation")
        if dict(self.calibration.initial.targets_kg_s) != targets:
            raise ValueError("calibration target record differs from reconciliation")
        if self.prior_m2.input_fingerprint != self.calibration.initial.m2_input_fingerprint:
            raise ValueError("prior M2 differs from the calibration initial evaluation")
        if self.final_m2.input_fingerprint != self.calibration.calibrated.m2_input_fingerprint:
            raise ValueError("final M2 differs from the calibrated evaluation")
        for name in CALIBRATION_TARGETS:
            if (
                self.final_m2.product_flows_kg_s[name]
                != self.calibration.calibrated.predictions_kg_s[name]
            ):
                raise ValueError("final M2 predictions differ from calibration")
        if self.calibration.calibrated.total_objective >= self.calibration.initial.total_objective:
            raise ValueError("successful M5 result must strictly improve the objective")
        if (
            self.calibration.initial_sensitivity.numerical_rank != 2
            or self.calibration.calibrated_sensitivity.numerical_rank != 2
        ):
            raise ValueError("successful M5 result requires full-rank initial/final sensitivity")
        if not 0.0 <= apparent_residual <= 0.03:
            raise ValueError("same-screen hydrocarbon apparent residual exceeds 3%")
        versions = dict(self.versions)
        if tuple(versions) != _PIPELINE_VERSION_KEYS:
            raise ValueError("pipeline version keys differ from the fixed contract")
        expected_versions = {
            "software_version": SOFTWARE_VERSION,
            "simulation_stage": "M5",
            "claim_scope": self.alignment.metadata["claim_scope"],
            "alignment_version": self.alignment.alignment_version,
            "reconciliation_config_version": self.alignment.reconciliation_config_version,
            "calibration_version": self.alignment.calibration_version,
            "model_version": self.alignment.model_version,
            "model_config_version": self.alignment.model_config_version,
            "base_parameter_set_version": self.alignment.base_parameter_set_version,
            "calibrated_parameter_set_version": self.calibration.versions[
                "calibrated_parameter_set_version"
            ],
            "base_case_version": self.alignment.base_case_version,
            "derived_case_version": self.alignment.derived_case_version,
            "observation_catalog_version": self.alignment.observation_catalog_version,
            "source_manifest_version": self.alignment.source_manifest_version,
        }
        if versions != expected_versions:
            raise ValueError("pipeline versions differ from alignment and calibration")
        expected_reconciliation_versions = {
            "model_version": self.alignment.model_version,
            "parameter_set_version": self.alignment.base_parameter_set_version,
            "case_version": self.alignment.derived_case_version,
            "observation_catalog_version": self.alignment.observation_catalog_version,
            "reconciliation_config_version": self.alignment.reconciliation_config_version,
        }
        if dict(self.reconciliation.versions) != expected_reconciliation_versions:
            raise ValueError("reconciliation versions differ from alignment")
        if (
            self.calibration.versions["calibration_version"]
            != self.alignment.calibration_version
            or self.calibration.versions["model_version"] != self.alignment.model_version
            or self.calibration.versions["model_config_version"]
            != self.alignment.model_config_version
            or self.calibration.versions["base_parameter_set_version"]
            != self.alignment.base_parameter_set_version
            or self.calibration.versions["case_version"]
            != self.alignment.base_case_version
        ):
            raise ValueError("calibration versions differ from alignment")
        if (
            self.calibrated_model.model_version != self.alignment.model_version
            or self.calibrated_model.config_version != self.alignment.model_config_version
            or self.calibrated_model.parameter_set_version
            != self.alignment.base_parameter_set_version
        ):
            raise ValueError("effective calibrated model versions differ from alignment")

        fingerprints = dict(self.fingerprints)
        if tuple(fingerprints) != _PIPELINE_FINGERPRINT_KEYS:
            raise ValueError("pipeline fingerprint keys differ from the fixed contract")
        if any(not _SHA256_PATTERN.fullmatch(value) for value in fingerprints.values()):
            raise ValueError("pipeline fingerprints must be lowercase SHA-256 digests")
        expected_observation_fingerprint = canonical_fingerprint(
            {"observations": [dict(catalog[item]) for item in catalog_ids]}
        )
        expected_target_fingerprint = canonical_fingerprint(
            {"unit": "kg/s", "values": targets}
        )
        calibrated_model_fingerprint = canonical_fingerprint(
            self.calibrated_model.as_dict()
        )
        effective_model_data = self.calibrated_model.as_dict()
        effective_equipment = cast(dict[str, object], effective_model_data["equipment"])
        effective_column = cast(dict[str, object], effective_equipment["column"])
        effective_cut_points = cast(list[object], effective_column["cut_points_k"])
        for parameter in self.calibration.parameters:
            index = CALIBRATION_PARAMETER_DEFINITIONS[parameter.path][0]
            effective_cut_points[index] = parameter.initial_k
        effective_model_fingerprint = canonical_fingerprint(effective_model_data)
        if self.calibration.fingerprints["base_model"] != effective_model_fingerprint:
            raise ValueError("calibration base model differs from reconstructed effective model")
        baseline_model_data = ModelConfig.from_mapping(effective_model_data).as_dict()
        baseline_equipment = cast(dict[str, object], baseline_model_data["equipment"])
        baseline_desalter = cast(dict[str, object], baseline_equipment["desalter"])
        baseline_wash_ratio = self.overlays.wash_water_ratio["baseline"]
        if isinstance(baseline_wash_ratio, bool) or not isinstance(
            baseline_wash_ratio, (int, float)
        ):
            raise TypeError("wash overlay baseline is not numeric")
        baseline_desalter["wash_water_ratio"] = float(baseline_wash_ratio)
        baseline_model_fingerprint = canonical_fingerprint(baseline_model_data)
        fingerprint_checks = {
            "alignment_object": self.alignment.fingerprint,
            "observation_objects": expected_observation_fingerprint,
            "baseline_model_object": baseline_model_fingerprint,
            "effective_model_object": effective_model_fingerprint,
            "effective_case_object": self.calibration.fingerprints["case"],
            "reconciliation_result": self.reconciliation.result_fingerprint,
            "calibration_result": self.calibration.result_fingerprint,
            "calibrated_model_object": calibrated_model_fingerprint,
        }
        mismatched_fingerprints = sorted(
            name for name, expected in fingerprint_checks.items()
            if fingerprints[name] != expected
        )
        if mismatched_fingerprints:
            raise ValueError(
                "pipeline object fingerprints differ: "
                + ", ".join(mismatched_fingerprints)
            )
        if self.reconciliation.observation_fingerprint != expected_observation_fingerprint:
            raise ValueError("reconciliation observation fingerprint differs from catalog")
        if self.calibration.fingerprints["targets"] != expected_target_fingerprint:
            raise ValueError("calibration target fingerprint differs from reconciliation")
        if self.calibration.fingerprints["calibrated_model"] != calibrated_model_fingerprint:
            raise ValueError("calibration calibrated-model fingerprint differs")

        object.__setattr__(
            self,
            "observation_catalog_evidence",
            MappingProxyType(catalog),
        )
        object.__setattr__(
            self,
            "selected_observations",
            MappingProxyType(selected),
        )
        object.__setattr__(
            self,
            "observation_offsets_s",
            MappingProxyType({key: float(value) for key, value in offsets.items()}),
        )
        object.__setattr__(self, "review_only_observation_ids", review_only)
        object.__setattr__(self, "calibration_targets_kg_s", MappingProxyType(targets))
        object.__setattr__(self, "versions", MappingProxyType(versions))
        object.__setattr__(self, "fingerprints", MappingProxyType(fingerprints))

    @property
    def result_fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "success",
            "data_origin": "mixed_sources",
            "source_composition": {
                "field_observations": "non_synthetic_source_evidence",
                "reconciled_unmeasured_flows": "M2_model_priors",
                "steady_predictions": "synthetic_model_outputs",
            },
            "alignment": self.alignment.as_dict(),
            "overlays": self.overlays.as_dict(),
            "observation_catalog_evidence": {
                key: dict(value)
                for key, value in self.observation_catalog_evidence.items()
            },
            "selected_observations": {
                key: dict(value) for key, value in self.selected_observations.items()
            },
            "observation_offsets_s": dict(self.observation_offsets_s),
            "review_only_observation_ids": list(self.review_only_observation_ids),
            "apparent_hydrocarbon_residual_fraction": (
                self.apparent_hydrocarbon_residual_fraction
            ),
            "prior_m2": self.prior_m2.as_dict(),
            "reconciliation": self.reconciliation.as_dict(),
            "calibration_targets_kg_s": dict(self.calibration_targets_kg_s),
            "calibration": self.calibration.as_dict(),
            "final_m2": self.final_m2.as_dict(),
            "calibrated_parameter_set": {
                "parameter_set_version": self.calibration.versions[
                    "calibrated_parameter_set_version"
                ],
                "base_model_reference": {
                    "model_version": self.versions["model_version"],
                    "model_config_version": self.versions["model_config_version"],
                    "base_parameter_set_version": self.versions[
                        "base_parameter_set_version"
                    ],
                    "effective_model_fingerprint": self.fingerprints[
                        "effective_model_object"
                    ],
                },
                "operating_overlays": self.overlays.as_dict(),
                "parameter_overrides": [
                    item.as_dict() for item in self.calibration.parameters
                ],
                "calibrated_model_fingerprint": self.fingerprints[
                    "calibrated_model_object"
                ],
            },
            "versions": dict(self.versions),
            "fingerprints": dict(self.fingerprints),
        }


@dataclass(frozen=True)
class _LoadedInputs:
    alignment: AlignmentConfig
    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    calibration_config: CalibrationConfig
    observations: tuple[Observation, ...]
    sources: tuple[SourceManifestRecord, ...]
    file_fingerprints: Mapping[str, str]


def _load_inputs(repo_root: Path, alignment_path: Path) -> _LoadedInputs:
    alignment = load_alignment_config(alignment_path)
    paths = alignment.paths
    model_path = _repo_path(repo_root, paths.model_config)
    case_path = _repo_path(repo_root, paths.case_config)
    observation_path = _repo_path(repo_root, paths.observation_catalog)
    source_path = _repo_path(repo_root, paths.source_manifest)
    calibration_path = _repo_path(repo_root, paths.calibration_config)
    model = load_model_config(model_path)
    case = load_case_config(case_path)
    catalog = load_component_catalog(_repo_path(repo_root, model.component_catalog_path))
    calibration = load_calibration_config(calibration_path)
    observations = load_observation_catalog(observation_path, repo_root=repo_root)
    sources = load_source_manifest(source_path, repo_root=repo_root)
    validate_observation_sources(observations, sources, repo_root=repo_root)
    validate_config_compatibility(model, case, software_version=SOFTWARE_VERSION, catalog=catalog)

    version_pairs = {
        "schema_version": (alignment.schema_version, model.schema_version),
        "model_version": (alignment.model_version, model.model_version),
        "model_config_version": (alignment.model_config_version, model.config_version),
        "base_parameter_set_version": (
            alignment.base_parameter_set_version,
            model.parameter_set_version,
        ),
        "base_case_version": (alignment.base_case_version, case.case_version),
        "observation_catalog_version": (
            alignment.observation_catalog_version,
            OBSERVATION_CATALOG_VERSION,
        ),
        "source_manifest_version": (
            alignment.source_manifest_version,
            SOURCE_MANIFEST_VERSION,
        ),
        "calibration_version": (
            alignment.calibration_version,
            calibration.calibration_version,
        ),
        "reconciliation_version": (
            alignment.reconciliation_config_version,
            calibration.data_reference.reconciliation_version,
        ),
        "reconciliation_path": (
            alignment.artifacts.reconciled_case,
            calibration.data_reference.path,
        ),
    }
    mismatches = sorted(name for name, pair in version_pairs.items() if pair[0] != pair[1])
    if mismatches:
        raise M5PipelineError(
            "source_preflight",
            "alignment input mismatch: " + ", ".join(mismatches),
        )
    if any(item.catalog_version != alignment.observation_catalog_version for item in observations):
        raise M5PipelineError("source_preflight", "observation catalog version drift")
    if any(item.manifest_version != alignment.source_manifest_version for item in sources):
        raise M5PipelineError("source_preflight", "source manifest version drift")
    file_fingerprints = {
        "alignment_file": file_sha256(alignment_path),
        "model_file": cdu_resource_file_sha256(model_path, paths.model_config),
        "case_file": cdu_resource_file_sha256(case_path, paths.case_config),
        "component_catalog_file": cdu_resource_file_sha256(
            _repo_path(repo_root, model.component_catalog_path),
            model.component_catalog_path,
        ),
        "observation_catalog_file": file_sha256(observation_path),
        "source_manifest_file": file_sha256(source_path),
        "calibration_config_file": file_sha256(calibration_path),
    }
    return _LoadedInputs(
        alignment=alignment,
        model=model,
        case=case,
        catalog=catalog,
        calibration_config=calibration,
        observations=observations,
        sources=sources,
        file_fingerprints=MappingProxyType(file_fingerprints),
    )


def _observation_index(observations: tuple[Observation, ...]) -> Mapping[str, Observation]:
    result = {item.id: item for item in observations}
    if len(result) != len(observations):  # pragma: no cover - loader already rejects
        raise M5PipelineError("source_preflight", "duplicate observation ids")
    return MappingProxyType(result)


def _require_observation(
    observations: Mapping[str, Observation],
    observation_id: str,
    *,
    unit: str,
) -> Observation:
    try:
        result = observations[observation_id]
    except KeyError as exc:
        raise M5PipelineError(
            "source_preflight", f"missing observation {observation_id}"
        ) from exc
    if result.si_unit != unit:
        raise M5PipelineError(
            "source_preflight",
            f"observation {observation_id} must use {unit}",
        )
    return result


def _validate_offset(observation: Observation, alignment: AlignmentConfig) -> float:
    offset = _observation_offset(observation, alignment)
    if abs(offset) > alignment.maximum_alignment_offset_s:
        raise M5PipelineError(
            "source_preflight",
            f"observation {observation.id} exceeds alignment offset limit",
        )
    return offset


def _observation_offset(observation: Observation, alignment: AlignmentConfig) -> float:
    return (observation.observed_at - alignment.case_reference_time).total_seconds()


def _expected_selected_observation_ids(alignment: AlignmentConfig) -> tuple[str, ...]:
    selected = {item.observation_id for item in alignment.boundary_measurements}
    selected.update(item.observation_id for item in alignment.excluded_internal)
    selected.update(
        {
            alignment.case_overlays.flash_temperature_observation_id,
            alignment.case_overlays.wash_water_observation_id,
            alignment.case_overlays.wash_ratio_feed_observation_id,
        }
    )
    return tuple(sorted(selected))


def _observation_payload_time(payload: Mapping[str, object], observation_id: str) -> datetime:
    raw = payload.get("observed_at")
    if not isinstance(raw, str):
        raise TypeError(f"observation {observation_id} omitted observed_at")
    try:
        observed_at = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"observation {observation_id} has invalid observed_at") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError(f"observation {observation_id} observed_at lacks timezone")
    return observed_at


def _observation_payload_number(
    payload: Mapping[str, object], observation_id: str, name: str
) -> float:
    raw = payload.get(name)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"observation {observation_id} has invalid {name}")
    return float(raw)


def _effective_inputs(
    loaded: _LoadedInputs,
    by_id: Mapping[str, Observation],
) -> tuple[ModelConfig, CaseConfig, CaseOverlayEvidence, Mapping[str, float]]:
    alignment = loaded.alignment
    expected_overlay_ids = (
        "obs-dcs-flash-temperature-ti-1012",
        "obs-dcs-wash-water-fic-1107",
        "obs-dcs-wash-ratio-feed-fi-1018",
    )
    actual_overlay_ids = (
        alignment.case_overlays.flash_temperature_observation_id,
        alignment.case_overlays.wash_water_observation_id,
        alignment.case_overlays.wash_ratio_feed_observation_id,
    )
    if actual_overlay_ids != expected_overlay_ids:
        raise M5PipelineError("case_overlay", "case overlay observation identities drifted")
    flash = _require_observation(
        by_id,
        alignment.case_overlays.flash_temperature_observation_id,
        unit="K",
    )
    wash = _require_observation(
        by_id,
        alignment.case_overlays.wash_water_observation_id,
        unit="kg/s",
    )
    wash_feed = _require_observation(
        by_id,
        alignment.case_overlays.wash_ratio_feed_observation_id,
        unit="kg/s",
    )
    if flash.instrument_tag != "TI-1012" or flash.variable != "flash_temperature":
        raise M5PipelineError("case_overlay", "flash overlay must use TI-1012")
    if flash.usage != "diagnostic_reference" or flash.status != "reference_only":
        raise M5PipelineError("case_overlay", "flash overlay must remain reference-only")
    if wash.usage != "data_coordination" or wash.status != "candidate":
        raise M5PipelineError("case_overlay", "wash water must be a coordination candidate")
    if wash_feed.usage != "diagnostic_reference" or wash_feed.status != "reference_only":
        raise M5PipelineError("case_overlay", "wash ratio denominator must be reference-only")
    offsets = {
        item.id: _validate_offset(item, alignment) for item in (flash, wash, wash_feed)
    }
    wash_ratio = wash.si_value / wash_feed.si_value
    wash_ratio_uncertainty = wash_ratio * math.sqrt(
        (wash.uncertainty_si / wash.si_value) ** 2
        + (wash_feed.uncertainty_si / wash_feed.si_value) ** 2
    )
    model_data = loaded.model.as_dict()
    equipment = cast(dict[str, object], model_data["equipment"])
    desalter = cast(dict[str, object], equipment["desalter"])
    baseline_wash_ratio = cast(float, desalter["wash_water_ratio"])
    desalter["wash_water_ratio"] = wash_ratio
    effective_model = ModelConfig.from_mapping(model_data)
    effective_conditions = dict(loaded.case.operating_conditions)
    baseline_flash_temperature = effective_conditions["flash_temperature_k"]
    effective_conditions["flash_temperature_k"] = flash.si_value
    effective_case = replace(
        loaded.case,
        operating_conditions=MappingProxyType(effective_conditions),
    )
    validate_config_compatibility(
        effective_model,
        effective_case,
        software_version=SOFTWARE_VERSION,
        catalog=loaded.catalog,
    )
    overlays = CaseOverlayEvidence(
        flash_temperature_k={
            "baseline": baseline_flash_temperature,
            "effective": flash.si_value,
            "unit": "K",
            "observation_id": flash.id,
            "instrument_tag": flash.instrument_tag,
            "offset_s": offsets[flash.id],
            "effective_uncertainty_k": flash.uncertainty_si,
            "uncertainty_basis": flash.uncertainty_basis,
            "revision_reason": (
                "Replace the baseline 220 degC case assumption with the source-traced "
                "TI-1012 operating reference for this case alignment only."
            ),
        },
        wash_water_ratio={
            "baseline": baseline_wash_ratio,
            "effective": wash_ratio,
            "unit": "kg/kg fresh feed",
            "numerator_observation_id": wash.id,
            "denominator_observation_id": wash_feed.id,
            "numerator_kg_s": wash.si_value,
            "denominator_kg_s": wash_feed.si_value,
            "formula": f"{wash.si_value}/{wash_feed.si_value}",
            "numerator_offset_s": offsets[wash.id],
            "denominator_offset_s": offsets[wash_feed.id],
            "numerator_uncertainty_kg_s": wash.uncertainty_si,
            "denominator_uncertainty_kg_s": wash_feed.uncertainty_si,
            "derived_uncertainty_kg_per_kg": wash_ratio_uncertainty,
            "propagation_formula": (
                "r*sqrt((u_numerator/numerator)^2+(u_denominator/denominator)^2)"
            ),
            "uncertainty_basis": (
                "First-order independent propagation of the two display/transcription "
                "resolution envelopes; not statistical one-sigma and excluding unknown "
                "cross-screen timing error."
            ),
            "revision_reason": (
                "Replace the 0.04 engineering wash-ratio assumption with the source-traced "
                "18.97/407.60 t/h ratio for this case alignment only."
            ),
        },
    )
    return effective_model, effective_case, overlays, MappingProxyType(offsets)


def _reconciliation_inputs(
    loaded: _LoadedInputs,
    by_id: Mapping[str, Observation],
    prior_m2: M2Evidence,
) -> tuple[FlowEstimateInput, ...]:
    values: list[FlowEstimateInput] = []
    for measurement_spec in loaded.alignment.boundary_measurements:
        observation = _require_observation(
            by_id, measurement_spec.observation_id, unit="kg/s"
        )
        expected_role = _MEASUREMENT_ROLES[measurement_spec.stream_id]
        if observation.variable_role != expected_role:
            raise M5PipelineError(
                "reconciliation_preflight",
                f"{observation.id} role differs from {expected_role}",
            )
        if observation.usage != "data_coordination" or observation.status != "candidate":
            raise M5PipelineError(
                "reconciliation_preflight",
                f"{observation.id} is not an active coordination observation",
            )
        _validate_offset(observation, loaded.alignment)
        values.append(
            FlowEstimateInput(
                stream_id=measurement_spec.stream_id,
                direction=(
                    "inlet"
                    if measurement_spec.stream_id in {"fresh_feed", "wash_water"}
                    else "outlet"
                ),
                z_kg_s=observation.si_value,
                sigma_kg_s=measurement_spec.scale_for(observation.si_value),
                source_refs=(f"observation:{observation.id}", f"source:{observation.source_id}"),
            )
        )
    for prior_spec in loaded.alignment.latent_priors:
        prior = prior_m2.product_flows_kg_s[prior_spec.stream_id]
        values.append(
            FlowEstimateInput(
                stream_id=prior_spec.stream_id,
                direction="outlet",
                prior_kg_s=prior,
                tau_kg_s=prior_spec.scale_for(max(prior, 1e-15)),
                source_refs=(f"m2-effective-baseline:{prior_m2.input_fingerprint}",),
            )
        )
    for internal_spec in loaded.alignment.excluded_internal:
        observation = _require_observation(
            by_id, internal_spec.observation_id, unit="kg/s"
        )
        expected_id, expected_role, expected_usage, expected_status = (
            _INTERNAL_OBSERVATION_CONTRACT[internal_spec.stream_id]
        )
        if internal_spec.observation_id != expected_id:
            raise M5PipelineError(
                "reconciliation_preflight",
                f"{internal_spec.stream_id} must use {expected_id}",
            )
        if (
            observation.variable_role != expected_role
            or observation.usage != expected_usage
            or observation.status != expected_status
        ):
            raise M5PipelineError(
                "reconciliation_preflight",
                f"{observation.id} differs from the fixed {internal_spec.stream_id} semantics",
            )
        _validate_offset(observation, loaded.alignment)
        values.append(
            FlowEstimateInput(
                stream_id=internal_spec.stream_id,
                direction="internal",
                z_kg_s=observation.si_value,
                sigma_kg_s=internal_spec.scale_for(observation.si_value),
                source_refs=(f"observation:{observation.id}", f"source:{observation.source_id}"),
                exclusion_reason=internal_spec.exclusion_reason,
            )
        )
    return tuple(values)


def _apparent_hydrocarbon_residual(
    loaded: _LoadedInputs,
    by_id: Mapping[str, Observation],
) -> float:
    specs = {item.stream_id: item for item in loaded.alignment.boundary_measurements}
    feed = by_id[specs["fresh_feed"].observation_id].si_value
    product_sum = math.fsum(
        by_id[specs[name].observation_id].si_value
        for name in ("gasoline", "kerosene", "light_diesel", "heavy_diesel", "residue")
    )
    return abs(feed - product_sum) / feed


def run_m5_pipeline(
    repo_root: Path,
    alignment_path: Path | None = None,
) -> M5PipelineResult:
    """Run source validation, effective-case overlays, WLS and two-cut calibration."""

    root = repo_root.resolve()
    chosen_alignment = (
        alignment_path.resolve()
        if alignment_path is not None
        else resolve_cdu_repository_path(
            root,
            "configs/reconciliation/m5_case_20260604_v0.1.0.json",
        )
    )
    try:
        loaded = _load_inputs(root, chosen_alignment)
    except M5PipelineError:
        raise
    except (ConfigurationError, OSError, TypeError, ValueError) as exc:
        raise M5PipelineError("source_preflight", str(exc)) from exc
    try:
        by_id = _observation_index(loaded.observations)
        effective_model, effective_case, overlays, overlay_offsets = _effective_inputs(
            loaded, by_id
        )
    except M5PipelineError:
        raise
    except (ArithmeticError, KeyError, OSError, TypeError, ValueError) as exc:
        raise M5PipelineError("case_overlay", str(exc)) from exc
    try:
        prior_recycle = solve_recycle(effective_model, effective_case, loaded.catalog)
        prior_m2 = _m2_evidence(
            prior_recycle,
            effective_model,
            stage="effective_baseline_m2",
        )
    except M5PipelineError:
        raise
    except (ArithmeticError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise M5PipelineError("effective_baseline_m2", str(exc)) from exc
    try:
        reconciliation_inputs = _reconciliation_inputs(loaded, by_id, prior_m2)
        observation_fingerprint = canonical_fingerprint(
            {"observations": [item.as_dict() for item in loaded.observations]}
        )
        reconciliation = reconcile_boundary_flows(
            reconciliation_inputs,
            versions={
                "model_version": loaded.alignment.model_version,
                "parameter_set_version": loaded.alignment.base_parameter_set_version,
                "case_version": loaded.alignment.derived_case_version,
                "observation_catalog_version": loaded.alignment.observation_catalog_version,
                "reconciliation_config_version": (
                    loaded.alignment.reconciliation_config_version
                ),
            },
            observation_fingerprint=observation_fingerprint,
        )
        targets = {
            name: reconciliation.entries[name].reconciled_kg_s
            for name in CALIBRATION_TARGETS
        }
    except M5PipelineError:
        raise
    except (ArithmeticError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise M5PipelineError("reconciliation", str(exc)) from exc
    try:
        calibration = run_calibration(
            loaded.calibration_config,
            effective_model,
            effective_case,
            loaded.catalog,
            targets,
        )
    except (
        ArithmeticError,
        ConfigurationError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise M5PipelineError("calibration", str(exc)) from exc
    try:
        calibrated_model = apply_calibration_parameters(
            effective_model,
            loaded.calibration_config,
            calibration.calibrated.parameters_k,
        )
    except (ArithmeticError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise M5PipelineError("calibration", str(exc)) from exc
    try:
        final_m2 = _m2_evidence(
            solve_recycle(calibrated_model, effective_case, loaded.catalog),
            calibrated_model,
            stage="final_m2",
        )
    except M5PipelineError:
        raise
    except (ArithmeticError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise M5PipelineError("final_m2", str(exc)) from exc
    selected_ids = _expected_selected_observation_ids(loaded.alignment)
    catalog_evidence = {
        item.id: item.as_dict() for item in loaded.observations
    }
    selected = {
        observation_id: by_id[observation_id].as_dict()
        for observation_id in selected_ids
    }
    offsets = {
        item.id: _observation_offset(item, loaded.alignment)
        for item in loaded.observations
    }
    if any(offsets[key] != value for key, value in overlay_offsets.items()):
        raise M5PipelineError("result_contract", "overlay offsets drifted")
    review_only = tuple(
        item.id for item in loaded.observations if item.id not in selected_ids
    )
    source_object_fingerprint = canonical_fingerprint(
        {"sources": [item.as_dict() for item in loaded.sources]}
    )
    versions = {
        "software_version": SOFTWARE_VERSION,
        "simulation_stage": "M5",
        "claim_scope": "case_alignment_only",
        "alignment_version": loaded.alignment.alignment_version,
        "reconciliation_config_version": (
            loaded.alignment.reconciliation_config_version
        ),
        "calibration_version": loaded.calibration_config.calibration_version,
        "model_version": loaded.model.model_version,
        "model_config_version": loaded.model.config_version,
        "base_parameter_set_version": loaded.model.parameter_set_version,
        "calibrated_parameter_set_version": (
            loaded.calibration_config.calibrated_parameter_set_version
        ),
        "base_case_version": loaded.case.case_version,
        "derived_case_version": loaded.alignment.derived_case_version,
        "observation_catalog_version": loaded.alignment.observation_catalog_version,
        "source_manifest_version": loaded.alignment.source_manifest_version,
    }
    fingerprints = {
        **dict(loaded.file_fingerprints),
        "alignment_object": loaded.alignment.fingerprint,
        "observation_objects": observation_fingerprint,
        "source_objects": source_object_fingerprint,
        "baseline_model_object": canonical_fingerprint(loaded.model.as_dict()),
        "baseline_case_object": canonical_fingerprint(loaded.case.as_dict()),
        "effective_model_object": canonical_fingerprint(effective_model.as_dict()),
        "effective_case_object": canonical_fingerprint(effective_case.as_dict()),
        "reconciliation_result": reconciliation.result_fingerprint,
        "calibration_result": calibration.result_fingerprint,
        "calibrated_model_object": canonical_fingerprint(calibrated_model.as_dict()),
    }
    try:
        return M5PipelineResult(
            alignment=loaded.alignment,
            overlays=overlays,
            observation_catalog_evidence=catalog_evidence,
            selected_observations=selected,
            observation_offsets_s=offsets,
            review_only_observation_ids=review_only,
            apparent_hydrocarbon_residual_fraction=_apparent_hydrocarbon_residual(
                loaded, by_id
            ),
            prior_m2=prior_m2,
            reconciliation=reconciliation,
            calibration_targets_kg_s=targets,
            calibration=calibration,
            final_m2=final_m2,
            calibrated_model=calibrated_model,
            versions=versions,
            fingerprints=fingerprints,
        )
    except M5PipelineError:
        raise
    except (ArithmeticError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise M5PipelineError("result_contract", str(exc)) from exc
