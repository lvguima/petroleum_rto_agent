"""Exact DMX model identities and documented candidate protocols (not live certification)."""

from dataclasses import dataclass
from typing import Literal

from .chat_settings import DMX_CHAT_MODEL

Thinking = Literal["default", "on", "off"]


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    label: str
    protocol: Literal["chat", "responses"]
    thinking_parameter: Literal["enable_thinking", "thinking", "always", "reasoning"]
    default_thinking: bool
    efforts: tuple[str, ...]
    context_tokens: int | None
    limits_source: str


# Capacity is the original provider's documented window, pending DMX verification.
# None is deliberate: a similarly named model is not evidence for the CDX suffix.
MODELS: tuple[ModelProfile, ...] = (
    ModelProfile(
        "qwen3.8-max-0902",
        "Qwen Max",
        "chat",
        "enable_thinking",
        True,
        ("low", "medium", "xhigh"),
        1_000_000,
        "https://platform.qianwenai.com/docs/developer-guides/getting-started/text-generation-models",
    ),
    ModelProfile(
        "kimi-k3",
        "Kimi K3",
        "chat",
        "always",
        True,
        ("low", "high", "max"),
        1_000_000,
        "https://platform.kimi.ai/docs/models",
    ),
    ModelProfile(
        "gpt-5.6-sol-cdx",
        "GPT Sol CDX",
        "responses",
        "reasoning",
        True,
        ("low", "medium", "high", "xhigh", "max"),
        None,
        "精确CDX型号容量和协议待核实；Responses仅为候选",
    ),
    ModelProfile(
        "deepseek-v4-pro-0813",
        "DeepSeek Pro",
        "chat",
        "thinking",
        True,
        ("high", "max"),
        1_000_000,
        "https://api-docs.deepseek.com/quick_start/pricing/",
    ),
    ModelProfile(
        "deepseek-v4-flash-0731",
        "DeepSeek Flash",
        "chat",
        "enable_thinking",
        False,
        (),  # This DMX channel is supported only in non-thinking mode.
        1_000_000,
        "https://api-docs.deepseek.com/quick_start/pricing/",
    ),
)
DEFAULT_MODEL_ID = DMX_CHAT_MODEL


def model_profile(model_id: str) -> ModelProfile:
    for profile in MODELS:
        if profile.model_id == model_id:
            return profile
    raise ValueError("未知模型ID，请使用/model列出的完整ID。")


@dataclass(frozen=True)
class ModelSelection:
    profile: ModelProfile
    thinking: Thinking = "default"
    effort: str | None = None
    # An application response budget, NOT a claim about the provider's maximum.
    output_tokens: int = 16_384

    def __post_init__(self) -> None:
        if self.thinking not in ("default", "on", "off"):
            raise ValueError("思考模式只支持default、on或off。")
        if self.profile.model_id == "deepseek-v4-flash-0731" and self.thinking_enabled:
            raise ValueError("当前Flash渠道仅使用非思考模式；如需思考，请切换其他模型。")
        if self.profile.thinking_parameter == "always" and self.thinking == "off":
            raise ValueError("此模型始终思考，不能关闭。")
        if self.effort is not None and (
            not self.thinking_enabled or self.effort not in self.profile.efforts
        ):
            raise ValueError("当前模型或模式不支持这个思考强度。")
        if type(self.output_tokens) is not int or self.output_tokens <= 0:
            raise ValueError("生成预算必须是正整数。")
        if (
            self.profile.context_tokens is not None
            and self.output_tokens >= self.profile.context_tokens
        ):
            raise ValueError("生成预算必须小于上下文容量。")

    @property
    def thinking_enabled(self) -> bool:
        return (
            self.profile.default_thinking if self.thinking == "default" else self.thinking == "on"
        )

    def parameters(self) -> dict[str, object]:
        enabled = self.thinking_enabled
        kind = self.profile.thinking_parameter
        if kind == "reasoning":
            reasoning: dict[str, object] = {"effort": (self.effort or "low") if enabled else "none"}
            if enabled:
                reasoning["summary"] = "auto"
            return {"reasoning": reasoning}
        result: dict[str, object] = {}
        if kind == "enable_thinking":
            result["enable_thinking"] = enabled
        elif kind == "thinking":
            result["thinking"] = {"type": "enabled" if enabled else "disabled"}
        if self.effort:
            result["reasoning_effort"] = self.effort
        if self.profile.model_id == "qwen3.8-max-0902" and enabled:
            result["preserve_thinking"] = True
        return result
