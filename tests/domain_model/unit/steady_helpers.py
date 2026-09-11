"""Synthetic steady observations/receipts for Agent tests; never attach to HYSYS."""

from pathlib import Path

from petroleum_rto.rto.runtime.steady import LIMITATIONS
from petroleum_rto.simulation.hysys import load_catalog
from petroleum_rto.simulation.models import (
    OperatingSnapshot,
    SolverState,
    SpecificationReading,
    StageReading,
    VariableReading,
)
from petroleum_rto.simulation.mv import internal_value


def synthetic_context(workspace: Path) -> dict:
    catalog = load_catalog()
    values = {
        b.variable_id: (
            3600.0
            if b.quantity_type in {"mass_flow", "heat_flow"}
            else 0.5
            if b.quantity_type == "ratio"
            else 42.0
        )
        for b in catalog.variables
    }
    values["C-1102.39_temperature_C"] = 156.8
    return OperatingSnapshot(
        case_id="mjh_atm",
        observed_at_utc="2026-09-11T08:30:00+00:00",
        source_case_path=str(workspace / "hysys/mjh_ATM.hsc"),
        source_disk_sha256="a" * 64,
        hysys_version="12.0",
        memory_is_dirty=True,
        solver=SolverState(True, False, True, True),
        degrees_of_freedom=0,
        variables=tuple(
            VariableReading(
                variable_id=b.variable_id,
                role=b.role,
                row=b.row,
                object_name=b.object_name,
                property_name=b.property_name,
                quantity_type=b.quantity_type,
                unit=b.unit,
                value=values[b.variable_id],
                internal_value=internal_value(values[b.variable_id], b.quantity_type),
                state=1 if b.role == "mv" else 0,
                can_modify=b.role == "mv",
            )
            for b in catalog.variables
        ),
        stages=tuple(StageReading(f"stage-{i}", 150, 100, 1000, 2000) for i in range(73)),
        specifications=tuple(
            SpecificationReading(
                b.column_specification,
                "ColumnTemperatureSpec" if b.quantity_type == "temperature" else "ColumnFlowSpec",
                True,
                True,
                b.quantity_type,
                b.unit,
                values[b.variable_id],
                values[b.variable_id],
            )
            for b in catalog.variables
            if b.column_specification is not None
        ),
        consistent_observation=True,
    ).to_dict()


def receipt() -> dict:
    return {
        "status": "complete",
        "workflow_id": "steady-" + "a" * 16,
        "result": {
            "schema_id": "steady-comparison-result",
            "schema_version": "1.0.0",
            "status": "comparison_only",
            "baseline": {"status": "passed", "target_c": 156.8, "actual_c": 156.8001},
            "candidate": {"status": "passed", "target_c": 156.9, "actual_c": 156.9002},
            "comparisons": [
                {
                    "metric_id": "mass_flow:AGO",
                    "label": "AGO物流总流量",
                    "unit": "kg/h",
                    "baseline": 56000.0,
                    "candidate": 57000.0,
                    "delta": 1000.0,
                }
            ],
            "eligible_for_optimization": False,
            "limitations": list(LIMITATIONS),
        },
    }
