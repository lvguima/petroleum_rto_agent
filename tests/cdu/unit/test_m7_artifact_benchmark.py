from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from scripts.cdu.benchmark_m7_artifacts import (
    ARTIFACT_BENCHMARK_CASES,
    EXPECTED_INPUT_RESOURCE_IDS,
    ArtifactBenchmarkValidationError,
    ArtifactCaseResult,
    ArtifactSnapshot,
    OperationMeasurement,
    RoundTripMeasurement,
    build_document,
    finalize_document,
    publish_outputs,
    summarize_case,
    validate_round_trip_measurement,
    verify_document,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _snapshot(
    *,
    status: str = "success",
    sample_count: int = 7201,
    result_fingerprint: str = _DIGEST_A,
    manifest_result_fingerprint: str = _DIGEST_A,
) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        runtime_status=status,  # type: ignore[arg-type]
        manifest_runtime_status=status,  # type: ignore[arg-type]
        sample_count=sample_count,
        duration_s=7200.0,
        time_step_s=1.0,
        result_fingerprint=result_fingerprint,
        manifest_result_fingerprint=manifest_result_fingerprint,
        input_resource_ids=EXPECTED_INPUT_RESOURCE_IDS,
    )


def _measurement(
    repetition: int,
    *,
    write_wall_s: float = 2.0,
    write_peak_bytes: int = 200,
    read_wall_s: float = 1.0,
    read_peak_bytes: int = 100,
) -> RoundTripMeasurement:
    snapshot = _snapshot()
    return RoundTripMeasurement(
        repetition=repetition,
        write=OperationMeasurement("write_run", write_wall_s, write_peak_bytes),
        read=OperationMeasurement("read_run", read_wall_s, read_peak_bytes),
        written_snapshot=snapshot,
        read_snapshot=snapshot,
    )


def _case_results() -> tuple[ArtifactCaseResult, ...]:
    results: list[ArtifactCaseResult] = []
    for specification in ARTIFACT_BENCHMARK_CASES:
        results.append(
            summarize_case(
                specification,
                (
                    _measurement(1),
                    _measurement(
                        2,
                        write_wall_s=4.0,
                        write_peak_bytes=400,
                        read_wall_s=3.0,
                        read_peak_bytes=300,
                    ),
                ),
            )
        )
    return tuple(results)


def test_summarize_case_uses_two_round_trip_medians_and_maxima() -> None:
    result = summarize_case(
        ARTIFACT_BENCHMARK_CASES[0],
        (
            _measurement(1),
            _measurement(
                2,
                write_wall_s=4.0,
                write_peak_bytes=400,
                read_wall_s=3.0,
                read_peak_bytes=300,
            ),
        ),
    )

    assert result.write_summary.wall_time_median_s == 3.0
    assert result.write_summary.wall_time_max_s == 4.0
    assert result.write_summary.python_peak_median_bytes == 300
    assert result.read_summary.wall_time_median_s == 2.0
    assert result.read_summary.python_peak_max_bytes == 300
    assert result.as_dict()["result_fingerprint"] == _DIGEST_A


@pytest.mark.parametrize(
    ("attack", "message"),
    [
        ("status", "expected status"),
        ("sample_count", "expected 7201 samples"),
        ("manifest_fingerprint", "differs from manifest"),
    ],
)
def test_forged_status_sample_or_manifest_fingerprint_is_rejected(
    attack: str,
    message: str,
) -> None:
    measurement = _measurement(1)
    if attack == "status":
        forged = replace(
            measurement,
            read_snapshot=_snapshot(status="failed"),
        )
    elif attack == "sample_count":
        forged = replace(
            measurement,
            read_snapshot=_snapshot(sample_count=7200),
        )
    else:
        forged = replace(
            measurement,
            read_snapshot=_snapshot(manifest_result_fingerprint=_DIGEST_B),
        )

    with pytest.raises(ArtifactBenchmarkValidationError, match=message):
        validate_round_trip_measurement(ARTIFACT_BENCHMARK_CASES[0], forged)


def test_report_fingerprint_is_self_verifying_and_detects_tampering() -> None:
    report = finalize_document(
        {
            "schema_version": "test-schema",
            "generated_at_utc": "2026-08-18T00:00:00Z",
            "status": "success",
            "cases": [{"case_id": "fixed"}],
        }
    )
    verify_document(report)

    tampered = deepcopy(report)
    tampered["status"] = "failed"
    with pytest.raises(
        ArtifactBenchmarkValidationError,
        match="fingerprint mismatch",
    ):
        verify_document(tampered)


def test_json_markdown_pair_is_transactionally_published_and_reloadable(
    tmp_path: Path,
) -> None:
    results = _case_results()
    document = build_document(results)
    json_path = tmp_path / "artifact-baseline.json"
    markdown_path = tmp_path / "artifact-baseline.md"
    json_path.write_text("old-json", encoding="utf-8")
    markdown_path.write_text("old-markdown", encoding="utf-8")

    publish_outputs(document, json_path, markdown_path)

    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    verify_document(loaded)
    markdown = markdown_path.read_text(encoding="utf-8")
    assert cast(str, document["report_fingerprint"]) in markdown
    assert json_path.name in markdown
    assert not tuple(tmp_path.glob("*.stage"))
    assert not tuple(tmp_path.glob("*.backup"))
