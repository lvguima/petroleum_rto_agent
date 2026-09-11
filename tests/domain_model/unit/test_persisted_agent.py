from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage, messages_to_dict
from test_native_protocol import Wire, call, chat, selection

from petroleum_rto.assistant import react
from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionError, SessionStore
from petroleum_rto.assistant.state import new_session, validate_session
from petroleum_rto.rto import load_operating_context
from petroleum_rto.rto.runtime import load_prepared_optimization

_PAGE_TEXT = "原始全文分页测试🙂" * 10_000 + "最后一页标记"
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from test_persisted_agent import _worker_main; _worker_main()"
)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _worker_main() -> None:
    """A separate interpreter runs the production Agent with an in-process HTTP fake."""
    workspace, path, action = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    calls = {"m2": 0, "m4": 0}

    def stop_m2(*args: Any, **kwargs: Any) -> None:
        calls["m2"] += 1
        raise KeyboardInterrupt("synthetic interruption before any simulation")

    def stop_m4(*args: Any, **kwargs: Any) -> None:
        calls["m4"] += 1
        raise AssertionError("unrequested M4 execution")

    react.solve_prepared_optimization = stop_m2
    react.verify_prepared_optimization = stop_m4
    wire = Wire([])
    runtime: ReactAgent | None = None
    store: SessionStore | None = None
    try:
        domain = AgentDomainTools(workspace)
        if action == "prepare":
            ordinary_info = domain.plant_info

            def large_info(state: dict[str, Any]) -> dict[str, Any]:
                return {**ordinary_info(state), "synthetic_reference_text": _PAGE_TEXT}

            domain.plant_info = large_info  # type: ignore[method-assign]
            context = load_operating_context(workspace / "configs/rto/contexts/case_20260604.json")
            args = {
                "snapshot_ref": context.fingerprint,
                "objectives": [
                    {"metric_id": "specific_furnace_fuel_energy_mj_per_t", "sense": "minimize"}
                ],
                "decision_variables": ["furnace_temperature_target_k"],
            }
            wire.replies.extend(
                [
                    chat(None, calls=[call("get_plant_info", call_id="info")]),
                    chat(None, calls=[call("read_operating_context", call_id="context")]),
                    chat(None, calls=[call("prepare_optimization", json.dumps(args), "prepare")]),
                    chat("固定方案已准备，请阅读程序摘要。"),
                ]
            )
        store = SessionStore(path)
        runtime = ReactAgent(wire.model(), domain, store=store)
        startup = runtime.startup()
        startup_requests = len(wire.requests)
        startup_stages = dict(calls)
        output: dict[str, Any] = {
            "startup": startup,
            "startup_requests": startup_requests,
            "startup_stages": startup_stages,
        }
        if action == "prepare":
            turn = runtime.handle("准备降低单位炉燃料热负荷；只调温度，不调压力。")
            assert not turn.errors, turn.errors
            assert not runtime.handle("/model kimi-k3").errors
            assert not runtime.handle("/thinking on high").errors
        elif action == "clear":
            assert not runtime.handle("/clear").errors
        elif action == "approve":
            turn = runtime.handle("/confirm")
            assert turn.errors, "the synthetic M2 interruption must be surfaced"
            output["turn_errors"] = turn.errors
        elif action == "followup":
            wire.replies.append(chat("该方案仍只允许调整温度。", reasoning="native-followup"))
            turn = runtime.handle("刚才允许调整哪些变量？")
            assert not turn.errors, turn.errors
        elif action == "resume":
            turn = runtime.handle("/resume")
            output["turn_errors"] = turn.errors
        elif action == "read-page":
            result_ref, saved_text = next(iter(runtime.data["context"]["results"].items()))
            args = {"result_ref": result_ref, "offset": len(saved_text) - 80, "max_characters": 80}
            wire.replies.extend(
                [
                    chat(
                        None,
                        calls=[call("read_tool_result", json.dumps(args), "page")],
                        reasoning="native-page-call",
                    ),
                    chat("已读取保存的最后一页。", reasoning="native-page-result"),
                ]
            )
            turn = runtime.handle("读取之前保存结果的最后80个字符。")
            assert not turn.errors, turn.errors
            page_message = next(
                message
                for message in reversed(runtime.messages)
                if isinstance(message, ToolMessage) and message.name == "read_tool_result"
            )
            output["page"] = json.loads(str(page_message.content))
            output["expected_page"] = saved_text[-80:]
        elif action != "restore":
            raise AssertionError(f"unknown worker action: {action}")
        data = runtime.data
        pending = data["pending"]
        output.update(
            requests=len(wire.requests),
            stages=calls,
            messages=len(runtime.messages),
            history_hash=_hash(messages_to_dict(runtime.messages)),
            model=data["model"],
            segment_start=data["segment_start"],
            snapshots=data["snapshots"],
            pending_status=pending["status"] if pending else None,
            pending_ref=pending["ref"] if pending else None,
            problem_hash=_hash(load_prepared_optimization(pending["prepared"]).problem.as_dict())
            if pending
            else None,
            prepared=pending["prepared"] if pending else None,
            context=data["context"],
            last_result=data["last_result"],
            page_sources_retained=all(
                any(isinstance(m, ToolMessage) and m.content == text for m in runtime.messages)
                for text in data["context"]["results"].values()
            ),
        )
        print(json.dumps(output, ensure_ascii=False))
    except SessionError as exc:
        print(
            json.dumps({"session_error": exc.code, "requests": len(wire.requests), "stages": calls})
        )
    finally:
        if runtime is not None:
            runtime.close()
        else:
            wire.transport.close()
            if store is not None:
                store.close()


@pytest.fixture
def isolated_workspace(tmp_path: Path, repo_root: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(repo_root / "configs/rto", workspace / "configs/rto")
    return workspace


def _run_process(workspace: Path, action: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _BOOTSTRAP,
            str(Path(__file__).parent),
            str(workspace),
            str(workspace / "runs/assistant/session.sqlite"),
            action,
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def prepared_session(isolated_workspace: Path) -> tuple[Path, dict[str, Any]]:
    saved = _run_process(isolated_workspace, "prepare")
    assert "session_error" not in saved, saved
    assert saved["requests"] == 4
    assert saved["stages"] == {"m2": 0, "m4": 0}
    assert saved["pending_status"] == "awaiting_confirmation"
    assert saved["context"]["results"], "the actual oversized tool result must be saved"
    return isolated_workspace, saved


def test_real_agent_recovers_messages_selection_plan_context_and_page_in_next_process(
    prepared_session: tuple[Path, dict[str, Any]],
) -> None:
    workspace, saved = prepared_session
    restored = _run_process(workspace, "restore")
    assert restored["startup"] and restored["startup_requests"] == 0
    assert restored["requests"] == 0 and restored["stages"] == {"m2": 0, "m4": 0}
    for key in (
        "history_hash",
        "messages",
        "model",
        "segment_start",
        "snapshots",
        "prepared",
        "context",
    ):
        assert restored[key] == saved[key], key
    assert restored["page_sources_retained"]
    assert restored["model"]["model_id"] == "kimi-k3"
    assert restored["model"]["thinking"] == "on" and restored["model"]["effort"] == "high"
    assert restored["pending_status"] == "awaiting_confirmation"
    assert b"fake-private-api-key" not in (workspace / "runs/assistant/session.sqlite").read_bytes()
    assert not (workspace / "runs/rto").exists()
    page = _run_process(workspace, "read-page")
    assert page["startup_requests"] == 0 and page["requests"] == 2
    assert page["page"]["text_chunk"] == page["expected_page"]
    assert page["page"]["next_offset"] is None
    assert page["stages"] == {"m2": 0, "m4": 0}


def test_restore_reconstructs_bound_problem_after_current_configs_change(
    prepared_session: tuple[Path, dict[str, Any]],
) -> None:
    workspace, saved = prepared_session
    (workspace / "configs/rto/capabilities/system_policy.json").write_text('{"changed": true}')
    (workspace / "configs/rto/contexts/case_20260604.json").unlink()
    restored = _run_process(workspace, "restore")
    assert "session_error" not in restored
    assert restored["problem_hash"] == saved["problem_hash"]
    assert restored["prepared"] == saved["prepared"]
    assert restored["requests"] == 0 and restored["stages"] == {"m2": 0, "m4": 0}


def test_clear_persists_empty_session_keeps_model_and_preserves_disk_evidence(
    prepared_session: tuple[Path, dict[str, Any]],
) -> None:
    workspace, saved = prepared_session
    approved = _run_process(workspace, "approve")
    assert approved["pending_status"] == "approved"
    marker = workspace / "runs/rto/preserved-test-evidence.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("preserved test evidence")
    cleared = _run_process(workspace, "clear")
    assert not cleared["pending_status"] and cleared["messages"] == 0
    assert not cleared["snapshots"] and not cleared["context"]["results"]
    restored = _run_process(workspace, "restore")
    assert restored["model"] == saved["model"]
    assert restored["messages"] == 0 and restored["last_result"] is None
    assert restored["pending_ref"] is None and restored["context"]["summary"] is None
    assert restored["requests"] == 0 and restored["stages"] == {"m2": 0, "m4": 0}
    assert marker.read_text() == "preserved test evidence"
    resumed = _run_process(workspace, "resume")
    assert resumed["turn_errors"] and resumed["stages"] == {"m2": 0, "m4": 0}


def test_approved_unfinished_restart_and_followup_do_not_resume_calculation(
    prepared_session: tuple[Path, dict[str, Any]],
) -> None:
    workspace, saved = prepared_session
    approved = _run_process(workspace, "approve")
    assert approved["pending_status"] == "approved"
    assert approved["stages"] == {"m2": 1, "m4": 0}
    assert approved["requests"] == 0
    restored = _run_process(workspace, "restore")
    assert "/resume" in "".join(restored["startup"])
    assert restored["pending_ref"] == saved["pending_ref"]
    assert restored["requests"] == 0 and restored["stages"] == {"m2": 0, "m4": 0}
    followup = _run_process(workspace, "followup")
    assert followup["pending_status"] == "approved"
    assert followup["stages"] == {"m2": 0, "m4": 0}
    resumed = _run_process(workspace, "resume")
    assert resumed["stages"] == {"m2": 1, "m4": 0}
    assert resumed["requests"] == 0 and resumed["pending_status"] == "approved"


def test_second_agent_process_cannot_change_a_held_session(
    prepared_session: tuple[Path, dict[str, Any]],
) -> None:
    workspace, saved = prepared_session
    with SessionStore(workspace / "runs/assistant/session.sqlite"):
        rejected = _run_process(workspace, "clear")
    assert rejected["session_error"] == "session-in-use"
    restored = _run_process(workspace, "restore")
    assert restored["history_hash"] == saved["history_hash"]
    assert restored["pending_ref"] == saved["pending_ref"]


def _corrupt_state(workspace: Path, mutate: Any) -> None:
    with SessionStore(workspace / "runs/assistant/session.sqlite") as store:
        record = store.saver.get_tuple(store.config)
        assert record is not None
        checkpoint = copy.deepcopy(record.checkpoint)
        mutate(checkpoint["channel_values"]["session"])
        type_name, blob = store.saver.serde.dumps_typed(checkpoint)
        store.saver.conn.execute(
            "UPDATE checkpoints SET type=?, checkpoint=? WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
            (type_name, blob, "current", "", checkpoint["id"]),
        )
        store.saver.conn.commit()


@pytest.mark.parametrize("corruption", ["missing-stage", "invalid-version", "bad-page"])
def test_actual_agent_refuses_corrupt_recovery_before_any_request_or_computation(
    prepared_session: tuple[Path, dict[str, Any]], corruption: str
) -> None:
    workspace, _ = prepared_session

    def mutate(data: dict[str, Any]) -> None:
        if corruption == "missing-stage":
            data["pending"]["status"] = "approved"
            data["pending"]["static"] = {"static_ref": "missing-evidence"}
        elif corruption == "invalid-version":
            data["schema_version"] = "999.0.0"
        else:
            ref = next(iter(data["context"]["results"]))
            data["context"]["results"][ref] = "tampered tool result"

    _corrupt_state(workspace, mutate)
    refused = _run_process(workspace, "restore")
    assert refused["session_error"] == "invalid-restored-session"
    assert refused["requests"] == 0 and refused["stages"] == {"m2": 0, "m4": 0}


@pytest.mark.parametrize("field", ["schema_version", "model", "pending", "context", "snapshots"])
def test_session_reader_rejects_missing_saved_fields(field: str) -> None:
    saved = new_session(selection())
    del saved[field]
    with pytest.raises(ValueError):
        validate_session(saved, 0)


@pytest.mark.parametrize(
    "field,value",
    [("schema_version", "999.0.0"), ("turn_id", -1), ("segment_start", 5), ("unknown", True)],
)
def test_session_reader_rejects_versions_ranges_and_unknown_fields(field: str, value: Any) -> None:
    saved = new_session(selection())
    saved[field] = value
    with pytest.raises(ValueError):
        validate_session(saved, 0)


@pytest.mark.parametrize("field", ["thinking", "effort", "output_tokens"])
def test_session_reader_rejects_missing_saved_model_fields(field: str) -> None:
    saved = new_session(selection())
    del saved["model"][field]
    with pytest.raises(ValueError):
        validate_session(saved, 0)
