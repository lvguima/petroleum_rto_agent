from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import petroleum_rto.cdu.runtime.cli as runtime_cli
from petroleum_rto.cdu.runtime.artifacts import RunRecord
from petroleum_rto.cdu.runtime.batch import (
    BATCH_REQUEST_VERSION,
    BatchRequest,
)
from petroleum_rto.cdu.runtime.cli import main
from petroleum_rto.cdu.runtime.contracts import RUNTIME_SCHEMA_VERSION, RunRequest
from petroleum_rto.cdu.runtime.presets import load_preset


def test_cli_lists_presets_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(("presets", "--json")) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert [item["preset_id"] for item in payload["presets"]] == [
        "steady-baseline",
        "open-loop-feed-step",
        "closed-loop-feed-step",
        "m6-abnormal-pump-trip",
        "m6-structural-rejection",
    ]


def test_cli_lists_inputs_emits_template_and_previews_custom_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert main(("inputs", "--preset", "open-loop-feed-step", "--json")) == 0
    inputs = json.loads(capsys.readouterr().out)
    ids = {item["input_id"] for item in inputs["inputs"]}
    assert "feed.mass_flow_t_h" in ids
    assert "dynamic.sensor_time_constant_s" in ids
    assert "inventory.flash_drum_ratio" in ids

    request_path = tmp_path / "custom-request.json"
    assert (
        main(
            (
                "template",
                "--preset",
                "steady-baseline",
                "--output",
                str(request_path),
            )
        )
        == 0
    )
    capsys.readouterr()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request == {
        "preset_id": "steady-baseline",
        "parameters": {},
        "overrides": {},
        "initial_state": {},
    }
    request["parameters"] = {"feed.mass_flow_t_h": 360.0}
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert main(("preview", "--request", str(request_path), "--json")) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["customized"] is True
    assert resolved["applied_inputs"]["feed.mass_flow_t_h"]["normalized_value"] == 100.0

    assert main(("preview", "--request", str(request_path))) == 0
    human_preview = capsys.readouterr().out
    assert "实际进料: 360.000 t/h" in human_preview
    assert "实际温压:" in human_preview
    assert "预览确认指纹:" in human_preview
    assert "实际动态参数:" not in human_preview

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert (
        main(
            (
                "run",
                "--request",
                str(request_path),
                "--output",
                str(tmp_path),
            )
        )
        == 0
    )
    interactive_output = capsys.readouterr().out
    assert "运行前预览: steady-baseline" in interactive_output
    assert "输入已确认，开始运行。" in interactive_output
    assert "实际进料" in interactive_output

    assert (
        main(
            (
                "run",
                "--request",
                str(request_path),
                "--confirm-preview",
                resolved["preview_fingerprint"],
                "--output",
                str(tmp_path),
                "--quiet",
            )
        )
        == 0
    )
    assert "success: steady-baseline" in capsys.readouterr().out


def test_cli_request_can_cancel_after_preview_without_creating_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = tmp_path / "sparse.json"
    request_path.write_text(
        json.dumps(
            {
                "preset_id": "steady-baseline",
                "parameters": {"feed.mass_flow_t_h": 350.0},
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"
    monkeypatch.setattr("builtins.input", lambda _: "n")

    assert (
        main(
            (
                "run",
                "--request",
                str(request_path),
                "--output",
                str(output_root),
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "实际进料: 350.000 t/h" in output
    assert "已取消，未启动仿真。" in output
    assert not output_root.exists()


@pytest.mark.parametrize("display_flag", ("--json", "--quiet"))
def test_cli_machine_display_requires_explicit_preview_fingerprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    display_flag: str,
) -> None:
    request_path = tmp_path / "sparse.json"
    request_path.write_text(
        json.dumps({"preset_id": "steady-baseline"}),
        encoding="utf-8",
    )

    assert main(("run", "--request", str(request_path), display_flag)) == 2
    assert "non-interactive request runs require --confirm-preview" in capsys.readouterr().err


def test_cli_dynamic_preview_displays_resolved_grid_initial_state_and_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "dynamic-preview.json"
    assert (
        main(
            (
                "template",
                "--preset",
                "open-loop-feed-step",
                "--output",
                str(request_path),
            )
        )
        == 0
    )
    capsys.readouterr()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["initial_state"] = {"inventory.flash_drum_ratio": 1.02}
    request["scenario"] = {
        "duration_s": 10.0,
        "time_step_s": 1.0,
        "events": [
            {
                "time_s": 2.0,
                "target": "fresh_feed_flow_kg_s",
                "value": 1.03,
                "value_basis": "nominal_ratio",
                "duration_s": None,
            }
        ],
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert main(("preview", "--request", str(request_path))) == 0
    output = capsys.readouterr().out
    assert "实际动态参数:" in output
    assert "flash_drum=1.02" in output
    assert "动态网格: 10.0 s / 1.0 s, 事件 1" in output
    assert "t=2.0 s: fresh_feed_flow_kg_s=1.03 (nominal_ratio)" in output


def test_cli_sparse_dynamic_request_previews_confirms_and_executes_in_one_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = tmp_path / "sparse-dynamic.json"
    request_path.write_text(
        json.dumps(
            {
                "preset_id": "open-loop-feed-step",
                "run_id": "sparse-dynamic-one-command",
                "parameters": {"feed.mass_flow_t_h": 360.0},
                "scenario": {
                    "duration_s": 6.0,
                    "time_step_s": 1.0,
                    "events": [],
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"
    monkeypatch.setattr("builtins.input", lambda _: "确认")

    assert (
        main(
            (
                "run",
                "--request",
                str(request_path),
                "--output",
                str(output_root),
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "实际进料: 360.000 t/h" in output
    assert "动态网格: 6.0 s / 1.0 s, 事件 0" in output
    assert "输入已确认，开始运行。" in output

    run_dir = output_root / "sparse-dynamic-one-command"
    assert main(("inspect", str(run_dir), "--json")) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["runtime_status"] == "success"
    assert summary["key_results"]["requested_duration_s"] == 6.0
    assert summary["key_results"]["sample_count"] == 7


def test_module_entrypoint_matches_console_facade_from_outside_repository(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    completed = subprocess.run(
        (sys.executable, "-m", "petroleum_rto.cdu.runtime", "presets", "--json"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert [item["preset_id"] for item in payload["presets"]] == [
        "steady-baseline",
        "open-loop-feed-step",
        "closed-loop-feed-step",
        "m6-abnormal-pump-trip",
        "m6-structural-rejection",
    ]


def test_windows_cli_pipe_uses_utf8_for_human_output(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "petroleum_rto.cdu.runtime",
            "inputs",
            "--preset",
            "steady-baseline",
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
    )

    assert completed.returncode == 0, completed.stderr
    assert "可调整输入: steady-baseline" in completed.stdout


def test_cli_run_and_inspect_share_the_stable_api(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            (
                "run",
                "--preset",
                "steady-baseline",
                "--output",
                str(tmp_path),
                "--json",
            )
        )
        == 0
    )
    run_payload = json.loads(capsys.readouterr().out)
    run_dir = Path(run_payload["run_dir"])
    assert run_payload["runtime_status"] == "success"

    assert main(("inspect", str(run_dir), "--json")) == 0
    inspect_payload = json.loads(capsys.readouterr().out)
    assert inspect_payload["result_fingerprint"] == run_payload["result_fingerprint"]
    assert inspect_payload["manifest_fingerprint"] == run_payload["manifest_fingerprint"]


def test_cli_normalizes_unknown_preset_to_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert (
        main(
            (
                "run",
                "--preset",
                "not-a-preset",
                "--output",
                str(tmp_path),
            )
        )
        == 2
    )
    assert "unknown runtime preset" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("runtime_status", "expected_exit_code"),
    [
        ("success", 0),
        ("limited", 0),
        ("rejected", 0),
        ("not_converged", 1),
        ("failed", 1),
    ],
)
def test_cli_run_exit_code_follows_public_runtime_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    runtime_status: str,
    expected_exit_code: int,
) -> None:
    def fake_run(request: RunRequest, *, output_root: Path) -> RunRecord:
        return cast(
            RunRecord,
            SimpleNamespace(
                run_dir=output_root / "fake-run",
                request=request,
                payload=SimpleNamespace(
                    runtime_status=runtime_status,
                    engine_status=(
                        "success"
                        if runtime_status in {"success", "limited", "rejected"}
                        else runtime_status
                    ),
                    summary={},
                    events=(),
                    errors=(),
                    result_fingerprint="a" * 64,
                ),
                manifest=SimpleNamespace(
                    run_id="fake-run",
                    manifest_fingerprint="b" * 64,
                ),
            ),
        )

    monkeypatch.setattr(runtime_cli, "run", fake_run)
    assert (
        main(
            (
                "run",
                "--preset",
                "steady-baseline",
                "--output",
                str(tmp_path),
                "--json",
            )
        )
        == expected_exit_code
    )
    assert json.loads(capsys.readouterr().out)["runtime_status"] == runtime_status


@pytest.mark.parametrize(
    ("field", "value", "error_match"),
    [
        ("random_seed", True, "random_seed"),
        ("preset_id", "../steady-baseline", "path traversal"),
        ("unknown_field", 1, "unknown"),
    ],
)
def test_cli_rejects_invalid_request_parameter_with_usage_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
    error_match: str,
) -> None:
    payload = load_preset("steady-baseline").as_dict()
    payload[field] = value
    request_path = tmp_path / f"invalid-{field}-request.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        main(
            (
                "run",
                "--request",
                str(request_path),
                "--output",
                str(tmp_path),
            )
        )
        == 2
    )
    assert error_match in capsys.readouterr().err


def test_cli_batch_executes_and_resumes_without_replacing_valid_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = BatchRequest(
        schema_version=RUNTIME_SCHEMA_VERSION,
        request_version=BATCH_REQUEST_VERSION,
        batch_id="cli-batch",
        items=(
            load_preset("steady-baseline"),
            load_preset("m6-structural-rejection"),
        ),
    )
    request_path = tmp_path / "batch-request.json"
    request_path.write_text(
        json.dumps(request.as_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    assert (
        main(
            (
                "batch",
                "--request",
                str(request_path),
                "--output",
                str(tmp_path),
                "--json",
            )
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["batch_status"] == "limited"
    assert first["completed_items"] == 2

    batch_dir = Path(first["batch_dir"])
    manifests_before = tuple(
        sorted(path.read_bytes() for path in batch_dir.glob("items/*/attempt-*/*/manifest.json"))
    )
    assert main(("batch", "--resume", str(batch_dir), "--json")) == 0
    resumed = json.loads(capsys.readouterr().out)
    manifests_after = tuple(
        sorted(path.read_bytes() for path in batch_dir.glob("items/*/attempt-*/*/manifest.json"))
    )
    assert resumed["batch_status"] == "limited"
    assert manifests_after == manifests_before
