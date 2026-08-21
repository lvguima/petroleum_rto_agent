from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.cdu.benchmark_m7 import (
    BENCHMARK_CASES,
    BenchmarkValidationError,
    RunMeasurement,
    finalize_document,
    summarize_case,
    validate_measurement,
    verify_document,
)

_DIGEST_A = "a" * 64


def _measurement(
    repetition: int,
    *,
    status: str = "success",
    wall_time_s: float = 1.0,
    peak_bytes: int = 100,
    sample_count: int = 0,
) -> RunMeasurement:
    return RunMeasurement(
        repetition=repetition,
        runtime_status=status,  # type: ignore[arg-type]
        wall_time_s=wall_time_s,
        python_peak_bytes=peak_bytes,
        sample_count=sample_count,
        duration_s=None,
        time_step_s=None,
        result_fingerprint=_DIGEST_A,
    )


def test_summarize_case_uses_fixed_three_run_median_and_max() -> None:
    result = summarize_case(
        BENCHMARK_CASES[0],
        (
            _measurement(1, wall_time_s=3.0, peak_bytes=300),
            _measurement(2, wall_time_s=1.0, peak_bytes=100),
            _measurement(3, wall_time_s=2.0, peak_bytes=200),
        ),
    )

    assert result.summary.wall_time_median_s == 2.0
    assert result.summary.wall_time_max_s == 3.0
    assert result.summary.python_peak_median_bytes == 200
    assert result.summary.python_peak_max_bytes == 300
    assert result.as_dict()["result_fingerprint"] == _DIGEST_A


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
    with pytest.raises(BenchmarkValidationError, match="fingerprint mismatch"):
        verify_document(tampered)


@pytest.mark.parametrize(
    ("measurement", "message"),
    [
        (_measurement(1, status="failed"), "expected status"),
        (_measurement(1, sample_count=1), "expected 0 samples"),
    ],
)
def test_invalid_status_or_sample_count_cannot_be_published(
    measurement: RunMeasurement,
    message: str,
) -> None:
    with pytest.raises(BenchmarkValidationError, match=message):
        validate_measurement(BENCHMARK_CASES[0], measurement)
