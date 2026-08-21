from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from petroleum_rto.cdu.validation.basis import M6Basis, load_m6_basis
from petroleum_rto.cdu.validation.domain import (
    ApplicabilityAssessment,
    DomainDimension,
    DomainRepresentation,
    assess_applicability,
)
from petroleum_rto.cdu.validation.protection import (
    ProtectionAction,
    ProtectionFrame,
    ProtectionRule,
    ProtectionTrace,
    run_protection,
)
from petroleum_rto.cdu.validation.results import (
    M6_COMPLETION_CHECK_IDS,
    M6_RESULT_METADATA,
    M6_RESULT_SCHEMA_VERSION,
    M6_SOURCE_COMPOSITION,
    M6ValidationResult,
    ScenarioValidationResult,
)
from petroleum_rto.cdu.validation.tracking import ControllerTrackingEvidence
from petroleum_rto.cdu.validation.uncertainty import (
    EngineeringInputInterval,
    InputSensitivitySpec,
    LocalSensitivityAnalysis,
    OutputSensitivitySpec,
    UncertaintyPropagationResult,
    propagate_uncertainty,
    run_local_sensitivity,
)

type PlanEvidence = tuple[
    Mapping[str, LocalSensitivityAnalysis],
    Mapping[str, UncertaintyPropagationResult],
]


@pytest.fixture(scope="module")
def m6_basis(repo_root: Path) -> M6Basis:
    return load_m6_basis(repo_root)


def _domain(
    status: str,
) -> ApplicabilityAssessment:
    representation: DomainRepresentation = (
        "unsupported" if status == "rejected" else "direct"
    )
    dimension = DomainDimension(
        dimension_id="scenario_input",
        unit="ratio",
        representation=representation,
        input_layer="M6_supervision",
        confidence="synthetic_logic_only",
        assumptions=("unit_test_assumption",),
        reference_value=1.0,
        normal_min=0.95,
        normal_max=1.05,
        limited_min=0.9,
        limited_max=1.1,
        source="M6_engineering_validation_envelope",
    )
    return assess_applicability(
        (dimension,),
        {dimension.dimension_id: 1.0},
        abnormal_verification=status == "limited",
    )


def _protection_trace() -> ProtectionTrace:
    signal = "furnace_outlet_temperature_k"
    rule = ProtectionRule(
        rule_id="high_temperature",
        priority=10,
        condition="high",
        signal_name=signal,
        trip_threshold=10.0,
        clear_threshold=8.0,
        trigger_delay_s=0.0,
        clear_delay_s=1.0,
        latching=False,
        action=ProtectionAction(
            {"furnace_fuel_duty_w": 0.8},
            ("furnace_temperature",),
        ),
    )
    return run_protection(
        (rule,),
        (ProtectionFrame(0.0, {signal: 11.0}, {signal: True}),),
    )


def _tracking_evidence(*, passed: bool = True) -> ControllerTrackingEvidence:
    tracking_error = 0.0 if passed else 1.0
    payload: dict[str, object] = {
        "loop_id": "furnace_temperature",
        "initial_output": 1.0,
        "protected_output": 0.8,
        "final_manual_output": 0.8,
        "return_automatic_output": 0.8,
        "manual_steps": 1,
        "maximum_manual_output_change": 0.2,
        "final_tracking_error": tracking_error,
        "automatic_return_jump": tracking_error,
        "tolerance": 1e-6,
        "passed": passed,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return ControllerTrackingEvidence(
        loop_id="furnace_temperature",
        initial_output=1.0,
        protected_output=0.8,
        final_manual_output=0.8,
        return_automatic_output=0.8,
        manual_steps=1,
        maximum_manual_output_change=0.2,
        final_tracking_error=tracking_error,
        automatic_return_jump=tracking_error,
        tolerance=1e-6,
        passed=passed,
        evidence_fingerprint=hashlib.sha256(encoded).hexdigest(),
    )
def _passed_scenario(
    scenario_id: str,
    execution_layer: str,
) -> ScenarioValidationResult:
    layer_source = {
        "M2_steady": "M2_steady_model_prediction",
        "M3_open_loop": "M3_open_loop_simulation",
        "M4_closed_loop": "M4_closed_loop_simulation",
    }[execution_layer]
    domain = _domain("passed")
    return ScenarioValidationResult(
        scenario_id=scenario_id,
        scenario_version=f"{scenario_id}-v0.1.0",
        claim_ids=(f"claim.{scenario_id}",),
        purpose=f"Verify {scenario_id}.",
        execution_layer=execution_layer,  # type: ignore[arg-type]
        scenario_status="passed",
        expected_status="passed",
        verification_outcome="passed",
        solver_called=True,
        domain=domain,
        metrics={"finite_output": 1.0},
        direction_checks={"expected_direction": True},
        conservation_checks={"mass_conservation": True},
        protection_trace=None,
        source_origins=(layer_source, "M6_synthetic_validation"),
        engine_status="success",
        input_fingerprint=domain.input_fingerprint,
    )


def _limited_scenario(scenario_id: str = "protection_trip") -> ScenarioValidationResult:
    domain = _domain("limited")
    return ScenarioValidationResult(
        scenario_id=scenario_id,
        scenario_version=f"{scenario_id}-v0.1.0",
        claim_ids=(f"claim.{scenario_id}",),
        purpose=f"Verify {scenario_id}.",
        execution_layer="M6_supervisory",
        scenario_status="limited",
        expected_status="limited",
        verification_outcome="passed",
        solver_called=False,
        domain=domain,
        metrics={"tracking_no_bump": 1.0},
        direction_checks={"protective_action_direction": True},
        conservation_checks={"tracking_no_bump": True},
        protection_trace=_protection_trace(),
        source_origins=("M6_synthetic_validation",),
        engine_status=None,
        input_fingerprint=domain.input_fingerprint,
    )


def _rejected_scenario(
    scenario_id: str = "stripping_steam",
) -> ScenarioValidationResult:
    domain = _domain("rejected")
    return ScenarioValidationResult(
        scenario_id=scenario_id,
        scenario_version=f"{scenario_id}-v0.1.0",
        claim_ids=(f"claim.{scenario_id}",),
        purpose=f"Verify {scenario_id}.",
        execution_layer="structural_rejection",
        scenario_status="rejected",
        expected_status="rejected",
        verification_outcome="passed",
        solver_called=False,
        domain=domain,
        metrics={},
        direction_checks={},
        conservation_checks={},
        protection_trace=None,
        source_origins=("M6_synthetic_validation",),
        engine_status=None,
        input_fingerprint=domain.input_fingerprint,
    )


@pytest.fixture(scope="module")
def plan_evidence(m6_basis: M6Basis) -> PlanEvidence:
    def evaluator(inputs: Mapping[str, float]) -> Mapping[str, float]:
        return {"quality_proxy": 2.0 * inputs["feed_ratio"]}

    def one_plan(input_id: str) -> tuple[
        LocalSensitivityAnalysis,
        UncertaintyPropagationResult,
    ]:
        analysis = run_local_sensitivity(
            (InputSensitivitySpec(input_id, 1.0, 0.01, 1.0),),
            (OutputSensitivitySpec("quality_proxy", 2.0),),
            evaluator,
            basis_fingerprint=m6_basis.analysis_basis_fingerprint,
        )
        propagation = propagate_uncertainty(
            analysis,
            (EngineeringInputInterval(input_id, 0.95, 1.05),),
        )
        return analysis, propagation

    steady_analysis, steady_result = one_plan("feed_ratio")

    def dynamic_evaluator(inputs: Mapping[str, float]) -> Mapping[str, float]:
        return {"quality_proxy": 3.0 * inputs["cooling_ratio"]}

    dynamic_analysis = run_local_sensitivity(
        (InputSensitivitySpec("cooling_ratio", 1.0, 0.01, 1.0),),
        (OutputSensitivitySpec("quality_proxy", 3.0),),
        dynamic_evaluator,
        basis_fingerprint=m6_basis.analysis_basis_fingerprint,
    )
    dynamic_result = propagate_uncertainty(
        dynamic_analysis,
        (EngineeringInputInterval("cooling_ratio", 0.9, 1.1),),
    )
    return (
        {"steady_plan": steady_analysis, "dynamic_plan": dynamic_analysis},
        {"steady_plan": steady_result, "dynamic_plan": dynamic_result},
    )


def _complete_result(
    basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> M6ValidationResult:
    scenarios = (
        _passed_scenario("steady_feed_increase", "M2_steady"),
        _passed_scenario("dynamic_feed_step", "M3_open_loop"),
        _passed_scenario("closed_loop_setpoint", "M4_closed_loop"),
        _limited_scenario(),
        _rejected_scenario(),
    )
    analyses, uncertainty = plan_evidence
    trace = _protection_trace()
    return M6ValidationResult(
        schema_version=M6_RESULT_SCHEMA_VERSION,
        status="success",
        basis=basis,
        validation_config_version="m6-validation-v0.1.0",
        validation_config_fingerprint="c" * 64,
        control_version="m4-control-v0.1.0",
        scenario_set_version="m6-scenarios-v0.1.0",
        required_scenario_ids=tuple(item.scenario_id for item in scenarios),
        scenarios=scenarios,
        required_plan_ids=("steady_plan", "dynamic_plan"),
        # Input mapping order is immaterial; the frozen result follows config order.
        sensitivity_analyses={
            "dynamic_plan": analyses["dynamic_plan"],
            "steady_plan": analyses["steady_plan"],
        },
        uncertainty_results={
            "dynamic_plan": uncertainty["dynamic_plan"],
            "steady_plan": uncertainty["steady_plan"],
        },
        plan_unquantified_sources={
            "steady_plan": ("parameter_correlation",),
            "dynamic_plan": ("field_dynamic_identification_gap",),
        },
        plan_source_origins={
            "steady_plan": (
                "M2_steady_model_prediction",
                "M6_synthetic_validation",
            ),
            "dynamic_plan": (
                "M3_open_loop_simulation",
                "M6_synthetic_validation",
            ),
        },
        required_protection_rule_ids=("high_temperature",),
        protection_traces={"high_temperature": trace},
        controller_tracking={
            "high_temperature.furnace_temperature": _tracking_evidence()
        },
        completion_checks={name: True for name in M6_COMPLETION_CHECK_IDS},
        source_composition=M6_SOURCE_COMPOSITION,
        metadata=M6_RESULT_METADATA,
        last_valid_scenario_ids=tuple(item.scenario_id for item in scenarios),
    )


def test_scenario_states_keep_execution_and_verification_semantics_separate() -> None:
    passed = _passed_scenario("normal_steady", "M2_steady")
    limited = _limited_scenario()
    rejected = _rejected_scenario()

    assert passed.scenario_status == "passed"
    assert limited.scenario_status == "limited"
    assert limited.verification_outcome == "passed"
    assert rejected.scenario_status == "rejected"
    assert rejected.verification_outcome == "passed"
    assert not rejected.solver_called
    assert rejected.metrics == {}
    assert rejected.protection_trace_summary is None


def test_structural_rejection_cannot_expose_numerical_or_solver_evidence() -> None:
    rejected = _rejected_scenario()

    with pytest.raises(ValueError, match="cannot expose numerical results"):
        replace(rejected, metrics={"invented_result": 1.0})
    with pytest.raises(ValueError, match="without a solver call"):
        replace(rejected, engine_status="success")
    with pytest.raises(ValueError, match="requires engine_status"):
        replace(rejected, solver_called=True)


def test_failed_scenario_requires_reason_and_cannot_claim_verification_pass() -> None:
    passed = _passed_scenario("direction_failure", "M2_steady")
    failed = replace(
        passed,
        scenario_status="failed",
        verification_outcome="failed",
        direction_checks={"expected_direction": False},
        engine_status="failed",
        failure_stage="direction_check",
        failure_reason="product response changed in the opposite direction",
    )
    assert failed.scenario_status == "failed"
    assert failed.metrics == passed.metrics

    with pytest.raises(ValueError, match="requires failure stage and reason"):
        replace(passed, scenario_status="failed", verification_outcome="failed")
    with pytest.raises(ValueError, match="verification_outcome differs"):
        replace(
            passed,
            direction_checks={"expected_direction": False},
            verification_outcome="passed",
        )


def test_protection_summary_and_scenario_serialization_are_deterministic() -> None:
    result = _limited_scenario()
    summary = result.protection_trace_summary
    assert summary is not None
    assert summary["event_count"] == 1
    assert summary["triggered_rule_ids"] == ["high_temperature"]
    assert summary["final_phases"] == {"high_temperature": "active"}
    assert result.protection_trace is not None
    serialized_trace = result.as_dict()["protection_trace"]
    assert serialized_trace == result.protection_trace.as_dict()
    assert isinstance(serialized_trace, dict)
    assert serialized_trace["events"][0]["event_kind"] == "triggered"
    assert result.as_dict() == result.as_dict()
    assert result.result_fingerprint == result.as_dict()["result_fingerprint"]
    json.dumps(result.as_dict(), sort_keys=True, allow_nan=False)

    with pytest.raises(TypeError):
        result.metrics["tracking_no_bump"] = 0.0  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.scenario_status = "failed"  # type: ignore[misc]


def test_complete_result_freezes_exact_scenario_and_two_plan_coverage(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)

    assert result.status == "success"
    assert result.completion_passed
    assert tuple(result.sensitivity_analyses) == ("steady_plan", "dynamic_plan")
    assert tuple(result.uncertainty_results) == ("steady_plan", "dynamic_plan")
    assert result.versions["base_parameter_set_version"] == (
        m6_basis.base_parameter_set_version
    )
    assert result.versions["derived_parameter_set_version"] == (
        m6_basis.derived_parameter_set_version
    )
    assert result.source_fingerprints["analysis_basis"] == (
        m6_basis.analysis_basis_fingerprint
    )
    assert result.as_dict() == result.as_dict()
    assert result.result_fingerprint == result.as_dict()["result_fingerprint"]
    json.dumps(result.as_dict(), sort_keys=True, allow_nan=False)

    with pytest.raises(TypeError):
        result.completion_checks["scenario_matrix"] = False  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]


def test_result_rejects_incomplete_scenario_or_plan_coverage(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)

    with pytest.raises(ValueError, match="required_scenario_ids"):
        replace(result, scenarios=result.scenarios[:-1])
    with pytest.raises(ValueError, match="sensitivity_analyses"):
        replace(
            result,
            sensitivity_analyses={
                "steady_plan": result.sensitivity_analyses["steady_plan"]
            },
        )
    with pytest.raises(ValueError, match="must contain steady and dynamic"):
        replace(result, required_plan_ids=("steady_plan",))


def test_result_rejects_cross_basis_or_cross_analysis_uncertainty_chains(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)

    def evaluator(inputs: Mapping[str, float]) -> Mapping[str, float]:
        return {"quality_proxy": inputs["foreign_input"]}

    foreign = run_local_sensitivity(
        (InputSensitivitySpec("foreign_input", 1.0, 0.01, 1.0),),
        (OutputSensitivitySpec("quality_proxy", 1.0),),
        evaluator,
        basis_fingerprint="f" * 64,
    )
    foreign_uncertainty = propagate_uncertainty(
        foreign,
        (EngineeringInputInterval("foreign_input", 0.9, 1.1),),
    )
    with pytest.raises(ValueError, match="uses another basis"):
        replace(
            result,
            sensitivity_analyses={
                "steady_plan": foreign,
                "dynamic_plan": result.sensitivity_analyses["dynamic_plan"],
            },
            uncertainty_results={
                "steady_plan": foreign_uncertainty,
                "dynamic_plan": result.uncertainty_results["dynamic_plan"],
            },
        )

    with pytest.raises(ValueError, match="uses another sensitivity analysis"):
        replace(
            result,
            uncertainty_results={
                "steady_plan": result.uncertainty_results["dynamic_plan"],
                "dynamic_plan": result.uncertainty_results["dynamic_plan"],
            },
        )


def test_top_level_success_requires_all_gates_and_no_failed_scenario(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)
    false_checks = dict(result.completion_checks)
    false_checks["deterministic_reproduction"] = False

    with pytest.raises(ValueError, match="must pass every evidence gate"):
        replace(result, completion_checks=false_checks)

    failed_result = replace(
        result,
        status="failed",
        completion_checks=false_checks,
        failure_stage="repeatability",
        failure_reason="second serialization differed",
        failure_time_s=0.0,
    )
    assert not failed_result.completion_passed
    assert failed_result.failure_stage == "repeatability"

    failed_scenario = replace(
        result.scenarios[0],
        scenario_status="failed",
        verification_outcome="failed",
        engine_status="failed",
        failure_stage="solver",
        failure_reason="non-converged",
    )
    scenarios = (failed_scenario, *result.scenarios[1:])
    with pytest.raises(ValueError, match="must pass every evidence gate"):
        replace(
            result,
            scenarios=scenarios,
            last_valid_scenario_ids=tuple(
                scenario.scenario_id for scenario in scenarios[1:]
            ),
        )


def test_protection_gate_requires_triggered_trace_and_passed_tracking(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)
    trace = result.protection_traces["high_temperature"]
    no_event = ProtectionTrace.initialize(trace.rules)
    with pytest.raises(ValueError, match="must pass every evidence gate"):
        replace(
            result,
            protection_traces={"high_temperature": no_event},
        )

    with pytest.raises(ValueError, match="must pass every evidence gate"):
        replace(
            result,
            controller_tracking={
                "high_temperature.furnace_temperature": _tracking_evidence(
                    passed=False
                )
            },
        )
    with pytest.raises(ValueError, match="exactly cover"):
        replace(result, controller_tracking={})


def test_plan_sources_and_failure_evidence_are_strict(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)
    with pytest.raises(ValueError, match="fixed model layer"):
        replace(
            result,
            plan_source_origins={
                "steady_plan": (
                    "M3_open_loop_simulation",
                    "M6_synthetic_validation",
                ),
                "dynamic_plan": (
                    "M2_steady_model_prediction",
                    "M6_synthetic_validation",
                ),
            },
        )

    checks = dict(result.completion_checks)
    checks["deterministic_reproduction"] = False
    failure_args: dict[str, object] = {
        "status": "failed",
        "completion_checks": checks,
        "failure_stage": "repeatability",
        "failure_reason": "serialization differed",
    }
    with pytest.raises(ValueError, match="requires failure_time_s"):
        replace(result, **failure_args)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite and non-negative"):
        replace(result, **failure_args, failure_time_s=-1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="last_valid_scenario_ids"):
        replace(
            result,
            **failure_args,  # type: ignore[arg-type]
            failure_time_s=0.0,
            last_valid_scenario_ids=result.last_valid_scenario_ids[:-1],
        )


def test_source_and_claim_contracts_cannot_be_diluted(
    m6_basis: M6Basis,
    plan_evidence: PlanEvidence,
) -> None:
    result = _complete_result(m6_basis, plan_evidence)
    metadata = dict(result.metadata)
    metadata["claim_scope"] = "field_validated"
    with pytest.raises(ValueError, match="synthetic claim contract"):
        replace(result, metadata=metadata)

    sources = dict(result.source_composition)
    sources.pop("source_traced_field_observation_catalog")
    with pytest.raises(ValueError, match="mixed-source contract"):
        replace(result, source_composition=sources)
