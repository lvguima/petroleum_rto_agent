from __future__ import annotations

import pytest

from petroleum_rto.cdu.core.conservation import material_balance
from petroleum_rto.cdu.core.types import MaterialStream


def test_material_balance_tracks_components_and_salt() -> None:
    feed = MaterialStream(
        "feed",
        10.0,
        300.0,
        200000.0,
        {"naphtha": 0.4, "residue": 0.6},
        salt_mass_flow_kg_s=0.01,
    )
    light = MaterialStream(
        "light",
        4.0,
        300.0,
        100000.0,
        {"naphtha": 1.0},
    )
    heavy = MaterialStream(
        "heavy",
        6.0,
        400.0,
        100000.0,
        {"residue": 1.0},
        salt_mass_flow_kg_s=0.01,
    )
    report = material_balance([feed], [light, heavy])
    assert report.passed()
    assert report.component_residuals_kg_s["naphtha"] == pytest.approx(0.0)
