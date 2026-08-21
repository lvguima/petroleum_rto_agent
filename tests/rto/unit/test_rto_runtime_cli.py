from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from petroleum_rto import rto
from petroleum_rto.rto import runtime
from petroleum_rto.rto.catalogs import load_rto_v2_bundle
from petroleum_rto.rto.compilation import SystemCompilationError
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.orchestration import OfflineRtoRequestV1
from petroleum_rto.rto.runtime import api, cli


@pytest.mark.parametrize(
    ("intent_name", "objective_count"),
    [
        ("minimize_specific_furnace_energy.json", 1),
        ("quality_yield_energy.json", 3),
    ],
)
def test_unified_single_and_multi_intent_and_problem_validation_call_no_solver(
    repo_root: Path,
    intent_name: str,
    objective_count: int,
) -> None:
    intent_file = repo_root / "configs" / "rto" / "intents" / intent_name
    context_file = repo_root / "configs" / "rto" / "contexts" / "case_20260604.json"

    resolution = api.validate_intent_file(
        repo_root=repo_root,
        intent_file=intent_file,
    )
    problem = api.validate_problem_files(
        repo_root=repo_root,
        intent_file=intent_file,
        context_file=context_file,
    )

    assert resolution.status == "resolved"
    assert resolution.resolved_intent is not None
    assert len(resolution.resolved_intent.objectives) == objective_count
    assert len(problem.objectives) == objective_count
    context = load_operating_context(context_file)
    assert problem.context_ref == context.ref


def test_default_capabilities_and_validate_problem_cli_use_unified_contracts(
    repo_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["capabilities", "--repo-root", str(repo_root)]) == 0
    capability_output = json.loads(capsys.readouterr().out)
    assert capability_output["manifest_id"] == "cdu-rto-public-capabilities"
    assert capability_output["solver_called"] is False
    assert len(capability_output["objectives"]) == 3

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
    assert problem_output["solver_called"] is False


def test_default_run_cli_uses_intent_context_and_has_no_version_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(cli, "run_offline", fake_run)
    monkeypatch.setattr(
        cli,
        "run_summary",
        lambda _: {
            "workflow_kind": "unified",
            "status": "completed_draft",
            "strategy_state": "draft",
            "control_authority": "none",
        },
    )
    intent_file = tmp_path / "intent.json"
    context_file = tmp_path / "context.json"

    assert (
        cli.main(
            [
                "run",
                "--repo-root",
                str(tmp_path),
                "--intent-file",
                str(intent_file),
                "--context-file",
                str(context_file),
                "--actor",
                "offline-builder",
                "--coverage-policy",
                "sampled-anchors",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["workflow_kind"] == "unified"
    assert output["strategy_state"] == "draft"
    assert output["control_authority"] == "none"
    assert calls == [
        {
            "repo_root": tmp_path.resolve(),
            "intent_file": intent_file.resolve(),
            "context_file": context_file.resolve(),
            "run_root": tmp_path / "runs" / "rto",
            "library_root": tmp_path / "runs" / "rto" / "strategy-library",
            "actor": "offline-builder",
            "coverage_policy": "sampled-anchors",
        }
    ]

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "run",
                "--intent-file",
                str(intent_file),
                "--context-file",
                str(context_file),
                "--actor",
                "offline-builder",
                "--request-version",
                "v2",
            ]
        )
    assert exc_info.value.code == 2


def test_default_inspect_cli_delegates_manifest_routing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_inspect(run_dir: Path, **kwargs: object) -> object:
        calls.append({"run_dir": run_dir, **kwargs})
        return object()

    monkeypatch.setattr(cli, "inspect_offline", fake_inspect)
    monkeypatch.setattr(
        cli,
        "run_summary",
        lambda _: {"workflow_kind": "unified", "control_authority": "none"},
    )

    assert (
        cli.main(
            [
                "inspect",
                "--repo-root",
                str(tmp_path),
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["workflow_kind"] == "unified"
    assert calls[0]["run_dir"] == (tmp_path / "run").resolve()
    assert calls[0]["legacy_request_file"] is None


def test_auto_inspect_routes_unified_manifest_without_legacy_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "unified"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_id": "offline-rto-workflow",
                "schema_version": "1.0.0",
                "manifest_version": "offline-rto-manifest-unified",
            }
        ),
        encoding="utf-8",
    )
    sentinel = cast(api.OfflineRunRecord, object())
    calls: list[Path] = []

    def fake_reader(path: Path, **_: object) -> api.OfflineRunRecord:
        calls.append(path)
        return sentinel

    monkeypatch.setattr(api, "read_unified_offline_run", fake_reader)

    assert (
        api.inspect_offline_auto(
            run_dir,
            repo_root=None,
            library_root=tmp_path / "library",
        )
        is sentinel
    )
    assert calls == [run_dir.resolve()]


def test_auto_inspect_routes_two_existing_legacy_manifests_without_version_argument(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    v1_run = repo_root / "runs" / "rto" / "offline-rto-fc174fafb89288e5"
    v2_run = repo_root / "runs" / "rto" / "offline-rto-v2-c69e0e46572af52f"
    v1_sentinel = cast(api.OfflineRunRecord, object())
    v2_sentinel = cast(api.OfflineRunRecord, object())
    v1_calls: list[dict[str, object]] = []
    v2_calls: list[dict[str, object]] = []
    v2_bundle_calls: list[Path | None] = []
    packaged_v2 = load_rto_v2_bundle()

    def fake_v1(path: Path, **kwargs: object) -> api.OfflineRunRecord:
        v1_calls.append({"run_dir": path, **kwargs})
        return v1_sentinel

    def fake_v2(path: Path, **kwargs: object) -> api.OfflineRunRecord:
        v2_calls.append({"run_dir": path, **kwargs})
        return v2_sentinel

    def tracked_v2_bundle(repo_root: Path | None = None) -> object:
        v2_bundle_calls.append(repo_root)
        return packaged_v2

    monkeypatch.setattr(api, "inspect_legacy_v1_offline", fake_v1)
    monkeypatch.setattr(api, "read_offline_run_v2", fake_v2)
    monkeypatch.setattr(
        api,
        "load_rto_v2_bundle",
        tracked_v2_bundle,
    )

    assert (
        api.inspect_offline_auto(
            v1_run,
            repo_root=repo_root,
            library_root=tmp_path / "library",
        )
        is v1_sentinel
    )
    assert (
        api.inspect_offline_auto(
            v2_run,
            repo_root=repo_root,
            library_root=tmp_path / "library",
        )
        is v2_sentinel
    )
    assert v1_calls[0]["repo_root"] is None
    assert "request_file" not in v1_calls[0]
    assert v2_bundle_calls == [None]
    assert v2_calls[0]["external_request"] is not None
    assert v2_calls[0]["resolved_intent"] is not None


def test_auto_inspect_requires_request_file_only_for_external_legacy_v1(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "runs" / "rto" / "offline-rto-fc174fafb89288e5" / "request.json"
    request = OfflineRtoRequestV1.from_mapping(json.loads(source.read_text(encoding="utf-8")))
    external = replace(
        request,
        external_request_ref=ContractRef("legacy-external-request", "f" * 64),
    )
    run_dir = tmp_path / "legacy-external-v1"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "manifest_version": "offline-rto-manifest-v1",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "request.json").write_text(
        json.dumps(external.as_dict()),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="legacy_request_file is required"):
        api.inspect_offline_auto(
            run_dir,
            repo_root=repo_root,
            library_root=tmp_path / "library",
        )

    validation_roots: list[Path | None] = []
    validate_external_request = api.validate_legacy_v1_request

    def tracked_validation(*, repo_root: Path | None, request_file: Path) -> object:
        validation_roots.append(repo_root)
        return validate_external_request(repo_root=repo_root, request_file=request_file)

    monkeypatch.setattr(api, "validate_legacy_v1_request", tracked_validation)
    with pytest.raises(ValueError, match="differs from the stored external request ref"):
        api.inspect_offline_auto(
            run_dir,
            repo_root=repo_root,
            library_root=tmp_path / "library",
            legacy_request_file=(repo_root / "configs/rto/requests/user_defined_feed_400_v1.json"),
        )
    assert validation_roots == [None]


def test_auto_inspect_rejects_manifest_signature_conflicts_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "bad-manifest"
    run_dir.mkdir()
    manifest = run_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_id": "offline-rto-workflow",
                "schema_version": "2.0.0",
                "manifest_version": "offline-rto-manifest-unified",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported or conflicting"):
        api.inspect_offline_auto(
            run_dir,
            repo_root=None,
            library_root=tmp_path / "library",
        )

    manifest.write_text(
        '{"schema_version":"1.0.0","schema_version":"1.0.0",'
        '"manifest_version":"offline-rto-manifest-v1"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        api.inspect_offline_auto(
            run_dir,
            repo_root=None,
            library_root=tmp_path / "library",
        )


def test_cli_reports_unknown_manifest_and_missing_legacy_request_as_usage_errors(
    capsys: pytest.CaptureFixture[str],
    repo_root: Path,
    tmp_path: Path,
) -> None:
    unknown = tmp_path / "unknown-manifest"
    unknown.mkdir()
    (unknown / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "9.0.0",
                "manifest_version": "offline-rto-manifest-unknown",
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "inspect",
                "--run-dir",
                str(unknown),
                "--library-root",
                str(tmp_path / "library"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported or conflicting offline manifest signature" in captured.err
    assert "Traceback" not in captured.err

    source = repo_root / "runs/rto/offline-rto-fc174fafb89288e5/request.json"
    request = OfflineRtoRequestV1.from_mapping(json.loads(source.read_text(encoding="utf-8")))
    external = replace(
        request,
        external_request_ref=ContractRef("legacy-external-request", "f" * 64),
    )
    missing_request = tmp_path / "missing-legacy-request"
    missing_request.mkdir()
    (missing_request / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "manifest_version": "offline-rto-manifest-v1",
            }
        ),
        encoding="utf-8",
    )
    (missing_request / "request.json").write_text(
        json.dumps(external.as_dict()),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "inspect",
                "--repo-root",
                str(repo_root),
                "--run-dir",
                str(missing_request),
                "--library-root",
                str(tmp_path / "library"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "legacy_request_file is required" in captured.err
    assert "Traceback" not in captured.err


def test_cli_reports_damaged_strict_evidence_as_system_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "damaged-evidence"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_id": "offline-rto-workflow",
                "schema_version": "1.0.0",
                "manifest_version": "offline-rto-manifest-unified",
            }
        ),
        encoding="utf-8",
    )

    def damaged_reader(*_: object, **__: object) -> object:
        raise ValueError("offline artifact hash differs: result.json")

    monkeypatch.setattr(api, "read_unified_offline_run", damaged_reader)
    assert (
        cli.main(
            [
                "inspect",
                "--run-dir",
                str(run_dir),
                "--library-root",
                str(tmp_path / "library"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "system error" in captured.err
    assert "offline artifact hash differs" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (TypeError("invalid parameter type"), 2),
        (ValueError("unsupported contract value"), 2),
        (OSError("evidence I/O failed"), 1),
        (RuntimeError("workflow is locked"), 1),
        (SystemCompilationError("trusted factory drifted"), 1),
    ],
)
def test_cli_classifies_contract_and_system_failures_without_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    expected_code: int,
) -> None:
    def failed_capabilities(**_: object) -> object:
        raise failure

    monkeypatch.setattr(cli, "capabilities", failed_capabilities)
    assert cli.main(["capabilities"]) == expected_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(failure) in captured.err
    assert "Traceback" not in captured.err


def test_cli_error_boundary_does_not_swallow_process_control_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(**_: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "capabilities", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["capabilities"])

    def exited(**_: object) -> object:
        raise SystemExit(7)

    monkeypatch.setattr(cli, "capabilities", exited)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["capabilities"])
    assert exc_info.value.code == 7

    def defective(**_: object) -> object:
        raise AssertionError("programming defect")

    monkeypatch.setattr(cli, "capabilities", defective)
    with pytest.raises(AssertionError, match="programming defect"):
        cli.main(["capabilities"])


def test_runtime_root_exports_canonical_unified_entries() -> None:
    assert runtime.run_offline is api.run_offline
    assert runtime.inspect_offline is api.inspect_offline
    assert runtime.query_strategies is api.query_strategies
    assert tuple(inspect.signature(api.run_offline).parameters) == (
        "repo_root",
        "intent_file",
        "context_file",
        "run_root",
        "library_root",
        "actor",
        "coverage_policy",
    )
    assert tuple(inspect.signature(api.inspect_offline).parameters) == (
        "run_dir",
        "repo_root",
        "library_root",
        "legacy_request_file",
    )
    assert tuple(inspect.signature(api.query_strategies).parameters) == (
        "library_root",
        "query",
    )
    assert rto.ProblemBuilder.__name__ == "UnifiedProblemBuilder"
    assert rto.CandidatePlanCompiler.__name__ == "UnifiedCandidatePlanCompiler"
    assert rto.OfflineRtoOrchestrator.__module__.endswith("orchestration.unified_service")
    assert rto.StrategyRepository.__module__.endswith("strategies.unified.repository")
    assert rto.read_offline_run.__module__.endswith("orchestration.unified_service")
    assert rto.LegacyRtoCatalogBundleV1.__name__ == "RtoCatalogBundle"
    assert rto.LegacyRtoCatalogBundleV2.__name__ == "RtoCatalogBundleV2"
    assert not hasattr(rto, "RtoCatalogBundle")
    assert not hasattr(rto, "RtoCatalogBundleV2")

    for retired_name in (
        "capabilities_v2",
        "external_request_summary",
        "external_request_summary_v2",
        "inspect_offline_v2",
        "run_offline_request",
        "run_offline_request_v2",
        "run_summary_v2",
        "validate_domain_intent_file_v2",
        "validate_external_request",
        "validate_external_request_v2",
    ):
        assert retired_name not in runtime.__all__
        assert not hasattr(runtime, retired_name)
        assert not hasattr(api, retired_name)


def test_explicit_legacy_validation_commands_remain_available(
    repo_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.main(
            [
                "legacy-validate-v1",
                "--repo-root",
                str(repo_root),
                "--request-file",
                str(repo_root / "configs/rto/requests/user_defined_feed_400_v1.json"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["request_id"] == "user-defined-feed-400-v1"

    assert (
        cli.main(
            [
                "legacy-validate-v2",
                "--repo-root",
                str(repo_root),
                "--request-file",
                str(repo_root / "configs/rto/requests/multiobjective_example_v2.json"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["request_version"].endswith("-v2")


def test_approve_cli_is_explicit_and_targets_default_unified_api(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    record = SimpleNamespace(
        entry=SimpleNamespace(ref=SimpleNamespace(as_dict=lambda: {"object_id": "s-r1"})),
        current_state="approved",
    )

    def fake_approve(**kwargs: object) -> object:
        calls.append(kwargs)
        return record

    monkeypatch.setattr(cli, "approve_strategy", fake_approve)

    assert (
        cli.main(
            [
                "approve",
                "--library-root",
                str(tmp_path),
                "--strategy-id",
                "strategy-1",
                "--revision",
                "1",
                "--actor",
                "reviewer",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["current_state"] == "approved"
    assert output["control_authority"] == "none"
    assert calls[0]["actor"] == "reviewer"
