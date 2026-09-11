"""MV binding, batch execution and persisted multi-variable evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace

import pytest
from test_hysys_reader import FakeQuantity
from test_point import diagnostic  # noqa: F401

from petroleum_rto.assistant.presentation import render_optimization_result
from petroleum_rto.rto.runtime import steady
from petroleum_rto.simulation import mv, point
from petroleum_rto.simulation.point_evidence import read_point_result

TARGETS = [
    {"variable_id": "C-1102.39_temperature_C", "value": 156.9, "unit": "C"},
    {"variable_id": "Water1.massflow_kg_h", "value": 43.0, "unit": "kg/h"},
    {"variable_id": "crude_oil.pressure_kPa", "value": 43.0, "unit": "kPa"},
]


@pytest.fixture
def batch(diagnostic, monkeypatch):  # noqa: F811
    state = diagnostic
    state.fail_mv = False

    def set_value(quantity, value, unit):
        assert unit == quantity.unit
        state.events.append(("SetValue", unit, value))
        if state.fail_mv and unit == "kg/h":
            raise RuntimeError("synthetic write failure")
        quantity.value = value
        quantity.Value = value / 3600 if unit == "kg/h" else value

    monkeypatch.setattr(FakeQuantity, "SetValue", set_value, raising=False)

    def configure(case):
        for b in state.catalog.variables:
            q = case.table.cells[f"C{b.row}"].ImportedVariable
            if b.role == "mv":
                q.Value = mv.internal_value(q.value, b.quantity_type)

    state.configure_open = configure
    state.configure_application = lambda app: configure(app.SimulationCases.Item(0))
    state.start = replace(
        state.start,
        variables=tuple(
            replace(r, internal_value=mv.internal_value(r.value, r.quantity_type))
            if r.role == "mv"
            else r
            for r in state.start.variables
        ),
    )
    (state.directory / "snapshot.json").write_text(
        json.dumps(state.start.to_dict()), encoding="utf-8"
    )
    manifest = json.loads(state.manifest.read_text())
    manifest["files"]["snapshot.json"] = hashlib.sha256(
        (state.directory / "snapshot.json").read_bytes()
    ).hexdigest()
    state.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    return state


def test_all_24_mv_bindings_accept_their_current_values_without_running(batch):
    changes = [
        {"variable_id": r.variable_id, "value": r.value, "unit": r.unit}
        for r in batch.start.variables
        if r.role == "mv"
    ]
    assert len(mv.parse_changes(changes, batch.start, batch.catalog)) == 24
    assert batch.events == []


@pytest.mark.parametrize("targets", [TARGETS[:1], TARGETS[1:2], TARGETS[2:], TARGETS])
def test_selected_mv_batch_uses_owned_case_and_strict_evidence(batch, targets):
    report = point.run_mv_point(batch.manifest, targets, batch.output)
    result = read_point_result(report)
    assert result.status == "passed"
    assert [c.to_dict() for c in result.changes] == targets
    assert result.target_c is None
    raw = json.loads(report.read_text())
    assert raw["schema_id"] == "hysys-mv-point"
    assert raw["checks"]["source_observation_unchanged"]
    assert raw["checks"]["restoration_comparison"]["equivalent"]
    assert not raw["checks"]["change_comparison"]["input_differences"]
    assert raw["checks"]["boundary_matches_changed_point"]
    assert all(r["setpoint_matches"] for r in raw["checks"]["target_response"]["variables"])
    assert batch.events.count(("Run", "candidate.hsc")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        [],
        TARGETS + TARGETS[:1],
        [{"variable_id": "Naptha.massflow_kg_h", "value": 1.0, "unit": "kg/h"}],
        [{"variable_id": "crude_oil.pressure_kPa", "value": True, "unit": "kPa"}],
        [{"variable_id": "crude_oil.pressure_kPa", "value": 1.0, "unit": "MPa"}],
        [{"variable_id": "crude_oil.pressure_kPa", "value": float("inf"), "unit": "kPa"}],
        [{"variable_id": "crude_oil.massflow_kg_h", "value": -1.0, "unit": "kg/h"}],
        [{"variable_id": "flash_column.liquid_volume_percent", "value": 101.0, "unit": "%"}],
        [{"variable_id": "../outside", "value": 1.0, "unit": "C"}],
    ],
)
def test_invalid_batch_fails_before_open_or_output(batch, bad):
    with pytest.raises(ValueError):
        point.run_mv_point(batch.manifest, bad, batch.output)
    assert not batch.output.exists()
    assert not batch.events


def test_partial_write_failure_discards_work_without_solving_and_restores(batch):
    batch.fail_mv = True
    result = read_point_result(point.run_mv_point(batch.manifest, TARGETS, batch.output))
    assert result.status == "failed"
    assert result.changed is None and result.restored is not None
    assert ("Run", "candidate.hsc") not in batch.events
    assert ("Close", "candidate.hsc", False) in batch.events
    assert any(e.stage == "change" for e in result.errors)


def test_modified_target_in_saved_report_is_rejected(batch):
    report = point.run_mv_point(batch.manifest, TARGETS, batch.output)
    raw = json.loads(report.read_text())
    raw["changes"][1]["value"] += 1
    report.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        read_point_result(report)


def test_mv_plan_pair_restores_and_reloads_without_new_execution(batch, monkeypatch, tmp_path):
    catalog = json.loads((batch.directory / "variables.json").read_text())
    monkeypatch.setattr(steady.backend, "read_catalog", lambda: catalog)
    monkeypatch.setattr(
        steady.backend,
        "capture",
        lambda context, directory, workspace: shutil.copytree(batch.directory, directory),
    )
    plan = steady.prepare_comparison(batch.start.to_dict(), list(reversed(TARGETS)))
    assert plan.changes == TARGETS  # Input order never changes plan identity or write order.
    assert plan == steady.prepare_comparison(batch.start.to_dict(), TARGETS)
    assert plan.as_dict()["schema_version"] == "2.0.0"
    result = steady.execute_comparison(plan, run_root=tmp_path / "rto", workspace=tmp_path)
    assert result["result"]["status"] == "comparison_only"
    assert result["result"]["schema_version"] == "2.0.0"
    assert len(result["result"]["candidate"]["responses"]) == 3
    rendered = render_optimization_result(result)
    assert all(c["variable_id"] in rendered for c in TARGETS)
    assert len(result["result"]["comparisons"]) == 12
    assert batch.events.count(("Run", "candidate.hsc")) == 2
    assert steady.read_prepared_result(plan, run_root=tmp_path / "rto") == result
    assert steady.execute_comparison(plan, run_root=tmp_path / "rto", workspace=tmp_path) == result
    assert batch.events.count(("Run", "candidate.hsc")) == 2


def test_unselected_input_drift_is_not_accepted(batch):
    changed = replace(
        batch.start,
        variables=tuple(
            replace(r, value=r.value + 1, internal_value=r.internal_value + 1)
            if r.variable_id == "crude_oil.pressure_kPa"
            else r
            for r in batch.start.variables
        ),
    )
    changes = mv.parse_changes([dict(TARGETS[0], value=156.8)], batch.start, batch.catalog)
    comparison, _ = mv.check_changes(batch.start, changed, changes, batch.catalog)
    assert comparison["input_differences"]
