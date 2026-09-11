"""Synthetic boundary values for point orchestration and persisted-evidence tests."""

import json

from petroleum_rto.simulation.boundary import (
    DEFAULT_BOUNDARY,
    BoundaryDefinition,
    BoundaryReadings,
    BoundarySnapshot,
    Component,
    EnergyReading,
    MaterialReading,
)
from petroleum_rto.simulation.models import OperatingSnapshot


def boundary_for(core: OperatingSnapshot) -> BoundarySnapshot:
    definition = BoundaryDefinition.from_dict(
        json.loads(DEFAULT_BOUNDARY.read_text(encoding="utf-8"))
    )

    def value(name: str, quantity: str) -> float:
        return next(
            x.value for x in core.variables if x.object_name == name and x.quantity_type == quantity
        )

    readings = BoundaryReadings(
        "Synthetic basis",
        "Synthetic property package",
        tuple(Component("H2O" if i == 7 else f"component-{i}", i >= 13, "") for i in range(44)),
        tuple(
            MaterialReading(
                x.name,
                x.direction,
                value(x.name, "temperature") if x.name == "Crude_Oil" else 100.0,
                value(x.name, "pressure") if x.name == "Crude_Oil" else 100.0,
                value(x.name, "mass_flow"),
                -1.0,
                -value(x.name, "mass_flow"),
                0.0,
                (0.0,) * 7 + (1.0,) + (0.0,) * 36,
            )
            for x in definition.materials
            if x.direction in ("in", "out")
        ),
        tuple(
            EnergyReading(x.scope, x.name, x.direction, 1000.0)
            for x in definition.energy
            if x.direction != "bridge"
        ),
        1000.0,
    )
    return BoundarySnapshot(definition, core, core, readings, readings)
