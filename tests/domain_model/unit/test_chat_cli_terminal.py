"""Exercise submitted input through a real terminal, without loading a model."""

from __future__ import annotations

import errno
import json
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("terminal editing requires a Unix PTY", allow_module_level=True)

import fcntl
import pty
import termios

_PROMPT = "你> ".encode()
_TURN_MARKER = b"__CLI_TURN_COMPLETE__"
_RESULT_MARKER = b"__CLI_RESULT__"
_PROGRESS_MARKER = "M2搜索：已评价候选1。".encode()
_TEXT_MARKER = "先到的中文🙂".encode()
_CHILD = r"""
import fcntl
import json
import os
import sys
import termios

fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
sys.path.insert(0, sys.argv[1])
from petroleum_rto.assistant import cli
from petroleum_rto.assistant.turn import AgentTurn

progress_fd = int(sys.argv[2]) if len(sys.argv) > 2 else None
text_mode = sys.argv[3] if len(sys.argv) > 3 else None
if progress_fd is not None:
    # Require explicit callback flushing, even though stdout is a terminal.
    sys.stdout.reconfigure(line_buffering=False, write_through=False)

class Recorder:
    def __init__(self):
        self.messages = []
        self.closed = False

    def handle(self, message, *, on_progress=None, on_text=None):
        self.messages.append(message)
        assert on_progress is not None
        assert on_text is not None
        if text_mode:
            on_text("先到的中文🙂")
            os.read(progress_fd, 1)
            if text_mode == "failure":
                return AgentTurn(
                    outputs=("受信结果仍保留。", "__CLI_TURN_COMPLETE__"),
                    errors=("模型调用失败。",),
                    text_streamed=True,
                )
            on_progress("M2搜索：已评价候选1。")
            on_text("后到的正文。")
            return AgentTurn(
                outputs=("模型> 先到的中文🙂后到的正文。", "受信结果仍保留。", "__CLI_TURN_COMPLETE__"),
                text_streamed=True,
            )
        on_progress("M2搜索：已评价候选1。")
        if progress_fd is not None:
            os.read(progress_fd, 1)
        return AgentTurn(
            outputs=("__CLI_TURN_COMPLETE__",),
            should_exit=message.strip() == "/exit",
        )

    def close(self):
        self.closed = True

runtime = Recorder()
cli._new_runtime = lambda: runtime
code = cli.main([])
print("\n__CLI_RESULT__" + json.dumps({
    "messages": runtime.messages, "closed": runtime.closed, "code": code,
}), flush=True)
raise SystemExit(code)
"""


def _read_until(master: int, pending: bytes, marker: bytes) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + 5
    while marker not in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
            pytest.fail(f"terminal timed out waiting for {marker!r}; output={pending!r}")
        try:
            chunk = os.read(master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            chunk = b""
        if not chunk:
            pytest.fail(f"terminal closed before {marker!r}; output={pending!r}")
        pending += chunk
    before, after = pending.split(marker, 1)
    return before, after


def _run_terminal(
    repo_root: Path,
    entries: list[bytes],
    *,
    columns: int = 80,
    finish: bytes = b"/exit\r",
) -> dict[str, object]:
    master, slave = pty.openpty()
    child: subprocess.Popen[bytes] | None = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        child = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", _CHILD, str(repo_root / "src")],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "LC_ALL": "C.UTF-8",
                "INPUTRC": os.devnull,
            },
        )
        os.close(slave)
        slave = -1
        _, pending = _read_until(master, b"", _PROMPT)
        for entry in entries:
            os.write(master, entry + b"\r")
            _, pending = _read_until(master, pending, _TURN_MARKER)
            _, pending = _read_until(master, pending, _PROMPT)
        os.write(master, finish)
        _, pending = _read_until(master, pending, _RESULT_MARKER)
        result_line, _ = _read_until(master, pending, b"\n")
        result = json.loads(result_line)
        assert child.wait(timeout=5) == 0
        assert isinstance(result, dict)
        return result
    finally:
        try:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        finally:
            os.close(master)
            if slave != -1:
                os.close(slave)


@pytest.mark.parametrize(
    ("keys", "expected", "columns"),
    [
        pytest.param(
            "错字".encode() + b"\x7f\x7f" + "现在".encode(),
            "现在\n",
            80,
            id="two-chinese-characters-two-deletes",
        ),
        pytest.param(b"wrong" + b"\x7f" * 5 + b"correct", "correct\n", 80, id="ascii-del"),
        pytest.param(b"bad\x08\x08\x08good", "good\n", 80, id="ascii-backspace"),
        pytest.param(
            b"abcd\x1b[D\x1b[D\x1b[3~\x1b[CX",
            "abdX\n",
            80,
            id="left-right-and-forward-delete",
        ),
        pytest.param(
            ("汉" * 12 + "错").encode() + b"\x7f" + "现在".encode(),
            "汉" * 12 + "现在\n",
            20,
            id="wrapped-chinese-input",
        ),
    ],
)
def test_terminal_edits_are_applied_before_dispatch(
    repo_root: Path, keys: bytes, expected: str, columns: int
) -> None:
    # Captured messages prove input integrity, not the terminal's visual redraw.
    assert _run_terminal(repo_root, [keys], columns=columns) == {
        "messages": [expected, "/exit\n"],
        "closed": True,
        "code": 0,
    }


def test_terminal_empty_line_does_not_end_the_session(repo_root: Path) -> None:
    assert _run_terminal(repo_root, [b"", "你好".encode()]) == {
        "messages": ["\n", "你好\n", "/exit\n"],
        "closed": True,
        "code": 0,
    }


def test_terminal_plain_multiline_input_is_dispatched_as_separate_turns(repo_root: Path) -> None:
    assert _run_terminal(repo_root, ["第一行\r第二行".encode()]) == {
        "messages": ["第一行\n", "第二行\n", "/exit\n"],
        "closed": True,
        "code": 0,
    }


@pytest.mark.parametrize(
    "finish",
    [
        pytest.param(b"\x04", id="eof"),
        pytest.param("未提交".encode() + b"\x03", id="interrupt-pending-input"),
    ],
)
def test_terminal_eof_and_interrupt_close_without_dispatch(repo_root: Path, finish: bytes) -> None:
    assert _run_terminal(repo_root, [], finish=finish) == {
        "messages": [],
        "closed": True,
        "code": 0,
    }


@pytest.mark.parametrize(
    ("columns", "interrupt", "text_mode"),
    [
        pytest.param(80, False, None, id="normal-terminal"),
        pytest.param(20, False, None, id="narrow-terminal"),
        pytest.param(20, True, None, id="interrupt-during-progress"),
        pytest.param(80, False, "success", id="text-before-completion-with-progress"),
        pytest.param(20, False, "failure", id="partial-text-before-failure"),
    ],
)
def test_terminal_progress_and_text_are_flushed_before_handle_finishes(
    repo_root: Path, columns: int, interrupt: bool, text_mode: str | None
) -> None:
    master, slave = pty.openpty()
    release_read, release_write = os.pipe()
    child: subprocess.Popen[bytes] | None = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", _CHILD, str(repo_root / "src"), str(release_read)]
            + ([text_mode] if text_mode else []),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            pass_fds=(release_read,),
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "LC_ALL": "C.UTF-8",
                "INPUTRC": os.devnull,
            },
        )
        os.close(slave)
        slave = -1
        before, pending = _read_until(master, b"", _PROMPT)
        transcript = before + _PROMPT
        os.write(master, "确认\r".encode())
        first_marker = _TEXT_MARKER if text_mode else _PROGRESS_MARKER
        before, pending = _read_until(master, pending, first_marker)
        transcript += before + first_marker
        # The child cannot return from handle until this test releases the pipe.
        assert child.poll() is None
        assert _TURN_MARKER not in transcript + pending
        assert _RESULT_MARKER not in transcript + pending
        if interrupt:
            os.write(master, b"\x03")
        else:
            os.write(release_write, b"1")
            before, pending = _read_until(master, pending, _PROMPT)
            transcript += before + _PROMPT
            os.write(master, b"\x04")
        before, pending = _read_until(master, pending, _RESULT_MARKER)
        transcript += before + _RESULT_MARKER
        result_line, _ = _read_until(master, pending, b"\n")
        assert json.loads(result_line) == {
            "messages": ["确认\n"],
            "closed": True,
            "code": 0,
        }
        assert child.wait(timeout=5) == 0
        assert transcript.count(_PROGRESS_MARKER) == (0 if text_mode == "failure" else 1)
        assert transcript.count(_TURN_MARKER) == (0 if interrupt else 1)
        if text_mode:
            assert transcript.count(_TEXT_MARKER) == 1
            assert transcript.count("模型> ".encode()) == 1
            assert transcript.count("后到的正文。".encode()) == (1 if text_mode == "success" else 0)
            assert ("本次模型回答未完整生成。".encode() in transcript) is (text_mode == "failure")
            assert "受信结果仍保留。".encode() in transcript
    finally:
        try:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        finally:
            os.close(release_read)
            os.close(release_write)
            os.close(master)
            if slave != -1:
                os.close(slave)
