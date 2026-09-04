"""Terminal interface for the minimal confirmation-gated engineering agent."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from petroleum_rto.rto.runtime import build_intent_communication_service

from .dmx_intent_adapter import DmxIntentAdapter
from .runtime import AgentRuntime
from .tools import AgentTools


def _new_runtime() -> AgentRuntime:
    """Compose one process-local Agent around a shared stateless DMX client."""

    from petroleum_rto.domain_model.chat import DmxChatClient, DmxChatSession

    workspace = Path.cwd().resolve()
    client = DmxChatClient.from_local_config()
    return AgentRuntime(
        DmxChatSession(client),
        AgentTools(workspace),
        build_intent_communication_service(repo_root=workspace),
        DmxIntentAdapter(client),
    )


def _write_safe_error(stream: TextIO, message: str) -> None:
    print(f"错误：{message}", file=stream)


def _run_repl(
    runtime: AgentRuntime,
    *,
    input_stream: TextIO,
    output: TextIO,
    error: TextIO,
) -> int:
    print("输入 /help 查看命令。", file=output)
    while True:
        print("你> ", end="", file=output, flush=True)
        line = input_stream.readline()
        if line == "":
            print(file=output)
            return 0
        turn = runtime.handle(line)
        for message in turn.outputs:
            print(message, file=output)
        for message in turn.errors:
            _write_safe_error(error, message)
        if turn.should_exit:
            return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        _write_safe_error(sys.stderr, "该命令无需参数；启动后输入 /help 查看命令。")
        return 2
    try:
        runtime = _new_runtime()
    except Exception:  # noqa: BLE001 - configuration exceptions may contain a credential
        _write_safe_error(sys.stderr, "无法启动对话，请检查本地DMX配置。")
        return 1
    try:
        return _run_repl(
            runtime,
            input_stream=sys.stdin,
            output=sys.stdout,
            error=sys.stderr,
        )
    except KeyboardInterrupt:
        print(file=sys.stdout)
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
