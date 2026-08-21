"""Fail-closed outbound data guard for domain-model requests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from ._json import canonical_json_bytes

MAX_TEXT_BYTES: Final[int] = 8 * 1024
MAX_REQUEST_BYTES: Final[int] = 256 * 1024

_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "apikey",
        "accesskey",
        "accesstoken",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "secret",
        "token",
    }
)
_TRUSTED_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "caseref",
        "operatingcontext",
        "currentsetpoints",
        "dataquality",
        "datatimestamp",
        "facts",
        "feedcomposition",
        "feedmassflowkgs",
        "freshfeedloadkgs",
        "freshfeedmassflowkgs",
        "initialinventoryratios",
        "initialstate",
        "modelref",
        "operatingmode",
        "providerid",
    }
)
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"(?i)\bsk[-_][A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:authorization|api[_ -]?key|access[_ -]?token|client[_ -]?secret|"
        r"password|secret|token)"
        r"(?![A-Za-z0-9_])\s*[:=]\s*[\"']?[^\s\"',}]{4,}"
    ),
    re.compile(r"(?i)(?:DMX|API|访问|认证)?(?:令牌|密钥)\s*[:=：]\s*[\"']?[^\s\"',}]{4,}"),
    re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|CLIENT_SECRET)\b"),
)
_CONTEXT_TEXT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)[\"']?(?:operating_context|current_setpoints|feed_composition|"
    r"fresh_feed_(?:load|mass_flow)_kg_s|initial_inventory_ratios|initial_state)"
    r"[\"']?\s*:"
)
_TRUSTED_SOURCE_TEXT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?ix)(?:(?<![A-Za-z0-9_])(?:DCS|SIS|LIMS)(?![A-Za-z0-9_])|"
    r"实时(?:工况|数据|设定值|给定值)|"
    r"仿真(?:工况|证据|结果|输出|对象)|"
    r"(?:CDU|HYSYS|机理模型).{0,20}(?:对象|内部字段|字段路径)\s*[:=]\s*\S+|"
    r"\bcurrent\s+(?:operating\s+conditions?|set[ -]?points?|feed(?:\s+(?:rate|flow|load|composition))?|initial\s+state)\b|"
    r"\blive\s+(?:process|operating|plant)\s+data\b)"
)
_CONTEXT_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?ix)(?:"
    r"(?:(?:当前|目前|实时|现有|现在(?:的)?)\s*)?"
    r"(?:进料(?:量|流量|负荷|组成)?|原油(?:处理量|流量|组成|硫(?:含量)?)|"
    r"(?:常压塔)?(?:塔顶)?压力|塔压|炉(?:出口(?:温度)?|温(?:度)?)|侧线温度|"
    r"汽提蒸汽(?:量|流量)?|回流比|液位|"
    r"feed(?:\s+(?:rate|flow|load|composition))?|top\s+pressure|"
    r"furnace(?:\s+outlet)?\s+temperature|reflux\s+ratio|level)"
    r".{0,80}?[-+]?\d+(?:\.\d+)?\s*"
    r"(?:t/?h|kg/?s|pa|kpa|mpa|bar|degc|°c|摄氏度|k|wt\s*%|%|吨/?小时|千克/?秒)"
    r"|[-+]?\d+(?:\.\d+)?\s*"
    r"(?:t/?h|kg/?s|pa|kpa|mpa|bar|degc|°c|摄氏度|k|wt\s*%|%|吨/?小时|千克/?秒)"
    r".{0,80}?(?:进料(?:量|流量|负荷|组成)?|原油(?:处理量|组成|硫(?:含量)?)|"
    r"(?:常压塔)?(?:塔顶)?压力|塔压|炉(?:出口(?:温度)?|温(?:度)?)|侧线温度|"
    r"汽提蒸汽(?:量|流量)?|回流比|液位|"
    r"feed(?:\s+(?:rate|flow|load|composition))?|top\s+pressure|"
    r"furnace(?:\s+outlet)?\s+temperature|reflux\s+ratio|level)"
    r")"
)
_CONTEXT_CATEGORY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?ix)(?:"
    r"(?:当前|目前|实时|现有|现在(?:的)?)\s*"
    r"(?:工况|操作模式|运行模式|初始状态|塔底液位|液位)"
    r".{0,40}?(?:是|为|处于|采用|偏高|偏低|不稳定|手动|自动|高硫|低硫)"
    r"|(?:初始状态|操作模式|运行模式|塔底液位)\s*"
    r"(?:是|为|处于|采用)?\s*(?:偏高|偏低|不稳定|手动|自动|高硫|低硫)"
    r")"
)
_QUALITATIVE_CONTEXT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?ix)(?:"
        r"(?:当前|目前|现在(?:的)?|现阶段)(?:的)?\s*"
        r"(?!目标|希望|想要|需要|优先|计划|要求|任务|意图|诉求)"
        r"(?:(?![。！？\n]).){0,16}?"
        r"(?:原油|原料|进料|装置|设备|机组|CDU|"
        r"常压(?:蒸馏)?(?:装置|塔)?|加热炉|炉|塔(?:顶|底)?|生产线)"
        r"|(?:原油|原料|进料|装置|设备|机组|CDU|"
        r"常压(?:蒸馏)?(?:装置|塔)?|加热炉|炉|塔(?:顶|底)?|生产线)"
        r"(?:(?![。！？\n]).){0,12}?(?:当前|目前|现在|现阶段)"
        r")"
        r"(?:(?![。！？\n]).){0,40}?"
        r"(?:"
        r"(?:酸值|TAN|硫(?:含量)?|含水(?:量)?|黏度|粘度|密度|性质)"
        r"(?:(?![。！？\n]).){0,12}?(?:偏高|偏低|过高|过低|升高|下降|异常|波动)"
        r"|(?:属于|为|是|呈)?\s*(?:高酸|低酸|高硫|低硫|高含水|低含水)(?:原油)?"
        r"|(?:处于|正在|为|是)?\s*(?:降|减|低|高|满|超)负荷(?:状态|工况|运行)?"
        r"|(?:运行|工况|操作(?:模式)?)"
        r"(?:(?![。！？\n]).){0,12}?(?:不稳定|不稳|异常|波动|受限|手动|自动|开车|停车|切换)"
        r")"
    ),
    re.compile(
        r"(?ix)(?:"
        r"(?:current(?:ly)?|presently|at\s+present|right\s+now)\s*[,;:]?\s*"
        r"(?!(?:the\s+)?(?:goal|objective|aim|request|priority|plan)\b|"
        r"(?:we\s+)?(?:want|hope|need|intend)\b|please\b)"
        r"(?:(?![.!?\n]).){0,20}?"
        r"(?:crude(?:\s+oil)?|feed|plant|unit|CDU|furnace|column|tower|process)"
        r"|(?:crude(?:\s+oil)?|feed|plant|unit|CDU|furnace|column|tower|process)"
        r"(?:(?![.!?\n]).){0,16}?"
        r"(?:current(?:ly)?|presently|at\s+present|right\s+now)"
        r")"
        r"(?:(?![.!?\n]).){0,48}?"
        r"(?:"
        r"(?:acid\s+number|TAN|sulfur(?:\s+content)?|water\s+content|viscosity|density|quality)"
        r"(?:(?![.!?\n]).){0,16}?(?:high|low|elevated|reduced|abnormal|fluctuat\w*)"
        r"|(?:high|low)[ -]?(?:acid|sulfur|water)(?:\s+crude)?"
        r"|(?:operat\w*|runn\w*|is|are)?\s*(?:at|under|in)?\s*"
        r"(?:reduced|lower|low|high|full|over)[ -]?load(?:\s+(?:operation|conditions?))?"
        r"|(?:operation|operating\s+(?:mode|conditions?)|process\s+conditions?)"
        r"(?:(?![.!?\n]).){0,16}?(?:unstable|abnormal|fluctuat\w*|constrained|manual|automatic|startup|shutdown)"
        r")"
    ),
)
_CURRENT_MEASUREMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?ix)(?:当前|目前|实时|现有|现在(?:的)?)"
    r"(?:(?![。！？\n]).){0,80}?[-+]?\d+(?:\.\d+)?\s*"
    r"(?:t/?h|kg/?s|m3/?h|pa|kpa|mpa|bar|degc|°c|摄氏度|k|"
    r"mw|kw|gj/?h|mj/?h|mj/?t|wt\s*%|mol\s*%|%|吨/?小时|千克/?秒)"
)
_CREDENTIAL_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])api[ _-]?key(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])authorization\s+header(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])access[ _-]?token(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])client[ _-]?secret(?![A-Za-z0-9_])|"
    r"(?:DMX|API|访问|认证)?(?:密钥|令牌))"
)


class EgressViolation(ValueError):
    """A safe, classified refusal that never echoes the suspected value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


class EgressGuard:
    """Bound request size and reject credential or trusted-context leakage."""

    def __init__(
        self,
        *,
        max_text_bytes: int = MAX_TEXT_BYTES,
        max_request_bytes: int = MAX_REQUEST_BYTES,
    ) -> None:
        if isinstance(max_text_bytes, bool) or not isinstance(max_text_bytes, int):
            raise TypeError("max_text_bytes must be an integer")
        if isinstance(max_request_bytes, bool) or not isinstance(max_request_bytes, int):
            raise TypeError("max_request_bytes must be an integer")
        if not 1 <= max_text_bytes <= MAX_TEXT_BYTES:
            raise ValueError("max_text_bytes must be within the fixed 8 KiB ceiling")
        if not 1 <= max_request_bytes <= MAX_REQUEST_BYTES:
            raise ValueError("max_request_bytes must be within the fixed 256 KiB ceiling")
        self.max_text_bytes = max_text_bytes
        self.max_request_bytes = max_request_bytes

    def inspect_text(self, value: str, *, context: str = "outbound text") -> None:
        if not isinstance(value, str):
            raise TypeError(f"{context} must be text")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise EgressViolation("invalid-text", f"{context} is not valid UTF-8") from exc
        if size > self.max_text_bytes:
            raise EgressViolation("text-too-large", f"{context} exceeds the 8 KiB policy")
        self._inspect_string(value)

    def inspect_request(self, value: Mapping[str, object]) -> bytes:
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise TypeError("outbound request must be an object with string keys")
        try:
            payload = canonical_json_bytes(value)
        except (TypeError, ValueError) as exc:
            raise EgressViolation(
                "invalid-json-request",
                "outbound request must contain only finite JSON values",
            ) from exc
        if len(payload) > self.max_request_bytes:
            raise EgressViolation(
                "request-too-large",
                "outbound request exceeds the 256 KiB policy",
            )
        self._inspect_value(value)
        return payload

    def validate_text(self, value: str, *, context: str = "outbound text") -> None:
        """Compatibility alias for callers that use validation terminology."""

        self.inspect_text(value, context=context)

    def validate_request(self, value: Mapping[str, object]) -> bytes:
        """Compatibility alias returning the canonical validated request bytes."""

        return self.inspect_request(value)

    def _inspect_value(self, value: object) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    raise EgressViolation("invalid-json-request", "outbound request has a bad key")
                key = _normalized_key(raw_key)
                if key in _SECRET_KEYS or key.endswith(
                    ("apikey", "accesstoken", "clientsecret", "credential", "password")
                ):
                    raise EgressViolation(
                        "suspected-credential",
                        "outbound request contains a credential-like field",
                    )
                if key in _TRUSTED_CONTEXT_KEYS:
                    raise EgressViolation(
                        "trusted-context-forbidden",
                        "outbound request contains a trusted operating-context field",
                    )
                self._inspect_value(item)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                self._inspect_value(item)
            return
        if isinstance(value, str):
            self._inspect_string(value)

    @staticmethod
    def _inspect_string(value: str) -> None:
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise EgressViolation(
                "suspected-credential",
                "outbound content contains credential-like material",
            )
        if _CONTEXT_TEXT_PATTERN.search(value):
            raise EgressViolation(
                "trusted-context-forbidden",
                "outbound content appears to contain trusted operating-context values",
            )
        if (
            _TRUSTED_SOURCE_TEXT_PATTERN.search(value)
            or _CONTEXT_VALUE_PATTERN.search(value)
            or _CONTEXT_CATEGORY_PATTERN.search(value)
            or any(pattern.search(value) for pattern in _QUALITATIVE_CONTEXT_PATTERNS)
            or _CURRENT_MEASUREMENT_PATTERN.search(value)
        ):
            raise EgressViolation(
                "trusted-context-forbidden",
                "outbound content appears to contain trusted operating-context material",
            )
        if _CREDENTIAL_REFERENCE_PATTERN.search(value):
            raise EgressViolation(
                "suspected-credential",
                "outbound content refers to provider credential material",
            )
