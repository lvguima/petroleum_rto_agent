"""Provider-neutral references to strictly reloadable simulator evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast

from .common import (
    as_mapping,
    canonical_fingerprint,
    digest,
    identifier,
    strict_keys,
    string_mapping,
    text,
)
from .problem import ENGINEERING_CLAIM_SCOPE
from .reference import ContractRef
from .simulation import SimulationRunBundle

RUN_EVIDENCE_SCHEMA_VERSION: Final[str] = "1.0.0"
PairRole = Literal["baseline", "candidate"]


@dataclass(frozen=True)
class RunEvidenceRef:
    """Compact reference to one immutable simulator artifact.

    ``run_ref`` and manifest/request fingerprints are retained for strict local
    reload, while semantic fingerprints remain stable if an unchanged artifact
    tree is relocated.
    """

    schema_version: str
    evidence_version: str
    pair_role: PairRole
    provider_id: str
    run_ref: str
    provider_request_fingerprint: str
    request_fingerprint: str
    effective_input_fingerprint: str
    result_fingerprint: str
    manifest_fingerprint: str
    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    claim_scope: str

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the run evidence contract")
        if self.claim_scope != ENGINEERING_CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        object.__setattr__(
            self,
            "evidence_version",
            identifier(self.evidence_version, context="evidence_version"),
        )
        if self.pair_role not in {"baseline", "candidate"}:
            raise ValueError("unsupported run evidence pair_role")
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        object.__setattr__(self, "run_ref", text(self.run_ref, context="run_ref"))
        for name in (
            "provider_request_fingerprint",
            "request_fingerprint",
            "effective_input_fingerprint",
            "result_fingerprint",
            "manifest_fingerprint",
        ):
            object.__setattr__(self, name, digest(getattr(self, name), context=name))
        object.__setattr__(self, "versions", string_mapping(self.versions, context="versions"))
        raw_sources = as_mapping(self.source_fingerprints, context="source_fingerprints")
        object.__setattr__(
            self,
            "source_fingerprints",
            MappingProxyType(
                {
                    text(key, context="source fingerprint key"): digest(
                        value,
                        context=f"source_fingerprints.{key}",
                    )
                    for key, value in raw_sources.items()
                }
            ),
        )

    @classmethod
    def from_bundle(
        cls,
        bundle: SimulationRunBundle,
        *,
        pair_role: PairRole,
    ) -> RunEvidenceRef:
        if not isinstance(bundle, SimulationRunBundle):
            raise TypeError("run evidence requires a SimulationRunBundle")
        return cls(
            schema_version=RUN_EVIDENCE_SCHEMA_VERSION,
            evidence_version="run-evidence",
            pair_role=pair_role,
            provider_id=bundle.provider_id,
            run_ref=bundle.run_ref,
            provider_request_fingerprint=bundle.provider_request_fingerprint,
            request_fingerprint=bundle.request_fingerprint,
            effective_input_fingerprint=bundle.effective_input_fingerprint,
            result_fingerprint=bundle.result_fingerprint,
            manifest_fingerprint=bundle.manifest_fingerprint,
            versions=bundle.versions,
            source_fingerprints=bundle.source_fingerprints,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_version": self.evidence_version,
            "pair_role": self.pair_role,
            "provider_id": self.provider_id,
            "provider_request_fingerprint": self.provider_request_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.semantic_payload())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(
            f"{self.pair_role}-evidence-{self.fingerprint[:16]}",
            self.fingerprint,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            **self.semantic_payload(),
            "run_ref": self.run_ref,
            "evidence_id": self.ref.object_id,
            "evidence_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RunEvidenceRef:
        strict_keys(
            value,
            required={
                "schema_version",
                "evidence_version",
                "pair_role",
                "provider_id",
                "run_ref",
                "provider_request_fingerprint",
                "request_fingerprint",
                "effective_input_fingerprint",
                "result_fingerprint",
                "manifest_fingerprint",
                "versions",
                "source_fingerprints",
                "claim_scope",
            },
            optional={"evidence_id", "evidence_fingerprint"},
            context="run evidence ref",
        )
        pair_role = value["pair_role"]
        if pair_role not in {"baseline", "candidate"}:
            raise ValueError("unsupported run evidence pair_role")
        evidence = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            evidence_version=identifier(value["evidence_version"], context="evidence_version"),
            pair_role=pair_role,
            provider_id=identifier(value["provider_id"], context="provider_id"),
            run_ref=text(value["run_ref"], context="run_ref"),
            provider_request_fingerprint=digest(
                value["provider_request_fingerprint"],
                context="provider_request_fingerprint",
            ),
            request_fingerprint=digest(
                value["request_fingerprint"],
                context="request_fingerprint",
            ),
            effective_input_fingerprint=digest(
                value["effective_input_fingerprint"],
                context="effective_input_fingerprint",
            ),
            result_fingerprint=digest(
                value["result_fingerprint"],
                context="result_fingerprint",
            ),
            manifest_fingerprint=digest(
                value["manifest_fingerprint"],
                context="manifest_fingerprint",
            ),
            versions=string_mapping(value["versions"], context="versions"),
            source_fingerprints=cast(
                Mapping[str, str],
                as_mapping(value["source_fingerprints"], context="source_fingerprints"),
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("evidence_id") not in {None, evidence.ref.object_id}:
            raise ValueError("evidence_id differs from evidence content")
        supplied = value.get("evidence_fingerprint")
        if supplied not in {None, evidence.fingerprint}:
            raise ValueError("evidence_fingerprint differs from evidence content")
        return evidence


__all__ = ["RUN_EVIDENCE_SCHEMA_VERSION", "PairRole", "RunEvidenceRef"]
