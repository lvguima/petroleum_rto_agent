"""Terminal interface for the unified native-tool engineering agent."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, TextIO

from .turn import AgentTurn


class TerminalRuntime(Protocol):
    def handle(self, message: str) -> AgentTurn: ...


def _new_runtime() -> TerminalRuntime:
    """Load optional framework dependencies only when starting the Agent."""

    from petroleum_rto.domain_model.chat_settings import load_dmx_chat_settings
    from petroleum_rto.domain_model.models import ModelSelection, model_profile
    from petroleum_rto.domain_model.native import DmxNativeModel, NativeTransport

    from .native_tools import AgentDomainTools
    from .react import SYSTEM_PROMPT, ReactAgent

    workspace = Path.cwd().resolve()
    settings = load_dmx_chat_settings()
    if not settings.url.endswith("/chat/completions"):
        raise ValueError("DMX配置应包含标准Chat端点，用于确定共享API根地址。")
    model = DmxNativeModel(
        transport=NativeTransport(
            settings.api_key, base_url=settings.url.removesuffix("/chat/completions")
        ),
        selection=ModelSelection(model_profile(settings.model)),
    )
    return ReactAgent(
        model,
        AgentDomainTools(workspace),
        system_prompt=SYSTEM_PROMPT + "\n" + (settings.system_prompt or ""),
    )


def _write_safe_error(stream: TextIO, message: str) -> None:
    print(f"错误：{message}", file=stream)


def _run_repl(
    runtime: TerminalRuntime,
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
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
