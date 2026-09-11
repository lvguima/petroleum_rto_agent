"""Checkpointed Agent acceptance with the real CDU M2/M4 runtime, all data in tmp."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionError, SessionStore
from petroleum_rto.domain_model.models import ModelSelection, model_profile
from petroleum_rto.domain_model.native import DmxNativeModel, NativeTransport
from petroleum_rto.rto.runtime import load_prepared_optimization

_CHAT_FIXTURE_MODEL = "deepseek-v4-flash-0731"


def _model(
    requests: list[dict[str, Any]], *, prepare: bool = False, explain: bool = False
) -> DmxNativeModel:
    """Fake only the remote model; every domain and simulation call stays real."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if explain:
            assert len(requests) == 1 and not payload.get("tools")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "核验结果已保存；此说明仅解释程序报告。",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        assert prepare, "restore and local actions must not contact a model"
        number = len(requests)
        message: dict[str, Any] = {"role": "assistant", "content": None}
        if number == 1:
            name, arguments = "read_operating_context", {}
        elif number == 2:
            context = json.loads(
                next(
                    message["content"]
                    for message in payload["messages"]
                    if message.get("role") == "tool" and message.get("tool_call_id") == "c1"
                )
            )
            name, arguments = (
                "prepare_optimization",
                {
                    "snapshot_ref": context["snapshot_ref"],
                    "objectives": [
                        {
                            "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                            "sense": "minimize",
                        }
                    ],
                    "decision_variables": ["furnace_temperature_target_k"],
                },
            )
        else:
            assert number == 3
            message["content"] = "已准备仅调整炉温的离线优化方案，请核对程序摘要。"
            name, arguments = "", {}
        if name:
            message["tool_calls"] = [
                {
                    "id": f"c{number}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"reply-{number}",
                "model": _CHAT_FIXTURE_MODEL,
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if name else "stop"}
                ],
            },
        )

    transport = NativeTransport("fake-test-key", http_transport=httpx.MockTransport(respond))
    return DmxNativeModel(
        transport=transport,
        selection=ModelSelection(model_profile(_CHAT_FIXTURE_MODEL)),
        use_stream=False,
    )


def _simulator_files(root: Path) -> dict[str, tuple[int, int, str]]:
    """Count and hash actual files so a silent rerun or rewrite cannot pass as reuse."""
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _reject_changed_receipt(
    workspace: Path, session_path: Path, requests: list[dict[str, Any]], *, completed_plan: bool
) -> None:
    original_database = session_path.read_bytes()
    runtime = ReactAgent(
        _model(requests), AgentDomainTools(workspace), store=SessionStore(session_path)
    )
    try:
        data = deepcopy(runtime.data)
        if completed_plan:
            # Keeping static_ref unchanged must not hide a corrupted receipt projection.
            data["pending"]["static"]["selection"]["status"] = "no_feasible"
        else:
            assert data["pending"] is None
            data["last_result"]["result"]["status"] = "no_feasible"
        runtime._update(data)
    finally:
        runtime.close()
    try:
        with pytest.raises(SessionError):
            ReactAgent(
                _model(requests), AgentDomainTools(workspace), store=SessionStore(session_path)
            )
    finally:
        session_path.write_bytes(original_database)


def test_real_cdu_execution_survives_configuration_drift_and_strict_checkpoint_reuse(
    repo_root: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(repo_root / "configs/rto", workspace / "configs/rto")
    session_path = workspace / "runs/assistant/session.sqlite"
    requests: list[dict[str, Any]] = []
    runtime = ReactAgent(
        _model(requests, prepare=True),
        AgentDomainTools(workspace),
        store=SessionStore(session_path),
    )
    try:
        prepared_turn = runtime.handle("降低单位进料炉燃料热负荷，只允许调整炉出口温度。")
        assert not prepared_turn.errors, prepared_turn.errors
        assert len(requests) == 3 and not (workspace / "runs/rto").exists()
        assert runtime.data["pending"]["status"] == "awaiting_confirmation"
        saved = runtime.data["pending"]["prepared"]
        fixed = load_prepared_optimization(saved)
        assert len(fixed.problem.decision_domains) == 1
    finally:
        runtime.close()

    # These external inputs are no longer authoritative for the displayed plan.
    context_path = workspace / "configs/rto/contexts/case_20260604.json"
    changed_context = json.loads(context_path.read_text())
    changed_context["data_timestamp"] = "2026-09-11T10:00:00+08:00"
    changed_context["current_setpoints"]["furnace_temperature_target_k"] = 630.0
    context_path.write_text(json.dumps(changed_context))
    (workspace / "configs/rto/capabilities/system_policy.json").write_text('{"changed": true}')
    resumed_requests: list[dict[str, Any]] = []
    runtime = ReactAgent(
        _model(resumed_requests, explain=True),
        AgentDomainTools(workspace),
        store=SessionStore(session_path),
    )
    try:
        assert runtime.startup() and not resumed_requests
        assert load_prepared_optimization(runtime.data["pending"]["prepared"]) == fixed
        assert runtime.handle("/resume").errors  # Awaiting approval is not permission to resume.
        progress: list[str] = []
        completed = runtime.handle("/confirm", on_progress=progress.append)
        assert not completed.errors, completed.errors
        pending = runtime.data["pending"]
        assert pending["status"] == "completed"
        assert pending["static"]["physical_m2_executions"] > 0
        result = pending["result"]
        assert result["physical_m2_executions"] == 0
        assert result["physical_m4_executions"] > 0
        assert result["result"]["status"] in {"success", "feasible_not_publishable"}
        assert len(resumed_requests) == 1 and not resumed_requests[0].get("tools")
        assert any("M2静态搜索已评价" in value for value in progress)
        assert any("M4动态复核已复核" in value for value in progress)
        run_dir = workspace / "runs/rto" / result["workflow_id"]
        dynamic = json.loads((run_dir / "dynamic_evaluations.json").read_text())
        shortlist = pending["static"]["selection"]["shortlist_proposal_refs"]
        assert len(dynamic["evaluations"]) == len(shortlist) > 0
        assert json.loads((run_dir / "problem.json").read_text()) == fixed.problem.as_dict()
        assert json.loads((run_dir / "context.json").read_text()) == fixed.context.as_dict()
    finally:
        runtime.close()
    simulator_root = run_dir / "simulator"
    evidence_before = _simulator_files(simulator_root)
    assert evidence_before
    queries: list[dict[str, Any]] = []
    runtime = ReactAgent(
        _model(queries), AgentDomainTools(workspace), store=SessionStore(session_path)
    )
    try:
        assert runtime.startup()
        assert runtime.data["pending"]["prepared"] == saved
        assert runtime.data["last_result"]["result"] == result["result"]
        assert not runtime.handle("/result").errors
        assert not runtime.handle("/result " + result["workflow_id"]).errors
        assert not runtime.handle("/confirm").errors
        assert runtime.handle("/resume").errors
        assert _simulator_files(simulator_root) == evidence_before and not queries
    finally:
        runtime.close()

    _reject_changed_receipt(workspace, session_path, queries, completed_plan=True)
    assert _simulator_files(simulator_root) == evidence_before and not queries

    # A valid SQLite checkpoint is insufficient when referenced physical evidence is missing.
    manifest_path = next(simulator_root.rglob("manifest.json"))
    original_manifest = manifest_path.read_bytes()
    for content in (None, b'{"tampered":true}'):
        if content is None:
            manifest_path.unlink()
        else:
            manifest_path.write_bytes(content)
        with pytest.raises(SessionError):
            ReactAgent(
                _model(queries), AgentDomainTools(workspace), store=SessionStore(session_path)
            )
        assert not queries
        manifest_path.write_bytes(original_manifest)
    restored_evidence = _simulator_files(simulator_root)

    runtime = ReactAgent(
        _model(queries), AgentDomainTools(workspace), store=SessionStore(session_path)
    )
    try:
        runtime.startup()
        assert not runtime.handle("/cancel").errors
        assert runtime.data["pending"] is None
        assert runtime.data["last_result"]["result"] == result["result"]
        assert not runtime.handle("/result").errors
        assert _simulator_files(simulator_root) == restored_evidence and not queries
    finally:
        runtime.close()
    _reject_changed_receipt(workspace, session_path, queries, completed_plan=False)
    assert _simulator_files(simulator_root) == restored_evidence and not queries
    runtime = ReactAgent(
        _model(queries), AgentDomainTools(workspace), store=SessionStore(session_path)
    )
    try:
        runtime.startup()
        assert runtime.data["pending"] is None
        assert runtime.data["last_result"]["result"] == result["result"]
        assert not runtime.handle("/result").errors
        assert runtime.handle("/resume").errors
        assert _simulator_files(simulator_root) == restored_evidence and not queries
    finally:
        runtime.close()
