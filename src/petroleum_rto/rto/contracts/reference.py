"""Neutral immutable references shared by all RTO contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .common import digest, identifier, strict_keys


@dataclass(frozen=True)
class ContractRef:
    """Reference an immutable object by stable identifier and content fingerprint."""

    object_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_id", identifier(self.object_id, context="object_id"))
        object.__setattr__(self, "fingerprint", digest(self.fingerprint, context="fingerprint"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ContractRef:
        strict_keys(value, required={"object_id", "fingerprint"}, context="contract ref")
        return cls(
            object_id=identifier(value["object_id"], context="object_id"),
            fingerprint=digest(value["fingerprint"], context="fingerprint"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"object_id": self.object_id, "fingerprint": self.fingerprint}
