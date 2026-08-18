"""Generate the formal M7 in-process performance baseline.

The timed region is deliberately limited to the stable runtime call
``execute(load_preset(...))``.  Run-directory creation, JSONL output, manifest
hashing, and report serialization are outside the measurement.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import re
import statistics
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from petroleum_rto.cdu.core.config import canonical_fingerprint
from petroleum_rto.cdu.runtime.contracts import RUNTIME_VERSION, RuntimeStatus
from petroleum_rto.cdu.runtime.executor import execute
from petroleum_rto.cdu.runtime.presets import load_preset
from petroleum_rto.cdu.runtime.provenance import (
    installed_source_tree_sha256,
    runtime_environment,
)

BENCHMARK_SCHEMA_VERSION = "m7.performance-baseline.schema-v1"
BENCHMARK_VERSION = "m7-performance-baseline-v0.1.0"
REPETITIONS = 3
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_PATH = (
    _PROJECT_ROOT / "reports" / "modeling" / "m7_performance_baseline_v0.1.0.json"
)
DEFAULT_MARKDOWN_PATH = (
    _PROJECT_ROOT / "reports" / "modeling" / "M7_PERFORMANCE_BASELINE.md"
)


class BenchmarkValidationError(RuntimeError):
    """Raised when a runtime outcome cannot be published as a valid baseline."""


@dataclass(frozen=True)
class BenchmarkCase:
    """Frozen acceptance contract for one representative M7 execution."""

    case_id: str
    model_layer: str
    preset_id: str
    expected_status: RuntimeStatus
    expected_sample_count: int
    expected_duration_s: float | None
    expected_time_step_s: float | None
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
class RunMeasurement:
    """One measured execution after all publication checks have passed."""

    repetition: int
    runtime_status: RuntimeStatus
    wall_time_s: float
    python_peak_bytes: int
    sample_count: int
    duration_s: float | None
    time_step_s: float | None
    result_fingerprint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "repetition": self.repetition,
            "runtime_status": self.runtime_status,
            "wall_time_s": self.wall_time_s,
            "python_peak_bytes": self.python_peak_bytes,
            "sample_count": self.sample_count,
            "duration_s": self.duration_s,
            "time_step_s": self.time_step_s,
            "result_fingerprint": self.result_fingerprint,
        }


@dataclass(frozen=True)
class CaseSummary:
    """Three-run aggregate retained in the machine-readable report."""

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
class CaseResult:
    """Validated measurements and their deterministic aggregate."""

    specification: BenchmarkCase
    measurements: tuple[RunMeasurement, ...]
    summary: CaseSummary

    def as_dict(self) -> dict[str, object]:
        return {
            **self.specification.as_dict(),
            "repetitions": [item.as_dict() for item in self.measurements],
            "summary": self.summary.as_dict(),
            "result_fingerprint": self.measurements[0].result_fingerprint,
        }


BENCHMARK_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        case_id="m2_steady_baseline",
        model_layer="M2",
        preset_id="steady-baseline",
        expected_status="success",
        expected_sample_count=0,
        expected_duration_s=None,
        expected_time_step_s=None,
        description="M2 source-closed steady recycle baseline.",
    ),
    BenchmarkCase(
        case_id="m3_open_loop_feed_plus_5pct_2h",
        model_layer="M3",
        preset_id="open-loop-feed-step",
        expected_status="success",
        expected_sample_count=7201,
        expected_duration_s=7200.0,
        expected_time_step_s=1.0,
        description="M3 two-hour open-loop +5% fresh-feed command step.",
    ),
    BenchmarkCase(
        case_id="m4_closed_loop_feed_plus_5pct_2h",
        model_layer="M4",
        preset_id="closed-loop-feed-step",
        expected_status="success",
        expected_sample_count=7201,
        expected_duration_s=7200.0,
        expected_time_step_s=1.0,
        description="M4 two-hour closed-loop +5% feed-setpoint step.",
    ),
    BenchmarkCase(
        case_id="m6_portable_pump_around_1_trip",
        model_layer="M6",
        preset_id="m6-abnormal-pump-trip",
        expected_status="limited",
        expected_sample_count=601,
        expected_duration_s=600.0,
        expected_time_step_s=1.0,
        description=(
            "Selected portable M6 pump-around-1 trip replay; this is not the full "
            "source-closed M6 validation matrix."
        ),
    ),
)


def _same_optional_number(actual: float | None, expected: float | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return math.isclose(actual, expected, rel_tol=0.0, abs_tol=1.0e-12)


def validate_measurement(specification: BenchmarkCase, item: RunMeasurement) -> None:
    """Reject any outcome that would make a success report misleading."""

    prefix = f"{specification.case_id} repetition {item.repetition}"
    if item.repetition < 1:
        raise BenchmarkValidationError(f"{prefix}: repetition must be positive")
    if item.runtime_status != specification.expected_status:
        raise BenchmarkValidationError(
            f"{prefix}: expected status {specification.expected_status!r}, "
            f"got {item.runtime_status!r}"
        )
    if item.sample_count != specification.expected_sample_count:
        raise BenchmarkValidationError(
            f"{prefix}: expected {specification.expected_sample_count} samples, "
            f"got {item.sample_count}"
        )
    if not _same_optional_number(
        item.duration_s, specification.expected_duration_s
    ):
        raise BenchmarkValidationError(
            f"{prefix}: unexpected duration {item.duration_s!r}"
        )
    if not _same_optional_number(
        item.time_step_s, specification.expected_time_step_s
    ):
        raise BenchmarkValidationError(
            f"{prefix}: unexpected time step {item.time_step_s!r}"
        )
    if not math.isfinite(item.wall_time_s) or item.wall_time_s <= 0.0:
        raise BenchmarkValidationError(f"{prefix}: wall time must be positive")
    if item.python_peak_bytes <= 0:
        raise BenchmarkValidationError(
            f"{prefix}: tracemalloc Python peak must be positive"
        )
    if not _DIGEST.fullmatch(item.result_fingerprint):
        raise BenchmarkValidationError(
            f"{prefix}: result fingerprint must be a lowercase SHA-256 digest"
        )


def summarize_case(
    specification: BenchmarkCase,
    measurements: Sequence[RunMeasurement],
) -> CaseResult:
    """Validate exactly three repetitions and compute median/max aggregates."""

    frozen = tuple(measurements)
    if len(frozen) != REPETITIONS:
        raise BenchmarkValidationError(
            f"{specification.case_id}: expected {REPETITIONS} repetitions, "
            f"got {len(frozen)}"
        )
    if tuple(item.repetition for item in frozen) != tuple(
        range(1, REPETITIONS + 1)
    ):
        raise BenchmarkValidationError(
            f"{specification.case_id}: repetition indices must be 1..{REPETITIONS}"
        )
    for item in frozen:
        validate_measurement(specification, item)
    fingerprints = {item.result_fingerprint for item in frozen}
    if len(fingerprints) != 1:
        raise BenchmarkValidationError(
            f"{specification.case_id}: result fingerprint changed across repetitions"
        )
    wall_times = [item.wall_time_s for item in frozen]
    peaks = [item.python_peak_bytes for item in frozen]
    summary = CaseSummary(
        wall_time_median_s=float(statistics.median(wall_times)),
        wall_time_max_s=max(wall_times),
        python_peak_median_bytes=int(statistics.median(peaks)),
        python_peak_max_bytes=max(peaks),
    )
    return CaseResult(specification, frozen, summary)


def benchmark_case(specification: BenchmarkCase) -> CaseResult:
    """Measure one frozen preset three times through the unified executor."""

    measurements: list[RunMeasurement] = []
    for repetition in range(1, REPETITIONS + 1):
        gc.collect()
        tracemalloc.start()
        try:
            started = time.perf_counter()
            outcome = execute(load_preset(specification.preset_id))
            wall_time_s = time.perf_counter() - started
            _, python_peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        measurement = RunMeasurement(
            repetition=repetition,
            runtime_status=outcome.runtime_status,
            wall_time_s=wall_time_s,
            python_peak_bytes=python_peak_bytes,
            sample_count=len(outcome.timeseries),
            duration_s=outcome.duration_s,
            time_step_s=outcome.time_step_s,
            result_fingerprint=outcome.result_fingerprint,
        )
        validate_measurement(specification, measurement)
        measurements.append(measurement)
        del outcome
    return summarize_case(specification, measurements)


def report_fingerprint(document: Mapping[str, object]) -> str:
    """Compute the project-standard fingerprint excluding the field itself."""

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
    """Raise when the supplied report fingerprint is missing or invalid."""

    supplied = document.get("report_fingerprint")
    if not isinstance(supplied, str) or not _DIGEST.fullmatch(supplied):
        raise BenchmarkValidationError("report fingerprint is missing or malformed")
    expected = report_fingerprint(document)
    if supplied != expected:
        raise BenchmarkValidationError("report fingerprint mismatch")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def build_document(results: Sequence[CaseResult]) -> dict[str, object]:
    """Build and self-sign the formal machine-readable baseline."""

    frozen = tuple(results)
    if tuple(result.specification for result in frozen) != BENCHMARK_CASES:
        raise BenchmarkValidationError(
            "benchmark results do not exactly cover the frozen ordered case set"
        )
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
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "benchmark_version": BENCHMARK_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "generated_at_utc": _utc_now(),
            "status": "success",
            "case_count": len(frozen),
            "repetitions_per_case": REPETITIONS,
            "measurement_scope": {
                "entrypoint": "execute(load_preset(preset_id))",
                "clock": "time.perf_counter wall clock",
                "included": (
                    "fixed preset loading, packaged resource loading, model execution, "
                    "and in-memory result construction"
                ),
                "excluded": (
                    "run artifact JSONL/JSON/manifest I/O, report serialization, and "
                    "result fingerprint calculation"
                ),
                "memory_metric": "tracemalloc peak traced bytes",
                "memory_limit": (
                    "tracemalloc observes Python-traced allocations only; it is not "
                    "whole-process RSS and may exclude native allocations"
                ),
                "process_model": "three ordered repetitions per case in one process",
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


def _mib(byte_count: int) -> float:
    return byte_count / (1024.0 * 1024.0)


def build_markdown(document: Mapping[str, object], json_name: str) -> str:
    """Render a compact human-readable view of a verified JSON baseline."""

    verify_document(document)
    raw_cases = document["cases"]
    if not isinstance(raw_cases, list):
        raise BenchmarkValidationError("report cases must be a list")
    environment = document["environment"]
    if not isinstance(environment, dict):
        raise BenchmarkValidationError("report environment must be an object")

    lines = [
        "# M7 正式性能基线",
        "",
        (
            "> 本报告由 `scripts/benchmark_m7.py` 从统一运行入口生成；"
            f"对应机器可读结果为 `{json_name}`。"
        ),
        "",
        "## 结论与测量边界",
        "",
        f"- 生成时间（UTC）：`{document['generated_at_utc']}`",
        f"- 基线状态：`{document['status']}`；4 个代表场景均完成 3 次严格校验运行。",
        "- 计时区间仅为 `execute(load_preset(preset_id))`，不包含运行包 JSONL/JSON/manifest 写盘、报告序列化或结果指纹计算。",
        "- 内存值是 `tracemalloc` 观测到的 Python 分配峰值，不是进程 RSS，可能不包含原生库分配。",
        "- 这是当前机器的报告型基线，不设置脱离环境的跨机器硬性能阈值。",
        "- M6 项是包内选定的泵循环一停运便携复现场景，不等同于重跑完整 source-closed M6 矩阵。",
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
            "## 三次运行汇总",
            "",
            "| 层级 | 预设 | 状态 | 样本数 | 步长 / s | Wall 中位 / s | Wall 最大 / s | Python 峰值中位 / MiB | Python 峰值最大 / MiB |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise BenchmarkValidationError("report case must be an object")
        summary = raw_case["summary"]
        if not isinstance(summary, dict):
            raise BenchmarkValidationError("case summary must be an object")
        wall = summary["wall_time_s"]
        peak = summary["python_peak_bytes"]
        if not isinstance(wall, dict) or not isinstance(peak, dict):
            raise BenchmarkValidationError("case aggregate must be an object")
        peak_median = _mib(int(peak["median"]))
        peak_max = _mib(int(peak["max"]))
        step = raw_case["expected_time_step_s"]
        step_text = "—" if step is None else f"{float(step):.1f}"
        lines.append(
            f"| {raw_case['model_layer']} | `{raw_case['preset_id']}` | "
            f"`{raw_case['expected_status']}` | {raw_case['expected_sample_count']} | "
            f"{step_text} | {float(wall['median']):.6f} | "
            f"{float(wall['max']):.6f} | {peak_median:.3f} | {peak_max:.3f} |"
        )

    lines.extend(
        [
            "",
            "## 单次测量明细",
            "",
            "| 层级 | 重复 | Wall / s | Python 峰值 / bytes | Python 峰值 / MiB | 结果指纹 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise BenchmarkValidationError("report case must be an object")
        repetitions = raw_case["repetitions"]
        if not isinstance(repetitions, list):
            raise BenchmarkValidationError("case repetitions must be a list")
        for raw_run in repetitions:
            if not isinstance(raw_run, dict):
                raise BenchmarkValidationError("case repetition must be an object")
            peak_bytes = int(raw_run["python_peak_bytes"])
            lines.append(
                f"| {raw_case['model_layer']} | {raw_run['repetition']} | "
                f"{float(raw_run['wall_time_s']):.6f} | {peak_bytes} | "
                f"{_mib(peak_bytes):.3f} | `{raw_run['result_fingerprint']}` |"
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
            "只有 JSON 去除 `report_fingerprint` 字段后的项目规范指纹与上述值一致时，本报告才完整。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_outputs(
    document: Mapping[str, object],
    json_path: Path,
    markdown_path: Path,
) -> None:
    verify_document(document)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(
        dict(document),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    markdown_text = build_markdown(document, json_path.name)
    json_temp = json_path.with_name(f".{json_path.name}.tmp")
    markdown_temp = markdown_path.with_name(f".{markdown_path.name}.tmp")
    try:
        json_temp.write_text(json_text, encoding="utf-8", newline="\n")
        markdown_temp.write_text(markdown_text, encoding="utf-8", newline="\n")
        json_temp.replace(json_path)
        markdown_temp.replace(markdown_path)
    finally:
        json_temp.unlink(missing_ok=True)
        markdown_temp.unlink(missing_ok=True)


def generate_baseline(json_path: Path, markdown_path: Path) -> dict[str, object]:
    """Run the complete formal case set and publish only after every check passes."""

    results = tuple(benchmark_case(specification) for specification in BENCHMARK_CASES)
    document = build_document(results)
    _write_outputs(document, json_path, markdown_path)
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
