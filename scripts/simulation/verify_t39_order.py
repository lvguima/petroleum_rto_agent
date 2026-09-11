"""Verify the fixed A→B→B→A order for T-39 using independent frozen-baseline point runs."""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from petroleum_rto.simulation.baseline import (
    _file_bytes,
    _json,
    _plain_directory,
    _sha256,
    read_baseline,
)
from petroleum_rto.simulation.comparison import compare_operating_points
from petroleum_rto.simulation.hysys import load_catalog
from petroleum_rto.simulation.point import run_t39_point
from petroleum_rto.simulation.point_evidence import (
    SPECIFICATION,
    PointResult,
    _bindings_match,
    _qualified,
    read_point_result,
)


def verify_t39_order(baseline_manifest: Path, output_dir: Path) -> Path:
    """Run only baseline Goal and Goal+0.1 C, without retries or solver-option changes.

    A failed point or changed evidence stops subsequent runs. Repeatability compares
    the two complete A results and the two complete B results with zero tolerance.
    This verifies visible observations in this order, not an optimization range.
    """
    manifest = baseline_manifest.expanduser().absolute()
    baseline = read_baseline(manifest)
    frozen = {
        path: _sha256(_file_bytes(path))
        for path in (
            manifest,
            baseline.directory / "baseline.hsc",
            baseline.directory / "snapshot.json",
            baseline.catalog_path,
        )
    }
    catalog = load_catalog(baseline.catalog_path)
    if not _bindings_match(catalog, baseline.snapshot) or not _qualified(
        catalog, baseline.snapshot
    ):
        raise ValueError("The frozen baseline does not qualify the T-39 point input")
    a = next(item.goal for item in baseline.snapshot.specifications if item.name == SPECIFICATION)
    if a is None or not math.isfinite(a):
        raise ValueError("The frozen T-39 goal must be finite")
    b = a + 0.1
    if not math.isfinite(b) or b <= a:
        raise ValueError("The fixed +0.1 C step must produce a distinct finite B target")

    def verify_frozen() -> None:
        if any(_sha256(_file_bytes(path)) != digest for path, digest in frozen.items()):
            raise ValueError("Frozen baseline files changed during order verification")
        if read_baseline(manifest) != baseline:
            raise ValueError("The frozen baseline observation changed")
        if frozen[baseline.directory / "baseline.hsc"] != baseline.baseline_sha256:
            raise ValueError("The frozen case identity changed during input validation")

    verify_frozen()
    directory = output_dir.expanduser().absolute()
    _plain_directory(directory.parent)
    directory.mkdir(exist_ok=False)
    sequence = ("A", "B", "B", "A")
    targets = {"A": a, "B": b}
    report: dict[str, Any] = {
        "schema_id": "hysys-t39-order-verification",
        "schema_version": "1.0.0",
        "status": "failed",
        "eligible_for_optimization": False,
        "isolation": "separate_cases_same_application",
        "baseline_manifest": {"path": str(manifest), "sha256": frozen[manifest]},
        "baseline_sha256": baseline.baseline_sha256,
        "frozen_files": {path.name: digest for path, digest in frozen.items()},
        "execution_order": list(sequence),
        "targets_c": targets,
        "runs": [],
        "comparisons": {"A_A": None, "B_B": None},
        "errors": [],
    }
    completed: list[tuple[Path, str, PointResult]] = []

    def checked_result(path: Path, digest: str, target: float) -> PointResult:
        payload = _file_bytes(path)
        if _sha256(payload) != digest:
            raise ValueError("A point result report changed")
        result = read_point_result(path)
        if _sha256(_file_bytes(path)) != digest:
            raise ValueError("A point result report changed during strict reload")
        if (
            result.baseline != baseline.snapshot
            or result.baseline_sha256 != baseline.baseline_sha256
            or result.target_c != target
            or _sha256(_file_bytes(path.parent / "variables.json")) != frozen[baseline.catalog_path]
            or _json(payload)["runtime_observations"]["baseline_manifest_sha256"]
            != frozen[manifest]
        ):
            raise ValueError(
                "Point evidence does not use the same frozen input and requested target"
            )
        if result.status != "passed" or result.changed is None:
            raise ValueError("Point execution or its recovery/source protection failed")
        return result

    phase = "verify-frozen-input"
    point_index = None
    try:
        for index, label in enumerate(sequence, 1):
            point_index = index
            phase = "verify-prior-evidence"
            verify_frozen()
            for path, digest, previous in completed:
                checked_result(path, digest, previous.target_c)
            child = directory / f"{index:02d}-{label}"
            if os.path.lexists(child):
                raise FileExistsError(f"Point output directory already exists: {child}")
            result_path = child / "report.json"
            entry: dict[str, Any] = {
                "index": index,
                "label": label,
                "target_c": targets[label],
                "directory": str(child),
                "result_path": str(result_path),
                "result_sha256": None,
                "status": "not-completed",
                "point_errors": [],
            }
            report["runs"].append(entry)
            phase = "run-point"
            returned = run_t39_point(manifest, targets[label], child)
            if returned.expanduser().absolute() != result_path:
                raise ValueError("Point API returned an unexpected result path")
            phase = "strict-reload-point"
            digest = _sha256(_file_bytes(result_path))
            entry["result_sha256"] = digest
            observed = read_point_result(result_path)
            entry["status"] = observed.status
            entry["point_errors"] = [asdict(error) for error in observed.errors]
            observed = checked_result(result_path, digest, targets[label])
            completed.append((result_path, digest, observed))
            phase = "verify-frozen-input-after-point"
            verify_frozen()
        phase = "verify-final-evidence"
        point_index = None
        verify_frozen()
        results = [checked_result(path, digest, item.target_c) for path, digest, item in completed]
        phase = "compare-repeated-points"
        for label, first, second in (("A_A", 0, 3), ("B_B", 1, 2)):
            first_changed, second_changed = results[first].changed, results[second].changed
            assert first_changed is not None and second_changed is not None
            report["comparisons"][label] = compare_operating_points(first_changed, second_changed)
        if all(value["equivalent"] for value in report["comparisons"].values()):
            report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - preserve failed order and stop
        report["errors"].append(
            {
                "phase": phase,
                "point_index": point_index,
                "exception_type": type(error).__name__,
                "message": str(error),
            }
        )
    path = directory / "report.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    path = verify_t39_order(args.baseline, args.output)
    status = json.loads(path.read_text(encoding="utf-8"))["status"]
    print(json.dumps({"status": status, "report_path": str(path)}))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
