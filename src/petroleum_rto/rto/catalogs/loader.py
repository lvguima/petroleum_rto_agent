"""Load the fixed RTO V1 policy bundle without filesystem scanning."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

from ..contracts import (
    ConstraintProfileV1,
    DecisionVariableCatalogV1,
    KpiCatalogV1,
    MultiObjectivePolicyV2,
    ObjectiveCatalogV2,
    OperatingContextV1,
    OptimizationIntentV1,
    OptimizationPolicyV1,
    PreferenceCatalogV2,
    PublishabilityCatalogV2,
)


@dataclass(frozen=True)
class RtoCatalogBundle:
    """The complete, source-controlled policy input required by ProblemBuilder."""

    decision_catalog: DecisionVariableCatalogV1
    kpi_catalog: KpiCatalogV1
    constraint_profile: ConstraintProfileV1
    policy: OptimizationPolicyV1
    context: OperatingContextV1
    intent: OptimizationIntentV1


@dataclass(frozen=True)
class RtoCatalogBundleV2:
    """V1 physical catalogs plus the strict multi-objective policy layer."""

    base: RtoCatalogBundle
    objective_catalog: ObjectiveCatalogV2
    preference_catalog: PreferenceCatalogV2
    publishability_catalog: PublishabilityCatalogV2
    policy: MultiObjectivePolicyV2


_FILES = {
    "decision_catalog": "catalogs/decision_variables_v1.json",
    "kpi_catalog": "catalogs/kpis_v1.json",
    "constraint_profile": "profiles/constraints_v1.json",
    "policy": "profiles/optimization_policy_v1.json",
    "context": "contexts/case_20260604_v1.json",
    "intent": "intents/minimize_specific_furnace_energy_v1.json",
}

_V2_FILES = {
    "objective_catalog": "catalogs/objectives_v2.json",
    "preference_catalog": "profiles/preferences_v2.json",
    "publishability_catalog": "profiles/publishability_v2.json",
    "policy": "profiles/multiobjective_policy_v2.json",
}


def _read_object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must contain one JSON object")
    return cast(Mapping[str, object], value)


def _bundle_from_objects(values: Mapping[str, Mapping[str, object]]) -> RtoCatalogBundle:
    return RtoCatalogBundle(
        decision_catalog=DecisionVariableCatalogV1.from_mapping(values["decision_catalog"]),
        kpi_catalog=KpiCatalogV1.from_mapping(values["kpi_catalog"]),
        constraint_profile=ConstraintProfileV1.from_mapping(values["constraint_profile"]),
        policy=OptimizationPolicyV1.from_mapping(values["policy"]),
        context=OperatingContextV1.from_mapping(values["context"]),
        intent=OptimizationIntentV1.from_mapping(values["intent"]),
    )


def _packaged_bundle() -> RtoCatalogBundle:
    resource = resources.files("petroleum_rto.rto.data").joinpath("rto_v1_bundle.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("packaged RTO V1 bundle must be one JSON object")
    expected = set(_FILES)
    if set(value) != expected:
        raise ValueError("packaged RTO V1 bundle fields differ")
    objects = {key: _read_mapping(value[key], context=f"packaged bundle.{key}") for key in _FILES}
    return _bundle_from_objects(objects)


def _read_mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be one JSON object")
    return cast(Mapping[str, object], value)


def load_rto_v1_bundle(repo_root: Path | None = None) -> RtoCatalogBundle:
    """Load fixed V1 inputs from a checkout or the installed package copy."""

    if repo_root is None:
        return _packaged_bundle()
    if not isinstance(repo_root, Path):
        raise TypeError("repo_root must be a pathlib.Path or None")
    root = repo_root.resolve()
    config_root = root / "configs" / "rto"
    resolved: dict[str, Path] = {}
    for key, relative in _FILES.items():
        path = (config_root / relative).resolve()
        if not path.is_relative_to(config_root.resolve()) or not path.is_file():
            raise ValueError(f"required RTO V1 config is missing or unsafe: {relative}")
        resolved[key] = path
    return _bundle_from_objects({key: _read_object(path) for key, path in resolved.items()})


def _v2_bundle_from_objects(
    base: RtoCatalogBundle,
    values: Mapping[str, Mapping[str, object]],
) -> RtoCatalogBundleV2:
    return RtoCatalogBundleV2(
        base=base,
        objective_catalog=ObjectiveCatalogV2.from_mapping(values["objective_catalog"]),
        preference_catalog=PreferenceCatalogV2.from_mapping(values["preference_catalog"]),
        publishability_catalog=PublishabilityCatalogV2.from_mapping(
            values["publishability_catalog"]
        ),
        policy=MultiObjectivePolicyV2.from_mapping(values["policy"]),
    )


def _packaged_v2_bundle() -> RtoCatalogBundleV2:
    resource = resources.files("petroleum_rto.rto.data").joinpath("rto_v2_bundle.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("packaged RTO V2 bundle must be one JSON object")
    if set(value) != set(_V2_FILES):
        raise ValueError("packaged RTO V2 bundle fields differ")
    objects = {
        key: _read_mapping(value[key], context=f"packaged V2 bundle.{key}") for key in _V2_FILES
    }
    return _v2_bundle_from_objects(load_rto_v1_bundle(), objects)


def load_rto_v2_bundle(repo_root: Path | None = None) -> RtoCatalogBundleV2:
    """Load strict V2 policy inputs while reusing the unchanged V1 physical catalogs."""

    if repo_root is None:
        return _packaged_v2_bundle()
    if not isinstance(repo_root, Path):
        raise TypeError("repo_root must be a pathlib.Path or None")
    root = repo_root.resolve()
    config_root = root / "configs" / "rto"
    resolved: dict[str, Path] = {}
    for key, relative in _V2_FILES.items():
        path = (config_root / relative).resolve()
        if not path.is_relative_to(config_root.resolve()) or not path.is_file():
            raise ValueError(f"required RTO V2 config is missing or unsafe: {relative}")
        resolved[key] = path
    return _v2_bundle_from_objects(
        load_rto_v1_bundle(repo_root),
        {key: _read_object(path) for key, path in resolved.items()},
    )
