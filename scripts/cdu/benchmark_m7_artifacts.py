"""Measure complete M7 two-hour run-artifact write and strict read costs.

Model execution and packaged-input loading are deliberately outside the two
measurement regions.  Each region records wall time and the peak Python-traced
allocation reported by :mod:`tracemalloc`; this is a report-only baseline and
does not impose a cross-machine performance threshold.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import re
import statistics
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from petroleum_rto.cdu.core.config import canonical_fingerprint
from petroleum_rto.cdu.runtime.api import runtime_input_resources
from petroleum_rto.cdu.runtime.artifacts import RunRecord, read_run, write_run
from petroleum_rto.cdu.runtime.contracts import (
    RUNTIME_VERSION,
    ExecutionPayload,
    RunRequest,
    RuntimeStatus,
)
from petroleum_rto.cdu.runtime.executor import execute
from petroleum_rto.cdu.runtime.presets import load_preset
from petroleum_rto.cdu.runtime.provenance import (
    installed_source_tree_sha256,
    runtime_environment,
)
from petroleum_rto.cdu.runtime.resources import list_runtime_resource_ids

ARTIFACT_BENCHMARK_SCHEMA_VERSION = "m7.artifact-io-baseline.schema-v1"
ARTIFACT_BENCHMARK_VERSION = "m7-artifact-io-baseline-v0.1.0"
REPETITIONS = 2
EXPECTED_INPUT_RESOURCE_IDS = list_runtime_resource_ids()

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSON_PATH = _PROJECT_ROOT / "reports" / "cdu" / "m7_artifact_io_baseline_v0.1.0.json"
DEFAULT_MARKDOWN_PATH = _PROJECT_ROOT / "reports" / "cdu" / "M7_ARTIFACT_IO_BASELINE.md"

OperationName = Literal["write_run", "read_run"]


class ArtifactBenchmarkValidationError(RuntimeError):
    """Raised when an artifact measurement is not safe to publish."""


@dataclass(frozen=True)
class ArtifactBenchmarkCase:
    """Frozen contract for one complete two-hour artifact round trip."""

    case_id: str
    model_layer: str
    preset_id: str
    expected_status: RuntimeStatus
    expected_sample_count: int
    expected_duration_s: float
    expected_time_step_s: float
    description: str

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "model_layer": self.model_layer,
            "preset_id": self.preset_id,
            "expected_status": self.expected_status,
            "expected_sample_count": self.expected_sample_count,
            "expected_duration_s": self.expected_duration_s,
            "expected_time_step_s": self.expected_time_step_s,
            "description": self.description,
        }


@dataclass(frozen=True)
class ArtifactSnapshot:
    """Small validated identity snapshot retained after a full payload is dropped."""

    runtime_status: RuntimeStatus
    manifest_runtime_status: RuntimeStatus
    sample_count: int
    duration_s: float | None
    time_step_s: float | None
    result_fingerprint: str
    manifest_result_fingerprint: str
    input_resource_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_status": self.runtime_status,
            "manifest_runtime_status": self.manifest_runtime_status,
            "sample_count": self.sample_count,
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "result_fingerprint": self.result_fingerprint,
            "manifest_result_fingerprint": self.manifest_result_fingerprint,
            "input_resource_count": len(self.input_resource_ids),
            "input_resource_ids": list(self.input_resource_ids),
        }


@dataclass(frozen=True)
class OperationMeasurement:
    """Wall time and traced Python allocation peak for one artifact operation."""

    operation: OperationName
    wall_time_s: float
    python_peak_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "wall_time_s": self.wall_time_s,
            "python_peak_bytes": self.python_peak_bytes,
        }


@dataclass(frozen=True)
class RoundTripMeasurement:
    """One write/read measurement pair for an already executed model payload."""

    repetition: int
    write: OperationMeasurement
    read: OperationMeasurement
    written_snapshot: ArtifactSnapshot
    read_snapshot: ArtifactSnapshot

    def as_dict(self) -> dict[str, object]:
        return {
            "repetition": self.repetition,
            "write": self.write.as_dict(),
            "read": self.read.as_dict(),
            "written_artifact": self.written_snapshot.as_dict(),
            "strict_readback": self.read_snapshot.as_dict(),
        }


@dataclass(frozen=True)
class OperationSummary:
    """Median and maximum values for one operation over the fixed repetitions."""

    wall_time_median_s: float
    wall_time_max_s: float
    python_peak_median_bytes: int
    python_peak_max_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "wall_time_s": {
                "median": self.wall_time_median_s,
                "max": self.wall_time_max_s,
            },
            "python_peak_bytes": {
                "median": self.python_peak_median_bytes,
                "max": self.python_peak_max_bytes,
            },
        }


@dataclass(frozen=True)
class ArtifactCaseResult:
    """Validated repetitions and aggregates for one frozen two-hour preset."""

    specification: ArtifactBenchmarkCase
    measurements: tuple[RoundTripMeasurement, ...]
    write_summary: OperationSummary
    read_summary: OperationSummary

    def as_dict(self) -> dict[str, object]:
        return {
            **self.specification.as_dict(),
            "repetitions": [item.as_dict() for item in self.measurements],
            "summary": {
                "write_run": self.write_summary.as_dict(),
                "read_run": self.read_summary.as_dict(),
            },
            "result_fingerprint": (self.measurements[0].written_snapshot.result_fingerprint),
        }


ARTIFACT_BENCHMARK_CASES: tuple[ArtifactBenchmarkCase, ...] = (
    ArtifactBenchmarkCase(
        case_id="m3_open_loop_feed_plus_5pct_2h_artifact_io",
        model_layer="M3",
        preset_id="open-loop-feed-step",
        expected_status="success",
        expected_sample_count=7201,
        expected_duration_s=7200.0,
        expected_time_step_s=1.0,
        description=(
            "Complete JSONL/JSON/manifest write and strict readback for the M3 "
            "two-hour open-loop +5% fresh-feed step."
        ),
    ),
    ArtifactBenchmarkCase(
        case_id="m4_closed_loop_feed_plus_5pct_2h_artifact_io",
        model_layer="M4",
        preset_id="closed-loop-feed-step",
        expected_status="success",
        expected_sample_count=7201,
        expected_duration_s=7200.0,
        expected_time_step_s=1.0,
        description=(
            "Complete JSONL/JSON/manifest write and strict readback for the M4 "
            "two-hour closed-loop +5% feed-setpoint step."
        ),
    ),
)


def _same_number(actual: float | None, expected: float) -> bool:
    return actual is not None and math.isclose(
        actual,
        expected,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    )


def _validate_operation(
    item: OperationMeasurement,
    *,
    expected_operation: OperationName,
    prefix: str,
) -> None:
    if item.operation != expected_operation:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: expected operation {expected_operation!r}, got {item.operation!r}"
        )
    if not math.isfinite(item.wall_time_s) or item.wall_time_s <= 0.0:
        raise ArtifactBenchmarkValidationError(f"{prefix}: artifact wall time must be positive")
    if item.python_peak_bytes <= 0:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: tracemalloc Python peak must be positive"
        )


def _validate_snapshot(
    specification: ArtifactBenchmarkCase,
    snapshot: ArtifactSnapshot,
    *,
    prefix: str,
) -> None:
    if snapshot.runtime_status != specification.expected_status:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: expected status {specification.expected_status!r}, "
            f"got {snapshot.runtime_status!r}"
        )
    if snapshot.manifest_runtime_status != snapshot.runtime_status:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: payload status differs from manifest status"
        )
    if snapshot.sample_count != specification.expected_sample_count:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: expected {specification.expected_sample_count} samples, "
            f"got {snapshot.sample_count}"
        )
    if not _same_number(snapshot.duration_s, specification.expected_duration_s):
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: unexpected duration {snapshot.duration_s!r}"
        )
    if not _same_number(snapshot.time_step_s, specification.expected_time_step_s):
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: unexpected time step {snapshot.time_step_s!r}"
        )
    if not _DIGEST.fullmatch(snapshot.result_fingerprint):
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: result fingerprint must be a lowercase SHA-256 digest"
        )
    if not _DIGEST.fullmatch(snapshot.manifest_result_fingerprint):
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: manifest result fingerprint must be a lowercase SHA-256 digest"
        )
    if snapshot.result_fingerprint != snapshot.manifest_result_fingerprint:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: result fingerprint differs from manifest"
        )
    if snapshot.input_resource_ids != EXPECTED_INPUT_RESOURCE_IDS:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: expected the fixed ordered set of 11 packaged inputs"
        )
    if len(snapshot.input_resource_ids) != 11:
        raise ArtifactBenchmarkValidationError(f"{prefix}: expected exactly 11 packaged inputs")


def validate_round_trip_measurement(
    specification: ArtifactBenchmarkCase,
    item: RoundTripMeasurement,
) -> None:
    """Reject a forged or incomplete artifact round-trip measurement."""

    prefix = f"{specification.case_id} repetition {item.repetition}"
    if item.repetition < 1:
        raise ArtifactBenchmarkValidationError(f"{prefix}: repetition must be positive")
    _validate_operation(
        item.write,
        expected_operation="write_run",
        prefix=f"{prefix} write",
    )
    _validate_operation(
        item.read,
        expected_operation="read_run",
        prefix=f"{prefix} read",
    )
    _validate_snapshot(
        specification,
        item.written_snapshot,
        prefix=f"{prefix} written artifact",
    )
    _validate_snapshot(
        specification,
        item.read_snapshot,
        prefix=f"{prefix} strict readback",
    )
    if item.written_snapshot.result_fingerprint != item.read_snapshot.result_fingerprint:
        raise ArtifactBenchmarkValidationError(
            f"{prefix}: result fingerprint changed across write/read"
        )


def _summarize_operation(
    measurements: Sequence[OperationMeasurement],
) -> OperationSummary:
    wall_times = [item.wall_time_s for item in measurements]
    peaks = [item.python_peak_bytes for item in measurements]
    return OperationSummary(
        wall_time_median_s=float(statistics.median(wall_times)),
        wall_time_max_s=max(wall_times),
        python_peak_median_bytes=int(statistics.median(peaks)),
        python_peak_max_bytes=max(peaks),
    )


def summarize_case(
    specification: ArtifactBenchmarkCase,
    measurements: Sequence[RoundTripMeasurement],
) -> ArtifactCaseResult:
    """Validate two repetitions and compute report-only write/read aggregates."""

    frozen = tuple(measurements)
    if len(frozen) != REPETITIONS:
        raise ArtifactBenchmarkValidationError(
            f"{specification.case_id}: expected {REPETITIONS} repetitions, got {len(frozen)}"
        )
    if tuple(item.repetition for item in frozen) != tuple(range(1, REPETITIONS + 1)):
        raise ArtifactBenchmarkValidationError(
            f"{specification.case_id}: repetition indices must be 1..{REPETITIONS}"
        )
    for item in frozen:
        validate_round_trip_measurement(specification, item)
    fingerprints = {item.written_snapshot.result_fingerprint for item in frozen}
    if len(fingerprints) != 1:
        raise ArtifactBenchmarkValidationError(
            f"{specification.case_id}: result fingerprint changed across repetitions"
        )
    return ArtifactCaseResult(
        specification=specification,
        measurements=frozen,
        write_summary=_summarize_operation([item.write for item in frozen]),
        read_summary=_summarize_operation([item.read for item in frozen]),
    )


def _measure[T](
    operation: OperationName,
    action: Callable[[], T],
) -> tuple[T, OperationMeasurement]:
    gc.collect()
    tracemalloc.start()
    try:
        started = time.perf_counter()
        value = action()
        wall_time_s = time.perf_counter() - started
        _, python_peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return value, OperationMeasurement(
        operation=operation,
        wall_time_s=wall_time_s,
        python_peak_bytes=python_peak_bytes,
    )


def _measure_write_run(
    request: RunRequest,
    payload: ExecutionPayload,
    output_root: Path,
    input_resources: Mapping[str, bytes],
) -> tuple[RunRecord, OperationMeasurement]:
    return _measure(
        "write_run",
        lambda: write_run(
            request,
            payload,
            output_root,
            input_resources=input_resources,
        ),
    )


def _bind_input_sources(
    payload: ExecutionPayload,
    input_resources: Mapping[str, bytes],
) -> ExecutionPayload:
    if tuple(input_resources) != EXPECTED_INPUT_RESOURCE_IDS:
        raise ArtifactBenchmarkValidationError(
            "artifact benchmark requires the fixed ordered set of 11 packaged inputs"
        )
    if len(input_resources) != 11:
        raise ArtifactBenchmarkValidationError(
            "artifact benchmark requires exactly 11 packaged inputs"
        )
    resource_fingerprints = {
        resource_id: hashlib.sha256(data).hexdigest()
        for resource_id, data in input_resources.items()
    }
    for resource_id, digest in resource_fingerprints.items():
        supplied = payload.source_fingerprints.get(resource_id)
        if supplied is not None and supplied != digest:
            raise ArtifactBenchmarkValidationError(
                f"execution source fingerprint differs for {resource_id!r}"
            )
    return replace(
        payload,
        source_fingerprints={
            **resource_fingerprints,
            **dict(payload.source_fingerprints),
        },
    )


def _snapshot_record(record: RunRecord) -> ArtifactSnapshot:
    input_artifact_ids = {
        artifact_id.removeprefix("input.")
        for artifact_id in record.manifest.artifacts
        if artifact_id.startswith("input.")
    }
    if input_artifact_ids != set(EXPECTED_INPUT_RESOURCE_IDS):
        raise ArtifactBenchmarkValidationError(
            "run manifest does not contain exactly the fixed 11 packaged inputs"
        )
    for resource_id in EXPECTED_INPUT_RESOURCE_IDS:
        descriptor = record.manifest.artifacts[f"input.{resource_id}"]
        if record.payload.source_fingerprints.get(resource_id) != descriptor.sha256:
            raise ArtifactBenchmarkValidationError(
                f"payload source fingerprint differs for {resource_id!r}"
            )
    return ArtifactSnapshot(
        runtime_status=record.payload.runtime_status,
        manifest_runtime_status=cast(
            RuntimeStatus,
            record.manifest.runtime_status,
        ),
        sample_count=len(record.payload.timeseries),
        duration_s=record.payload.duration_s,
        time_step_s=record.payload.time_step_s,
        result_fingerprint=record.payload.result_fingerprint,
        manifest_result_fingerprint=record.manifest.result_fingerprint,
        input_resource_ids=EXPECTED_INPUT_RESOURCE_IDS,
    )


def benchmark_repetition(
    specification: ArtifactBenchmarkCase,
    repetition: int,
) -> RoundTripMeasurement:
    """Execute outside tracing, then separately measure write and strict readback."""

    request = load_preset(specification.preset_id)
    payload = execute(request)

    # The fixed 11 packaged resources and their payload bindings are prepared
    # before tracemalloc starts, so the write metric covers publication only.
    input_resources = runtime_input_resources()
    payload = _bind_input_sources(payload, input_resources)

    with tempfile.TemporaryDirectory(prefix="m7-artifact-io-") as temporary:
        output_root = Path(temporary) / "runs"

        written, write_measurement = _measure_write_run(
            request,
            payload,
            output_root,
            input_resources,
        )
        written_snapshot = _snapshot_record(written)
        run_dir = written.run_dir

        # Do not let the original 7201-sample payload or writer record remain
        # live while read_run reconstructs and validates its own full payload.
        del payload
        del written
        del input_resources
        gc.collect()

        readback, read_measurement = _measure(
            "read_run",
            lambda: read_run(run_dir),
        )
        read_snapshot = _snapshot_record(readback)
        del readback

    measurement = RoundTripMeasurement(
        repetition=repetition,
        write=write_measurement,
        read=read_measurement,
        written_snapshot=written_snapshot,
        read_snapshot=read_snapshot,
    )
    validate_round_trip_measurement(specification, measurement)
    return measurement


def benchmark_case(specification: ArtifactBenchmarkCase) -> ArtifactCaseResult:
    """Run the fixed two artifact round trips for one two-hour preset."""

    measurements = tuple(
        benchmark_repetition(specification, repetition) for repetition in range(1, REPETITIONS + 1)
    )
    return summarize_case(specification, measurements)


def report_fingerprint(document: Mapping[str, object]) -> str:
    """Compute the project-standard fingerprint excluding the signature field."""

    unsigned = dict(document)
    unsigned.pop("report_fingerprint", None)
    return canonical_fingerprint(unsigned)


def finalize_document(document: Mapping[str, object]) -> dict[str, object]:
    """Attach a self-verifying fingerprint to a report document."""

    finalized = dict(document)
    finalized.pop("report_fingerprint", None)
    finalized["report_fingerprint"] = report_fingerprint(finalized)
    return finalized


def verify_document(document: Mapping[str, object]) -> None:
    """Raise when a report's self-fingerprint is absent, malformed, or stale."""

    supplied = document.get("report_fingerprint")
    if not isinstance(supplied, str) or not _DIGEST.fullmatch(supplied):
        raise ArtifactBenchmarkValidationError("report fingerprint is missing or malformed")
    if supplied != report_fingerprint(document):
        raise ArtifactBenchmarkValidationError("report fingerprint mismatch")


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def build_document(results: Sequence[ArtifactCaseResult]) -> dict[str, object]:
    """Build and self-sign the complete artifact I/O baseline document."""

    frozen = tuple(results)
    if tuple(result.specification for result in frozen) != ARTIFACT_BENCHMARK_CASES:
        raise ArtifactBenchmarkValidationError(
            "artifact results do not exactly cover the frozen ordered case set"
        )
    for result in frozen:
        summarize_case(result.specification, result.measurements)
    environment = dict(runtime_environment())
    source_tree_sha256 = installed_source_tree_sha256()
    environment_fingerprint = canonical_fingerprint(
        {
            "environment": environment,
            "installed_source_tree_sha256": source_tree_sha256,
        }
    )
    return finalize_document(
        {
            "schema_version": ARTIFACT_BENCHMARK_SCHEMA_VERSION,
            "benchmark_version": ARTIFACT_BENCHMARK_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "generated_at_utc": _utc_now(),
            "status": "success",
            "case_count": len(frozen),
            "repetitions_per_case": REPETITIONS,
            "input_resource_count": len(EXPECTED_INPUT_RESOURCE_IDS),
            "input_resource_ids": list(EXPECTED_INPUT_RESOURCE_IDS),
            "measurement_scope": {
                "model_preparation": (
                    "execute(load_preset(preset_id)) completes before either measurement region"
                ),
                "write_entrypoint": "write_run(request, payload, output_root, ...)",
                "write_preparation": (
                    "the full payload and fixed 11 packaged input byte strings are "
                    "loaded and bound before tracemalloc starts"
                ),
                "read_entrypoint": "read_run(run_dir)",
                "read_preparation": (
                    "the original payload, writer RunRecord, and input byte mapping "
                    "are deleted and garbage collection completes before tracing"
                ),
                "clock": "time.perf_counter wall clock",
                "memory_metric": "tracemalloc peak traced bytes",
                "memory_limit": (
                    "tracemalloc observes Python-traced allocations only; it is not "
                    "whole-process RSS and may exclude native allocations"
                ),
                "process_model": (
                    "two ordered repetitions per case in one process; temporary run "
                    "directories are removed after strict readback"
                ),
                "threshold_policy": (
                    "report-only environment-specific baseline; no cross-machine "
                    "performance pass threshold"
                ),
            },
            "environment": environment,
            "installed_source_tree_sha256": source_tree_sha256,
            "environment_fingerprint": environment_fingerprint,
            "cases": [result.as_dict() for result in frozen],
        }
    )


def _object_mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ArtifactBenchmarkValidationError(f"{context} must be an object")
    return cast(Mapping[str, object], value)


def _object_list(value: object, *, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ArtifactBenchmarkValidationError(f"{context} must be a list")
    return cast(list[object], value)


def _mib(byte_count: int) -> float:
    return byte_count / (1024.0 * 1024.0)


def build_markdown(document: Mapping[str, object], json_name: str) -> str:
    """Render a compact human-readable view of a verified JSON report."""

    verify_document(document)
    raw_cases = _object_list(document.get("cases"), context="report cases")
    environment = _object_mapping(
        document.get("environment"),
        context="report environment",
    )
    lines = [
        "# M7 完整 2 h Artifact I/O 内存基线",
        "",
        (
            "> 本报告由 `scripts/cdu/benchmark_m7_artifacts.py` 从固定 M3/M4 "
            f"2 h 预设生成；机器可读结果为 `{json_name}`。"
        ),
        "",
        "## 结论与边界",
        "",
        f"- 生成时间（UTC）：`{document['generated_at_utc']}`",
        "- 两个场景各执行 2 次完整运行包写出和严格读回；每次均核对 7201 个样本、7200 s / 1 s 网格、结果指纹和 11 项输入。",
        "- 模型 `execute` 在计时和内存测量区间外完成；写出前也已加载 11 项包内输入。",
        "- 读回前已删除原始完整 payload、writer 记录和输入字节映射，并显式执行垃圾回收。",
        "- 内存值为 `tracemalloc` 观测的 Python 分配峰值，不是进程 RSS，可能不含原生库分配。",
        "- 这是当前机器的报告型基线，不设置脱离环境的跨机器硬性能阈值。",
        "",
        "## 运行环境",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
    ]
    for key in sorted(environment):
        lines.append(f"| `{key}` | `{environment[key]}` |")
    lines.extend(
        [
            f"| `installed_source_tree_sha256` | `{document['installed_source_tree_sha256']}` |",
            f"| `environment_fingerprint` | `{document['environment_fingerprint']}` |",
            "",
            "## 两次运行汇总",
            "",
            "| 层级 | 预设 | 操作 | Wall 中位 / s | Wall 最大 / s | Python 峰值中位 / MiB | Python 峰值最大 / MiB |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for raw_case_value in raw_cases:
        raw_case = _object_mapping(raw_case_value, context="report case")
        summary = _object_mapping(raw_case.get("summary"), context="case summary")
        for operation in ("write_run", "read_run"):
            operation_summary = _object_mapping(
                summary.get(operation),
                context=f"{operation} summary",
            )
            wall = _object_mapping(
                operation_summary.get("wall_time_s"),
                context=f"{operation} wall summary",
            )
            peak = _object_mapping(
                operation_summary.get("python_peak_bytes"),
                context=f"{operation} peak summary",
            )
            lines.append(
                f"| {raw_case['model_layer']} | `{raw_case['preset_id']}` | "
                f"`{operation}` | {float(cast(float, wall['median'])):.6f} | "
                f"{float(cast(float, wall['max'])):.6f} | "
                f"{_mib(int(cast(int, peak['median']))):.3f} | "
                f"{_mib(int(cast(int, peak['max']))):.3f} |"
            )
    lines.extend(
        [
            "",
            "## 单次测量明细",
            "",
            "| 层级 | 重复 | 操作 | Wall / s | Python 峰值 / bytes | Python 峰值 / MiB | 结果指纹 |",
            "| --- | ---: | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for raw_case_value in raw_cases:
        raw_case = _object_mapping(raw_case_value, context="report case")
        repetitions = _object_list(
            raw_case.get("repetitions"),
            context="case repetitions",
        )
        for raw_repetition_value in repetitions:
            raw_repetition = _object_mapping(
                raw_repetition_value,
                context="case repetition",
            )
            artifact = _object_mapping(
                raw_repetition.get("written_artifact"),
                context="written artifact snapshot",
            )
            for operation in ("write", "read"):
                operation_measurement = _object_mapping(
                    raw_repetition.get(operation),
                    context=f"{operation} measurement",
                )
                peak_bytes = int(cast(int, operation_measurement["python_peak_bytes"]))
                lines.append(
                    f"| {raw_case['model_layer']} | {raw_repetition['repetition']} | "
                    f"`{operation_measurement['operation']}` | "
                    f"{float(cast(float, operation_measurement['wall_time_s'])):.6f} | "
                    f"{peak_bytes} | {_mib(peak_bytes):.3f} | "
                    f"`{artifact['result_fingerprint']}` |"
                )
    lines.extend(
        [
            "",
            "## 完整性",
            "",
            f"- JSON schema：`{document['schema_version']}`",
            f"- 基线版本：`{document['benchmark_version']}`",
            f"- Runtime 版本：`{document['runtime_version']}`",
            f"- 报告自身指纹：`{document['report_fingerprint']}`",
            "",
            "只有 JSON 去除 `report_fingerprint` 后的项目规范指纹与上述值一致时，本报告才完整。",
            "",
        ]
    )
    return "\n".join(lines)


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(document),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _verify_report_pair(
    document: Mapping[str, object],
    json_path: Path,
    markdown_path: Path,
    expected_markdown: str,
    expected_json_name: str,
) -> None:
    try:
        loaded_value = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactBenchmarkValidationError(
            f"cannot reload artifact benchmark JSON: {exc}"
        ) from exc
    loaded = _object_mapping(loaded_value, context="artifact benchmark JSON")
    verify_document(loaded)
    if canonical_fingerprint(loaded) != canonical_fingerprint(document):
        raise ArtifactBenchmarkValidationError(
            "reloaded artifact benchmark JSON differs from the source document"
        )
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ArtifactBenchmarkValidationError(
            f"cannot reload artifact benchmark Markdown: {exc}"
        ) from exc
    if markdown != expected_markdown:
        raise ArtifactBenchmarkValidationError(
            "reloaded artifact benchmark Markdown differs from staged content"
        )
    fingerprint = cast(str, document["report_fingerprint"])
    if fingerprint not in markdown or expected_json_name not in markdown:
        raise ArtifactBenchmarkValidationError(
            "artifact benchmark Markdown is not bound to the JSON report"
        )


def publish_outputs(
    document: Mapping[str, object],
    json_path: Path,
    markdown_path: Path,
) -> None:
    """Stage, verify, and transactionally publish the JSON/Markdown pair."""

    verify_document(document)
    if json_path.resolve() == markdown_path.resolve():
        raise ArtifactBenchmarkValidationError("JSON and Markdown output paths must differ")
    markdown = build_markdown(document, json_path.name)
    contents = {
        "json": _json_bytes(document),
        "markdown": markdown.encode("utf-8"),
    }
    targets = {"json": json_path, "markdown": markdown_path}
    token = uuid.uuid4().hex
    staged: dict[str, Path] = {}
    try:
        for name in ("json", "markdown"):
            target = targets[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not target.is_file():
                raise ArtifactBenchmarkValidationError(
                    f"artifact benchmark target is not a file: {target}"
                )
            stage = target.with_name(f".{target.name}.{token}.stage")
            with stage.open("xb") as handle:
                handle.write(contents[name])
            if stage.read_bytes() != contents[name]:
                raise OSError(f"staged artifact benchmark {name} differs")
            staged[name] = stage
        _verify_report_pair(
            document,
            staged["json"],
            staged["markdown"],
            markdown,
            json_path.name,
        )
    except Exception:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise

    backups: dict[str, Path] = {}
    published: list[str] = []
    publication_succeeded = False
    try:
        for name in ("json", "markdown"):
            target = targets[name]
            if target.exists():
                backup = target.with_name(f".{target.name}.{token}.backup")
                target.replace(backup)
                backups[name] = backup
        for name in ("json", "markdown"):
            staged[name].replace(targets[name])
            published.append(name)
        _verify_report_pair(
            document,
            json_path,
            markdown_path,
            markdown,
            json_path.name,
        )
        publication_succeeded = True
    except Exception as publication_error:
        rollback_errors: list[str] = []
        for name in reversed(published):
            try:
                targets[name].unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem failure path
                rollback_errors.append(f"remove {name}: {exc}")
        for name in ("markdown", "json"):
            prior_backup = backups.get(name)
            if prior_backup is not None and prior_backup.exists():
                try:
                    prior_backup.replace(targets[name])
                except OSError as exc:  # pragma: no cover - filesystem failure path
                    rollback_errors.append(f"restore {name}: {exc}")
        if rollback_errors:
            raise RuntimeError(
                "artifact benchmark rollback was incomplete: " + "; ".join(rollback_errors)
            ) from publication_error
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        if publication_succeeded:
            for path in backups.values():
                path.unlink(missing_ok=True)


def generate_baseline(
    json_path: Path,
    markdown_path: Path,
) -> dict[str, object]:
    """Run all four long preparations and publish only a fully valid report."""

    results = tuple(benchmark_case(specification) for specification in ARTIFACT_BENCHMARK_CASES)
    document = build_document(results)
    publish_outputs(document, json_path, markdown_path)
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON_PATH)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    document = generate_baseline(args.json, args.markdown)
    print(
        json.dumps(
            {
                "status": document["status"],
                "report_fingerprint": document["report_fingerprint"],
                "json": str(args.json.resolve()),
                "markdown": str(args.markdown.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
