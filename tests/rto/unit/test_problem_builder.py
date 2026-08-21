from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.rto import LegacyProblemBuilderV1 as ProblemBuilder
from petroleum_rto.rto import load_rto_v1_bundle
from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.contracts import (
    ContractRef,
    OptimizationProblemV1,
    canonical_json_bytes,
)


def test_problem_builder_is_pure_and_deterministic(repo_root: Path) -> None:
    bundle = load_rto_v1_bundle(repo_root)
    builder = ProblemBuilder()

    first = builder.build(bundle)
    second = builder.build(bundle)

    assert first == second
    assert first.problem_id == "problem-53c57767429f65f4"
    assert canonical_json_bytes(first.as_dict()) == canonical_json_bytes(second.as_dict())
    assert first.objective_metric_id == "specific_furnace_fuel_energy_mj_per_t"
    assert tuple(item.variable_id for item in first.decision_domains) == (
        "furnace_temperature_target_k",
        "tower_top_pressure_target_pa_a",
    )
    assert OptimizationProblemV1.from_mapping(first.as_dict()) == first


def test_problem_builder_rejects_context_reference_drift(repo_root: Path) -> None:
    bundle = load_rto_v1_bundle(repo_root)
    bad_intent = replace(
        bundle.intent,
        operating_context_ref=ContractRef(bundle.context.context_id, "0" * 64),
    )
    bad_bundle = replace(bundle, intent=bad_intent)

    with pytest.raises(ValueError, match="context reference"):
        ProblemBuilder().build(bad_bundle)


def test_problem_builder_rejects_context_as_decision(repo_root: Path) -> None:
    bundle = load_rto_v1_bundle(repo_root)
    feed = bundle.decision_catalog.by_id("fresh_feed_load_kg_s")
    bad_feed = replace(feed, role="decision", enabled=True, m4_loop="feed_flow")
    variables = tuple(
        bad_feed if item.variable_id == feed.variable_id else item
        for item in bundle.decision_catalog.variables
    )
    bad_catalog = replace(bundle.decision_catalog, variables=variables)
    bad_bundle = RtoCatalogBundle(
        decision_catalog=bad_catalog,
        kpi_catalog=bundle.kpi_catalog,
        constraint_profile=bundle.constraint_profile,
        policy=bundle.policy,
        context=bundle.context,
        intent=replace(bundle.intent, decision_profile_id=bad_catalog.catalog_id),
    )

    with pytest.raises(ValueError, match="fresh feed"):
        ProblemBuilder().build(bad_bundle)


def test_problem_builder_accepts_feed_as_fixed_sampled_context(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
) -> None:
    bundle, _ = rto_basis
    context = replace(
        bundle.context,
        context_id="case-20260604-feed-0950",
        feed_mass_flow_kg_s=bundle.context.feed_mass_flow_kg_s * 0.95,
    )
    intent = replace(
        bundle.intent,
        intent_id="minimize-specific-furnace-energy-feed-0950",
        operating_context_ref=context.ref,
    )

    problem = ProblemBuilder().build(replace(bundle, context=context, intent=intent))

    assert problem.context_ref == context.ref


def test_problem_builder_rejects_feed_outside_sampled_context_domain(
    rto_basis: tuple[RtoCatalogBundle, OptimizationProblemV1],
) -> None:
    bundle, _ = rto_basis
    context = replace(
        bundle.context,
        context_id="case-20260604-feed-too-high",
        feed_mass_flow_kg_s=bundle.context.feed_mass_flow_kg_s * 1.051,
    )
    intent = replace(bundle.intent, operating_context_ref=context.ref)

    with pytest.raises(ValueError, match="sampled"):
        ProblemBuilder().build(replace(bundle, context=context, intent=intent))
