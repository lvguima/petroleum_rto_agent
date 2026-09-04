from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from petroleum_rto import rto
from petroleum_rto.rto import runtime
from petroleum_rto.rto.capabilities import load_capability_bundle
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.intent import load_optimization_intent
from petroleum_rto.rto.orchestration.result import OptimizationRunReceipt
from petroleum_rto.rto.problem import ProblemBuilder
from petroleum_rto.rto.runtime import api, cli


@pytest.mark.parametrize(
    ("intent_name", "objective_count"),
    [
        ("minimize_specific_furnace_energy.json", 1),
        ("quality_yield_energy.json", 3),
    ],
)
def test_single_and_multi_intents_share_one_validation_path(
    repo_root: Path,
    intent_name: str,
    objective_count: int,
) -> None:
    intent_file = repo_root / "configs/rto/intents" / intent_name
    context_file = repo_root / "configs/rto/contexts/case_20260604.json"
    resolution = api.validate_intent_file(repo_root=repo_root, intent_file=intent_file)
    problem = api.validate_problem_files(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )
    assert resolution.status == "resolved"
    assert resolution.resolved_intent is not None
    assert len(resolution.resolved_intent.objectives) == objective_count
    assert len(problem.objectives) == objective_count
    assert problem.context_ref == load_operating_context(context_file).ref


def test_capabilities_and_problem_cli_do_not_call_a_solver(
    repo_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["capabilities", "--repo-root", str(repo_root)]) == 0
    capability_output = json.loads(capsys.readouterr().out)
    assert capability_output["manifest_id"] == "cdu-rto-public-capabilities"
    assert capability_output["solver_called"] is False

    assert (
        cli.main(
            [
                "validate-problem",
                "--repo-root",
                str(repo_root),
                "--intent-file",
                str(repo_root / "configs/rto/intents/quality_yield_energy.json"),
                "--context-file",
                str(repo_root / "configs/rto/contexts/case_20260604.json"),
            ]
        )
        == 0
    )
    problem_output = json.loads(capsys.readouterr().out)
    assert problem_output["status"] == "valid"
    assert len(problem_output["objectives"]) == 3
    assert problem_output["execution_route_ref"]["object_id"] == "multiobjective-pareto-route"
    assert problem_output["solver_called"] is False


def test_cli_exposes_no_version_or_legacy_commands() -> None:
    parser = cli._parser()
    for command in ("legacy-run-v1", "legacy-run-v2", "legacy-inspect-v1"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])


def test_public_surfaces_expose_only_current_neutral_names() -> None:
    assert runtime.run_offline is api.run_offline
    assert runtime.run_confirmed_optimization is api.run_confirmed_optimization
    assert runtime.inspect_offline is api.inspect_offline
    assert "run_confirmed_optimization" in api.__all__
    assert "prepare_optimization_preview" not in api.__all__
    assert not hasattr(runtime, "prepare_optimization_preview")
    assert not any("legacy" in name.lower() for name in runtime.__all__)
    assert not any(name.endswith(("V1", "V2")) for name in rto.__all__)
    assert not any(name.startswith("Unified") for name in rto.__all__)


def test_confirmed_run_builds_once_from_latest_context_and_returns_memory_summary(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_capability_bundle(repo_root)
    intent = load_optimization_intent(
        repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    )
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    problem = ProblemBuilder().build(bundle, intent, context)
    build_calls: list[tuple[object, ...]] = []
    received: list[object] = []
    resolution_inputs: list[tuple[object, object]] = []
    context_loads = 0
    resolve_intent = False
    summary = runtime.OptimizationRunSummary(
        status="success",
        targets=(
            runtime.OptimizationTargetSummary(
                metric_id="specific_furnace_energy",
                business_name="单位原油炉热负荷",
                sense="minimize",
                priority=1,
                unit="J/kg",
            ),
        ),
        operating_context=runtime.OptimizationContextSummary(
            operating_mode="steady_crude_distillation",
            fresh_feed_load_kg_s=100.0,
            fresh_feed_load_t_per_h=360.0,
            data_timestamp="2026-06-04T08:00:00Z",
            data_quality="trusted_synthetic_fixture",
        ),
        baseline_values=(
            runtime.OptimizationBaselineSummary(
                metric_id="specific_furnace_energy",
                value=1.0,
                unit="J/kg",
            ),
        ),
        recommended_adjustments=(
            runtime.OptimizationAdjustmentSummary(
                variable_id="furnace_outlet_temperature_k",
                business_name="常压炉出口温度",
                unit="K",
                baseline_value=640.0,
                recommended_value=638.0,
                adjustment=-2.0,
            ),
        ),
        predicted_effects=(
            runtime.OptimizationPredictedEffectSummary(
                metric_id="specific_furnace_energy",
                predicted_value=0.9,
                unit="J/kg",
                directional_improvement=0.1,
                relative_improvement=0.1,
            ),
        ),
        alternative_candidates=(),
    )

    class _Builder:
        def build(self, *args: object) -> object:
            build_calls.append(args)
            return problem

    class _Orchestrator:
        def __init__(self, *args: object) -> None:
            pass

        def run_compact(self, *args: object, **kwargs: object) -> object:
            received.extend(args)
            return OptimizationRunReceipt(
                workflow_id="offline-rto-0123456789abcdef",
                result_source="offline-rto-0123456789abcdef/result.json",
                result_summary=summary.as_dict(),
            )

    class _Resolution:
        @property
        def status(self) -> str:
            return "resolved" if resolve_intent else "unsupported"

        @property
        def resolved_intent(self) -> object | None:
            return intent if resolve_intent else None

    class _Resolver:
        def resolve(self, *args: object) -> _Resolution:
            resolution_inputs.append((args[0], args[1]))
            return _Resolution()

    def load_context(_: Path) -> object:
        nonlocal context_loads
        context_loads += 1
        return context

    monkeypatch.setattr(api, "load_capability_bundle", lambda _: bundle)
    monkeypatch.setattr(api, "load_operating_context", load_context)
    monkeypatch.setattr(api, "ProblemBuilder", _Builder)
    monkeypatch.setattr(api, "IntentResolver", _Resolver)
    monkeypatch.setattr(api, "OfflineRtoOrchestrator", _Orchestrator)

    monkeypatch.setattr(
        api,
        "inspect_offline",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("confirmed run must not reread the persisted result")
        ),
    )

    with pytest.raises(ValueError, match="unsupported under the current capabilities"):
        api.run_confirmed_optimization(
            repo_root=repo_root,
            intent=intent,
            context_file=tmp_path / "context.json",
            run_root=tmp_path / "runs",
        )
    assert context_loads == 0
    assert build_calls == []
    assert received == []

    resolve_intent = True
    result = api.run_confirmed_optimization(
        repo_root=repo_root,
        intent=intent,
        context_file=tmp_path / "context.json",
        run_root=tmp_path / "runs",
    )

    assert context_loads == 1
    assert len(resolution_inputs) == 2
    assert all(resolved[0] is intent for resolved in resolution_inputs)
    assert len(build_calls) == 1
    assert build_calls[0] == (bundle, intent, context)
    assert received[2] is context
    assert received[3] is problem
    assert result == {
        "workflow_id": "offline-rto-0123456789abcdef",
        "result_source": "offline-rto-0123456789abcdef/result.json",
        "result_summary": summary.as_dict(),
    }


def test_optimization_run_receipt_is_frozen_and_validates_its_locator() -> None:
    targets: list[object] = []
    summary = {
        "status": "success",
        "targets": targets,
        "operating_context": {},
        "baseline_values": [],
        "recommended_adjustments": [],
        "predicted_effects": [],
        "alternative_candidates": [],
    }
    receipt = OptimizationRunReceipt(
        workflow_id="offline-rto-0123456789abcdef",
        result_source="offline-rto-0123456789abcdef/result.json",
        result_summary=summary,
    )
    summary["status"] = "changed"
    targets.append({"metric_id": "changed"})

    assert receipt.result_summary["targets"] == ()
    assert receipt.as_dict()["result_summary"] == {
        "status": "success",
        "targets": [],
        "operating_context": {},
        "baseline_values": [],
        "recommended_adjustments": [],
        "predicted_effects": [],
        "alternative_candidates": [],
    }
    with pytest.raises(FrozenInstanceError):
        receipt.workflow_id = "offline-rto-fedcba9876543210"  # type: ignore[misc]
    with pytest.raises(ValueError, match="workflow format"):
        OptimizationRunReceipt(
            workflow_id="../offline-rto-0123456789abcdef",
            result_source="../offline-rto-0123456789abcdef/result.json",
            result_summary=summary,
        )
    with pytest.raises(ValueError, match="workflow-relative"):
        OptimizationRunReceipt(
            workflow_id="offline-rto-0123456789abcdef",
            result_source="offline-rto-fedcba9876543210/result.json",
            result_summary=summary,
        )
    legacy_summary = dict(summary)
    legacy_summary.pop("alternative_candidates")
    with pytest.raises(ValueError, match="compact result contract"):
        OptimizationRunReceipt(
            workflow_id="offline-rto-0123456789abcdef",
            result_source="offline-rto-0123456789abcdef/result.json",
            result_summary=legacy_summary,
        )


def test_run_path_builds_once_and_passes_the_same_immutable_problem(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_capability_bundle(repo_root)
    intent_file = repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    context_file = repo_root / "configs/rto/contexts/case_20260604.json"
    intent = load_optimization_intent(intent_file)
    context = load_operating_context(context_file)
    problem = ProblemBuilder().build(bundle, intent, context)
    build_calls: list[tuple[object, ...]] = []
    received: list[object] = []
    sentinel = object()

    class _Builder:
        def build(self, *args: object) -> object:
            build_calls.append(args)
            return problem

    class _Orchestrator:
        def __init__(self, *args: object) -> None:
            pass

        def run(self, *args: object, **kwargs: object) -> object:
            received.extend(args)
            return sentinel

    monkeypatch.setattr(api, "load_capability_bundle", lambda _: bundle)
    monkeypatch.setattr(api, "load_optimization_intent", lambda _: intent)
    monkeypatch.setattr(api, "load_operating_context", lambda _: context)
    monkeypatch.setattr(api, "ProblemBuilder", _Builder)
    monkeypatch.setattr(api, "OfflineRtoOrchestrator", _Orchestrator)

    result = api.run_offline(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
        run_root=tmp_path / "runs",
    )

    assert result is sentinel
    assert len(build_calls) == 1
    assert received[3] is problem


def test_inspection_wraps_strict_reader_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def damaged_reader(*args: object, **kwargs: object) -> object:
        raise ValueError("fingerprint mismatch")

    monkeypatch.setattr(api, "read_offline_run", damaged_reader)
    with pytest.raises(api.OfflineInspectionError, match="fingerprint mismatch"):
        api.inspect_offline(tmp_path / "run")
