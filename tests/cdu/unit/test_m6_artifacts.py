from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Literal, cast

import pytest

from petroleum_rto.cdu.core.config import canonical_fingerprint
from petroleum_rto.cdu.repository import resolve_cdu_repository_path
from petroleum_rto.cdu.validation import artifacts as artifacts_module
from petroleum_rto.cdu.validation.artifacts import (
    M6_ARTIFACT_PATHS,
    M6ArtifactManifest,
    m6_failure_payload,
    verify_m6_artifacts,
    write_m6_artifacts,
)
from petroleum_rto.cdu.validation.basis import M6Basis, load_m6_basis
from petroleum_rto.cdu.validation.config import (
    M6ValidationConfig,
    UncertaintyPlan,
    ValidationScenarioSpec,
    load_m6_validation_config,
)
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
    ExecutionLayer,
    M6ValidationResult,
    ScenarioValidationResult,
)
from petroleum_rto.cdu.validation.tracking import ControllerTrackingEvidence
from petroleum_rto.cdu.validation.uncertainty import (
    LocalSensitivityAnalysis,
    UncertaintyPropagationResult,
    propagate_uncertainty,
    run_local_sensitivity,
)

type ScenarioDomainStatus = Literal["passed", "limited", "rejected"]
type PlanEvidence = tuple[
    Mapping[str, LocalSensitivityAnalysis],
    Mapping[str, UncertaintyPropagationResult],
]


def _json_object(path: Path) -> dict[str, object]:
    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.fixture(scope="module")
def m6_basis(repo_root: Path) -> M6Basis:
    return load_m6_basis(repo_root)


@pytest.fixture(scope="module")
def m6_config(repo_root: Path) -> M6ValidationConfig:
    return load_m6_validation_config(repo_root / "configs/cdu/validation/m6_validation_v0.1.0.json")


@pytest.fixture(autouse=True)
def frozen_source_loaders(
    monkeypatch: pytest.MonkeyPatch,
    m6_basis: M6Basis,
    m6_config: M6ValidationConfig,
) -> None:
    monkeypatch.setattr(
        artifacts_module,
        "load_m6_validation_config",
        lambda path: m6_config,
    )
    monkeypatch.setattr(
        artifacts_module,
        "load_m6_basis",
        lambda root: m6_basis,
    )


def _domain(status: ScenarioDomainStatus) -> ApplicabilityAssessment:
    representation: DomainRepresentation = "unsupported" if status == "rejected" else "direct"
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


def _passed_scenario(
    scenario_id: str,
    execution_layer: ExecutionLayer = "M2_steady",
) -> ScenarioValidationResult:
    source = {
        "M2_steady": "M2_steady_model_prediction",
        "M3_open_loop": "M3_open_loop_simulation",
        "M4_closed_loop": "M4_closed_loop_simulation",
        "M6_supervisory": "M6_synthetic_validation",
        "structural_rejection": "M6_synthetic_validation",
    }[execution_layer]
    domain = _domain("passed")
    return ScenarioValidationResult(
        scenario_id=scenario_id,
        scenario_version=f"{scenario_id}-v0.1.0",
        claim_ids=(f"claim.{scenario_id}",),
        purpose=f"Verify {scenario_id}.",
        execution_layer=execution_layer,
        scenario_status="passed",
        expected_status="passed",
        verification_outcome="passed",
        solver_called=True,
        domain=domain,
        metrics={"finite_output": 1.0},
        direction_checks={"expected_direction": True},
        conservation_checks={"mass_conservation": True},
        protection_trace=None,
        source_origins=(source, "M6_synthetic_validation"),
        engine_status="success",
        input_fingerprint=domain.input_fingerprint,
    )


def _limited_scenario() -> ScenarioValidationResult:
    domain = _domain("limited")
    return ScenarioValidationResult(
        scenario_id="synthetic_protection_trip",
        scenario_version="synthetic-protection-trip-v0.1.0",
        claim_ids=("claim.synthetic_protection",),
        purpose="Verify a synthetic protection trip.",
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


def _rejected_scenario() -> ScenarioValidationResult:
    domain = _domain("rejected")
    return ScenarioValidationResult(
        scenario_id="stripping_steam_request",
        scenario_version="stripping-steam-request-v0.1.0",
        claim_ids=("claim.structural_rejection",),
        purpose="Verify structural rejection.",
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
def plan_evidence(
    m6_basis: M6Basis,
    m6_config: M6ValidationConfig,
) -> PlanEvidence:
    def build_plan(
        plan: UncertaintyPlan,
    ) -> tuple[LocalSensitivityAnalysis, UncertaintyPropagationResult]:
        def evaluator(inputs: Mapping[str, float]) -> Mapping[str, float]:
            weighted = sum(
                (index + 1.0) * inputs[item.input_id] for index, item in enumerate(plan.inputs)
            )
            return {
                item.output_id: (index + 1.0) * weighted for index, item in enumerate(plan.outputs)
            }

        analysis = run_local_sensitivity(
            plan.inputs,
            plan.outputs,
            evaluator,
            basis_fingerprint=m6_basis.analysis_basis_fingerprint,
        )
        uncertainty = propagate_uncertainty(analysis, plan.intervals)
        return analysis, uncertainty

    steady_analysis, steady_uncertainty = build_plan(m6_config.steady_uncertainty)
    dynamic_analysis, dynamic_uncertainty = build_plan(m6_config.dynamic_uncertainty)
    return (
        {
            m6_config.steady_uncertainty.plan_id: steady_analysis,
            m6_config.dynamic_uncertainty.plan_id: dynamic_analysis,
        },
        {
            m6_config.steady_uncertainty.plan_id: steady_uncertainty,
            m6_config.dynamic_uncertainty.plan_id: dynamic_uncertainty,
        },
    )


def _scenario_from_spec(
    config: M6ValidationConfig,
    spec: ValidationScenarioSpec,
) -> ScenarioValidationResult:
    domain = assess_applicability(
        config.domain_dimensions,
        spec.inputs,
        abnormal_verification=spec.abnormal_verification,
    )
    rejected = spec.expected_status == "rejected"
    layer_source = {
        "M2_steady": "M2_steady_model_prediction",
        "M3_open_loop": "M3_open_loop_simulation",
        "M4_closed_loop": "M4_closed_loop_simulation",
        "M6_supervision": "M6_synthetic_validation",
        "structural_rejection": "M6_synthetic_validation",
    }[spec.execution_layer]
    origins = (
        ("M6_synthetic_validation",)
        if layer_source == "M6_synthetic_validation"
        else (layer_source, "M6_synthetic_validation")
    )
    return ScenarioValidationResult(
        scenario_id=spec.scenario_id,
        scenario_version=spec.scenario_version,
        claim_ids=spec.claim_ids,
        purpose=spec.purpose,
        execution_layer=(
            "M6_supervisory" if spec.execution_layer == "M6_supervision" else spec.execution_layer
        ),
        scenario_status=spec.expected_status,
        expected_status=spec.expected_status,
        verification_outcome="passed",
        solver_called=not rejected,
        domain=domain,
        metrics={} if rejected else {"finite_output": 1.0},
        direction_checks={} if rejected else {"expected_behavior": True},
        conservation_checks={} if rejected else {"mass_conservation": True},
        protection_trace=None,
        source_origins=origins,
        engine_status=None if rejected else "success",
        input_fingerprint=domain.input_fingerprint,
    )


def _trace_for_rule(rule: ProtectionRule) -> ProtectionTrace:
    if rule.condition == "invalid":
        observed = 0.0
        valid = False
    elif rule.condition == "high":
        assert rule.trip_threshold is not None
        observed = rule.trip_threshold + max(1.0, abs(rule.trip_threshold) * 0.01)
        valid = True
    else:
        assert rule.trip_threshold is not None
        observed = rule.trip_threshold - max(1.0, abs(rule.trip_threshold) * 0.01)
        valid = True
    frames = [
        ProtectionFrame(
            0.0,
            {rule.signal_name: observed},
            {rule.signal_name: valid},
        )
    ]
    if rule.trigger_delay_s > 0.0:
        frames.append(
            ProtectionFrame(
                rule.trigger_delay_s,
                {rule.signal_name: observed},
                {rule.signal_name: valid},
            )
        )
    trace = run_protection((rule,), tuple(frames))
    assert any(event.event_kind == "triggered" for event in trace.events)
    return trace


def _tracking_evidence(loop_id: str) -> ControllerTrackingEvidence:
    payload: dict[str, object] = {
        "loop_id": loop_id,
        "initial_output": 1.0,
        "protected_output": 1.0,
        "final_manual_output": 1.0,
        "return_automatic_output": 1.0,
        "manual_steps": 1,
        "maximum_manual_output_change": 0.0,
        "final_tracking_error": 0.0,
        "automatic_return_jump": 0.0,
        "tolerance": 1e-6,
        "passed": True,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return ControllerTrackingEvidence(
        loop_id=loop_id,
        initial_output=1.0,
        protected_output=1.0,
        final_manual_output=1.0,
        return_automatic_output=1.0,
        manual_steps=1,
        maximum_manual_output_change=0.0,
        final_tracking_error=0.0,
        automatic_return_jump=0.0,
        tolerance=1e-6,
        passed=True,
        evidence_fingerprint=fingerprint,
    )


@pytest.fixture(scope="module")
def successful_result(
    m6_basis: M6Basis,
    m6_config: M6ValidationConfig,
    plan_evidence: PlanEvidence,
) -> M6ValidationResult:
    scenarios = tuple(_scenario_from_spec(m6_config, spec) for spec in m6_config.scenarios)
    traces = {rule.rule_id: _trace_for_rule(rule) for rule in m6_config.protection_rules}
    tracking = {
        f"{rule.rule_id}.{loop_id}": _tracking_evidence(loop_id)
        for rule in m6_config.protection_rules
        for loop_id in rule.action.manual_tracking_loop_ids
    }
    analyses, uncertainty = plan_evidence
    plan_ids = (
        m6_config.steady_uncertainty.plan_id,
        m6_config.dynamic_uncertainty.plan_id,
    )
    return M6ValidationResult(
        schema_version=M6_RESULT_SCHEMA_VERSION,
        status="success",
        basis=m6_basis,
        validation_config_version=m6_config.validation_version,
        validation_config_fingerprint=m6_config.input_fingerprint,
        control_version=m6_config.control_version,
        scenario_set_version="m6-scenarios-v0.1.0",
        required_scenario_ids=tuple(item.scenario_id for item in scenarios),
        scenarios=scenarios,
        required_plan_ids=plan_ids,
        sensitivity_analyses=analyses,
        uncertainty_results=uncertainty,
        plan_unquantified_sources={
            m6_config.steady_uncertainty.plan_id: (
                m6_config.steady_uncertainty.unquantified_sources
            ),
            m6_config.dynamic_uncertainty.plan_id: (
                m6_config.dynamic_uncertainty.unquantified_sources
            ),
        },
        plan_source_origins={
            m6_config.steady_uncertainty.plan_id: (
                "M2_steady_model_prediction",
                "M6_synthetic_validation",
            ),
            m6_config.dynamic_uncertainty.plan_id: (
                "M3_open_loop_simulation",
                "M6_synthetic_validation",
            ),
        },
        required_protection_rule_ids=tuple(traces),
        protection_traces=traces,
        controller_tracking=tracking,
        completion_checks={name: True for name in M6_COMPLETION_CHECK_IDS},
        source_composition=M6_SOURCE_COMPOSITION,
        metadata=M6_RESULT_METADATA,
        last_valid_scenario_ids=tuple(item.scenario_id for item in scenarios),
    )


def _failed_result(result: M6ValidationResult) -> M6ValidationResult:
    checks = dict(result.completion_checks)
    checks["deterministic_reproduction"] = False
    return replace(
        result,
        status="failed",
        completion_checks=checks,
        failure_stage="repeatability",
        failure_reason="the repeated serialization differed",
        failure_time_s=0.0,
    )


def test_writer_publishes_the_fixed_auditable_four_file_suite(
    successful_result: M6ValidationResult,
    tmp_path: Path,
) -> None:
    published = write_m6_artifacts(successful_result, tmp_path)

    assert dict(published.paths) == dict(M6_ARTIFACT_PATHS)
    assert published.result_fingerprint == successful_result.result_fingerprint
    for name, relative_path in published.paths.items():
        path = resolve_cdu_repository_path(tmp_path, relative_path)
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == published.sha256[name]
        assert len(data) == published.size_bytes[name]

    evidence = _json_object(
        resolve_cdu_repository_path(tmp_path, published.paths["validation_evidence"])
    )
    report = _json_object(resolve_cdu_repository_path(tmp_path, published.paths["report_json"]))
    manifest = _json_object(
        resolve_cdu_repository_path(tmp_path, published.paths["artifact_manifest"])
    )
    markdown = (
        resolve_cdu_repository_path(tmp_path, published.paths["report_markdown"])
    ).read_text(encoding="utf-8")
    assert evidence == successful_result.as_dict()
    assert _mapping(report["scenario_summary"])["total"] == 22
    assert set(_mapping(report["plans"])) == set(successful_result.required_plan_ids)
    assert _mapping(report["protection_traces"]) == evidence["protection_traces"]
    assert _mapping(report["controller_tracking"]) == evidence["controller_tracking"]
    limitations = cast(list[dict[str, object]], report["limitations"])
    assert {item["limitation_id"] for item in limitations} >= {
        "engineering_validation_only",
        "local_first_order_envelope",
        "synthetic_protection_not_sis",
        "structurally_rejected_scenarios",
    }
    assert "不构成现场动态精度" in markdown
    assert "## 场景矩阵" in markdown
    assert "## 灵敏度与不确定度" in markdown
    assert "## 保护事件与控制器跟踪" in markdown

    assert manifest["status"] == "valid"
    assert manifest["validation_status"] == "success"
    assert manifest["result_fingerprint"] == successful_result.result_fingerprint
    manifest_artifacts = _mapping(manifest["artifacts"])
    assert set(manifest_artifacts) == set(M6_ARTIFACT_PATHS) - {"artifact_manifest"}
    for name, raw_entry in manifest_artifacts.items():
        entry = _mapping(raw_entry)
        assert entry["path"] == published.paths[name]
        assert entry["sha256"] == published.sha256[name]
        assert entry["bytes"] == published.size_bytes[name]
    unsigned_manifest = dict(manifest)
    fingerprint = cast(str, unsigned_manifest.pop("manifest_fingerprint"))
    assert canonical_fingerprint(unsigned_manifest) == fingerprint
    assert (
        verify_m6_artifacts(
            tmp_path,
            expected_result_fingerprint=successful_result.result_fingerprint,
        )
        == published
    )


def test_repeated_publication_is_byte_identical_and_contains_no_clock_time(
    successful_result: M6ValidationResult,
    tmp_path: Path,
) -> None:
    first = write_m6_artifacts(successful_result, tmp_path)
    first_bytes = {
        name: (resolve_cdu_repository_path(tmp_path, relative_path)).read_bytes()
        for name, relative_path in first.paths.items()
    }
    second = write_m6_artifacts(successful_result, tmp_path)
    second_bytes = {
        name: (resolve_cdu_repository_path(tmp_path, relative_path)).read_bytes()
        for name, relative_path in second.paths.items()
    }

    assert first == second
    assert first.as_dict() == second.as_dict()
    assert first_bytes == second_bytes
    manifest = _json_object(
        resolve_cdu_repository_path(tmp_path, second.paths["artifact_manifest"])
    )
    assert "generated_at" not in manifest
    assert "created_at" not in manifest
    assert "timestamp" not in manifest


def test_failed_result_only_builds_an_in_memory_failure_payload(
    successful_result: M6ValidationResult,
    tmp_path: Path,
) -> None:
    failed = _failed_result(successful_result)

    with pytest.raises(ValueError, match="only a complete successful"):
        write_m6_artifacts(failed, tmp_path)
    assert not any(path.is_file() for path in tmp_path.rglob("*"))

    payload = m6_failure_payload(failed)
    assert payload["status"] == "failed"
    assert payload["valid_artifact_suite"] is False
    assert payload["failure_stage"] == "repeatability"
    assert payload["failure_time_s"] == 0.0
    assert payload["last_valid_scenario_ids"] == list(failed.last_valid_scenario_ids)
    assert set(_mapping(payload["last_valid_evidence"])) == set(failed.last_valid_scenario_ids)
    unsigned = dict(payload)
    fingerprint = cast(str, unsigned.pop("failure_fingerprint"))
    assert canonical_fingerprint(unsigned) == fingerprint
    with pytest.raises(ValueError, match="requires an incomplete failed"):
        m6_failure_payload(successful_result)


def test_staged_validation_failure_leaves_no_formal_or_temporary_files(
    successful_result: M6ValidationResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_staged_bundle(staged: Mapping[str, Path]) -> None:
        del staged
        raise RuntimeError("injected staged verification failure")

    monkeypatch.setattr(
        artifacts_module,
        "_validate_staged_bundle",
        reject_staged_bundle,
    )
    with pytest.raises(RuntimeError, match="injected staged verification failure"):
        write_m6_artifacts(successful_result, tmp_path)

    for relative_path in M6_ARTIFACT_PATHS.values():
        assert not (resolve_cdu_repository_path(tmp_path, relative_path)).exists()
    assert not any(tmp_path.rglob("*.stage"))
    assert not any(tmp_path.rglob("*.backup"))


def test_mid_publish_failure_restores_every_prior_file_and_manifest(
    successful_result: M6ValidationResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = {
        name: resolve_cdu_repository_path(tmp_path, relative_path)
        for name, relative_path in M6_ARTIFACT_PATHS.items()
    }
    previous: dict[str, bytes] = {}
    for name, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        previous[name] = f"previous-{name}\n".encode()
        target.write_bytes(previous[name])
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
            raise OSError("injected M6 report publication failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_report_publication)
    with pytest.raises(OSError, match="injected M6 report publication failure"):
        write_m6_artifacts(successful_result, tmp_path)

    assert failure_injected
    assert {name: target.read_bytes() for name, target in targets.items()} == previous
    assert not any(tmp_path.rglob("*.stage"))
    assert not any(tmp_path.rglob("*.backup"))


def test_manifest_is_the_last_staged_file_published(
    successful_result: M6ValidationResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = Path.replace
    published_targets: list[Path] = []

    def observe_publication(self: Path, target: str | Path) -> Path:
        if self.name.endswith(".stage"):
            published_targets.append(Path(target).resolve())
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", observe_publication)
    write_m6_artifacts(successful_result, tmp_path)

    assert published_targets == [
        (resolve_cdu_repository_path(tmp_path, M6_ARTIFACT_PATHS[name])).resolve()
        for name in (
            "validation_evidence",
            "report_json",
            "report_markdown",
            "artifact_manifest",
        )
    ]


def test_verifier_rejects_tampered_markdown_hash_or_wrong_expected_result(
    successful_result: M6ValidationResult,
    tmp_path: Path,
) -> None:
    published = write_m6_artifacts(successful_result, tmp_path)
    with pytest.raises(ValueError, match="differs from expected"):
        verify_m6_artifacts(tmp_path, expected_result_fingerprint="f" * 64)

    markdown_path = resolve_cdu_repository_path(tmp_path, published.paths["report_markdown"])
    markdown_path.write_bytes(markdown_path.read_bytes() + b"tampered\n")
    with pytest.raises(ValueError, match="hash/bytes mismatch for report_markdown"):
        verify_m6_artifacts(tmp_path)


def test_manifest_contract_is_deeply_frozen_and_paths_are_not_configurable(
    successful_result: M6ValidationResult,
    tmp_path: Path,
) -> None:
    published = write_m6_artifacts(successful_result, tmp_path)
    assert tuple(published.paths) == (
        "validation_evidence",
        "report_json",
        "report_markdown",
        "artifact_manifest",
    )
    with pytest.raises(TypeError):
        published.paths["validation_evidence"] = "elsewhere.json"  # type: ignore[index]
    with pytest.raises(TypeError):
        published.sha256["validation_evidence"] = "0" * 64  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        published.result_fingerprint = "0" * 64  # type: ignore[misc]
    with pytest.raises(ValueError, match="existing directory"):
        verify_m6_artifacts(tmp_path / "missing")

    with pytest.raises(ValueError, match="fixed formal suite"):
        M6ArtifactManifest(
            paths={"validation_evidence": "elsewhere.json"},
            sha256=published.sha256,
            size_bytes=published.size_bytes,
            result_fingerprint=published.result_fingerprint,
            manifest_fingerprint=published.manifest_fingerprint,
        )


def test_writer_rejects_wrong_config_fingerprint_and_three_scenario_suite(
    successful_result: M6ValidationResult,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="fingerprint differs from frozen config"):
        write_m6_artifacts(
            replace(
                successful_result,
                validation_config_fingerprint="c" * 64,
            ),
            tmp_path,
        )

    scenarios = successful_result.scenarios[:3]
    fake_three = replace(
        successful_result,
        required_scenario_ids=tuple(item.scenario_id for item in scenarios),
        scenarios=scenarios,
        last_valid_scenario_ids=tuple(item.scenario_id for item in scenarios),
    )
    assert fake_three.completion_passed
    with pytest.raises(ValueError, match="scenarios differ from frozen config"):
        write_m6_artifacts(fake_three, tmp_path)
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_verifier_reloads_the_source_verified_basis(
    successful_result: M6ValidationResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_m6_artifacts(successful_result, tmp_path)

    def reject_source_drift(root: Path) -> M6Basis:
        del root
        raise ValueError("injected M5 source drift")

    monkeypatch.setattr(artifacts_module, "load_m6_basis", reject_source_drift)
    with pytest.raises(ValueError, match="injected M5 source drift"):
        verify_m6_artifacts(tmp_path)
