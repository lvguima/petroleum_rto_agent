from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.rto import LegacyCandidatePlanCompilerV1 as CandidatePlanCompiler
from petroleum_rto.rto import LegacyProblemBuilderV1 as ProblemBuilder
from petroleum_rto.rto import load_rto_v1_bundle
from petroleum_rto.rto.adapters import CduM7RequestFactory, CduM7Simulator
from petroleum_rto.rto.evaluation import SteadyEvaluationService
from petroleum_rto.rto.optimizer import DeterministicGridOptimizer


def test_real_m2_search_is_cached_repeatable_and_within_budget(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    bundle = load_rto_v1_bundle(repo_root)
    problem = ProblemBuilder().build(bundle)
    service = SteadyEvaluationService(
        problem,
        bundle.context,
        bundle.kpi_catalog,
        CandidatePlanCompiler(),
        CduM7RequestFactory(),
        CduM7Simulator(tmp_path / "runs"),
    )
    optimizer = DeterministicGridOptimizer()

    first = optimizer.search(problem, bundle.context, service)
    first_execution_count = service.physical_execution_count
    second = optimizer.search(problem, bundle.context, service)

    assert first.status == "success"
    assert 25 <= len(first.proposals) <= 33
    assert first_execution_count == len(first.proposals)
    assert first_execution_count <= problem.search_plan.maximum_m2_executions
    assert service.physical_execution_count == first_execution_count
    assert service.cache_hit_count == len(first.proposals)
    assert first.fingerprint == second.fingerprint
    assert first.ranked_feasible
    assert first.ranked_feasible[0].baseline_objective == pytest.approx(188.378985)
    assert all(item.status == "feasible" for item in first.ranked_feasible)
