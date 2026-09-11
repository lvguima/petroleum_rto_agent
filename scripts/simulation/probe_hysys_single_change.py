"""Run the fixed T-39 +0.1 C integration check through the formal single-point API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from petroleum_rto.simulation.baseline import read_baseline
from petroleum_rto.simulation.point import run_t39_point
from petroleum_rto.simulation.point_evidence import read_point_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    baseline = read_baseline(args.baseline)
    goal = next(item.goal for item in baseline.snapshot.specifications if item.name == "T-39")
    if goal is None:
        raise ValueError("The frozen baseline has no known T-39 temperature goal")
    path = run_t39_point(args.baseline, goal + 0.1, args.output)
    result = read_point_result(path)
    print(json.dumps({"status": result.status, "report_path": str(path)}))
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
