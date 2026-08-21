"""RTO-owned composition for the public domain-model communication boundary."""

from __future__ import annotations

from pathlib import Path

from ..capabilities import load_capability_bundle
from .service import IntentCommunicationService


def build_intent_communication_service(
    *,
    repo_root: Path | None = None,
) -> IntentCommunicationService:
    """Build the service without exposing the internal capability bundle upstream."""

    return IntentCommunicationService.from_bundle(load_capability_bundle(repo_root))
