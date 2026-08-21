from __future__ import annotations

from pathlib import Path

from petroleum_rto.cdu.core.config import load_component_catalog
from petroleum_rto.cdu.properties.components import ALL_COMPONENTS, ComponentCatalog


def test_component_catalog_is_complete_and_round_trips(repo_root: Path) -> None:
    path = repo_root / "configs/cdu/models/components_v0.1.0.json"
    catalog = load_component_catalog(path)
    assert set(catalog.components) == set(ALL_COMPONENTS)
    assert all(component.source for component in catalog.components.values())
    assert all(component.confidence in {"low", "medium", "high"} for component in catalog.components.values())
    rebuilt = ComponentCatalog.from_mapping(catalog.as_dict())
    assert rebuilt == catalog
