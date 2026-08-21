from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.calibration.calibration import CalibrationError, run_calibration
from petroleum_rto.cdu.calibration.config import CalibrationConfig, load_calibration_config
from petroleum_rto.cdu.core.config import (
    CaseConfig,
    ModelConfig,
    load_case_config,
    load_component_catalog,
    load_model_config,
)
from petroleum_rto.cdu.core.types import BalanceReport
from petroleum_rto.cdu.flowsheet.recycle import RecycleSolveResult, solve_recycle
from petroleum_rto.cdu.properties.components import ComponentCatalog
from petroleum_rto.cdu.repository import resolve_cdu_repository_path


@pytest.fixture(scope="module")
def calibration_inputs(
    repo_root: Path,
) -> tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog]:
    config = load_calibration_config(
        repo_root / "configs/cdu/calibration/m5_case_20260604_v0.1.0.json"
    )
    model = load_model_config(repo_root / "configs/cdu/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cdu/cases/case_20260604.json")
    catalog = load_component_catalog(
        resolve_cdu_repository_path(repo_root, model.component_catalog_path)
    )
    return config, model, case, catalog


@pytest.fixture(scope="module")
def representative_reconciled_targets_kg_s() -> dict[str, float]:
    return {
        "light_diesel": 22.25 / 3.6,
        "heavy_diesel": 82.41 / 3.6,
        "residue": 203.72 / 3.6,
    }


def test_real_m2_calibration_reduces_weighted_error_and_stays_bounded(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    result = run_calibration(
        *calibration_inputs,
        representative_reconciled_targets_kg_s,
    )

    assert result.status == "success"
    assert result.calibrated.data_misfit < result.initial.data_misfit
    assert result.calibrated.total_objective < result.initial.total_objective
    assert result.initial.regularization_penalty == 0.0
    assert result.calibrated.total_objective == pytest.approx(
        result.calibrated.data_misfit + result.calibrated.regularization_penalty,
        abs=1e-12,
    )
    assert result.calibrated.data_misfit < 0.01 * result.initial.data_misfit
    for parameter in result.parameters:
        assert parameter.lower_bound_k <= parameter.calibrated_k <= parameter.upper_bound_k
    assert result.boundary_hits == ()
    assert result.final_step_k < calibration_inputs[0].optimizer.minimum_step_k
    assert result.versions["simulation_stage"] == "M5"
    assert result.versions["reconciliation_version"] == (
        calibration_inputs[0].data_reference.reconciliation_version
    )
    assert len(result.result_fingerprint) == 64
    assert set(result.calibrated.predictions_kg_s) == {
        "light_diesel",
        "heavy_diesel",
        "residue",
    }
    for name, prediction in result.calibrated.predictions_kg_s.items():
        assert result.calibrated.errors_kg_s[name] == pytest.approx(
            prediction - representative_reconciled_targets_kg_s[name],
            abs=1e-12,
        )


def test_central_sensitivity_is_three_by_two_full_rank_and_well_conditioned(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    result = run_calibration(
        *calibration_inputs,
        representative_reconciled_targets_kg_s,
    )
    initial = result.initial_sensitivity
    sensitivity = result.calibrated_sensitivity

    for analysis in (initial, sensitivity):
        assert len(analysis.matrix_kg_s_per_k) == 3
        assert all(len(row) == 2 for row in analysis.matrix_kg_s_per_k)
        assert analysis.numerical_rank == 2
        assert analysis.condition_number is not None
        assert 1.0 <= analysis.condition_number < 3.0
        assert analysis.column_cosine == pytest.approx(-0.5, abs=0.03)
        assert analysis.singular_values[0] > analysis.singular_values[1] > 0.0
        assert len(analysis.normalized_matrix) == 3
        for row_index, target in enumerate(("light_diesel", "heavy_diesel", "residue")):
            for column_index, path in enumerate(
                ("column.cut_points_k[2]", "column.cut_points_k[3]")
            ):
                assert analysis.normalized_matrix[row_index][column_index] == pytest.approx(
                    analysis.matrix_kg_s_per_k[row_index][column_index]
                    * analysis.parameter_scales_k[path]
                    / analysis.target_scales_kg_s[target],
                    abs=1e-12,
                )
    assert initial.requested_parameters_k == {
        "column.cut_points_k[2]": 583.15,
        "column.cut_points_k[3]": 638.15,
    }
    assert initial.reference_parameters_k == initial.requested_parameters_k
    assert not initial.reference_adjusted_for_bounds
    assert sensitivity.requested_parameters_k == result.calibrated.parameters_k
    assert sensitivity.reference_parameters_k == sensitivity.requested_parameters_k
    assert not sensitivity.reference_adjusted_for_bounds
    assert result.sensitivity is result.calibrated_sensitivity


def test_calibration_is_exactly_repeatable(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    first = run_calibration(
        *calibration_inputs,
        representative_reconciled_targets_kg_s,
    )
    second = run_calibration(
        *calibration_inputs,
        representative_reconciled_targets_kg_s,
    )

    assert first.as_dict() == second.as_dict()
    assert first.result_fingerprint == second.result_fingerprint


def test_extreme_targets_report_both_physical_boundary_hits(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
) -> None:
    result = run_calibration(
        *calibration_inputs,
        {"light_diesel": 0.1, "heavy_diesel": 40.0, "residue": 30.0},
    )

    assert result.boundary_hits == (
        "column.cut_points_k[2]",
        "column.cut_points_k[3]",
    )
    assert result.parameters[0].at_lower_bound
    assert result.parameters[1].at_upper_bound
    assert result.calibrated_sensitivity.reference_adjusted_for_bounds
    assert result.calibrated_sensitivity.reference_parameters_k == {
        "column.cut_points_k[2]": 569.15,
        "column.cut_points_k[3]": 652.15,
    }
    assert math.isfinite(result.calibrated.total_objective)


def test_nonconverged_m2_evaluation_fails_calibration(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    def nonconverged_solver(
        model: ModelConfig,
        case: CaseConfig,
        catalog: ComponentCatalog,
    ) -> RecycleSolveResult:
        del model, case, catalog
        return RecycleSolveResult(
            status="not_converged",
            flowsheet=None,
            iterations=1,
            final_residual=0.1,
            residual_history=(0.1,),
            reflux=None,
            failure_reason="forced non-convergence",
            failure_stage="convergence",
        )

    with pytest.raises(CalibrationError, match="did not converge"):
        run_calibration(
            *calibration_inputs,
            representative_reconciled_targets_kg_s,
            solver=nonconverged_solver,
        )


def test_conservation_failure_cannot_supply_an_objective(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    def nonconserving_solver(
        model: ModelConfig,
        case: CaseConfig,
        catalog: ComponentCatalog,
    ) -> RecycleSolveResult:
        valid = solve_recycle(model, case, catalog)
        flowsheet = valid.require_converged()
        bad_balance = BalanceReport(
            inlet_kg_s=flowsheet.balance.inlet_kg_s,
            outlet_kg_s=flowsheet.balance.outlet_kg_s + 1.0,
            component_residuals_kg_s=flowsheet.balance.component_residuals_kg_s,
            salt_residual_kg_s=flowsheet.balance.salt_residual_kg_s,
            energy_residual_w=flowsheet.balance.energy_residual_w,
        )
        return replace(valid, flowsheet=replace(flowsheet, balance=bad_balance))

    with pytest.raises(CalibrationError, match="overall conservation"):
        run_calibration(
            *calibration_inputs,
            representative_reconciled_targets_kg_s,
            solver=nonconserving_solver,
        )


def test_rank_degenerate_solver_is_rejected(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    _, base_model, _, _ = calibration_inputs

    def parameter_insensitive_solver(
        model: ModelConfig,
        case: CaseConfig,
        catalog: ComponentCatalog,
    ) -> RecycleSolveResult:
        del model
        return solve_recycle(base_model, case, catalog)

    with pytest.raises(CalibrationError, match="not full rank"):
        run_calibration(
            *calibration_inputs,
            representative_reconciled_targets_kg_s,
            solver=parameter_insensitive_solver,
        )


def test_success_result_rejects_replace_forgery(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    representative_reconciled_targets_kg_s: dict[str, float],
) -> None:
    result = run_calibration(
        *calibration_inputs,
        representative_reconciled_targets_kg_s,
    )

    with pytest.raises(ValueError, match="strictly improve"):
        replace(result, calibrated=result.initial)
    altered_parameter = replace(
        result.parameters[0],
        calibrated_k=result.parameters[0].calibrated_k + 0.1,
    )
    with pytest.raises(ValueError, match="differs from the calibrated evaluation"):
        replace(result, parameters=(altered_parameter, result.parameters[1]))
    with pytest.raises(ValueError, match="numerical rank"):
        replace(result.initial_sensitivity, numerical_rank=1)
    forged_normalized_matrix = (
        (
            result.initial_sensitivity.normalized_matrix[0][0] + 1.0,
            result.initial_sensitivity.normalized_matrix[0][1],
        ),
        *result.initial_sensitivity.normalized_matrix[1:],
    )
    with pytest.raises(ValueError, match="normalized sensitivity"):
        replace(
            result.initial_sensitivity,
            normalized_matrix=forged_normalized_matrix,
        )


def test_no_improvement_is_an_explicit_calibration_failure(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
) -> None:
    _, model, case, catalog = calibration_inputs
    flowsheet = solve_recycle(model, case, catalog).require_converged()
    exact_prior_targets = {
        name: flowsheet.products[name].mass_flow_kg_s
        for name in ("light_diesel", "heavy_diesel", "residue")
    }

    with pytest.raises(CalibrationError, match="did not strictly improve"):
        run_calibration(*calibration_inputs, exact_prior_targets)


@pytest.mark.parametrize(
    "targets",
    [
        {"light_diesel": 1.0, "heavy_diesel": 2.0},
        {
            "light_diesel": 1.0,
            "heavy_diesel": 2.0,
            "residue": 3.0,
            "gasoline": 4.0,
        },
        {"light_diesel": 1.0, "heavy_diesel": 2.0, "residue": 0.0},
    ],
)
def test_target_contract_rejects_missing_extra_or_nonpositive_values(
    calibration_inputs: tuple[CalibrationConfig, ModelConfig, CaseConfig, ComponentCatalog],
    targets: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        run_calibration(*calibration_inputs, targets)
