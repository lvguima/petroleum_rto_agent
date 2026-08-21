from __future__ import annotations

import json
from pathlib import Path

import pytest

from petroleum_rto import rto
from petroleum_rto.rto import runtime
from petroleum_rto.rto.capabilities import load_capability_bundle
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.intent import load_optimization_intent
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
    assert runtime.inspect_offline is api.inspect_offline
    assert not any("legacy" in name.lower() for name in runtime.__all__)
    assert not any(name.endswith(("V1", "V2")) for name in rto.__all__)
    assert not any(name.startswith("Unified") for name in rto.__all__)


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
    build_calls: list[tuple[object, object, object]] = []
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
        library_root=tmp_path / "library",
        actor="test",
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
        api.inspect_offline(
            tmp_path / "run",
            library_root=tmp_path / "library",
        )
