from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from test_native_protocol import Wire, call, chat

from petroleum_rto.assistant.session import SessionError, SessionJsonSerializer, SessionStore


class SavedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    local: dict[str, Any]


_WORKER = """
import json, sys
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from petroleum_rto.assistant.session import SessionStore, SessionError
try:
    with SessionStore(Path(sys.argv[1])) as store:
        builder = StateGraph(MessagesState)
        builder.add_node('reply', lambda state: {'messages': [AIMessage(content='已保存中文')]})
        builder.add_edge(START, 'reply')
        builder.add_edge('reply', END)
        graph = builder.compile(checkpointer=store.saver)
        if sys.argv[2] == 'hold':
            print('locked', flush=True)
            sys.stdin.readline()
        elif sys.argv[2] == 'write':
            graph.invoke({'messages': [HumanMessage(content='原始会话')]}, store.config)
        elif sys.argv[2] == 'read':
            print(json.dumps([m.content for m in graph.get_state(store.config).values['messages']], ensure_ascii=False))
        elif sys.argv[2] == 'clear':
            store.clear()
            print(json.dumps(graph.get_state(store.config).values))
except SessionError as exc:
    print(exc.code, flush=True)
    sys.exit(9)
"""


def _child(path: Path, action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _WORKER, str(path), action],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_two_real_processes_restore_and_clear_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "assistant/session.sqlite"
    written = _child(path, "write")
    assert written.returncode == 0, written.stderr
    read = _child(path, "read")
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout) == ["原始会话", "已保存中文"]
    cleared = _child(path, "clear")
    assert cleared.returncode == 0, cleared.stderr
    assert json.loads(cleared.stdout) == {}
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM writes").fetchone() == (0,)


def test_second_process_fails_without_overwrite_and_crash_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "session.sqlite"
    holder = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(path), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        contender = _child(path, "write")
        assert contender.returncode == 9
        assert contender.stdout.strip() == "session-in-use"
    finally:
        holder.kill()
        holder.communicate(timeout=10)
    assert _child(path, "write").returncode == 0


def test_official_agent_checkpoints_tool_routing_and_native_message_fields(tmp_path: Path) -> None:
    wire = Wire([chat(None, calls=[call("read_marker")]), chat("标记读取完成。")])

    @tool
    def read_marker() -> str:
        """Read a synthetic test marker."""
        return "中文标记"

    path = tmp_path / "session.sqlite"
    with SessionStore(path) as store:
        graph = create_agent(wire.model(), [read_marker], checkpointer=store.saver)
        result = graph.invoke({"messages": [HumanMessage(content="读取标记")]}, store.config)
        saved_messages = result["messages"]
        assert len(saved_messages) == 4
        assert isinstance(saved_messages[2], ToolMessage)
        assert saved_messages[1].additional_kwargs
        assert len(list(store.saver.list(store.config))) > 1
    with SessionStore(path) as store:
        graph = create_agent(wire.model(), [read_marker], checkpointer=store.saver)
        assert graph.get_state(store.config).values["messages"] == saved_messages
        assert len(wire.requests) == 2


def test_official_interrupt_can_resume_after_close_and_preserves_json_payload(
    tmp_path: Path,
) -> None:
    builder = StateGraph(SavedState)

    def approval(state: SavedState) -> dict[str, Any]:
        approved = interrupt({"version": 2, "description": "固定方案"})
        return {"local": {**state["local"], "approved": approved}}

    builder.add_node("approval", approval)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    path = tmp_path / "session.sqlite"
    with SessionStore(path) as store:
        graph = builder.compile(checkpointer=store.saver)
        result = graph.invoke({"messages": [], "local": {"model": "chosen"}}, store.config)
        assert result["__interrupt__"][0].value["version"] == 2
    with SessionStore(path) as store:
        graph = builder.compile(checkpointer=store.saver)
        assert graph.get_state(store.config).next == ("approval",)
        resumed = graph.invoke(Command(resume=True), store.config)
        assert resumed["local"] == {"model": "chosen", "approved": True}
        store.clear()
        graph.update_state(
            store.config, {"messages": [], "local": {"model": "chosen"}}, as_node="approval"
        )
    with SessionStore(path) as store:
        graph = builder.compile(checkpointer=store.saver)
        state = graph.get_state(store.config)
        assert state.values["local"] == {"model": "chosen"}
        assert not state.next
        assert not state.tasks


def test_files_private_and_exception_close_releases_owner(tmp_path: Path) -> None:
    path = tmp_path / "session.sqlite"
    with pytest.raises(RuntimeError, match="test failure"), SessionStore(path) as store:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for item in tmp_path.iterdir():
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
        raise RuntimeError("test failure")
    with SessionStore(path):
        pass
    store.close()


@pytest.mark.parametrize("target", ["database", "lock", "wal", "directory"])
def test_symbolic_links_rejected_without_modifying_target(tmp_path: Path, target: str) -> None:
    preserved = tmp_path / "preserved"
    preserved.write_text("leave untouched")
    path = tmp_path / "session.sqlite"
    if target == "directory":
        actual = tmp_path / "actual"
        actual.mkdir()
        (tmp_path / "linked").symlink_to(actual, target_is_directory=True)
        path = tmp_path / "linked/session.sqlite"
    else:
        suffix = {"database": "", "lock": ".lock", "wal": "-wal"}[target]
        Path(str(path) + suffix).symlink_to(preserved)
    with pytest.raises(SessionError):
        SessionStore(path)
    assert preserved.read_text() == "leave untouched"


@pytest.mark.parametrize(
    "corruption", ["not-sqlite", "empty-database", "unknown-version", "foreign-database"]
)
def test_corrupt_or_unknown_database_refused_and_lock_released(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / "session.sqlite"
    if corruption == "empty-database":
        path.touch()
    elif corruption == "not-sqlite":
        path.write_bytes(b"not a SQLite database")
    elif corruption == "unknown-version":
        with SessionStore(path):
            pass
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=999")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
    original = path.read_bytes()
    with pytest.raises(SessionError):
        SessionStore(path)
    assert path.read_bytes() == original
    path.unlink()
    with SessionStore(path):
        pass


def test_serializer_preserves_reasoning_usage_and_untrusted_json_as_data() -> None:
    serializer = SessionJsonSerializer()
    values = [
        HumanMessage(content="中文输入"),
        SystemMessage(content="工作摘要"),
        AIMessage(
            content="",
            additional_kwargs={"native": {"reasoning_content": "retained", "output": []}},
            tool_calls=[{"name": "marker", "args": {}, "id": "call-1", "type": "tool_call"}],
            usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        ),
        ToolMessage(content="中文结果", tool_call_id="call-1"),
        {"kind": "message", "value": {"type": "evil"}},
    ]
    encoded = serializer.dumps_typed(values)
    assert serializer.loads_typed(encoded) == values
    assert "retained" in encoded[1].decode()


@pytest.mark.parametrize("value", [object(), b"bytes", {1: "bad-key"}, float("nan")])
def test_serializer_does_not_serialize_arbitrary_objects(value: Any) -> None:
    serializer = SessionJsonSerializer()
    with pytest.raises(SessionError):
        serializer.dumps_typed({"nested": value})
    with pytest.raises(SessionError):
        serializer.dumps_typed(AIMessage(content="", additional_kwargs={"object": value}))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"kind":"dict","kind":"message","value":{}}',
        b'{"kind":"constructor","value":{"module":"os","method":"system"}}',
        b'{"kind":"message","value":{"type":"ai","content":"incomplete"}}',
        b'{"kind":"dict","value":{"number":NaN}}',
    ],
)
def test_serializer_rejects_malformed_or_executable_payloads(payload: bytes) -> None:
    serializer = SessionJsonSerializer()
    type_name = serializer.dumps_typed(None)[0]
    with pytest.raises(SessionError):
        serializer.loads_typed((type_name, payload))
    with pytest.raises(SessionError, match="不支持"):
        serializer.loads_typed(("pickle", payload))


def test_corrupt_latest_checkpoint_is_not_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / "session.sqlite"
    assert _child(path, "write").returncode == 0
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE checkpoints SET type='pickle', checkpoint=?", (b"unsafe",))
    with pytest.raises(SessionError, match="不支持"):
        SessionStore(path)
    assert _child(path, "write").returncode == 9


@pytest.mark.parametrize("corruption", ["missing-table", "broken-metadata"])
def test_missing_records_and_corrupt_metadata_fail_safely(tmp_path: Path, corruption: str) -> None:
    path = tmp_path / "session.sqlite"
    assert _child(path, "write").returncode == 0
    with sqlite3.connect(path) as connection:
        if corruption == "missing-table":
            connection.execute("DROP TABLE writes")
        else:
            connection.execute("UPDATE checkpoints SET metadata=?", (b"{broken",))
    with pytest.raises(SessionError):
        SessionStore(path)


def test_hardlinked_database_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first.sqlite"
    first.write_bytes(b"preserve")
    linked = tmp_path / "linked.sqlite"
    os.link(first, linked)
    with pytest.raises(SessionError, match="普通文件"):
        SessionStore(linked)
    assert first.read_bytes() == b"preserve"
