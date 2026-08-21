from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from petroleum_rto.rto.contracts.common import JsonValue
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE
from petroleum_rto.rto.contracts.simulation import (
    SIMULATION_SCHEMA_VERSION,
    SimulationRunBundle,
)


def _m2_summary(objective: float, *, quality_scale: float, yield_delta: float) -> dict[str, object]:
    products = ("gasoline", "kerosene", "light_diesel", "heavy_diesel")
    quality = {
        name: {
            "density_kg_m3_proxy": (800.0 + index * 10.0) * quality_scale,
            "t50_k_proxy": (400.0 + index * 20.0) * quality_scale,
        }
        for index, name in enumerate(products)
    }
    return {
        "flowsheet": {
            "balance": {"relative_residual": 0.0},
            "diagnostics": {
                "gasoline_yield_mass_fraction": 0.16 + yield_delta,
                "kerosene_yield_mass_fraction": 0.08,
                "light_diesel_yield_mass_fraction": 0.06,
                "heavy_diesel_yield_mass_fraction": 0.20,
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
def make_bundle() -> Callable[..., SimulationRunBundle]:
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
    ) -> SimulationRunBundle:
        nonlocal counter
        counter += 1
        success = accepted if stage == "M4" else True
        summary = (
            _m2_summary(objective, quality_scale=quality_scale, yield_delta=yield_delta)
            if stage == "M2"
            else _m4_summary(accepted, control_fingerprint=control_fingerprint)
        )
        return SimulationRunBundle(
            schema_version=SIMULATION_SCHEMA_VERSION,
            bundle_version="simulation-run-bundle",
            provider_id="cdu-m7-v1",
            provider_request_fingerprint=provider_request_fingerprint,
            run_ref=f"/tmp/run-{counter}",
            runtime_status=runtime_status or ("success" if success else "failed"),
            engine_status=engine_status or ("success" if success else "failed"),
            summary=cast(dict[str, JsonValue], summary),
            sample_count=7201 if stage == "M4" else 0,
            event_count=2 if stage == "M4" else 0,
            request_fingerprint=f"{counter % 10}" * 64,
            effective_input_fingerprint=f"{(counter + 1) % 10}" * 64,
            result_fingerprint=f"{(counter + 2) % 10}" * 64,
            manifest_fingerprint=f"{(counter + 3) % 10}" * 64,
            versions={"model_version": "m7", "simulation_stage": stage},
            source_fingerprints={
                "control.pi": stable_source * 64,
                "runtime_effective_object.case": f"{(counter + 4) % 10}" * 64,
            },
            failure_stage=None if success else "performance",
            failure_reason=None if success else "synthetic dynamic failure",
            synthetic=True,
            claim_scope=ENGINEERING_CLAIM_SCOPE,
        )

    return build
