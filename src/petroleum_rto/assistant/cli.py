"""Terminal interface for the unified native-tool engineering agent."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

from .turn import AgentTurn


class TerminalRuntime(Protocol):
    def handle(
        self,
        message: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> AgentTurn: ...


def _new_runtime() -> TerminalRuntime:
    """Load optional framework dependencies only when starting the Agent."""

    from petroleum_rto.domain_model.chat_settings import load_dmx_chat_settings
    from petroleum_rto.domain_model.models import ModelSelection, model_profile
    from petroleum_rto.domain_model.native import DmxNativeModel, NativeTransport

    from .native_tools import AgentDomainTools
    from .react import SYSTEM_PROMPT, ReactAgent
    from .session import SessionStore

    workspace = Path.cwd().resolve()
    settings = load_dmx_chat_settings()
    if not settings.url.endswith("/chat/completions"):
        raise ValueError("DMX配置应包含标准Chat端点，用于确定共享API根地址。")
    transport = NativeTransport(
        settings.api_key, base_url=settings.url.removesuffix("/chat/completions")
    )
    store: SessionStore | None = None
    try:
        store = SessionStore(workspace / "runs/assistant/steady-session.sqlite")
        model = DmxNativeModel(
            transport=transport,
            selection=ModelSelection(model_profile(settings.model)),
            use_stream=True,
        )
        return ReactAgent(
            model,
            AgentDomainTools(workspace),
            store=store,
            system_prompt=SYSTEM_PROMPT + "\n" + (settings.system_prompt or ""),
        )
    except BaseException:
        try:
            if store is not None:
                store.close()
        finally:
            transport.close()
        raise


def _write_safe_error(stream: TextIO, message: str) -> None:
    print(f"错误：{message}", file=stream)


def _run_repl(
    runtime: TerminalRuntime,
    *,
    input_stream: TextIO,
    output: TextIO,
    error: TextIO,
) -> int:
    interactive = (
        input_stream is sys.stdin
        and output is sys.stdout
        and input_stream.isatty()
        and output.isatty()
    )
    if interactive and sys.platform != "win32":
        try:
            # Importing readline enables native editing for input(), not TextIO.readline().
            import readline  # noqa: F401
        except ImportError:
            _write_safe_error(error, "当前Python环境缺少终端行编辑支持，请使用包含readline的环境。")
            return 1
    # Windows input() uses the native console editor without the Unix readline module.
    startup = getattr(runtime, "startup", None)
    if callable(startup):
        for message in startup():
            print(message, file=output)
    print("输入 /help 查看命令。", file=output)

    text_started = False
    text_open = False

    def finish_text() -> None:
        nonlocal text_open
        if text_open:
            print(file=output, flush=True)
            text_open = False

    def report_progress(message: str) -> None:
        finish_text()
        print(message, file=output, flush=True)

    def report_text(fragment: str) -> None:
        nonlocal text_started, text_open
        if not fragment:
            return
        if not text_started:
            print("模型> ", end="", file=output)
            text_started = True
        print(fragment, end="", file=output, flush=True)
        text_open = not fragment.endswith("\n")

    while True:
        if interactive:
            try:
                # Keep the existing runtime contract: a blank line is "\n", EOF is "".
                line = input("你> ") + "\n"
            except EOFError:
                line = ""
        else:
            print("你> ", end="", file=output, flush=True)
            line = input_stream.readline()
        if line == "":
            print(file=output)
            return 0
        text_started = False
        try:
            turn = (
                runtime.handle(line, on_progress=report_progress, on_text=report_text)
                if interactive
                else runtime.handle(line)
            )
        finally:
            finish_text()
        if interactive and turn.text_streamed and turn.errors:
            print("本次模型回答未完整生成。", file=output, flush=True)
        for message in turn.outputs:
            if interactive and message in turn.streamed_outputs:
                continue
            if interactive and turn.text_streamed and message.startswith("模型> "):
                continue
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
        from .session import SessionError
    except ImportError:
        _write_safe_error(sys.stderr, "无法启动对话，请检查领域模型运行依赖。")
        return 1
    try:
        runtime = _new_runtime()
    except SessionError as exc:
        _write_safe_error(sys.stderr, str(exc))
        return 1
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
    except SessionError as exc:
        _write_safe_error(sys.stderr, str(exc))
        return 1
    except KeyboardInterrupt:
        print(file=sys.stdout)
        return 0
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
