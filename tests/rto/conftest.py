from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from petroleum_rto.rto import LegacyProblemBuilderV1 as ProblemBuilder
from petroleum_rto.rto import load_rto_v1_bundle
from petroleum_rto.rto.catalogs import RtoCatalogBundle
from petroleum_rto.rto.contracts import (
    CLAIM_SCOPE,
    RTO_SCHEMA_VERSION,
    CandidateEvaluationV1,
    CandidateProposalV1,
    ConstraintOutcomeV1,
    ContractRef,
    EvaluationStage,
    EvaluationStatus,
    JsonValue,
    OptimizationProblemV1,
    RunEvidenceRefV1,
    SimulationRunBundleV1,
)


@pytest.fixture
def rto_basis(repo_root: Path) -> tuple[RtoCatalogBundle, OptimizationProblemV1]:
    bundle = load_rto_v1_bundle(repo_root)
    return bundle, ProblemBuilder().build(bundle)


@pytest.fixture
def make_proposal() -> Callable[..., CandidateProposalV1]:
    def build(
        problem: OptimizationProblemV1,
        context_ref: ContractRef,
        *,
        temperature_k: float = 627.35,
        pressure_pa_a: float = 151325.0,
        sequence: int = 0,
    ) -> CandidateProposalV1:
        return CandidateProposalV1(
            schema_version=RTO_SCHEMA_VERSION,
            proposal_version="candidate-proposal-v1",
            candidate_id=f"fixture-{sequence}",
            sequence=sequence,
            origin="fixture",
            problem_ref=problem.ref,
            context_ref=context_ref,
            decision_values={
                "furnace_temperature_target_k": temperature_k,
                "tower_top_pressure_target_pa_a": pressure_pa_a,
            },
            output_kind="steady-setpoint-vector",
            claim_scope=CLAIM_SCOPE,
        )

    return build


def _m2_summary(
    objective: float,
    *,
    quality_scale: float,
    yield_delta: float,
) -> dict[str, object]:
    products = ("gasoline", "kerosene", "light_diesel", "heavy_diesel")
    quality = {
        name: {
            "density_kg_m3_proxy": (800.0 + index * 10.0) * quality_scale,
            "t50_k_proxy": (400.0 + index * 20.0) * quality_scale,
        }
        for index, name in enumerate(products)
    }
    yields = {
        "gasoline_yield_mass_fraction": 0.16 + yield_delta,
        "kerosene_yield_mass_fraction": 0.08,
        "light_diesel_yield_mass_fraction": 0.06,
        "heavy_diesel_yield_mass_fraction": 0.20,
    }
    return {
        "flowsheet": {
            "balance": {"relative_residual": 0.0},
            "diagnostics": {
                **yields,
                "conservation_gate_passed": 1.0,
                "furnace_fuel_duty_w": objective * 100.0 * 1000.0,
            },
            "qualities": quality,
            "streams": {"fresh_crude": {"mass_flow_kg_s": 100.0}},
        }
    }


def _m4_summary(accepted: bool, *, control_fingerprint: str) -> dict[str, object]:
    checks = {
        "plant_execution": True,
        "plant_conservation": True,
        "automatic_initialization_no_bump": True,
        "baseline_hold": True,
        "loop_performance": accepted,
        "true_inventory_safety": True,
    }
    loops = {
        f"loop-{index}": {
            "passed": accepted,
            "settling_time_s": 100.0 if accepted else None,
            "longest_continuous_saturation_s": 0.0,
            "final_error_fraction": 0.001,
        }
        for index in range(7)
    }
    return {
        "acceptance_passed": accepted,
        "acceptance_checks": checks,
        "loop_performance": loops,
        "control_fingerprint": control_fingerprint,
    }


@pytest.fixture
def make_bundle() -> Callable[..., SimulationRunBundleV1]:
    counter = 0

    def build(
        provider_request_fingerprint: str,
        *,
        stage: str,
        objective: float = 188.0,
        quality_scale: float = 1.0,
        yield_delta: float = 0.0,
        accepted: bool = True,
        runtime_status: str | None = None,
        engine_status: str | None = None,
        stable_source: str = "a",
        control_fingerprint: str = "c" * 64,
    ) -> SimulationRunBundleV1:
        nonlocal counter
        counter += 1
        success = accepted if stage == "M4" else True
        runtime = runtime_status or ("success" if success else "failed")
        engine = engine_status or ("success" if success else "failed")
        summary = (
            _m2_summary(objective, quality_scale=quality_scale, yield_delta=yield_delta)
            if stage == "M2"
            else _m4_summary(accepted, control_fingerprint=control_fingerprint)
        )
        return SimulationRunBundleV1(
            schema_version=RTO_SCHEMA_VERSION,
            bundle_version="simulation-run-bundle-v1",
            provider_id="cdu-m7-v1",
            provider_request_fingerprint=provider_request_fingerprint,
            run_ref=f"/tmp/run-{counter}",
            runtime_status=runtime,
            engine_status=engine,
            summary=cast(dict[str, JsonValue], summary),
            sample_count=7201 if stage == "M4" else 0,
            event_count=2 if stage == "M4" else 0,
            request_fingerprint=f"{counter % 10}" * 64,
            effective_input_fingerprint=f"{(counter + 1) % 10}" * 64,
            result_fingerprint=f"{(counter + 2) % 10}" * 64,
            manifest_fingerprint=f"{(counter + 3) % 10}" * 64,
            versions={"model_version": "v1", "simulation_stage": stage},
            source_fingerprints={
                "control.pi": stable_source * 64,
                "runtime_effective_object.case": f"{(counter + 4) % 10}" * 64,
            },
            failure_stage=None if success else "performance",
            failure_reason=None if success else "synthetic dynamic failure",
            synthetic=True,
            claim_scope=CLAIM_SCOPE,
        )

    return build


def _evidence(seed: str) -> RunEvidenceRefV1:
    return RunEvidenceRefV1(
        provider_id="fake-provider",
        run_ref=f"/tmp/{seed}",
        provider_request_fingerprint=seed * 64,
        request_fingerprint=seed * 64,
        effective_input_fingerprint=seed * 64,
        result_fingerprint=seed * 64,
        manifest_fingerprint=seed * 64,
        versions={"model": "v1"},
        source_fingerprints={"source": seed * 64},
    )


@pytest.fixture
def make_evaluation() -> Callable[..., CandidateEvaluationV1]:
    def build(
        proposal: CandidateProposalV1,
        *,
        stage: EvaluationStage = "M2",
        status: EvaluationStatus = "feasible",
        objective: float = 180.0,
        margin: float = 1.0,
        improvement: float = 0.01,
    ) -> CandidateEvaluationV1:
        feasible = status == "feasible"
        constraint = ConstraintOutcomeV1(
            constraint_id="fixture-constraint",
            stage=stage,
            metric_id="fixture-metric",
            operator="ge",
            limit=0.0,
            candidate_value=1.0 if feasible else -1.0,
            baseline_value=1.0,
            normalized_margin=margin if feasible else -1.0,
            passed=feasible,
        )
        return CandidateEvaluationV1(
            schema_version=RTO_SCHEMA_VERSION,
            evaluation_version="candidate-evaluation-v1",
            stage=stage,
            status=status,
            problem_ref=proposal.problem_ref,
            context_ref=proposal.context_ref,
            proposal_ref=proposal.ref,
            pair_id=f"pair-{stage.lower()}-{proposal.fingerprint[:16]}",
            objective_metric_id=(
                "specific_furnace_fuel_energy_mj_per_t" if stage == "M2" and feasible else None
            ),
            baseline_objective=188.0 if stage == "M2" and feasible else None,
            candidate_objective=objective if stage == "M2" and feasible else None,
            objective_delta=objective - 188.0 if stage == "M2" and feasible else None,
            relative_improvement=improvement if stage == "M2" and feasible else None,
            metrics={"fixture-metric": 1.0 if feasible else -1.0},
            constraints=(constraint,),
            minimum_normalized_margin=margin if feasible else -1.0,
            normalized_action_l1=0.0,
            reason_codes=() if feasible else ("fixture-infeasible",),
            baseline_evidence=_evidence("a"),
            candidate_evidence=_evidence("b"),
            claim_scope=CLAIM_SCOPE,
        )

    return build
