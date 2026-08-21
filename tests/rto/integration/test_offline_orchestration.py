from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto import (
    LegacyOfflineRtoOrchestratorV1 as OfflineRtoOrchestrator,
)
from petroleum_rto.rto import (
    LegacyStrategyRepositoryV1 as StrategyRepository,
)
from petroleum_rto.rto import (
    SimulationPreviewV1,
    SimulationRunBundleV1,
    load_rto_v1_bundle,
)
from petroleum_rto.rto import (
    bind_legacy_external_optimization_request_v1 as bind_external_optimization_request,
)
from petroleum_rto.rto import (
    load_legacy_external_optimization_request_v1 as load_external_optimization_request,
)
from petroleum_rto.rto import (
    read_legacy_offline_run_v1 as read_offline_run,
)
from petroleum_rto.rto.adapters import CduM7RequestFactory
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    ContractRef,
    JsonValue,
    SimulationEvaluationRequestV1,
)
from petroleum_rto.rto.contracts.common import thaw_json
from petroleum_rto.rto.runtime import build_chat_result_summary, run_summary


class _DeterministicSimulator:
    def __init__(
        self,
        make_bundle: Callable[..., SimulationRunBundleV1],
    ) -> None:
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
                "run_ref": f"/fake-rto-evidence/{seed}",
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


def test_offline_workflow_builds_sampled_draft_and_resumes_without_execution(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle = load_rto_v1_bundle(repo_root)
    simulator = _DeterministicSimulator(make_bundle)
    repository = StrategyRepository(tmp_path / "library")
    orchestrator = OfflineRtoOrchestrator(
        CduM7RequestFactory(),
        lambda _: simulator,
    )

    first = orchestrator.run(
        bundle,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="offline-builder",
    )
    execution_count = simulator.execution_count
    second = orchestrator.run(
        bundle,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="offline-builder",
    )

    assert first.result.status == "completed_draft"
    assert first.strategy is not None
    assert first.strategy.coverage_kind == "sampled_anchors"
    assert len(first.strategy.anchors) == 3
    serialized_anchors = cast(list[dict[str, object]], first.strategy.as_dict()["anchors"])
    assert all("static_evaluation" not in item for item in serialized_anchors)
    assert all("dynamic_evaluation" not in item for item in serialized_anchors)
    assert first.result.requested_anchor_count == 3
    assert first.result.passed_anchor_count == 3
    assert repository.read_ref(first.strategy.ref).current_state == "draft"
    assert first.physical_m2_executions > 0
    assert first.physical_m4_executions > 0
    assert simulator.execution_count == execution_count
    assert second.result.fingerprint == first.result.fingerprint
    assert second.strategy == first.strategy
    assert second.recovered_stages == ("manifest",)
    assert second.physical_m2_executions == 0
    assert second.physical_m4_executions == 0
    chat_summary = build_chat_result_summary(second)
    assert run_summary(second)["selected_setpoints"] == chat_summary["selected_setpoints"]
    assert chat_summary["status"] == "success"
    assert len(chat_summary["selected_setpoints"]) == 2
    assert chat_summary["objectives"][0]["unit"] == "MJ/t"
    assert all(item["passed"] is True for item in chat_summary["constraints"])


def test_offline_workflow_recovers_static_stage_and_rejects_artifact_tampering(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    bundle = load_rto_v1_bundle(repo_root)
    simulator = _DeterministicSimulator(make_bundle)
    repository = StrategyRepository(tmp_path / "library")
    orchestrator = OfflineRtoOrchestrator(CduM7RequestFactory(), lambda _: simulator)
    first = orchestrator.run(
        bundle,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="offline-builder",
        coverage_policy="point",
    )
    for name in (
        "manifest.json",
        "optimization_result.json",
        "anchor_validation.json",
        "strategy.json",
        "result.json",
    ):
        (first.run_dir / name).unlink()
    events = (first.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    (first.run_dir / "events.jsonl").write_text("\n".join(events[:3]) + "\n", encoding="utf-8")
    executions_before = simulator.execution_count

    resumed = orchestrator.run(
        bundle,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="offline-builder",
        coverage_policy="point",
    )

    assert "static-search" in resumed.recovered_stages
    assert resumed.physical_m2_executions == 0
    assert resumed.physical_m4_executions > 0
    assert simulator.execution_count > executions_before

    result_path = resumed.run_dir / "result.json"
    result_path.write_text(result_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        read_offline_run(
            resumed.run_dir,
            bundle=bundle,
            strategy_repository=repository,
            simulator=simulator,
        )


def test_external_json_request_runs_through_the_existing_offline_workflow(
    repo_root: Path,
    tmp_path: Path,
    make_bundle: Callable[..., SimulationRunBundleV1],
) -> None:
    request = load_external_optimization_request(
        repo_root / "configs/rto/requests/user_defined_feed_400_v1.json"
    )
    bound = bind_external_optimization_request(load_rto_v1_bundle(repo_root), request)
    simulator = _DeterministicSimulator(make_bundle)
    repository = StrategyRepository(tmp_path / "library")

    orchestrator = OfflineRtoOrchestrator(CduM7RequestFactory(), lambda _: simulator)
    record = orchestrator.run(
        bound.bundle,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="domain-model-adapter",
        coverage_policy=bound.external_request.coverage_policy,
        external_request_ref=bound.external_request.ref,
    )

    assert record.result.status == "completed_draft"
    assert record.problem == bound.problem
    assert record.request.context_ref == bound.bundle.context.ref
    assert record.request.intent_ref == bound.bundle.intent.ref
    assert record.request.external_request_ref == request.ref
    assert record.result.requested_anchor_count == 1
    assert record.strategy is not None
    assert record.strategy.coverage_kind == "point"
    assert record.strategy.anchors[0].feed_mass_flow_kg_s == pytest.approx(400.0 / 3.6)
    assert repository.read_ref(record.strategy.ref).current_state == "draft"

    recovered = orchestrator.run(
        bound.bundle,
        run_root=tmp_path / "runs",
        strategy_repository=repository,
        actor="domain-model-adapter",
        coverage_policy=bound.external_request.coverage_policy,
        external_request_ref=bound.external_request.ref,
    )
    assert recovered.recovered_stages == ("manifest",)
    with pytest.raises(ValueError, match="offline request differs"):
        read_offline_run(
            record.run_dir,
            bundle=bound.bundle,
            strategy_repository=repository,
            simulator=simulator,
        )
