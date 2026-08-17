from __future__ import annotations

import json
import math

import pytest

from petroleum_rto.cdu.core.types import (
    BalanceReport,
    ControlSignals,
    EquipmentState,
    MaterialStream,
    SimulationResult,
    UnitResult,
)


def sample_stream() -> MaterialStream:
    return MaterialStream("sample", 1.0, 300.0, 100000.0, {"naphtha": 1.0})


def test_balance_report_checks_all_conserved_quantities() -> None:
    report = BalanceReport(
        inlet_kg_s=10.0,
        outlet_kg_s=10.0,
        component_residuals_kg_s={"naphtha": 1e-10},
        salt_residual_kg_s=1e-13,
        energy_residual_w=0.1,
    )
    assert report.passed(energy_atol_w=1.0)
    assert not report.passed(component_atol_kg_s=1e-12)
    assert json.dumps(report.as_dict(), allow_nan=False)


def test_results_are_serializable() -> None:
    stream = sample_stream()
    balance = BalanceReport(1.0, 1.0)
    unit = UnitResult(
        {"out": stream},
        duty_w=2.0,
        diagnostics={"ratio": 0.5},
        balance=balance,
    )
    state = EquipmentState("heater", {"temperature_k": 300.0})
    signals = ControlSignals({"duty_w": 2.0}, {"duty": "manual"})
    result = SimulationResult(
        "success",
        streams={"out": stream},
        equipment_states={"heater": state},
        control_signals=signals,
        balance=balance,
        metrics={"runtime_s": 0.01},
        events=({"time_s": 0.0, "kind": "start"},),
        versions={"model_version": "test-0.1.0"},
    )
    assert json.dumps(unit.as_dict(), allow_nan=False)
    assert json.dumps(result.as_dict(), allow_nan=False)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BalanceReport(math.nan, 1.0),
        lambda: UnitResult({"out": sample_stream()}, duty_w=math.inf),
        lambda: EquipmentState("heater", {"temperature_k": math.nan}),
        lambda: ControlSignals({"command": math.inf}),
        lambda: SimulationResult("success", metrics={"bad": math.nan}),
    ],
)
def test_result_contracts_reject_nonfinite_values(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_simulation_status_is_strict() -> None:
    with pytest.raises(ValueError, match="status"):
        SimulationResult("maybe")
