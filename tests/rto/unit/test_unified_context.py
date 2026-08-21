from __future__ import annotations

from petroleum_rto.rto.context import load_operating_context
from petroleum_rto.rto.contracts.context import (
    OPERATING_CONTEXT_SCHEMA_ID,
    OPERATING_CONTEXT_SCHEMA_VERSION,
    OperatingContext,
)
from petroleum_rto.rto.contracts.problem import ENGINEERING_CLAIM_SCOPE
from petroleum_rto.rto.contracts.reference import ContractRef


def _ref(name: str, digit: str) -> ContractRef:
    return ContractRef(name, digit * 64)


def test_operating_context_is_generic_trusted_input_and_round_trips() -> None:
    context = OperatingContext(
        schema_id=OPERATING_CONTEXT_SCHEMA_ID,
        schema_version=OPERATING_CONTEXT_SCHEMA_VERSION,
        context_version="case-20260604",
        context_id="case-20260604-nominal",
        context_schema_ref=_ref("cdu-context-schema", "1"),
        provider_id="cdu-m7",
        model_ref=_ref("cdu-effective-model", "2"),
        case_ref=_ref("case-20260604-effective", "3"),
        operating_mode="normal-steady",
        facts={
            "feed_mass_flow_kg_s": 113.1388888888889,
            "feed_composition": {"naphtha": 0.2, "residue": 0.8},
        },
        current_setpoints={
            "furnace_temperature_target_k": 628.35,
            "tower_top_pressure_target_pa_a": 152325.0,
        },
        initial_state={"flash_drum_inventory_ratio": 1.0},
        data_timestamp="2026-06-04T09:16:00+08:00",
        data_quality="weak-time-alignment",
        claim_scope=ENGINEERING_CLAIM_SCOPE,
    )

    restored = OperatingContext.from_mapping(context.as_dict())

    assert restored == context
    assert restored.ref == context.ref
    assert "objectives" not in restored.as_dict()
    assert "algorithm" not in restored.as_dict()


def test_versioned_context_fixture_uses_the_unified_schema(repo_root) -> None:
    context = load_operating_context(
        repo_root / "configs" / "rto" / "contexts" / "case_20260604.json"
    )

    assert context.schema_id == "operating-context"
    assert context.facts["fresh_feed_load_kg_s"] == 113.1388888888889
    assert context.current_setpoints["furnace_temperature_target_k"] == 628.35
