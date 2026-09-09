"""Local model configuration; native framework dependencies load at Agent startup."""

from .chat_settings import DMX_CHAT_MODEL, DMX_CHAT_URL, DMX_SYSTEM_PROMPT

__all__ = ["DMX_CHAT_MODEL", "DMX_CHAT_URL", "DMX_SYSTEM_PROMPT"]
