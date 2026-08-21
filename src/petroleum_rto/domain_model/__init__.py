"""Minimal DMX chat API plus frozen strict-intent compatibility exports."""

from .chat import DmxChatClient, DmxChatError, DmxChatSession
from .chat_settings import DMX_CHAT_MODEL, DMX_CHAT_URL, DMX_SYSTEM_PROMPT
from .egress import MAX_REQUEST_BYTES, MAX_TEXT_BYTES, EgressGuard, EgressViolation
from .evidence import (
    EVIDENCE_MANIFEST_SCHEMA_ID,
    EVIDENCE_SCHEMA_VERSION,
    INVOCATION_EVIDENCE_SCHEMA_ID,
    SESSION_EVIDENCE_SCHEMA_ID,
    EvidenceRecord,
    EvidenceStore,
    InvocationEvidence,
    SessionEvidence,
)
from .loader import load_provider_catalog, packaged_provider_catalog_bytes
from .models import (
    DMX_BASE_URL,
    DMX_CREDENTIAL_ENV,
    DMX_PROVIDER_ID,
    PROVIDER_CATALOG_SCHEMA_ID,
    PROVIDER_CATALOG_SCHEMA_VERSION,
    ApiStyle,
    ModelProfile,
    ProviderCatalog,
    ProviderModelInfo,
    ProviderProfile,
)
from .prompt import CompiledPrompt, PromptCompiler

__all__ = [
    "DMX_BASE_URL",
    "DMX_CHAT_MODEL",
    "DMX_CHAT_URL",
    "DMX_CREDENTIAL_ENV",
    "DMX_PROVIDER_ID",
    "DMX_SYSTEM_PROMPT",
    "EVIDENCE_MANIFEST_SCHEMA_ID",
    "EVIDENCE_SCHEMA_VERSION",
    "INVOCATION_EVIDENCE_SCHEMA_ID",
    "MAX_REQUEST_BYTES",
    "MAX_TEXT_BYTES",
    "PROVIDER_CATALOG_SCHEMA_ID",
    "PROVIDER_CATALOG_SCHEMA_VERSION",
    "SESSION_EVIDENCE_SCHEMA_ID",
    "ApiStyle",
    "CompiledPrompt",
    "DmxChatClient",
    "DmxChatError",
    "DmxChatSession",
    "EgressGuard",
    "EgressViolation",
    "EvidenceRecord",
    "EvidenceStore",
    "InvocationEvidence",
    "ModelProfile",
    "PromptCompiler",
    "ProviderCatalog",
    "ProviderModelInfo",
    "ProviderProfile",
    "SessionEvidence",
    "load_provider_catalog",
    "packaged_provider_catalog_bytes",
]
