from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from petroleum_rto.rto.unified_inputs import (
    IntentResolver,
    ObjectiveSense,
    OptimizationIntent,
    PreferenceRequest,
    ResultRequest,
    load_optimization_intent,
)


def _raw(*, multi: bool = False) -> dict[str, Any]:
    objectives: list[dict[str, object]] = [
        {
            "metric_id": "specific_furnace_fuel_energy_mj_per_t",
            "sense": "minimize",
            "priority": 1,
        }
    ]
    if multi:
        objectives = [
            {
                "metric_id": "quality_proxy_max_abs_relative_change",
                "sense": "minimize",
                "priority": 1,
            },
            {
                "metric_id": "valuable_distillate_yield",
                "sense": "maximize",
                "priority": 2,
            },
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "sense": "minimize",
                "priority": 3,
            },
        ]
    objective_ids = [str(item["metric_id"]) for item in objectives]
    return {
        "schema_id": "optimization-intent",
        "schema_version": "1.0.0",
        "intent_id": "unified-multi" if multi else "unified-single",
        "objectives": objectives,
        "decision_variables": [
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        ],
        "constraints": [],
        "preference": {
            "method": "lexicographic" if multi else "single-objective",
            "objective_order": objective_ids,
        },
        "result_request": {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": multi,
            "max_candidates": 5 if multi else 1,
        },
        "ambiguities": [],
    }


class _Capabilities:
    def __init__(self) -> None:
        self._objectives: dict[str, ObjectiveSense] = {
            "quality_proxy_max_abs_relative_change": "minimize",
            "valuable_distillate_yield": "maximize",
            "specific_furnace_fuel_energy_mj_per_t": "minimize",
        }
        self._decisions = {
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        }
        self._constraints = {"m2-structural-numeric", "m4-stability-acceptance"}

    def objective_sense(self, metric_id: str) -> ObjectiveSense | None:
        return self._objectives.get(metric_id)

    def supports_decision_variable(self, variable_id: str) -> bool:
        return variable_id in self._decisions

    def supports_constraint(self, constraint_id: str) -> bool:
        return constraint_id in self._constraints

    def supports_preference(
        self,
        preference: PreferenceRequest,
        objective_ids: tuple[str, ...],
    ) -> bool:
        expected = "single-objective" if len(objective_ids) == 1 else "lexicographic"
        return preference.method == expected and preference.objective_order == objective_ids

    def supports_result_request(
        self,
        result_request: ResultRequest,
        objective_ids: tuple[str, ...],
    ) -> bool:
        maximum = 1 if len(objective_ids) == 1 else 5
        return (
            result_request.output_kind == "steady-setpoint-vector"
            and result_request.max_candidates <= maximum
        )


@pytest.mark.parametrize("multi", [False, True])
def test_unified_intent_accepts_one_or_many_objectives_and_round_trips(multi: bool) -> None:
    intent = OptimizationIntent.from_mapping(_raw(multi=multi))

    assert len(intent.objectives) == (3 if multi else 1)
    assert OptimizationIntent.from_mapping(intent.as_dict()) == intent
    assert len(intent.fingerprint) == 64
    assert not hasattr(intent, "operating_context")
    assert not hasattr(intent, "profile_id")
    assert not hasattr(intent, "algorithm_id")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update({"context": {}}), "fields differ"),
        (lambda raw: raw.update({"profile_id": "legacy-profile"}), "fields differ"),
        (lambda raw: raw.update({"algorithm_id": "solver-choice"}), "fields differ"),
        (lambda raw: raw.update({"objectives": []}), "at least one"),
        (
            lambda raw: raw["objectives"].append(dict(raw["objectives"][0])),
            "unique",
        ),
        (
            lambda raw: raw["objectives"][0].update({"priority": 2}),
            "contiguous",
        ),
        (
            lambda raw: raw["result_request"].update(
                {"include_alternatives": False, "max_candidates": 2}
            ),
            "must be 1",
        ),
    ],
)
def test_unified_intent_rejects_unsafe_or_out_of_scope_shapes(mutation, message: str) -> None:
    raw = _raw()
    mutation(raw)

    with pytest.raises((TypeError, ValueError), match=message):
        OptimizationIntent.from_mapping(raw)


def test_unified_loader_rejects_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    valid = tmp_path / "intent.json"
    valid.write_text(json.dumps(_raw(), ensure_ascii=False), encoding="utf-8")
    assert load_optimization_intent(valid) == OptimizationIntent.from_mapping(_raw())

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_id":"optimization-intent","schema_id":"optimization-intent"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_optimization_intent(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        json.dumps(_raw()).replace('"max_candidates": 1', '"max_candidates": NaN'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-finite"):
        load_optimization_intent(nonfinite)


def test_versioned_examples_share_one_schema_for_single_and_multi(repo_root: Path) -> None:
    root = repo_root / "configs" / "rto" / "intents"
    single = load_optimization_intent(root / "minimize_specific_furnace_energy.json")
    multi = load_optimization_intent(root / "quality_yield_energy.json")

    assert single.schema_id == multi.schema_id == "optimization-intent"
    assert single.schema_version == multi.schema_version == "1.0.0"
    assert len(single.objectives) == 1
    assert len(multi.objectives) == 3


@pytest.mark.parametrize("multi", [False, True])
def test_resolver_accepts_supported_single_and_multi_intents_without_context_or_solver(
    multi: bool,
) -> None:
    intent = OptimizationIntent.from_mapping(_raw(multi=multi))

    result = IntentResolver().resolve(intent, _Capabilities())

    assert result.status == "resolved"
    assert result.resolved_intent is intent
    assert result.issues == ()
    assert set(result.as_dict()) == {"status", "resolved_intent", "issues"}


def test_resolver_prioritizes_ambiguities_over_unsupported_capabilities() -> None:
    raw = _raw()
    raw["ambiguities"] = ["confirm-energy-direction"]
    raw["objectives"][0]["metric_id"] = "unknown-objective"
    raw["preference"]["objective_order"] = ["unknown-objective"]
    intent = OptimizationIntent.from_mapping(raw)

    result = IntentResolver().resolve(intent, _Capabilities())

    assert result.status == "needs_clarification"
    assert result.resolved_intent is None
    assert tuple(item.code for item in result.issues) == (
        "needs-clarification",
        "unsupported-objective",
    )
    assert result.issues[0].json_pointer == "/ambiguities/0"


def test_resolver_returns_structured_unsupported_issues() -> None:
    intent = OptimizationIntent.from_mapping(_raw())
    unsupported = replace(
        intent,
        objectives=(replace(intent.objectives[0], sense="maximize"),),
        decision_variables=("unknown-decision",),
        constraints=("unknown-constraint",),
        preference=PreferenceRequest(
            method="weighted-sum",
            objective_order=("specific_furnace_fuel_energy_mj_per_t",),
        ),
        result_request=ResultRequest(
            output_kind="time-trajectory",
            include_alternatives=False,
            max_candidates=1,
        ),
    )

    result = IntentResolver().resolve(unsupported, _Capabilities())

    assert result.status == "unsupported"
    assert result.resolved_intent is None
    assert tuple(item.code for item in result.issues) == (
        "objective-sense-mismatch",
        "unsupported-decision-variable",
        "unsupported-constraint",
        "unsupported-preference",
        "unsupported-result-request",
    )
    assert tuple(item.json_pointer for item in result.issues) == (
        "/objectives/0/sense",
        "/decision_variables/0",
        "/constraints/0",
        "/preference",
        "/result_request",
    )
    assert result.issues[0].supported_values == ("minimize",)
    serialized_issues = result.as_dict()["issues"]
    assert isinstance(serialized_issues, list)
    assert all(
        isinstance(item, dict)
        and set(item) == {"code", "json_pointer", "message", "supported_values"}
        for item in serialized_issues
    )
