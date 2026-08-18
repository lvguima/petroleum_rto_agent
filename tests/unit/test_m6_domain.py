from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from petroleum_rto.cdu.validation.domain import (
    ApplicabilityAssessment,
    DomainConfidence,
    DomainDimension,
    DomainInputLayer,
    DomainRepresentation,
    assess_applicability,
)


def _dimension(
    dimension_id: str = "feed_load_ratio",
    representation: DomainRepresentation = "direct",
    input_layer: DomainInputLayer = "M2_steady",
    confidence: DomainConfidence = "low_engineering",
    assumptions: tuple[str, ...] = ("synthetic_engineering_envelope",),
) -> DomainDimension:
    return DomainDimension(
        dimension_id=dimension_id,
        unit="ratio",
        representation=representation,
        reference_value=1.0,
        normal_min=0.95,
        normal_max=1.05,
        limited_min=0.9,
        limited_max=1.1,
        source="M6_engineering_validation_envelope",
        input_layer=input_layer,
        confidence=confidence,
        assumptions=assumptions,
    )


def _domain() -> tuple[DomainDimension, ...]:
    return (
        _dimension(),
        _dimension("available_furnace_duty_ratio", "proxy"),
        _dimension("stripping_steam_ratio", "unsupported"),
    )


def test_domain_dimension_is_frozen_and_requires_strictly_nested_bounds() -> None:
    dimension = _dimension()
    with pytest.raises(FrozenInstanceError):
        dimension.reference_value = 2.0  # type: ignore[misc]

    with pytest.raises(ValueError, match="limited_min < normal_min"):
        DomainDimension(
            dimension_id="bad_bounds",
            unit="ratio",
            representation="direct",
            reference_value=1.0,
            normal_min=0.9,
            normal_max=1.1,
            limited_min=0.9,
            limited_max=1.2,
            source="test",
        )
    with pytest.raises(ValueError, match="representation"):
        _dimension(representation=cast(DomainRepresentation, "invented"))
    with pytest.raises(ValueError, match="finite"):
        DomainDimension(
            dimension_id="bad_reference",
            unit="ratio",
            representation="direct",
            reference_value=math.inf,
            normal_min=0.9,
            normal_max=1.1,
            limited_min=0.8,
            limited_max=1.2,
            source="test",
        )


def test_domain_dimension_provenance_is_strict_serialized_and_fingerprinted() -> None:
    dimension = _dimension(
        representation="proxy",
        input_layer="M3_open_loop",
        confidence="low_proxy",
        assumptions=(
            "command_proxy_not_field_capacity",
            "synthetic_engineering_envelope",
        ),
    )
    assert dimension.as_dict() == {
        "dimension_id": "feed_load_ratio",
        "unit": "ratio",
        "representation": "proxy",
        "input_layer": "M3_open_loop",
        "confidence": "low_proxy",
        "assumptions": [
            "command_proxy_not_field_capacity",
            "synthetic_engineering_envelope",
        ],
        "reference_value": 1.0,
        "normal_min": 0.95,
        "normal_max": 1.05,
        "limited_min": 0.9,
        "limited_max": 1.1,
        "source": "M6_engineering_validation_envelope",
    }
    baseline = assess_applicability((dimension,), {dimension.dimension_id: 1.0})
    changed = assess_applicability(
        (replace(dimension, assumptions=("different_assumption",)),),
        {dimension.dimension_id: 1.0},
    )
    assert baseline.input_fingerprint != changed.input_fingerprint
    with pytest.raises(ValueError, match="input_layer"):
        _dimension(input_layer=cast(DomainInputLayer, "field_DCS"))
    with pytest.raises(ValueError, match="confidence"):
        _dimension(confidence=cast(DomainConfidence, "validated"))
    with pytest.raises(ValueError, match="cannot be empty"):
        _dimension(assumptions=())
    with pytest.raises(ValueError, match="duplicates"):
        _dimension(assumptions=("same", "same"))
    with pytest.raises(TypeError, match="sequence"):
        _dimension(assumptions=cast(tuple[str, ...], "not_a_sequence"))


def test_partial_override_merges_references_without_activating_unrequested_dimensions() -> None:
    result = assess_applicability(_domain(), {"feed_load_ratio": 1.04})

    assert result.status == "passed"
    assert result.solver_allowed
    assert result.resolved_inputs == {
        "available_furnace_duty_ratio": 1.0,
        "feed_load_ratio": 1.04,
        "stripping_steam_ratio": 1.0,
    }
    by_id = {item.dimension_id: item for item in result.dimensions}
    assert by_id["feed_load_ratio"].requested
    assert not by_id["available_furnace_duty_ratio"].requested
    assert by_id["available_furnace_duty_ratio"].status == "passed"
    assert not by_id["stripping_steam_ratio"].requested
    assert by_id["stripping_steam_ratio"].status == "passed"


def test_explicit_proxy_is_limited_and_explicit_unsupported_is_rejected_pre_solver() -> None:
    proxy = assess_applicability(
        _domain(),
        {"available_furnace_duty_ratio": 0.98},
    )
    assert proxy.status == "limited"
    assert proxy.solver_allowed
    assert "available_furnace_duty_ratio:proxy_representation" in proxy.reasons

    calls = 0

    def evaluate_only_if_allowed(result: ApplicabilityAssessment) -> None:
        nonlocal calls
        if result.solver_allowed:
            calls += 1

    normal = assess_applicability(_domain(), {"feed_load_ratio": 1.02})
    evaluate_only_if_allowed(normal)
    assert calls == 1

    unsupported = assess_applicability(
        _domain(),
        {"stripping_steam_ratio": 1.02},
    )
    evaluate_only_if_allowed(unsupported)
    assert unsupported.status == "rejected"
    assert not unsupported.solver_allowed
    assert "stripping_steam_ratio:unsupported_model_input" in unsupported.reasons
    assert calls == 1


def test_piecewise_distances_and_boundary_statuses_are_exact() -> None:
    dimension = _dimension()

    normal_edge = assess_applicability((dimension,), {dimension.dimension_id: 1.05})
    normal_item = normal_edge.dimensions[0]
    assert normal_edge.status == "passed"
    assert normal_item.reference_distance == pytest.approx(1.0)
    assert normal_item.excess_distance == 0.0

    limited_mid = assess_applicability((dimension,), {dimension.dimension_id: 1.075})
    limited_item = limited_mid.dimensions[0]
    assert limited_mid.status == "limited"
    assert limited_item.reference_distance == pytest.approx(1.5)
    assert limited_item.excess_distance == pytest.approx(0.5)

    limited_edge = assess_applicability((dimension,), {dimension.dimension_id: 0.9})
    assert limited_edge.status == "limited"
    assert limited_edge.dimensions[0].excess_distance == pytest.approx(1.0)

    rejected = assess_applicability((dimension,), {dimension.dimension_id: 1.1001})
    assert rejected.status == "rejected"
    assert rejected.dimensions[0].excess_distance is not None
    assert rejected.dimensions[0].excess_distance > 1.0


def test_abnormal_verification_forces_limited_but_does_not_override_rejection() -> None:
    limited = assess_applicability(
        (_dimension(),),
        {"feed_load_ratio": 1.0},
        abnormal_verification=True,
    )
    assert limited.status == "limited"
    assert limited.solver_allowed
    assert limited.reasons == ("abnormal_verification_mode",)

    rejected = assess_applicability(
        (_dimension(),),
        {"feed_load_ratio": 2.0},
        abnormal_verification=True,
    )
    assert rejected.status == "rejected"
    assert "abnormal_verification_mode" not in rejected.reasons


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf, True, "1.0"])
def test_nonfinite_or_nonnumeric_input_is_rejected_with_json_safe_evidence(
    bad_value: object,
) -> None:
    result = assess_applicability((_dimension(),), {"feed_load_ratio": bad_value})

    assert result.status == "rejected"
    assert not result.solver_allowed
    assert result.dimensions[0].value is None
    assert result.dimensions[0].invalid_value is not None
    json.dumps(result.as_dict(), allow_nan=False)


def test_unknown_inputs_reject_and_do_not_change_known_reference_resolution() -> None:
    result = assess_applicability((_dimension(),), {"unknown_factor": 2.0})

    assert result.status == "rejected"
    assert result.unknown_inputs == ("unknown_factor",)
    assert result.resolved_inputs == {"feed_load_ratio": 1.0}
    assert result.reasons == ("unknown_input:unknown_factor",)


def test_assessment_is_order_independent_repeatable_and_self_describing() -> None:
    dimensions = _domain()
    first = assess_applicability(dimensions, {"feed_load_ratio": 1.075})
    second = assess_applicability(tuple(reversed(dimensions)), {"feed_load_ratio": 1.075})

    assert first == second
    assert first.as_dict() == second.as_dict()
    assert first.input_fingerprint == second.input_fingerprint
    assert len(first.input_fingerprint) == 64
    assert first.maximum_reference_distance == pytest.approx(1.5)
    assert first.maximum_excess_distance == pytest.approx(0.5)
