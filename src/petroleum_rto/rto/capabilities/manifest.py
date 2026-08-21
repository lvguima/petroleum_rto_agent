"""Public, sanitized projection of the internal capability bundle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..contracts.common import (
    JsonValue,
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    freeze_json_mapping,
    identifier,
    strict_keys,
    thaw_json,
)
from ..contracts.problem import ENGINEERING_CLAIM_SCOPE
from ..contracts.reference import ContractRef
from .models import CAPABILITY_SCHEMA_VERSION, CapabilityBundle

_METRIC_FIELDS = {
    "metric_id",
    "business_name",
    "stage",
    "unit",
    "direction",
    "proxy",
    "availability",
    "availability_reason",
}
_OBJECTIVE_FIELDS = {
    "objective_id",
    "business_name",
    "metric_id",
    "sense",
    "normalization_scale",
    "availability",
    "availability_reason",
}
_DECISION_FIELDS = {
    "decision_id",
    "business_name",
    "display_unit",
    "canonical_unit",
    "lower_bound",
    "upper_bound",
    "coarse_step",
    "refine_step",
    "availability",
    "availability_reason",
}
_GUARDRAIL_FIELDS = {
    "guardrail_id",
    "business_name",
    "metric_id",
    "stage",
    "unit",
    "allowed_operators",
    "availability",
    "availability_reason",
}
_SELECTOR_FIELDS = {
    "selector_id",
    "business_name",
    "method",
    "minimum_objectives",
    "maximum_objectives",
    "availability",
    "availability_reason",
}
_ROUTE_FIELDS = {
    "route_id",
    "selector_id",
    "minimum_objectives",
    "maximum_objectives",
    "maximum_m2_candidates",
    "top_k",
}
_BINDING_FIELDS = {
    "guardrail_id",
    "priority",
    "operator",
    "limit",
    "normalization_scale",
}


def _freeze_rows(
    rows: tuple[Mapping[str, object], ...],
    *,
    required: set[str],
    context: str,
) -> tuple[Mapping[str, JsonValue], ...]:
    frozen: list[Mapping[str, JsonValue]] = []
    for row in rows:
        strict_keys(row, required=required, context=context)
        frozen.append(freeze_json_mapping(row, context=context))
    return tuple(frozen)


def _thaw_rows(rows: tuple[Mapping[str, JsonValue], ...]) -> list[object]:
    return [thaw_json(row) for row in rows]


@dataclass(frozen=True)
class PublicCapabilityManifest:
    schema_version: str
    manifest_id: str
    manifest_version: str
    catalog_ref: ContractRef
    system_policy_ref: ContractRef
    claim_scope: str
    metrics: tuple[Mapping[str, JsonValue], ...]
    objectives: tuple[Mapping[str, JsonValue], ...]
    decisions: tuple[Mapping[str, JsonValue], ...]
    guardrails: tuple[Mapping[str, JsonValue], ...]
    selectors: tuple[Mapping[str, JsonValue], ...]
    execution_routes: tuple[Mapping[str, JsonValue], ...]
    hard_guardrails: tuple[Mapping[str, JsonValue], ...]
    publishability_guardrails: tuple[Mapping[str, JsonValue], ...]

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ValueError("manifest schema_version differs from the capability contract")
        object.__setattr__(self, "manifest_id", identifier(self.manifest_id, context="manifest_id"))
        object.__setattr__(
            self, "manifest_version", identifier(self.manifest_version, context="manifest_version")
        )
        for name in ("catalog_ref", "system_policy_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        if self.claim_scope != ENGINEERING_CLAIM_SCOPE:
            raise ValueError("claim_scope must be engineering_simulation_only")
        row_specs = (
            ("metrics", _METRIC_FIELDS, "public metric"),
            ("objectives", _OBJECTIVE_FIELDS, "public objective"),
            ("decisions", _DECISION_FIELDS, "public decision"),
            ("guardrails", _GUARDRAIL_FIELDS, "public guardrail"),
            ("selectors", _SELECTOR_FIELDS, "public selector"),
            ("execution_routes", _ROUTE_FIELDS, "public execution route"),
            ("hard_guardrails", _BINDING_FIELDS, "public hard guardrail"),
            (
                "publishability_guardrails",
                _BINDING_FIELDS,
                "public publishability guardrail",
            ),
        )
        for name, required, context in row_specs:
            rows = tuple(getattr(self, name))
            if not rows:
                raise ValueError(f"{name} must be non-empty")
            object.__setattr__(
                self,
                name,
                _freeze_rows(rows, required=required, context=context),
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "catalog_ref": self.catalog_ref.as_dict(),
            "system_policy_ref": self.system_policy_ref.as_dict(),
            "claim_scope": self.claim_scope,
            "metrics": _thaw_rows(self.metrics),
            "objectives": _thaw_rows(self.objectives),
            "decisions": _thaw_rows(self.decisions),
            "guardrails": _thaw_rows(self.guardrails),
            "selectors": _thaw_rows(self.selectors),
            "execution_routes": _thaw_rows(self.execution_routes),
            "hard_guardrails": _thaw_rows(self.hard_guardrails),
            "publishability_guardrails": _thaw_rows(self.publishability_guardrails),
        }

    @classmethod
    def from_mapping(cls, value: object) -> PublicCapabilityManifest:
        """Strictly restore the public projection used by upstream model requests."""

        raw = as_mapping(value, context="public capability manifest")
        strict_keys(
            raw,
            required={
                "schema_version",
                "manifest_id",
                "manifest_version",
                "catalog_ref",
                "system_policy_ref",
                "claim_scope",
                "metrics",
                "objectives",
                "decisions",
                "guardrails",
                "selectors",
                "execution_routes",
                "hard_guardrails",
                "publishability_guardrails",
            },
            context="public capability manifest",
        )

        def rows(name: str) -> tuple[Mapping[str, JsonValue], ...]:
            return tuple(
                freeze_json_mapping(
                    as_mapping(item, context=f"public capability manifest.{name}[{index}]"),
                    context=f"public capability manifest.{name}[{index}]",
                )
                for index, item in enumerate(as_sequence(raw[name], context=name))
            )

        return cls(
            schema_version=identifier(raw["schema_version"], context="schema_version"),
            manifest_id=identifier(raw["manifest_id"], context="manifest_id"),
            manifest_version=identifier(raw["manifest_version"], context="manifest_version"),
            catalog_ref=ContractRef.from_mapping(
                as_mapping(raw["catalog_ref"], context="catalog_ref")
            ),
            system_policy_ref=ContractRef.from_mapping(
                as_mapping(raw["system_policy_ref"], context="system_policy_ref")
            ),
            claim_scope=identifier(raw["claim_scope"], context="claim_scope"),
            metrics=rows("metrics"),
            objectives=rows("objectives"),
            decisions=rows("decisions"),
            guardrails=rows("guardrails"),
            selectors=rows("selectors"),
            execution_routes=rows("execution_routes"),
            hard_guardrails=rows("hard_guardrails"),
            publishability_guardrails=rows("publishability_guardrails"),
        )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.manifest_id, self.fingerprint)


def build_public_capability_manifest(
    bundle: CapabilityBundle,
) -> PublicCapabilityManifest:
    """Project internal bindings into the capability contract safe for upstream callers."""

    if not isinstance(bundle, CapabilityBundle):
        raise TypeError("bundle must be CapabilityBundle")
    catalog = bundle.catalog
    policy = bundle.system_policy
    return PublicCapabilityManifest(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        manifest_id="cdu-rto-public-capabilities",
        manifest_version="2.0.0",
        catalog_ref=catalog.ref,
        system_policy_ref=policy.ref,
        claim_scope=catalog.claim_scope,
        metrics=tuple(
            {
                "metric_id": item.metric_id,
                "business_name": item.business_name,
                "stage": item.stage,
                "unit": item.unit,
                "direction": item.direction,
                "proxy": item.proxy,
                "availability": item.availability,
                "availability_reason": item.availability_reason,
            }
            for item in catalog.metrics
        ),
        objectives=tuple(
            {
                "objective_id": item.objective_id,
                "business_name": item.business_name,
                "metric_id": item.metric_id,
                "sense": item.sense,
                "normalization_scale": item.normalization_scale,
                "availability": item.availability,
                "availability_reason": item.availability_reason,
            }
            for item in catalog.objectives
        ),
        decisions=tuple(
            {
                "decision_id": item.decision_id,
                "business_name": item.business_name,
                "display_unit": item.display_unit,
                "canonical_unit": item.canonical_unit,
                "lower_bound": item.lower_bound,
                "upper_bound": item.upper_bound,
                "coarse_step": item.coarse_step,
                "refine_step": item.refine_step,
                "availability": item.availability,
                "availability_reason": item.availability_reason,
            }
            for item in catalog.decisions
        ),
        guardrails=tuple(
            {
                "guardrail_id": item.guardrail_id,
                "business_name": item.business_name,
                "metric_id": item.metric_id,
                "stage": item.stage,
                "unit": item.unit,
                "allowed_operators": tuple(item.allowed_operators),
                "availability": item.availability,
                "availability_reason": item.availability_reason,
            }
            for item in catalog.guardrails
        ),
        selectors=tuple(
            {
                "selector_id": item.selector_id,
                "business_name": item.business_name,
                "method": item.method,
                "minimum_objectives": item.minimum_objectives,
                "maximum_objectives": item.maximum_objectives,
                "availability": item.availability,
                "availability_reason": item.availability_reason,
            }
            for item in catalog.selectors
        ),
        execution_routes=tuple(
            {
                "route_id": item.route_id,
                "selector_id": item.selector_id,
                "minimum_objectives": item.minimum_objectives,
                "maximum_objectives": item.maximum_objectives,
                "maximum_m2_candidates": item.maximum_m2_candidates,
                "top_k": item.top_k,
            }
            for item in policy.execution_routes
        ),
        hard_guardrails=_freeze_rows(
            tuple(item.as_dict() for item in policy.hard_guardrails),
            required=_BINDING_FIELDS,
            context="public hard guardrail",
        ),
        publishability_guardrails=_freeze_rows(
            tuple(item.as_dict() for item in policy.publishability_guardrails),
            required=_BINDING_FIELDS,
            context="public publishability guardrail",
        ),
    )
