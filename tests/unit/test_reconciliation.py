from __future__ import annotations

import json
import math
import operator
from dataclasses import replace
from typing import cast

import pytest

from petroleum_rto.cdu.calibration.reconciliation import (
    BOUNDARY_STREAM_IDS,
    DATA_ORIGIN,
    INTERNAL_STREAM_IDS,
    FlowDirection,
    FlowEstimateInput,
    NonNegativeConstraintError,
    ReconciliationResult,
    reconcile_boundary_flows,
)

_OBSERVATION_FINGERPRINT = "a" * 64
_VERSIONS = {
    "model_version": "cdu-mini-v0.1.0",
    "parameter_set_version": "engineering-initial-v0.1.0",
    "case_version": "case-20260604-v0.1.0",
    "observation_catalog_version": "cdu-observations-v0.1.0",
    "reconciliation_config_version": "m5-reconciliation-v0.1.0",
}
_DIRECTIONS: dict[str, FlowDirection] = {
    "fresh_feed": "inlet",
    "wash_water": "inlet",
    "gasoline": "outlet",
    "kerosene": "outlet",
    "light_diesel": "outlet",
    "heavy_diesel": "outlet",
    "residue": "outlet",
    "offgas": "outlet",
    "aqueous": "outlet",
    "brine": "outlet",
}
_RAW_VALUES = {
    "fresh_feed": 100.0,
    "wash_water": 4.0,
    "gasoline": 20.0,
    "kerosene": 10.0,
    "light_diesel": 10.0,
    "heavy_diesel": 10.0,
    "residue": 40.0,
    "offgas": 2.0,
    "aqueous": 4.0,
    "brine": 4.0,
}


def _measured_boundary_inputs() -> list[FlowEstimateInput]:
    return [
        FlowEstimateInput(
            stream_id=stream_id,
            direction=_DIRECTIONS[stream_id],
            z_kg_s=_RAW_VALUES[stream_id],
            sigma_kg_s=1.0,
            source_refs=(f"catalog:{stream_id}",),
        )
        for stream_id in BOUNDARY_STREAM_IDS
    ]


def _run(inputs: list[FlowEstimateInput]) -> ReconciliationResult:
    return reconcile_boundary_flows(
        inputs,
        versions=_VERSIONS,
        observation_fingerprint=_OBSERVATION_FINGERPRINT,
    )


def test_known_analytic_equality_solution_and_audit_fields() -> None:
    result = _run(_measured_boundary_inputs())

    assert result.status == "success"
    assert result.pre_reconciliation_residual_kg_s == pytest.approx(4.0)
    assert result.post_reconciliation_residual_kg_s == pytest.approx(0.0, abs=1e-12)
    assert result.objective == pytest.approx(1.6)
    assert result.entries["fresh_feed"].reconciled_kg_s == pytest.approx(99.6)
    assert result.entries["wash_water"].adjustment_kg_s == pytest.approx(-0.4)
    assert result.entries["gasoline"].reconciled_kg_s == pytest.approx(20.4)
    assert result.entries["gasoline"].pull == pytest.approx(0.4)
    assert result.entries["gasoline"].normalized_adjustment == pytest.approx(0.4)
    assert result.entries["gasoline"].raw_kg_s == 20.0
    assert result.entries["gasoline"].prior_kg_s is None
    assert result.reconciled_values_kg_s["heavy_diesel"] == pytest.approx(10.4)
    assert result.synthetic is False
    assert result.data_origin == DATA_ORIGIN
    assert len(result.input_fingerprint) == 64
    assert len(result.result_fingerprint) == 64

    payload = result.as_dict()
    assert payload["synthetic"] is False
    assert payload["data_origin"] == "M5_reconciled_field_observations"
    entries_payload = cast(dict[str, dict[str, object]], payload["entries"])
    assert entries_payload["gasoline"]["normalized_adjustment"] == pytest.approx(0.4)
    assert payload == result.as_dict()
    assert json.dumps(payload, allow_nan=False)


def test_latent_prior_uses_tau_and_preserves_prior_separately_from_raw() -> None:
    inputs = _measured_boundary_inputs()
    offgas_index = BOUNDARY_STREAM_IDS.index("offgas")
    inputs[offgas_index] = FlowEstimateInput(
        stream_id="offgas",
        direction="outlet",
        prior_kg_s=2.0,
        tau_kg_s=2.0,
        source_refs=("model-prior:offgas",),
    )

    result = _run(inputs)
    offgas = result.entries["offgas"]

    assert result.objective == pytest.approx(16.0 / 13.0)
    assert offgas.basis == "latent_prior"
    assert offgas.raw_kg_s is None
    assert offgas.sigma_kg_s is None
    assert offgas.prior_kg_s == 2.0
    assert offgas.tau_kg_s == 2.0
    assert offgas.adjustment_kg_s == pytest.approx(16.0 / 13.0)
    assert offgas.pull == pytest.approx(8.0 / 13.0)


def test_internal_reflux_top_circulation_and_pump_arounds_are_explicitly_excluded() -> None:
    inputs = _measured_boundary_inputs()
    for index, stream_id in enumerate(INTERNAL_STREAM_IDS, start=1):
        inputs.append(
            FlowEstimateInput(
                stream_id=stream_id,
                direction="internal",
                z_kg_s=1000.0 * index,
                sigma_kg_s=0.001,
                source_refs=(f"dcs:{stream_id}",),
                exclusion_reason="internal recycle is outside the net CDU boundary",
            )
        )

    result = _run(inputs)

    assert result.pre_reconciliation_residual_kg_s == pytest.approx(4.0)
    assert set(result.entries) == set(BOUNDARY_STREAM_IDS)
    assert set(result.excluded_internal) == set(INTERNAL_STREAM_IDS)
    excluded_payload = cast(
        dict[str, dict[str, object]],
        result.as_dict()["excluded_internal"],
    )
    assert excluded_payload["reflux"]["excluded_from_balance"] is True
    assert "dcs:top_circulation" in result.source_refs


def test_negative_unconstrained_candidate_fails_explicitly() -> None:
    inputs = _measured_boundary_inputs()
    replacements = {
        "fresh_feed": (0.1, 100.0),
        "wash_water": (100.0, 0.1),
        "gasoline": (0.0, 0.1),
        "kerosene": (0.0, 0.1),
        "light_diesel": (0.0, 0.1),
        "heavy_diesel": (0.0, 0.1),
        "residue": (0.0, 0.1),
        "offgas": (0.0, 0.1),
        "aqueous": (0.0, 0.1),
        "brine": (0.0, 0.1),
    }
    inputs = [
        replace(item, z_kg_s=replacements[item.stream_id][0], sigma_kg_s=replacements[item.stream_id][1])
        for item in inputs
    ]

    with pytest.raises(NonNegativeConstraintError, match="non-negative-flow") as caught:
        _run(inputs)

    assert caught.value.violating_candidates_kg_s["fresh_feed"] < 0.0
    assert caught.value.pre_reconciliation_residual_kg_s == pytest.approx(100.1)
    with pytest.raises(TypeError):
        operator.setitem(
            caught.value.violating_candidates_kg_s,
            "fresh_feed",
            0.0,
        )  # type: ignore[call-overload]


def test_input_order_and_version_mapping_order_do_not_change_fingerprints() -> None:
    inputs = _measured_boundary_inputs()
    first = _run(inputs)
    reversed_versions = dict(reversed(tuple(_VERSIONS.items())))
    second = reconcile_boundary_flows(
        list(reversed(inputs)),
        versions=reversed_versions,
        observation_fingerprint=_OBSERVATION_FINGERPRINT,
    )

    assert first.input_fingerprint == second.input_fingerprint
    assert first.result_fingerprint == second.result_fingerprint
    assert first.as_dict() == second.as_dict()


def test_result_mappings_are_immutable() -> None:
    result = _run(_measured_boundary_inputs())

    with pytest.raises(TypeError):
        operator.delitem(result.entries, "gasoline")  # type: ignore[call-overload]
    with pytest.raises(TypeError):
        operator.setitem(  # type: ignore[call-overload]
            result.reconciled_values_kg_s,
            "gasoline",
            0.0,
        )
    with pytest.raises(TypeError):
        operator.setitem(  # type: ignore[call-overload]
            result.versions,
            "model_version",
            "other",
        )


def test_result_rejects_replace_tampering_of_residuals_and_fingerprints() -> None:
    result = _run(_measured_boundary_inputs())

    with pytest.raises(ValueError, match="post-reconciliation residual"):
        replace(result, post_reconciliation_residual_kg_s=1.0)
    with pytest.raises(ValueError, match="input_fingerprint"):
        replace(result, input_fingerprint="b" * 64)
    with pytest.raises(ValueError, match="result_fingerprint"):
        replace(result, result_fingerprint="b" * 64)


def test_result_rejects_nonclosing_or_nonoptimal_reconciled_entries() -> None:
    result = _run(_measured_boundary_inputs())
    nonclosing_entries = dict(result.entries)
    fresh_feed = result.entries["fresh_feed"]
    nonclosing_entries["fresh_feed"] = replace(
        fresh_feed,
        reconciled_kg_s=99.7,
        adjustment_kg_s=-0.3,
        pull=-0.3,
    )
    nonclosing_objective = sum(entry.pull**2 for entry in nonclosing_entries.values())
    with pytest.raises(ValueError, match="must close the boundary equality"):
        replace(
            result,
            entries=nonclosing_entries,
            post_reconciliation_residual_kg_s=0.1,
            objective=nonclosing_objective,
        )

    nonoptimal_entries = dict(result.entries)
    nonoptimal_entries["fresh_feed"] = replace(
        fresh_feed,
        reconciled_kg_s=99.5,
        adjustment_kg_s=-0.5,
        pull=-0.5,
    )
    gasoline = result.entries["gasoline"]
    nonoptimal_entries["gasoline"] = replace(
        gasoline,
        reconciled_kg_s=20.3,
        adjustment_kg_s=0.3,
        pull=0.3,
    )
    nonoptimal_objective = sum(entry.pull**2 for entry in nonoptimal_entries.values())
    with pytest.raises(ValueError, match="analytic WLS optimum"):
        replace(
            result,
            entries=nonoptimal_entries,
            objective=nonoptimal_objective,
        )


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (
            {"z_kg_s": 1.0},
            "z_kg_s and sigma_kg_s",
        ),
        (
            {"z_kg_s": 1.0, "sigma_kg_s": 0.0},
            "must be positive",
        ),
        (
            {"z_kg_s": -1.0, "sigma_kg_s": 1.0},
            "must be non-negative",
        ),
        (
            {
                "z_kg_s": 1.0,
                "sigma_kg_s": 1.0,
                "prior_kg_s": 1.0,
                "tau_kg_s": 1.0,
            },
            "exactly one",
        ),
        (
            {"z_kg_s": math.nan, "sigma_kg_s": 1.0},
            "must be finite",
        ),
        (
            {"z_kg_s": True, "sigma_kg_s": 1.0},
            "non-boolean",
        ),
    ],
)
def test_flow_input_rejects_invalid_estimates(
    kwargs: dict[str, float | bool],
    match: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        FlowEstimateInput(
            stream_id="gasoline",
            direction="outlet",
            source_refs=("catalog:gasoline",),
            **kwargs,  # type: ignore[arg-type]
        )


def test_flow_input_rejects_wrong_boundaries_and_implicit_internal_flows() -> None:
    with pytest.raises(ValueError, match="must have boundary direction"):
        FlowEstimateInput(
            stream_id="fresh_feed",
            direction="outlet",
            z_kg_s=1.0,
            sigma_kg_s=1.0,
            source_refs=("catalog:fresh_feed",),
        )
    with pytest.raises(ValueError, match="unsupported boundary stream"):
        FlowEstimateInput(
            stream_id="reflux",
            direction="outlet",
            z_kg_s=1.0,
            sigma_kg_s=1.0,
            source_refs=("dcs:reflux",),
        )
    with pytest.raises(ValueError, match="exclusion_reason"):
        FlowEstimateInput(
            stream_id="reflux",
            direction="internal",
            z_kg_s=1.0,
            sigma_kg_s=1.0,
            source_refs=("dcs:reflux",),
        )


def test_reconciliation_rejects_incomplete_duplicate_and_bad_traceability() -> None:
    inputs = _measured_boundary_inputs()

    with pytest.raises(ValueError, match="incomplete"):
        _run(inputs[:-1])
    with pytest.raises(ValueError, match="must be unique"):
        _run([*inputs, inputs[0]])
    with pytest.raises(ValueError, match="observation_fingerprint"):
        reconcile_boundary_flows(
            inputs,
            versions=_VERSIONS,
            observation_fingerprint="not-a-digest",
        )
    bad_versions = dict(_VERSIONS)
    bad_versions.pop("case_version")
    with pytest.raises(ValueError, match="missing=.*case_version"):
        reconcile_boundary_flows(
            inputs,
            versions=bad_versions,
            observation_fingerprint=_OBSERVATION_FINGERPRINT,
        )
