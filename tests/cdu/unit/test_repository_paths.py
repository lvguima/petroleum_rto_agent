from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.cdu.repository import resolve_cdu_repository_path


@pytest.mark.parametrize(
    ("resource_id", "physical_path"),
    [
        ("configs/models/model.json", "configs/cdu/models/model.json"),
        ("data/gold/result.json", "data/cdu/gold/result.json"),
        ("reports/modeling/report.md", "reports/cdu/report.md"),
        ("base_files/raw.xlsx", "data/cdu/raw/raw.xlsx"),
        ("docs/STATUS.md", "docs/STATUS.md"),
    ],
)
def test_cdu_resource_ids_map_to_namespaced_paths(
    tmp_path: Path,
    resource_id: str,
    physical_path: str,
) -> None:
    assert resolve_cdu_repository_path(tmp_path, resource_id) == tmp_path / physical_path


@pytest.mark.parametrize(
    "resource_id",
    ["", "../outside.json", "/absolute.json", "configs\\model.json"],
)
def test_cdu_resource_path_rejects_unsafe_ids(
    tmp_path: Path,
    resource_id: str,
) -> None:
    with pytest.raises(ValueError):
        resolve_cdu_repository_path(tmp_path, resource_id)


def test_existing_legacy_layout_remains_read_compatible(tmp_path: Path) -> None:
    legacy = tmp_path / "configs" / "models" / "model.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}\n", encoding="utf-8")

    assert resolve_cdu_repository_path(tmp_path, "configs/models/model.json") == legacy
