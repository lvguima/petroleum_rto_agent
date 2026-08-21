"""Neutral simulation request, preview, and evidence contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast

from .common import (
    JsonValue,
    as_mapping,
    boolean,
    canonical_fingerprint,
    digest,
    freeze_json_mapping,
    identifier,
    integer,
    strict_keys,
    string_mapping,
    text,
    thaw_json,
)
from .problem import ENGINEERING_CLAIM_SCOPE
from .reference import ContractRef

SIMULATION_SCHEMA_VERSION: Final[str] = "1.0.0"
SimulationStage = Literal["M2", "M4"]
SimulationPairRole = Literal["baseline", "candidate"]


def _schema(value: str) -> None:
    if value != SIMULATION_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the simulation contract")


def _claim(value: str) -> None:
    if value != ENGINEERING_CLAIM_SCOPE:
        raise ValueError("claim_scope must be engineering_simulation_only")


@dataclass(frozen=True)
class SimulationEvaluationRequest:
    schema_version: str
    request_version: str
    stage: SimulationStage
    pair_id: str
    pair_role: SimulationPairRole
    problem_ref: ContractRef
    context_ref: ContractRef
    proposal_ref: ContractRef | None
    provider_id: str
    compiler_version: str
    provider_request: Mapping[str, JsonValue]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("request_version", "pair_id", "provider_id", "compiler_version"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        if self.stage not in {"M2", "M4"} or self.pair_role not in {
            "baseline",
            "candidate",
        }:
            raise ValueError("unsupported simulation stage or pair role")
        if not isinstance(self.problem_ref, ContractRef) or not isinstance(
            self.context_ref, ContractRef
        ):
            raise TypeError("simulation refs must be ContractRef values")
        if self.pair_role == "candidate" and not isinstance(self.proposal_ref, ContractRef):
            raise TypeError("candidate simulation request requires a proposal ref")
        if self.pair_role == "baseline" and self.proposal_ref is not None:
            raise ValueError("baseline simulation request cannot carry a proposal ref")
        object.__setattr__(
            self,
            "provider_request",
            freeze_json_mapping(self.provider_request, context="provider_request"),
        )

    @property
    def provider_request_fingerprint(self) -> str:
        return canonical_fingerprint(self.provider_request)

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_version": self.request_version,
            "stage": self.stage,
            "pair_id": self.pair_id,
            "pair_role": self.pair_role,
            "problem_ref": self.problem_ref.as_dict(),
            "context_ref": self.context_ref.as_dict(),
            "proposal_ref": None if self.proposal_ref is None else self.proposal_ref.as_dict(),
            "provider_id": self.provider_id,
            "compiler_version": self.compiler_version,
            "provider_request": thaw_json(cast(JsonValue, self.provider_request)),
            "claim_scope": self.claim_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.fingerprint_payload())

    @property
    def request_id(self) -> str:
        return f"simulation-{self.fingerprint[:16]}"

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.request_id, self.fingerprint)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_payload(),
            "request_id": self.request_id,
            "simulation_request_fingerprint": self.fingerprint,
            "provider_request_fingerprint": self.provider_request_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SimulationEvaluationRequest:
        strict_keys(
            value,
            required={
                "schema_version",
                "request_version",
                "stage",
                "pair_id",
                "pair_role",
                "problem_ref",
                "context_ref",
                "proposal_ref",
                "provider_id",
                "compiler_version",
                "provider_request",
                "claim_scope",
            },
            optional={
                "request_id",
                "simulation_request_fingerprint",
                "provider_request_fingerprint",
            },
            context="simulation evaluation request",
        )
        stage = value["stage"]
        pair_role = value["pair_role"]
        if stage not in {"M2", "M4"} or pair_role not in {"baseline", "candidate"}:
            raise ValueError("unsupported simulation stage or pair role")
        raw_proposal_ref = value["proposal_ref"]
        request = cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            request_version=identifier(value["request_version"], context="request_version"),
            stage=stage,
            pair_id=identifier(value["pair_id"], context="pair_id"),
            pair_role=pair_role,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            context_ref=ContractRef.from_mapping(
                as_mapping(value["context_ref"], context="context_ref")
            ),
            proposal_ref=(
                None
                if raw_proposal_ref is None
                else ContractRef.from_mapping(as_mapping(raw_proposal_ref, context="proposal_ref"))
            ),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            compiler_version=identifier(value["compiler_version"], context="compiler_version"),
            provider_request=freeze_json_mapping(
                value["provider_request"], context="provider_request"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )
        if value.get("request_id") not in {None, request.request_id}:
            raise ValueError("request_id differs from request content")
        supplied = value.get("simulation_request_fingerprint")
        if (
            supplied is not None
            and digest(supplied, context="simulation_request_fingerprint") != request.fingerprint
        ):
            raise ValueError("simulation_request_fingerprint differs from request content")
        supplied_provider = value.get("provider_request_fingerprint")
        if (
            supplied_provider is not None
            and digest(supplied_provider, context="provider_request_fingerprint")
            != request.provider_request_fingerprint
        ):
            raise ValueError("provider_request_fingerprint differs from provider request")
        return request


@dataclass(frozen=True)
class SimulationPreview:
    schema_version: str
    preview_version: str
    simulation_request_ref: ContractRef
    provider_id: str
    provider_preview_fingerprint: str
    effective_input_fingerprint: str
    base_object_fingerprints: Mapping[str, str]
    effective_object_fingerprints: Mapping[str, str]
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        if not isinstance(self.simulation_request_ref, ContractRef):
            raise TypeError("simulation_request_ref must be a ContractRef")
        object.__setattr__(
            self, "preview_version", identifier(self.preview_version, context="preview_version")
        )
        object.__setattr__(self, "provider_id", identifier(self.provider_id, context="provider_id"))
        for name in ("provider_preview_fingerprint", "effective_input_fingerprint"):
            object.__setattr__(self, name, digest(getattr(self, name), context=name))
        for name in ("base_object_fingerprints", "effective_object_fingerprints"):
            raw = getattr(self, name)
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    {
                        identifier(key, context=f"{name} key"): digest(
                            item, context=f"{name} fingerprint"
                        )
                        for key, item in raw.items()
                    }
                ),
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "preview_version": self.preview_version,
            "simulation_request_ref": self.simulation_request_ref.as_dict(),
            "provider_id": self.provider_id,
            "provider_preview_fingerprint": self.provider_preview_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "base_object_fingerprints": dict(self.base_object_fingerprints),
            "effective_object_fingerprints": dict(self.effective_object_fingerprints),
            "claim_scope": self.claim_scope,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SimulationPreview:
        strict_keys(
            value,
            required={
                "schema_version",
                "preview_version",
                "simulation_request_ref",
                "provider_id",
                "provider_preview_fingerprint",
                "effective_input_fingerprint",
                "base_object_fingerprints",
                "effective_object_fingerprints",
                "claim_scope",
            },
            context="simulation preview",
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            preview_version=identifier(value["preview_version"], context="preview_version"),
            simulation_request_ref=ContractRef.from_mapping(
                as_mapping(value["simulation_request_ref"], context="simulation_request_ref")
            ),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            provider_preview_fingerprint=digest(
                value["provider_preview_fingerprint"], context="provider_preview_fingerprint"
            ),
            effective_input_fingerprint=digest(
                value["effective_input_fingerprint"], context="effective_input_fingerprint"
            ),
            base_object_fingerprints=string_mapping(
                value["base_object_fingerprints"], context="base_object_fingerprints"
            ),
            effective_object_fingerprints=string_mapping(
                value["effective_object_fingerprints"], context="effective_object_fingerprints"
            ),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )


@dataclass(frozen=True)
class SimulationRunBundle:
    schema_version: str
    bundle_version: str
    provider_id: str
    provider_request_fingerprint: str
    run_ref: str
    runtime_status: str
    engine_status: str
    summary: Mapping[str, JsonValue]
    sample_count: int
    event_count: int
    request_fingerprint: str
    effective_input_fingerprint: str
    result_fingerprint: str
    manifest_fingerprint: str
    versions: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    failure_stage: str | None
    failure_reason: str | None
    synthetic: bool
    claim_scope: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _claim(self.claim_scope)
        for name in ("bundle_version", "provider_id", "runtime_status", "engine_status"):
            object.__setattr__(self, name, identifier(getattr(self, name), context=name))
        object.__setattr__(self, "run_ref", text(self.run_ref, context="run_ref"))
        for name in (
            "provider_request_fingerprint",
            "request_fingerprint",
            "effective_input_fingerprint",
            "result_fingerprint",
            "manifest_fingerprint",
        ):
            object.__setattr__(self, name, digest(getattr(self, name), context=name))
        object.__setattr__(self, "summary", freeze_json_mapping(self.summary, context="summary"))
        object.__setattr__(self, "sample_count", integer(self.sample_count, context="sample_count"))
        object.__setattr__(self, "event_count", integer(self.event_count, context="event_count"))
        object.__setattr__(
            self,
            "versions",
            MappingProxyType(
                {
                    identifier(key, context="version key"): text(item, context="version value")
                    for key, item in self.versions.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "source_fingerprints",
            MappingProxyType(
                {
                    text(key, context="source key"): digest(item, context="source fingerprint")
                    for key, item in self.source_fingerprints.items()
                }
            ),
        )
        if self.failure_stage is not None:
            object.__setattr__(
                self, "failure_stage", text(self.failure_stage, context="failure_stage")
            )
        if self.failure_reason is not None:
            object.__setattr__(
                self, "failure_reason", text(self.failure_reason, context="failure_reason")
            )
        if not isinstance(self.synthetic, bool) or not self.synthetic:
            raise ValueError("RTO accepts only explicitly synthetic simulation evidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bundle_version": self.bundle_version,
            "provider_id": self.provider_id,
            "provider_request_fingerprint": self.provider_request_fingerprint,
            "run_ref": self.run_ref,
            "runtime_status": self.runtime_status,
            "engine_status": self.engine_status,
            "summary": thaw_json(cast(JsonValue, self.summary)),
            "sample_count": self.sample_count,
            "event_count": self.event_count,
            "request_fingerprint": self.request_fingerprint,
            "effective_input_fingerprint": self.effective_input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "synthetic": self.synthetic,
            "claim_scope": self.claim_scope,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SimulationRunBundle:
        strict_keys(
            value,
            required={
                "schema_version",
                "bundle_version",
                "provider_id",
                "provider_request_fingerprint",
                "run_ref",
                "runtime_status",
                "engine_status",
                "summary",
                "sample_count",
                "event_count",
                "request_fingerprint",
                "effective_input_fingerprint",
                "result_fingerprint",
                "manifest_fingerprint",
                "versions",
                "source_fingerprints",
                "failure_stage",
                "failure_reason",
                "synthetic",
                "claim_scope",
            },
            context="simulation run bundle",
        )
        source_fingerprints = as_mapping(
            value["source_fingerprints"], context="source_fingerprints"
        )
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            bundle_version=identifier(value["bundle_version"], context="bundle_version"),
            provider_id=identifier(value["provider_id"], context="provider_id"),
            provider_request_fingerprint=digest(
                value["provider_request_fingerprint"], context="provider_request_fingerprint"
            ),
            run_ref=text(value["run_ref"], context="run_ref"),
            runtime_status=identifier(value["runtime_status"], context="runtime_status"),
            engine_status=identifier(value["engine_status"], context="engine_status"),
            summary=freeze_json_mapping(value["summary"], context="summary"),
            sample_count=integer(value["sample_count"], context="sample_count"),
            event_count=integer(value["event_count"], context="event_count"),
            request_fingerprint=digest(value["request_fingerprint"], context="request_fingerprint"),
            effective_input_fingerprint=digest(
                value["effective_input_fingerprint"], context="effective_input_fingerprint"
            ),
            result_fingerprint=digest(value["result_fingerprint"], context="result_fingerprint"),
            manifest_fingerprint=digest(
                value["manifest_fingerprint"], context="manifest_fingerprint"
            ),
            versions=string_mapping(value["versions"], context="versions"),
            source_fingerprints={
                text(key, context="source key"): digest(item, context=f"source_fingerprints.{key}")
                for key, item in source_fingerprints.items()
            },
            failure_stage=(
                None
                if value["failure_stage"] is None
                else text(value["failure_stage"], context="failure_stage")
            ),
            failure_reason=(
                None
                if value["failure_reason"] is None
                else text(value["failure_reason"], context="failure_reason")
            ),
            synthetic=boolean(value["synthetic"], context="synthetic"),
            claim_scope=text(value["claim_scope"], context="claim_scope"),
        )


__all__ = [
    "SIMULATION_SCHEMA_VERSION",
    "SimulationEvaluationRequest",
    "SimulationPairRole",
    "SimulationPreview",
    "SimulationRunBundle",
    "SimulationStage",
]
