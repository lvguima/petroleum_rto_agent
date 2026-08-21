from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import pytest

from petroleum_rto.rto._file_lock import exclusive_file_lock
from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.capabilities import load_capability_bundle
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.common import canonical_fingerprint, canonical_json_bytes
from petroleum_rto.rto.contracts.models import CLAIM_SCOPE, RTO_SCHEMA_VERSION
from petroleum_rto.rto.contracts.simulation import (
    SimulationEvaluationRequest,
    SimulationPreview,
    SimulationRunBundle,
)
from petroleum_rto.rto.orchestration.unified_models import WorkflowEvent
from petroleum_rto.rto.orchestration.unified_service import (
    OfflineRtoOrchestrator,
    OfflineRtoRunRecord,
    _offline_result,
    _replay_evaluations,
    _validate_top_level_entries,
    read_offline_run,
)
from petroleum_rto.rto.runtime import api as runtime_api
from petroleum_rto.rto.runtime import build_chat_result_summary
from petroleum_rto.rto.strategies.unified import StrategyRepository
from petroleum_rto.rto.unified_inputs import load_optimization_intent


class _PersistedSimulator:
    def __init__(
        self,
        output_root: Path,
        context_model_fingerprint: str,
        context_case_fingerprint: str,
        make_bundle: Callable[..., SimulationRunBundle],
    ) -> None:
        self._output_root = output_root
        self._model = context_model_fingerprint
        self._case = context_case_fingerprint
        self._make_bundle = make_bundle
        self.evaluate_calls = 0

    def preview(self, request: SimulationEvaluationRequest) -> SimulationPreview:
        return SimulationPreview(
            schema_version=RTO_SCHEMA_VERSION,
            preview_version="workflow-fake-preview",
            simulation_request_ref=request.ref,
            provider_id=request.provider_id,
            provider_preview_fingerprint=request.fingerprint,
            effective_input_fingerprint=request.provider_request_fingerprint,
            base_object_fingerprints={"model": self._model, "case": self._case},
            effective_object_fingerprints={},
            claim_scope=CLAIM_SCOPE,
        )

    def evaluate(
        self,
        request: SimulationEvaluationRequest,
        expected_preview_fingerprint: str,
    ) -> SimulationRunBundle:
        if expected_preview_fingerprint != request.fingerprint:
            raise ValueError("preview fingerprint differs")
        self.evaluate_calls += 1
        run_dir = self._output_root / f"fake-{self.evaluate_calls:04d}"
        run_dir.mkdir(parents=True, exist_ok=False)
        if request.stage == "M2":
            bundle = self._make_bundle(
                request.provider_request_fingerprint,
                stage="M2",
                objective=188.0 if request.pair_role == "baseline" else 180.0,
                quality_scale=1.0,
                yield_delta=0.0,
            )
        else:
            bundle = self._make_bundle(
                request.provider_request_fingerprint,
                stage="M4",
                accepted=True,
            )
        bundle = replace(bundle, run_ref=str(run_dir.resolve()))
        (run_dir / "bundle.json").write_text(
            json.dumps(bundle.as_dict(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return bundle

    def read_evidence(self, run_ref: Path) -> SimulationRunBundle:
        raw = json.loads((run_ref / "bundle.json").read_text(encoding="utf-8"))
        return SimulationRunBundle.from_mapping(cast(dict[str, object], raw))


class _InterruptingSimulator(_PersistedSimulator):
    def __init__(
        self,
        output_root: Path,
        context_model_fingerprint: str,
        context_case_fingerprint: str,
        make_bundle: Callable[..., SimulationRunBundle],
        *,
        fail_stage: Literal["M2", "M4"],
    ) -> None:
        super().__init__(
            output_root,
            context_model_fingerprint,
            context_case_fingerprint,
            make_bundle,
        )
        self._fail_stage = fail_stage

    def preview(self, request: SimulationEvaluationRequest) -> SimulationPreview:
        if request.stage == self._fail_stage:
            raise OSError(f"synthetic {self._fail_stage} infrastructure interruption")
        return super().preview(request)


def _run(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    *,
    multi: bool,
    coverage_policy: Literal["point", "sampled-anchors"],
) -> tuple[
    OfflineRtoRunRecord,
    OfflineRtoOrchestrator,
    StrategyRepository,
    _PersistedSimulator,
]:
    bundle = load_capability_bundle(repo_root)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    intent = load_optimization_intent(
        repo_root
        / "configs/rto/intents"
        / ("quality_yield_energy.json" if multi else "minimize_specific_furnace_energy.json")
    )
    simulator = _PersistedSimulator(
        tmp_path / "runs" / "placeholder" / "simulator",
        context.model_ref.fingerprint,
        context.case_ref.fingerprint,
        make_bundle,
    )

    def simulator_factory(output_root: Path) -> _PersistedSimulator:
        simulator._output_root = output_root
        return simulator

    orchestrator = OfflineRtoOrchestrator(CduM7RequestFactory(), simulator_factory)
    repository = StrategyRepository(tmp_path / "library")
    record = orchestrator.run(
        bundle,
        intent,
        context,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="workflow-test",
        coverage_policy=coverage_policy,
    )
    return record, orchestrator, repository, simulator


@pytest.mark.parametrize("multi", [False, True])
def test_unified_single_and_multi_workflows_resume_without_new_simulation(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    multi: bool,
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=multi,
        coverage_policy="point",
    )
    assert record.result.status == "completed_draft"
    assert record.strategy is not None
    assert record.anchor_validation is not None
    assert len(record.anchor_validation.attempts) == 1
    assert record.anchor_validation.passed
    assert record.finalization.result.status == "success"
    assert record.problem.objectives and len(record.problem.objectives) == (3 if multi else 1)
    assert record.problem.result_request.mode == ("pareto-and-selected" if multi else "selected")
    first_calls = simulator.evaluate_calls
    assert first_calls > 0

    repeated = orchestrator.run(
        record.capability_snapshot.bundle,
        record.intent,
        record.context,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="workflow-test",
        coverage_policy="point",
    )
    inspected = read_offline_run(
        record.run_dir,
        strategy_repository=repository,
        request_factory=CduM7RequestFactory(),
        simulator=simulator,
    )

    assert simulator.evaluate_calls == first_calls
    assert repeated.physical_m2_executions == repeated.physical_m4_executions == 0
    assert inspected.physical_m2_executions == inspected.physical_m4_executions == 0
    assert repeated.result == inspected.result == record.result
    assert repeated.manifest.fingerprint == inspected.manifest.fingerprint
    assert repository.read(record.strategy.strategy_id, 1).current_state == "draft"


def test_runtime_auto_inspect_strictly_replays_a_unified_run(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _, _, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=True,
        coverage_policy="point",
    )
    calls_before = simulator.evaluate_calls
    monkeypatch.setattr(runtime_api, "CduM7Simulator", lambda _: simulator)

    inspected = runtime_api.inspect_offline_auto(
        record.run_dir,
        repo_root=repo_root,
        library_root=tmp_path / "library",
    )
    summary = runtime_api.run_summary(inspected)
    chat_summary = build_chat_result_summary(inspected)

    assert inspected.result == record.result
    assert inspected.manifest.fingerprint == record.manifest.fingerprint
    assert simulator.evaluate_calls == calls_before
    assert summary["workflow_kind"] == "unified"
    assert summary["objective_count"] == 3
    assert summary["control_authority"] == "none"
    assert summary["selected_setpoints"] == chat_summary["selected_setpoints"]
    assert chat_summary["status"] == "success"
    assert chat_summary["claim_scope"] == "engineering_simulation_only"
    assert chat_summary["field_validated"] is False
    assert chat_summary["control_authority"] == "none"
    assert {item["unit"] for item in chat_summary["selected_setpoints"]} == {
        "K",
        "Pa(a)",
    }
    assert len(chat_summary["objectives"]) == 3
    assert all(item["passed"] is True for item in chat_summary["constraints"])
    assert not {
        "context",
        "solver",
        "formula_id",
        "refs",
        "fingerprints",
        "paths",
        "evidence",
        "strategy",
    } & set(chat_summary)

    unselected_result = replace(
        inspected.finalization.result,
        status="no_verified_candidate",
        selected_proposal_ref=None,
        selected_static_evaluation_ref=None,
        selected_dynamic_evaluation_ref=None,
        publishability_assessment_ref=None,
        publishable=False,
        termination_reason="all-dynamic-candidates-rejected",
    )
    unselected = replace(
        inspected,
        finalization=replace(
            inspected.finalization,
            publishability=None,
            result=unselected_result,
        ),
    )
    assert build_chat_result_summary(unselected)["selected_setpoints"] == []
    assert build_chat_result_summary(unselected)["objectives"] == []
    assert build_chat_result_summary(unselected)["constraints"] == []


def test_sampled_anchor_workflow_is_discrete_and_strictly_relocatable(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, _, repository, _ = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="sampled-anchors",
    )
    assert record.anchor_validation is not None
    assert tuple(item.ratio for item in record.anchor_validation.attempts) == (0.95, 1.0, 1.05)
    assert record.anchor_validation.passed
    assert record.strategy is not None
    assert record.strategy.coverage_kind == "sampled_anchors"
    relocated = tmp_path / "relocated" / record.run_dir.name
    shutil.copytree(record.run_dir, relocated)
    simulator = _PersistedSimulator(
        relocated / "simulator",
        record.context.model_ref.fingerprint,
        record.context.case_ref.fingerprint,
        make_bundle,
    )

    inspected = read_offline_run(
        relocated,
        strategy_repository=repository,
        request_factory=CduM7RequestFactory(),
        simulator=simulator,
    )

    assert inspected.result == record.result
    assert inspected.manifest.fingerprint == record.manifest.fingerprint
    assert simulator.evaluate_calls == 0


def test_manifest_and_relative_evidence_are_both_enforced(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, _, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    static_path = record.run_dir / "static_solve.json"
    payload = json.loads(static_path.read_text(encoding="utf-8"))
    first = payload["solver_result"]["evaluations"][0]["evidence_refs"][0]
    first["run_ref"] = str((record.run_dir / first["run_ref"]).resolve())
    payload.pop("execution_ref")
    static_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest_path = record.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["static_solve.json"] = hashlib.sha256(static_path.read_bytes()).hexdigest()
    manifest.pop("manifest_fingerprint")
    manifest["manifest_fingerprint"] = canonical_fingerprint(manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="relative"):
        read_offline_run(
            record.run_dir,
            strategy_repository=repository,
            request_factory=CduM7RequestFactory(),
            simulator=simulator,
        )


@pytest.mark.parametrize(
    ("stage", "artifact_name"),
    [
        ("inputs-ready", "request.json"),
        ("problem-ready", "problem.json"),
        ("route-ready", "solver_route.json"),
        ("static-solve-ready", "static_solve.json"),
        ("static-selection-ready", "static_selection.json"),
        ("dynamic-evaluations-ready", "dynamic_evaluations.json"),
        ("finalization-ready", "finalization.json"),
        ("anchor-validation-ready", "anchor_validation.json"),
        ("strategy-draft-ready", "strategy_draft.json"),
        ("workflow-complete", "result.json"),
    ],
)
def test_committed_event_without_artifact_never_reexecutes(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    stage: str,
    artifact_name: str,
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    corrupt_root = tmp_path / f"corrupt-{stage}"
    corrupt_run = corrupt_root / record.run_dir.name
    shutil.copytree(record.run_dir, corrupt_run)
    (corrupt_run / "manifest.json").unlink()
    (corrupt_run / artifact_name).unlink()
    calls = simulator.evaluate_calls

    with pytest.raises(ValueError, match="event exists but committed artifact is missing"):
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=corrupt_root,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        )

    assert simulator.evaluate_calls == calls


def test_noncontiguous_event_branch_is_rejected_before_missing_stage_reexecutes(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    corrupt_root = tmp_path / "noncontiguous-events"
    corrupt_run = corrupt_root / record.run_dir.name
    shutil.copytree(record.run_dir, corrupt_run)
    (corrupt_run / "manifest.json").unlink()
    (corrupt_run / "static_solve.json").unlink()
    rewritten: list[WorkflowEvent] = []
    for event in record.events:
        if event.stage == "static-solve-ready":
            continue
        rewritten.append(
            replace(
                event,
                sequence=len(rewritten),
                previous_event_fingerprint=(None if not rewritten else rewritten[-1].fingerprint),
            )
        )
    (corrupt_run / "events.jsonl").write_bytes(
        b"".join(canonical_json_bytes(item.as_dict()) + b"\n" for item in rewritten)
    )
    calls = simulator.evaluate_calls

    with pytest.raises(ValueError, match="legal contiguous branch prefix"):
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=corrupt_root,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        )

    assert simulator.evaluate_calls == calls
    assert not (corrupt_run / "static_solve.json").exists()


def test_strict_replay_recomputes_evaluation_from_immutable_evidence(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, _, _, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    stored = record.solver_execution.result.evaluations[0]
    metric_id = next(iter(stored.metrics))
    tampered = replace(
        stored,
        metrics={**stored.metrics, metric_id: stored.metrics[metric_id] + 1.0},
    )

    with pytest.raises(ValueError, match="differs from strict evidence replay"):
        _replay_evaluations(
            (tampered,),
            proposals=record.solver_execution.result.proposals,
            problem=record.problem,
            context=record.context,
            catalog=record.capability_snapshot.bundle.catalog,
            request_factory=CduM7RequestFactory(),
            run_dir=record.run_dir,
            simulator=simulator,
        )


@pytest.mark.parametrize(
    ("fail_stage", "artifact_name", "event_stage"),
    [
        ("M2", "static_solve.json", "static-solve-ready"),
        ("M4", "dynamic_evaluations.json", "dynamic-evaluations-ready"),
    ],
)
def test_infrastructure_interruption_does_not_commit_unreplayable_stage(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    fail_stage: Literal["M2", "M4"],
    artifact_name: str,
    event_stage: str,
) -> None:
    bundle = load_capability_bundle(repo_root)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    intent = load_optimization_intent(
        repo_root / "configs/rto/intents/minimize_specific_furnace_energy.json"
    )
    simulator = _InterruptingSimulator(
        tmp_path / "placeholder" / "simulator",
        context.model_ref.fingerprint,
        context.case_ref.fingerprint,
        make_bundle,
        fail_stage=fail_stage,
    )

    def simulator_factory(output_root: Path) -> _InterruptingSimulator:
        simulator._output_root = output_root
        return simulator

    run_root = tmp_path / "interrupted-runs"
    orchestrator = OfflineRtoOrchestrator(CduM7RequestFactory(), simulator_factory)
    with pytest.raises(ValueError, match="replayable paired evidence"):
        orchestrator.run(
            bundle,
            intent,
            context,
            run_root=run_root,
            strategy_repository=StrategyRepository(tmp_path / "interrupted-library"),
            actor="workflow-test",
            coverage_policy="point",
        )

    run_dirs = tuple(run_root.iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert not (run_dir / artifact_name).exists()
    assert not (run_dir / "manifest.json").exists()
    stages = tuple(
        json.loads(line)["stage"]
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert event_stage not in stages
    if fail_stage == "M2":
        assert simulator.evaluate_calls == 0


@pytest.mark.parametrize(
    "extra_kind",
    [
        "unknown-file",
        "temporary-file",
        "unknown-directory",
        "fifo",
        "symbolic-link",
    ],
)
def test_manifest_rejects_every_uncommitted_top_level_entry(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
    extra_kind: str,
) -> None:
    record, _, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    if extra_kind == "symbolic-link":
        (record.run_dir / "unexpected-link").symlink_to(record.run_dir / "result.json")
    elif extra_kind == "unknown-directory":
        (record.run_dir / "unexpected-directory").mkdir()
    elif extra_kind == "fifo":
        os.mkfifo(record.run_dir / "unexpected-fifo")
    else:
        name = "unexpected.json" if extra_kind == "unknown-file" else ".result.json.tmp-999"
        (record.run_dir / name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="symbolic link|unexpected top-level"):
        read_offline_run(
            record.run_dir,
            strategy_repository=repository,
            request_factory=CduM7RequestFactory(),
            simulator=simulator,
        )


def test_manifest_preflight_rejects_unknown_directory_without_half_commit(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    manifest_path = record.run_dir / "manifest.json"
    manifest_path.unlink()
    (record.run_dir / "unexpected-directory").mkdir()
    calls = simulator.evaluate_calls

    with pytest.raises(ValueError, match="unexpected top-level entry"):
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=record.run_dir.parent,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        )

    assert simulator.evaluate_calls == calls
    assert not manifest_path.exists()


def test_manifest_preflight_rejects_socket_like_special_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SocketLikeEntry:
        name = "simulator"

        @staticmethod
        def is_symlink() -> bool:
            return False

        @staticmethod
        def is_file() -> bool:
            return False

        @staticmethod
        def is_dir() -> bool:
            return False

    entry = cast(Path, _SocketLikeEntry())
    monkeypatch.setattr(Path, "iterdir", lambda _: iter((entry,)))

    with pytest.raises(ValueError, match="simulator entry must be a top-level directory"):
        _validate_top_level_entries(
            tmp_path,
            expected_files=frozenset(),
            manifest_required=False,
        )


def test_manifest_requires_its_self_fingerprint(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, _, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    manifest_path = record.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("manifest_fingerprint")
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_fingerprint"):
        read_offline_run(
            record.run_dir,
            strategy_repository=repository,
            request_factory=CduM7RequestFactory(),
            simulator=simulator,
        )


def test_unknown_manifest_version_and_incomplete_event_line_are_rejected(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    manifest_path = record.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = "offline-rto-manifest-unknown"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_version"):
        read_offline_run(
            record.run_dir,
            strategy_repository=repository,
            request_factory=CduM7RequestFactory(),
            simulator=simulator,
        )

    manifest_path.unlink()
    events_path = record.run_dir / "events.jsonl"
    events_path.write_bytes(events_path.read_bytes().removesuffix(b"\n"))
    calls = simulator.evaluate_calls
    with pytest.raises(ValueError, match="incomplete final line"):
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=record.run_dir.parent,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        )
    assert simulator.evaluate_calls == calls


def test_unowned_workflow_lock_file_does_not_block_strict_resume(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    lock_path = record.run_dir / ".workflow.lock"
    lock_path.write_text("malformed stale diagnostics\n", encoding="ascii")
    calls = simulator.evaluate_calls

    resumed = orchestrator.run(
        record.capability_snapshot.bundle,
        record.intent,
        record.context,
        run_root=record.run_dir.parent,
        strategy_repository=repository,
        actor="workflow-test",
        coverage_policy="point",
    )

    assert resumed.result == record.result
    assert simulator.evaluate_calls == calls
    assert lock_path.is_file()
    assert lock_path.read_text(encoding="ascii") == f"pid={os.getpid()}\n"


def test_active_workflow_kernel_lock_rejects_second_writer_without_simulation(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    calls = simulator.evaluate_calls
    with (
        exclusive_file_lock(record.run_dir / ".workflow.lock", label="test workflow"),
        pytest.raises(RuntimeError, match="locked by another writer"),
    ):
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=record.run_dir.parent,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        )

    assert simulator.evaluate_calls == calls
    assert (
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=record.run_dir.parent,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        ).result
        == record.result
    )


def test_valid_artifact_without_event_is_recovered_without_simulation(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    (record.run_dir / "manifest.json").unlink()
    events_path = record.run_dir / "events.jsonl"
    lines = events_path.read_bytes().splitlines(keepends=True)
    assert json.loads(lines[-1])["stage"] == "workflow-complete"
    events_path.write_bytes(b"".join(lines[:-1]))
    calls = simulator.evaluate_calls

    resumed = orchestrator.run(
        record.capability_snapshot.bundle,
        record.intent,
        record.context,
        run_root=record.run_dir.parent,
        strategy_repository=repository,
        actor="workflow-test",
        coverage_policy="point",
    )

    assert resumed.result == record.result
    assert "workflow-complete" in resumed.recovered_stages
    assert simulator.evaluate_calls == calls


def test_verified_success_cannot_drop_strategy_and_repository_stale_lock_recovers(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, _, repository, _ = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    assert record.anchor_validation is not None and record.anchor_validation.passed
    with pytest.raises(ValueError, match="requires a strategy draft"):
        _offline_result(
            record.request,
            record.problem,
            record.routing,
            record.solver_execution,
            record.static_selection,
            record.dynamic_verification,
            record.finalization,
            record.anchor_validation,
            None,
        )

    lock_path = repository.root / ".unified-strategy-repository.lock"
    lock_path.write_text("2147483647", encoding="ascii")
    approved = repository.approve(
        record.strategy.strategy_id if record.strategy is not None else "missing-strategy",
        1,
        actor="workflow-test",
    )
    assert approved.current_state == "approved"
    assert lock_path.is_file()
    assert lock_path.read_text(encoding="ascii") == f"pid={os.getpid()}\n"


def test_strategy_repository_mismatch_stops_before_manifest_commit(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundle],
) -> None:
    record, orchestrator, repository, simulator = _run(
        repo_root,
        tmp_path,
        make_bundle,
        multi=False,
        coverage_policy="point",
    )
    strategy = record.strategy
    assert strategy is not None
    stored = repository.read(strategy.strategy_id, strategy.revision)
    assert len(stored.events) == 1
    tampered_entry = replace(strategy, hold_policy="tampered-hold")
    tampered_event = replace(stored.events[0], strategy_ref=tampered_entry.ref)
    entry_path = repository.entries_root / strategy.strategy_id / "r1" / "entry.json"
    entry_path.write_bytes(canonical_json_bytes(tampered_entry.as_dict()))
    entry_path.with_name("events.jsonl").write_bytes(
        canonical_json_bytes(tampered_event.as_dict()) + b"\n"
    )
    manifest_path = record.run_dir / "manifest.json"
    manifest_path.unlink()
    calls = simulator.evaluate_calls

    with pytest.raises(ValueError, match="repository payload differs"):
        orchestrator.run(
            record.capability_snapshot.bundle,
            record.intent,
            record.context,
            run_root=record.run_dir.parent,
            strategy_repository=repository,
            actor="workflow-test",
            coverage_policy="point",
        )

    assert simulator.evaluate_calls == calls
    assert not manifest_path.exists()
