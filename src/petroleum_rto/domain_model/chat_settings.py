"""Minimal local settings for the DMXAPI Chat Completions client."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from .credentials import load_local_dmx_api_key

# 只需编辑这三个值即可切换接口、模型或助手角色。
DMX_CHAT_URL: Final[str] = "https://www.dmxapi.cn/v1/chat/completions"
DMX_CHAT_MODEL: Final[str] = "deepseek-v4-flash-0731"
DMX_SYSTEM_PROMPT: Final[str] = (
    "你是石油炼化RTO助手。请用清晰中文回答；解释RTO结果时必须保留原始数值和单位，"
    "并明确结果仅来自离线工程仿真、未经现场验证且没有现场控制权。"
)


class DmxChatSettingsError(ValueError):
    """Safe local configuration error that never contains the API key."""


@dataclass(frozen=True, slots=True)
class DmxChatSettings:
    """The four values needed by the minimal DMXAPI chat client."""

    api_key: str = field(repr=False)
    url: str = DMX_CHAT_URL
    model: str = DMX_CHAT_MODEL
    system_prompt: str | None = DMX_SYSTEM_PROMPT

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith("https://"):
            raise DmxChatSettingsError("DMXAPI chat URL must be a non-empty HTTPS URL")
        if self.url != self.url.strip():
            raise DmxChatSettingsError("DMXAPI chat URL must not contain surrounding whitespace")
        if not isinstance(self.api_key, str) or not _valid_api_key(self.api_key):
            raise DmxChatSettingsError("DMXAPI API key is missing or invalid")
        if not isinstance(self.model, str) or not self.model.strip():
            raise DmxChatSettingsError("DMXAPI model must be non-empty text")
        if self.model != self.model.strip():
            raise DmxChatSettingsError("DMXAPI model must not contain surrounding whitespace")
        if self.system_prompt is not None and (
            not isinstance(self.system_prompt, str) or not self.system_prompt.strip()
        ):
            raise DmxChatSettingsError("system_prompt must be non-empty text when provided")


def _valid_api_key(value: str) -> bool:
    return (
        8 <= len(value) <= 2048
        and value == value.strip()
        and all(33 <= ord(character) <= 126 for character in value)
    )


def load_dmx_chat_settings(
    *,
    key_loader: Callable[[], str | None] | None = None,
    model: str = DMX_CHAT_MODEL,
    system_prompt: str | None = DMX_SYSTEM_PROMPT,
) -> DmxChatSettings:
    """Load the local SK and combine it with the fixed Chat Completions defaults."""

    loader = load_local_dmx_api_key if key_loader is None else key_loader
    try:
        api_key = loader()
    except Exception:  # noqa: BLE001 - configuration boundary must not reflect secret details
        raise DmxChatSettingsError("DMXAPI local configuration could not be loaded") from None
    if api_key is None:
        raise DmxChatSettingsError("DMXAPI API key is missing or invalid")
    return DmxChatSettings(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
    )


__all__ = [
    "DMX_CHAT_MODEL",
    "DMX_CHAT_URL",
    "DMX_SYSTEM_PROMPT",
    "DmxChatSettings",
    "DmxChatSettingsError",
    "load_dmx_chat_settings",
]
