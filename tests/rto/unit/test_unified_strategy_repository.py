from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto._file_lock import exclusive_file_lock
from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.candidate import (
    CandidateEvaluation,
    CandidateProposal,
    ConstraintOutcome,
)
from petroleum_rto.rto.contracts.context import OperatingContext
from petroleum_rto.rto.contracts.problem import ConstraintRule, OptimizationProblem
from petroleum_rto.rto.contracts.reference import ContractRef
from petroleum_rto.rto.selection import FinalizationArtifacts, FinalSelector
from petroleum_rto.rto.strategies import (
    StrategyBuilder,
    StrategyEntry,
    StrategyQuery,
    StrategyRepository,
    anchor_from_verified_candidate,
)
from tests.rto.unit.test_unified_finalization import (
    _basis,
    _dynamic_evaluation,
    _mapping,
    _proposal,
    _solver_result,
    _static_evaluation,
)


def _constraint_outcome(rule: ConstraintRule, raw_value: float) -> ConstraintOutcome:
    if rule.operator == "le":
        passed = raw_value <= rule.limit
        margin = (rule.limit - raw_value) / rule.normalization_scale
    elif rule.operator == "ge":
        passed = raw_value >= rule.limit
        margin = (raw_value - rule.limit) / rule.normalization_scale
    else:
        passed = math.isclose(raw_value, rule.limit, rel_tol=0.0, abs_tol=1e-12)
        margin = 0.0 if passed else -abs(raw_value - rule.limit) / rule.normalization_scale
    return ConstraintOutcome(
        constraint_id=rule.constraint_id,
        metric_id=rule.metric_id,
        raw_value=raw_value,
        limit=rule.limit,
        normalized_margin=margin,
        passed=passed,
    )


def _action_l1(problem: OptimizationProblem, proposal: CandidateProposal) -> float:
    return sum(
        abs(proposal.decision_values[item.variable_id] - item.nominal_value)
        / (item.upper_bound - item.lower_bound)
        for item in problem.decision_domains
    )


def _valid_static(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
    values: tuple[float, ...],
    *,
    publish_improvement: float = 0.006,
) -> CandidateEvaluation:
    base = _static_evaluation(
        problem,
        proposal,
        values,
        publish_improvement=publish_improvement,
    )
    outcomes = tuple(
        replace(
            outcome,
            relative_directional_improvement=(
                None
                if abs(outcome.baseline_value) <= 1e-12
                else outcome.directional_absolute_improvement / abs(outcome.baseline_value)
            ),
            normalized_directional_improvement=(
                outcome.directional_absolute_improvement / spec.normalization_scale
            ),
        )
        for spec, outcome in zip(problem.objectives, base.objective_outcomes, strict=True)
    )
    metrics = {item.metric_id: item.candidate_value for item in outcomes}
    metrics["specific_furnace_fuel_improvement_fraction"] = publish_improvement
    constraints: list[ConstraintOutcome] = []
    for rule in problem.hard_constraints:
        if rule.evaluation_stage != "M2":
            continue
        if rule.metric_id in metrics:
            raw = metrics[rule.metric_id]
        elif rule.operator == "eq":
            raw = rule.limit
        elif rule.operator == "le":
            raw = rule.limit - rule.normalization_scale
        else:
            raw = rule.limit + rule.normalization_scale
        metrics[rule.metric_id] = raw
        constraints.append(_constraint_outcome(rule, raw))
    return replace(
        base,
        objective_outcomes=outcomes,
        metrics=metrics,
        constraints=tuple(constraints),
        minimum_normalized_margin=min(item.normalized_margin for item in constraints),
        normalized_action_l1=_action_l1(problem, proposal),
    )


def _valid_dynamic(
    problem: OptimizationProblem,
    proposal: CandidateProposal,
) -> CandidateEvaluation:
    base = _dynamic_evaluation(problem, proposal.ref, "feasible")
    metrics: dict[str, float] = {}
    constraints: list[ConstraintOutcome] = []
    for rule in problem.hard_constraints:
        if rule.evaluation_stage != "M4":
            continue
        if rule.operator == "eq":
            raw = rule.limit
        elif rule.operator == "le":
            raw = rule.limit - rule.normalization_scale
        else:
            raw = rule.limit + rule.normalization_scale
        metrics[rule.metric_id] = raw
        constraints.append(_constraint_outcome(rule, raw))
    return replace(
        base,
        metrics=metrics,
        constraints=tuple(constraints),
        minimum_normalized_margin=min(item.normalized_margin for item in constraints),
        normalized_action_l1=_action_l1(problem, proposal),
    )


def _objective_values(problem: OptimizationProblem) -> tuple[float, ...]:
    values = {
        "quality_proxy_max_abs_relative_change": 0.001,
        "valuable_distillate_yield": 0.5,
        "specific_furnace_fuel_energy_mj_per_t": 100.0,
    }
    return tuple(values[item.metric_id] for item in problem.objectives)


def _artifacts(
    repo_root: Path,
    *,
    multi: bool,
    publish_improvement: float = 0.006,
) -> tuple[
    OperatingContext,
    OptimizationProblem,
    CandidateProposal,
    CandidateEvaluation,
    CandidateEvaluation,
    FinalizationArtifacts,
]:
    bundle, problem = _basis(repo_root, multi=multi)
    context = load_operating_context(repo_root / "configs/rto/contexts/case_20260604.json")
    proposal = _proposal(problem, 0)
    static = _valid_static(
        problem,
        proposal,
        _objective_values(problem),
        publish_improvement=publish_improvement,
    )
    dynamic = _valid_dynamic(problem, proposal)
    solver = _solver_result(problem, (proposal,), (static,))
    artifacts = FinalSelector().select(
        problem,
        solver,
        _mapping((static,)),
        {proposal.ref: dynamic},
        bundle,
    )
    return context, problem, proposal, static, dynamic, artifacts


def _entry(repo_root: Path, *, multi: bool = False) -> StrategyEntry:
    context, problem, proposal, static, dynamic, artifacts = _artifacts(
        repo_root,
        multi=multi,
    )
    return StrategyBuilder().build(
        problem,
        context,
        proposal,
        static,
        dynamic,
        artifacts,
    )


@pytest.mark.parametrize(("multi", "objective_count"), [(False, 1), (True, 3)])
def test_unified_entry_is_vectorized_offline_and_round_trips(
    repo_root: Path,
    multi: bool,
    objective_count: int,
) -> None:
    entry = _entry(repo_root, multi=multi)

    assert len(entry.objective_order) == objective_count
    assert len(entry.anchors[0].objective_summaries) == objective_count
    assert set(entry.action_values) == {
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    }
    assert entry.execution_scope == "offline_simulation_only"
    assert entry.control_authority == "none"
    assert not entry.field_validated
    assert not entry.dcs_write_capability
    assert StrategyEntry.from_mapping(entry.as_dict()) == entry
    serialized = json.dumps(entry.as_dict(), allow_nan=False)
    assert "timeseries" not in serialized
    assert '"static_evaluation"' not in serialized
    assert '"dynamic_evaluation"' not in serialized

    unknown = entry.as_dict()
    unknown["unknown"] = True
    with pytest.raises(ValueError, match="fields differ"):
        StrategyEntry.from_mapping(unknown)

    bad_identity = entry.as_dict()
    bad_identity["strategy_ref"] = ContractRef("wrong", "f" * 64).as_dict()
    with pytest.raises(ValueError, match="strategy_ref"):
        StrategyEntry.from_mapping(bad_identity)


def test_builder_rejects_nonpublishable_or_incomplete_evidence(repo_root: Path) -> None:
    context, problem, proposal, static, dynamic, artifacts = _artifacts(
        repo_root,
        multi=False,
        publish_improvement=0.001,
    )
    assert artifacts.result.status == "feasible_not_publishable"
    with pytest.raises(ValueError, match="publishable"):
        StrategyBuilder().build(
            problem,
            context,
            proposal,
            static,
            dynamic,
            artifacts,
        )

    context, problem, proposal, static, dynamic, artifacts = _artifacts(
        repo_root,
        multi=False,
    )
    incomplete = replace(
        static,
        constraints=static.constraints[:1],
        minimum_normalized_margin=static.constraints[0].normalized_margin,
    )
    with pytest.raises(ValueError, match="every stage hard constraint"):
        anchor_from_verified_candidate(
            problem,
            context,
            proposal,
            incomplete,
            dynamic,
            finalization_result_ref=artifacts.result.ref,
        )

    mismatched_applicability = {
        "fresh_feed_load_kg_s": cast(float, context.facts["fresh_feed_load_kg_s"]) + 1.0
    }
    with pytest.raises(ValueError, match="operating context facts"):
        anchor_from_verified_candidate(
            problem,
            context,
            proposal,
            static,
            dynamic,
            finalization_result_ref=artifacts.result.ref,
            applicability_values=mismatched_applicability,
        )

    assessment = artifacts.publishability
    assert assessment is not None
    forged_outcome = replace(assessment.outcomes[0], limit=assessment.outcomes[0].limit - 1.0)
    forged_assessment = replace(assessment, outcomes=(forged_outcome,))
    forged_result = replace(
        artifacts.result,
        publishability_assessment_ref=forged_assessment.ref,
    )
    forged_artifacts = FinalizationArtifacts(
        artifacts.static_selection,
        forged_assessment,
        forged_result,
    )
    with pytest.raises(ValueError, match="publishability assessment differs"):
        StrategyBuilder().build(
            problem,
            context,
            proposal,
            static,
            dynamic,
            forged_artifacts,
        )

    foreign_solver = ContractRef("foreign-solver-result", "e" * 64)
    foreign_result = replace(artifacts.result, solver_result_ref=foreign_solver)
    foreign_artifacts = FinalizationArtifacts(
        artifacts.static_selection,
        artifacts.publishability,
        foreign_result,
    )
    with pytest.raises(ValueError, match="another problem or context"):
        StrategyBuilder().build(
            problem,
            context,
            proposal,
            static,
            dynamic,
            foreign_artifacts,
        )


def _sampled_entry(repo_root: Path) -> StrategyEntry:
    context, problem, proposal, static, dynamic, artifacts = _artifacts(
        repo_root,
        multi=False,
    )
    central_feed = cast(float, context.facts["fresh_feed_load_kg_s"])
    anchor_facts = dict(context.facts)
    anchor_facts["fresh_feed_load_kg_s"] = central_feed * 0.95
    anchor_context = replace(
        context,
        context_id="case-20260604-feed-095",
        facts=anchor_facts,
    )
    anchor_problem = replace(problem, context_ref=anchor_context.ref)
    anchor_proposal = replace(
        proposal,
        candidate_id="candidate-anchor-095",
        problem_ref=anchor_problem.ref,
        context_ref=anchor_context.ref,
    )
    anchor_static = _valid_static(
        anchor_problem,
        anchor_proposal,
        _objective_values(anchor_problem),
    )
    anchor_dynamic = _valid_dynamic(anchor_problem, anchor_proposal)
    sampled = anchor_from_verified_candidate(
        anchor_problem,
        anchor_context,
        anchor_proposal,
        anchor_static,
        anchor_dynamic,
        finalization_result_ref=artifacts.result.ref,
        applicability_values={
            "feed_ratio": 0.95,
            "fresh_feed_load_kg_s": central_feed * 0.95,
        },
    )
    return StrategyBuilder().build(
        problem,
        context,
        proposal,
        static,
        dynamic,
        artifacts,
        additional_anchors=(sampled,),
        applicability_values={
            "feed_ratio": 1.0,
            "fresh_feed_load_kg_s": central_feed,
        },
    )


def test_sampled_helper_uses_center_finalization_and_query_never_interpolates(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    entry = _sampled_entry(repo_root)
    assert entry.coverage_kind == "sampled_anchors"
    assert all(
        item.finalization_result_ref == entry.finalization_result_ref for item in entry.anchors
    )
    repository = StrategyRepository(tmp_path / "library")
    repository.create_draft(entry, actor="builder", occurred_at="2026-08-20T10:00:00+08:00")
    query = StrategyQuery(
        case_ref=entry.case_ref,
        operating_mode=entry.operating_mode,
        applicability_values=dict(entry.anchors[0].applicability_values),
        measurement_tolerances={"feed_ratio": 1e-6, "fresh_feed_load_kg_s": 1e-6},
        required_dependency_refs=(entry.system_policy_ref,),
    )
    assert StrategyQuery.from_mapping(query.as_dict()) == query
    assert repository.query(query) == ()
    repository.approve(entry.strategy_id, 1, actor="reviewer")
    repository.publish(entry.strategy_id, 1, actor="publisher")
    assert len(repository.query(query)) == 1

    low, high = sorted(item.applicability_values["fresh_feed_load_kg_s"] for item in entry.anchors)
    between = replace(
        query,
        applicability_values={
            "feed_ratio": 0.975,
            "fresh_feed_load_kg_s": (low + high) / 2.0,
        },
    )
    assert repository.query(between) == ()
    wrong_dependency = replace(
        query,
        required_dependency_refs=(ContractRef("unknown-dependency", "f" * 64),),
    )
    assert repository.query(wrong_dependency) == ()


def test_repository_full_append_only_lifecycle_and_revision_closure(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    entry = _entry(repo_root)
    repository = StrategyRepository(tmp_path / "library")
    draft = repository.create_draft(
        entry,
        actor="builder",
        occurred_at="2026-08-20T10:00:00+08:00",
    )
    entry_path = repository.entries_root / entry.strategy_id / "r1" / "entry.json"
    events_path = entry_path.with_name("events.jsonl")
    entry_bytes = entry_path.read_bytes()
    created_events = events_path.read_bytes()
    assert draft.current_state == "draft"

    with pytest.raises(ValueError, match="pending_revalidation"):
        repository.create_draft(
            replace(entry, revision=2, supersedes=entry.ref),
            actor="builder",
        )
    assert not (repository.entries_root / entry.strategy_id / "r2").exists()

    repository.approve(
        entry.strategy_id,
        1,
        actor="reviewer",
        occurred_at="2026-08-20T10:01:00+08:00",
    )
    release = repository.publish(
        entry.strategy_id,
        1,
        actor="publisher",
        occurred_at="2026-08-20T10:02:00+08:00",
    )
    release_path = repository.releases_root / f"{release.release_id}.json"
    release_bytes = release_path.read_bytes()
    repository.request_revalidation(
        entry.strategy_id,
        1,
        actor="version-monitor",
        occurred_at="2026-08-20T10:03:00+08:00",
    )
    replacement = replace(entry, revision=2, supersedes=entry.ref)
    repository.create_draft(
        replacement,
        actor="builder",
        occurred_at="2026-08-20T10:04:00+08:00",
    )
    repository.approve(
        replacement.strategy_id,
        2,
        actor="reviewer",
        occurred_at="2026-08-20T10:05:00+08:00",
    )
    with pytest.raises(ValueError, match="published"):
        repository.supersede(
            entry.strategy_id,
            1,
            replacement.strategy_id,
            2,
            actor="publisher",
        )
    repository.publish(
        replacement.strategy_id,
        2,
        actor="publisher",
        occurred_at="2026-08-20T10:06:00+08:00",
    )
    old = repository.supersede(
        entry.strategy_id,
        1,
        replacement.strategy_id,
        2,
        actor="publisher",
        occurred_at="2026-08-20T10:07:00+08:00",
    )
    retired = repository.retire(
        replacement.strategy_id,
        2,
        actor="reviewer",
        occurred_at="2026-08-20T10:08:00+08:00",
    )

    assert old.current_state == "superseded"
    assert retired.current_state == "retired"
    assert old.events[-1].related_strategy_ref == replacement.ref
    assert repository.read_ref(replacement.ref).entry == replacement
    assert repository.read_release(release.release_id) == release
    assert entry_path.read_bytes() == entry_bytes
    assert release_path.read_bytes() == release_bytes
    assert events_path.read_bytes().startswith(created_events)


def test_repository_rejects_tampering_early_events_or_orphan_revisions(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    entry = _entry(repo_root)
    empty = StrategyRepository(tmp_path / "empty")
    with pytest.raises(ValueError, match="cannot read"):
        empty.create_draft(
            replace(entry, revision=2, supersedes=entry.ref),
            actor="builder",
        )
    assert not (empty.entries_root / entry.strategy_id / "r2").exists()

    repository = StrategyRepository(tmp_path / "library")
    repository.create_draft(
        entry,
        actor="builder",
        occurred_at="2026-08-20T10:00:00+08:00",
    )
    repository.approve(
        entry.strategy_id,
        1,
        actor="reviewer",
        occurred_at="2026-08-20T10:01:00+08:00",
    )
    entry_path = repository.entries_root / entry.strategy_id / "r1" / "entry.json"
    events_path = entry_path.with_name("events.jsonl")
    event_bytes = events_path.read_bytes()
    with pytest.raises(ValueError, match="non-decreasing"):
        repository.publish(
            entry.strategy_id,
            1,
            actor="publisher",
            occurred_at="2026-08-20T09:59:00+08:00",
        )
    assert events_path.read_bytes() == event_bytes
    assert repository.read(entry.strategy_id, 1).current_state == "approved"

    original = entry_path.read_text(encoding="utf-8")
    raw = json.loads(original)
    raw["unknown"] = True
    entry_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="fields differ"):
        repository.read(entry.strategy_id, 1)
    entry_path.write_text(original, encoding="utf-8")

    duplicate = original.replace("{", '{"schema_version":"duplicate",', 1)
    entry_path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ValueError, match="cannot parse strict"):
        repository.read(entry.strategy_id, 1)
    entry_path.write_text(original, encoding="utf-8")

    events = events_path.read_text(encoding="utf-8")
    events_path.write_text(
        events.replace('"actor":"builder"', '"actor":"attacker"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="event_fingerprint"):
        repository.read(entry.strategy_id, 1)


def test_repository_rejects_a_concurrent_writer_lock(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    entry = _entry(repo_root)
    repository = StrategyRepository(tmp_path / "library")
    lock_path = repository.root / ".strategy-repository.lock"

    with (
        exclusive_file_lock(lock_path, label="test strategy repository"),
        pytest.raises(RuntimeError, match="locked by another writer"),
    ):
        repository.create_draft(entry, actor="builder")

    record = repository.create_draft(entry, actor="builder")
    assert record.entry == entry
    assert lock_path.is_file()
