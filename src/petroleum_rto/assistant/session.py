"""One local LangGraph session with an exclusive owner and JSON-only checkpoints."""

from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import stat
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Interrupt, Send

DEFAULT_SESSION_PATH = Path(__file__).resolve().parents[3] / "runs/assistant/session.sqlite"
SESSION_THREAD_ID = "current"
_APPLICATION_ID = 0x52544F41
_DATABASE_VERSION = 1
_SERIALIZER_TYPE = "rto-session-json-v1"
_MESSAGE_TYPES: dict[str, type[BaseMessage]] = {
    "ai": AIMessage,
    "human": HumanMessage,
    "system": SystemMessage,
    "tool": ToolMessage,
}


class SessionError(ValueError):
    """A local session cannot safely be opened, saved, or reconstructed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _invalid_data() -> SessionError:
    return SessionError("invalid-session-data", "本机会话包含损坏或不支持的数据，未恢复执行。")


def _plain_json(value: Any) -> Any:
    """Validate message fields before any Pydantic serialization can coerce objects."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_plain_json(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _plain_json(item) for key, item in value.items()}
    raise _invalid_data()


def _encode(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_encode(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        # Every dictionary is tagged, so user JSON can never impersonate an object tag.
        return {"kind": "dict", "value": {key: _encode(item) for key, item in value.items()}}
    if type(value) is tuple:
        return {"kind": "tuple", "value": [_encode(item) for item in value]}
    if type(value) in _MESSAGE_TYPES.values():
        if value.model_extra:
            raise _invalid_data()
        data = {field: getattr(value, field) for field in type(value).model_fields}
        return {"kind": "message", "value": _plain_json(data)}
    if type(value) is Interrupt:
        return {"kind": "interrupt", "value": {"id": value.id, "value": _encode(value.value)}}
    if type(value) is Send and value.timeout is None:
        return {"kind": "send", "value": {"node": value.node, "arg": _encode(value.arg)}}
    raise _invalid_data()


def _decode(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_decode(item) for item in value]
    if type(value) is not dict or set(value) != {"kind", "value"}:
        raise _invalid_data()
    kind, payload = value["kind"], value["value"]
    if kind == "dict" and type(payload) is dict:
        return {key: _decode(item) for key, item in payload.items()}
    if kind == "tuple" and type(payload) is list:
        return tuple(_decode(item) for item in payload)
    if kind == "message" and type(payload) is dict:
        message_type = payload.get("type")
        if type(message_type) is not str or message_type not in _MESSAGE_TYPES:
            raise _invalid_data()
        message_class = _MESSAGE_TYPES[message_type]
        if set(payload) != set(message_class.model_fields):
            raise _invalid_data()
        return message_class.model_validate(_plain_json(payload), strict=True)
    if (
        kind == "interrupt"
        and type(payload) is dict
        and set(payload) == {"id", "value"}
        and type(payload["id"]) is str
    ):
        return Interrupt(value=_decode(payload["value"]), id=payload["id"])
    if (
        kind == "send"
        and type(payload) is dict
        and set(payload) == {"node", "arg"}
        and type(payload["node"]) is str
    ):
        return Send(payload["node"], _decode(payload["arg"]))
    raise _invalid_data()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_data()
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise _invalid_data()


class SessionJsonSerializer:
    """Explicit framework types only; no imports, generic constructors, or pickle."""

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return _SERIALIZER_TYPE, json.dumps(
                _encode(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            raise _invalid_data() from exc

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if data[0] != _SERIALIZER_TYPE:
            raise SessionError("unsupported-session-format", "不支持此本机会话格式，未恢复执行。")
        try:
            return _decode(
                json.loads(
                    data[1], object_pairs_hook=_unique_object, parse_constant=_reject_constant
                )
            )
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            raise _invalid_data() from exc


def _private_file(path: Path) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SessionError("unsafe-session-path", "会话文件必须是当前用户拥有的普通文件。")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class SessionStore:
    """Hold the single-session lock until the owning CLI closes or exits."""

    def __init__(self, path: Path = DEFAULT_SESSION_PATH) -> None:
        self.path = path.absolute()
        self._lock_fd: int | None = None
        self._connection: sqlite3.Connection | None = None
        self.saver: SqliteSaver
        try:
            for parent in (self.path, *self.path.parents):
                if parent.is_symlink():
                    raise SessionError("unsafe-session-path", "会话路径不能包含符号链接。")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._lock_fd = _private_file(self.path.with_suffix(self.path.suffix + ".lock"))
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionError(
                    "session-in-use", "另一个助手进程正在使用本机会话；请先退出该进程。"
                ) from exc
            database_existed = self.path.exists()
            descriptor = _private_file(self.path)
            database_size = os.fstat(descriptor).st_size
            os.close(descriptor)
            if database_existed and database_size == 0:
                raise SessionError(
                    "invalid-session-database", "已有本机会话数据库为空或被截断，未恢复执行。"
                )
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = self.path.with_name(self.path.name + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    descriptor = _private_file(sidecar)
                    os.close(descriptor)
            connection = sqlite3.connect(self.path, check_same_thread=False, timeout=0)
            self._connection = connection
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise SessionError("invalid-session-database", "本机会话数据库损坏，未恢复执行。")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if application_id == 0 and version == 0 and not tables:
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_DATABASE_VERSION}")
            elif application_id != _APPLICATION_ID or version != _DATABASE_VERSION:
                raise SessionError(
                    "unsupported-session-format", "不支持此本机会话版本，未恢复执行。"
                )
            elif {row[0] for row in tables} != {"checkpoints", "writes"}:
                raise SessionError(
                    "invalid-session-database",
                    "本机会话数据库缺少必要记录或结构不兼容，未恢复执行。",
                )
            self.saver = SqliteSaver(connection, serde=SessionJsonSerializer())
            self.saver.get_tuple(self.config)
        except SessionError:
            self.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise SessionError(
                "session-storage-error", "本机会话存储无法安全打开或已损坏，未恢复执行。"
            ) from exc
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            self.close()
            raise _invalid_data() from exc
        except BaseException:
            self.close()
            raise

    @property
    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": SESSION_THREAD_ID, "checkpoint_ns": ""}}

    def clear(self) -> None:
        """Delete every checkpoint and pending write; the caller saves the new state."""
        self.saver.delete_thread(SESSION_THREAD_ID)

    def close(self) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
