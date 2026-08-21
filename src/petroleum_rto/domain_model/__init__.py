"""Minimal DMX chat API."""

from .chat import (
    MAX_CHAT_HISTORY_BYTES,
    MAX_CHAT_HISTORY_MESSAGES,
    MAX_CHAT_REQUEST_BYTES,
    MAX_CHAT_RESPONSE_BYTES,
    DmxChatClient,
    DmxChatError,
    DmxChatSession,
)
from .chat_settings import DMX_CHAT_MODEL, DMX_CHAT_URL, DMX_SYSTEM_PROMPT

__all__ = [
    "DMX_CHAT_MODEL",
    "DMX_CHAT_URL",
    "DMX_SYSTEM_PROMPT",
    "MAX_CHAT_HISTORY_BYTES",
    "MAX_CHAT_HISTORY_MESSAGES",
    "MAX_CHAT_REQUEST_BYTES",
    "MAX_CHAT_RESPONSE_BYTES",
    "DmxChatClient",
    "DmxChatError",
    "DmxChatSession",
]
