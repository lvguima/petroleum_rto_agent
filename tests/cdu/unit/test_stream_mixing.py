from __future__ import annotations

import pytest

from petroleum_rto.cdu.core.types import MaterialStream, merge_streams


def test_merge_preserves_bulk_components_and_salt() -> None:
    first = MaterialStream(
        "first",
        10.0,
        300.0,
        300000.0,
        {"naphtha": 1.0},
        salt_mass_flow_kg_s=0.001,
    )
    second = MaterialStream(
        "second",
        20.0,
        450.0,
        200000.0,
        {"residue": 0.75, "water": 0.25},
        salt_mass_flow_kg_s=0.002,
    )
    mixed = merge_streams("mixed", [first, second])
    assert mixed.mass_flow_kg_s == pytest.approx(30.0)
    assert mixed.component_flow_kg_s("naphtha") == pytest.approx(10.0)
    assert mixed.component_flow_kg_s("residue") == pytest.approx(15.0)
    assert mixed.component_flow_kg_s("water") == pytest.approx(5.0)
    assert mixed.salt_mass_flow_kg_s == pytest.approx(0.003)
    assert mixed.temperature_k == pytest.approx(400.0)
    assert mixed.pressure_pa == pytest.approx(200000.0)


def test_merge_requires_positive_combined_flow() -> None:
    zero = MaterialStream("zero", 0.0, 300.0, 100000.0, {"naphtha": 1.0})
    with pytest.raises(ValueError, match="positive"):
        merge_streams("bad", [zero])
