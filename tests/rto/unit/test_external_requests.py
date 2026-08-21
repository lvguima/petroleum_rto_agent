from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from petroleum_rto.rto import (
    LegacyExternalOptimizationRequestV1 as ExternalOptimizationRequestV1,
)
from petroleum_rto.rto import (
    bind_legacy_external_optimization_request_v1 as bind_external_optimization_request,
)
from petroleum_rto.rto import (
    load_legacy_external_optimization_request_v1 as load_external_optimization_request,
)
from petroleum_rto.rto import (
    load_rto_v1_bundle,
)


def _request_path(repo_root: Path) -> Path:
    return repo_root / "configs/rto/requests/user_defined_feed_400_v1.json"


def _raw(repo_root: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_request_path(repo_root).read_text(encoding="utf-8")))


def test_external_request_round_trips_and_binds_without_solver(repo_root: Path) -> None:
    request = load_external_optimization_request(_request_path(repo_root))
    rebound = ExternalOptimizationRequestV1.from_mapping(request.as_dict())
    bound = bind_external_optimization_request(load_rto_v1_bundle(repo_root), request)

    assert rebound == request
    assert request.fingerprint == "d8bd20f116de882a0743953f5044d7f531677de5c69cb619594ca75f76ae8a94"
    assert bound.bundle.context.feed_mass_flow_kg_s == pytest.approx(400.0 / 3.6)
    assert bound.bundle.intent.source_type == "human"
    assert bound.problem.problem_id == "problem-54712dcc1665a0b0"
    assert bound.external_request.coverage_policy == "point"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update({"unknown": 1}), "fields differ"),
        (
            lambda raw: raw["operating_context"].update({"feed_mass_flow_t_h": True}),
            "must be numeric",
        ),
        (
            lambda raw: raw["optimization_intent"].update({"source_type": "untrusted-agent"}),
            "source_type",
        ),
        (
            lambda raw: raw["optimization_intent"].update(
                {"objective_profile_id": "arbitrary-python-formula"}
            ),
            "not implemented",
        ),
    ],
)
def test_external_request_rejects_unknown_unsafe_or_unimplemented_values(
    repo_root: Path,
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    raw = _raw(repo_root)
    mutation(raw)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        request = load_external_optimization_request(path)
        bind_external_optimization_request(load_rto_v1_bundle(repo_root), request)


def test_external_request_rejects_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"1.0.0","schema_version":"1.0.0"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_external_optimization_request(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"feed":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        load_external_optimization_request(nonfinite)


def test_domain_model_source_uses_the_same_strict_contract(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    raw = _raw(repo_root)
    raw["request_id"] = "domain-model-request-v1"
    raw["optimization_intent"]["intent_id"] = "domain-model-energy-intent"
    raw["optimization_intent"]["source_type"] = "domain-model"
    raw["optimization_intent"]["source_ref"] = "cdu-domain-model-v1-inference-0001"
    path = tmp_path / "domain-model-request.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    request = load_external_optimization_request(path)
    bound = bind_external_optimization_request(load_rto_v1_bundle(repo_root), request)

    assert request.optimization_intent.source_type == "domain-model"
    assert bound.bundle.intent.source_type == "domain-model"
    assert bound.bundle.intent.source_ref == "cdu-domain-model-v1-inference-0001"
    assert bound.problem.context_ref == bound.bundle.context.ref


def test_external_request_rejects_stale_context_and_out_of_domain_anchors(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    raw = _raw(repo_root)
    raw["operating_context"]["base_context_ref"]["fingerprint"] = "0" * 64
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="base_context_ref"):
        bind_external_optimization_request(
            load_rto_v1_bundle(repo_root), load_external_optimization_request(path)
        )

    raw = _raw(repo_root)
    raw["coverage_policy"] = "sampled-anchors"
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="sampled anchor feed"):
        bind_external_optimization_request(
            load_rto_v1_bundle(repo_root), load_external_optimization_request(path)
        )
