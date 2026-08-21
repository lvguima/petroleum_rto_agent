from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto import LegacyCandidatePlanCompilerV1 as CandidatePlanCompiler
from petroleum_rto.rto import LegacyProblemBuilderV1 as ProblemBuilder
from petroleum_rto.rto import load_rto_v1_bundle
from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.compilation import CompiledPair, assert_compiled_pair
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateProposalV1,
    JsonValue,
    SimulationEvaluationRequestV1,
)
from petroleum_rto.rto.contracts.common import thaw_json


def _proposal(repo_root: Path) -> tuple[object, object, CandidateProposalV1]:
    bundle = load_rto_v1_bundle(repo_root)
    problem = ProblemBuilder().build(bundle)
    proposal = CandidateProposalV1(
        schema_version=RTO_SCHEMA_VERSION,
        proposal_version="candidate-proposal-v1",
        candidate_id="ratio-gold",
        sequence=0,
        origin="fixture",
        problem_ref=problem.ref,
        context_ref=bundle.context.ref,
        decision_values={
            "furnace_temperature_target_k": 626.65,
            "tower_top_pressure_target_pa_a": 151325.0,
        },
        output_kind="steady-setpoint-vector",
        claim_scope=CLAIM_SCOPE,
    )
    return bundle, problem, proposal


def _plain(value: object) -> dict[str, object]:
    thawed = thaw_json(cast(JsonValue, value))
    assert isinstance(thawed, dict)
    return cast(dict[str, object], thawed)


def test_m2_pair_only_changes_two_decision_parameters(repo_root: Path) -> None:
    bundle, problem, proposal = _proposal(repo_root)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M2",
        request_factory=CduM7RequestFactory(),
    )
    baseline = _plain(pair.baseline.provider_request)
    candidate = _plain(pair.candidate.provider_request)
    assert baseline["parameters"] == {
        "feed.mass_flow_t_h": 407.3,
        "operating.furnace_outlet_temperature_c": 355.2,
        "operating.tower_top_pressure_mpa_g": 0.051,
    }
    assert candidate["parameters"] == {
        "feed.mass_flow_t_h": 407.3,
        "operating.furnace_outlet_temperature_c": 353.5,
        "operating.tower_top_pressure_mpa_g": 0.05,
    }
    assert CandidateProposalV1.from_mapping(proposal.as_dict()) == proposal
    assert SimulationEvaluationRequestV1.from_mapping(pair.candidate.as_dict()) == pair.candidate


def test_m4_pair_only_changes_events_and_uses_absolute_ratios(repo_root: Path) -> None:
    bundle, problem, proposal = _proposal(repo_root)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M4",
        request_factory=CduM7RequestFactory(),
    )
    baseline = _plain(pair.baseline.provider_request)
    candidate = _plain(pair.candidate.provider_request)
    assert baseline["parameters"] == candidate["parameters"]
    assert baseline["initial_state"] == candidate["initial_state"]
    baseline_scenario = cast(dict[str, object], baseline["scenario"])
    candidate_scenario = cast(dict[str, object], candidate["scenario"])
    assert baseline_scenario["events"] == []
    events = cast(list[dict[str, object]], candidate_scenario["events"])
    assert [event["time_s"] for event in events] == [600.0, 600.0]
    assert [event["target"] for event in events] == [
        "furnace_temperature.setpoint_ratio",
        "top_pressure.setpoint_ratio",
    ]
    assert cast(float, events[0]["value"]) == pytest.approx(0.9972945015)
    assert cast(float, events[1]["value"]) == pytest.approx(0.9934350894)
    assert all(event["value_basis"] == "setpoint_ratio" for event in events)
    assert all(event["duration_s"] is None for event in events)


def test_compiler_rejects_out_of_domain_candidate(repo_root: Path) -> None:
    bundle, problem, proposal = _proposal(repo_root)
    bad = replace(
        proposal,
        decision_values={
            **proposal.decision_values,
            "furnace_temperature_target_k": 700.0,
        },
    )
    with pytest.raises(ValueError, match="outside"):
        CandidatePlanCompiler().compile_pair(
            problem,
            bundle.context,
            bad,
            stage="M2",
            request_factory=CduM7RequestFactory(),
        )


def test_pair_checker_rejects_nonwhitelisted_m4_drift(repo_root: Path) -> None:
    bundle, problem, proposal = _proposal(repo_root)
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage="M4",
        request_factory=CduM7RequestFactory(),
    )
    provider = _plain(pair.candidate.provider_request)
    provider["random_seed"] = 1
    corrupted = CompiledPair(
        baseline=pair.baseline,
        candidate=replace(pair.candidate, provider_request=provider),
    )
    with pytest.raises(ValueError, match="outside"):
        assert_compiled_pair(corrupted)
