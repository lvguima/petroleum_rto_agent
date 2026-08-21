from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.contracts import (
    CandidateEvaluationV1,
    CandidateProposalV1,
    OptimizationProblemV1,
    OptimizationResultV1,
    StaticSearchResultV1,
)
from petroleum_rto.rto.optimizer import DeterministicGridOptimizer, DynamicFinalSelector
from petroleum_rto.rto.strategies import (
    StrategyBuilder,
    StrategyEntryV1,
    StrategyQueryV1,
    StrategyRepository,
    anchor_from_evaluations,
)


class _StaticEvaluator:
    def __init__(self, make_evaluation: Callable[..., CandidateEvaluationV1]) -> None:
        self._make = make_evaluation

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        temperature = proposal.decision_values["furnace_temperature_target_k"]
        pressure = proposal.decision_values["tower_top_pressure_target_pa_a"]
        objective = 180.0 + (temperature - 628.35) ** 2 + ((pressure - 152325.0) / 1000.0) ** 2
        return self._make(proposal, objective=objective, improvement=0.02)


class _DynamicEvaluator:
    def __init__(self, make_evaluation: Callable[..., CandidateEvaluationV1]) -> None:
        self._make = make_evaluation

    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1:
        return self._make(proposal, stage="M4", status="feasible", margin=0.8)


def _draft(
    basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> tuple[StrategyEntryV1, StaticSearchResultV1, OptimizationResultV1]:
    bundle, problem = basis
    static = DeterministicGridOptimizer().search(
        problem,
        bundle.context,
        _StaticEvaluator(make_evaluation),
    )
    result = DynamicFinalSelector().select(
        problem,
        static,
        _DynamicEvaluator(make_evaluation),
    )
    assert result.status == "success"
    assert result.selected_proposal_ref is not None
    assert result.selected_static_evaluation_ref is not None
    assert result.selected_dynamic_evaluation_ref is not None
    proposal = next(item for item in static.proposals if item.ref == result.selected_proposal_ref)
    selected_static = next(
        item for item in static.evaluations if item.ref == result.selected_static_evaluation_ref
    )
    selected_dynamic = next(
        item
        for item in result.dynamic_evaluations
        if item.ref == result.selected_dynamic_evaluation_ref
    )
    anchor = anchor_from_evaluations(
        bundle.context,
        proposal,
        selected_static,
        selected_dynamic,
    )
    entry = StrategyBuilder().build(
        problem,
        bundle.context,
        static,
        result,
        (anchor,),
    )
    return entry, static, result


def test_strategy_builder_requires_publishable_complete_evidence_and_round_trips(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    entry, static, result = _draft(rto_basis, make_evaluation)

    assert entry.coverage_kind == "point"
    assert entry.execution_scope == "offline_simulation_only"
    assert entry.control_authority == "none"
    assert not entry.field_validated
    assert not entry.dcs_write_capability
    assert StrategyEntryV1.from_mapping(entry.as_dict()) == entry
    serialized = json.dumps(entry.as_dict())
    assert "timeseries" not in serialized
    assert '"static_evaluation"' not in serialized
    assert '"dynamic_evaluation"' not in serialized
    assert entry.anchors[0].static_evaluation_ref == result.selected_static_evaluation_ref
    assert entry.anchors[0].dynamic_evaluation_ref == result.selected_dynamic_evaluation_ref

    unavailable = replace(result, status="feasible_not_publishable", publishable=False)
    with pytest.raises(ValueError, match="publishable"):
        StrategyBuilder().build(
            rto_basis[1],
            rto_basis[0].context,
            static,
            unavailable,
            entry.anchors,
        )


def test_legacy_embedded_evaluations_are_read_but_not_reserialized(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
) -> None:
    entry, static, result = _draft(rto_basis, make_evaluation)
    assert result.selected_static_evaluation_ref is not None
    assert result.selected_dynamic_evaluation_ref is not None
    selected_static = next(
        item for item in static.evaluations if item.ref == result.selected_static_evaluation_ref
    )
    selected_dynamic = next(
        item
        for item in result.dynamic_evaluations
        if item.ref == result.selected_dynamic_evaluation_ref
    )
    legacy = entry.as_dict()
    anchor = cast(list[dict[str, object]], legacy["anchors"])[0]
    anchor["static_evaluation"] = selected_static.as_dict()
    anchor["dynamic_evaluation"] = selected_dynamic.as_dict()

    loaded = StrategyEntryV1.from_mapping(legacy)
    compact_anchor = cast(list[dict[str, object]], loaded.as_dict()["anchors"])[0]

    assert loaded == entry
    assert "static_evaluation" not in compact_anchor
    assert "dynamic_evaluation" not in compact_anchor


def test_unreviewed_strategy_is_not_queryable_and_publish_requires_approval(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
    tmp_path: Path,
) -> None:
    entry, _, _ = _draft(rto_basis, make_evaluation)
    repository = StrategyRepository(tmp_path / "library")
    record = repository.create_draft(
        entry,
        actor="builder",
        occurred_at="2026-08-19T12:00:00+08:00",
    )
    query = StrategyQueryV1(
        case_ref=entry.case_ref,
        operating_mode=entry.operating_mode,
        feed_mass_flow_kg_s=entry.anchors[0].feed_mass_flow_kg_s,
        measurement_tolerance_kg_s=1e-9,
    )

    assert record.current_state == "draft"
    assert repository.query(query) == ()
    with pytest.raises(ValueError, match="approved"):
        repository.publish(entry.strategy_id, entry.revision, actor="reviewer")

    approved = repository.approve(
        entry.strategy_id,
        entry.revision,
        actor="reviewer",
        occurred_at="2026-08-19T12:01:00+08:00",
    )
    assert approved.current_state == "approved"
    release = repository.publish(
        entry.strategy_id,
        entry.revision,
        actor="publisher",
        occurred_at="2026-08-19T12:02:00+08:00",
    )

    published = repository.read(entry.strategy_id, entry.revision)
    assert published.current_state == "published"
    assert published.release_ref == release.ref
    assert repository.read_release(release.release_id) == release
    assert repository.query(query) == (published,)


def test_query_matches_only_explicit_anchor_not_continuous_interval(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
    tmp_path: Path,
) -> None:
    entry, _, _ = _draft(rto_basis, make_evaluation)
    repository = StrategyRepository(tmp_path / "library")
    repository.create_draft(entry, actor="builder", occurred_at="2026-08-19T12:00:00+08:00")
    repository.approve(entry.strategy_id, 1, actor="reviewer")
    repository.publish(entry.strategy_id, 1, actor="publisher")
    anchor = entry.anchors[0].feed_mass_flow_kg_s

    outside = StrategyQueryV1(
        case_ref=entry.case_ref,
        operating_mode=entry.operating_mode,
        feed_mass_flow_kg_s=anchor * 1.01,
        measurement_tolerance_kg_s=0.001,
    )
    dependency_mismatch = StrategyQueryV1(
        case_ref=entry.case_ref,
        operating_mode=entry.operating_mode,
        feed_mass_flow_kg_s=anchor,
        measurement_tolerance_kg_s=0.001,
        required_dependency_refs=(replace(entry.case_ref, fingerprint="f" * 64),),
    )

    assert repository.query(outside) == ()
    assert repository.query(dependency_mismatch) == ()


def test_sampled_anchor_query_does_not_interpolate_between_samples(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
    tmp_path: Path,
) -> None:
    point, _, _ = _draft(rto_basis, make_evaluation)
    center = point.anchors[0]
    sampled = replace(
        point,
        coverage_kind="sampled_anchors",
        anchors=(
            replace(center, feed_mass_flow_kg_s=100.0),
            replace(center, feed_mass_flow_kg_s=110.0),
            replace(center, feed_mass_flow_kg_s=120.0),
        ),
    )
    repository = StrategyRepository(tmp_path / "library")
    repository.create_draft(sampled, actor="builder")
    repository.approve(sampled.strategy_id, 1, actor="reviewer")
    repository.publish(sampled.strategy_id, 1, actor="publisher")

    exact = StrategyQueryV1(
        case_ref=sampled.case_ref,
        operating_mode=sampled.operating_mode,
        feed_mass_flow_kg_s=110.0,
        measurement_tolerance_kg_s=0.01,
    )
    between = replace(exact, feed_mass_flow_kg_s=115.0)

    assert len(repository.query(exact)) == 1
    assert repository.query(between) == ()


def test_payload_and_event_tampering_are_rejected(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
    tmp_path: Path,
) -> None:
    entry, _, _ = _draft(rto_basis, make_evaluation)
    repository = StrategyRepository(tmp_path / "library")
    repository.create_draft(entry, actor="builder", occurred_at="2026-08-19T12:00:00+08:00")
    entry_path = repository.entries_root / entry.strategy_id / f"r{entry.revision}" / "entry.json"
    original_entry = entry_path.read_text(encoding="utf-8")
    entry_path.write_text(
        original_entry.replace('"field_validated":false', '"field_validated":true'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="field_validated"):
        repository.read(entry.strategy_id, entry.revision)

    entry_path.write_text(original_entry, encoding="utf-8")
    events_path = entry_path.with_name("events.jsonl")
    original_events = events_path.read_text(encoding="utf-8")
    events_path.write_text(
        original_events.replace('"actor":"builder"', '"actor":"attacker"'), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="event_fingerprint"):
        repository.read(entry.strategy_id, entry.revision)


def test_new_revision_publishes_before_old_revision_is_superseded(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
    tmp_path: Path,
) -> None:
    entry, _, _ = _draft(rto_basis, make_evaluation)
    repository = StrategyRepository(tmp_path / "library")
    repository.create_draft(entry, actor="builder")
    repository.approve(entry.strategy_id, 1, actor="reviewer")
    repository.publish(entry.strategy_id, 1, actor="publisher")
    repository.request_revalidation(entry.strategy_id, 1, actor="version-monitor")
    replacement = replace(entry, revision=2, supersedes=entry.ref)
    repository.create_draft(replacement, actor="builder")
    repository.approve(replacement.strategy_id, 2, actor="reviewer")

    with pytest.raises(ValueError, match="published"):
        repository.supersede(entry.strategy_id, 1, replacement.strategy_id, 2, actor="publisher")

    repository.publish(replacement.strategy_id, 2, actor="publisher")
    old = repository.supersede(
        entry.strategy_id,
        1,
        replacement.strategy_id,
        2,
        actor="publisher",
    )

    assert old.current_state == "superseded"
    assert old.events[-1].related_strategy_ref == replacement.ref
    assert repository.read(replacement.strategy_id, 2).current_state == "published"


def test_repository_rejects_a_concurrent_writer_lock(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
    make_evaluation: Callable[..., CandidateEvaluationV1],
    tmp_path: Path,
) -> None:
    entry, _, _ = _draft(rto_basis, make_evaluation)
    repository = StrategyRepository(tmp_path / "library")
    repository.root.mkdir(parents=True)
    (repository.root / ".strategy-repository.lock").write_text("other-writer", encoding="utf-8")

    with pytest.raises(RuntimeError, match="locked"):
        repository.create_draft(entry, actor="builder")
