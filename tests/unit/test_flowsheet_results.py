from __future__ import annotations

import json
import math
import operator
from collections.abc import Mapping
from typing import cast

import pytest

from petroleum_rto.cdu.core.types import BalanceReport, MaterialStream, UnitResult
from petroleum_rto.cdu.flowsheet.results import SteadyFlowsheetResult


def sample_stream(name: str = "product") -> MaterialStream:
    return MaterialStream(name, 1.0, 300.0, 101325.0, {"naphtha": 1.0})


def sample_result() -> SteadyFlowsheetResult:
    stream = sample_stream()
    balance = BalanceReport(
        inlet_kg_s=1.0,
        outlet_kg_s=1.0,
        component_residuals_kg_s={"naphtha": 0.0},
    )
    unit_result = UnitResult(
        outlets={"out": stream},
        diagnostics={"temperature_k": 300.0},
        balance=balance,
    )
    return SteadyFlowsheetResult(
        status="success",
        streams={"product": stream},
        products={"gasoline": stream},
        unit_results={"column": unit_result},
        qualities={"gasoline": {"density_kg_m3": 720.0}},
        balance=balance,
        diagnostics={"iterations": 1.0},
        versions={"model_version": "test-0.1.0"},
        input_fingerprint="a" * 64,
        warnings=("test warning",),
    )


def test_steady_flowsheet_result_constructs_and_serializes() -> None:
    result = sample_result()

    payload = result.as_dict()

    assert result.status == "success"
    assert payload["input_fingerprint"] == "a" * 64
    assert json.dumps(payload, allow_nan=False)


def test_steady_flowsheet_result_mappings_are_immutable() -> None:
    result = sample_result()

    with pytest.raises(TypeError):
        operator.setitem(result.streams, "other", sample_stream("other"))
    with pytest.raises(TypeError):
        operator.setitem(result.qualities["gasoline"], "flash_point_k", 320.0)
    with pytest.raises(TypeError):
        operator.setitem(result.diagnostics, "iterations", 2.0)


def test_steady_flowsheet_result_rejects_invalid_values() -> None:
    valid = sample_result()
    invalid_streams = cast(Mapping[str, MaterialStream], {"bad": object()})

    with pytest.raises(TypeError, match="streams"):
        SteadyFlowsheetResult(
            status="success",
            streams=invalid_streams,
            products=valid.products,
            unit_results=valid.unit_results,
            qualities=valid.qualities,
            balance=valid.balance,
            diagnostics=valid.diagnostics,
            versions=valid.versions,
            input_fingerprint=valid.input_fingerprint,
        )
    with pytest.raises(TypeError, match="diagnostics"):
        SteadyFlowsheetResult(
            status="success",
            streams=valid.streams,
            products=valid.products,
            unit_results=valid.unit_results,
            qualities=valid.qualities,
            balance=valid.balance,
            diagnostics={"bad": math.nan},
            versions=valid.versions,
            input_fingerprint=valid.input_fingerprint,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        SteadyFlowsheetResult(
            status="success",
            streams=valid.streams,
            products=valid.products,
            unit_results=valid.unit_results,
            qualities=valid.qualities,
            balance=valid.balance,
            diagnostics=valid.diagnostics,
            versions=valid.versions,
            input_fingerprint="not-a-fingerprint",
        )


def test_as_simulation_result_preserves_status_and_balance() -> None:
    result = sample_result()

    simulation_result = result.as_simulation_result()

    assert simulation_result.status == result.status
    assert simulation_result.balance is result.balance
    assert simulation_result.streams == result.streams
    assert simulation_result.metrics == result.diagnostics
    assert simulation_result.versions == result.versions
