"""Minimal DMX chat CLI with operating-status and RTO-result explanation."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TextIO, cast

_HELP = """可用命令：
  /result <run-dir|result.json>  读取并解释一份已有的离线RTO结果
  /clear                         清空本次内存对话
  /help                          显示帮助
  /exit                          退出
"""

_SIMULATION_CONTEXT_PATH = Path("configs/rto/contexts/case_20260604.json")

_STATUS_INSTRUCTION = (
    "以下是程序读取的当前工况数据。请先理解数据，再直接用简洁、自然的中文回答用户问题。"
    "只使用这些数据能够支持的事实，提炼相关数值、单位、状态和工艺含义，不要输出JSON，"
    "也不要复述输入结构。不得补造未提供的测量、设备、阈值、趋势、报警或执行结果。"
    "如果用户询问整体工况，概括当前模式和关键参数；如果只询问一个参数，只回答相关内容。"
    "如果同一句还要求优化或调整，只能解释目标并说明下一步需要形成优化预览；"
    "不得声称已经运行优化、改变设定值或得到新的计算结果。\n"
)

_RESULT_INSTRUCTION = (
    "以下是本地程序读取的RTO结果数据。请先理解数据，再用简洁、自然的中文直接解释结果，"
    "保留设定值、数值和单位，不要输出JSON，也不要复述输入结构。"
    "只回答用户问到的内容，不主动扩展无关说明。\n"
    "RTO结果数据："
)


class _ChatSession(Protocol):
    def ask(self, message: str) -> str: ...

    def clear(self) -> None: ...


def _new_session() -> _ChatSession:
    """Late-bound adapter point for the deliberately small DMX chat client."""

    from petroleum_rto.domain_model.chat import DmxChatClient, DmxChatSession

    client = DmxChatClient.from_local_config()
    return cast(_ChatSession, DmxChatSession(client))


def _result_run_dir(source: str) -> Path:
    path = Path(source).expanduser().resolve()
    if path.name == "result.json":
        if not path.is_file():
            raise ValueError("result.json does not exist")
        return path.parent
    if not path.is_dir():
        raise ValueError("RTO result source must be a run directory or result.json")
    return path


def _load_rto_result_summary(source: str) -> Mapping[str, object]:
    """Strictly inspect one existing run and return only its approved chat projection."""

    from petroleum_rto.rto.runtime import build_chat_result_summary, inspect_offline

    run_dir = _result_run_dir(source)
    workspace = Path.cwd().resolve()
    record = inspect_offline(
        run_dir,
        library_root=workspace / "runs" / "rto" / "strategy-library",
    )
    summary = build_chat_result_summary(record)
    if not isinstance(summary, Mapping):
        raise TypeError("RTO chat summary must be a mapping")
    return summary


def _load_simulation_status() -> Mapping[str, object]:
    """Load the configured trusted simulation context and return its safe projection."""

    from petroleum_rto.rto import load_operating_context
    from petroleum_rto.rto.runtime import build_chat_operating_status

    context_path = Path.cwd().resolve() / _SIMULATION_CONTEXT_PATH
    context = load_operating_context(context_path)
    summary = build_chat_operating_status(context)
    if not isinstance(summary, Mapping):
        raise TypeError("simulation status summary must be a mapping")
    return summary


def _normalized_summary(summary: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(summary),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("chat summary must contain finite JSON values") from exc


def _write_safe_error(stream: TextIO, message: str) -> None:
    print(f"错误：{message}", file=stream)


def _ask(session: _ChatSession, message: str, *, output: TextIO, error: TextIO) -> bool:
    try:
        reply = session.ask(message)
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("empty model response")
    except Exception:  # noqa: BLE001 - never echo provider errors or credentials at the UI boundary
        _write_safe_error(error, "模型调用失败，请检查本地配置和网络后重试。")
        return False
    print(f"模型> {reply}", file=output)
    return True


def _handle_result(
    session: _ChatSession,
    source: str,
    *,
    output: TextIO,
    error: TextIO,
) -> None:
    if not source:
        _write_safe_error(error, "请使用 /result <run-dir|result.json>。")
        return
    try:
        summary = _load_rto_result_summary(source)
        normalized = _normalized_summary(summary)
    except Exception:  # noqa: BLE001 - local paths/evidence details stay inside the boundary
        _write_safe_error(error, "RTO结果读取或严格校验失败，未向模型发送任何内容。")
        return
    print("RTO设定值概要：", file=output)
    print(json.dumps(dict(summary), ensure_ascii=False, sort_keys=True, indent=2), file=output)
    _ask(session, f"{_RESULT_INSTRUCTION}{normalized}", output=output, error=error)


def _needs_operating_context(message: str) -> bool:
    compact = "".join(message.casefold().split())
    current_markers = ("当前", "现在", "目前", "实时", "此刻")
    subjects = ("常压", "装置", "cdu")
    simulation_subjects = ("仿真软件", "模拟器", "仿真")
    measured_fields = (
        "进料",
        "处理量",
        "加工量",
        "炉温",
        "炉出口温度",
        "加热炉温度",
        "塔顶压力",
        "设定值",
        "设定点",
        "库存",
        "液位",
        "运行模式",
        "操作模式",
    )
    value_questions = (
        "是多少",
        "多少",
        "多大",
        "什么值",
        "读数",
        "正常吗",
        "高不高",
        "低不低",
        "查看",
        "查询",
        "读取",
        "显示",
    )
    simulation_state_questions = (
        "运行状态",
        "是否运行",
        "在运行",
        "在跑",
        "运行吗",
        "启动了吗",
        "在线吗",
        "空闲吗",
        "停了吗",
        "状态如何",
        "状态怎么样",
    )

    has_current = any(marker in compact for marker in current_markers)
    has_subject = any(subject in compact for subject in subjects)
    has_simulation_subject = any(subject in compact for subject in simulation_subjects)
    has_measured_field = any(field in compact for field in measured_fields)

    if "rto" in compact and not (has_subject or has_simulation_subject):
        return False
    if has_current and "工况" in compact:
        return True
    if has_current and (has_subject or has_simulation_subject) and "状态" in compact:
        return True
    if has_subject and any(
        question in compact
        for question in ("运行状态", "工况状态", "现在怎么样", "目前怎么样", "工况怎么样")
    ):
        return True
    if has_measured_field and (
        has_current
        or any(question in compact for question in value_questions)
        or compact.endswith(("呢", "呢?", "呢？"))
        or any(
            question in compact
            for question in ("运行模式是什么", "操作模式是什么", "工况模式是什么")
        )
    ):
        return True
    if has_simulation_subject and any(
        question in compact for question in simulation_state_questions
    ):
        return True
    return "currentcduoperatingstatus" in compact or "currentsimulationstatus" in compact


def _handle_status(
    session: _ChatSession,
    question: str,
    *,
    output: TextIO,
    error: TextIO,
) -> None:
    try:
        summary = _load_simulation_status()
        normalized = _normalized_summary(summary)
    except Exception:  # noqa: BLE001 - paths and trusted context details stay local
        _write_safe_error(error, "仿真工况读取或严格校验失败，未向模型发送任何内容。")
        return
    prompt = f"{_STATUS_INSTRUCTION}用户问题：{question}\n当前工况数据：{normalized}"
    _ask(session, prompt, output=output, error=error)


def _run_repl(
    session: _ChatSession,
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
        message = line.strip()
        if not message:
            continue
        if message == "/exit":
            return 0
        if message == "/help":
            print(_HELP, end="", file=output)
            continue
        if message == "/clear":
            try:
                session.clear()
            except Exception:  # noqa: BLE001 - never echo configuration or provider details
                _write_safe_error(error, "清空对话失败，请重新启动命令。")
                continue
            print("对话已清空。", file=output)
            continue
        if message == "/result" or message.startswith("/result "):
            _, _, source = message.partition(" ")
            _handle_result(session, source.strip(), output=output, error=error)
            continue
        if message.startswith("/"):
            _write_safe_error(error, "未知命令，请输入 /help 查看支持的命令。")
            continue
        if _needs_operating_context(message):
            _handle_status(
                session,
                message,
                output=output,
                error=error,
            )
            continue
        _ask(session, message, output=output, error=error)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        _write_safe_error(sys.stderr, "该命令无需参数；启动后输入 /help 查看命令。")
        return 2
    try:
        session = _new_session()
    except Exception:  # noqa: BLE001 - configuration exceptions may contain a credential
        _write_safe_error(sys.stderr, "无法启动对话，请检查本地DMX配置。")
        return 1
    try:
        return _run_repl(
            session,
            input_stream=sys.stdin,
            output=sys.stdout,
            error=sys.stderr,
        )
    except KeyboardInterrupt:
        print(file=sys.stdout)
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
