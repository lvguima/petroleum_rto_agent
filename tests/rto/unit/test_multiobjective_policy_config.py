from __future__ import annotations

import json
from pathlib import Path
from typing import cast


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_r7_three_objective_policy_is_consistent(repo_root: Path) -> None:
    root = repo_root / "configs" / "rto"
    objectives = _object(root / "catalogs" / "objectives_v2.json")
    preferences = _object(root / "profiles" / "preferences_v2.json")
    policy = _object(root / "profiles" / "multiobjective_policy_v2.json")
    publishability = _object(root / "profiles" / "publishability_v2.json")
    kpis = _object(root / "catalogs" / "kpis_v1.json")

    objective_rows = cast(list[dict[str, object]], objectives["objectives"])
    metric_ids = tuple(cast(str, item["metric_id"]) for item in objective_rows)
    assert metric_ids == (
        "quality_proxy_max_abs_relative_change",
        "valuable_distillate_yield",
        "specific_furnace_fuel_energy_mj_per_t",
    )
    assert tuple(item["sense"] for item in objective_rows) == (
        "minimize",
        "maximize",
        "minimize",
    )
    assert all(item["stage"] == "M2" for item in objective_rows)
    assert all(cast(float, item["normalization_scale"]) > 0.0 for item in objective_rows)
    assert objective_rows[0]["relative_improvement_policy"] == "zero-baseline-null"

    kpi_rows = cast(list[dict[str, object]], kpis["kpis"])
    kpi_directions = {cast(str, item["kpi_id"]): item["direction"] for item in kpi_rows}
    assert tuple(kpi_directions[item] for item in metric_ids) == (
        "minimize",
        "maximize",
        "minimize",
    )

    preference = cast(list[dict[str, object]], preferences["profiles"])[0]
    assert tuple(cast(list[str], preference["objective_order"])) == metric_ids
    assert preference["method"] == "lexicographic"
    assert "weights" not in preference

    evaluation = cast(dict[str, object], policy["evaluation"])
    search = cast(dict[str, object], policy["search"])
    assert evaluation["top_k"] == 5
    assert search["points_per_dimension"] == 9
    assert search["maximum_m2_candidates"] == 9**2
    assert search["randomness"] == "none"
    assert policy["constraint_profile_id"] == "cdu-v1-constraints"

    publish_profile = cast(list[dict[str, object]], publishability["profiles"])[0]
    assert publish_profile == {
        "profile_id": "minimum-energy-improvement-v1",
        "metric_id": "specific_furnace_fuel_energy_mj_per_t",
        "comparison": "relative-directional-improvement-ge",
        "limit": 0.005,
        "failure_status": "feasible_not_publishable",
    }


def test_r7_v1_policy_files_remain_byte_stable(repo_root: Path) -> None:
    expected = {
        "catalogs/decision_variables_v1.json": (
            "e258f1242c10bd1339cdd443baa935066960b2aab703a1a48ed76fb7b4ab7f3d"
        ),
        "catalogs/kpis_v1.json": (
            "c111bc50ee9a6e1f8dbdd97979b1497a8bfe9c8cfdddd01e4e6ee8f2ed53ef5c"
        ),
        "profiles/constraints_v1.json": (
            "14d1e99ed5c1de390d6b5f02eba5fa8a4b38d24745c9fe11500dbbe50e0ec3cb"
        ),
        "profiles/optimization_policy_v1.json": (
            "6b23dc6f4548b579b1b2fb3309224e42329d687fbbde14858f65b41433ab7eb6"
        ),
        "intents/minimize_specific_furnace_energy_v1.json": (
            "4a373a737a1376fd60ed0f406bb5e3291d2ee2ba97827a13b04a381b4d1fed2b"
        ),
        "requests/user_defined_feed_400_v1.json": (
            "611ed8160b73d956196955c92b297909ecf9f0bd58eb996a0bd4c2a7bc3e9e1c"
        ),
    }
    import hashlib

    root = repo_root / "configs" / "rto"
    assert {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in expected
    } == expected
