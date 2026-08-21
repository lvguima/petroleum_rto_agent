from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto.contracts.candidate import CandidateEvaluation, CandidateProposal
from petroleum_rto.rto.contracts.finalization import (
    FinalizationResult,
    PublishabilityAssessment,
    StaticPreferenceSelection,
)
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.contracts.solver_result import SolverResult
from petroleum_rto.rto.selection import UnifiedFinalSelector
from tests.rto.unit.test_unified_finalization import (
    _basis,
    _dynamic_evaluation,
    _mapping,
    _proposal,
    _solver_result,
    _static_evaluation,
)


def test_candidate_proposal_and_evaluation_round_trip_with_strict_identity(
    repo_root: Path,
) -> None:
    _, problem = _basis(repo_root, multi=False)
    proposal = _proposal(problem, 0)
    evaluation = _static_evaluation(problem, proposal, (187.0,))

    assert CandidateProposal.from_mapping(proposal.as_dict()) == proposal
    assert CandidateEvaluation.from_mapping(evaluation.as_dict()) == evaluation

    relocated = evaluation.as_dict()
    evidence = cast(list[dict[str, object]], relocated["evidence_refs"])
    evidence[0]["run_ref"] = "/relocated/baseline"
    restored = CandidateEvaluation.from_mapping(relocated)

    assert restored.evidence_refs[0].run_ref == "/relocated/baseline"
    assert restored.fingerprint == evaluation.fingerprint


def test_candidate_readers_reject_unknown_fields_tampered_identity_and_derived_values(
    repo_root: Path,
) -> None:
    _, problem = _basis(repo_root, multi=False)
    proposal = _proposal(problem, 0)
    evaluation = _static_evaluation(problem, proposal, (187.0,))

    proposal_unknown = proposal.as_dict()
    proposal_unknown["unknown"] = True
    with pytest.raises(ValueError, match="fields differ"):
        CandidateProposal.from_mapping(proposal_unknown)

    proposal_identity = proposal.as_dict()
    proposal_identity["proposal_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="proposal_fingerprint"):
        CandidateProposal.from_mapping(proposal_identity)

    evaluation_identity = evaluation.as_dict()
    evaluation_identity["evaluation_id"] = "evaluation-tampered"
    with pytest.raises(ValueError, match="evaluation_id"):
        CandidateEvaluation.from_mapping(evaluation_identity)

    evaluation_margin = evaluation.as_dict()
    evaluation_margin["minimum_normalized_margin"] = 0.5
    with pytest.raises(ValueError, match="differs from constraint outcomes"):
        CandidateEvaluation.from_mapping(evaluation_margin)

    evaluation_outcome = evaluation.as_dict()
    outcomes = cast(list[dict[str, object]], evaluation_outcome["objective_outcomes"])
    outcomes[0]["directional_absolute_improvement"] = 999.0
    with pytest.raises(ValueError, match="directional improvement"):
        CandidateEvaluation.from_mapping(evaluation_outcome)


def test_solver_result_round_trip_embeds_artifacts_and_closes_all_refs(repo_root: Path) -> None:
    _, problem = _basis(repo_root, multi=False)
    proposals = tuple(_proposal(problem, index) for index in range(2))
    evaluations = tuple(
        _static_evaluation(problem, proposal, (value,))
        for proposal, value in zip(proposals, (187.0, 188.0), strict=True)
    )
    result = _solver_result(problem, proposals, evaluations)

    payload = result.as_dict()
    restored = SolverResult.from_mapping(payload)

    assert restored == result
    assert restored.fingerprint == result.fingerprint
    assert len(cast(list[object], payload["proposals"])) == 2
    assert len(cast(list[object], payload["evaluations"])) == 2
    json.dumps(payload, allow_nan=False)

    mismatched_refs = result.as_dict()
    mismatched_refs["proposal_refs"] = list(reversed(cast(list[object], payload["proposal_refs"])))
    with pytest.raises(ValueError, match="proposal_refs differ"):
        SolverResult.from_mapping(mismatched_refs)

    tampered_identity = result.as_dict()
    tampered_identity["solver_result_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="solver_result_fingerprint"):
        SolverResult.from_mapping(tampered_identity)

    unknown = result.as_dict()
    unknown["unknown"] = True
    with pytest.raises(ValueError, match="fields differ"):
        SolverResult.from_mapping(unknown)


def test_solver_result_rejects_foreign_problem_and_nonfeasible_solution_group(
    repo_root: Path,
) -> None:
    _, problem = _basis(repo_root, multi=False)
    proposal = _proposal(problem, 0)
    evaluation = _static_evaluation(problem, proposal, (187.0,))
    result = _solver_result(problem, (proposal,), (evaluation,))
    foreign_problem = replace(problem, problem_version="foreign-problem")
    foreign_proposal = replace(proposal, problem_ref=foreign_problem.ref)
    foreign_evaluation = replace(
        evaluation,
        problem_ref=foreign_problem.ref,
        proposal_ref=foreign_proposal.ref,
    )

    with pytest.raises(ValueError, match="reference the solver problem"):
        replace(
            result,
            proposals=(foreign_proposal,),
            evaluations=(foreign_evaluation,),
            solution_groups=(),
            status="evaluation_error",
        )

    process_failure = replace(
        evaluation,
        status="process_infeasible",
        reason_codes=("synthetic-process-failure",),
    )
    with pytest.raises(ValueError, match="only feasible evaluations"):
        _solver_result(
            problem,
            (proposal,),
            (process_failure,),
            groups=((process_failure.ref,),),
        )

    dynamic = _dynamic_evaluation(problem, proposal.ref, "feasible")
    with pytest.raises(ValueError, match="only M2 evaluations"):
        replace(result, evaluations=(dynamic,))

    with pytest.raises(ValueError, match="requires an evaluation failure"):
        replace(result, status="evaluation_error", solution_groups=())


def test_finalization_subcontracts_keep_strict_readers_and_cross_artifact_identity(
    repo_root: Path,
) -> None:
    bundle, problem = _basis(repo_root, multi=False)
    proposal = _proposal(problem, 0)
    static = (_static_evaluation(problem, proposal, (187.0,)),)
    solver = _solver_result(problem, (proposal,), static)
    selector = UnifiedFinalSelector()
    selection = selector.rank_static(problem, solver, _mapping(static))
    dynamic = {proposal.ref: _dynamic_evaluation(problem, proposal.ref, "feasible")}
    artifacts = selector.select(problem, solver, _mapping(static), dynamic, bundle)
    assert artifacts.publishability is not None

    assert StaticPreferenceSelection.from_mapping(selection.as_dict()) == selection
    assert (
        PublishabilityAssessment.from_mapping(artifacts.publishability.as_dict())
        == artifacts.publishability
    )
    assert FinalizationResult.from_mapping(artifacts.result.as_dict()) == artifacts.result

    for contract, reader, id_field in (
        (selection, StaticPreferenceSelection.from_mapping, "selection_id"),
        (
            artifacts.publishability,
            PublishabilityAssessment.from_mapping,
            "assessment_id",
        ),
        (artifacts.result, FinalizationResult.from_mapping, "result_id"),
    ):
        raw = contract.as_dict()
        raw[id_field] = "tampered-id"
        with pytest.raises(ValueError, match=id_field):
            reader(raw)

    foreign_static_ref = ContractRef("foreign-static-selection", "f" * 64)
    tampered_result = replace(artifacts.result, static_selection_ref=foreign_static_ref)
    with pytest.raises(ValueError, match="another static selection"):
        type(artifacts)(selection, artifacts.publishability, tampered_result)

    foreign_dynamic_ref = ContractRef("foreign-dynamic-evaluation", "e" * 64)
    with pytest.raises(ValueError, match="must align with the selected proposal"):
        replace(
            artifacts.result,
            selected_dynamic_evaluation_ref=foreign_dynamic_ref,
        )
