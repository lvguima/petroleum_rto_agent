from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from petroleum_rto.rto import LegacyCandidatePlanCompilerV1 as CandidatePlanCompiler
from petroleum_rto.rto import LegacyProblemBuilderV1 as ProblemBuilder
from petroleum_rto.rto import load_rto_v1_bundle
from petroleum_rto.rto.adapters import CduM7RequestFactory, CduM7Simulator
from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.compilation import CompiledPair
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateProposalV1,
    SimulationPreviewV1,
    SimulationRunBundleV1,
)


def _compiled(repo_root: Path, stage: str) -> tuple[RtoCatalogBundle, CompiledPair]:
    bundle = load_rto_v1_bundle(repo_root)
    problem = ProblemBuilder().build(bundle)
    proposal = CandidateProposalV1(
        schema_version=RTO_SCHEMA_VERSION,
        proposal_version="candidate-proposal-v1",
        candidate_id="adapter-fixture",
        sequence=0,
        origin="fixture",
        problem_ref=problem.ref,
        context_ref=bundle.context.ref,
        decision_values={
            "furnace_temperature_target_k": 627.35,
            "tower_top_pressure_target_pa_a": 151325.0,
        },
        output_kind="steady-setpoint-vector",
        claim_scope=CLAIM_SCOPE,
    )
    pair = CandidatePlanCompiler().compile_pair(
        problem,
        bundle.context,
        proposal,
        stage=stage,
        request_factory=CduM7RequestFactory(),
    )
    return bundle, pair


def _path_value(root: object, dotted_path: str) -> object:
    current = root
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


def test_m2_adapter_preview_confirm_run_and_strict_read(repo_root: Path, tmp_path: Path) -> None:
    bundle, pair = _compiled(repo_root, "M2")
    simulator = CduM7Simulator(tmp_path / "runs")
    resolved = simulator.preview(pair.candidate)

    assert resolved.base_object_fingerprints["model"] == bundle.context.model_ref.fingerprint
    assert resolved.base_object_fingerprints["case"] == bundle.context.case_ref.fingerprint
    with pytest.raises(ValueError, match="preview fingerprint"):
        simulator.evaluate(pair.candidate, "0" * 64)

    result = simulator.evaluate(
        pair.candidate,
        resolved.provider_preview_fingerprint,
    )
    reloaded = simulator.read_evidence(Path(result.run_ref))
    assert result == reloaded
    assert SimulationPreviewV1.from_mapping(resolved.as_dict()) == resolved
    assert SimulationRunBundleV1.from_mapping(result.as_dict()) == result
    assert result.runtime_status == "success"
    assert result.synthetic
    assert result.sample_count == 0
    assert result.provider_request_fingerprint == pair.candidate.provider_request_fingerprint
    evidence = {
        "runtime_status": result.runtime_status,
        "engine_status": result.engine_status,
        "summary": result.summary,
        "source_fingerprints": result.source_fingerprints,
    }
    for kpi in bundle.kpi_catalog.kpis:
        if kpi.stage == "M2":
            for source_path in kpi.source_paths:
                if not source_path.startswith("paired."):
                    assert _path_value(evidence, source_path) is not None


def test_m4_pair_previews_without_inheriting_feed_step(repo_root: Path, tmp_path: Path) -> None:
    bundle, pair = _compiled(repo_root, "M4")
    simulator = CduM7Simulator(tmp_path / "runs")
    baseline = simulator.preview(pair.baseline)
    candidate = simulator.preview(pair.candidate)

    assert baseline.base_object_fingerprints["model"] == bundle.context.model_ref.fingerprint
    assert candidate.base_object_fingerprints["case"] == bundle.context.case_ref.fingerprint
    assert baseline.provider_preview_fingerprint != candidate.provider_preview_fingerprint
    assert baseline.effective_input_fingerprint != candidate.effective_input_fingerprint
