"""Native Chat/Responses transport and LangChain model, preserving provider state."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from .credentials import contains_credential
from .models import ModelSelection


class NativeModelError(RuntimeError):
    """Only safe, local messages cross the CLI boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NativeModelError("invalid-response")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise NativeModelError("invalid-response")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("non-finite JSON")


def _json(value: str | bytes) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise NativeModelError("invalid-response") from None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def request_payload(
    selection: ModelSelection,
    messages: list[BaseMessage],
    tools: list[dict[str, Any]],
    *,
    stream: bool,
    check_capacity: bool = True,
) -> dict[str, Any]:
    """Build a complete native request and reject orphan/cross-model tool state."""
    profile = selection.profile
    pending: set[str] = set()
    seen_calls: set[str] = set()
    records: list[dict[str, Any]] = []
    for message in messages:
        if pending and not isinstance(message, ToolMessage):
            raise NativeModelError("unfinished-tool-call")
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise NativeModelError("orphan-tool-result")
            pending.remove(message.tool_call_id)
            records.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
                if profile.protocol == "chat"
                else {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
        elif isinstance(message, AIMessage):
            if message.additional_kwargs.get("dmx_model_id") != profile.model_id:
                raise NativeModelError("foreign-model-history")
            for call in message.tool_calls:
                call_id = call.get("id")
                if not call_id or call_id in seen_calls:
                    raise NativeModelError("invalid-tool-call-id")
                pending.add(call_id)
                seen_calls.add(call_id)
            raw = message.additional_kwargs["dmx_native"]
            if profile.protocol == "chat":
                raw = _object(raw)
                if (
                    tools
                    and selection.thinking_enabled
                    and profile.model_id == "deepseek-v4-pro-0813"
                    and not isinstance(raw.get("reasoning_content"), str)
                ):
                    # DeepSeek requires reasoning from every earlier assistant turn
                    # when tools are present, including final answers without calls.
                    raise NativeModelError("missing-reasoning-history")
                records.append(raw)
            else:
                if not isinstance(raw, list):
                    raise NativeModelError("invalid-history")
                records.extend(raw)
        elif isinstance(message, (HumanMessage, SystemMessage)):
            records.append(
                {
                    "role": "user" if isinstance(message, HumanMessage) else "system",
                    "content": message.content,
                }
            )
        else:
            raise NativeModelError("invalid-history")
    if pending:
        raise NativeModelError("unfinished-tool-call")
    payload: dict[str, Any] = {
        "model": profile.model_id,
        "stream": stream,
        **selection.parameters(),
    }
    if profile.protocol == "chat":
        payload.update(messages=records, tools=tools, max_tokens=selection.output_tokens)
        if stream:
            payload["stream_options"] = {"include_usage": True}
    else:
        payload.update(
            input=records,
            tools=[{"type": "function", **t["function"]} for t in tools],
            max_output_tokens=selection.output_tokens,
            store=False,
            include=["reasoning.encrypted_content"],
        )
    if not tools:
        payload.pop("tools")
    window = profile.context_tokens
    if window is None:
        raise NativeModelError("unknown-model-capacity")
    # Conservative text-only estimate: one UTF-8 byte per token, plus per-record
    # framing allowance. This is not a vendor tokenizer or a claimed token count.
    # Includes schemas and opaque reasoning, runs again after every tool result.
    estimated = request_size(payload)
    if check_capacity and estimated + selection.output_tokens > window:
        raise NativeModelError("context-overflow")
    return payload


def request_size(payload: dict[str, Any]) -> int:
    """Conservative wire-byte estimate shared by compaction and the final guard."""
    records = payload.get("messages", payload.get("input", []))
    return len(_dump(payload).encode("utf-8")) + 64 * (
        len(records) + len(payload.get("tools", [])) + 1
    )


def _calls(raw: list[Any], *, responses: bool) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    ids: set[str] = set()
    for value in raw:
        item = _object(value)
        if item.get("type") != ("function_call" if responses else "function"):
            raise NativeModelError("unsupported-tool-type")
        function = item if responses else _object(item.get("function"))
        call_id = _text(item.get("call_id" if responses else "id"))
        if call_id in ids:
            raise NativeModelError("invalid-tool-call-id")
        ids.add(call_id)
        args = _json(_text(function.get("arguments")))
        if not isinstance(args, dict):
            raise NativeModelError("invalid-tool-arguments")
        calls.append(
            {"name": _text(function.get("name")), "args": args, "id": call_id, "type": "tool_call"}
        )
    return calls


def parse_response(selection: ModelSelection, payload: dict[str, Any]) -> AIMessage:
    """Retain raw assistant/output items, usage, stop reason and declared model."""
    profile = selection.profile
    raw: Any
    if profile.protocol == "chat":
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise NativeModelError("invalid-response")
        choice = _object(choices[0])
        reason = choice.get("finish_reason")
        if reason not in ("stop", "tool_calls"):
            raise NativeModelError("incomplete-response")
        raw = _object(choice.get("message"))
        if raw.get("role") != "assistant":
            raise NativeModelError("invalid-response")
        content = raw.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise NativeModelError("unsupported-content")
        raw_calls = raw.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise NativeModelError("invalid-response")
        calls = _calls(raw_calls, responses=False)
        if bool(calls) != (reason == "tool_calls"):
            raise NativeModelError("invalid-response")
        reasoning = raw.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise NativeModelError("invalid-reasoning")
        if profile.thinking_parameter == "always" and not reasoning:
            raise NativeModelError("missing-reasoning")
        if calls and selection.thinking_enabled and reasoning is None:
            raise NativeModelError("missing-reasoning")
    else:
        if payload.get("status") != "completed":
            raise NativeModelError("incomplete-response")
        raw = payload.get("output")
        if not isinstance(raw, list):
            raise NativeModelError("invalid-response")
        chunks: list[str] = []
        functions: list[Any] = []
        for item in raw:
            item = _object(item)
            kind = item.get("type")
            if kind == "message":
                if item.get("role") != "assistant" or item.get("status") != "completed":
                    raise NativeModelError("incomplete-response")
                for part in item.get("content", []):
                    part = _object(part)
                    if part.get("type") == "output_text":
                        chunks.append(_text(part.get("text")))
                    elif part.get("type") == "refusal":
                        chunks.append(_text(part.get("refusal")))
                    else:
                        raise NativeModelError("unsupported-content")
            elif kind == "function_call":
                if item.get("status", "completed") != "completed":
                    raise NativeModelError("incomplete-response")
                functions.append(item)
            elif kind == "reasoning":
                if (
                    not isinstance(item.get("encrypted_content"), str)
                    or not item["encrypted_content"]
                ):
                    raise NativeModelError("missing-reasoning")
            else:
                raise NativeModelError("unsupported-output-item")
        content = "".join(chunks)
        calls = _calls(functions, responses=True)
        reason = "completed"
    if not content.strip() and not calls:
        raise NativeModelError("empty-response")
    return AIMessage(
        content=content,
        tool_calls=calls,
        additional_kwargs={"dmx_model_id": profile.model_id, "dmx_native": raw},
        response_metadata={
            "requested_model": profile.model_id,
            "response_model": payload.get("model"),
            "response_id": payload.get("id"),
            "finish_reason": reason,
            "usage": payload.get("usage"),
        },
    )


class NativeTransport:
    """No automatic retry, redirects, ambient proxy, logging or credential reflection."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://www.dmxapi.cn/v1",
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://") or base_url != base_url.strip():
            raise ValueError("DMX地址必须使用HTTPS。")
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=180,
            follow_redirects=False,
            trust_env=False,
            transport=http_transport,
        )

    def close(self) -> None:
        self._client.close()

    def complete(self, selection: ModelSelection, payload: dict[str, Any]) -> AIMessage:
        if contains_credential(payload, self._api_key):
            raise NativeModelError("credential-in-request")
        endpoint = "chat/completions" if selection.profile.protocol == "chat" else "responses"
        # Response bytes are a transport resource cap proportional to the requested
        # generation budget; JSON escaping and SSE framing need more than 4 bytes/token.
        max_bytes = selection.output_tokens * 256
        try:
            with self._client.stream(
                "POST", endpoint, json=payload, headers={"Authorization": f"Bearer {self._api_key}"}
            ) as response:
                if response.status_code != 200:
                    code = {
                        401: "authentication-failed",
                        403: "permission-denied",
                        429: "rate-limited",
                    }.get(response.status_code, "http-error")
                    raise NativeModelError(code)
                if payload["stream"]:
                    raw = self._stream_response(selection, response, max_bytes)
                else:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise NativeModelError("response-too-large")
                    raw = _object(_json(bytes(body)))
                if contains_credential(raw, self._api_key):
                    raise NativeModelError("credential-in-response")
                return parse_response(selection, raw)
        except NativeModelError:
            raise
        except httpx.RequestError:
            raise NativeModelError("transport-failed") from None
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise NativeModelError("invalid-response") from None

    @staticmethod
    def _events(response: httpx.Response, max_bytes: int) -> Iterator[str]:
        # Bound bytes before line splitting, including a malicious unterminated line.
        buffer = bytearray()
        total = 0
        data: list[str] = []
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise NativeModelError("response-too-large")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                text = line.rstrip(b"\r").decode("utf-8")
                if not text:
                    if data:
                        yield "\n".join(data)
                        data = []
                elif text.startswith("data:"):
                    data.append(text[5:].lstrip(" "))
        if buffer or data:
            raise NativeModelError("incomplete-stream")

    def _stream_response(
        self, selection: ModelSelection, response: httpx.Response, max_bytes: int
    ) -> dict[str, Any]:
        if selection.profile.protocol == "responses":
            added: dict[int, dict[str, Any]] = {}
            completed: dict[int, dict[str, Any]] = {}
            for event in self._events(response, max_bytes):
                item = _object(_json(event))
                kind = item.get("type")
                if kind in ("response.output_item.added", "response.output_item.done"):
                    index = item.get("output_index")
                    if type(index) is not int or index < 0:
                        raise NativeModelError("invalid-response")
                    output = _object(item.get("item"))
                    _text(output.get("id"))
                    _text(output.get("type"))
                    records = added if kind == "response.output_item.added" else completed
                    if index in records or (records is added and index in completed):
                        raise NativeModelError("invalid-response")
                    if records is completed and index in added:
                        for key in ("id", "type", "call_id", "name"):
                            if key in added[index] and output.get(key) != added[index][key]:
                                raise NativeModelError("invalid-response")
                    records[index] = output
                if item.get("type") == "response.completed":
                    terminal_result = _object(item.get("response"))
                    terminal_output = terminal_result.get("output")
                    if not isinstance(terminal_output, list):
                        raise NativeModelError("invalid-response")
                    if added or completed:
                        if not added.keys() <= completed.keys() or sorted(completed) != list(
                            range(len(completed))
                        ):
                            raise NativeModelError("incomplete-stream")
                        outputs = [completed[i] for i in sorted(completed)]
                        if len({value["id"] for value in outputs}) != len(outputs):
                            raise NativeModelError("invalid-response")
                        # DMX may emit complete done items but an empty terminal output.
                        # Only finalized items are used; a conflicting terminal is rejected.
                        if terminal_output and terminal_output != outputs:
                            raise NativeModelError("invalid-response")
                        terminal_result["output"] = outputs
                    return terminal_result
                if item.get("type") in ("error", "response.failed", "response.incomplete"):
                    raise NativeModelError("incomplete-stream")
            raise NativeModelError("incomplete-stream")
        message: dict[str, Any] = {"role": "assistant", "content": ""}
        calls: dict[int, dict[str, Any]] = {}
        result: dict[str, Any] = {}
        finish: str | None = None
        for event in self._events(response, max_bytes):
            if event == "[DONE]":
                if finish is None:
                    raise NativeModelError("incomplete-stream")
                if calls:
                    if sorted(calls) != list(range(len(calls))):
                        raise NativeModelError("invalid-response")
                    message["tool_calls"] = [calls[i] for i in sorted(calls)]
                result["choices"] = [{"message": message, "finish_reason": finish}]
                return result
            item = _object(_json(event))
            if "error" in item:
                raise NativeModelError("incomplete-stream")
            for key in ("id", "model", "usage"):
                if item.get(key) is not None:
                    result[key] = item[key]
            choices = item.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise NativeModelError("invalid-response")
            if not choices:
                continue  # usage-only terminal chunk
            choice = _object(choices[0])
            if choice.get("index", 0) != 0 or finish is not None:
                raise NativeModelError("invalid-response")
            delta = _object(choice.get("delta"))
            if delta.get("role") not in (None, "assistant"):
                raise NativeModelError("invalid-response")
            for key in ("content", "reasoning_content"):
                if delta.get(key) is not None:
                    if not isinstance(delta[key], str):
                        raise NativeModelError("invalid-response")
                    message[key] = message.get(key, "") + delta[key]
            for partial in delta.get("tool_calls") or []:
                partial = _object(partial)
                index = partial.get("index")
                if type(index) is not int or index < 0:
                    raise NativeModelError("invalid-response")
                call = calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if partial.get("type", "function") != "function":
                    raise NativeModelError("unsupported-tool-type")
                if partial.get("id"):
                    call["id"] += _text(partial["id"])
                for key, value in _object(partial.get("function", {})).items():
                    if key not in ("name", "arguments"):
                        raise NativeModelError("invalid-response")
                    # A nullable delta field means no new fragment, not a new value.
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        raise NativeModelError("invalid-response")
                    call["function"][key] += value
            finish = choice.get("finish_reason")
        raise NativeModelError("incomplete-stream")


class DmxNativeModel(BaseChatModel):
    """Small framework adapter; provider-specific opaque state stays in this layer."""

    transport: NativeTransport = Field(exclude=True, repr=False)
    selection: ModelSelection
    use_stream: bool = True

    @property
    def _llm_type(self) -> str:
        return "dmx-native"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if tool_choice not in (None, "auto"):
            raise ValueError("Only automatic tool selection is supported")
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stop:
            raise NativeModelError("unsupported-stop")
        payload = request_payload(
            self.selection, messages, kwargs.get("tools", []), stream=self.use_stream
        )
        result = self.transport.complete(self.selection, payload)
        return ChatResult(generations=[ChatGeneration(message=result)])
