from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.catalogs import load_rto_v2_bundle
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    ContractRef,
    JsonValue,
    SimulationEvaluationRequestV1,
    SimulationPreviewV1,
    SimulationRunBundleV1,
)
from petroleum_rto.rto.contracts.common import thaw_json
from petroleum_rto.rto.inputs import (
    bind_external_optimization_request_v2,
    load_external_optimization_request_v2,
)
from petroleum_rto.rto.orchestration import (
    OfflineRtoOrchestratorV2,
    read_offline_run_v2,
)
from petroleum_rto.rto.runtime import build_chat_result_summary, run_summary
from petroleum_rto.rto.strategies import StrategyDraftRepositoryV2


class _V2DeterministicSimulator:
    def __init__(self, make_bundle: Callable[..., SimulationRunBundleV1]) -> None:
        self._make_bundle = make_bundle
        self._evidence: dict[Path, SimulationRunBundleV1] = {}
        self.execution_count = 0

    def preview(self, request: SimulationEvaluationRequestV1) -> SimulationPreviewV1:
        return SimulationPreviewV1(
            schema_version=RTO_SCHEMA_VERSION,
            preview_version="simulation-preview-v1",
            simulation_request_ref=ContractRef(request.request_id, request.fingerprint),
            provider_id=request.provider_id,
            provider_preview_fingerprint=request.fingerprint,
            effective_input_fingerprint=request.provider_request_fingerprint,
            base_object_fingerprints={"base": "a" * 64},
            effective_object_fingerprints={"effective": "b" * 64},
            claim_scope=CLAIM_SCOPE,
        )

    def evaluate(
        self,
        request: SimulationEvaluationRequestV1,
        expected_preview_fingerprint: str,
    ) -> SimulationRunBundleV1:
        if expected_preview_fingerprint != request.fingerprint:
            raise ValueError("preview fingerprint mismatch")
        raw = thaw_json(cast(JsonValue, request.provider_request))
        assert isinstance(raw, dict)
        parameters = raw["parameters"]
        assert isinstance(parameters, dict)
        temperature_k = float(parameters["operating.furnace_outlet_temperature_c"]) + 273.15
        pressure_pa_a = (
            float(parameters["operating.tower_top_pressure_mpa_g"]) * 1_000_000.0 + 101325.0
        )
        objective = (
            180.0 + (temperature_k - 626.35) ** 2 + ((pressure_pa_a - 152325.0) / 1000.0) ** 2
        )
        base = self._make_bundle(
            request.provider_request_fingerprint,
            stage=request.stage,
            objective=objective,
        )
        seed = hashlib.sha256(
            f"{request.stage}:{request.provider_request_fingerprint}".encode()
        ).hexdigest()
        value = base.as_dict()
        value.update(
            {
                "run_ref": f"/fake-rto-v2-evidence/{seed}",
                "request_fingerprint": hashlib.sha256(f"request:{seed}".encode()).hexdigest(),
                "effective_input_fingerprint": hashlib.sha256(
                    f"effective:{seed}".encode()
                ).hexdigest(),
                "result_fingerprint": hashlib.sha256(f"result:{seed}".encode()).hexdigest(),
                "manifest_fingerprint": hashlib.sha256(f"manifest:{seed}".encode()).hexdigest(),
                "source_fingerprints": {
                    "control.pi": "c" * 64,
                    "runtime_effective_object.case": seed,
                },
            }
        )
        bundle = SimulationRunBundleV1.from_mapping(value)
        self._evidence[Path(bundle.run_ref)] = bundle
        self.execution_count += 1
        return bundle

    def read_evidence(self, run_ref: Path) -> SimulationRunBundleV1:
        return self._evidence[run_ref]


def _bound(repo_root: Path):
    request = load_external_optimization_request_v2(
        repo_root / "configs/rto/requests/multiobjective_example_v2.json"
    )
    return bind_external_optimization_request_v2(load_rto_v2_bundle(repo_root), request)


def test_v2_workflow_builds_compact_point_draft_and_resumes_without_execution(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bound = _bound(repo_root)
    simulator = _V2DeterministicSimulator(make_bundle)
    repository = StrategyDraftRepositoryV2(tmp_path / "library")
    orchestrator = OfflineRtoOrchestratorV2(CduM7RequestFactory(), lambda _: simulator)

    first = orchestrator.run(
        bound.bundle,
        bound.external_request,
        bound.resolved_intent,
        bound.problem,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="domain-model-adapter",
    )
    execution_count = simulator.execution_count
    second = orchestrator.run(
        bound.bundle,
        bound.external_request,
        bound.resolved_intent,
        bound.problem,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="domain-model-adapter",
    )

    assert first.result.status == "completed_draft"
    assert first.pareto_search.grid_count == 81
    assert first.physical_m2_executions == 81
    assert first.physical_m4_executions > 0
    assert first.strategy is not None
    assert first.strategy.state == "draft"
    assert len(first.strategy.anchors) == 1
    strategy_json = first.strategy.as_dict()
    assert "pareto_layers" not in strategy_json
    assert "evaluations" not in strategy_json
    assert "timeseries" not in strategy_json
    assert not first.strategy.field_validated
    assert not first.strategy.dcs_write_capability
    assert not hasattr(repository, "approve")
    assert not hasattr(repository, "publish")
    assert simulator.execution_count == execution_count
    assert second.result.fingerprint == first.result.fingerprint
    assert second.physical_m2_executions == 0
    assert second.physical_m4_executions == 0
    assert "workflow-complete" in second.recovered_stages
    chat_summary = build_chat_result_summary(second)
    assert run_summary(second)["selected_setpoints"] == chat_summary["selected_setpoints"]
    assert chat_summary["status"] == "success"
    assert len(chat_summary["selected_setpoints"]) == 2
    assert len(chat_summary["objectives"]) == 3
    assert all(item["passed"] is True for item in chat_summary["constraints"])


def test_v2_workflow_recovers_pareto_and_rejects_manifest_tampering(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bound = _bound(repo_root)
    simulator = _V2DeterministicSimulator(make_bundle)
    repository = StrategyDraftRepositoryV2(tmp_path / "library")
    orchestrator = OfflineRtoOrchestratorV2(CduM7RequestFactory(), lambda _: simulator)
    first = orchestrator.run(
        bound.bundle,
        bound.external_request,
        bound.resolved_intent,
        bound.problem,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="domain-model-adapter",
    )
    for name in (
        "manifest.json",
        "dynamic_verification.json",
        "optimization_result.json",
        "anchor_validation.json",
        "strategy_draft.json",
        "result.json",
    ):
        (first.run_dir / name).unlink()
    events = (first.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    (first.run_dir / "events.jsonl").write_text("\n".join(events[:4]) + "\n", encoding="utf-8")

    resumed = orchestrator.run(
        bound.bundle,
        bound.external_request,
        bound.resolved_intent,
        bound.problem,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="domain-model-adapter",
    )

    assert "pareto-search" in resumed.recovered_stages
    assert "preference-selection" in resumed.recovered_stages
    assert resumed.physical_m2_executions == 0
    assert resumed.physical_m4_executions > 0

    result_path = resumed.run_dir / "result.json"
    result_path.write_text(result_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        read_offline_run_v2(
            resumed.run_dir,
            bundle=bound.bundle,
            external_request=bound.external_request,
            resolved_intent=bound.resolved_intent,
            strategy_repository=repository,
            simulator=simulator,
        )
