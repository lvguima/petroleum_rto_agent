from __future__ import annotations

import json
import math

import pytest

from petroleum_rto.cdu.core.types import MaterialStream, stream_from_component_flows


def make_stream(**overrides: object) -> MaterialStream:
    values: dict[str, object] = {
        "name": "test",
        "mass_flow_kg_s": 10.0,
        "temperature_k": 350.0,
        "pressure_pa": 200000.0,
        "mass_fractions": {"naphtha": 0.4, "residue": 0.6},
        "salt_mass_flow_kg_s": 0.001,
    }
    values.update(overrides)
    return MaterialStream(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mass_flow_kg_s", -1.0),
        ("mass_flow_kg_s", math.nan),
        ("temperature_k", 0.0),
        ("pressure_pa", -1.0),
        ("salt_mass_flow_kg_s", math.inf),
    ],
)
def test_invalid_scalar_is_rejected(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        make_stream(**{field: value})


def test_composition_must_already_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to one"):
        make_stream(mass_fractions={"naphtha": 0.2})


def test_unknown_and_negative_components_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        make_stream(mass_fractions={"naptha_typo": 1.0})
    with pytest.raises(ValueError, match="non-negative"):
        make_stream(mass_fractions={"naphtha": 1.1, "residue": -0.1})


def test_stream_is_immutable_and_json_serializable() -> None:
    stream = make_stream(metadata={"source": "test"})
    with pytest.raises(TypeError):
        stream.mass_fractions["naphtha"] = 0.5  # type: ignore[index]
    encoded = json.dumps(stream.as_dict(), allow_nan=False)
    assert "naphtha" in encoded


def test_construct_from_component_flows() -> None:
    stream = stream_from_component_flows(
        "built",
        {"naphtha": 2.0, "residue": 3.0},
        temperature_k=400.0,
        pressure_pa=100000.0,
        salt_mass_flow_kg_s=0.01,
    )
    assert stream.mass_flow_kg_s == pytest.approx(5.0)
    assert stream.mass_fractions["naphtha"] == pytest.approx(0.4)
    assert stream.component_flow_kg_s("residue") == pytest.approx(3.0)
