"""Provider-neutral port implemented by a future vertical-domain model adapter."""

from __future__ import annotations

from typing import Protocol

from .invocation import DomainModelInvocationResult
from .models import DomainModelRequest


class DomainModelPort(Protocol):
    """Invoke one provider without receiving trusted operating context."""

    @property
    def provider_id(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    def invoke(self, request: DomainModelRequest) -> DomainModelInvocationResult: ...
